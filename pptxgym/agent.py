"""Run a stage that needs judgement, as a headless `claude -p` subprocess.

Two rules learned the hard way and worth keeping:

**Point at the skill by absolute path and say "read it in full".**  Skill
auto-discovery is not something to rely on in headless mode; an explicit path
worked on every one of the seventeen agents run so far.

**Never let file existence mean success.**  The agent's job is done when the
file it wrote passes this pipeline's own checker, which is why every runner
takes a `check` callable and re-runs it after the process exits.
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


async def run_agent(spec: AgentRun) -> dict:
    cmd = ["claude", "--agent", spec.name, "-p", spec.prompt,
           "--max-turns", str(spec.max_turns),
           "--output-format", "stream-json", "--verbose",
           "--allowedTools", ",".join(spec.allowed_tools)]
    if spec.model:
        cmd += ["--model", spec.model]
    if os.environ.get("PPTXGYM_SKIP_PERMISSIONS") == "1":
        cmd += ["--permission-mode", "dontAsk"]

    log = spec.log or (spec.cwd / f"{spec.name}.jsonl")
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
            **_infra_failure(log)}


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
    try:
        tail = log.read_text(errors="replace")[-4000:]
    except OSError:
        return {}
    for line in reversed(tail.splitlines()):
        if '"type":"result"' not in line:
            continue
        try:
            d = json.loads(line[line.index("{"):])
        except (ValueError, json.JSONDecodeError):
            return {}
        why = d.get("terminal_reason")
        if why in ("api_error", "auth_error"):
            return {"status": "infra",
                    "why": f"{d.get('api_error_status') or ''} "
                           f"{str(d.get('result'))[:120]}".strip()}
        if why in ("max_turns", "max_tokens", "refusal"):
            return {"status": "truncated",
                    "why": f"the run stopped on {why} — whatever it wrote is "
                           f"an interrupted answer, not a finished one"}
        return {}
    return {}


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
  recipe   : {deck.recipe}          (read every step's `_why`)
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
