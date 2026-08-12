---
name: ppt-task-solvability
description: Decide whether a degraded PPT task can actually be solved — is the required end state determinate from what the solver is given, or does something have to be guessed. Produces evidence, not a fixed file.
---

# Can this task actually be done?

What `reconcile` judges is **consistency**: are the instruction and the file
talking about the same thing? It never once tries to do the task, so it cannot
catch four classes of problem:

| class | the enum value it produces | what it is |
|---|---|---|
| unsolvable | `undetermined` | every promise was kept, but the information is not enough to determine a unique answer |
| given away | `leaked` | the answer leaks in a way a static check cannot see |
| ambiguous | `ambiguous` | there are several end states that all make sense |
| overdetermined | `overdetermined` | too much was supplied and the task degenerates into copying |

Your job is to find these four.

**This step has to give the same answer twice.** The same bundle was once
probed five times and came back `solvable`, `solvable`, `undetermined`,
`leaked`, `leaked` — not because the runs found different things, but because
each one invented its own bar for how much slack is too much and its own rule
for which finding was the headline. Everything below exists to take those two
decisions away from you. **Walk the passes in order, record what each one
finds, and read the verdict off the table.** Where a rule applies, the rule
decides — not your sense of how serious it feels.

---

## You may only look at what the solver can look at

`bundle/` is the whole of what a solver gets, and the whole of what you may
open:

| given to you | explicitly forbidden |
|---|---|
| `bundle/input.pptx` — the broken file | `source.pptx` — the original |
| `bundle/instruction.md` — verbatim, all the solver is told | `delta.json` — every change |
| `bundle/assets/` — every file shipped with the task | `recipe.json` — how it was broken |
| the asset list in your prompt (file + why) | `proposal.json` — the original intent |
| | `digest.json`, `renders/`, `compare/` |

**This is not a matter of discipline, it is the precondition for this step
meaning anything at all.** Reading the delta is reading the answer key, after
which "it can be done" carries no information.

**The pipeline scans your log.** Read any of the files on the forbidden list and
this step is marked failed outright, without your conclusion being looked at.

Two consequences worth stating, because rules that ignored them used to send
runs looking for things they could not see:

- **Anything outside `bundle/` cannot leak.** A solver never sees `digest.json`
  or the deck directory. If you believe a pipeline file is a problem, that is a
  `rework` note, never a `leaks` entry.
- **You cannot see a degradation's `disclosure`.** So never reason about what
  the proposal *meant* to disclose. An asset that is in `bundle/assets/` was
  shipped on purpose; the only question you may ask about it is the one in
  Pass 3.

---

## Do not actually fix it

Do not write `input.pptx`, and do not write anything other than your output
file.

**What you are producing is an answer key, not a fixed file.** Anything you
cannot write evidence for is underdetermined, and saying so is the most
valuable output of this step.

---

# The procedure

Five passes, in this order. **The order is load-bearing**: Pass 1 is what the
solver can see in PowerPoint or WPS, Pass 3 is what only unzipping reveals, and
a leak is exactly the difference between them. If you unzip first you cannot
tell them apart any more — which is how the same residue got written up as
"evidence that pins the end state" by one run and "leak" by the next.

## Pass 1 — evidence, from what the application shows

Do this before you unzip anything. The tool for it is

```
python -m pptxgym.evaluation.inventory <deck-dir>/bundle/input.pptx
```

plus the instruction and the files in `bundle/assets/`.

Two things about that command, both learned by a run that failed for them:

- **Do not use `pptxgym.office.tools shapes`**, and do not `ls` the deck directory. The
  first reads `digest.json`, which is built from `source.pptx`; the second is a
  read of the deck root. Both are outside `bundle/`, and both void the run.
- Inventory prints a *package* view: it carries paragraph run splits and part
  names as well as shapes. Read the shape level here — text, `bbox`, fill,
  table cells, picture presence, the animation block. Anything below that level
  is Pass 3's, and taking it now is the mistake the pass order exists to
  prevent.

