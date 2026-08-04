"""Run a stage that needs judgement, as a headless `claude -p` subprocess.

Two rules learned the hard way and worth keeping:

**Point at the skill by absolute path and say "read it in full".**  Skill
auto-discovery is not something to rely on in headless mode; an explicit path
worked on every one of the seventeen agents run so far.

**Never let file existence mean success.**  The agent's job is done when the
file it wrote passes this pipeline's own checker, which is why every runner
takes a `check` callable and re-runs it after the process exits.

**Retry the infrastructure, never the answer.**  A 429, a 403 or an aborted
stream says nothing about the deck and ends it anyway; the rate limit is per
*account*, so it is the one constraint more machines cannot buy around, and on
a multi-hour batch it will be hit — ten of the ten-deck pilot's ~100 agent runs
died this way.  A timeout and a `max_turns` stop are the opposite: the agent
was working and reached a real ceiling, so a retry buys the same answer for a
second helping of the budget.  Only what `_infra_failure` names `infra` is
retried, and every attempt keeps its own log.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / ".claude" / "agents"
SKILLS = ROOT / ".claude" / "skills"

# --------------------------------------------------------------------------- #
# how hard to try again
#
# Three retries — four attempts — with `min(30 * attempt, 120)` seconds
# between them: the curve CUA-Gym's batch orchestrator settled on, and at most
# 30 + 60 + 90 = three minutes of waiting per deck.  The wait is the number to
# argue about, not the count.
#
# Three minutes is chosen against what the pilot's logs actually contain: an
# aborted stream, a 403 during a token refresh, a burst that clears in a
# minute.  It deliberately does *not* try to outlast the other thing in there
# — `You've hit your session limit · resets 8:40pm` — because waiting hours
# inside a stage would hold a pool slot for hours, and choosing to do that is
# a human's decision, not a retry's.  Those decks park with the reset time in
# their `why`, which is where `status` shows it.
#
# The retry itself is nearly free — an infra failure aborts in seconds and
# burns no turns — so the only real cost is the wait.
# --------------------------------------------------------------------------- #

API_RETRIES = 3

# An expired login does not un-expire on a timer.  It is retried *once*, and
# once only, because the one thing waiting can fix here is a token-refresh
# race — thirty attempts against a revoked key is thirty times the same 403,
# and it buries the one message a human has to see (two decks were once parked
# as `needs_human` for exactly this, and the log is how we know).  This cap
# covers any 403, not only the `auth_error` label: see `_infra_failure`.
AUTH_RETRIES = 1

BACKOFF_STEP = 30
BACKOFF_CAP = 120


def backoff_seconds(attempt: int) -> int:
    """How long to wait after attempt `attempt` failed."""
    return min(BACKOFF_STEP * attempt, BACKOFF_CAP)


# --------------------------------------------------------------------------- #
# which model runs which stage
#
# A knob, not an assignment.  All five agent stages are judgement — the
# mechanical work (degrade, materialise, score, harden, package) is Python
# with no model in it at all — so there is no stage that is obviously cheap,
# and nothing here picks one.  The default is what it has always been: no
# `--model` and no `--effort`, so `claude` decides and this file moves
# nothing until somebody changes one variable and measures it.
# --------------------------------------------------------------------------- #

#: what the command line calls a stage -> the key the pipeline records under.
#: Both spellings are accepted because both are what people type.
STAGE_OF = {"propose": "proposed", "proposed": "proposed",
            "recipe": "recipe",
            "reconcile": "reconciled", "reconciled": "reconciled",
            "solvable": "solvable", "repair": "repair"}

ROLES = ("propose", "recipe", "reconcile", "solvable", "repair")

#: `claude --effort <level>`.  Validated because a typo is silently ignored by
#: the CLI, and a stage that was supposed to be running at `high` and was not
#: is a measurement that quietly means nothing.
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def parse_per_stage(text: str | None, what: str = "model") -> dict:
    """`opus` — every stage — or `propose=opus,recipe=sonnet` — named ones.

    Returns stage-key -> value, with `"*"` holding the bare form.  A bare
    value is what `--model opus` has always meant and keeps meaning.
    """
    out: dict[str, str] = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        role, sep, val = part.partition("=")
        role, val = role.strip(), val.strip()
        if not sep:
            out["*"] = role
            continue
        if role not in STAGE_OF:
            raise ValueError(
                f"--{what}: {role!r} is not an agent stage. "
                f"Pick from {', '.join(ROLES)}, or give a bare value to set "
                f"every stage at once.")
        if not val:
            raise ValueError(f"--{what}: {role}= with nothing after it")
        if what == "effort" and val not in EFFORTS:
            raise ValueError(
                f"--effort: {val!r} is not a level. Pick from "
                f"{', '.join(EFFORTS)}.")
        out[STAGE_OF[role]] = val
    if what == "effort" and out.get("*") not in (None, *EFFORTS):
        raise ValueError(f"--effort: {out['*']!r} is not a level. Pick from "
                         f"{', '.join(EFFORTS)}.")
    return out


@dataclass
class Assignment:
    """Which model, effort and fallback each agent stage asks for."""

    model: dict = field(default_factory=dict)
    effort: dict = field(default_factory=dict)
    fallback: dict = field(default_factory=dict)

    @classmethod
    def from_args(cls, args) -> "Assignment":
        return cls(parse_per_stage(getattr(args, "model", None), "model"),
                   parse_per_stage(getattr(args, "effort", None), "effort"),
                   parse_per_stage(getattr(args, "fallback_model", None),
                                   "fallback-model"))

    def for_stage(self, stage: str) -> dict:
        key = STAGE_OF.get(stage, stage)
        return {"model": self.model.get(key, self.model.get("*")),
                "effort": self.effort.get(key, self.effort.get("*")),
                "fallback_model": self.fallback.get(key,
                                                    self.fallback.get("*"))}

    def apply(self, spec: "AgentRun", stage: str) -> dict:
        """Put this stage's assignment on the spec, and say what it was."""
        got = self.for_stage(stage)
        spec.model = got["model"]
        spec.effort = got["effort"]
        spec.fallback_model = got["fallback_model"]
        return got


