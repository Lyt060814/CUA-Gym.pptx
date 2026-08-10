---
name: ppt-task-proposal
description: Inspect a real PPT deck and propose complete computer-use RL tasks — what to break, what the agent sees, what he is told, the instruction text itself, and the difficulty. Use when turning a source deck into training tasks. Judge the design, not the implementation; someone else builds it.
---

# Proposing PPT tasks

## What it is you are actually designing

We are building training tasks for **computer-use reinforcement learning**.
Once a task is built, an agent will sit in front of WPS / PowerPoint and
complete it **with a mouse and a keyboard** — dragging, right-clicking, opening
the format pane, using the chart editor, drawing shapes, inserting pictures.

So the source of every criterion is one sentence:

> **What will this task make the agent do inside PPT software? Is that thing
> worth practising?**

What is worth practising is **one complete piece of work**: rearranging
scattered pictures, rebuilding a deleted chart, putting back a 3-D effect that
was flattened, reconstructing a whole block of content from a reference.

What is not worth practising is **flipping one switch**: changing a shadow
angle, toggling a boolean. Even if it is scorable, the agent is done in three
steps and learns no GUI skill at all.

**Work out what the agent has to do first, then work out what damage forces
that thing out.**

---

## How tasks and degradations relate

**One task = one broken file + one instruction**, but it can contain
**several degradations**.

```
deck
 └── task (one input file, one instruction, one difficulty)
      ├── degradation 1  (slide 3, rebuild the deleted chart)
      ├── degradation 2  (slide 5, a row of cards knocked apart)
      └── degradation 3  (whole deck, header labels inconsistent)
```

- One degradation = **one independent repair job**, with its own scope, anchor,
  reasoning level, interaction level and step estimate
- A degradation's scope can be **a single slide, several slides, or the whole
  deck**, and one task can mix them
- One deck can yield several tasks, each derived independently from the
  **original deck**; they do not stack

So what you have to deliver is:

| level | what you decide |
|---|---|
| **each degradation** | what breaks, what the agent has to do, how he can know the target, what reasoning and GUI interaction it requires |
| **each task** | which degradations make it up, what materials are provided, **the instruction text itself**, the overall difficulty and step count |

---

## What a good task looks like

- **Knock a group of pictures/shapes apart** → the agent lays them back out
- **Delete a chart** → the agent rebuilds one from the data and the style of
  its siblings
- **Flatten a 3-D / shadow / gradient effect** → the agent puts it back
- **Hollow out a whole block of content** → the agent rebuilds it from a
  reference
- **Delete half a row of cards** → the agent completes it from the surviving
  half
- **Wipe the animation** → the agent redoes it from the build keyframe sequence

What they have in common: **the action is clear, the work is real, and one
look tells you whether it came out right.**

### Task-shape catalogue

**Single slide / local**

| shape | what the agent has to do |
|---|---|
| rearrange | a group of elements knocked apart / misaligned → lay them back out by layout and alignment |
| rebuild an object | a chart, table or diagram deleted whole → make a new one |
| redo an effect | a group of elements' 3-D / shadow / gradient / outline wiped → put it back |
| rebuild a block | a large block of content hollowed out → rebuild it from a reference |
| refill content | text or data cleared → restore it from the materials or a sibling slide |
| restore relationships | connectors off their targets, pairings scrambled, order shuffled → restore the correct relationships |
| pull back into style | a group of elements has drifted from the deck's visual system → bring it back into line |
| redo animation | animation/transition wiped → redo it from the build keyframe sequence |

**Whole slide / whole deck**

| shape | what the agent has to do |
|---|---|
| rebuild a slide | a whole slide hollowed out (only the layout left) or deleted → rebuild the slide from the reference image |
| restore slide order | slide order shuffled → put it back by the thumbnails or the deck's own logic |
| cross-slide consistency | the table of contents does not match the sections, a slide has the wrong layout applied, page numbering is broken → fix the consistency of the whole deck |
| master-level repair | the master was broken so that several slides fail at once → decide between "fix the master once" and "fix each slide" |
| replicate from nothing | given the rendered PDF plus a materials pack, build the whole presentation from a blank deck |

