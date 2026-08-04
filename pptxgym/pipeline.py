"""Per-deck state machine.

Every stage reads a directory and writes a directory.  Nothing is carried in a
conversation, which is what makes the run resumable, inspectable, and equally
runnable by a person or by a headless agent.

    ingested -> inspected -> proposed -> recipe -> degraded
             -> materialised -> reconciled

`materialised` produces the files the instruction promises; `reconciled` is the
last judgement gate, checking that the broken file, the instruction and the
assets still describe each other.

Stages after that (reward authoring, verification, packaging) are deliberately
absent: they exist as code elsewhere but have not been through a batch run yet,
and a stage that has never been exercised does not belong in a pipeline that
other people are meant to trust.

A deck directory:

    work/deck0001/
      meta.json        where the source came from, how many slides
      source.pptx      the untouched deck — also the ground truth
      digest.json      structural digest for the proposer
      digest.min.json  same, compact, for an agent's context
      renders/p-NN.png one per slide
      proposal.json    what tasks this deck should yield  (agent)
      recipe.json      how to actually break it           (agent)
      input.pptx       the broken file
      delta.json       every change, with its prior value
      assets/          what the solver gets besides the broken file
      task.json        the final record: instruction, assets, verdict (agent)
      state.json       stage -> {status, at, detail}
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

STAGES = ["ingested", "inspected", "proposed", "recipe", "degraded",
          "materialised", "reconciled", "solvable"]
AGENT_STAGES = {"proposed", "recipe", "reconciled", "solvable"}

# What each stage read to reach its verdict.  A stage is `ok` only while these
# are the same bytes it saw: `invalidate_from` handles the repair path, but any
# other way of touching an upstream file — a hand re-run, a fixed executor, an
# edited recipe — used to leave every downstream tick standing, and a stale
# tick is worse than a missing one because it claims the deck was judged.
STAGE_INPUTS = {
    "inspected": ["source.pptx"],
    "proposed": ["digest.json"],
    "recipe": ["proposal.json", "digest.json"],
    "degraded": ["recipe.json", "source.pptx"],
    "materialised": ["proposal.json", "delta.json"],
    "reconciled": ["input.pptx", "delta.json", "assets/manifest.json"],
    "solvable": ["input.pptx", "task.json", "assets/manifest.json"],
}

# stages whose run may continue from something other than a clean `ok`
PROMOTES = {"materialised": ("ok", "partial")}

# The verdicts that let a deck through.  Everything else — `needs_rework`,
# `leaked`, `ambiguous`, `overdetermined`, and `undetermined` — sends it back.
# `undetermined` used to pass, which reads the wrong way round: it means the
# probe could not decide, and an undecided gate is not a passed gate.
PASSING_VERDICTS = {"ready", "solvable"}

# what the solvability probe is not allowed to open: all of it is the answer
FORBIDDEN_TO_PROBE = ("source.pptx", "delta.json", "recipe.json",
                      "proposal.json")
MAX_REPAIRS = 3

# what a repair to each stage invalidates, in order
DOWNSTREAM = {
    "proposed": ["recipe", "degraded", "materialised", "reconciled", "solvable"],
    "recipe": ["degraded", "materialised", "reconciled", "solvable"],
    "materialise": ["materialised", "reconciled", "solvable"],
    "materialised": ["materialised", "reconciled", "solvable"],
}


class StageError(RuntimeError):
    pass


_DIGESTS: dict[tuple, str] = {}


def _digest(path: Path) -> str:
    """Content hash, memoised on (size, mtime) so a status table is cheap."""
    import hashlib
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    hit = _DIGESTS.get(key)
    if hit:
        return hit
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    _DIGESTS[key] = out = h.hexdigest()[:16]
    return out


@dataclass
class Deck:
    root: Path

    @property
    def id(self) -> str:
        return self.root.name

    # -- paths -------------------------------------------------------------- #
    @property
    def source(self):
        return self.root / "source.pptx"

    @property
    def digest(self):
        return self.root / "digest.json"

    @property
    def digest_min(self):
        return self.root / "digest.min.json"

    @property
    def renders(self):
        return self.root / "renders"

    @property
    def proposal(self):
        return self.root / "proposal.json"

    @property
    def recipe(self):
        return self.root / "recipe.json"

    @property
    def input_pptx(self):
        return self.root / "input.pptx"

    @property
    def delta(self):
        return self.root / "delta.json"

    # -- state -------------------------------------------------------------- #
    def state(self) -> dict:
        f = self.root / "state.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def fingerprint(self, stage: str) -> dict:
        """Digest of everything `stage` reads, as it stands right now."""
        out = {}
        for rel in STAGE_INPUTS.get(stage, []):
            p = self.root / rel
            out[rel] = _digest(p) if p.exists() else None
        return out

    def mark(self, stage: str, status: str, **detail):
        st = self.state()
        st[stage] = {"status": status, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     **detail, "_in": self.fingerprint(stage)}
        (self.root / "state.json").write_text(
            json.dumps(st, ensure_ascii=False, indent=1))

    def stale(self, stage: str) -> list[str]:
        """Inputs that have changed since the stage last ran.

        Staleness is inherited.  A changed `recipe.json` only moves the files
        `degraded` reads; `reconciled` still sees the same `input.pptx` until
        the degrade is re-run, and would otherwise keep its tick while sitting
        on top of a stage everyone can see is out of date.
        """
        st = self.state()
        rec = st.get(stage, {})
        was = rec.get("_in")
        if was is None:                     # ran before fingerprints existed
            return []
        now = self.fingerprint(stage)
        out = [k for k in set(was) | set(now) if was.get(k) != now.get(k)]
        for up in STAGES[:STAGES.index(stage)]:
            if st.get(up, {}).get("status") in ("ok", "partial") and self.stale(up):
                out.append(f"<{up}>")
        return sorted(out)

    def status_of(self, stage: str) -> str | None:
        """The recorded status, downgraded to "stale" if its inputs moved."""
        s = self.state().get(stage, {}).get("status")
        if s in ("ok", "partial") and self.stale(stage):
            return "stale"
        return s

    def done(self, stage: str) -> bool:
        return self.status_of(stage) == "ok"

    def promoted(self, stage: str) -> bool:
        """May the run move past this stage?

        Not the same question as `done`.  `materialised` is designed to end at
        `partial` — an asset the proposal promised and the deck cannot supply
        is a mismatch for `reconciled` to judge, not a reason to stop.  Driving
        the run off `done` alone re-ran those decks and then parked them as
        `needs_human` for reaching exactly the state they were meant to reach.
        """
        return self.status_of(stage) in PROMOTES.get(stage, ("ok",))

    def stage_now(self) -> str:
        """The furthest stage completed, or "" if nothing has run."""
        last = ""
        for s in STAGES:
            if self.done(s):
                last = s
            else:
                break
        return last

    def meta(self) -> dict:
        f = self.root / "meta.json"
        return json.loads(f.read_text()) if f.exists() else {}


def ingest(pptx: Path, work: Path, deck_id: str | None = None) -> Deck:
    """Register a source deck. The source is also the ground truth — nothing
    downstream ever writes to it."""
    work.mkdir(parents=True, exist_ok=True)
    if deck_id is None:
        n = 1 + max([int(p.name[4:]) for p in work.glob("deck[0-9]*")
                     if p.name[4:].isdigit()] or [0])
        deck_id = f"deck{n:04d}"
    deck = Deck(work / deck_id)
    deck.root.mkdir(parents=True, exist_ok=True)
    shutil.copy(pptx, deck.source)

    from pptx import Presentation
    prs = Presentation(str(deck.source))
    (deck.root / "meta.json").write_text(json.dumps({
        "id": deck_id, "origin": str(Path(pptx).resolve()),
        "name": Path(pptx).name, "slides": len(prs.slides),
        "size_in": [round(prs.slide_width / 914400, 1),
                    round(prs.slide_height / 914400, 1)],
    }, ensure_ascii=False, indent=1))
    deck.mark("ingested", "ok", slides=len(prs.slides))
    return deck


def inspect(deck: Deck, dpi: int = 110, force: bool = False) -> dict:
    """Digest + one render per slide. Deterministic; no agent involved."""
    from . import deck_digest, render

    # the round trip is measured first: its numbers belong in the digest, so
    # the proposer can see which shapes on this deck are unsafe to move
    rt_f = deck.root / "roundtrip.json"
    if force or not rt_f.exists():
        from . import roundtrip as rtmod
        try:
            rt_pre = rtmod.check(str(deck.source))
        except Exception as e:                                   # noqa: BLE001
            rt_pre = {"verdict": "unmeasured", "error": str(e)[:160]}
        rt_f.write_text(json.dumps(rt_pre, ensure_ascii=False, indent=1))
    rt_now = json.loads(rt_f.read_text())
    drift = {"verdict": rt_now.get("verdict"),
             "changed_frac": rt_now.get("changed_frac"),
             "drift_in": rt_now.get("drift") or {},
             "kinds_that_move": sorted(
                 set((rt_now.get("by_kind") or {}).get("moved", {}))
                 | set((rt_now.get("by_kind") or {}).get("resized", {})))}

    if deck.digest.exists() and not force:
        pass
    else:
        d = deck_digest.digest(str(deck.source), drift=drift)
        deck.digest.write_text(json.dumps(d, ensure_ascii=False, indent=1))
        # the compact copy is what an agent reads: same content, ~half the
        # tokens, and the indented one stays for humans
        deck.digest_min.write_text(
            json.dumps(d, ensure_ascii=False, separators=(",", ":")))

    deck.renders.mkdir(exist_ok=True)
    have = sorted(deck.renders.glob("p-*.png"))
    n_slides = deck.meta().get("slides", 0)
    if force or len(have) < n_slides:
        for f in have:
            f.unlink()
        render.render_pptx(str(deck.source), str(deck.renders), "p", dpi=dpi)
        have = sorted(deck.renders.glob("p-*.png"))

    # What the renderer changes on its own, measured once per deck.  Recorded,
    # not gated: the decks measured so far run from 8% to 61% of shapes touched,
    # and until that spread is decomposed into "placeholder autofit" versus
    # "real drift" any threshold would be a number picked to look decisive.
    rt = rt_now
    detail = {"digest_kb": deck.digest.stat().st_size // 1024,
              "digest_min_kb": deck.digest_min.stat().st_size // 1024,
              "renders": len(have),
              "roundtrip": rt.get("verdict"),
              "roundtrip_changed_frac": rt.get("changed_frac")}
    if len(have) < n_slides:
        deck.mark("inspected", "failed", **detail,
                  error=f"rendered {len(have)} of {n_slides} slides")
        raise StageError(f"{deck.id}: rendered {len(have)} of {n_slides} slides")
    deck.mark("inspected", "ok", **detail)
    return detail


def check_proposal(deck: Deck) -> dict:
    """Promote `proposed` only if the file is a usable proposal, not merely
    present.  File-existence as a success test is how a batch run ends up full
    of plausible rubbish."""
    if not deck.proposal.exists():
        raise StageError(f"{deck.id}: no proposal.json")
    try:
        p = json.loads(deck.proposal.read_text())
    except json.JSONDecodeError as e:
        raise StageError(f"{deck.id}: proposal.json is not valid JSON ({e})")

    tasks = p.get("tasks")
    if tasks is None:
        raise StageError(f"{deck.id}: proposal has no `tasks` key")
    if not tasks:
        # an empty proposal is a legitimate answer, but it has to say why
        if not (p.get("no_task_reason") or "").strip():
            raise StageError(f"{deck.id}: empty `tasks` with no no_task_reason")
        return {"tasks": 0, "reason": p["no_task_reason"][:120]}

    n_slides = deck.meta().get("slides", 10 ** 6)
    out = []
    for t in tasks:
        for key in ("name", "difficulty", "est_steps", "instruction",
                    "degradations"):
            if not t.get(key):
                raise StageError(f"{deck.id}: task missing `{key}`")
        if not t["degradations"]:
            raise StageError(f"{deck.id}: task {t['name']} has no degradations")
        for g in t["degradations"]:
            for page in g.get("slides", []):
                if not 1 <= page <= n_slides:
                    raise StageError(
                        f"{deck.id}: {g.get('id')} targets slide {page}, "
                        f"deck has {n_slides}")
        total = sum(g.get("est_steps", 0) for g in t["degradations"])
        band = ("easy" if t["est_steps"] <= 100
                else "medium" if t["est_steps"] <= 300 else "hard")
        if band != t["difficulty"]:
            raise StageError(
                f"{deck.id}: task {t['name']} says {t['difficulty']} but "
                f"{t['est_steps']} steps is {band}")
        out.append({"name": t["name"], "difficulty": t["difficulty"],
                    "est_steps": t["est_steps"], "sum_of_parts": total,
                    "degradations": len(t["degradations"])})
    return {"tasks": len(tasks), "detail": out}


def check_recipe(deck: Deck) -> dict:
    """The recipe has to name ops that exist and slides that exist."""
    from . import degrade_exec

    if not deck.recipe.exists():
        raise StageError(f"{deck.id}: no recipe.json")
    r = json.loads(deck.recipe.read_text())
    n_slides = deck.meta().get("slides", 10 ** 6)
    n_steps = 0
    for page, steps in (r.get("slides") or {}).items():
        if not str(page).isdigit() or not 1 <= int(page) <= n_slides:
            raise StageError(f"{deck.id}: recipe targets slide {page!r}")
        for st in steps:
            if st.get("op") not in degrade_exec.REGISTRY:
                raise StageError(
                    f"{deck.id}: unknown op {st.get('op')!r} "
                    f"(known: {', '.join(sorted(degrade_exec.REGISTRY))})")
            n_steps += 1
    for key in ("smartart", "chart"):
        for spec in (r.get(key) or []):
            if not 1 <= spec.get("slide", 0) <= n_slides:
                raise StageError(f"{deck.id}: {key} targets slide "
                                 f"{spec.get('slide')!r}")
            n_steps += 1
    if not n_steps:
        raise StageError(f"{deck.id}: recipe does nothing")
    return {"steps": n_steps, "slides": len(r.get("slides") or {})}


def degrade(deck: Deck) -> dict:
    """Apply the recipe, then gate the package. A broken or leaky file never
    reaches the next stage."""
    from . import degrade_exec, pkg_check

    recipe = json.loads(deck.recipe.read_text())
    delta = degrade_exec.run(str(deck.source), recipe, str(deck.input_pptx))
    deck.delta.write_text(json.dumps(delta, ensure_ascii=False, indent=1))

    integ = pkg_check.check(str(deck.input_pptx))
    leak = pkg_check.leak_check(str(deck.input_pptx), delta, str(deck.source))
    problems = (integ["problems"] + integ["duplicate_ids"]
                + leak["leaks"] + leak["dead_rels"])
    n = sum(len(v) for v in delta["slides"].values())
    detail = {"changes": n, "slides": len(delta["slides"]),
              "gate": "ok" if not problems else "FAILED",
              "problems": problems[:8]}
    if problems:
        deck.mark("degraded", "failed", **detail)
        raise StageError(f"{deck.id}: package gate failed — {problems[0]}")
    deck.mark("degraded", "ok", **detail)
    return detail


def materialise(deck: Deck) -> dict:
    """Produce the files the task promises. Deterministic.

    Refuses to promote while anything the proposal declared is missing: an
    unmet asset is not a cosmetic gap, it is a task nobody can attempt.
    """
    from . import assets

    m = assets.materialise(deck)
    detail = {"produced": len(m["produced"]), "unmet": len(m["unmet"]),
              "kinds": sorted({p["kind"] for p in m["produced"]})}
    if m["unmet"]:
        detail["problems"] = [f"{u['kind']}: {u['why']}" for u in m["unmet"]][:6]
    if not m["produced"] and m["unmet"]:
        deck.mark("materialised", "failed", **detail)
        raise StageError(f"{deck.id}: no asset could be produced — "
                         f"{m['unmet'][0]['why']}")
    if m["unmet"]:
        # An asset that cannot be produced is usually not a tooling failure but
        # a promise the proposal could not keep — here, chart numbers for a
        # figure that was always a bitmap.  That is a mismatch between the
        # instruction and the deck, which is precisely what `reconciled` judges,
        # so it goes forward marked `partial` rather than dying here.  The
        # reconciler is required to address every unmet asset.
        deck.mark("materialised", "partial", **detail)
        return detail
    deck.mark("materialised", "ok", **detail)
    return detail


def check_reconcile(deck: Deck) -> dict:
    """The reconciler's output has to be a task record that stands on its own."""
    f = deck.root / "task.json"
    if not f.exists():
        raise StageError(f"{deck.id}: no task.json")
    try:
        t = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        raise StageError(f"{deck.id}: task.json is not valid JSON ({e})")

    for key in ("name", "instruction", "difficulty", "est_steps",
                "assets", "instruction_changed", "notes"):
        if key not in t:
            raise StageError(f"{deck.id}: task.json missing `{key}`")
    if not (t["instruction"] or "").strip():
        raise StageError(f"{deck.id}: empty instruction")

    # every file the record hands to the solver has to be on disk
    adir = deck.root / "assets"
    missing = [a for a in t["assets"]
               if a.get("file") and not (adir / a["file"]).exists()]
    if missing:
        raise StageError(f"{deck.id}: task.json lists {missing[0].get('file')!r} "
                         f"but it is not in assets/")
    if t.get("verdict") == "needs_rework":
        rw = t.get("rework")
        if not rw:
            raise StageError(f"{deck.id}: needs_rework with no `rework` list — "
                             f"a verdict nobody can act on is a dead end")
        for r in rw:
            if r.get("stage") not in ("proposed", "recipe", "materialise"):
                raise StageError(
                    f"{deck.id}: rework targets {r.get('stage')!r}; must be one "
                    f"of proposed / recipe / materialise")
            if not (r.get("what") or "").strip():
                raise StageError(f"{deck.id}: rework entry with no `what`")
    if t["instruction_changed"] and not (t.get("notes") or "").strip():
        raise StageError(f"{deck.id}: instruction was changed with no note "
                         f"saying why")

    # anything materialise could not produce has to be dealt with, not ignored:
    # the instruction promised it to the solver
    man_f = deck.root / "assets" / "manifest.json"
    if man_f.exists():
        unmet = json.loads(man_f.read_text()).get("unmet") or []
        if unmet and t.get("verdict") != "needs_rework":
            blob = ((t.get("notes") or "") + " "
                    + " ".join(d.get("note") or "" for d in t.get("degradations") or [])
                    ).lower()
            unaddressed = [u["kind"] for u in unmet
                           if u["kind"].lower() not in blob]
            if unaddressed:
                raise StageError(
                    f"{deck.id}: asset {unaddressed[0]!r} could not be produced "
                    f"and the task record never mentions it")
    return {"assets": len(t["assets"]), "verdict": t.get("verdict"),
            "instruction_changed": bool(t["instruction_changed"]),
            "difficulty": t["difficulty"], "est_steps": t["est_steps"]}


