# CUA-Gym.pptx

Turn real PowerPoint decks into RL training tasks for computer-use agents.

Point it at a directory full of `.pptx` files and the pipeline works out the
structure of each deck, decides what task it should yield, materialises that
task as a **genuinely broken file**, and at every step blocks substandard
output with executable criteria.

```bash
pip install -e .
pptxgym ingest corpus/
pptxgym run --workers 6                       # 6 agent stages at once
pptxgym status
```

**Concurrency comes in two currencies.** `--workers` (i.e. `--agent-workers`)
caps how many `claude -p` processes run at once; `--cpu-workers` caps how many
soffice / render jobs run at once (default cores/4). Measured over 10 decks,
the agent stages take 85% of the wall clock, so a single unified number either
starves rendering or blows up the API — we have hit both. Decks themselves are
not rate-limited: a slot is claimed **per stage** and returned the moment the
stage ends, so a deck stuck in the repair loop does not sit on resources it is
not using.

---

## Where it has got to

```
ingested → inspected → proposed → recipe → degraded → materialised → reconciled → solvable
           script       agent      agent    script     script          agent       agent

         → scored → hardened → packaged
           script     script     script
```

`materialise` actually produces the assets the instruction promises: reference
images, **masked reference images**, deleted pictures, value CSVs for
charts/tables, animation keyframes. The masking looks like a judgement call but
is not — `delta.json` records the original bbox of every degradation, and the
mask is the union of those boxes.

`reconcile` is the last judgement gate, and it answers three questions: does
the damage the instruction describes match the damage in the file? Is what the
instruction promises actually in `assets/`? After the approximations, is the
difficulty still right? It produces `task.json` and may return a verdict of
`needs_rework`.

`solvable` asks what reconcile cannot: **can this task actually be solved?**
reconcile checks consistency; it never once tries to do the task. So a task can
pass reconcile perfectly and still be unsolvable, given away, ambiguous or
overdetermined.

The crux of this stage is the **information barrier**: saying "it can be done"
while holding the answer key carries no information at all. **The barrier is
structural, not requested**: before the run starts, everything the solver would
get is copied into `bundle/` (the broken file, `instruction.md`, `assets/` —
not the manifest, which records why the damage was done), and the probe works
only inside that directory. That directory is also the shape the task ships in;
it is not scaffolding.

Log scanning stays as a backstop, but the rule has changed: **what is judged is
"it read outside `bundle/`", not "a filename appeared in a string"**. The
earlier version substring-matched on filenames, and it was wrong in both
directions — `grep -rn "source.pptx" pptxgym` (reading our own code) counted as
peeking, a probe writing "I did not open source.pptx" in its report counted as
peeking, while actually opening `../source.pptx` looked clean. **Four probe
runs out of ten were voided this way**, four valid conclusions thrown away.

What it produces is evidence (per-degradation end state / evidence /
undetermined items, a leak list, a measured step count), not a fixed file.

### What happens when something is sent back

**Five gates share one repair loop; it is not five mechanisms.** `reconcile`
returning `needs_rework`, `solvable` returning anything other than `solvable`,
`scored` rejecting the plan, `hardened` being broken, `packaged`'s consistency
check reporting `fail` — **the pipeline neither stops nor pretends not to
notice**. The two agent gates write their own `rework`; the latter three are
deterministic and always send the task back to `recipe` (the floor will not
come down, the attacks score, the instruction and the file disagree — all three
say **the damage itself** was the wrong choice, not that it was not done well
enough). `repair` (an orchestrator agent) changes that upstream artefact, and
Python invalidates and re-runs the affected downstream stages. At most 3 times,
after which it is marked `needs_human` and left there. **The verdict that gave
the order retires with it** — `solvability.json` / `plan.json` / `attacks.json`
/ `consistency.json` are archived and deleted, otherwise the next round reads
the same complaint again and repairs a fixed deck all the way to
`MAX_REPAIRS`.

**A pass mark invalidates itself, in both directions.** Every stage records a
content hash of what it read (`_in` in `state.json`). The moment an upstream
artefact changes — a manual re-run, a fixed executor, an edited recipe — the
downstream ✓ becomes `≈ stale` and re-runs, and it **propagates along the
chain**: change the recipe and everything from `degraded` to `packaged` goes
stale. The hash is over content, so a re-run that produces the same bytes
causes no false invalidation.

The other direction is invisible to hashes: **a gate saying "no" usually
changes no file at all**, so every ✓ below it stays untouched. `deck0008` sat
in exactly that state — reconcile had returned `needs_rework` while `solvable`
still carried the previous round's ✓, so the entire deterministic tail could
happily score, attack and package a task that had already been rejected. **A
verdict that has not been withdrawn is not the same as a verdict that still
holds**: a failing upstream now drags every mark below it to `stale`.

The loop has three locks against dilution:

- **the orchestrator may not write `task.json`** — the verdict belongs to
  reconcile, and editing it is issuing yourself a pass
- **archive before every re-run** into `attempts/<stage>-NN/`, keeping both
  artefacts and logs — otherwise "fixed it" and "laundered the verdict" cannot
  be told apart afterwards
- the skill requires a section in `repair.md` titled **"why this is not
  shrinking the task"**; not being able to write it usually means you are
  shrinking it

### After `solvable`: three deterministic stages

These three used to be **a manual sequence in somebody's head** — that is how
three decks became tasks. A sequence in your head cannot be resumed, does not
invalidate itself when something upstream changes, and worst of all **it never
refuses**. Now they are stages, their criteria are executable, and a "no" sends
the task back to `recipe`.