The catalogue is a starting point, not a boundary. **Every deck has structure
of its own, and that is often where the better task is hiding.** Propose ideas
outside the catalogue boldly, as long as it is still "one complete piece of
work".

---

## Three design levers

For the same damage, turning these knobs produces tasks of completely different
quality:

**1. How much to delete — "leave half" is usually better than "delete all".**
Deleting 3 of a row of 6 cards is far better than deleting all 6: the 3
survivors pin down the size, the spacing, the font and the palette, and the task
goes from "invent it out of thin air" to "complete the pattern" — the answer is
unique and the agent has something definite to hold on to.
**If you can leave an anchor, leave an anchor.**

**2. How far away the anchor sits.**
A sibling element on the same slide (easiest) → one on another slide (medium) →
only a reference image (medium) → only the instruction's description (hardest).
Anchor distance is one of the best ways to deepen reasoning, and far healthier
than piling on quantity. It is not the only route to difficulty: specialised
GUI interaction can be hard even with an on-slide anchor.

**About positional degradations: look only at the numbers for the software that
does the scoring.**

> **The task is solved and scored in WPS, so only `renderer_drift.wps`
> constrains positional degradations; `renderer_drift.libreoffice` is a
> corpus-fragility signal, not a tolerance, and must never be used to veto a
> positional task.**

`digest.json`'s `deck_summary.renderer_drift` now records both renderers, and
marks `governs` (which one has the say) and `reading` (this deck's conclusion in
one sentence). How to use it:

- **`governs: "wps"` and `wps.changed_frac` is 0** — this is the case for all 10
  decks measured: WPS opens and saves and **does not move a single shape**. On
  these decks positional degradations have **no renderer noise**; knocking
  things apart to be rearranged is a perfectly good task, so propose it.
- **`governs: "wps"` but `wps.changed_frac` is not 0** — this is the real
  constraint: the displacement has to be far larger than `wps.drift_in.p90_in`,
  and do not make the kinds listed in `wps.kinds_that_move` the scored target.
- **`governs: null` (this deck has never been measured on WPS)** — unknown is
  not the same as safe: propose positional degradations anyway, but make the
  displacement **large and obvious**, prefer pictures, cards and diagrams as
  scored targets, do not use text boxes or tables themselves as positional
  targets, and write "WPS drift unmeasured" in the `note`.
- **`libreoffice.*` has exactly one use**: the harder it tears this deck up
  (`verdict: fragile`, a high `changed_frac`), the more the deck is built on
  fragile constructions, which is worth a sentence in `deck_read`.
  Measured: LibreOffice moved 7.6%–61.5% of shapes while WPS moved 0%, and
  almost all of that difference is text boxes and tables reflowing — **that is
  the proxy renderer's behaviour, not a property of this deck, and certainly not
  a tolerance.**

**3. Whether to supply materials.**
Pictures can be deleted but cannot be drawn — whenever the agent has to insert
a picture again, **the material must be supplied**. Same for data: to rebuild a
chart, the numbers need a source (written into the instruction, or supplied as
a data file).

---

## Three hard rules

**1. Do not think about implementation.** Do not wonder "is this easy to code",
"how do you change the XML". The implementer will solve that, and will write
new tools if the existing ones fall short. **Abandoning a good idea because you
are afraid it is hard to build is the gravest mistake in this job.**

**2. Do not propose atomic little changes.** A single property of a single
shape is **a building block at the implementation level**, not a task.
Self-test: if it comes out as "change property X of thing Y to Z" → too small;
if it comes out as "this group of things was messed up, make it right again" →
correct.

**3. One deck usually yields 1 task; do not shred it into pieces.**
- **Default target: 1 task per deck** (in practice it floats between 0 and 2).
  A 20-slide deck yielding three or four tasks has been **shredded** — the right
  move is to **merge those degradations into the same task**.
