# Audit — the nine artefacts the ten-deck run emitted

Read-only audit at `1a32068`. This file is the only thing this audit wrote.
What follows is everything the two endpoint numbers (ground truth 1.000, broken
input 0.000) cannot see, ranked by whether it would give a **wrong score to a
real agent**.

*(The working tree was not clean during the audit — `cli.py`, `degrade_exec.py`,
`observe.py`, `pipeline.py` and `tests/test_operators.py` carry other work in
progress. The modules every conclusion below rests on — `inventory.py`,
`comparators.py`, `emit.py`, `assets.py`, `consistency.py` — are unmodified at
`1a32068`, verified by `git diff`.)*

The runtime question is settled: the embedded evaluator in all nine files is
byte-for-byte the pipeline's own. The problems are elsewhere — in what the
instruction promises, in where the weights come from, and in one package that
should not be in the directory at all.

Nine emitted tasks, mapped to their decks:

| task id | deck | components | difficulty |
| --- | --- | ---: | --- |
| `9c4d67ef347c` | deck0002 | 28 | hard |
| `755e402204c5` | deck0003 | 9 | medium |
| `821df4a40fed` | deck0004 | 25 | hard |
| `de4bf5d6c094` | deck0005 | 34 | hard |
| `bca594340f93` | deck0006 | **98** | medium |
| `febe30ab6a49` | deck0007 | **5** | medium |
| `1100013` | deck0008 | 8 | hard |
| `9bbc9703d8c9` | deck0009 | 24 | hard |
| `04a8b177cff7` | deck0010 | 7 | medium |

---

## Tier 1 — these mis-score real work

### 1.1 `task_9c4d67ef347c`: the instruction exempts the animation, the plan charges for it. Ceiling 0.9199.

The instruction's second-to-last sentence:

> The click-build animations on several of the mangled slides went out with the
> deleted shapes; **you do not need to re-create any animation, only the artwork.**

`tests/assets/plan.json` scores three `strip_animation` components anyway:

| id | deg | plan slide (0-based) | prose slide | weight | floor |
| --- | --- | ---: | ---: | ---: | --- |
| `c008` | d1 | 5 | 6 | 0.032534 | `steps 0/8` |
| `c021` | d2 | 6 | 7 | 0.016743 | `steps 0/8` |
| `c027` | d4 | 15 | 16 | 0.030822 | `steps 0/2` |

**Measured, not inferred.** I built the candidate the instruction describes —
`gt_inventory` with the `animation` block of slides 5/6/15 reverted to
`init_inventory` — and scored it with `comparators.score`:

```
perfect artwork, no animation re-created -> 0.919901   (gate None, penalty 0.0)
  c008 strip_animation w 0.032534 score 0.0
  c021 strip_animation w 0.016743 score 0.0
  c027 strip_animation w 0.030822 score 0.0
full ground truth                        -> 1.0
```

An agent that does exactly what it was told cannot exceed **0.9199**. 8.01% of
the score is unreachable by obedience. Re-adding the deleted shapes does not
restore the timing tree — `_cmp_strip_animation` rebuilds `_anim_signature(gt_slide)`
and pays `hit/len(want)`, floor 0.0.

Neither gate catches this: the coherence probe scores `ground_truth` at 1.0
because `source.pptx` still has its animations, and the consistency checker
explicitly declines to judge instruction wording. The conflict lives only
between the prose and the plan, and nothing compares those two.

`9c4d67ef347c` is the only task with `strip_animation` components. Fix is one
line either way: drop the three components, or delete the exculpatory sentence.

### 1.2 Weights come from a step count the pipeline itself measured to be wrong

`plan["weight_source"] == "est_steps"` on every deck: each degradation's weight
is its proposer-declared `est_steps` over the total. The `solvable` stage then
independently measures the same work and writes its own breakdown into
`solvability.json` — and disagrees, per degradation, by up to **8×**. Nothing
reconciles them. Only the *totals* are ever compared, and even then loosely
("agrees with the declared 285 within a band"), which is exactly why this
survives: the per-degradation errors cancel in the sum.

**deck0006 (`bca594340f93`)** — the worst case, and the run's biggest task:

