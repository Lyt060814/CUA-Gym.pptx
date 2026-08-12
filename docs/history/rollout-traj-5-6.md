# Rollout trajectory review — tasks 1100005 and 1100006, model `muse-spark-1.1`

Source: OSWorld Monitor `http://13.223.43.53:8080`, `/api/task/tasks/<id>` (full step list +
per-step screenshots via `/task/tasks/<id>/screenshot/<file>.png`). Task definitions read from
`/home/yitongli/XLANG/osworld2.0-rollout/evaluation_examples/task_class/task_11000{05,06}.py`
and `evaluation_examples/task_assets/task_11000{05,06}/`.

Both trajectories were read step by step in full (112 and 146 steps). Every claim below points at a
step number or at a file I ran.

## Method note — the replay, and why you can trust the numbers

The monitor exposes only the scalar `result`; it does not expose the evaluator's per-component
output, gate detail, or `runtime_evidence`. So I reconstructed each candidate deck offline: I took
`assets/init.pptx`, re-applied the agent's *exact* python-pptx scripts in the order it ran them, and
scored the result with the task file's own `inventory_pptx()` + `evaluate_candidate()` against the
shipped `gt_inventory.json` / `plan.json`.

- **1100005 replay score = 0.1588. Recorded rollout result = 0.1588.** Exact to four decimals.
- The 1100006 replay is verified byte-for-byte against the agent's own on-VM verification dumps:
  step 144's shape listing (`9 PICTURE (13) 2286000 5029200 4114800 164592`, `10 TEXT_BOX (17)
  1097280 4206240 914400 164592 CPT1B`) and step 145's counts (`Slide11 label counts 28`,
  `Slide13 label counts 32`, `Slide8 colors sample ['000000','FE6060','64A8EF','A72D39',...]`)
  reproduce identically in my replay.

Replays live in `/tmp/replay5` and `/tmp/replay6` (throwaway; nothing in any repo was modified).

---

# A. What this model can and cannot do

Extrapolation warning: this is two trajectories from one model on two tasks of the same family.
Everything below is what I saw; I flag where I am generalising.

## It plans, at a coarse level, and it re-plans

Both runs open with a real plan stated in the first `response` (1100006 step 1: *"Reconstructing a
damaged supplementary table and figure panel from a reference image and assets. Locating the
desktop materials folder and inspecting the reference assets."*). It then decomposes the instruction
into per-slide sub-goals and works them in order. On 1100006 it re-planned twice on slide 3 after
seeing its own output overlapped the box plots (steps 115, 128, 130) — three successive layout
attempts, each strictly better positioned than the last. That is planning, not reaction.

## It finds and uses the Desktop reference material — competently

- 1100006 step 3: `ls ~/Desktop/pptx_task_1100006_materials/` within three actions.
- Step 4: opens all seven reference PNGs in `eog` in one command; step 6 re-opens `p06--2.png`
  alone to read the antibody table off the image, and transcribes 7 data rows × 7 columns from it
  (step 85 / 107). Cross-checking the transcription against `reference-p06.png`: it is essentially
  correct.
- 1100005 step 5: `cat ~/Desktop/pptx_task_1100005_materials/p06-table.csv`; step 80 parses that CSV
  with `csv.DictReader` and builds the table from it. Step 74/76/79/100/105 crop `reference-p07.png`
  with PIL, iterating the crop box five times while eyeballing the result in `eog`.
- 1100005 step 46-48: it works out that the two plots missing from slide 8 are the *same images*
  living on slide 18, extracts `shp.image.blob` and re-inserts them — an exact image-hash match.
  That is a genuinely good piece of reasoning from an instruction that only said "the same two plots
  still exist somewhere else in this deck".

## It cannot, or will not, operate WPS's UI

This is the sharpest limitation and it is identical in both runs.

- 1100006 step 1's screenshot shows WPS Presentation open, maximised, with the deck loaded. The
  model never once uses it. At step 19 it runs `xdg-open ~/Desktop/pptx_task_1100006.pptx &`,
  gets LibreOffice Impress, and at step 21 reasons *"Confirming LibreOffice Impress is acceptable
  for editing the pptx file"*. By step 104's screenshot the WPS dock icon is gone entirely — WPS was
  closed somewhere between step 13 (icon still present) and step 104.
- 1100005 is more explicit: at step 87 it runs `pkill -f wps; sleep 1; ps aux | grep wps` after
  Ctrl+Q and three clicks on the window's X (steps 83-86) failed to close it. It later relaunches
  WPS (step 91) but only to *look* at slides.
- In 1100005 it does drive WPS's slide-thumbnail navigation for ~15 steps (steps 30-45; step 42's
  screenshot shows WPS 2019 with slide 7 selected). That is the whole of its WPS usage: scrolling
  and clicking thumbnails to read.
- Across 258 steps it never selects a shape, never opens a format pane, never inserts a table
  through the UI, never uses the colour picker, never uses the eyedropper, never completes a dialog.
  Every edit in both tasks is a `python-pptx` script typed into a GNOME Terminal.

So on this evidence: it can read a deck in a GUI and it can drive a file manager / image viewer /
terminal, but its whole editing capability is scripted. Whether it *can't* use WPS or simply
prefers not to, I cannot tell from two runs — but the 1100005 window-close sequence (steps 83-86,
four failed attempts at a title-bar X) suggests genuine weakness at WPS window/widget targeting.

## Its terminal I/O is a severe bottleneck

- 1100006 steps 85-101 are one long failure to write a script to disk. `cat > /tmp/fix.py << 'PY'`
  is entered one clipboard round-trip per line (~200 X11 clipboard handoffs in a single action);
  step 86 diagnoses *"stray carriage-return characters and heredoc terminator handling"*; steps
  89-99 detour through VS Code and a GTK save dialog; step 101 runs `python3 -m py_compile
  /tmp/fix.py` and gets an IndentationError. Roughly 17 of 146 steps produce nothing.
- It recovers correctly: at step 102 it abandons the script file and switches to small inline
  `python3 - <<'PY'` snippets, one slide at a time (steps 102, 107, 108, 109, 110), each ending in
  `prs.save()`. Every one of those succeeded. **That is the clearest example of recovery from a
  repeated mistake I saw** — it changed strategy after ~4 failed attempts rather than looping.
- The counter-example: terminal scrollback truncation. 1100005 steps 17, 18, 19, 20, 23, 26, 27, 28
  and 1100006 steps 39-54 are the same query re-run with a narrower filter because the output
  scrolled off. It reaches for `> /tmp/out.txt; cat` only intermittently (1100005 step 63) and then
  forgets again (steps 64, 65, 66, 67, 68). ~20 steps per run burnt on this.

## Where it stopped, and why

Neither run stopped from failure — both ran to a self-declared `DONE` well under the 500-step cap
(146 and 112 steps) after a verification pass it wrote itself.

- 1100006 step 145 runs a checker: `Slide6 has_table True`, `Slide11 membranes [..., 'No. 6
  membrane']`, `Slide8 colors sample ['000000','FE6060','64A8EF','A72D39', ...]`, and stops at 146
  with *"Confirming the final save path and stopping after all fixes."*
- 1100005 step 111 does the same and prints `Mangled titles remaining 0`, `Slide10 distinct
  positions 6`, `Slide19 CAEN boxes total: 3`; step 112 is `DONE`.

Where it *stopped making progress* is different from where it stopped: it stopped making
**scoreable** progress the moment a sub-goal required matching geometry it had no way to read. On
1100005 slide 19 it burns steps 62-81 trying to reconstruct the HV distribution diagram, and every
coordinate it uses is a guess narrated as such — step 81: *"# For simplicity, draw lines from each
new box left edge to central area x~4300000"*, *"# Let's estimate positions"*. All 19 slide-19
components score 0. It never considered that the surviving group's children could give it the grid.

---

# B. Task soundness

## 1100005 — 34 components, 9 pages, scored 0.1588 (5.4/34). Deserved: about that.

Verified per component (replay output in full):

| what it did | components | earned |
|---|---|---|
| slide 6 table rebuilt from `p06-table.csv` | p06-delete-1 | **0.6** (position ✓, size ✗) |
| slide 10 labels moved onto the equipment | 6 × p10-move | **3 of 6 at 1.0** |
| slide 19 diagram guessed | 19 comps | 1.0 total (two 0.4s + one 0.6, all geometry luck) |
| slide 7 diagram | p07-delete-3 | 0 |
| slide 8 plots re-inserted (exact image hash) | 2 comps | 0.4 |
| 5 restyled titles | 5 × text_style | **0** |

The score is honest for the slide-19 work (it guessed, it got guess-level credit). The rubric is
**not** honest about three things:

**Defect 5-A (worst) — the five title components are unwinnable, and the instruction cannot tell
you the answer.** GT requires the title run to carry `sz` and `b=1` and to carry **no explicit
`latin` typeface and no explicit colour** (`gt_inventory` p8/p11/p12/p16 runs are
`{"sz":32.0,"b":true}` — no `font`, no `color`). The degraded file has
`{"sz":24,"b":false,"font":"Calibri","color":"#595959"}`. There is no WPS or LibreOffice UI action
that produces "no explicit colour"; the colour picker's black writes `#000000`. The model did the
right diagnostic work (steps 22-29: it dumped every title's size/bold/name/colour across all 22
slides and identified exactly the five mangled ones) and at step 80 set `bold=True`, `size=Pt(28)`,
`name=None`, `color=RGBColor(0,0,0)`. Setting `name=None` was *correct* and clever. It scored 0 on
all five, solely because of the black. That is the rubric and the instruction disagreeing.
Worse: the instruction says the titles "no longer match the way titles are set on the rest of the
deck", but the rest of the deck is not consistent — GT sizes are 28 on slides 2,3,4,6,7,12,13 and
32 on 5,8,9,10,11,14,16. p11 wants 32 while p12 wants 28. Nothing visible to the agent determines
which.

**Defect 5-B — slide 7 is the mirror image of 1100006's slide 6 defect.** The instruction says only
that the page "no longer has the dark-box optical layout diagram… reference-p07.png shows what that
page is supposed to look like". Nothing says it must be a native group. GT path `3` is a 20-shape
group whose content key is the *text* `"LASER ND Filters Mirrors Fiber Holders MCP-PMT"`. The model
cropped the reference render and inserted it as a picture (steps 74-79, 103, 106) — a defensible
reading of the instruction — and the checker reports `nothing on slide 7 matches the missing group
(txt)`. Note `gate_native_objects` does *not* catch this (a group is not a chart/table/SmartArt), so
the task neither forbids the picture nor rewards it.

**Defect 5-C — near-miss geometry, and credit for nothing.** `p08-delete-0` scores 0 on *both*
halves: the model re-inserted the exact original image (hash match) but its centre is 0.37 in from
GT against a 0.35 in tolerance, and its height is 3 000 000 EMU against a 1 000 961 EMU tolerance on
4 003 844. Meanwhile `p19-delete-1_55` ("5.0 M, 0.5 W") is awarded 0.4 for *size* because the
model's generic `Emu(1000000) × Emu(300000)` default textbox happened to land within 25 % of GT —
that is 0.4 of a component for a default constructor argument, not for placing a resistor label.
Same for `1_49`. Three of 34 components are decided by textbox-default luck.

**Credit concentration:** 19 of 34 components (56 %) are slide 19, the hardest sub-goal — an
unaided vector rebuild of a HV distribution diagram from a raster render. The model earned 1.0 of
those 19. So here the *hard* sub-goal gates the majority of the score, which is the opposite skew
from 1100006 but equally unhelpful as a training signal.

## 1100006 — 105 components, scored 0.0. Deserved 0.5333.

### The headline: the recorded 0.0 is a false zero

Replaying the agent's edits (steps 102, 107, 108, 109, 110, 128, 130) onto `init.pptx` and scoring
with the task's own evaluator gives:

```
score 0.5333  mean 0.5333
gates {"untouched_slides_intact": true, "no_full_page_paste": true,
       "no_stray_additions": true, "deck_shape_preserved": true,
       "native_objects_present": true}
p08 outline    1.0 × 21   0.0 × 7
p08 text_style 1.0 × 21   0.0 × 7
p11 restored   1.0 ×  9   0.0 × 8
p13 restored   1.0 ×  5   0.0 × 6
p03/p06        0.0 × 21
```

All five gates pass. The candidate is provably the file that was on disk: its slide-3 shape
geometry, slide-11/13 heading text and label counts, and slide-8 colour list all match, digit for
digit, the agent's own verification dumps in the step 144 and step 145 screenshots. Nothing wrote
the file after step 130 — steps 144's Ctrl+S/Ctrl+Q went to a GNOME Terminal (no Impress window is
on screen in that screenshot), and WPS had been dead since before step 104.

