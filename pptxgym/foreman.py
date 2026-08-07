"""Run one orchestrator per deck, and judge nothing.

This is the run loop the orchestrator architecture leaves behind. The old
`run` sequenced eleven stages per deck and owned every verdict in between;
that judgment now belongs to one agent per deck (`.claude/agents/
orchestrator.md`), and what remains for Python is exactly what a scaling
pipeline still owes:

- **prep** — ingest and inspect are deterministic; a deck arrives at its
  orchestrator with the digest and renders already on disk, the same way the
  old flow prepped them.
- **spawn** — one `claude --agent orchestrator` per deck, through
  `agent.run_agent`, which is where the infra-retry, log-archiving and
  which-model-actually-ran machinery already lives.
- **guard** — the shared tools are fingerprinted before each orchestrator and
  any edit is reverted (or named, when it cannot be) exactly as the repair
  loop did. One repair agent once patched `degrade_exec` mid-run, correctly,
  and silently changed what every other deck would be degraded into. An
  owner owns one deck; the tools are everybody's.
- **collect** — a deck ships when the measurements say so, and the foreman
  *runs* them rather than reading the scoreboard: `score` and `harden`
  re-execute from the artefacts at collect time, the bundle is checked
  against what the instruction promises, and a REVIEW.md must exist. Both
  trial-2 orchestrators overrode a harden stop in state.json — documented,
  and in the event substantively right — but a record an agent can edit is
  not a record a shipping decision can stand on. Re-execution costs a
  minute or two per deck and closes that door for every deck at once.
  Anything that fails parks, with the orchestrator's own last words as the
  reason. Nothing here reads a proposal, weighs a finding or overrides an
  agent; a deck this loop cannot mechanically verify is a deck a human
  looks at.

What is deliberately absent: stage sequencing, rework routing, repair
budgets, and every per-stage gate. The orchestrator's manual carries the
doctrine; this file carries the plumbing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from . import agent as agentmod
from . import pipeline as pl

#: What the orchestrator runs on unless told otherwise. A directive, not a
#: default-by-accident: the session default of whoever launched the foreman
#: is exactly the thing this exists to override.
MODEL = "opus"
EFFORT = "high"

#: Per-specialist model assignment, chosen by where a wrong answer leaks.
#: `reconcile` keeps the top model: an instruction-reality mismatch it misses
#: sails past every hard gate (the deck0006 lesson — gt scored 1.000 while
#: following the instruction scored 0). The other three degrade safely:
#: a bad proposal or recipe is caught by the orchestrator's review and by
#: mechanical verbs seconds later, and the solvability probe *should* match
#: the strength of the policy the tasks will train, not the strongest model
#: available — a task only opus can solve is difficulty inflation.
ASSIGN = {
    "propose": ("sonnet", "high"),
    "recipe": ("sonnet", "medium"),
    "reconcile": ("opus", "high"),
    "solvable": ("sonnet", "high"),
}

#: Wall clock is the stop; turns are not meant to be the binding constraint.
#:
#: Measured over three Jobs runs: an easy deck finishes in 46–72 turns, a
#: middling one in 85–109, and the hard tail wants ~215–230 — deck0008 and
#: deck0009 each burned a full 131-turn budget, parked, and needed 84 and 99
#: more to finish. deck0004 shipped on turn 130 exactly, which is a pass by
#: one turn. Three of eight decks hitting the ceiling means the ceiling is
#: measuring us, not them.
#:
#: 220 turns at the observed ~2 turns/minute lands inside the 120-minute
#: wall, so the deck that is genuinely stuck still stops on the clock — and
#: that is the honest stop, because the budget we actually care about is
#: time. Raising this costs nothing on the decks that do not need it: an
#: orchestrator that finishes in 60 turns bills 60.
MAX_TURNS = 220
TIMEOUT_MIN = 120

#: How many times a stopped orchestrator is handed its own deck back.
#:
#: `codex exec` ends when the model stops calling tools, so an agent that
#: decides it has done enough for now simply exits — three of the first five
#: muse-spark decks stopped at 15-16 minutes, none out of budget, none having
#: written the REVIEW.md the brief asks for, one of them a single `package`
#: away from shipping. claude has `--max-turns` for this; the codex lane had
#: nothing. Handing the work back costs nothing on a deck that finished.
CONTINUATIONS = {"claude": 2, "codex": 5}

#: The orchestrator's toolset. `Task` is the subagent tool's name in the CLI
#: (verified against the session init record of the first trial), and it is
#: the one entry the specialist stages' default list does not carry.
TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "Task"]


def mission(deck: pl.Deck, work: Path, turns: int,
            assign: dict[str, tuple[str, str]], wps: bool = True,
            engine: str = "claude") -> str:
    """The per-deck brief. Doctrine lives in orchestrator.md; this names
    the deck, the boundaries and the budget, and nothing else."""
    meta = deck.meta()
    if engine == "claude":
        lanes = "\n".join(f"      {verb}: --model {m} --effort {e}"
                          for verb, (m, e) in assign.items())
    else:
        # the ASSIGN table speaks claude model names; a codex-lane deck runs
        # every verb on the engine the environment already pins
        lanes = ("      every verb: no --model or --effort flags — this "
                 "deck's lane is set by the environment")
    if not wps:
        lanes += ("\n- This machine has no working WPS round trip: always "
                  "pass --no-wps to harden. The gap travels as a caveat; it "
                  "is not yours to compensate for.")
    return f"""You own one deck end to end.