@dataclass
class AgentRun:
    name: str                 # .claude/agents/<name>.md
    prompt: str
    cwd: Path = ROOT
    max_turns: int = 60
    timeout_min: int = 30
    model: str | None = None
    allowed_tools: list[str] = field(
        default_factory=lambda: ["Bash", "Read", "Write", "Edit", "Glob", "Grep"])
    log: Path | None = None
    api_retries: int = API_RETRIES
    effort: str | None = None
    #: `claude --fallback-model`.  Only safe because `ran_as` can see, after
    #: the fact, which model actually produced the work: a deck silently
    #: finished on a weaker model would otherwise be an artefact we could
    #: never tell apart from the rest of the batch.
    fallback_model: str | None = None
    #: what this run is expected to write.  A run cut off by a 403 can leave a
    #: well-formed *partial* file behind — that is the failure `_infra_failure`
    #: exists for — and if the retry then dies without writing, the checker
    #: would judge the half-written one and pass it.  Named here, it is moved
    #: aside with the log it belongs to.
    outputs: list[Path] = field(default_factory=list)


async def run_agent(spec: AgentRun) -> dict:
    """Run the agent, retrying only when it was the API that failed.

    Returns the last attempt's result, plus `attempts`, plus — when there was
    more than one — `retried` (the evidence of each) and `recovered`.  A run
    that limped through on its fourth attempt is a fact about the account's
    headroom, and one that reads identically to a clean run is a fact nobody
    ever learns.
    """
    log = spec.log or (spec.cwd / f"{spec.name}.jsonl")
    history: list[dict] = []
    attempt = 1
    while True:
        before = {p: _stamp(p) for p in spec.outputs}
        res = await _run_once(spec, log)
        if res["status"] != "infra":
            break                     # a verdict, a timeout, or a ceiling hit
        if attempt > _retries_allowed(spec, res.get("kind")):
            break
        history.append({"attempt": attempt, "kind": res.get("kind"),
                        "why": res.get("why"),
                        "kept": _keep_attempt(log, spec, attempt, before),
                        "backoff_s": backoff_seconds(attempt)})
        await _hold_off(attempt)
        attempt += 1
    res["attempts"] = attempt
    if history:
        res["retried"] = history
        if res["status"] == "infra":
            res["why"] = (f"{res.get('why', '')} — gave up after "
                          f"{attempt} attempts").strip()
        else:
            res["recovered"] = True
    return res