def archive_attempt(deck: Deck, stage: str) -> str | None:
    """Move a stage's artefacts into attempts/ before it runs again.

    Agent logs were opened with "w" and `task.json` was overwritten, so a
    second run destroyed the evidence of the first.  When a repair loop is
    allowed to retry until a gate passes, that is the difference between
    "it was fixed" and "the verdict was laundered" — and afterwards nobody,
    including the pipeline, can tell which happened.
    """
    art = {"proposed": ["proposal.json", "proposed.jsonl"],
           "recipe": ["recipe.json", "recipe.jsonl"],
           "reconciled": ["task.json", "reconciled.jsonl"],
           "degraded": ["delta.json"],
           "solvable": ["solvability.json", "solvable.jsonl"],
           "materialised": []}.get(stage, [])
    live = [f for f in art if (deck.root / f).exists()]
    if not live:
        return None
    n = 1 + len(list((deck.root / "attempts").glob(f"{stage}-*")))
    dest = deck.root / "attempts" / f"{stage}-{n:02d}"
    dest.mkdir(parents=True, exist_ok=True)
    for f in live:
        shutil.copy2(deck.root / f, dest / f)
    prev = deck.state().get(stage, {})
    (dest / "state.json").write_text(json.dumps(prev, ensure_ascii=False, indent=1))
    return str(dest.relative_to(deck.root))