I cannot say *why* the harness recorded 0.0, because the monitor returns only the scalar. The two
mechanisms that fit are (a) `_persist_open_deck` / `get_vm_file` throwing, which lands in
`_failed()` → `score: 0.0`, or (b) the rollout server scoring a different file. It is *not* the
WPS force-save clobbering the deck: WPS was already dead (dock icon absent at step 104 and 146), and
the identical situation on 1100005 (WPS killed at step 87) produced a correct, non-zero score.
**This needs the raw evaluator dict, which the monitor does not expose — hand it to whoever is
checking harness plumbing.** As it stands, the best task in the batch was scored zero for work worth
0.53.

### Defect 6-A (worst) — slide 6's instruction demands the one thing that scores zero

The instruction: *"rebuild it there as a **real, editable table (not a pasted picture)**, laid out
and filled in to match the supplied reference image."*

The ground truth for that page is a **picture**:

```
gt_inventory p6 → {"path":"0","kind":"picture","box":[2890998,1033382,5795802,4099469],
                   "image":"d7199c1b91c51e69"}
```

and `sha256(assets/materials/p06--2.png)[:16] == d7199c1b91c51e69` — the supplied material *is* the
original embedded image, byte for byte. The single scored component `p06-delete-0` uses
`_content_key` = `("img","d7199c1b91c51e69")`, so the **only** way to earn it is to insert that PNG
as a picture — exactly what the instruction forbids. An agent that obeys the instruction scores 0;
an agent that disobeys it scores 1. The model obeyed (step 107: `slide6.shapes.add_table(9,7,...)`,
transcribed correctly off `p06--2.png`) and got 0. `gate_native_objects` cannot save it either: GT
page 6 contains zero table objects, so the gate is vacuous there.