- **The degradations in one task do not have to be related to each other.**
  Rebuild the chart on slide 3, complete the cards on slide 8, fix the headers
  across the deck — these can perfectly well be one task. The real-world
  scenario is exactly "somebody messed this file up and several things are
  broken", and the instruction says so naturally.
- Only when you have genuinely found **two groups so different in nature that
  putting them together makes the instruction awkward** do you split off a
  second task; that is not common.
- **If there is no good target, hand in a blank.** A pure-text deck, or a deck
  that is mostly template slides, will only yield a low-quality task if you
  force one. `tasks: []` with a sentence of reasoning is a valid answer.
- Each task is **independent of the others**: each derives its own broken file
  from the original deck; they do not stack.

---

## How to read the deck

No forensics needed. Look at the renders, together with the structural digest,
and get hold of:

1. **What carries the information on each slide?** Parallel cards? A big chart?
   A flowchart? Or a wall of body text?
2. **Where is there repetition / grouping?** Repetition means an anchor — the
   least effortful and most reliable task structure there is.
3. **What has obviously been designed by hand?** Custom palettes, careful
   layout, 3-D effects, animation. Breaking these is what makes restoring them
   worth something, and a blind scripted change cannot guess them.
4. **What conventions hold between slides?** A contents page matching the
   sections, a shared master, a progressive build sequence — these are the
   handholds for deck-level tasks.

You do **not** need to look up colour values, compute data, or read the XML. A
proposal is a judgement, not a piece of research; the implementer will read the
concrete numbers out of the file. **Any specific number you write will be
re-checked, so do not make them up.**

---

## Self-check

Run every task through these:

1. **Is it enough of a job?** Three or five clicks does not count.
2. **Can you see at a glance that it is broken?** A change invisible in the
   full-slide render is a change the agent will never find.
3. **How is he supposed to know what it should look like?** An anchor has to
   exist: a sibling element on the slide / a reference image / information given
   in the instruction.
   **Merely "existing" is not enough — it has to pin down a specific value.**
   See "An anchor pins down a value, not a category" below.
   This is currently the single biggest source of rejections (four out of ten
   decks sent back).
4. **Is the answer unique?** "Make this slide look better", "arrange the labels
   so they do not overlap" — the solution is a region, not a point, and it
   cannot be scored.
5. **What makes it difficult?** Classify three independent facts for every
   degradation: where the evidence lives (`reach`), what has to be reasoned
   out (`reasoning`), and how demanding the actual GUI work is (`interaction`).
   Difficulty comes from reasoning and interaction, not scope or length — see
   "Difficulty has two axes" below. Estimate GUI steps too, but they size the
   job and apportion reward. Reference magnitudes: adding one card ~30 steps,
   redrawing a set of callouts ~60, building a chart ~90, rebuilding a whole
   slide ~150.
6. **Is this yet another "delete it and put it back"?** See "Do not only ever
   set one kind of question" below.
7. **Are the disclosure levels all bunched together?** See "Actually use anchor
   distance" below.

Three common traps:

- **When the original is itself untidy, do not set a "restore the tidiness"
  task** — it contradicts the ground truth.
- **A value that happens to be the software's default is worth little**
  (rotation 0, the default shadow) — a script that clears things to the default
  guesses it right.
- **Do not test precise values the eye cannot reach** (the exact boundary of a
  picture crop); the score becomes noise.

---

## An anchor pins down a value, not a category

**Four of the first ten decks got as far as the last gate before being sent
back, all with the same defect.** All of them had filled in `anchor`, none of
them made it up, all of them sounded plausible — but **working from that anchor
gives you a range, not a determinate answer**. When you score item by item, a
range cannot be scored.

Four real rejections:

| deck | the anchor it wrote | what the probe measured |
|---|---|---|
| d5 | "the fourteen other intact titles in the deck" | pins down the family/weight/colour, **not the point size** — title point sizes were never consistent, so it only narrows to a 28–32pt range |
| d7 | "other slides have the same row of options" | the options **differ in content** on every slide; same kind is not same value |
| d9 | "the surviving elements on the slide" | that note on slide 4 has **no twin** anywhere in the deck; there is nowhere to look it up |
| d2 | "just look at the image" | the instruction never said **what the bubbles on the right say or how many there are**, and the render cannot be read for it |