Work root: {work.resolve()}
Deck: {deck.id} ({meta.get('slides', '?')} slides). ingest and inspect have \
already run; the digest and renders are ready under {deck.root.resolve()}/.

Deliver either a packaged task in {work.resolve()}/emitted/ whose three
measurements hold, or a reasoned no in {deck.root.resolve()}/REVIEW.md. Write
REVIEW.md as you go.

Standing controls:
- When you run a specialist verb, always add exactly the flags for its lane:
{lanes}
- Only {deck.root.resolve()}/ is yours. Other decks' directories, and any
  other work root, are other experiments' evidence — do not read them.
- The pipeline's code and prompts are not yours to change; a defect in a verb
  is a finding for REVIEW.md, not an edit.
- Never git commit, push or checkout.

Your turn budget is {turns}; specialists bill their own. End with three
lines: the task, the three measurement readings, and where REVIEW.md stands."""


# --------------------------------------------------------------------------- #
# the mechanical verdict
# --------------------------------------------------------------------------- #


def shipped(deck: pl.Deck) -> tuple[bool, str]:
    """Does the mechanical record say this deck produced a task?

    The cheap read — used by `pick_decks` to skip finished decks, and by
    `run_deck` as the pre-filter before the expensive part. A pass here is a
    statement about the *record*, which the orchestrator can edit; shipping
    additionally requires `verify`, which the orchestrator cannot.
    """
    # The recorded status, not `done()`, which downgrades a stage whose
    # fingerprints moved. Collect re-executes `harden` on every deck it
    # ships, rewriting the `attacks.json` that `packaged` fingerprints — so
    # `done("packaged")` reads False for exactly the decks that shipped, and
    # a resume sweep re-runs them from scratch. It did: deck0003 spent 118
    # minutes and deck0001 27 re-deriving tasks that were already published,
    # and deck0001's second attempt ended worse than its first.
    if (deck.state().get("packaged") or {}).get("status") != "ok":
        return False, "state.json does not record `packaged: ok`"
    if not (deck.root / "REVIEW.md").exists():
        return False, "no REVIEW.md — the argument is half the deliverable"
    sc = (deck.state().get("scored") or {})
    gt, inp = sc.get("gt"), sc.get("input")
    if gt != 1.0 or inp != 0.0:
        return False, f"scored records gt={gt} input={inp}, not 1.0/0.0"
    gaps = pl.bundle_problems(deck)
    if gaps:
        return False, f"bundle: {gaps[0]}"
    return True, ""


def verify(deck: pl.Deck, wps: bool = True) -> tuple[bool, str]:
    """Re-execute the two cheatable measurements from the artefacts.

    `state.json` is the orchestrator's to correct, and both trial-2
    orchestrators corrected it — over a harden stop, with written reasons,
    and (that time) rightly. The scoreboard being editable is a feature; a
    shipping decision reading it is the bug. So collect runs `score` and
    `harden` itself: same verbs, same artefacts, a minute or two per deck,
    and nothing an agent wrote anywhere changes what they compute. The
    bundle check in `shipped` is already an execution, not a record.
    """
    try:
        sc = pl.score_task(deck)
    except (pl.StageError, OSError, ValueError) as e:
        return False, f"score would not run: {e}"
    gt, inp = sc.get("gt"), sc.get("input")
    if gt != 1.0 or inp != 0.0:
        return False, f"score re-executed: gt={gt} input={inp}, not 1.0/0.0"
    try:
        hd = pl.harden(deck, wps=wps)
    except (pl.StageError, OSError, ValueError) as e:
        return False, f"harden would not run: {e}"
    if hd.get("beaten"):
        return False, ("harden re-executed: beaten by "
                       + ", ".join(hd["beaten"]))
    if hd.get("problems"):
        return False, f"harden re-executed: {hd['problems'][0]}"
    return True, ""


def _last_words(res: dict, log: Path) -> str:
    """The orchestrator's final line, for the park record."""
    txt = (agentmod.last_result(log).get("result") or "").strip()
    if txt:
        return txt[-400:]
    return str(res.get("why") or res.get("status") or "")[:400]