| stage | what it does | when it says no |
|---|---|---|
| `scored` | derives `plan.json` from `delta.json`, one component per change | ground truth is not 1.000, the broken file is not 0.000, some component's floor exceeds 0.15, some degradation has nobody scoring it |
| `hardened` | runs the [attack battery](attack-report.md): 14 cheats + 6 **legitimate variants** | any cheat clears the threshold, or any legitimate solution scores nothing, or an applicable attack **cannot be constructed** (a gate that has never fired is not a gate) |
| `packaged` | mechanical `consistency` check + `emit` writes out a runnable task | `consistency` reports `fail` (instruction and file contradict each other). `warn` is recorded, not blocking |

The two known points in `scored` need no agent at all: the ground truth is
necessarily a full score, and the broken file handed to the solver is
necessarily zero. **If either fails to hold, what needs changing is the recipe,
not the tolerance** — the reasoning is in [REWARD.md](REWARD.md), which records
the measured renderer drift and font differences, and why tolerance is exactly
the attack surface for reward hacking.

`hardened`'s `gt_roundtrip` really does open a WPS window, so a machine without
WPS **cannot** harden a task; it can only say so outright (`--no-wps`), and
that sends the task back as "an unverified gate".

`consistency`, inside `packaged`, used to be **on no code path at all**. Of the
four trajectories from the last batch that anyone read, two died on defects it
would have caught, and reconcile had waved both through.

---

## Design

**A skill lives wherever the judgement lives; everything else is a script.**

Skills enter the context; scripts are merely executed. So only two things are
skills:

| skill | why it cannot be code |
|---|---|
| `ppt-task-proposal` | which slide is worth a task, which difficulty band, how far away the reference sits, how to write the instruction — no assertion can express any of that |
| `ppt-degrade-recipe` | plain English → shape path requires looking at the render, and requires running it once and checking the result |

census / digest / render / degrade / smartart / charts / the integrity gate are
all ordinary modules; the agent does not need to read them, only to run them.
The idioms live in [TOOLS.md](TOOLS.md), not in a skill.

**Agent files carry the job contract, skills carry the domain judgement.**
`.claude/agents/*.md` says only "here is your input, here is the file you must
output, here is the definition of done"; all of the thinking is in the skill.
That is why the same skill can be shared by batch mode and manual mode.

**Stages hand off only through files.** Nothing lives in a conversation, so any
step can be re-run on its own, a human can take one step over and hand it back,
and a run can resume from a break.

**Whoever writes the recipe does not stamp their own work.** The recipe-writer
must run the recipe and look at the render — otherwise there is no way to know
the paths were chosen correctly — but **executing** and **committing** are two
different things. It runs `tools trial` into a scratch directory, where neither
artefacts nor state persist; the real commit is done by the orchestration
layer, and the deck lock will refuse its call to `pptxgym degrade`. Otherwise
that ✓ in `status` is one the author stamped themselves.

**Done ≠ the file exists.** Every agent stage is re-validated after it runs: the
proposal must parse, have all its fields, have a difficulty band and step count
that agree with each other, and cite slide numbers that exist; the recipe must
use only registered operators, stay in range on slide numbers, and not be a
no-op. Anything substandard is marked `failed` with a log left behind; it does
not carry plausible-looking garbage further down the chain.

---

## Layout

```
.claude/
  agents/    proposer.md  recipe-writer.md          job contracts, a few dozen lines
  skills/    ppt-task-proposal/  ppt-degrade-recipe/  domain judgement
pptxgym/
  census.py styles.py text_style.py                 OOXML parsing
  render.py anim_steps.py deck_digest.py            render / animation / structural digest
  degrade_exec.py smartart.py charts.py             degradation execution
  pkg_check.py                                      integrity and answer-leak gate
  pipeline.py agent.py cli.py tools.py              state machine / headless agent / CLI
work/<deck-id>/                                     one directory per deck
```

---

## One deck directory

```
work/deck0001/
  meta.json  source.pptx  digest.json  digest.min.json  renders/p-NN.png
  proposal.json  recipe.json  input.pptx  delta.json  state.json
  proposed.jsonl  recipe.jsonl            full transcript of each agent stage
```

`source.pptx` is both the input and the ground truth; no stage writes it.
`delta.json` records every change **together with the value it had before**, so
the same record can both build the file and describe what the solver has to
restore.

---

## Two mechanisms you have to know about

**Answer leaks.** Deleting a shape from the spTree is not enough: the picture's
bitmap, SmartArt's `data*.xml` (which holds the text of every node) and a
chart's embedded workbook are all still alive, and `unzip` reads them. The
executor clears the relationships along with the shape and the gate re-checks —
it only counts as passing when `degrade` prints `gate=ok`.

**Composite objects need partial edits.** Dropping one column of a SmartArt,
one series of a chart, one row of a table, or restyling only certain paragraphs
each has its own dedicated entry point. Deleting the whole thing destroys the
surviving elements, which were the **anchor**, and turns "fill it back in from
the pattern" into "rebuild it from nothing" — the difficulty and the point of
the task both change.

---

## What you need

- Python 3.10+
- LibreOffice (`soffice`) and Poppler (`pdftoppm`) — for rendering
- Claude Code CLI (`claude`) — for the agent stages

The headless agent goes through permission prompts by default. For batch runs
set `PPTXGYM_SKIP_PERMISSIONS=1`; it only reads and writes under `work/`.