Also note the instruction sells this as the headline sub-goal ("the entire table is gone") and the
rubric prices it at **1 component out of 105 — 0.95 % of the score**.

### Defect 6-B — 105 components are 57 tests, and 40 % of the score is six edits

Grouping the 105 components by the `(page, check, content_key, gt-property)` triple that the
checkers actually compare gives **57 distinct tests**. The collapse is on slide 8: all 28 lane-tick
connectors are `[.., 144000, 0]` boxes with no text and no image, so `_content_key` gives
`("geo","connector",0.2,0.0)` for all of them, and all seven "CON" textboxes share
`("txt","a3dbc4b644a9a2c5")`. `check_outline` and `check_text_style` both take the **max over every
shape with that key** — so satisfying one shape satisfies all seven components that reference it.

I tested this. Starting from the untouched `init.pptx` I recoloured **three** of the 28 lane-label
groups (one LAC, one EXE, one EXLA — three text runs and three connector lines, six property sets)
and left the other 25 wrong:

```
score from editing 3 of 28 lane-label groups: 0.4   gates True
components at 1.0: 42
```

**0.40 of a 105-component task for six edits, with 25 of 28 labels still visibly wrong.** That is
the answer to "is the credit really spread": no. It is not that one hard sub-goal gates 90 % — it is
that the *cheapest* sub-goal (recolour existing text, one sentence at the end of the instruction:
"Finally… have lost the group colour coding") carries 56/105 = 53 % of the score and can be
substantially farmed. Meanwhile the two sub-goals the instruction leads with — rebuild the table
(1 component) and rebuild panel C from four blot strips plus twelve labels (20 components) — carry
20 %.

