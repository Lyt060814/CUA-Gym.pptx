# Rollout trajectory review — tasks 1100001 and 1100004, model `muse-spark-1.1`

Source: OSWorld Monitor `http://13.223.43.53:8080`, JSON at `/api/task/tasks/<id>`.
Both trajectories read step by step in full (1100001: 500 steps; 1100004: 205 steps).
Task sources read first: `/home/yitongli/XLANG/osworld2.0-rollout/evaluation_examples/task_class/task_1100001.py`,
`task_1100004.py`, and the plans/inventories under `evaluation_examples/task_assets/task_11000{01,04}/tests/assets/`.

**Both runs scored 0.0.** The monitor exposes only the scalar (`status.result`); there is no
per-component breakdown, no `runtime_evidence`, and `/api/task/tasks/<id>/analysis` returns
`{"content": null}`. Where I give component-level numbers below they come from running the
task file's own `evaluate_candidate()` locally against inventories I reconstructed from the
scripts the model typed — that is a reconstruction, clearly labelled, not a measurement of the
file the grader actually saw.

---

## 0. What the two rubrics ask for

Shared evaluator (`pptx.degradation-restoration.v1`): mean progress over N components,
**multiplied by zero unless all five hard gates pass** (`untouched_slides_intact`,
`no_full_page_paste`, `no_stray_additions`, `deck_shape_preserved`, `native_objects_present`).

Shapes are matched across the rebuild by `_content_key`: image sha256 prefix → text sha256 →
otherwise `("geo", kind, round(w/914400,1), round(h/914400,1))`. For anything without an image
or text this means **the restored shape's width and height must land in the same 0.1-inch
bucket as the original, i.e. within ±0.05 in ≈ 1.3 mm, or the component scores 0 outright**.

| | 1100001 | 1100004 |
|---|---|---|
| components | 23 | 28 (only 25 distinct — `p14/p16/p18-set_font-1` each appear **twice**, so the three font components carry 6/28 = 21% of the weight) |
| affected pages | 3,4,5,6,7,8,11,13,16,17,18 | 7,9,10,12,14,16,18 |
| floors | all 0.0 | six p07 components have `floor: 0.4` |
| tolerances | mostly default 0.35 in / 25 % | hand-tuned per shape: `tol_in` 0.08–0.317, `size_frac` 0.069–0.25 |

Verified offline calibration (running the evaluator on the shipped inventories):
init → 0.0, gt → 1.0 for both tasks, all gates green. The `floor: 0.4` entries in 1100004 are
real: on the untouched file `p07-scatter-{3,5,6,7,8,9}` each score raw 0.4 (size already right,
position wrong), normalised to 0.0. The consequence is that those six are **pure position
checks with a binary outcome**, and for `p07-scatter-9` ("Minus 2") the position tolerance is
`0.08 in` — 2 mm of centre error on a shape that must be dragged. That is not a
computer-use-achievable tolerance by hand; it is only reachable by copying an exact coordinate
from another slide, which is in fact what the model did (see §2).

---

## 1. Task 1100001 — "Olivier's ASF 2019 talk"

### 1.1 What the model did, step by step (intent level)

**Steps 1–18 — orientation, GUI-first, clumsy.** It tries to open Files by clicking the
Activities corner and typing "Files" (steps 2, 10, 12); this fails repeatedly. It does find the
`pptx_task_1100001_materials` folder and opens `reference-p04-masked.png` and `reference-p13.png`
in the image viewer (steps 14–18). At step 6 it already notes the gap that matters:
*"Inferring which reference files correspond to slides 4 and 13 and noting missing references
for slides 6 and 8."* It had four material files: the logo JPG, the slide-4 EMF, and two
reference PNGs — nothing for slides 6 or 8.