**The one-line criterion:**

> Hand the anchor to somebody who has never seen the original: is there
> **only one thing** they could produce, or are there **several that all make
> sense**? The latter means it is not pinned down.

Three traps that keep recurring:

- **"There are sibling elements on the slide" ≠ the same values.** Siblings pin
  down the *category* (they are all titles, they are all cards). For any
  property that already varies between siblings (point size, width, the actual
  words), a sibling anchor does not hold it.
- **"Just look at the reference image" ≠ readable from the image.** Small type,
  occlusion, similar colours — something unreadable in the render is something
  you have not given even after giving the image. **Estimate whether that value
  can really be made out at the export DPI.**
- **The one-of-a-kind thing has no backup.** For content that is unique in the
  deck, an on-slide anchor cannot exist by construction; either give a render of
  the original slide or write the value into the instruction.

There are three remedies, near to far (prefer the far ones when they remain
determinate — anchor distance can deepen the reasoning):

1. **Write the value into the instruction** — the safest, and the one that
   lowers difficulty most. Suited to key values that make the task unsolvable
   without them.
2. **Add a reference image of that slide** (masking the damaged region where
   necessary) — this keeps the reasoning component.
3. **Change the degradation** — if there is no comfortable way to disclose it,
   it may be a thing that should not have been tested at all.

**Note: a degradation whose main anchor is `deck_anchor` can still additionally
ask for a reference image.** Disclosure is stackable: pin most of it down on the
slide, and let the reference image supply the one unique part. That is not
self-contradiction, it is using anchor distance precisely — just spell out in
`disclosure_detail` which part relies on which.

---

## Do not only ever set one kind of question

Last round, 10 decks produced 10 tasks, and **all 10 names began with
`restore-`**. Every one of them was "delete a few things, have the agent put
them back". Individually each one passes; together they are a homogeneous batch
of data — the agent practises the same one thing over and over and learns
nothing else.

**Delete-and-restore is the easiest shape to think of, not the only shape.**
None of the following has ever been proposed once, though the catalogue has
listed them all along:

| shape never once proposed | what the deck has to have for it |
|---|---|
| **restore slide order** | an intrinsic order: contents, sections, progressive builds, a timeline |
| **master-level repair** | several slides sharing one layout, with the fault in the layout — the agent has to decide between "fix the layout once" and "fix each slide" |
| **replicate from nothing** | a self-contained run of 2–3 slides that can be rebuilt from blank against a reference image |
| **pull back into style** | the deck has a clear visual system and a few slides have drifted from it |
| **restore relationships** | connectors, pairings, topology — what is broken is "what connects to what", not "what is missing" |
| **redo animation** | a genuine step-by-step build logic (see the keyframe section above) |

**Before you start, ask once: does this deck have the structure to support one
of the above?** If it does, propose that one by preference — a homogeneous
delete-task can be got out of any deck, while these cannot, and running into one
makes it scarce. Only if it does not do you fall back to delete-and-restore, and
then it is **chosen** rather than defaulted into.

By the same token, do not name every task `restore-*` — the name reflects the
shape in your head.

---

## Difficulty has two axes, not one

**`difficulty` is computed from `reasoning` and `interaction`, and the checker
recomputes it.** `reach` and `est_steps` remain required, but neither sets the
band: reach locates the evidence and steps size the work.

This distinction matters. Rebuilding an editable chart, a connector topology
or an animation sequence can be hard while staying on one slide. Conversely,
replacing the same explicit footer on every slide is deck-wide and long, but
neither mentally nor operationally hard.

### Evidence reach: where the answer lives

| `reach` | where the evidence lives |
| --- | --- |
| `on_slide` | on the damaged slide: surviving siblings, labels, data or geometry pin down the answer |
| `cross_slide` | on a different, nameable slide: a table carries the chart values, or an intact copy carries the structure |
| `deck_wide` | spread across the deck: several sections together expose a convention, sequence or exception |