TOOL_PATHS = ("pptxgym", ".claude")


def tool_tree_state() -> str | None:
    """Fingerprint of the pipeline's own code and prompts.

    A repair fixes one deck; the tools are shared by all of them.  One repair
    agent patched `degrade_exec` mid-run — correctly, as it happens, but
    nobody reviewed it and it silently changed what every other deck would be
    degraded into.  The failure mode this guards against is the one that looks
    identical to success: quieting the gate instead of fixing the deck.

    None when this is not a git tree, in which case the check is skipped
    rather than guessed at.
    """
    import subprocess
    root = Path(__file__).resolve().parents[1]
    try:
        r = subprocess.run(["git", "-C", str(root), "status", "--porcelain",
                            "--", *TOOL_PATHS],
                           capture_output=True, text=True, timeout=30)
        h = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode or h.returncode:
        return None
    return h.stdout.strip() + "\n" + r.stdout


def revert_tool_changes(deck: Deck, before: str, label: str) -> str | None:
    """Undo an agent's edits to the shared tools, keeping the diff as evidence.

    Only touches paths that were clean before the run: reverting a change
    somebody was in the middle of making would be a worse bug than the one
    being prevented.
    """
    import subprocess
    if before is None or tool_tree_state() == before:
        return None
    root = Path(__file__).resolve().parents[1]
    was_dirty = {ln[3:].split(" -> ")[-1]
                 for ln in before.splitlines()[1:] if ln[3:]}
    now = subprocess.run(["git", "-C", str(root), "status", "--porcelain",
                          "--", *TOOL_PATHS], capture_output=True, text=True)
    touched = [ln[3:].split(" -> ")[-1] for ln in now.stdout.splitlines()
               if ln[3:] and ln[3:].split(" -> ")[-1] not in was_dirty]
    if not touched:
        return None
    diff = subprocess.run(["git", "-C", str(root), "diff", "--", *touched],
                          capture_output=True, text=True).stdout
    out = deck.root / f"{label}-tool-change.diff"
    out.write_text(f"# reverted: a repair may not edit the shared tools\n"
                   f"# files: {', '.join(touched)}\n\n{diff}")
    subprocess.run(["git", "-C", str(root), "checkout", "--", *touched],
                   capture_output=True)
    return f"{len(touched)} tool file(s) ({', '.join(touched[:3])})"