| deg | what it is | declared | weight | measured | reward per measured step |
| --- | --- | ---: | ---: | ---: | ---: |
| d1 | insert one bitmap on slide 6 | 120 | **0.3158** | **~15** | **0.02105** |
| d2 | rebuild panel C: 4 pictures + 16 objects | 90 | **0.2368** | **~140** | **0.00169** |
| d3 | relabel slide 11 | 60 | 0.1579 | ~60 | 0.00263 |
| d4 | relabel slide 13 | 55 | 0.1447 | ~45 | 0.00322 |
| d5 | recolour 21 labels + 28 bars, slide 8 | 55 | 0.1447 | ~50 | 0.00289 |

The cheapest job in the deck pays **12.4× more per step** than the most
expensive one. Pasting one image (~15 steps) earns 0.316; rebuilding twenty
shapes (~140 steps) earns 0.237. For RL data that is the whole ballgame: the
gradient points at the fifteen-step job.

**deck0007 (`febe30ab6a49`)**:

| deg | what it is | declared | weight | measured | per step |
| --- | --- | ---: | ---: | ---: | ---: |
| d1 | insert one picture | 115 | 0.2949 | ~25 | 0.01179 |
| d2 | rebuild 5 styled boxes from a render | 100 | 0.2564 | ~75 | 0.00342 |
| d3 | copy a diagram onto two slides | 130 | **0.3333** | **~30** | 0.01111 |
| d4 | re-insert 2 SmartArt nodes | 45 | 0.1154 | ~35 | 0.00330 |

d1 + d3 = **62.8% of the score for ~55 measured steps**; d2 + d4 = 37.2% for
~110. The largest-weighted job in the task (d3, 0.333) is a copy-paste that the
probe measures at ~30 steps.

Milder but present on the two other decks whose probes broke the estimate down:
deck0010 max/min per-step ratio 2.0 (`d4` 0.1373 for ~15 steps vs `d2` 0.2941
for ~65), deck0004 1.9 (`d3` 0.1806 for ~35 vs `d2` 0.1806 for ~65). Every
distortion runs the same direction — the cheap jobs are over-paid.

`consistency.py`'s own docstring says *"Is the difficulty still right? Step
counts come from a human's model of GUI work. **Nothing in the artefacts
measures it.**"* That is no longer true — `solvability.json` measures it — and
the number it measures is not the number the weights use.

### 1.3 `task_bca594340f93` ships 8 blot strips where 4 exist

`assets/materials/` holds 13 files that resolve to 8 distinct images. Five are
byte-identical duplicates under a second name, left over from an earlier
`materialise` run that `work/deck0006/assets/` never cleared:

```
84a4aea382  p03--4.png  ==  p03--44536-47548-4.png
e58c79814f  p03--5.png  ==  p03--44536-47548-5.png
2819ffc848  p03--7.png  ==  p03--44536-47548-7.png
7aa2dd3632  p03--8.png  ==  p03--44536-47548-8.png
d7199c1b91  p06--2.png  ==  p06--44536-47548-2.png
```

Slide 3's panel (C) is missing exactly **four** blot strips (`c001`–`c004`,
d2, weight 0.2368). The folder offers eight, and the instruction says only "the
strip images are in the assets folder". The deck's own solvability probe wrote
this down and shipped anyway: *"the duplicate pairs could make a solver think
there are eight distinct blot strips instead of four"* — filed as "Bundle
hygiene, non-blocking".

It is not hygiene. An agent that builds eight strips has d2 wrong (0.2368) and
a slide that does not match the reference. It also ships two renders of page 3
(`reference-p03.png` unmasked, plus the superseded `reference-p03-masked.png`)
where the instruction promises one.

The mechanism is structural: `pipeline.bundle()` copies everything in
`deck/assets/` except `manifest.json`, and `emit()` copies every file under
`bundle/assets/` into `assets/materials/` flattened onto `f.name`. Neither
consults `manifest["produced"]`. Nothing anywhere asks "is this a file the
proposal decided to ship?"

*(Related, latent: the flatten is `shutil.copy2(f, .../materials/f.name)` over
`rglob("*")`. Two files in different subdirectories with the same basename
overwrite silently. No bundle in this batch has a subdirectory, so it does not
bite today; the `keyframes` producer, which writes into `build-pNN/`
directories, is where it would fire first.)*