def _retries_allowed(spec: AgentRun, kind: str | None) -> int:
    budget = max(0, int(spec.api_retries or 0))
    return min(budget, AUTH_RETRIES) if kind == "auth_error" else budget


async def _hold_off(attempt: int) -> int:
    """The wait between attempts, as its own function so a test can skip it."""
    delay = backoff_seconds(attempt)
    await asyncio.sleep(delay)
    return delay


def _stamp(p: Path) -> tuple | None:
    """Enough of a file to tell whether an attempt rewrote it.

    A wall-clock comparison was the obvious way and the wrong one: file
    timestamps are quantised — 10ms here — so a file written *after* the
    attempt started can carry an mtime a few milliseconds before it, and the
    evidence is silently left behind.  Before-and-after needs no clock.
    """
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _keep_attempt(log: Path, spec: AgentRun, attempt: int,
                  before: dict) -> str:
    """Move a dead attempt's evidence out of the way of the next one.

    The log is opened `"w"`, so without this the retry erases the reason the
    first attempt died — and that reason is the only thing separating an
    expired login from a lazy agent.  It goes to `retries/<stem>-try-NN/`
    rather than beside the log on purpose: `repairs_done` counts
    `repair-*.jsonl` in the deck root, so a retry log left there would spend
    somebody's repair budget on an outage.
    """
    n = attempt
    while (dest := log.parent / "retries" / f"{log.stem}-try-{n:02d}").exists():
        n += 1
    dest.mkdir(parents=True, exist_ok=True)
    # the log and its stderr are this attempt's by construction; an output file
    # is only ours if this attempt actually wrote it — one that predates the
    # attempt belongs to a stage that already succeeded, and moving it would
    # destroy the artefact the pipeline is standing on
    mine = [log, log.with_suffix(".stderr.log")]
    mine += [p for p in spec.outputs if _stamp(p) not in (None, before.get(p))]
    for src in mine:
        try:
            src.rename(dest / src.name)
        except OSError:
            pass
    return str(dest)


async def _run_once(spec: AgentRun, log: Path) -> dict:
    cmd = ["claude", "--agent", spec.name, "-p", spec.prompt,
           "--max-turns", str(spec.max_turns),
           "--output-format", "stream-json", "--verbose",
           "--allowedTools", ",".join(spec.allowed_tools)]
    if spec.model:
        cmd += ["--model", spec.model]
    if spec.effort:
        cmd += ["--effort", spec.effort]
    if spec.fallback_model:
        # `--fallback-model` only works with --print, which is what -p is
        cmd += ["--fallback-model", spec.fallback_model]
    if os.environ.get("PPTXGYM_SKIP_PERMISSIONS") == "1":
        cmd += ["--permission-mode", "dontAsk"]

    log.parent.mkdir(parents=True, exist_ok=True)
    err = log.with_suffix(".stderr.log")
    with open(log, "w") as lf, open(err, "w") as ef:
        ef.write(f"$ claude --agent {spec.name} -p <prompt>\n\n{spec.prompt}\n")
        ef.write("=" * 60 + "\n")
        ef.flush()
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=lf, stderr=ef, cwd=str(spec.cwd))
        try:
            await asyncio.wait_for(proc.wait(), timeout=spec.timeout_min * 60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"status": "timeout", "log": str(log)}
    return {"status": "exited", "returncode": proc.returncode, "log": str(log),
            **ran_as(log), **_infra_failure(log)}