def repairs_done(deck: Deck) -> int:
    """How many times the repairer has actually run on this deck.

    Counting archived `reconciled-*` attempts was a proxy for it, and a poor
    one in both directions: reconcile is re-run for reasons that have nothing
    to do with a repair, and a repair that fails before reconcile gets to run
    is not counted at all — so `MAX_REPAIRS` was never quite the limit it
    claimed to be.  The repairer's own log is the thing being counted.
    """
    return len(list(deck.root.glob("repair-*.jsonl")))


def invalidate_from(deck: Deck, stage: str):
    """Drop the stage states a repair has made stale, so they re-run."""
    st = deck.state()
    for s in DOWNSTREAM.get(stage, []):
        st.pop(s, None)
    (deck.root / "state.json").write_text(
        json.dumps(st, ensure_ascii=False, indent=1))


def bundle(deck: Deck) -> Path:
    """Everything a solver is given, and nothing else, in one directory.

    The information barrier used to be a request plus a substring scan of the
    probe's log, and the scan was wrong in both directions: it failed a run for
    `grep -rn "source.pptx" pptxgym` (reading *our* code) and for a report that
    named the forbidden files in prose to say it had not opened them, while a
    probe that quietly opened `../source.pptx` would read as clean.

    A directory containing only the broken file, the assets and the instruction
    makes the barrier structural.  The scan stays as a backstop, but it now has
    something unambiguous to look for: the probe works here, so any path that
    climbs back into the deck is a deliberate reach for the answer key.

    It is also the shape the task ships in, so this is not scaffolding.
    """
    b = deck.root / "bundle"
    if b.exists():
        shutil.rmtree(b)
    (b / "assets").mkdir(parents=True)
    shutil.copy2(deck.input_pptx, b / "input.pptx")
    src = deck.root / "assets"
    for f in sorted(src.iterdir()) if src.exists() else []:
        # the manifest records *why* an asset could not be produced, in terms
        # of what was broken — solver-visible only by accident
        if f.name == "manifest.json":
            continue
        (shutil.copytree if f.is_dir() else shutil.copy2)(f, b / "assets" / f.name)
    t = json.loads((deck.root / "task.json").read_text())
    (b / "instruction.md").write_text(
        f"# {t.get('name', deck.id)}\n\n{t.get('instruction', '')}\n",
        encoding="utf-8")
    return b