### 1.4 Save-As recovery is dead in exactly the case it exists for

`_stray_candidate` runs

```
find /home/user/Desktop /home/user -maxdepth 2 -name '*.pptx' -type f \
     -newer '{DECK_VM_PATH}' ...
```

`-newer` needs the reference file to exist. `evaluate` calls this when
`not result_path` — i.e. **when the pinned deck is gone**, which is precisely
when `-newer` has nothing to compare against. Verified: `find` errors out and
prints nothing, the shell swallows it (`2>/dev/null`), and the recovery returns
empty.

So an agent that *renames or moves* the deck instead of copying it gets 0.0
even though the machinery to rescue it is right there. Save-As, which leaves
the original in place, still works — I drove that path and it scores correctly.
Fix: reference the materials directory, or a timestamp captured at setup.

---

## Tier 2 — what shipped is not what the pipeline decided to ship

### 2.1 `task_1100013` is a stale build of a deck the pipeline has since rejected

This is the "older deck0008 build at 8" in the run notes, and it should not be
in the ship directory.

`work/deck0008/state.json` now reads `"reconciled": "rejected", "scored":
"rejected"` — it never reached `hardened` or `packaged`. Its current
`plan.json` carries a non-empty `rejected` list:

> `coherence: media_not_pasted fires on 'ground_truth', which is correct work: 2 original media part(s) pasted back`
> `coherence: over-eagerness alone zeroes the score; a scope violation must cost a fraction, never everything`

deck0008 is one of the two parked decks. But `work/emitted/` still holds the
task emitted from a superseded build at 05:34, and nothing distinguishes it
from the eight legitimate ones:

| | shipped `task_1100013` | current `work/deck0008` |
| --- | --- | --- |
| `init.pptx` sha256 | `9267bef9…` | `e6629cf6…` (**differs**) |
| plan components | 8 | 9 (different ids, ops, slides, weights) |
| materials | `cospar_by_month.csv`, `p05-Picture-4.png`, `p11-table.csv` | `cospar_by_month.csv`, `reference-p05.png` |

The other eight are byte-identical to their decks' current `bundle/input.pptx`.

Two consequences beyond the staleness itself:

- **It is the one task with no `INSTRUCTION_MATERIALS` paragraph** — it predates
  commit `e963508`, which added it. Check 4's question ("does it appear when and
  only when materials were uploaded?") answers **8 of 9**. Its instruction tells
  the agent the files are "in the same folder as the deck" and "next to the
  deck"; they are one directory down, in `task_1100013_materials/`. This is the
  exact failure the `INSTRUCTION_MATERIALS` comment block was written to stop.
- **It ships two files deck0008's current proposal forbids by name.**
  `p05-Picture-4.png` has sha256 prefix `c84c64ac04accd4e`, which is exactly the
  `picture.blob` digest `gt_inventory` records for `c001` (weight 0.100) and is
  absent from `init_inventory` — the deleted picture's own bytes. The current
  proposal replaced it with a render on the stated grounds that the render *"is
  a weaker giveaway than the previous asset, which was the deleted picture's own
  bytes"*. `p11-table.csv` is named in a proposal note reading *"DECISION, NOT A
  REQUEST: … p11-table.csv must not be produced or named by the instruction"*.

The shipped package is internally coherent — I re-scored it with today's
comparator and got ground truth 1.0, broken 0.0, no gate — so it will not
mis-score *itself*. It is simply not the task anyone approved.

**Action: delete `task_1100013` from `work/emitted/`.** Re-emitting deck0008
needs a decision first, since its plan is currently rejected.

### 2.2 Three of the nine collide with tasks already in the rollout repo

`osworld2.0-rollout` already contains `task_1100011` (`source: pptxgym/deck0002`),
`task_1100012` (`pptxgym/deck0003`) and `task_1100013` (`pptxgym/deck0008`).
The emitted set re-derives ids from the source checksum
(`pipeline.task_id_for` → `csum[:12]`), so copying it in gives **one overwrite
plus two duplicates**:

| in the rollout repo | source deck | emitted now |
| --- | --- | --- |
| `task_1100011` | deck0002 | `task_9c4d67ef347c` |
| `task_1100012` | deck0003 | `task_755e402204c5` |
| `task_1100013` | deck0008 | `task_1100013` (explicit `--task-id` override) |

deck0002 and deck0003 would exist twice under two ids with two answer keys —
two AMI-hours per rollout and double-counting in any aggregate.

### 2.3 `task_9bbc9703d8c9`: the masked reference does not mask, and the instruction says it does

The instruction promises, verbatim:

> Use the supplied reference image for that page (`assets/reference-p04-masked.png`):
> it is the original slide with each annotation's box marked in place at its
> original position and size **and its text blanked out**, so it shows you the
> slots but not which piece belongs in which.

It is not blanked out. `assets.mask_regions` hatches the shape's *frame* plus
0.06 in; these are non-wrapping text boxes whose glyphs paint outside it. I
looked at the shipped PNG: `IC`…`M` flanking both IC50 boxes, `423)`,
`affinity` twice, `centre`, and `min` twice are all plainly legible — 8 of the
13 slots identified outright.

**Ranked here rather than in Tier 1, deliberately.** d1's thirteen components
are `move` ops scored on position; reading the fragments tells the agent *which*
shape goes *where*, but it still has to perform thirteen moves. The score an
agent gets still matches the file it produced. Two further facts settle the
severity:

- The solvability probe **saw it** and wrote it down: *"The mask is imperfect —
  text overflowing its frame stays legible outside the boxes … a mask-quality
  wart rather than a leak."*
- Its reason is the more interesting one: the hatched rectangles are drawn at
  the ground-truth shapes' **exact sizes**, so the probe verified a unique
  13-of-13 assignment by box geometry alone (every best match within 0.5 px,
  runner-up ≥3 px away). The mapping the instruction says the image withholds is
  recoverable by measuring it, overflow or no overflow.

So the defect is that the instruction makes a false statement about a shipped
asset, and d1 is a measurement exercise rather than the matching exercise it is
sold as. `consistency.py` names this gap honestly — *"Is a masked reference
masked enough? … needs the render looked at"* — and delegates it to the
solvability agent, which looked, and passed it.

The other two masked renders are fine: `reference-p02-masked.png`
(`febe30ab6a49`) hatches a picture frame cleanly with nothing legible outside.

### 2.4 `task_de4bf5d6c094` ships a CSV nobody asked for, against its own disclosure tier

`assets/materials/p06-table.csv` is a verbatim, cell-for-cell copy of the
ground-truth 10×2 "Feature / Parameter" table on slide index 5, which `c002`
scores at **weight 0.2289** via `_facet_table_all` (which also checks
`n_rows`/`n_cols` — the CSV supplies those too). deck0005's degradation d4
declares `disclosure: "describe"`, and the proposal's only `data` asset request
is for slide 19. `assets.manifest.json` records the miss honestly: *"the 'data'
asset for slide 19 was never produced … what came back instead was
p06-table.csv, for slide(s) [6]"*. The byproduct was never removed, `bundle()`
copied it, and the instruction was later written around it.

The agent still builds a table and types twenty cells, so this is not free
score — it converts the deck's second-heaviest component from recall-from-
description to transcription, against the declared tier. Note that removing the
CSV now would make `c002` unsolvable: the instruction gives no prose contents
for that table.

---

## Tier 3 — costs a reader, not an agent

### 3.1 Every partial-score description names the wrong slide, and none carries any explanation

`emit._describe` produces `f"{comp['op']} on slide {comp.get('slide')}"`.
`plan["components"][].slide` is **0-based**. So every entry in every emitted
`DESCRIPTIONS` dict, and every row of every `README.md` scoring table, is one
less than the slide the instruction names:

```
task_febe30ab6a49:  'c001': 'delete on slide 1'      # the instruction says slide 2
                    'c005': 'smartart_drop_nodes on slide 18'
```

`c005`'s own `spec` in the same file says `"slide": 19`. The plan mixes the two
conventions internally: `components[].slide` is 0-based while
`unscoreable[].slide`, `solvability.json` and the material filenames
(`reference-p06.png`) are all 1-based.