def _infra_failure(log: Path) -> dict:
    """How the run ended, when that is not "it finished".

    The stage checkers judge the *shape* of the report, which a half-written
    one satisfies perfectly.  One probe was cut off by a 403 after writing a
    well-formed verdict; the checker passed it, and it read `solvable, no
    leaks` where the finished re-run read `undetermined, 2 leaks`.  Nothing
    downstream could have caught that — the only evidence is here.

    A 403 or a rate limit is also indistinguishable from a lazy agent (no
    output file at all), and two decks were parked as `needs_human` for an
    expired login.  Infrastructure failure has to be named as such, or the
    pipeline fills up with content judgements it never actually made.
    """
    d = last_result(log)
    if d:
        why = d.get("terminal_reason")
        if why == "aborted_streaming":
            # The stream died mid-answer: `is_error`, no result text, and
            # `subtype: error_during_execution`.  Three of the ten-deck
            # pilot's runs ended this way and every one of them was read as a
            # clean exit, which handed the checker whatever half-file was on
            # disk — the exact laundering this function exists to stop.
            return {"status": "infra", "kind": "aborted_streaming",
                    "why": "the response stream was aborted mid-run; nothing "
                           "it wrote is a finished answer"}
        if why in ("api_error", "auth_error"):
            # `kind` is what decides how hard it is worth trying again: a rate
            # limit clears on its own, a revoked key does not.
            #
            # A 403 counts as the second kind whatever the CLI calls it. Every
            # 403 in the pilot came through as `api_error` reading "Failed to
            # authenticate. API Error: 403 Request not allowed" — the expired
            # login that parked two decks was one of these — and no amount of
            # waiting has ever fixed one.
            kind = "auth_error" if (why == "auth_error"
                                    or d.get("api_error_status") == 403) \
                else "api_error"
            return {"status": "infra", "kind": kind,
                    "why": f"{d.get('api_error_status') or ''} "
                           f"{str(d.get('result'))[:120]}".strip()}
        if why in ("max_turns", "max_tokens", "refusal"):
            return {"status": "truncated", "kind": why,
                    "why": f"the run stopped on {why} — whatever it wrote is "
                           f"an interrupted answer, not a finished one"}
    return {}


#: How much of the log's tail to read looking for the result record.  It is
#: one line and carries `modelUsage`, so it grows with every model that ran —
#: measured at 1.6–2.4 KB today, and a `--fallback-model` adds another entry.
#: Clip its head and `json.loads` fails, which reads as "no opinion": the run
#: would be classified as a clean exit however it really ended.
RESULT_TAIL = 32000


def last_result(log: Path) -> dict:
    """The `type: result` record `claude` ends a run with, or `{}`."""
    try:
        tail = log.read_text(errors="replace")[-RESULT_TAIL:]
    except OSError:
        return {}
    for line in reversed(tail.splitlines()):
        if '"type":"result"' not in line:
            continue
        try:
            return json.loads(line[line.index("{"):])
        except (ValueError, json.JSONDecodeError):
            return {}
    return {}