# --------------------------------------------------------------------------- #
# the prefilter: park a doomed deck before it costs agent money
#
# Render *failures* already park at prep — `inspect` raises and no
# orchestrator is spawned. These two checks catch the renders that succeed
# and still lie: a soffice run that produced pages of blank white (the deck
# "rendered" and every downstream judgement would be about an empty image),
# and a deck set in fonts this machine does not carry, where every render
# reflows away from the file the task will actually be graded in. Both are
# facts about the machine-deck pair, cost under a second, and turn a deck
# that would burn an hour of agent time into a parked row with a named
# reason.
# --------------------------------------------------------------------------- #

#: Common Latin faces every renderer substitutes acceptably — carlito for
#: calibri where it exists, metric-similar sans/serif where it does not. The
#: three trial decks were all set in calibri, rendered under substitution on
#: a machine without carlito, shipped, and passed the on-VM smoke test: this
#: class of drift is measured-harmless. What is NOT substitutable is a
#: script the machine has no glyphs for — that renders as tofu boxes and
#: every judgement downstream is about a slide that does not exist.
FONT_SUBSTITUTABLE = {
    "calibri", "calibri light", "cambria", "arial", "arial black",
    "arial narrow", "helvetica", "times new roman", "courier new",
    "century gothic", "verdana", "tahoma", "georgia", "garamond",
    "trebuchet ms", "comic sans ms", "impact", "segoe ui", "segoe ui light",
    "segoe ui semibold", "lato", "open sans", "roboto", "montserrat",
    "book antiqua", "palatino linotype", "franklin gothic book",
    "franklin gothic medium", "gill sans mt", "candara", "corbel",
    "constantia", "consolas", "rockwell", "baskerville old face",
    "lucida sans", "lucida grande", "lucida console", "lucida sans unicode",
}

#: Symbol/decoration faces and theme placeholders: absence does not reflow
#: body text, so absence does not park a deck.
FONT_IGNORE = {"wingdings", "wingdings 2", "wingdings 3", "webdings",
               "symbol", "marlett", "mt extra",
               "+mn-lt", "+mj-lt", "+mn-ea", "+mj-ea", "+mn-cs", "+mj-cs"}