Reach is batch telemetry and a design lever. It does **not** imply a band by
itself: looking elsewhere may be a direct lookup, while difficult operation
may begin after the answer is already visible on the damaged slide.

### Reasoning: what has to be worked out

| `reasoning` | test |
| --- | --- |
| `direct` | the target state can be read directly from one explicit source; the solver still has to locate or transcribe it |
| `relational` | the solver must combine, align or reconcile two or more cues before the target state is determined |
| `inductive` | the solver must infer a rule or sequence from examples and distinguish it from a legitimate exception |

Every degradation names its `inference`, even when reasoning is `direct`:
state the lookup or determination the solver cannot skip. Do not upgrade a
lookup to relational merely because its source is on another slide.

### Interaction: how demanding the GUI work is

| `interaction` | test |
| --- | --- |
| `basic` | edit, move, resize or format existing ordinary objects with independent constraints |
| `compound` | create/rebuild several related objects or coordinate grouping, alignment, ordering or a standard native object |
| `expert` | operate a specialised editor or reconstruct coupled structure: chart series/axes/labels, animation/keyframes/triggers, connector topology, nested groups/z-order, advanced crop/mask, SmartArt, equations or similarly constrained native content |

Every degradation names `interaction_evidence`: the concrete editor, object
structure and coupled constraints involved. "Many objects", "many steps" and
"visually complex" are size or impressions, not evidence of expert work.

### The computed band

| condition | task difficulty |
| --- | --- |
| all degradations are `direct` + `basic` | easy |
| any `relational`, or any `compound` | medium |
| any `inductive`, any `expert`, or the task combines `relational` + `compound` | hard |

The task uses the strongest reasoning and interaction present anywhere in its
degradations. A hard task therefore has an auditable basis; it is not a model's
adjective.

**Inductive tasks must name a `distractor`** — something that looks like it
falls under the inferred rule but is a legitimate exception and must not be
changed. This requirement belongs to induction, not to every hard task: a
single-slide expert chart rebuild does not need a fake distractor merely to
justify its band.

### Search for hard candidates before settling

Before choosing a task, explicitly inspect both routes to genuine hard work:

1. **single-slide expert interaction** — editable charts, animation builds,
   connector graphs, nested grouping/layering, advanced crop/mask, complex
   tables/infographics, equations and other constrained native objects;
2. **reasoning-heavy work** — relationships that must be reconciled, a visual
   or narrative rule induced from examples, or an apparent anomaly that is in
   fact a legitimate exception.

Prefer the hardest candidate that remains unique, feasible and scoreable. If
the deck supports neither route, produce a good medium/easy task or a reasoned
no. Never invent complexity to hit a batch quota.

**Long is still not hard.** Repeating a basic edit thirty times increases
`est_steps`; it does not change `reasoning` or `interaction`.

---

## Actually use anchor distance

Last round's 50 degradations, by disclosure method:

```
deck_anchor 32  ·  reference_image 10  ·  describe 6  ·  reference_image_masked 2
```

**64% landed in the least effortful disclosure band**, and almost nobody used
the middle ones. The result was reasoning kept shallow while work was stacked
up **by quantity**. Anchor distance is one useful way to create relational or
inductive reasoning, but it is not the only source of difficulty.

`deck_anchor` is handy because it is cheap and reliable (the answer is on
another slide, no materials needed), but it also means **the task never gets
past "copy it from over there"**. Pull the anchor one band further away and the
same damage becomes reasoning:

- **`reference_image_masked` is the most underrated band.** Mask the damaged
  region, expose only its surroundings, and what the agent gets is
  "circumstantial evidence" rather than "the answer key". It applies more widely
  than you would think — wherever you are about to hand over a full render, ask
  first: with the target region masked out, do the surrounding alignment,
  whitespace and same-row elements still let the answer be deduced? If they do,
  it should be the masked band.
- **`describe` is not the same as "give nothing".** Spell the constraints out in
  the instruction ("green outline, no fill, boxing those two lines") and the
  geometry is determined by the slide's own content — the answer is still unique.