The second half is worse. `_describe` is written to append the degradation's
prose — `deg.get("what_the_file_looks_like") or deg.get("what_breaks")` — but
the degradation dicts in `plan.json` only ever carry
`['components', 'est_steps', 'id', 'weight']`. That prose lives in `task.json`,
not `plan.json`. Result: **0 of 238 component descriptions across all nine
tasks carry any explanation.** `DESCRIPTIONS` is a class attribute, so this
lands in every rollout's evaluator breakdown.

### 3.2 Orphan and unrewarded items

- `task_9bbc9703d8c9` ships `p10-table.csv` (314 B) that the instruction never
  mentions; the slide-10 values are pasted inline in the prose instead.
- `task_821df4a40fed` asks for "the typeface or the text colour" on three
  slides; the six matching `set_font` components are in `unscoreable` (the
  ground truth inherits rather than states them). Same shape in
  `task_bca594340f93`: seven `set_font` components on slide 8 are unscoreable
  while the instruction asks for the whole group colour coding. Asked-for,
  unrewarded, harmless — `_scope_survivors` only charges for shapes that vanish.
- Six instructions name the materials folder loosely ("the assets folder", "the
  folder beside the deck", and twice as a literal relative path
  `assets/reference-pNN.png`). The appended `INSTRUCTION_MATERIALS` paragraph
  resolves the folder-*name* cases; the two `assets/…` path forms may still cost
  one failed lookup.
- A table rebuilt as loose text boxes scores 0 on `table_drop_rows` /
  `clear_table_cells` components (`9c4d67ef347c` −0.164, `9bbc9703d8c9` −0.148
  in the `rebuilt_by_hand` probe). This is correct — a table has to be a table —
  but neither instruction says the word "table" is load-bearing.
- `_persist_if_unsaved` reports `save_status: "not needed — the file on disk had
  already moved"` when the file is simply **absent**. Misleading evidence
  string; the score is right.

---

## What passed

### Check 1 — the runtime is the pipeline's. Clean, 9 of 9.

For every emitted file I loaded its embedded `inventory_pptx` and `score` and
compared against the live `pptxgym.inventory` / `pptxgym.comparators` on the
same decks:

- `inventory_pptx` output is **byte-identical** (canonical JSON, sorted keys) on
  `source.pptx`, `input.pptx` and the shipped `assets/init.pptx` — 27 of 27
  comparisons.
- `score(plan, cand, gt, init)` returns an **identical dict** — not just an
  identical total — for ground truth, for the broken input, and for the shipped
  init: 27 of 27.
- `emit.runtime_source()` regenerated from the modules on disk today appears
  verbatim in all nine files, so no file was emitted against a different
  comparator.
- The baked answer key agrees with the embedded runtime: `gt_inventory.json` ==
  embedded inventory of `source.pptx`, `init_inventory.json` == embedded
  inventory of the shipped `init.pptx`, 9 of 9.
- The namespace collision the docstring describes is definitively absent from
  the artefacts, not merely from the pins: run-level styling survives in every
  shipped inventory (163 / 318 / 159 / 278 / 602 / 1218 / 970 / 496 / 274 runs).
- Both modules are stdlib-only by AST inspection, and I re-ran one emitted file
  with `pptx`, `lxml`, `PIL` and `numpy` blocked at `__import__`: identical
  output.

The one flag was `init_inventory.json` not matching `work/deck0008/input.pptx` —
that is §2.1, not a runtime defect.