#: CJK faces: covered by any installed CJK-capable family (the runtime image
#: carries fonts-noto-cjk), missing only on a machine with no CJK glyphs at
#: all — which is exactly the tofu case the check exists for.
FONT_CJK = {"microsoft yahei", "microsoft yahei ui", "simsun", "nsimsun",
            "simhei", "kaiti", "fangsong", "dengxian", "microsoft jhenghei",
            "mingliu", "pmingliu", "dfkai-sb", "pingfang sc", "pingfang tc",
            "ms gothic", "ms pgothic", "ms mincho", "ms pmincho", "meiryo",
            "yu gothic", "yu mincho", "malgun gothic", "batang", "gulim",
            "dotum", "stsong", "stkaiti", "stheiti", "stfangsong"}


def _fonts_wanted(pptx: Path) -> set[str]:
    """The typefaces the deck's *slides* actually name, lowercased.

    Slides only, on purpose: theme and master XML declare a face per foreign
    script slot (`<a:font script="Thai" typeface="Angsana New"/>` and thirty
    siblings) whether or not any run uses it, and the first version of this
    check read those and parked all three shipped trial decks over Thai and
    Khmer faces their slides never use. `<a:font>` declarations are stripped
    for the same reason wherever they appear.
    """
    import re
    import zipfile
    out: set[str] = set()
    try:
        with zipfile.ZipFile(pptx) as z:
            for name in z.namelist():
                if not re.fullmatch(r"ppt/slides/[^/]+\.xml", name):
                    continue
                xml = re.sub(rb"<a:font\b[^>]*/>", b"", z.read(name))
                for m in re.finditer(rb'typeface="([^"]{1,80})"', xml):
                    face = m.group(1).decode("utf-8", "replace").strip().lower()
                    if face and face not in FONT_IGNORE:
                        out.add(face)
    except (OSError, zipfile.BadZipFile):
        pass
    return out