**Self-check: list this task's degradations by disclosure band; if they are all
in the same band, you are not using this knob.** A healthy task usually spans
two or three bands: several copied from on-slide anchors, one deduced from a
masked image, one from description alone.

---

## How to write the instruction (the easiest part to get wrong)

The instruction is part of the task, and you write the text yourself, **in
English**.

**State the target state only, never the steps.** This is the fatal mistake —
the moment the steps are written in, the agent can follow them without looking
at the slide and the task has been wasted:

| ❌ no | ✅ yes |
|---|---|
| "Move the second picture 200pt to the left" | "The images on this slide are out of order" |
| "Select the title and change its fill to #A92D55" | "Some elements no longer match the deck's colour scheme" |
| "Do the following in order: 1. insert a chart 2. choose a bar chart 3. …" | "The chart on slide 4 is missing — rebuild it from the data below" |

The rules:
- **No precise values**, unless the value is itself information being supplied
  to the agent (the data of the chart to be rebuilt)
- **Do not enumerate every element to be fixed**, unless the task genuinely is
  "these specific ones"
- You may say **which slides** have problems — that is not giving the game away,
  and it makes the scoring scope clear
- Use **the voice of a real working situation** ("You're preparing this deck
  for…"), not "Task: please perform the following operations"

When a task carries several unrelated degradations, write the instruction as a
real scenario:
"This file got messed up as it was passed around and several things are broken —
on slide X…, on slide Y…, and the headers across the deck are inconsistent too.
Fix them all."
One paragraph accounting for every affected place, while each place still says
only **what state it is in**, never **how to fix it**.

---

## The materials list

Spell out what the agent gets besides the broken file:

| material | when to give it |
|---|---|
| **original renders** of certain slides | the target is unique in the deck and can only be restored from the image |
| **masked or cropped renders** | see below |
| **image assets** (png/jpg) | the task requires inserting a picture again |
| **data** (inline in the instruction or a csv file) | the task requires rebuilding a chart/table |
| **source documents** (a report PDF, an email) | the task requires updating content from external information |
| **the build keyframe sequence** | the task requires redoing an animation — see below |

**Giving the complete original render of a slide is the same as giving that
slide's answer.** So be precise — give only the slides restoration requires, do
not hand over the whole deck out of convenience.

**A masked/cropped render** is a useful middle band: mask the part that was
broken and expose only the surrounding context, or crop out just one region of
the slide. What the agent gets is then "circumstantial evidence" rather than
"the answer key", which makes **inference from context** possible, at a
difficulty between "there is an anchor on the slide" and "there is no anchor at
all". When you use it, spell out: **what is masked, what is exposed, and why it
is cut that way**.

### The anchor for animation: a keyframe sequence, not a screen recording

**Animation tasks can be proposed now.** They could not before, because the only
anchor anyone could think of was a screen recording of the slideshow, and the
corpus pipeline cannot get one. That slot is now filled by the **build keyframe
sequence**, which is more useful than a recording:

An animated slide is decomposed into N click steps and each step is rendered as
an image — frame 0 is before the click, frame k is the picture after click k. A
manifest accompanies the frames, stating which shapes each step fired, whether
they entered or exited, and whether they are this click's trigger or fired
alongside it. Additionally:

- **Motion paths** are drawn as dashed trajectories overlaid on the frame (with
  a start circle and an end arrowhead)
- **Emphasis effects** get a pair of "baseline frame + peak frame" (they change
  a property and change it back, so the sequence alone shows no trace)
- **Slide transitions** get a type name and a duration, no image — a render
  cannot express one anyway
- **Interactive triggers** are drawn as a "click this shape → these shapes fire"
  relationship diagram

**Why not a recording.** Two reasons, neither of them a technical limitation.
First, an agent that gets an mp4 in the VM has to open a player and drag the
scrubber, and every viewing costs it operation steps; a set of annotated stills
it takes in at a glance. Second, what the scoring judges is a **discrete
sequence** anyway — which object appears at which click, with what class of
effect — and keyframes are determinate and diffable, while a recording actually
blurs that criterion. Easing curves and the feel of the motion are not scored,
so they do not need to be given.

**So stop vetoing animation tasks for "there is no recording".** The question to
ask is whether this slide's build sequence is itself worth practising (is there
real step-by-step logic, or has one marker simply been given a default fade-in).
One or two default entrance effects is still flipping a switch and is not worth
a task — that is **the degradation being too small**, not a missing material.