def ran_as(log: Path) -> dict:
    """Which model actually did the work, from the log rather than the flag.

    `--model` is a request and `--fallback-model` is permission to ignore it.
    Recording the request alone would mean a deck quietly finished on a weaker
    model is an artefact nobody can tell apart from the rest of the batch —
    which is the whole reason to record anything here.  Two independent
    witnesses in the log: the session's model on the `system/init` line, and
    every model that actually produced tokens in the result's `modelUsage`.

    The little background model is in `modelUsage` on every run (a few dozen
    tokens, for titles and the like), so "the model that ran" is the one that
    wrote the most output, not merely one that appears.
    """
    out: dict = {}
    try:
        with open(log, errors="replace") as fh:
            head = fh.readline()
    except OSError:
        return {}
    try:
        d = json.loads(head)
        if d.get("type") == "system" and d.get("model"):
            out["model_session"] = d["model"]
    except (ValueError, json.JSONDecodeError):
        pass
    usage = last_result(log).get("modelUsage") or {}
    if isinstance(usage, dict) and usage:
        worked = max(usage.items(),
                     key=lambda kv: (kv[1] or {}).get("outputTokens", 0)
                     if isinstance(kv[1], dict) else 0)
        out["model_ran"] = worked[0]
        out["model_tokens"] = {k: (v or {}).get("outputTokens", 0)
                               for k, v in usage.items() if isinstance(v, dict)}
    ran, session = out.get("model_ran"), out.get("model_session")
    if ran and session and ran != session:
        # the fallback fired, or the session was re-routed. Either way this
        # deck was not made by the model we asked for, and that is the fact
        # the record exists to keep.
        out["fallback"] = True
    return out


def skill_path(name: str) -> str:
    p = SKILLS / name / "SKILL.md"
    if not p.exists():
        raise FileNotFoundError(f"no skill {name!r} at {p}")
    return str(p)


def propose_prompt(deck) -> str:
    return f"""Propose computer-use RL tasks from one real PPT deck.

FIRST: read {skill_path('ppt-task-proposal')} in full and follow it exactly.
It defines the judgement criteria, the difficulty calibration, the output JSON
schema and the hard rules.

Deck materials:
- structural digest (JSON): {deck.digest_min}
  (large decks: read it with offset/limit rather than all at once)
- page renders, one per slide, p-01.png …: {deck.renders}/

Read the digest AND look at every render before deciding. The digest carries
text formatting, table cell grids, SmartArt nodes, connector topology, z-order
and overlaps, grouping, layout/master identity, speaker notes, transitions,
animation build steps, motion paths, and `hard_target` flags marking shapes
nobody can recreate through a GUI. Use all of it; hard_target shapes make fine
context and terrible targets.

Write the JSON — exactly the schema in the skill, nothing else — to:
  {deck.proposal}

Then reply with one line: task count, difficulty, total est_steps. Do not
paste the JSON into your reply."""


def reconcile_prompt(deck) -> str:
    import json as _json
    task = (_json.loads(deck.proposal.read_text()).get("tasks") or [{}])[0]
    pages = sorted({p for g in task.get("degradations") or []
                    for p in g.get("slides", [])})
    return f"""Reconcile one degraded PPT task against its own instruction and
assets, then write the final task record.

FIRST: read {skill_path('ppt-task-reconcile')} in full and follow it exactly.

Your deck: {deck.id}  ({deck.meta().get('name')}, {deck.meta().get('slides')} slides)
  proposal : {deck.proposal}
  recipe   : {deck.recipe}          (every step names its `deg` and explains
                                     itself in `_why` — read both)
  delta    : {deck.delta}
  assets   : {deck.root / 'assets' / 'manifest.json'}
  write to : {deck.root / 'task.json'}

Affected pages: {pages}
Render them before judging anything:
  python -m pptxgym.tools pair {deck.root} {' '.join(str(p) for p in pages[:8])}

Reply with one line: verdict, whether the instruction changed, and the biggest
thing you fixed or flagged. Do not paste the JSON."""