def _fonts_installed() -> set[str] | None:
    """Lowercased family names from fontconfig, or None when there is no
    fontconfig to ask — an unanswerable check must not park anything."""
    import subprocess
    try:
        raw = subprocess.run(["fc-list", ":", "family"], capture_output=True,
                             text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    out: set[str] = set()
    for line in raw.splitlines():
        for fam in line.split(","):
            fam = fam.strip().lower()
            if fam:
                out.add(fam)
    return out or None


#: Weight and width suffixes that name a cut, not a family. "Calibri-Light"
#: reflows exactly as far as Calibri does; treating it as a different font
#: parked a real deck on the first Jobs run (as did "Open Sans Light" and
#: "Helvetica Neue" — three of five parks that morning were this class).
_FONT_CUTS = {"light", "thin", "medium", "semibold", "demibold", "bold",
              "black", "heavy", "extra", "ultra", "condensed", "narrow",
              "extended", "italic", "oblique", "regular", "book", "neue",
              "pro", "std", "mt", "ms"}


def _font_family(face: str) -> str:
    """The family a face name points at: lowercased, separators unified,
    trailing cut words stripped."""
    words = [w for w in face.lower().replace("-", " ").replace("_", " ")
             .split() if w]
    while len(words) > 1 and words[-1] in _FONT_CUTS:
        words.pop()
    return " ".join(words)


def missing_fonts(pptx: Path) -> list[str]:
    """Faces the file names that nothing installed can honestly draw.

    Takes a bare path so corpus selection can run the same check before a
    deck ever becomes a `Deck` — the earlier a font gap is caught, the less
    it has already cost."""
    installed = _fonts_installed()
    if installed is None:
        return []
    families = {_font_family(f) for f in installed} | installed
    subst = {_font_family(f) for f in FONT_SUBSTITUTABLE}
    cjk = {_font_family(f) for f in FONT_CJK}
    missing = []
    for face in sorted(_fonts_wanted(pptx)):
        fam = _font_family(face)
        if fam in families or fam in subst:
            continue
        # a cut of a known family: "helvetica neue" is still helvetica
        if any(fam.startswith(s + " ") or fam == s for s in subst):
            continue
        # any CJK face is covered by any installed CJK-capable family
        if fam in cjk and any("cjk" in f for f in families):
            continue
        # a family fontconfig knows by a longer name ("noto sans cjk sc")
        if any(fam in f or f in fam for f in families):
            continue
        missing.append(face)
    return missing


def fonts_missing(deck: pl.Deck) -> list[str]:
    return missing_fonts(deck.source)


def renders_blank(deck: pl.Deck) -> bool:
    """True when every rendered page is (near-)uniform — soffice succeeded
    at producing pages and failed at producing the deck."""
    try:
        from PIL import Image, ImageStat
    except ImportError:
        return False
    pages = sorted(deck.root.glob("renders/*.png"))
    if not pages:
        return False
    for page in pages:
        try:
            with Image.open(page) as im:
                stat = ImageStat.Stat(im.convert("L"))
        except OSError:
            continue
        if stat.stddev[0] > 4.0:        # any real content clears this easily
            return False
    return True


def prefilter(deck: pl.Deck) -> str | None:
    """The reason this deck must not cost agent money, or None."""
    missing = fonts_missing(deck)
    if missing:
        return ("missing fonts, renders will not match the graded file: "
                + ", ".join(missing[:6]))
    if renders_blank(deck):
        return "every rendered page is blank — soffice produced nothing"
    return None


def assignment(args) -> dict[str, tuple[str, str]]:
    """The specialist lanes this run will brief, from defaults and flags.

    `--assign propose=sonnet:high,recipe=haiku:low` edits single lanes;
    `--specialist-model` / `--specialist-effort` override every lane at once
    (the one-model experiments — an all-opus baseline, an all-cheap trial —
    are runs, not code edits).
    """
    out = dict(ASSIGN)
    for part in (getattr(args, "assign", None) or "").split(","):
        if not part.strip():
            continue
        verb, _, spec = part.partition("=")
        m, _, e = spec.partition(":")
        base = out.get(verb.strip(), (MODEL, EFFORT))
        out[verb.strip()] = (m.strip() or base[0], e.strip() or base[1])
    sm = getattr(args, "specialist_model", None)
    se = getattr(args, "specialist_effort", None)
    if sm or se:
        out = {v: (sm or m, se or e) for v, (m, e) in out.items()}
    return out


# --------------------------------------------------------------------------- #
# one deck, prep to verdict
# --------------------------------------------------------------------------- #


def _witness(log, engine: str) -> dict:
    """ran_as, per engine — which model actually did the work."""
    return (agentmod.codex_ran_as(log) if engine == "codex"
            else agentmod.ran_as(log))


async def run_deck(deck: pl.Deck, work: Path, args,
                   engine: str = "claude") -> dict:
    started = time.monotonic()

    def _finish(outcome: str, why: str = "", **extra) -> dict:
        rec = {"deck": deck.id, "outcome": outcome, "why": why,
               "engine": engine,
               "minutes": round((time.monotonic() - started) / 60, 1),
               "at": time.strftime("%Y-%m-%dT%H:%M:%S"), **extra}
        (deck.root / "foreman.json").write_text(json.dumps(rec, indent=1))
        pl.log_event("deck_done", deck=deck.id, outcome=outcome, why=why)
        return rec

    # ---- prep: deterministic, no model ---------------------------------- #
    if not deck.done("inspected"):
        try:
            await asyncio.to_thread(pl.inspect, deck, roundtrip=args.roundtrip)
        except pl.StageError as e:
            return _finish("parked", f"inspect failed — {e}")
    # A deck whose record is already complete ships on re-verification alone
    # — no orchestrator, and no prefilter either (its work is done; this
    # machine's fonts no longer matter). This is what a resume sweep meets:
    # the first Jobs run finished deck0003 end to end and then parked it
    # because its orchestrator spent turn 100 writing the summary; the work
    # was all there, and spawning a fresh agent to re-conclude it would cost
    # real money to learn nothing.
    ok, _ = shipped(deck)
    if ok and not args.force:
        ok, why = await asyncio.to_thread(verify, deck,
                                          getattr(args, "wps", True))
        if ok:
            return _finish("shipped", task=(deck.state().get("packaged")
                           or {}).get("task_id"), agent="record")
        pl.log_event("reverify_failed", deck=deck.id, why=why)

    gap = await asyncio.to_thread(prefilter, deck)
    if gap:
        return _finish("parked", f"prefilter: {gap}", kind="prefilter")

    # ---- spawn: one owner ------------------------------------------------ #
    before = pl.tool_tree_state()
    log = deck.root / "orchestrator.jsonl"
    # Lane purity travels as environment: the orchestrator's Bash inherits
    # PPTXGYM_ENGINE, so every specialist verb it runs lands on the same
    # engine without a flag. Model/effort pins are claude-lane vocabulary;
    # the codex lane runs on its own model default (or --codex-model) and
    # maps effort onto reasoning effort.
    model = args.model if engine == "claude" \
        else getattr(args, "codex_model", None)
    def outstanding() -> str | None:
        """What this deck still owes, in the words the record uses.

        A deck that shipped owes nothing. A deck that did not, but wrote its
        reasoned no, owes nothing either — that is the other half of the
        brief, and pushing an agent that has argued its case would only buy
        an argument it already made.
        """
        ok, why = shipped(deck)
        if ok or (deck.root / "REVIEW.md").exists():
            return None
        return why

    spec = agentmod.AgentRun(
        "orchestrator",
        mission(deck, work, args.max_turns, assignment(args),
                wps=getattr(args, "wps", True), engine=engine),
        max_turns=args.max_turns, timeout_min=args.timeout,
        model=model, effort=args.effort, engine=engine,
        allowed_tools=list(TOOLS), log=log,
        outputs=[deck.root / "REVIEW.md"],
        unfinished=outstanding,
        continuations=CONTINUATIONS.get(engine, 0),
        env={"PPTXGYM_SKIP_PERMISSIONS": "1",
             agentmod.ENGINE_ENV: engine})
    pl.log_event("deck_started", deck=deck.id, model=model,
                 effort=args.effort, max_turns=args.max_turns,
                 engine=engine)
    res = await agentmod.run_agent(spec)

    # ---- guard: the tools are everybody's -------------------------------- #
    touched = pl.revert_tool_changes(deck, before, "foreman")
    if touched:
        return _finish("parked", f"the orchestrator edited the shared tools "
                                 f"({touched}); reverted where possible, "
                                 f"diff kept beside the log",
                       agent=res.get("status"), **_witness(log, engine))

    # ---- collect: the record decides ------------------------------------- #
    # The goods before the messenger: a truncated or timed-out orchestrator
    # whose deck is mechanically complete and re-verifies clean has shipped —
    # deck0003 finished all nine stages and was parked for spending its last
    # turn on the summary. Only when the record does not ship does how the
    # orchestrator ended become the reason.
    ok, why = shipped(deck)
    if ok:
        # the record says shipped; now the measurements themselves get the
        # last word, re-executed from the artefacts in a worker thread
        ok, why = await asyncio.to_thread(verify, deck,
                                          getattr(args, "wps", True))
    if ok:
        return _finish("shipped", task=(deck.state().get("packaged") or {})
                       .get("task_id"), agent=res.get("status"),
                       **_witness(log, engine))
    if res.get("status") in ("infra", "timeout", "truncated", "barrier"):
        return _finish("parked", f"orchestrator {res.get('status')}: "
                                 f"{str(res.get('why') or '')[:200]}",
                       agent=res.get("status"), **_witness(log, engine))
    # A reasoned no is a normal outcome, not a failure: the deck parks with
    # the orchestrator's own words and its REVIEW.md is the record to read.
    return _finish("parked", why, last=_last_words(res, log),
                   agent=res.get("status"), **_witness(log, engine))


# --------------------------------------------------------------------------- #
# the batch
# --------------------------------------------------------------------------- #


def dirty_tool_paths() -> list[str]:
    """Uncommitted paths in the tool tree, or [] when clean / not a git tree."""
    return sorted(pl._tool_entries(pl.tool_tree_state()))


def pick_decks(work: Path, args) -> list[pl.Deck]:
    if args.deck:
        return [pl.Deck(work / d) for d in args.deck]
    out = []
    for deck in pl.decks_in(work):
        if not args.force and shipped(deck)[0]:
            # Skip only what is *booked* as shipped. A complete record under
            # a parked foreman.json — deck0003, finished on turn 100 and
            # parked for it — must be picked so the prep shortcut can verify
            # and book it; skipping here would leave a finished task
            # permanently invisible to every summary.
            try:
                booked = json.loads(
                    (deck.root / "foreman.json").read_text()).get("outcome")
            except (OSError, ValueError):
                booked = None
            if booked == "shipped":
                continue
        out.append(deck)
    return out


class _NullGate:
    """An `async with` that gates nothing — the claude lane's second cap."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


_NULL_GATE = _NullGate()


def parse_engine_split(text: str | None, n_decks: int) -> list[str]:
    """`claude=20,codex=10` -> one engine per deck, in deck order.

    Counts short of the batch leave the remainder on the *first* named
    engine — the calibrated lane should absorb the rounding, not the one
    under trial.  Unknown engines are an error, not a silent claude.
    """
    if not text:
        return ["claude"] * n_decks
    pairs = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, cnt = part.partition("=")
        name = name.strip()
        if name not in agentmod.ENGINES:
            raise ValueError(f"--engine-split: {name!r} is not an engine; "
                             f"pick from {agentmod.ENGINES}")
        if not sep or not cnt.strip().isdigit():
            raise ValueError(f"--engine-split: {part!r} wants engine=count")
        pairs.append((name, int(cnt)))
    out: list[str] = []
    for name, cnt in pairs:
        out += [name] * cnt
    if len(out) < n_decks and pairs:
        out += [pairs[0][0]] * (n_decks - len(out))
    return out[:n_decks]


async def run_batch(work: Path, decks: list[pl.Deck], args) -> list[dict]:
    sem = asyncio.Semaphore(max(1, args.workers))
    engines = parse_engine_split(getattr(args, "engine_split", None),
                                 len(decks))
    # An optional, tighter cap for the lane whose quota is shared with live
    # rollouts. Off by default: the calibration's 429s looked like a
    # concurrency wall and were not one — the relay serves ten concurrent
    # requests in 4-10s when the rollouts are idle, and the bursts that do
    # arrive are ridden out by waiting (SHARED_RETRIES), not by throttling
    # ourselves to a third speed all day. The flag is here for the days
    # when the other tenant is genuinely hammering it.
    codex_cap = getattr(args, "codex_workers", None) or max(1, args.workers)
    codex_sem = asyncio.Semaphore(codex_cap)
    started = time.monotonic()
    done = 0

    async def one(deck: pl.Deck, engine: str) -> dict:
        nonlocal done
        async with sem:
            lane = codex_sem if engine == "codex" else _NULL_GATE
            async with lane:
                rec = await run_deck(deck, work, args, engine=engine)
        # Printed here rather than after the gather: a batch that dies
        # mid-run used to leave no trace of which decks had finished — the
        # first calibration was killed with 25 minutes of blank log, and
        # nothing on stdout said nine of ten decks were already parked.
        done += 1
        print(f"  [{done}/{len(decks)}] {rec['deck']} {rec['outcome']:8s} "
              f"{rec.get('minutes', '?')}min {engine} "
              f"— {str(rec.get('why') or '')[:80]} "
              f"[{(time.monotonic() - started) / 60:.0f}min elapsed]",
              flush=True)
        return rec

    return list(await asyncio.gather(
        *(one(d, e) for d, e in zip(decks, engines))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="pptxgym.foreman",
        description="one orchestrator per deck; prep, spawn, guard, collect")
    ap.add_argument("paths", nargs="*",
                    help=".pptx files or directories to ingest first")
    ap.add_argument("--work", default="work")
    ap.add_argument("--deck", nargs="*", help="deck ids (default: every deck "
                    "in the work root not already shipped)")
    ap.add_argument("--workers", type=int, default=2,
                    help="orchestrators running at once")
    ap.add_argument("--max-turns", type=int, default=MAX_TURNS)
    ap.add_argument("--timeout", type=int, default=TIMEOUT_MIN,
                    help="minutes of wall clock per deck")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--effort", default=EFFORT)
    ap.add_argument("--assign", default=None,
                    help="edit specialist lanes: verb=model:effort[,...] "
                         "(defaults: " + ", ".join(
                             f"{v}={m}:{e}" for v, (m, e) in ASSIGN.items())
                         + ")")
    ap.add_argument("--specialist-model", default=None,
                    help="override the model of every specialist lane")
    ap.add_argument("--specialist-effort", default=None,
                    help="override the effort of every specialist lane")
    ap.add_argument("--engine-split", default=None,
                    help="run part of the batch on another engine, in deck "
                         "order: claude=20,codex=10 (default: all claude)")
    ap.add_argument("--codex-model", default=None,
                    help="model for codex-lane decks (default: codex's own)")
    ap.add_argument("--codex-workers", type=int, default=None,
                    help="throttle the codex lane below --workers, for when "
                         "the rollouts sharing its quota are busy "
                         "(default: no extra cap)")
    ap.add_argument("--roundtrip", action="store_true", default=True)
    ap.add_argument("--no-roundtrip", dest="roundtrip", action="store_false")
    ap.add_argument("--no-wps", dest="wps", action="store_false", default=True,
                    help="this machine has no WPS round trip: the orchestrator "
                         "is told to pass --no-wps to harden, and collect's "
                         "re-execution does the same")
    ap.add_argument("--force", action="store_true",
                    help="re-run decks that already shipped")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="launch even though the tool tree has uncommitted "
                         "changes (the guard will blame the agents for them)")
    args = ap.parse_args(argv)

    # The guard attributes an agent's edits by comparing the tree around its
    # run, and that comparison is only sound from a clean start. Run 2's two
    # decks finished their work and were parked anyway, because the foreman
    # was launched around the foreman's own uncommitted source: the spawn
    # fingerprint recorded the dirty files, the supervisor committed them
    # mid-run, and at collect time the guard could only say "the tree changed
    # and I cannot attribute it" — which is a park. Commit first.
    dirty = dirty_tool_paths()
    if dirty and not args.allow_dirty:
        print("refusing to start: the tool tree has uncommitted changes ("
              + ", ".join(dirty[:5]) + ("…" if len(dirty) > 5 else "") + ").\n"
              "Every mid-run commit would then read as an agent's edit and "
              "park its deck. Commit (or stash) first, or pass --allow-dirty "
              "to take that risk knowingly.")
        return 2

    work = Path(args.work)
    if args.paths:
        r = pl.ingest_many(args.paths, work)
        print(f"ingested: {len(r['registered'])} registered, "
              f"{len(r['duplicate'])} duplicate, {len(r['rejected'])} rejected")
    decks = pick_decks(work, args)
    if not decks:
        print("nothing to do — every deck in the work root has shipped")
        return 0

    pl.open_run(work, argv=argv or sys.argv[1:], cmd="foreman",
                decks=[d.id for d in decks],
                limits={"workers": args.workers, "max_turns": args.max_turns,
                        "timeout_min": args.timeout, "model": args.model,
                        "effort": args.effort,
                        "engine_split": args.engine_split})
    try:
        results = asyncio.run(run_batch(work, decks, args))
    finally:
        pl.close_run()

    for r in results:
        line = f"  {r['deck']}  {r['outcome']:8s} {r.get('minutes', '?')}min"
        if r["outcome"] == "shipped":
            line += f"  task_{r.get('task')}"
        if r.get("why"):
            line += f"  — {r['why'][:120]}"
        print(line)
    done = sum(1 for r in results if r["outcome"] == "shipped")
    print(f"{done}/{len(results)} shipped")
    return 0 if done == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