`INIT_SHA256` and the `MATERIAL_SHA256` frozenset match the files on disk in all
nine (the frozenset correctly collapses deck0006's five duplicate pairs).
`WEIGHTS` and `DESCRIPTIONS` cover exactly the plan's component ids, and the
weights sum to 1.000 ± 1e-5. No component has a floor above 0.

### Check 2 — the two outliers. Neither is degenerate, and the framing inverts.

| task | deck | n | max | min | ratio | degradations |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `bca594340f93` | deck0006 | **98** | 0.31579 | 0.002954 | **106.9** | 5 |
| `febe30ab6a49` | deck0007 | **5** | 0.29487 | 0.115385 | **2.6** | 4 |

For context, the other seven run between 3.3 and 23.2.

**deck0006 at 98 is a real task, and 98 is a count of atoms, not of jobs.** Five
degradations: d1 = 1 component (a deleted bitmap, 0.3158), d2 = 20 deletes on
slide 3, d3 = 17 on slide 11, d4 = 11 on slide 13, d5 = 49 restyles on slide 8
(21 `set_font` + 28 `outline`). Every one of the 49 addresses a distinct shape
at a distinct position — I checked, all 49 specs are unique — so they are not
duplicates, and 0.295% per component is fine-grained rather than noisy. It takes
16 components to reach half the weight, 44 to reach 80%.

The distribution question answers the opposite way round from the framing:
**deck0006 is the one with a dominating component.** `c021` alone is 31.6% of
the score — 107× the smallest, and one shape. The other 97 components share
68.4%. And by §1.2 it is the *cheapest* job in the deck.

**deck0007 at 5 is enough to be a task, and has the healthier distribution.**
Four jobs — restore a deleted picture, rebuild five styled boxes, paste a
diagram onto two slides, re-insert two SmartArt nodes — measured by the
solvability probe at ~185 GUI steps. No component exceeds 0.295; the spread is
2.6:1, the tightest in the batch. Its real defect is §1.2, not its size: score
granularity is coarse (11.5% minimum increment) and d3's 0.333 is a copy-paste.

**Verdict: neither is broken as a task. Both are mis-weighted, and they are the
two worst-weighted decks in the run.**

### Check 3 — the answer key is not in the bundle. Clean on the upload list.

`setup()` uploads exactly `AGENT_ASSETS/"init.pptx"` plus every *file* in
`AGENT_ASSETS/"materials"`. `TEST_ASSETS` is a sibling and is never in that
list, in any of the nine. No `plan.json` / `gt_inventory.json` /
`init_inventory.json` under `assets/`, and no renamed copy — I content-hashed
every material and every `init.pptx` against the three fixtures: zero matches.
`metadata.json` and `README.md` sit at the task-assets root, outside
`AGENT_ASSETS`, so setup never touches them (worth noting for whoever copies
these trees: `README.md` prints the whole scoring table).

Inside the shipped `init.pptx`, nine for nine: no orphaned pre-damage media,
`docProps/app.xml` byte-identical to source but carrying slide titles only (and
no degradation in any deck touches a title), no notes slide describing removed
content, nothing in `core.xml` / `custom.xml` / `customXml` / `revisionInfo`.
An automated sweep — every string present in `gt_inventory` and absent from
`init_inventory`, ≥4 chars, grepped across every XML part — returned zero true
hits.

The disclosure defects that do exist are §1.3, §2.3 and §2.4, all in the
materials rather than in the deck.

For the record, the anti-leak logic does fire: `extract_deleted_images` withheld
`p18-Picture-2/3.png` on deck0003 and `p17-Picture-10.png` on deck0010 because
those bitmaps are still reachable in the damaged deck, and both instructions
were written to match.

### Check 4 — the instruction is about the file that shipped. One failure, one gap.

`INSTRUCTION_MATERIALS` appears on **8 of 9** — missing on `task_1100013`
(§2.1). No task ships an empty `materials/`, so the "omit when empty" branch is
untested in this batch. For the eight, the stated path is byte-equal to
`MATERIALS_VM_DIR` and to what `setup()` creates. `INSTRUCTION_SUFFIX` names the
same `DECK_VM_PATH` the task pins, 9 of 9.

Everything else here checks out and it is worth saying so precisely, because
this is the class that killed two trajectories in the previous batch:

- **No promised-but-missing file anywhere.** Every filename named in prose ships.
- **Every 1-based slide number in every instruction lands on `plan.slide + 1`.**
  Verified against `init.pptx`, including `bca594340f93`, which names five pages
  by caption *and* number.
- **Every scored component has `floor: 0.0`** — no task describes damage that is
  already fine in `init.pptx`. The "hunt the phantom" class is empty.
- **Counts in prose are all correct** ("three of the six illustrated examples",
  "four of them, because one slot carried two pictures", "both are now two rows
  short", "two gaps — one in the top row, one in the bottom").
- **Every "this bit is still fine / look over there" claim is true** — spot-
  checked on all nine, including the load-bearing ones (deck0003's surviving
  arrow on slide 13 and the maps reused from slide 1; deck0007's three checklist
  slides carrying the same node set; deck0009's intact annotation layer on
  slide 12).
- `metadata.json["instruction"]` is byte-equal to `Task.instruction` (suffix
  included) and `README.md` carries the unsuffixed prose, exactly as `emit.py`
  claims — 9 of 9.

The one substantive miss is §1.1, which is not a missing asset but a promise the
plan contradicts.

### Check 5 — the harness contract. Clean against the real `BaseTask`.

Loaded against `/home/yitongli/XLANG/osworld2.0-rollout` with only
`desktop_env.evaluators.getters` stubbed. All nine import, `TASK_CLASS` is a
real `BaseTask` subclass, and construction reproduces
`task_loader._instantiate_task_from_module` (a no-arg `module.TASK_CLASS()`).

The one that would have been the headline — `evaluate` returning a dict where
the runner wants a float — is **supported, twice over**:
`desktop_env.py:256-265` returns `result if isinstance(result, dict) else
float(result)`, and `lib_run_single.py:45-53` pulls `result["score"]` and dumps
the whole dict to `result.json`. All nine were driven through the real dispatch:
every one returned a dict with a float score in [0,1] that survives
`json.dumps`.

`setup(self, setup_controller, use_proxy=False)` matches the real dispatch at
`desktop_env.py:241-254`, and every `execute` / `launch` / `_upload_file_setup`
call binds cleanly against the real `SetupController` signatures.
`intermediate_eval_safe = False` is read at `lib_run_single.py:93-96` and is
correct — `evaluate` pkills WPS. `volume_size = 60` is read at
`run_multienv_claude.py:491`. `platform = "linux"` maps to `os_type="Ubuntu"`.
Class names like `Task04a8b177cff7` are valid identifiers; nothing on any runner
path coerces the id to `int`.

Two attributes are inert rather than wrong, and match every shipped WPS task:
`snapshot = "wps"` is read by nothing (the AMI comes from `image` / the default
map, and `image` is unset on all nine), and `fixed_ip` /
`possibility_of_env_change` are not in `BaseTask._fields()`.

The real harness-level problem is §2.2.

Note one hard dependency: `evaluate` reads the answer key off the **host**
filesystem at `TASK_DIR.parent/"task_assets"/…`, and a missing fixture raises
`FileNotFoundError` rather than scoring 0 — deliberately. The asset trees must
ship alongside the `.py`.

### Check 6 — the thumbnail. Zero leaks, by luck.

Seven of the nine shipped `init.pptx` carry a `docProps/thumbnail.jpeg`
byte-identical to the one in `source.pptx` — definitionally a pre-damage render.
**No deck in this batch damages slide index 0**, so every one is a picture of an
undamaged title slide. Free score from this vector: **0.000 on every deck.**

| task | deck | thumbnail in init | identical to source | resolution | lowest damaged index | verdict |
| --- | --- | --- | --- | --- | ---: | --- |
| `febe30ab6a49` | deck0007 | no (source has none) | — | — | **1** | no thumbnail — nearest miss |
| `1100013` | deck0008 | yes | yes | **768×432** | 2 | does not leak |
| `de4bf5d6c094` | deck0005 | yes | yes | **768×432** | 3 | does not leak |
| `bca594340f93` | deck0006 | yes | yes | 256×144 | 2 | does not leak |
| `9c4d67ef347c` | deck0002 | yes | yes | 256×144 | 5 | does not leak |
| `755e402204c5` | deck0003 | yes | yes | 256×192 | 6 | does not leak |
| `821df4a40fed` | deck0004 | yes | yes | 256×144 | 6 | does not leak |
| `04a8b177cff7` | deck0010 | yes | yes | 256×144 | 14 | does not leak |
| `9bbc9703d8c9` | deck0009 | no (source has none) | — | — | 3 | no thumbnail |

All seven were viewed: every one is the deck's title slide.

**This is luck, not a safeguard.** `pkg_check.ALWAYS_KEEP` explicitly lists
`docProps/thumbnail.jpeg`, nothing regenerates or strips it, and nothing in the
planner excludes slide index 0 from damage. deck0007 landed on index **1**. Had
the proposer picked 0 on a deck with a thumbnail, the giveaway would have been a
deleted `Picture 8` spanning 97% of the slide width, worth 0.295 — unmistakable
even at 256×144. At 768×432 (deck0005, deck0008) body text is fully legible, so
those two would leak content rather than just layout.

The 0-based convention was verified independently rather than assumed, by
semantic shape-tree diffs of `source.pptx` vs `init.pptx` across all nine decks:
observed changes fall inside `damage.slides` under the 0-based reading and never
under the 1-based one.

### The `evaluate()` wrapper, which had never run anywhere

`evaluate` only executes on a VM, so its save contract, stray recovery and
failure shaping have never been exercised. I drove all nine branches against a
mock VM (`task_febe30ab6a49`). Every one behaved as documented:

| scenario | score | outcome |
| --- | ---: | --- |
| correct deck saved in place | 1.0 | scored at the pinned path |
| untouched; forced save writes nothing | 0.0 | "byte-identical to the one supplied" |
| work only in the GUI; forced save flushes it | 1.0 | `SAVED — the bytes at the pinned path changed` |
| deck deleted from the VM | 0.0 | "not on the VM at the path the task pinned" |
| Save-As elsewhere, one candidate | 1.0 | stray found and scored |
| stray beside a supplied material and an unedited copy | 1.0 | both decoys correctly rejected by digest |
| two indistinguishable candidates | 0.0 | none scored, both named in the reason |
| pinned path holds a non-zip | 0.0 | "not a zip container" |
| pinned path holds a zip with no `presentation.xml` | 0.0 | "no ppt/presentation.xml" |

The forced-save-only-when-nothing-was-written logic works, and the material and
unedited-copy decoys are both rejected on digest, exactly as the docstrings
claim. §1.4 is the one hole.

---

## Test suite

`python -m pytest tests/ -q` → **9 failed, 583 passed** (152 s). All nine are
fixture drift: the decks the tests pin have moved underneath them.

- `test_a_delta_without_deg_is_refused` — deck0001's delta now *has* `deg`, so
  the rejection reason the test greps for is gone (it is rejected for five other
  reasons).
- `test_a_high_floor_rejects_the_plan` — deck0009 no longer has a floor above
  the threshold.
- four `[deck0008]` parametrisations — deck0008's plan is now `rejected`, so
  `plan_accepted` fires and `test_the_ground_truth_scores_one` sees a gate.
- `test_the_two_decks_that_shipped_broken_are_the_two_that_fail`,
  `test_an_unsatisfiable_component_is_dropped_not_left_to_punish`,
  `test_an_image_the_application_re_encoded_does_not_read_as_a_deleted_shape`,
  `test_the_hand_rebuild_of_a_real_deck_is_also_paid_for` — same shape.

None indicates a defect in the shipped artefacts. But tests parametrised over
live `work/` decks stop being pins the moment a deck is re-run, which is what
happened here — and the four `[deck0008]` failures are the same event as §2.1
seen from the other side.

---

## Summary of actions

| | action | why |
| --- | --- | --- |
| 1 | `task_9c4d67ef347c`: drop the three `strip_animation` components, or delete the animation sentence | 8.01% is unreachable by an obedient agent |
| 2 | Reconcile `est_steps` against `solvability.est_steps_measured` per degradation before weighting | weights are up to 12× off per step on deck0006 |
| 3 | Delete `task_1100013` from `work/emitted/` | stale build of a rejected deck; no materials paragraph; ships two forbidden assets |
| 4 | Gate `bundle()` on `manifest["produced"]` | deck0006 ships 8 strips for 4; deck0005 ships an unrequested CSV |
| 5 | Resolve the id collision with the rollout repo before copying anything in | deck0002 and deck0003 would ship twice |
| 6 | `_stray_candidate`: stop using the pinned deck as the `-newer` reference | recovery is dead when the deck is gone |
| 7 | `mask_regions`: mask the ink extent, not the frame; and reconsider drawing the hatch at the exact gt size | `9bbc9703d8c9`'s instruction makes a false claim about a shipped asset |
| 8 | `_describe`: `slide + 1`, and read the prose from `task.json` | 238 of 238 descriptions are off by one and prose-free |