The line you are working to is **could a solver see this without leaving
PowerPoint or WPS?** It is not a judgement call — it is a list:

| the application shows it | only the package shows it |
|---|---|
| text on a slide, and the notes | where one run of text ends and the next begins |
| a picture, a table's cells, a chart's data | `endParaRPr`, `rPr` on an empty run |
| position, size, fill, outline, font colour | relationship ids, a missing `vmlDrawing` |
| the animation pane's build order | part names, `docProps/`, `[Content_Types].xml` |
| a shape's name in the selection pane | anything that needs `unzip` to read |

The right column is Pass 3's business. Do not let it into Pass 1 — a run that
counted run-boundary residue as evidence called the deck `solvable`, and the
run that met the same residue in Pass 3 called it `leaked`.

For **each degradation**, write the end state you believe is required, then walk
this closed list and record which items hit. Cite the hit — a slide number, a
filename, a quoted line. **Write all six keys into `checks`**, an empty string
for the ones that did not hit: an omitted key and a miss read identically, and
"I looked and found nothing" is the more useful of the two. The pipeline
rejects a degradation whose `checks` is missing one.

| # | evidence item |
|---|---|
| **E1** | **twin** — another slide in `input.pptx` carries the same element, or a build step of it, that can be copied or read off |
| **E2** | **asset** — a file in `bundle/assets/` carries the content |
| **E3** | **instruction** — the instruction states the content or the value outright |
| **E4** | **reference render** — an asset image shows the end state, masked or not |
| **E5** | **what survived** — something still on the damaged slide pins it: a ghost frame, a caption, a surviving sibling, a placeholder, the layout, the notes, the animation order |
| **E6** | **derivation** — the value follows from surviving content in one step you can write down (a column total, a naming convention the deck keeps everywhere, a term the instruction defines) |

E6 is the only one that may lean on knowledge the bundle does not contain, and
it costs you a sentence: **write the inference and name the alternative you
rejected.** If you cannot write it in one sentence a reader would accept, E6
did not hit.

Every part of the end state that no item pins is a **gap**. Take each gap to
Pass 2.

## Pass 2 — every gap through the two-solver test

This is the pass that used to be a feeling. Runs have called the very same
finding "residual", "non-blocking", "sub-gradeable detail", "only the object
type is loose" and "mechanism only" — five severities, invented five times, and
the deck's verdict swung on which word got picked. There are exactly two
reasons a gap does not count, and nothing else qualifies.

For each gap, name the end states a careful solver could produce. Then:

- **T1 — the bundle says either is acceptable.** The deck's own convention makes
  the choices identical, or the difference does not show on screen at all.
  Quote what says so.
- **T2 — pinned, to a precision.** Evidence from Pass 1 fixes the value and only
  the reading of it is loose: measuring off a render to ±1 px, matching a twin's
  box to ±0.05 in. Name the evidence and state the slop.

A gap that is T1 or T2 is a **tolerance**. Record it in `tolerance`, with the
rule and the citation, and it does not touch determinacy.

**Everything else is a real gap.** The sharpener, when you are tempted to wave
one through: *this deck was made by breaking a real one, so exactly one end
state is the original.* A difference the original had an opinion about, which
the bundle does not tell you, is a gap however small it looks.

**Position and size need an anchor, not a region.** This is where runs have
disagreed most, so it has its own rule: geometry is a tolerance only when the
bundle gives something to measure *from* — a twin's box, a surviving sibling's
edge, a ghost frame, a render, an explicit coordinate, or an instruction phrase
that names a reference object ("aligned with the caption above it"). A bare
region — "above the legend", "somewhere in the empty left half" — is **not** an
anchor and **not** a tolerance, however narrow the band: two solvers using it
produce different files and nothing in the bundle decides between them. "The
vertical position is free over 2.2 inches" is a gap, and the fix is an anchor,
which is a `rework` note. Geometry is scored binary at 0.01 in, so "roughly
there" was never going to pass.