---

## How to set the size (and what hard used to mean)

**Calibrate against this frame of reference first; do not go by feel.**

### Historical large hard benchmarks

**The baseline**: rebuilding a roughly three-slide presentation from nothing,
with complex objects such as charts in it.

Here is a harder set for reference — we previously built four benchmark tasks on
which a frontier model, given 300 steps, scored 0.0–0.25:

- a 21-slide quarterly board deck: read an email plus a PDF, update all the Q1
  data to Q2 across 9 slides, and rebuild 4 diagrams at the same time (split a
  pie, add a node with a headshot to an org chart, add a bar to a departmental
  bar chart, add a row to a Gantt chart)
- a 26-slide pitch deck: the 3-D pyramid/ribbons all flattened, all shadows
  gone, the infographic knocked apart, and all the Lorem Ipsum throughout to be
  replaced with the real content from two PDFs
- a 19-slide physics teaching deck: 6 slides needing diagrams, equation objects
  and animation restored
- a 22-slide physics teaching deck: 8 slides needing equations, geometric
  diagrams, animation and slide transitions restored

Those last two handed their animation over as a screen recording, which is
**not** what we do now — see the animation section above. They are here for the
size of the job, not as a model for how to hand one over.

**These four are at the harder end of hard**, and notice *why*. They combine
relational/inductive reasoning with expert interaction: Q2 numbers must be
reconciled from documents, charts and diagrams use specialised structures, and
equations and builds must be reconstructed from the lesson logic. Their large
size is a consequence, not the definition of hard.

### The three size bands (by the agent's GUI step count)

**These bands size the job; they no longer set `difficulty`.** They are recorded
as `size_band` and they matter for two things: `est_steps` apportions the
reward, and a task whose declared steps disagree with its own parts is
rejected. A 90-step task that requires deck-wide induction is `hard` and small;
a 90-step single-slide animation rebuild is also `hard` and small; a 250-step
task of putting scattered pictures back can be `medium` and long.

| band | steps | what it looks like |
|---|---|---|
| **small** | **≤ 100 steps** | one or two repairs. e.g. a row of cards lost a few, completed from the ones beside them |
| **medium** | **100–300 steps** | three to five repairs, or rebuilding one or two complete objects (chart/diagram/block) |
| **large** | **300+ steps** | multiple slides, multiple kinds of object, several stacked; the four benchmark tasks above are all in this band and still are not finished |

**Note this calibration is a band higher than intuition.** One degradation is
usually 40–90 steps, so:

- a task containing only 1 degradation is almost certainly small
- to reach medium usually takes **3–5** degradations
- to reach large usually takes **6 or more**, or several jobs at the "rebuild a
  whole slide" level

**Do not reach for size when you want difficulty.** Adding a sixth
put-it-back repair makes the task longer, flakier and more likely to time out;
choosing a real relational judgement or expert native-object operation makes it
harder at similar length. Padding is what thirty decks did.

### A measured bias: declared step counts run systematically high

The solvability probe **actually does the task** inside the bundle and counts
the steps. Across 12 tasks measured so far, **every single one came in under the
declared value**, without exception:

```
declared 375 → measured 300 (−20%)    declared 390 → measured 345 (−12%)
declared 310 → measured 255 (−18%)    an earlier batch of 9 averaged −12%, worst −25%
```

A bias this consistent in one direction means **it is not individual proposals
estimating badly, it is this whole scale running high**. The consequence is not
ugly numbers: reward apportionment and runtime expectations are biased, and a
task declared at 320 steps may in fact fit comfortably inside a 300-step run.
It does not change the difficulty band, which comes from reasoning and
interaction.