### Defect 6-C — 14 components require an XML absence no UI can express

The GT for the seven "CON" groups on slide 8 carries **no colour at all**: connector
`{"w_emu":28575,"color":null,"none":false}` and run `{"sz":5.0,"b":true,"font":"Arial"}` with no
`color` key. The degraded file sets both to `#000000`, which is *visually identical*. So the
restoration for one quarter of slide 8 is "make the black not-black-but-still-black", and neither
leaving it alone nor picking black in a colour dialog can pass. In the replay these are exactly the
7 + 7 = 14 components that score 0 while the other 42 score 1.0. The model reasoned about this
correctly at step 46 (*"noting missing rgb property for CON default black"*) and still could not act
on it — it wrote `'CON': '000000'` (step 85, step 102), which is the only sane thing to do.

### What was answerable, and what the model got right

Answerability is otherwise fine on 1100006. The slide-11 heading "No. 6 membrane" and the slide-13
"No. 8 membrane" are inferable — the undamaged sibling pages 12 and 14/15 use the identical 5/6/7/8
and 9/10/11/12 layout, and the page's own caption names membrane No. 6 for panel (B). The model
found them (steps 52-53, 69, 83) and placed them at EMU coordinates derived from the sibling panel's
offset (step 108: `off = Emu(7436028) - Emu(3368303)`), landing within 0.13 in of ground truth. Nine
of the 17 slide-11 components and five of the 11 slide-13 components are genuinely earned. The
colour values `FE6060 / 64A8EF / A72D39` it read out of the file itself (steps 44-51) rather than
guessing. This is real, correct, *deserved* work — and the run scored 0.0 for it.