def barrier_breaches(deck: Deck, log: Path) -> list[str]:
    """Tool calls in which the probe reached outside its bundle.

    Only tools that *read* count.  A report that mentions `source.pptx` in a
    sentence is not a peek, and treating it as one cost two real verdicts.
    """
    import re

    if not log.exists():
        return []
    reading = {"Read", "Bash", "Grep", "Glob", "NotebookRead"}
    root = str(deck.root.resolve())
    pat = re.compile(re.escape(root) + r"(/[^\s\"']*)?"
                     r"|(?<![\w.])work/" + re.escape(deck.id) + r"(/[^\s\"']*)?")
    bad = []
    with open(log) as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            for c in ((d.get("message") or {}).get("content") or []):
                if not (isinstance(c, dict) and c.get("type") == "tool_use"):
                    continue
                if c.get("name") not in reading:
                    continue
                blob = json.dumps(c.get("input") or {}, ensure_ascii=False)
                for m in pat.finditer(blob):
                    tail = m.group(1) or m.group(2) or ""
                    # the bundle, plus the file it was told to write: every
                    # probe re-reads its own report to check the JSON parses,
                    # and calling that a peek voided four more runs
                    if tail.startswith(("/bundle", "/solvability.json")):
                        continue
                    bad.append(f"{c.get('name')}: …{m.group(0)[-70:]}")
                if any(f"../{f}" in blob or f"/../" in blob
                       for f in FORBIDDEN_TO_PROBE):
                    bad.append(f"{c.get('name')}: climbed out of the bundle")
    return sorted(set(bad))


