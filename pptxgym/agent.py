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
    return {"status": "exited", "returncode": proc.returncode, "log": str(log)}


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


def repair_prompt(deck) -> str:
    import json as _json
    t = _json.loads((deck.root / "task.json").read_text())
    rw = t.get("rework") or []
    lines = "\n".join(
        f"  [{i+1}] stage={r.get('stage')}  {r.get('what')}"
        f"\n      why: {r.get('why', '')}" for i, r in enumerate(rw))
    n = len(list((deck.root / "attempts").glob("reconciled-*")))
    return f"""Repair one PPT task that reconcile rejected.

FIRST: read {skill_path('ppt-task-repair')} in full and follow it exactly.

Your deck: {deck.id}  ({deck.meta().get('name')})
  verdict  : needs_rework — {t.get('verdict_reason', '')}
  task     : {deck.root / 'task.json'}   (READ ONLY — never write it)
  proposal : {deck.proposal}
  recipe   : {deck.recipe}
  delta    : {deck.delta}
  assets   : {deck.root / 'assets' / 'manifest.json'}
  renders  : {deck.renders}/p-NN.png
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
