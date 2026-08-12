"""Prompt construction for the four judgement stages.

The harness executes text; this module owns that text and the small amount of
deck summarisation needed to build it. Keeping it separate makes prompt edits
reviewable without mixing them with retries, subprocesses, or event parsing.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import profiles


RESOURCE_ROOT = Path(__file__).resolve().parents[1] / "resources"
SKILLS = RESOURCE_ROOT / "skills"


def skill_path(name: str) -> str:
    path = SKILLS / name / "SKILL.md"
    if not path.exists():
        raise FileNotFoundError(f"no skill {name!r} at {path}")
    return str(path)


def propose_prompt(deck) -> str:
    focus = profiles.assigned_focus(deck)
    focused = (f"\nThis deck is assigned focus `{focus}`. Produce only a "
               "coherent task in that native feature family and set the "
               "task's `focus` to that exact value; if no safe and uniquely "
               "recoverable target exists, return no task.\n"
               if focus else "")
    return f"""Propose computer-use RL tasks from one real PPT deck.
{focused}

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
    task = (json.loads(deck.proposal.read_text()).get("tasks") or [{}])[0]
    pages = sorted({p for group in task.get("degradations") or []
                    for p in group.get("slides", [])})

    unmet = []
    manifest = deck.root / "assets" / "manifest.json"
    if manifest.exists():
        try:
            unmet = json.loads(manifest.read_text()).get("unmet") or []
        except (OSError, ValueError):
            unmet = []
    could_not = ""
    if unmet:
        lines = "\n".join(
            f"  - {item.get('kind')} for slide(s) {item.get('slides')}: "
            f"{str(item.get('why'))[:160]}" for item in unmet[:8])
        kinds = ", ".join(sorted({str(item.get("kind")) for item in unmet}))
        could_not = f"""
MATERIALISE COULD NOT PRODUCE {len(unmet)} ASSET(S) THE TASK PROMISES:

{lines}

The instruction offers these to the solver and they do not exist. Do exactly
one of the following — doing neither is a rejection:

  (a) set `verdict` to "needs_rework", with a `rework` entry naming the stage
      that has to change (proposed / recipe / materialise) and what, or
  (b) deal with it in the record and say so — the words {kinds} must appear
      literally in `notes`, or in the note on the degradation that wanted
      them, because a substring match is how the gate reads this.

What you may not do is quietly drop the promise from the instruction and say
nothing about it.
"""

    return f"""Reconcile one degraded PPT task against its own instruction and
assets, then write the final task record.
{could_not}
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
  python -m pptxgym.office.tools pair {deck.root} {' '.join(str(p) for p in pages[:8])}

Reply with one line: verdict, whether the instruction changed, and the biggest
thing you fixed or flagged. Do not paste the JSON."""


def solvability_prompt(deck, ws=None) -> str:
    """Build the sealed witness prompt in the workspace it can actually see."""
    task = json.loads((deck.root / "task.json").read_text())

    def line(value, *keys, prefix=""):
        if isinstance(value, str):
            return f"{prefix}{value}"
        if isinstance(value, dict):
            values = [value.get(key) for key in keys]
            head = values[0] if values else None
            tail = next((item for item in values[1:] if item), None)
            return f"{prefix}{head}" + (f"  — {tail}" if tail else "")
        return f"{prefix}{value!r}"

    assets = "\n".join(line(item, "file", "why", "kind", prefix="      ")
                       for item in task.get("assets") or []) or "      (none)"
    degradations = "\n".join(
        line(item, "id", "slides", prefix="    ")
        for item in task.get("degradations") or [])
    bundle = ws.bundle if ws is not None else deck.root / "bundle"
    report = ws.report if ws is not None else deck.root / "solvability.json"
    sealed = ("The pipeline's own directories are not merely off limits, they "
              "are not there: this process runs in a namespace where the deck "
              "directory and the corpus do not exist. There is nothing to "
              "resist and nothing to find."
              if ws is not None and ws.kind == "namespace+deny" else
              "The deck directory is denied to you by the permission system "
              "and every read you make is checked against it afterwards.")
    return f"""Judge whether one degraded PPT task can actually be solved.

FIRST: read {skill_path('ppt-task-solvability')} in full and follow it exactly.

Everything a solver would get is in one directory, and it is your whole world:

  {bundle}/
    input.pptx        the broken file
    instruction.md    verbatim, all the solver is told
    assets/
{assets}

  the task claims {len(task.get('degradations') or [])} separate breaks:
{degradations}
  and declares difficulty {task.get('difficulty')} / {task.get('est_steps')} steps.

{sealed} So work from the bundle and from {ws.dir if ws is not None else bundle.parent}/,
which is yours; naming a pipeline file in prose is fine, and there is no file
outside the bundle worth going to look for.

`python3 -m pptxgym.…` works here as it always has.

Do not repair anything. Write your findings — the schema is in the skill — to:
  {report}

Reply with one line: verdict, and the single most important finding."""


def _degradation_checklist(task: dict) -> str:
    """List every proposed degradation the recipe must implement."""
    out = []
    for item in task.get("degradations") or []:
        if not isinstance(item, dict):
            out.append(f"  - {item}")
            continue
        what = (item.get("what") or item.get("anchor")
                or item.get("disclosure_detail") or "")
        out.append(f"  - {item.get('id')}  slides {item.get('slides')}"
                   + (f"  — {str(what)[:90]}" if what else ""))
    return "\n".join(out) or "  (the proposal lists none — that is a defect)"


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
{task.get('slides')}. Implement every one of them:

{_degradation_checklist(task)}

Every id above must appear as the `deg` of at least one recipe step. A
degradation with no step is the single commonest way a deck is rejected here —
five times across three runs, and twice it was the same id on the same deck
on a second attempt. The instruction still asks the solver for that work,
nothing broke, so nothing it produces can be scored. If one of them cannot be
done on this deck, say so in your reply rather than leaving it out silently: it
is the proposal that is then wrong, and fixing it is not your job here.

Look at the renders before choosing any path — a digest entry called `path 3`
is only identifiable against the picture.

Then reply with one line: how many steps on how many slides, plus anything you
had to approximate or skip. Do not paste the JSON."""
