---
name: ppt-task-reconcile
description: Check a degraded PPT task against its own instruction — does the broken file still match what the solver is told, are the promised assets there, is the difficulty still honest — and produce the final task record. Use after materialise, as the last gate before a task is considered real.
---

# Reconciling: are the instruction and what happened to the file still the same thing?

The instruction was written at the **proposal** stage, when there was no file
yet. Since then the recipe stage may have approximated some things, and the
asset stage may have produced only part of what was promised. **Nobody has gone
back and checked.**

Your job is to check, and then to produce this task's final record, `task.json`.

**You are the last human-style judgement on this chain.** Once past you, this
task is treated as real.

---

## The four things to look at

| file | what you are looking for |
|---|---|
| `proposal.json` | the instruction text, which assets were declared, what each degradation originally intended to break |
| `recipe.json` | what was actually done, **especially the approximations and skips written into each step's `_why`** |
| `delta.json` | every change, together with its prior value |
| `assets/manifest.json` | the assets actually produced |

Plus **the renders**:
`python -m pptxgym.office.tools pair <deck-dir> <slide numbers…>`
— the original and the broken file side by side, slide by slide. **You must look
at these**; it is the only way to verify that the file really is broken the way
the instruction says.

**Before you start, run the machine reconciliation once**:
`python -m pptxgym.evaluation.consistency <deck-dir>`

It runs **the decidable part** of the questions below ahead of you — whether the
things the instruction claims are broken can be found in `delta.json` or in the
difference between the two files, whether the ground truth can itself pass this
task, whether the assets shipped are simply the answer, whether the solver can
get at a deleted picture, whether any change belongs to no degradation.
Every `fail` it reports has to be accounted for in `task.json`.

**But "it did not report" does not imply "there is no problem"**: whether the
difficulty is right, whether the mask is sufficient, whether wording like
"knocked apart" describes the damage that was actually done — it cannot judge
any of that. That is still yours to look at.

---

## Four questions that must be answered

### 1. Does the damage the instruction describes match the damage in the file?

Compare every place the instruction mentions against `delta.json`, one by one.
The typical mismatches:

- **The instruction says "fill in the missing tier", and the whole block was
  deleted.** When the recipe approximates with a wholesale deletion, the
  surviving elements — the anchor — are gone, and the task goes from "complete
  the pattern" to "rebuild from nothing". The instruction has to be rewritten,
  and the difficulty most likely goes up.
- **The instruction says "these slides", and other ones were touched.** The
  recipe swapped in an equivalent slide.
- **The damage the instruction describes never happened** (the recipe skipped
  it). Then either delete that part of the instruction, or send the whole task
  back.

The last batch of ten tasks was shipped and run against a real model. Four
trajectories were read, and **two of them died on exactly this section, both
having been waved through at this gate**:

- **The instruction says a picture is missing; both pictures are there in the
  ground truth.** (deck0004 slide 9) The instruction reads "the illustrations
  that sat above the other two went with them", but in fact only the SmartArt's
  nodes were dropped: slide 9 of `delta.json` has nothing but a
  `smartart_drop_nodes` entry, and both pictures are lying there untouched in
  the broken file. The model spent dozens of steps looking for a picture that
  did not exist, and finally deleted the real SmartArt to make the slide make
  sense — 0.63 of work scored 0.
  **For every "something is missing" in the instruction, go to `delta.json` and
  find the change that produced it; if you cannot find it, it did not happen.**

- **The instruction demands that "slide 6 must be a real table, not a pasted
  picture", and the ground truth is a picture.** (deck0006) The original object
  on that slide is a `picture`, the reference image `p06--2.png` shipped with
  the task is byte-for-byte identical to it, and the one scorable component in
  the reward recognises exactly those bytes.
  **Following the instruction scores 0, and the only way to score is precisely
  the one the instruction forbids.** The model followed it and got 0.

**This section's real criteria are two sentences, and both must hold:**

> **One. The ground truth must be a legal solution to its own task.**
> Take every "what it should end up as" requirement in the instruction and check
> it against `source.pptx`; if it does not hold, the task has no correct answer.
> **For any wording that specifies an object type** — "a real, editable table",
> "a native chart", "not a pasted picture", "rather than a screenshot" — **go
> and look at whether that thing's `kind` in the ground truth is `picture` or
> `table`/`chart`/`smartart` before you write it down.**
> If it is a `picture`, you may not write that, and writing it sets the task an
> unreachable finish line.
>
> **Two. Every "this is broken" in the instruction must correspond to a change
> that really exists in `delta.json`.**
> If it does not, delete that sentence from the instruction, or send it back to
> `recipe`; do not paper over it by rewording.

Three traps from the same source, all visible directly in the existing
artefacts, so check them while you are here:

- **The asset shipped is itself the answer.** A reference image / original
  bitmap that is byte-identical to the deleted object is **permitted** on its
  own (a cropped scan cannot be drawn, so it has to be given).
  But **the moment the instruction also demands "rebuild it as something
  else"**, the two requirements are mutually exclusive — pasting the file scores
  and following the instruction does not. That is deck0006.
