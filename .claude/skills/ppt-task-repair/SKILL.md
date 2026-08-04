---
name: ppt-task-repair
description: Fix a PPT task that reconcile rejected — read the rework directive, change the upstream artefact that caused it, and let the pipeline re-run. Use only when a deck's verdict is needs_rework.
---

# Repairing a task that was sent back

`reconcile` returned `needs_rework` and stated **which step it goes back to** in
`task.json`'s `rework`. Your job is to **change that upstream artefact** and then
let the pipeline re-run.

**You do not judge, you only repair.** The judgement belongs to `reconcile` — it
will run again, and whether this passes is for it to say.

---

## Four red lines you may not cross

**1. You may not write `task.json`.** That is reconcile's output. Editing it is
**issuing yourself a pass**, and the last independent judgement on the whole
chain is gone.

**2. You may not change `source.pptx`.** It is the ground truth.

**3. You may not change any code under `pptxgym/`.** What you are repairing is
**one deck**, and the tooling is **shared by every deck**. Changing a producer
or a gate inside a repair loop reaches far beyond the task in your hands, and
nobody reviews it.
The most dangerous form is easy to recognise: **changing the gate so it stops
complaining, instead of fixing the deck** — that looks exactly like having fixed
it.

If you genuinely find a defect in the tooling (it does happen; it has happened
once), **write it into `repair.md` and stop there**: the root cause, which decks
it affects, what you suggest changing. This deck stopping for a human is a valid
outcome.
And remember: even if you did change it, it would not take effect immediately —
your process imported that module long ago. The last time somebody did this, the
artefacts came out of the pre-fix module while the log said "fixed".

**4. You may not shrink the task until it passes.** This is the most dangerous
failure mode, and it looks like success: delete the degradation that will not
pass, make the instruction vaguer, drop the difficulty a band — `reconcile` then
passes, and the dataset has gained a **diluted** sample that nobody can spot.

> **The criterion: a repair should make the task solvable, not make it easy.**
> If your change means the agent has less to do, stop and work out whether you
> are laundering it.
> If it genuinely cannot be saved, say so in `repair.md` and let it stop for a
> human — **that is a valid outcome.**

---

## What you may change

Change whatever `rework[].stage` points at:

| stage | what to change | typical situation |
|---|---|---|
| `materialise` | the `assets` declaration in `proposal.json` | the promised reference image was not produced / the mask covered something that should be visible / a required asset is missing |
| `recipe` | `recipe.json` | the wrong thing was deleted / content unique in the deck was destroyed / a wholesale deletion where a local edit was called for |
| `proposed` | the degradations or the instruction in `proposal.json` | this degradation does not stand up in the first place (the anchor does not exist, the answer is not unique) |

Once you have changed it, **do not** run the later stages yourself — the
pipeline marks the affected stages for re-running and executes them
automatically.

---

## The four most common rejections, and how to fix them

**① The promised asset cannot be produced.**
Ask first: **can this asset come from a different source?** For example, the
proposal wants "a CSV of the chart's values" while the original is a bitmap — so
change `assets` to supply **the original image itself**, and rewrite the
proposal's instruction to match.
If there really is no alternative source, this degradation does not stand up;
fall back to `proposed`.

**② The mask covered something that should have been disclosed.**
`materialise`'s mask is the union of every bbox in the delta, and **it does not
judge whether any clue is left after masking**. If the broken thing fills the
whole slide, the masked image is a blank sheet. The fix is usually to change
this degradation's `disclosure` from `reference_image_masked` to
`reference_image`, or to `deck_anchor` (if another slide has sibling elements).

**③ The recipe destroyed content that is unique in the deck.**
The deleted thing has no copy elsewhere and no reference image was given —
unsolvable.
Either narrow the damage in `recipe.json` (leaving one sibling as an anchor), or
add a reference image of that slide to `assets` in `proposal.json`.

**④ A wholesale deletion where a local edit was called for.**
The two top-level keys `smartart` / `chart` exist for exactly this (see
`ppt-degrade-recipe`).
Replace that `delete` in `recipe.json` with the corresponding local edit.

---

## Output

After changing the upstream artefact, write a `repair.md`, appended after the
existing content (**do not overwrite**):

```markdown
## Repair N — <date>

**Reason sent back**: <copy the `what` from rework>

**What was changed**
- `recipe.json` p19: replaced the wholesale `delete` with `smartart.drop_text`, keeping the other four cells as anchors

**Why this is not shrinking the task**
- the scope of the damage is unchanged, the agent still has to rebuild two cells; what changed is that it now has a reference to work from

**What could not be fixed**
- (if any) …… and why
```

**The "why this is not shrinking the task" section is mandatory.** Not being
able to write it usually means you are shrinking it.

If it cannot be repaired this time, write down in `repair.md` where it is stuck
and what decision a human has to make, then stop.