def check_solvability(deck: Deck) -> dict:
    """Judge the probe's report — and first, whether it stayed blind.

    A solvability verdict reached with the answer key open carries no
    information, so the barrier is verified rather than requested.
    """
    breaches = barrier_breaches(deck, deck.root / "solvable.jsonl")
    if breaches:
        raise StageError(
            f"{deck.id}: the probe read outside its bundle "
            f"({breaches[0]}) — that is the answer key, so its verdict "
            f"means nothing")

    f = deck.root / "solvability.json"
    if not f.exists():
        raise StageError(f"{deck.id}: no solvability.json")
    try:
        r = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        raise StageError(f"{deck.id}: solvability.json is not valid JSON ({e})")

    verdict = r.get("verdict")
    if verdict not in ("solvable", "undetermined", "leaked", "ambiguous",
                       "overdetermined"):
        raise StageError(f"{deck.id}: unknown verdict {verdict!r}")
    if not r.get("degradations"):
        raise StageError(f"{deck.id}: no per-degradation findings")
    for d in r["degradations"]:
        if not (d.get("end_state") or "").strip():
            raise StageError(f"{deck.id}: {d.get('id')} has no end_state")
        if d.get("determinate") and not (d.get("evidence") or "").strip():
            raise StageError(
                f"{deck.id}: {d.get('id')} is called determinate with no "
                f"evidence — that is a guess wearing a verdict")
    if verdict != "solvable":
        rw = r.get("rework") or []
        if not rw:
            raise StageError(f"{deck.id}: verdict {verdict!r} with no `rework`")
        for x in rw:
            if x.get("stage") not in ("proposed", "recipe", "materialise"):
                raise StageError(f"{deck.id}: rework targets {x.get('stage')!r}")
    return {"verdict": verdict, "leaks": len(r.get("leaks") or []),
            "steps_measured": r.get("est_steps_measured"),
            "steps_declared": r.get("est_steps_declared"),
            "undetermined": sum(1 for d in r["degradations"]
                                if not d.get("determinate"))}