### A gap only counts if it is in something that is scored

The grader compares a closed list of properties. Judging determinacy against
anything outside it refuses a task over a difference nobody measures, and that
is not hypothetical: a deck was refused because its rebuilt table's **column
widths** were not pinned — while the table check compares row and column
*counts* and *cell text* and has never once looked at a column width. That
refusal was the mirror image of the defect it exists to catch.

What is scored, and nothing else:

| about a shape | what is compared |
|---|---|
| identity | which shape it is, its preset geometry, its members if it is a group |
| position | its centre |
| size | its extent |
| text | the text, and run properties where the degradation touched them |
| fill, line, effects | colour and style, resolved through the theme |
| picture | the image bytes, exactly |
| crop | the crop rectangle |
| table | row and column **counts**, and **cell text** |
| chart | series and category values |
| diagram / SmartArt | its nodes and their topology |
| connector | what it joins |
| equation | native equation symbols and mathematical structure |
| animation | target object, build order, effect, trigger, duration, motion path and repeat/auto-reverse semantics |

Anything not on that list — column widths, row heights, z-order, margins,
line spacing, font sizes outside a run the degradation
touched — **is not a gap**. If the only thing you cannot pin is one of those,
the degradation is `determinate`, and saying otherwise costs a good task.

Then classify the degradation, once:

| the degradation's state | write |
|---|---|
| no real gaps | `determinate: true`, `undetermined: ""`, `rivals: []` |
| real gaps, and you **can** name the competing end states | `determinate: false`, `rivals: ["…", "…"]` |
| real gaps, and you **cannot** even enumerate them | `determinate: false`, `rivals: []`, and `undetermined` says which part and why |

`undetermined` must be empty whenever `determinate` is true. If you find
yourself writing a caveat there on a degradation you called determinate, you
have skipped Pass 2: that caveat is either a tolerance or a gap, and it belongs
in one of the two fields that exist for them.

## Pass 3 — now unzip: what only the package reveals

Unzip `bundle/input.pptx` and read the XML. The solver can do this too, so
anything here is genuinely available — which is the point.

**You may not revise Pass 1 with what you find here.** Its evidence list is
closed. What you are doing now is measuring one thing: does the package hand
over an answer the application does not?

A finding is a **leak** when **both** hold:

1. **It is reachable only by opening the package** — not visible on a slide, not
   in the notes, not in a file in `bundle/assets/`; and
2. **It is load-bearing** — remove it, and something you wrote in Pass 1 or
   Pass 2 changes: a gap closes, a rival is eliminated, an end state is decided.

Both, or it is not a leak. Findings that are true but fail one of the two go in
`residue`, which is a note for a reader and moves no verdict.

Fixed answers to the three that come up every run, so they stop being decided
afresh:

- **A file in `bundle/assets/` is never a leak.** It was shipped on purpose and
  the solver is meant to have it. If it hands over a whole degradation's answer,
  that is Pass 5's `overdetermined` question. If it is undeclared, restores
  nothing, or duplicates another asset, that is a `rework` note against
  `materialise` — a tidiness defect, not a leak.
- **A reference image is not a leak**, even a generous one, when the instruction
  points the solver at it. That is disclosure working as designed. It is a leak
  only if the instruction says it is masked or partial and it is not.
- **Knowing *which* slides were touched is not a leak.** Stripped relationships,
  a missing `vmlDrawing`, an empty rels file: these say a slide was rewritten,
  and the instruction names the damaged slides anyway. Only content is a leak.

## Pass 4 — steps, per degradation

Give **each** degradation a step count, using the magnitudes in
`ppt-task-proposal`: adding one card ~30, redrawing a set of callouts ~60,
building a chart ~90, rebuilding a whole slide ~150. Write it in the
degradation's `est_steps_measured` — the scoring stage reads that field and
weights the reward by it, and a deck without it falls back to parsing your
prose.