### Did it get credit for anything it didn't do?

On the recorded 0.0, no. On the score it deserved, yes and substantially: of the 42 slide-8
components it would have earned, only 8 correspond to distinct edits; the other 34 are the same
eight facts re-scored. And on 1100005, `p19-delete-1_55` and `p19-delete-1_49` award 0.4 each for a
default textbox size at a wrong position.

---

# Summary table

| | 1100005 | 1100006 |
|---|---|---|
| steps / cap | 112 / 500 | 146 / 500 |
| recorded | 0.1588 | 0.0 |
| replay of the agent's own file | **0.1588** (exact) | **0.5333**, all gates pass |
| deserved | ~0.16 (+5 title comps blocked by a rubric it cannot satisfy) | 0.53 |
| used WPS to edit? | no — `pkill -f wps` at step 87 | no — WPS closed by ~step 20 |
| worst defect | 5 text_style components require absent font+colour attributes and a title size the deck does not determine | instruction demands "a real editable table, not a pasted picture" where the ground truth *is* the pasted picture |

## Recommendations (out of scope for this review, listed for the record)

1. Get the raw evaluator dict for 1100006 out of the rollout server before drawing any conclusion
   about this model on this task. The recorded 0.0 disagrees with the artefact by 0.53.
2. `_content_key` needs a positional discriminator for shapes with no text and no image, otherwise
   N identical shapes collapse into one test scored N times.
3. `check_outline` / `check_text_style` / `check_restored_shape` should match hits to GT shapes
   one-to-one (greedy nearest) rather than each taking a global max.
4. Never author an instruction phrase like "not a pasted picture" without checking `_kind()` of the
   ground-truth shape.
5. Any component whose GT value is the *absence* of an explicit property should be dropped or
   normalised (treat missing colour and `#000000` as equal when the theme resolves to black).