- **The solver simply cannot get at the deleted picture.** Its bytes are not in
  `assets/`, and no slide in `input.pptx` still draws it — so only `source.pptx`
  has it. The ground truth will then be judged by the media gate as "pasted the
  original asset back in". Either put it in `assets/`, or change the goal to one
  that does not need those bytes.
- **An entry in `delta.json` carries no `deg`.** That change belongs to no
  degradation, so no sentence of the instruction corresponds to it. The reward
  stage will score "a job nobody asked for", and the solver has no way of
  knowing it has to do it.

### 2. Is what the instruction promises actually in `assets/`?

Every time the instruction says something like "the reference image shows…",
"the logo file is provided", "the data is below", that is a promise to the
solver.
**Find them sentence by sentence and check each one against
`assets/manifest.json`.**

Promised but absent → either change the instruction so it no longer promises it
(if the task is solvable without it), or the task does not stand up. **Do not
pretend it is there.**

### 3. The other way round: does the instruction give the game away?

Once the assets have been produced, ask again:

- For a slide given a complete render, is the answer thereby given? This is
  **permitted**, but confirm the proposal designed it that way
  (`disclosure: reference_image`) rather than intending a masked image and
  producing a complete one.
- Is the area the masked image covers sufficient? **Go and look at the image.**
  If a corner of the damaged thing is still showing, or an unmasked twin element
  beside it lays the answer out, that has to be flagged.

### 4. Is the difficulty still right?

Approximation changes the workload. Deleting a whole block is much heavier than
filling in a gap.
Re-estimate against the proposal's magnitudes: adding one card ~30 steps,
redrawing a set of callouts ~60, building a chart ~90, rebuilding a whole slide
~150.

If you change it, say why in `notes`. **≤100 easy / 100–300 medium / 300+
hard.**

---

## The rules for changing the instruction

Exactly as at the proposal stage, not relaxed by a word:

**State the target state only, never the steps.**

| ❌ | ✅ |
|---|---|
| "Move the second picture 200pt to the left" | "The images on this slide are out of order" |
| "Select the title and change the fill to #A92D55" | "Some elements no longer match the deck's colour scheme" |

- No precise values, unless the value is itself information being supplied to
  the agent
- You may say which slides have problems; that is not giving the game away
- Keep the voice of a real working situation, not "Task: please perform the
  following operations"
- **If it does not have to change, do not change it.** The smaller the change
  the better; you are reconciling, not rewriting.

---

## Output: `task.json`

```json
{
  "name": "<keep the task name from the proposal>",
  "instruction": "the English instruction text — the version as checked, and amended where necessary",
  "instruction_changed": true,
  "difficulty": "medium",
  "est_steps": 290,
  "assets": [
    {"kind": "reference_image", "file": "reference-p13.png", "slide": 13,
     "masked": false, "why": "this two-column plate layout exists only on this slide"}
  ],
  "degradations": [
    {"id": "d1", "slides": [4], "implemented": "as_proposed|approximated|skipped",
     "what_the_file_looks_like": "one sentence: what this slide looks like now",
     "note": "what was approximated and what it cost (may be empty)"}
  ],
  "notes": "why the instruction was changed, why the difficulty was adjusted, what known weaknesses remain",
  "verdict": "ready|needs_rework",
  "verdict_reason": "one sentence",
  "rework": [
    {"stage": "materialise",
     "what": "slide 15 needs a reference image — the four sentences went with the SmartArt and are nowhere else in the deck",
     "why": "a fifth of the task's answer is unknowable to the solver, and changing the instruction cannot supply it"}
  ]
}
```

- the `file` in `assets` **must** be the name of a file that really exists under
  `assets/`; the pipeline checks each one
- when `instruction_changed` is true, `notes` may not be empty
- a `verdict` of `needs_rework` is **a valid answer**: when the file and the
  instruction do not match and amending the instruction cannot save it, it
  should go back, not be forced into a task that runs but is mislabelled
- **when the verdict is `needs_rework`, `rework` is mandatory**, and it must say
  **which step it goes back to**: `materialise` (the asset was not produced /
  the mask covered the wrong thing), `recipe` (the wrong thing was deleted),
  `proposed` (this degradation does not stand up in the first place). The
  pipeline uses it to decide which stages to re-run; a paragraph of prose is
  something nobody can act on, and the validator will reject it

---

## Scope discipline

Your turn budget is 40 and the machine consistency check has already run the
decidable half of this manual. Spend your turns where only judgement works:
the instruction-versus-file mismatches of section 1, and the renders. Do not
re-derive what `consistency` already reported, do not audit slides the task
does not touch, and when a finding would not change `verdict`, `instruction`
or `rework`, note it in `notes` in one line and move on.

## The principle in one sentence

**If you cannot do it, write that down honestly; do not pretend you did.**

Under-reporting is far worse than not being able to do it. Not being able to do
it is a known tooling gap and can be filled; under-reporting puts a sample into
the dataset **whose label does not match its content**, and it runs all the way
into training with nobody ever able to detect it again.