`est_steps_measured` at the top level is **the sum of them**, and the pipeline
adds them up: a total that is not the sum is rejected.

Then compare it to the declared total. When they differ by **more than 25%**,
add a `rework` entry with `"stage": "proposed"` saying so — the pipeline does
the division and refuses a report that is outside the band without one. Inside
the band, say nothing.

## Pass 5 — the verdict, from the table

First line that matches wins. Nothing else decides the verdict — not which
finding feels biggest, not how the deck reads overall. **The pipeline walks
this table over the findings in your own file and refuses the report if the
two disagree**, so the verdict is not somewhere you have discretion left: by
the time you reach here it has already been decided by what you wrote down.

| # | condition | verdict |
|---|---|---|
| 1 | `leaks` is non-empty | `leaked` |
| 2 | any degradation has `determinate: false` and `rivals: []` | `undetermined` |
| 3 | any degradation has a non-empty `rivals` | `ambiguous` |
| 4 | any degradation is `overdetermined: true` | `overdetermined` |
| 5 | otherwise | `solvable` |

`leaked` comes first because closing a leak can turn a determinate degradation
indeterminate — the leak has to go before the other questions can even be asked
again. `undetermined` comes before `ambiguous` because a gap you cannot
enumerate is the worse defect of the two.

A degradation is **`overdetermined: true`** only when E2/E3/E4 alone give the
whole end state *and* producing it needs no reconstruction — paste the file,
retype the given line. If the solver still has to choose placement, formatting,
or which of several things goes where, it is not overdetermined.

`undetermined` is not a way of saying you ran out of time. It means at least one
degradation's end state is not pinned. If you could not finish a pass, say so in
`verdict_reason` — and still walk the table with what you have.

---

## Output: `solvability.json`

`verdict` is one of `solvable`, `undetermined`, `leaked`, `ambiguous`,
`overdetermined`. The example below is a whole file, not a fragment: it is a
valid report by every rule in this skill, and the gate accepts it as it stands.
Note that its two degradations disagree about determinacy and its verdict is
neither of theirs — line 1 of the table matched first.

