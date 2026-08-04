---
name: ppt-task-solvability
description: Decide whether a degraded PPT task can actually be solved — is the required end state determinate from what the solver is given, or does something have to be guessed. Produces evidence, not a fixed file.
---

# Can this task actually be done?

What `reconcile` judges is **consistency**: are the instruction and the file
talking about the same thing? It never once tries to do the task, so it cannot
catch four classes of problem:

| | |
|---|---|
| **unsolvable** | every promise was kept, but the information is not enough to determine a unique answer |
| **given away** | the answer leaks in a way a static check cannot see |
| **ambiguous** | there are several end states that all make sense |
| **overdetermined** | too much was supplied and the task degenerates into copying |

Your job is to find these four.

---

## You may only look at what the solver can look at

| given to you | explicitly forbidden |
|---|---|
| `input.pptx` — the broken file | `source.pptx` — the original |
| `task.json`'s `instruction` and `assets` | `delta.json` — every change |
| the assets in the `assets/` directory | `recipe.json` — how it was broken |
| | `proposal.json` — the original intent |

**This is not a matter of discipline, it is the precondition for this step
meaning anything at all.** Reading the delta is reading the answer key, after
which "it can be done" carries no information.

**The pipeline scans your log.** Read any of the files on the forbidden list and
this step is marked failed outright, without your conclusion being looked at.

---

## Do not actually fix it

Do not write `input.pptx`, and do not write anything other than your output
file.

**What you are producing is an answer key, not a fixed file.** For each
degradation, answer three questions:

1. **What must the end state be?** Specific enough to be scored.
2. **What evidence pins it down?** A sibling element on the slide? A reference
   image? A number in the instruction?
3. **Which parts can I not determine?** Say which part, and why.

Question 3 is the most valuable output of this step. **Anything you cannot write
evidence for is underdetermined.**

You may use `python -m pptxgym.tools shapes <deck-dir>` to look at the broken
file's structure, and you may unzip `input.pptx` and read the XML — the solver
can do that too, and if it turns up the answer, that is exactly the "given away"
you are here to report.

---

## The specific criteria for the four classes

**Unsolvable / underdetermined.** You cannot deduce a degradation's end state
from what you have been given.
Typical: the one and only copy of that content was deleted along with the shape,
there is no copy anywhere else in the deck, and no reference image was provided.

**Given away.** Any one of:
- unzipping `input.pptx` reveals the text or data of the deleted content
- another slide carries a fully equivalent twin element that can just be copied
- the reference image provided draws the answer outright, while this
  degradation's `disclosure` was supposed to be masked or described

The first two are bugs and must be reported. The third depends on whether that
degradation's `disclosure` in `task.json` was meant to be given that way — if it
was, it is not a problem.

**Ambiguous.** You can think of two end states that both make sense but produce
visibly different results. Say which two, and what it would take to
disambiguate.

**Overdetermined.** The assets lay the answer out directly, the task degenerates
into copying, and the agent learns nothing.

---

## While you are at it, estimate a step count

Estimate how many GUI steps this task really takes, using the magnitudes in
`ppt-task-proposal`: adding one card ~30, redrawing a set of callouts ~60,
building a chart ~90, rebuilding a whole slide ~150.

If it differs from `est_steps` in `task.json` by a band or more, report it —
the difficulty was set at the proposal stage, and you are the first to estimate
it against **the real broken file**.

---

## Output: `solvability.json`

```json
{
  "verdict": "solvable | undetermined | leaked | ambiguous | overdetermined",
  "verdict_reason": "one sentence",
  "degradations": [
    {"id": "d1", "slides": [4],
     "end_state": "slide 4 must show the world production chart again, in the same position as before",
     "evidence": "assets/p04-Picture-3.emf is the original image; the masked reference image gives its frame position",
     "determinate": true,
     "undetermined": ""}
  ],
  "leaks": [
    {"what": "unzipping input.pptx reveals the five deleted statute names in ppt/diagrams/data5.xml",
     "where": "ppt/diagrams/data5.xml"}
  ],
  "est_steps_measured": 240,
  "est_steps_declared": 290,
  "rework": [
    {"stage": "materialise",
     "what": "slide 15 needs a reference image",
     "why": "those four sentences went with the SmartArt, they are nowhere else in the deck, and the solver has no way of knowing them"}
  ]
}
```

- when `verdict` is not `solvable`, **`rework` is mandatory**, and it must name
  which of `proposed` / `recipe` / `materialise` to go back to — the pipeline
  uses it to decide what to re-run
- when `leaks` is non-empty, the verdict should be `leaked` even if every
  degradation is determinate
- **"it cannot be done" is a valid answer.** Insisting it can is putting an
  unsolvable sample into the dataset

---

## The principle in one sentence

**You do not fix the task, you report its state.**

"It cannot be done" triggers the repair loop, and the least effortful way to
repair is **to make the task simpler**. So your report must be precise about
**which one thing is missing**, not "it is too hard" — the former can be
supplied, the latter will only be used to whittle the task down.