**Steps 19–35 — real WPS UI operation, and it works.** Insert ▸ Picture ▸ file dialog ▸ selects
`p04-Picture-3.emf` (steps 21–24), then drags and corner-resizes the inserted figure against the
masked reference (steps 25–31). Step 33 accidentally replaces the figure with the reference PNG;
step 34 recognises it (*"Troubleshooting an accidental image replacement where a masked reference
PNG overwrote the production figure"*) and deletes it. The screenshot at step 46 shows slide 4
with the World-production area chart correctly placed and the status bar reading `Slide 4 / 19`.
**So this model can select a shape, open a WPS dialog, insert a file, drag and resize, and undo
its own mistake.**

**Steps 36–62 — pivot to the terminal.** It opens a terminal, `pip3 install python-pptx`
(step 43), and from then on treats the deck as a file. It measures the masked reference with
PIL (steps 41, 96–98), converts pixel fractions to EMU (steps 149–151), enumerates every shape
via python-pptx, and at **step 79** derives the logo's canonical geometry from the slides that
still have it: `correct = (49213,218251,2952750,1162050)` — which is exactly the ground-truth
box. At **step 82** it hashes the material JPG and every embedded image and matches them, i.e.
it independently reinvented the evaluator's own identity test.

**Step 75 — the run is destroyed here, silently.** Its recovery idiom for a lost window is
"click Activities → type `Terminal` → Enter → immediately type the whole heredoc". At step 75
the Activities click misses; focus stays in WPS's slide pane; the ~11 `press('enter')` calls in
that one action each insert a blank slide. The status bar goes `Slide 6 / 19` at step 74 →
`Slide 16 / 29` at step 77. The same thing happens again later; by step 130 the screenshot
shows `Slide 50 / 61` with dozens of blank slides carrying the old LaSalle Beauvais master logo.
The model *notices the symptom* (step 130: *"Diagnosing slide count inflation from added master
layouts versus content slides"*) but misdiagnoses it as a WPS/master artefact and never connects
it to its own typing. From step 133 on it re-derives a slide-index mapping into the 61-slide
file (task slide 8 = file index 17, task slide 13 = file index 54) and keeps working inside the
corrupted deck.

**Steps 170–202 — a genuinely competent restoration script.** In one pass (step 170) it:
normalises every logo to the canonical box, deletes duplicate logos, adds the logo via
`add_picture` to slides that lack it, and re-places the world-production figure at
`(1849901,1568547,6070209,4283612)`. Step 171 adds an unfilled green-bordered rectangle over the
lower half of slide 6's table screenshot. Step 172 adds a red-bordered rectangle over the
lanthanide row, two thin black vertical rules at 33 %/66 % of the plot group, and three bold
`LREE/MREE/HREE` text boxes. Step 173/177 repositions the slide-13 panels onto a guessed 2×2
grid. Step 187 catches its own regression (the logo-resize heuristic had shrunk the world
figure) and step 200 re-inserts the EMF. This is real, targeted, self-correcting work.

**Step 210 — it stops.** From step 210 to step 500 the model emits `ASK_USER` on **289 of the
remaining 291 steps** (the only exceptions are steps 370 and 478). There is no user in this
harness. 58 % of the entire episode is a no-op loop. The last substantive action is step 209,
at 19:36, after 2 h 17 m of work.

### 1.2 Score against what it deserved

Actual: **0.0**. Reconstructing its final edit set on an *uncorrupted* 19-slide deck and running
the evaluator gives **mean component progress 0.4348** (10/23 components at 1.0: all nine logo
components plus the slide-4 figure) — and **score still 0.0**, because
`untouched_slides_intact` fails on pages 1 and 12. On the deck it actually produced,
`deck_shape_preserved` also fails (61 ≠ 19). So 0.0 is the rubric-correct answer, but it hides
~44 % of genuinely correct restoration.

### 1.3 Task defects

**D1 (worst). "Bring the branding back in line with the rest of the deck" is undecidable, and
guessing wrong is a hard zero.** The instruction never says which pages lost the logo. In the
ground truth 17 of 19 slides carry it; slides 1 (title) and **12** do not — slide 12 is a
full-bleed figure page sandwiched between slides 11 and 13 which *do* carry it, so its bare
corner is indistinguishable from damage. The model raised exactly this at step 80
(*"Resolving whether the title slide is an exception to the new-logo placement rule"*), could
not resolve it, and applied "add the logo to every slide with content". Verified locally: that
rule flags `changed_untouched_slides: [1, 12]` and zeroes the whole task. A consistency
instruction plus a hard "don't touch undamaged slides" gate is a contradiction.

**D2. Slides 6 and 8 ask for shapes whose dimensions the agent was never shown.** No reference
image is supplied for either page (materials are only the logo, the slide-4 EMF, and references
for p04/p13). The agent must redraw a green rectangle whose stored width rounds to 9.1 in and
height to 1.2 in, a red rectangle at 2.8 × 0.2 in, and two zero-width rules 2.2 in tall — from
prose alone. Its slide-6 rectangle came out 9.185 in wide (1.3 % too wide) and scored **0**:
`nothing on slide 6 matches the missing autoshape (geo)`. Four components (p06-delete-3,
p08-delete-1, p08-delete-8, p08-delete-9) are unscoreable in practice. Related: GT records the
two rules as `autoshape` (`p:sp`); a line drawn from WPS's Shapes gallery serialises as `p:cxnSp`
→ `kind` mismatch → 0 regardless of geometry.

**D3. The LREE/MREE/HREE components compare group-child coordinates against slide EMU.** In
`ppt/slides/slide8.xml` the plot group has `chOff (2789,2357) / chExt (2394,1777)`, so
`gt_path 6/1` is recorded as `box [3288, 2568, 492, 231]`. `check_restored_shape` compares that
raw against a candidate box in slide EMU. I simulated adding the three labels as ordinary text
boxes in the visually correct place inside the plot: **all three score 0.0**. Conversely, *any*
text box inserted inside that group passes both the 0.35-in centre test and the size test
trivially, because the tolerances (320 040 EMU and 18 288 EMU) dwarf the group's entire 2 394-unit
child space. The check is a coordinate-frame accident: unachievable by the natural action,
free by the unnatural one. The model did the natural thing at step 172 and got nothing.

**D4. Weight distribution.** 9 of 23 components (39 %) are the same logo restored on seven
pages plus two move/resize pairs — one insight, nine payouts — while the six hand-drawn
annotations on slides 6 and 8, which are the interesting work, are worth 6/23 and are
unscoreable per D2/D3.

**Not a defect:** the material files' bytes are byte-identical to the embedded originals
(`p03-Image-4.jpg` → `fb07c5b79f7c97d5`, `p04-Picture-3.emf` → `d525ab8b12a39836`), so inserting
them reproduces the ground-truth `_content_key` exactly. That part is well built.

**Did it get credit for anything it did not do?** No — it got 0.0.

---

## 2. Task 1100004 — "CAD WALK rehearsal deck"

### 2.1 What the model did, step by step

**Steps 1–13 — straight to the terminal.** `ctrl+alt+t` works first time (step 2), python-pptx
installed at step 5. This run never really tries to edit through the GUI; WPS/Impress are used
only as viewers.

**Steps 15–107 — the strongest analysis in either trajectory.** It dumps shapes, positions and
run fonts for every candidate slide; searches the deck for "Patient Measurement"/"Healthy
Estimate" to find the canonical comparison row (step 25) and notices the caption wording differs
between slides 6 and 13 (step 26); compares the red operator glyphs by fill/line/theme colour
and finally by raw XML (steps 69–71); extracts `ppt/diagrams/data1.xml` and `drawing1.xml` from
the zip and parses `ptLst`/`cxnLst` to understand the SmartArt (steps 74–87, 105, 121–122); and
at **step 103** works out the correct fix for slides 14/16/18 unaided:
*"Clearing explicit font and color on runs to restore master inheritance."* That is exactly what
the evaluator wants and it is not what the instruction says (see D6).

**Steps 108–118 — correct edits, applied to the real file.** `fix.py`/`fix2.py` copy slide 6's
picture geometry onto slide 7's same-named pictures, **swap the two operator positions** (it
inferred that slide 7's Minus sits where slide 6's Equal sits, and vice versa), pull slide 13's
caption boxes onto slide 7, and rebuild slide 12's comparison row by extracting image blobs and
coordinates from a reference slide. `fix_font.py` (step 115) strips `solidFill`/`latin` children
from every run on 14/16/18 and nulls `font.name`/`font.size`. `fix10.py` (steps 117–118) adds the
"Y = MX+ B" model box and three red arrows to slide 10. The step-111 screenshot shows
`after cleanup 2` — slide 12 really did have exactly title + slide-number at that moment, i.e.
these edits landed on the genuine 20-slide deck.

**Step 125/126 — the first fatal move.** Unable to edit SmartArt from python-pptx and with no
GUI text pane available, it deletes `Content Placeholder 4` (the SmartArt graphic frame) **and
both slide-9 pictures**, and rebuilds the pipeline as three red rounded rectangles plus three
grey "illustration" placeholders. This trips `native_objects_present` (gt smartart 1, candidate 0)
and `no_stray_additions` (page 9: gt 5, candidate 8). From this point the score is pinned to 0
no matter what else it does.

**Steps 132–146 — the second fatal move.** It opens the deck in LibreOffice to check visually,
then at step 144 types a python heredoc while Impress has focus. Step 145's own note:
*"Diagnosing unexpected slide creation by identifying terminal focus causing keystrokes to be
captured by LibreOffice Impress."* It quits, is offered a save dialog for a 45-slide document
(step 146: *"Evaluating the corrupted 45-slide save dialog and deciding to choose Don't Save"*),
clicks through two dialogs — and at step 152 finds the on-disk file is now 45 slides with real
content only in 32–39. Every edit from steps 108–118 is gone.

The same focus bug had already fired once, earlier: WPS's status bar reads `Slide 1 / 20` through
step 48, `Slide 43 / 50` at step 51 and `Slide 50 / 57` at step 52 — 30 blank slides created by
the Enter keys in the step-49/50 heredocs. That instance was harmless only because WPS was later
killed without saving.

**Steps 153–187 — competent forensics, no luck.** It searches `~/.local/share/Trash`,
`/tmp`, `~/.config/libreoffice`, then finds WPS's autosave directory
`~/.local/share/Kingsoft/office6/data/backup/*.dps`, restores the best candidate (39 slides,
content only in 32–39), and deletes the 31 leading blanks.

**Steps 188–199 — fabrication.** It rebuilds a 20-slide deck **from `Presentation()`, the stock
python-pptx 4:3 template**, with a hand-typed list of 20 titles reconstructed from memory,
grey/black rectangles standing in for the pressure panels, and images chosen by file size
(`all_imgs_sorted[:3]`). Step 205: `DONE`.

### 2.2 Score against what it deserved

Actual: **0.0**, and for the final artefact that is correct — the deck it left behind is
fabricated. But the state it had reached at step ~126, reconstructed from the scripts it typed
and run through the task's own evaluator, gives **mean component progress 0.6321** with these
components at 1.0: `p07-resize-2/-4`, `p07-scatter-2/-3/-4/-6/-7/-8`, `p12-delete-5/-6/-7`, and
all six `p14/p16/p18-set_font-1`; `p09-smartart_drop_nodes` at 0.7. Score still 0.0, gated by
`native_objects_present` and `no_stray_additions` on page 9. (Reconstruction, not a measurement;
the p07 picture components assume slide 6 uses the same `Picture 4/5/6` names as slide 7, which
the model's script relied on.)

So: ~63 % of the component work done, ~21 % of it the hardest and least obvious part, and 0.0.

### 2.3 Task defects

**D5 (worst). Slide 9's instruction is factually wrong, and its rubric contradicts itself.**
The instruction says *"the alignment pipeline is down to its first stage — the illustrations
that sat above the other two went with them"*. The ground truth has exactly **two** pictures on
slide 9 and **both are present in the degraded file** — nothing was deleted; the only degradation
is `smartart_drop_nodes`. The model hunted for the phantom missing illustration for dozens of
steps (step 68 *"Checking the desktop for alternate files and backups to locate the missing
stage illustrations"*; step 121 *"tiny PNG sizes to rule out foot illustrations"*), concluded the
two survivors were not the right ones, and **deleted them** at step 125. On top of that,
`check_diagram_nodes` contains an explicit partial-credit path — *"allow a hand-built
equivalent, but say so"*, worth 0.7 — for precisely the rebuild the instruction invites, while
`gate_native_objects` then multiplies the whole task by zero for doing it. The instruction never
says the object is a SmartArt or that it must stay one.

**D6. The font components can only be won by an action the instruction does not describe.**
Ground truth records **no explicit run properties** on slides 14/16/18 (the text inherits from
the master); the degraded file adds `latin typeface="Georgia"` and `srgbClr 1F4E79`.
`check_text_style` compares candidate run dicts to GT run dicts on `font/sz/b/i/u/color`, so any
candidate that *sets* a typeface and colour fails. Verified locally: setting the deck's typeface
and colour explicitly on all three slides → **text_style 0.0**; deleting the explicit properties
→ **1.0** (overall 0.2143). The instruction says *"it doesn't use the typeface or the text colour
every other slide in this deck uses"*, which reads as "set them correctly" — the losing move. The
`_runs_of` docstring even asserts the opposite of what is true here: *"a solver restyling text
writes explicit properties, and that is what the original carries too."* The model got this
right only because it read the XML (step 103), not because the task told it. And these three
checks are duplicated in `plan.json`, so the trap carries 21 % of the weight.

**D7. Slide 12's "same measurement images" is ambiguous and the wrong reading is unrecoverable.**
GT slide 12 uses the images from slides 6/7 (`02240090`, `398b5bec`, `4ee5436d`); slide 13
("Example – Hallux Valgus") uses three *different* images. The only textual cue is the caption
set: slide 12's captions are "Patient Measurement / Healthy **Estimate** / Abnormality", which
match slide **13**, not slide 6 ("Healthy **Measurement**"). The model spotted the discrepancy
(step 26) and deliberately chose slide 13 as the model (step 131: *"Resolving which image set to
reuse by matching example slides rather than subject-specific ones"*). Because the evaluator
matches on image sha256, that costs `p12-delete-2/-3/-4` — three components — while the three
captions it copied from the same slide score 1.0. Same evidence, opposite verdicts.

**D8. Hand-tuned tolerances make six components binary position tests at millimetre precision.**
`p07-scatter-9` has `tol_in: 0.08` (2 mm) and `floor: 0.4`; `p07-scatter-2/-4` have
`size_frac` 0.09–0.14. There is no GUI action that hits 2 mm reliably; the only way through is
to read an exact coordinate off another slide and write it back — a scripting task, not a
computer-use task. The model happened to do exactly that and passed, which proves the point
rather than refuting it.

**D9. Connectors are unrestorable by construction.** `p10-delete-12/-13/-17/-18` are `cxnSp`
connectors; their `_content_key` is `("geo","connector", w, h)` to 0.1 in. Any arrow the agent
inserts from a shape gallery is a `p:sp` autoshape → kind mismatch → 0, before geometry is even
considered. Four of 28 components are unwinnable without hand-writing `p:cxnSp` XML.

**Did it get credit for anything it did not do?** No. Worth flagging for future runs though:
`check_diagram_nodes` does a substring test over the concatenation of all node texts, so a
candidate that keeps the SmartArt and pastes all six phrases into a *single* node scores 1.0.
That is a cheap hack the gate does not catch.

---

## 3. Cross-cutting

**Capability, from what I saw in these two runs.** It plans at the level of a task list and
holds it across hours (both trajectories return repeatedly to the same slide-by-slide agenda).
It finds and uses the Desktop reference material without being told where to click. It can drive
WPS's UI for insert/drag/resize and recover from a wrong click (1100001 steps 21–34). Its real
strength is *falling back to a shell*: both runs install python-pptx within ~40 steps and from
then on reason about the deck as OOXML — hashing image blobs to identify the logo (1100001
step 82), deriving canonical geometry from undamaged siblings (1100001 step 79; 1100004
steps 108–111), parsing `ppt/diagrams/data1.xml` (1100004 steps 74–87), and diagnosing an
inherited-vs-explicit run-property problem from raw `rPr` XML (1100004 step 103). It also
audits its own work and catches regressions (1100001 step 187; 1100004 steps 110, 138).

**Limitation, same evidence base.** It types blind. Its window-switch idiom is
"click a coordinate → immediately type a 20-line heredoc", with no check that focus moved. When
the click misses and a presentation app has focus, every `Enter` inserts a slide. This fired at
1100001 step 75 (19→29 slides), again before step 130 (→61), and at 1100004 steps 49–50 (20→50)
and step 144 (→45, then saved over the file). It is the direct cause of both zeros, and it
repeats the same idiom after diagnosing the symptom. Second: no terminal-output discipline —
1100001 steps 47–59 are thirteen consecutive retries of the same heredoc because the output was
not visible. Third, and specific to 1100001: after step 209 it emits `ASK_USER` 289 times, i.e.
it has no "no user available, keep going or stop" fallback and burns 58 % of the budget.

**Both zeros were deserved by the letter of the rubric and both hide most of the work**: 0.435
of component progress in 1100001, 0.632 in 1100004 (reconstructed). For RL, a rubric whose
output collapses to exactly 0.0 in both runs — for two different gate violations, neither of
which is the thing the instruction emphasised — carries no gradient. The gates are worth
keeping as *anti-hack* filters but should not multiply the component score to zero; report them
alongside the mean instead.

**Cheapest fixes, in order of value**
1. 1100004: drop `gate_native_objects` (or scope it to objects the instruction names) — it
   zeroes 0.63 of real progress for an action the checker itself grants 0.7 for. (D5)
2. 1100004: fix the slide-9 instruction — it describes damage that was never applied. (D5)
3. 1100001: either name the pages whose logo was removed, or drop `gate_untouched_slides` for
   deck-wide consistency instructions. Slides 1 and 12 are unguessable exceptions. (D1)
4. Both: normalise group-child coordinates before comparing boxes, or exclude group children
   from `restored_shape`. (D3)
5. 1100004: rewrite the 14/16/18 instruction to say the text should *inherit* the deck's
   styling, and de-duplicate the three `set_font` component ids. (D6)
6. Both: `_content_key`'s geo bucket is too tight for freehand shapes. Use a relative size
   tolerance for matching, not a 0.1-in rounding bucket. (D2, D9)
7. 1100004: say plainly which slide is the visual model for slide 12, since the caption text
   points at the wrong one. (D7)