```json
{
  "verdict": "leaked",
  "verdict_reason": "table line 1: `leaks` is non-empty, and the diagram data hands d3 its wording",
  "degradations": [
    {
      "id": "d1",
      "slides": [4],
      "end_state": "slide 4 must show the world production chart again, in the same position as before",
      "checks": {"E1": "", "E2": "assets/p04-Picture-3.emf is the image itself",
                 "E3": "", "E4": "assets/reference-p04-masked.png fixes the frame",
                 "E5": "", "E6": ""},
      "evidence": "assets/p04-Picture-3.emf is the original image; the masked reference gives its frame position",
      "determinate": true,
      "rivals": [],
      "undetermined": "",
      "tolerance": [
        {"what": "frame position to about 0.04 in", "rule": "T2",
         "why": "read off the masked reference at 25 px/in"}
      ],
      "est_steps_measured": 60,
      "overdetermined": false
    },
    {
      "id": "d3",
      "slides": [15],
      "end_state": "slide 15's SmartArt must carry the five statute names again, in the order the surrounding text uses",
      "checks": {"E1": "", "E2": "", "E3": "", "E4": "", "E5": "",
                 "E6": ""},
      "evidence": "",
      "determinate": false,
      "rivals": [],
      "undetermined": "the wording of all five nodes. Nothing on slide 15 or anywhere else in the deck carries it, and no asset ships it, so the end states cannot even be enumerated",
      "tolerance": [],
      "est_steps_measured": 180,
      "overdetermined": false
    }
  ],
  "leaks": [
    {"what": "unzipping input.pptx reveals the five deleted statute names",
     "where": "ppt/diagrams/data5.xml",
     "load_bearing": "d3 has no E1-E6 hit for the wording; this supplies it"}
  ],
  "residue": [
    {"what": "slide 7's rels lost rId1 while slide 5's kept it", "why_not_a_leak": "says a slide was rewritten, carries no content"}
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

- **every degradation carries all nine keys** — `id`, `end_state`, `checks`,
  `determinate`, `rivals`, `undetermined`, `tolerance`, `est_steps_measured`,
  `overdetermined` — and the pipeline rejects one that is missing any. Write
  the empty value (`""`, `[]`, `false`) rather than leaving the key out: an
  absent `rivals` and an empty one are the same thing to the Pass 5 table, so a
  degradation that found rivals and forgot the field reads as one that found
  none. `end_state` and `est_steps_measured` are owed determinate or not —
  write the best end state you can even when you are reporting that it is not
  reachable
- every `leaks` entry carries `what`, `where` and `load_bearing`, and `where`
  **starts with the location** — `ppt/diagrams/data5.xml` — because that first
  token is what the pipeline reads when it checks the entry is inside
  `bundle/`. Every `residue` entry carries `what` and `why_not_a_leak`
- when `verdict` is not `solvable`, **`rework` is mandatory**, and every entry
  must name one of `proposed` / `recipe` / `materialise` — the pipeline uses it
  to decide what to re-run:

| what you found | where it goes back to |
|---|---|
| a leak the degrade step left behind | `recipe` |
| a leak inside a shipped asset, or a missing reference image | `materialise` |
| a gap only a clearer instruction can close | `proposed` |
| rivals that only the instruction can choose between | `proposed` |
| overdetermined — the asset is the answer | `materialise`, or `proposed` if the instruction is the giveaway |

- **"it cannot be done" is a valid answer.** Insisting it can is putting an
  unsolvable sample into the dataset

---

## Before you write the file

Seven checks. Each one caught a real report that shipped — **and the pipeline
now runs every one of them.** They are arithmetic over what you wrote, not a
second opinion about the deck: a report that fails one is handed back without
its verdict being read, and the deck buys another probe. So they are cheap to
pass and expensive to skip.

1. Does every degradation carry all nine keys, with `checks` holding all of
   E1–E6? And does every degradation you called determinate have at least one
   E-item cited and a non-empty `evidence`? An uncited determinate degradation
   is a guess wearing a verdict.
2. Is `undetermined` empty, and `rivals` empty, on every degradation you called
   determinate? And where you wrote `determinate: false` with no rivals, does
   `undetermined` actually say which part is not pinned and why?
3. Does every `tolerance` entry name `T1` or `T2` and cite what makes it one?
4. Does every `leaks` entry state its `load_bearing` reason, and does its
   `where` start with a location inside `bundle/` — not a render, not a
   pipeline file, not `bundle/assets/`?
5. Do the per-degradation step counts add up to the top-level one, and is the
   25% band against the declared total either satisfied or reported as a
   `proposed` rework note?
6. When the verdict is not `solvable`, is there a `rework` list, and does every
   entry name `proposed`, `recipe` or `materialise` and say something in
   `what`?
7. **Run the Pass 5 table against your own findings and compare it to the
   verdict you wrote.** If they disagree, the findings win: either the verdict
   is wrong, or a finding is misfiled. A report with a non-empty `leaks` and a
   verdict of `undetermined` has shipped, and it was neither answer — this is
   the check that would have caught it.

What the pipeline does **not** check is every judgement these rest on: whether
a leak is really load-bearing, whether a gap is really a T1/T2 tolerance,
whether an anchor is really an anchor, whether 60 steps is really 60. Those are
yours, made with the bundle open, and no checker can re-make them from your
JSON. It only holds you to what follows from them.

---

## The principle in one sentence

**You do not fix the task, you report its state.**

"It cannot be done" triggers the repair loop, and the least effortful way to
repair is **to make the task simpler**. So your report must be precise about
**which one thing is missing**, not "it is too hard" — the former can be
supplied, the latter will only be used to whittle the task down.