def solvability_prompt(deck) -> str:
    import json as _json
    t = _json.loads((deck.root / "task.json").read_text())
    assets = "\n".join(
        f"      {a.get('file')}  — {a.get('why') or a.get('kind')}"
        for a in t.get("assets") or []) or "      (none)"
    degs = "\n".join(f"    {d.get('id')}  p{d.get('slides')}"
                      for d in t.get("degradations") or [])
    return f"""Judge whether one degraded PPT task can actually be solved.

FIRST: read {skill_path('ppt-task-solvability')} in full and follow it exactly.

Everything a solver would get is in one directory, and it is the only part of
the deck you may open:

  {deck.root / 'bundle'}/
    input.pptx        the broken file
    instruction.md    verbatim, all the solver is told
    assets/
{assets}

  the task claims {len(t.get('degradations') or [])} separate breaks:
{degs}
  and declares difficulty {t.get('difficulty')} / {t.get('est_steps')} steps.

Read nothing else under {deck.root}/ — the rest of that directory is the answer
key. The pipeline checks every read you make and discards your verdict without
looking at it if one lands outside the bundle. Writing your report there is
fine, and so is naming those files in prose; it is opening them that voids the
run.

Do not repair anything. Write your findings — the schema is in the skill — to:
  {deck.root / 'solvability.json'}

Reply with one line: verdict, and the single most important finding."""


def repair_prompt(deck, rework=None, source: str = "") -> str:
    """The work order comes from whichever gate rejected the deck.

    Reading it out of `task.json` alone was a break in the loop: a deck the
    solvability probe rejected reached the repairer with an empty work order
    and nothing to do, so it would have been "repaired" without a change.
    """
    import json as _json
    t = _json.loads((deck.root / "task.json").read_text())
    if rework is None:
        rework, source = (t.get("rework") or []), "task.json"
    lines = "\n".join(
        f"  [{i+1}] stage={r.get('stage')}  {r.get('what')}"
        f"\n      why: {r.get('why', '')}" for i, r in enumerate(rework))
    from .pipeline import repairs_done
    n = repairs_done(deck)
    verdict_note = t.get("verdict_reason", "")
    sj = deck.root / "solvability.json"
    if source.startswith("solvability") and sj.exists():
        s = _json.loads(sj.read_text())
        verdict_note = (f"the solvability probe called this task "
                        f"{s.get('verdict')!r}: {s.get('summary', '')}")
    return f"""Repair one PPT task that a gate rejected.

FIRST: read {skill_path('ppt-task-repair')} in full and follow it exactly.

Your deck: {deck.id}  ({deck.meta().get('name')})
  rejected by: {source or 'reconcile'}
  verdict  : {verdict_note}
  task     : {deck.root / 'task.json'}   (READ ONLY — never write it)
  proposal : {deck.proposal}
  recipe   : {deck.recipe}
  delta    : {deck.delta}
  assets   : {deck.root / 'assets' / 'manifest.json'}
  renders  : {deck.renders}/p-NN.png
  report   : {deck.root / 'solvability.json'}   (the full findings, if present)
  log      : {deck.root / 'repair.md'}   (append, never overwrite)

Repair attempt {n + 1}. Previous attempts are preserved under
{deck.root / 'attempts'}/ — read them before repeating a fix that did not work.

Work order:
{lines}

Change the upstream artefact each entry names. Do not run the later stages
yourself; the pipeline re-runs them and reconcile judges again.

Reply with one line: which stage you repaired, what you changed, and whether
you expect it to pass. Do not paste JSON."""


def recipe_prompt(deck) -> str:
    proposal = json.loads(deck.proposal.read_text())
    tasks = proposal.get("tasks") or []
    task = tasks[0] if tasks else {}
    return f"""Turn a task proposal into an executable degradation recipe, for
one deck.

FIRST: read {skill_path('ppt-degrade-recipe')} in full and follow it exactly.

Your deck: {deck.id}  ({deck.meta().get('name')}, {deck.meta().get('slides')} slides)
  proposal : {deck.proposal}
  digest   : {deck.digest}
  renders  : {deck.renders}/p-NN.png
  source   : {deck.source}
  write to : {deck.recipe}

The proposal's task is "{task.get('name', '?')}" with
{len(task.get('degradations') or [])} degradations on slides
{task.get('slides')}. Implement every one of them.

Look at the renders before choosing any path — a digest entry called `path 3`
is only identifiable against the picture.

Then reply with one line: how many steps on how many slides, plus anything you
had to approximate or skip. Do not paste the JSON."""