def decks_in(work: Path) -> list[Deck]:
    return [Deck(p) for p in sorted(work.glob("deck*")) if p.is_dir()]


class DeckBusy(RuntimeError):
    pass


class lock:
    """Refuse to run two stages on one deck at once.

    Stages hand off through files, so two processes working the same deck will
    happily interleave: one writes a fresh proposal while the other is midway
    through a recipe for the previous one, and the pair that lands on disk has
    never been consistent.  It is invisible afterwards — the files all parse —
    which is exactly why it needs a lock rather than care.
    """

    def __init__(self, deck: Deck, stage: str):
        self.path = deck.root / ".lock"
        self.stage = stage
        self.deck = deck

    def __enter__(self):
        import os
        if self.path.exists():
            try:
                held = json.loads(self.path.read_text())
            except Exception:                                # noqa: BLE001
                held = {}
            pid = held.get("pid")
            alive = False
            if pid:
                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False
            if alive:
                raise DeckBusy(
                    f"{self.deck.id} is locked by pid {pid} running "
                    f"{held.get('stage')!r} since {held.get('at')}")
        self.path.write_text(json.dumps(
            {"pid": os.getpid(), "stage": self.stage,
             "at": time.strftime("%Y-%m-%dT%H:%M:%S")}))
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
        return False