So when estimating steps:

- **count real GUI operations** — select, open the pane, change the value,
  confirm — not "this job feels big"
- when you land near a band boundary (90–110, 280–320), **take the lower one**;
  do not inflate to reach the next band
- **discount degradations that reuse the same operations** — drawing the same
  box a second time is much faster than the first, and the 40–90 steps each
  above is the magnitude for the **first** time

### Two levels

- **Each degradation** gets `reach`, `reasoning`, `interaction`,
  `interaction_evidence` and its own step estimate.
- **The task's difficulty** is computed from the strongest reasoning and
  interaction across them. One inductive or expert degradation makes the task
  hard; relational reasoning plus compound interaction also makes it hard.
  Its **size** is still the sum of the steps.

**Do not stuff in rubbish to reach a step count.** Every degradation has to
stand up on its own (pass the self-check); padding with small changes makes the
agent miss a few and turns the score random, which harms the training signal
rather than helping. Under the old rule padding was the only way to reach a
higher band, which is exactly why the rule is gone.

---

## Output format

Output JSON only. **Keep it terse**, a sentence or two per field, except for
`instruction`.

```json
{
  "deck_read": "two or three sentences: what this deck is, what carries the information on each slide, where there is exploitable repetition or design",
  "tasks": [
    {
      "name": "short task name, e.g. rebuild-results-section",
      "capability": ["chart rebuild", "layout rearrangement"],
      "slides": [3, 5],
      "degradations": [
        {
          "id": "d1",
          "scope": "slide | multi-slide | deck",
          "slides": [3],
          "what_breaks": "plain English: what is broken and what state it ends up in (not written as operation steps)",
          "agent_will_do": "what the agent has to do inside the PPT software, one sentence",
          "why_good": "why this one is worth practising",
          "anchor": "how he is supposed to know what it should look like",
          "disclosure": "deck_anchor | reference_image | reference_image_masked | reference_keyframes | describe",
          "disclosure_detail": "what the anchor is / what is masked and what is exposed / what external information has to be given",
          "reach": "on_slide | cross_slide | deck_wide",
          "inference": "the step of reasoning the solver cannot skip: what he has to work out before he can know what correct looks like",
          "reasoning": "direct | relational | inductive",
          "interaction": "basic | compound | expert",
          "interaction_evidence": "the concrete GUI editor/object structure and coupled constraints that justify the interaction level; never use step/object count alone",
          "est_steps": 70,
          "note": "known weaknesses or things the implementer should watch for (may be empty)"
        }
      ],
      "assets": [
        {"kind": "reference_keyframes", "slides": [7],
         "note": "frame-by-frame images of that slide's 8-step build sequence + the effect manifest; if it contains motion paths/emphasis, say that overlaid trajectories or peak frames are needed"},
        {"kind": "reference_image", "slides": [3], "masked": false,
         "note": "why this slide needs it; if masked=true, say what is masked/cropped"},
        {"kind": "data", "note": "which data has to be provided, roughly what it contains"}
      ],
      "instruction": "the English instruction text, covering every degradation in this task. Target state only, no steps",
      "distractor": "a legitimate exception that looks like it falls under the inferred rule and must not be touched (REQUIRED when any degradation is inductive, otherwise may be empty)",
      "difficulty": "easy | medium | hard (derived from reasoning + interaction, and the checker recomputes it)",
      "est_steps": 150,
      "note": "task-level remarks (may be empty)"
    }
  ],
  "no_task_reason": "if tasks is empty, say why this deck is unsuitable",
  "rejected": [
    {"what": "an idea considered and rejected", "why": "one-line reason"}
  ]
}
```

- **Propose only 1 task by default**, packing every degradation on this deck
  worth doing into it; `capability` is an array, and one task spanning several
  capabilities is normal and good.
- In the rare case of a 2nd task, the two `capability` sets must not overlap
  entirely.
- `est_steps` is the sum of the degradations' step counts. It sets `size_band`
  and reward proportions, never `difficulty`.
