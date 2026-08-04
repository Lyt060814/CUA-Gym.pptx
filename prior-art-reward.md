# Stocktake of the off-the-shelf parts for the reward stage — what can be moved, and where it breaks when it is

REWARD.md section 6 says "the existing code lives elsewhere; align before
moving it". This file is that alignment.

Three files in `/home/yitongli/XLANG/pptx-tasks/scaling/pipeline/`:
`ops.py` (17 operators + comparators), `evaluator.py` (registry-driven
scoring), `verify.py` (4 probes + an 8-case adversarial battery). This repo's
`pptxgym/degrade_exec.py` has **25**.

**One-sentence conclusion: the intersection of the two registries is empty, and
of the 25 operators, 0 can be scored today.**
`audit_delta` run for real against `work/deck0001/delta.json` rejects all 23
records (`op='delete' has no registered comparator`). This is not simply a
matter of names not lining up — see section 3.

---

## 1. Operator mapping table

The 25 in `degrade_exec.REGISTRY`, one by one against the 17 in
`ops.REGISTRY`. The criterion is **semantics**: sharing a name does not count,
damaging the same thing does.

| # | `degrade_exec` operator | `ops.py` counterpart | relation | notes |
|---|---|---|---|---|
| 1 | `delete` | **none** | ✗ nothing at all | only `chart_delete` deletes anything, and only native charts. Deleting a picture / text box / table / group / SmartArt has no comparator whatsoever. **This is 153/271 = 56% of the degradations across ten decks** |
| 2 | `delete_slide` | **none (and conflicting)** | ✗ incompatible in reverse | `evaluator` has a hard gate `slide_count`: an unequal slide count returns 0 outright. A slide-deletion task is permanently 0 under the existing scorer |
| 3 | `scatter` | `move_shape` / `_geom_compare` | ≈ semantically equivalent | both are "push it away from where it was at random, restore it to the original coordinates". The comparator logic is usable; the record shape has to be converted |
| 4 | `move` | `move_shape` / `_geom_compare` | ≈ semantically equivalent | same, except the displacement is deterministic |
| 5 | `resize` | `resize_shape` / `_geom_compare` | ≈ semantically equivalent | the same `_geom_compare`; `ops.py`'s three geometry operators share one comparator |
| 6 | `rotate` | `rotate_shape` / `_geom_compare` | ≈ semantically equivalent | `was_deg` is in degrees and `expected.rot` is in degrees; the units agree |
| 7 | `swap` | no same-named one, `_geom_compare` usable | ~ partial overlap | swaps the positions of two shapes. `ops.py` has no notion of "pairing", but the restoration criterion is just "each returns to its own centre", which is exactly `_geom_compare`'s position term. **But position is only 0.6 of `_geom_compare`, with rot/size giving away 0.2 each** — swap does not touch rot/size, so doing nothing scores 0.4 |
| 8 | `clear_text` | `clear_text` / `_clear_text_compare` | ≈ same name, same semantics | both empty `a:t`. The `ops.py` version leaves `…` in the first run, the `degrade_exec` version clears everything (or keeps the first paragraph). The comparator is text similarity, which applies to both |
| 9 | `set_text` | no same-named one, `_clear_text_compare` usable | ~ partial overlap | the damage differs (rewritten wrongly vs wiped), but the restoration target is the same: the original text comes back. Feed it `was_text` |
| 10 | `set_font` | **none** | ✗ nothing at all | `ops.py` has no run-level text-style comparator at all. `color_mutate` reads the shape fill, not the run fill. **This is 43/271 = 16% of the degradations** |
| 11 | `strip_effects` | `strip_outershdw` / `strip_glow` / `strip_reflection` / `strip_softedge` / `flatten_3d` / `gradient_to_solid` | ~ 1:6 fan-in | one operator kills six classes of thing at once. All six comparators exist, but one delta record has to be split into six scoring units. **Neither side has a comparator for `effectRef` (the theme effect index) or `effectDag`** — and `strip_effects` changes `effectRef` |
| 12 | `recolor` | `color_mutate` / `_color_compare` | ~ partial overlap | equivalent for solid → solid (a ΔE criterion). But `recolor` also kills `gradFill`/`blipFill`/`pattFill`; if the original was a gradient, `_color_compare` reads `fill.color` and gets `None` → `delta_e(None,·)=100` → **a perfect restoration also scores 0**. It has to dispatch to `_grad_compare` based on the original fill type, and in the record the original type exists only as an XML string |
| 13 | `outline` | `line_remove` (mode=remove) / `line_reset` + `line_color_mutate` (mode=set) | ~ partial overlap | decent coverage. But `ops.py` wants a parsed line dict `{w,dash,color,head,tail}` and `degrade_exec` stores the string `was_ln_xml` (**truncated at 600 characters**). The `lnRef` theme index has no comparator |
| 14 | `zorder` | **none** | ✗ nothing at all | census has `z` / `top_z` fields, so the facts are there and the comparator is not |
| 15 | `ungroup` | **none (and unmeasurable)** | ✗ structural gap | `evaluate.leaf_shapes` explicitly filters out `kind == "group"`, and `match_slides` only matches leaves. **Regrouped and never-grouped are byte-identical in the eyes of the existing scorer.** This is the other side of the "0.000 render difference" that `operator-audit.md` found |
| 16 | `clear_table_cells` | **none** | ✗ nothing at all | census's `read_table` already stores `rows[].cells` (each cell's text truncated to 80 characters, **at most 24 rows**) and `merges`. The facts are complete, the comparators are zero |
| 17 | `table_drop_rows` | **none** | ✗ nothing at all | same |
| 18 | `table_drop_cols` | **none** | ✗ nothing at all | same. Note that `n_cols` comes from `tblGrid`, which is exactly where the merge arithmetic error in `operator-audit.md` shows up |
| 19 | `text_runs` | **none** | ✗ nothing at all | paragraph-level restyling / paragraph deletion. `text_style.shape_text` already gives per-run formatting **after inheritance is resolved** (capped at 12 paragraphs × 8 runs); there is no comparator. Deleting paragraphs can be caught indirectly by `_clear_text_compare`'s similarity; restyling cannot be caught at all |
| 20 | `detach_connector` | **none** | ✗ nothing at all | census has `st_cxn` / `end_cxn`, so the facts are there and the comparator is not |
| 21 | `crop` | `reset_crop` / `_crop_compare` | ≈ the same thing, **but the comparator is broken** | see section 2. And `degrade_exec` also supports `mode=set` (change it to the wrong crop), while `ops.py`'s apply only clears |
| 22 | `anim_drop_steps` | **none** | ✗ nothing at all | census records no animation at all. `anim_steps.py` exists on both sides, but no comparator reads it |
| 23 | `strip_animation` | **none** | ✗ nothing at all | |
| 24 | `strip_transition` | **none** | ✗ nothing at all | |
| 25 | `blank_slide` | **none** | ✗ nothing at all | it is `delete` in bulk |
| + | `smartart_drop_nodes` (`run()` writes it straight into delta) | **none** | ✗ nothing at all | |
| + | `chart_edit` (same) | the **body** of `_chart_compare` is usable | ~ partial overlap | `_chart_compare` is the best-written thing in `ops.py` (it requires a native chart and uses a 2% relative tolerance on values). But it wants `expected.{plot,title,series}`, and `charts.rewrite`'s report contains no complete spec; on top of that, `evaluator` sees `family=="chart"` and goes to `_nearest_chart` slot matching for rebuilds, whereas `chart_edit` leaves the frame in place and should match by path |

`run()` also writes four **top-level** records: `deleted_slides`,
`reorder_slides`, `cleared_notes`, `layout_edits`. `evaluator.evaluate` only
iterates `delta["slides"]`, so these four are **completely invisible** — not
scored low, but never entering the scoring at all.

### The reverse: comparators in `ops.py` with no corresponding degradation operator

There is only one true orphan:

- **`round_to_rect`** (rounded corners become square) — `degrade_exec` has no
  operator that changes `prstGeom`.
- `chart_delete` is half an orphan: the semantics line up when `delete` removes
  a chart frame, but **no `degrade_exec` operator records a chart's series
  spec**, so `expected` can never be filled in and having the comparator is the
  same as not having it.

The other 15 all find a semantic source among the 25 (six strip-class ones
crowd into a single `strip_effects`).

### Weighted by actual usage

271 records across the ten decks' `work/*/delta.json`:

| operator | records | comparator? |
|---|---|---|
| `delete` | 153 | ✗ |
| `set_font` | 43 | ✗ |
| `outline` | 28 | ~ (needs adapting) |
| `move` | 24 | ≈ |
| `scatter` | 8 | ≈ |
| `resize` | 7 | ≈ |
| `strip_animation` | 3 | ✗ |
| `smartart_drop_nodes` | 3 | ✗ |
| `clear_table_cells` | 2 | ✗ |

**Across the ten tasks already produced, comparator logic covers 67/271 =
24.7% of the degradations; of the deletions, which are 56%, it covers none.**

---

## 2. What each of the 17 comparators actually does

What it reads, what tolerance, what it returns, whether it gives partial
credit. The numbers are the real values in `ops.DEFAULT_TOL`.

```
DEFAULT_TOL = {dir_deg: 15.0, blur_pct: 0.35, delta_e: 25.0, width_pct: 0.30,
               angle_deg: 20.0, pos_emu: 109728 (=0.12in), rot_deg: 7.0,
               size_ratio: 0.12}
```

Every comparator has the signature
`compare(entry, rec, tol, r_gt, r_res) -> (float, str)`, where `rec` is the
census record of the **result file** and `r_gt` / `r_res` are the two sides'
`ThemeResolver`s.

| comparator | reads | tolerance | partial credit |
|---|---|---|---|
| `strip_outershdw` | `rec.style.effects.outerShdw` | direction 15°, blur `pct_close(±35%, floor 25400 EMU)`, colour ΔE≤25 | **yes**: presence 0.3 + direction 0.3 + blur 0.15 + colour 0.25 |
| `strip_glow` / `strip_reflection` / `strip_softedge` | same, each with its own key | radius ±35% | **yes**: presence 0.7 + radius 0.3 |
| `flatten_3d` | `rec.style.sp3d` / `scene3d` | none (boolean) | **yes**: nodes restored / nodes expected |
| `gradient_to_solid` | `rec.style.fill.stops` | endpoint colours ΔE≤25 (**taking the smaller of the two directions**, so reversed stop order is allowed), angle 20° | **yes**: is a gradient 0.3 + colour 0.5 + angle 0.2. When `expected.angle` is None the angle points are given away |
| `line_reset` | `rec.style.line` | width ±30% (floor 6350 EMU=0.5pt), colour ΔE≤25 | **yes**: dash 0.3 + width 0.2 + colour 0.3 + arrowheads 0.2. **If there were no arrowheads to begin with, that 0.2 is given away; if the original colour cannot be resolved (`e_rgb is None`), so is the 0.3** |
| `line_remove` | same | same | **yes**: width 0.4 + colour 0.6. Stricter than `line_reset`: a hairline counts as not restored, and there are no giveaway terms |
| `line_color_mutate` | `rec.style.line.color` | ΔE≤25 → 1.0; ΔE≤40 → **0.5**; otherwise 0 | **yes**: three bands |
| `round_to_rect` | `rec.style.prstGeom` | none. Any of `ROUND_GEOMS` (8 rounded/chamfered kinds) counts as correct | no: 1.0 / 0.0 |
| `color_mutate` | `rec.style.fill.color` (through the resolver) | ΔE≤25 → 1.0; ≤40 → **0.5**; otherwise 0 | **yes**: three bands |
| `move_shape` / `rotate_shape` / `resize_shape` | `rec.cx/cy/w/h/rot` | position **0.12in**; **half credit out to 0.30in** (2.5×); rotation 7°; size ±12% | **yes**: a hit is 0.6+0.2(rot)+0.2(size); half band 0.3+0.1+0.1; otherwise 0 |
| `clear_text` | `rec.text`, NFC-normalised + whitespace-folded + lowercased | `SequenceMatcher.ratio()` ≥0.95 → 1.0; ≥0.75 → **0.5**; otherwise 0 | **yes**: three bands |
| `reset_crop` | `rec["srcRect"]` ← **this key does not exist** | ±3000 per edge (three thousandths of a %) | **yes**: edges hit / edges total |
| `chart_delete` | `rec.chart` (census reads it from `numCache`) | 2% relative on values (`_num_close(rel=0.02)`) | **yes**: type 0.2 + series count 0.15 + series names 0.15×hit rate + categories 0.10×hit rate + values 0.40×hit rate. **A non-native chart (a pasted image) is permanently 0** |

### `reset_crop` is broken, and has never once run

census puts the crop at `rec["crop"]["srcRect"]`, and **divides the values by
1000** (`{k: int(v)/1000.0 ...}`, i.e. it stores 12.5 to mean 12.5%); delta's
`expected.srcRect` stores `dict(src.attrib)`, the raw string `"12500"`. The
comparator reads `rec.get("srcRect")` — top level, which does not exist.
Measured:

```
perfect restoration (a census-shaped rec) -> (0.0, '0/2 crop edges')
if rec carried the raw srcRect directly   -> (1.0, '2/2 crop edges')
```

**Two bugs stacked on each other: the key is one level off, and the unit is
1000× out.** It went unnoticed because `reset_crop` **never once appears** in
the 11 ops-style deltas.

### Comparators that have never run on a real task

The operators appearing across the 11 ops-style deltas (4 webui tasks + 5 seed5
candidates + 2 copies): `line_reset` (8), `line_remove` (6), `move_shape` (6),
`chart_delete` (4), `color_mutate` (4), `clear_text` (3), `strip_shadow` (3),
`gradient_to_solid` (3), `line_color_mutate` (2), `round_to_rect` (2),
`rotate_shape` (1), `resize_shape` (1).

**Never run: `strip_glow`, `strip_reflection`, `strip_softedge`, `flatten_3d`,
`reset_crop` (5 of the 17).**

And one self-demonstration: those 3 records say `strip_shadow`, while the
current registry calls it `strip_outershdw`. **The same repo's own registry has
already drifted once, and the old deltas would not pass `audit_delta` today.**
That is precisely what the discipline being established here is meant to
prevent.

---

## 3. The delta record contract: where the two sides fail to line up (**the section that matters most**)

### Two record shapes

```jsonc
// what build_task.py writes (what ops.py / evaluator.py / audit_delta expect)
{"gt": ..., "seed": 0, "tolerance": {...8 items...}, "slides": {"2": [
  {"op": "line_remove", "op_zh": "边框去除", "family": "style",
   "path": "10", "key": "txt:bebafa0258fb5f92#0", "directive": "…",
   "bbox": {"cx": 7128830.5, "cy": 881012.0, "w": 3720733, "h": 1049712},
   "expected": {...a parsed style dict...}, "removed_xml": "<a:ln …complete…>",
   "floor": 0.0}]}}

// what degrade_exec.py writes
{"gt": ..., "recipe": ..., "input": ..., "dropped_rels": {...}, "slides": {"2": [
  {"path": "10", "op": "outline", "shape_id": "7", "name": "…", "text": "…",
   "kind": "textbox", "mode": "remove",
   "was_ln_xml": "<a:ln …first 600 characters…", "was_style_line": "1",
   "box": [150763, 1637752, 11890473, 3285640]}]}}
```

### Conflicts, one by one

| # | field | `ops.py` side | `degrade_exec` side | how it fails | severity |
|---|---|---|---|---|---|
| 1 | **`expected`** | every comparator reads `entry["expected"]` on its first line | **not one operator writes it**. The original values are scattered across 12 different keys: `was` / `now` / `was_text` / `now_text` / `was_sizes` / `was_props` / `was_ln_xml` / `was_fill_xml` / `was_xml` / `was_srcRect` / `was_deg` / `was_index` / `was_attachments` / `cleared` / `removed` / `touched` | **silently full marks** — see below | **highest** |
| 2 | `bbox` | `{cx, cy, w, h}`, **centre + size**, float | `box`: `[x, y, cx, cy]`, **top-left + size**, list; deck-level entries (`path="-"`) **do not have it at all**; `_box()` may return `None` | `KeyError: 'bbox'`, loudly | high but visible |
| 3 | `key` | `audit_delta` requires it; `evaluator` uses it in the report | not written. There is `shape_id` / `name`, but that is not a stable instance key | refuses to publish | medium |
| 4 | `tolerance` (top level) | `evaluator` writes `delta["tolerance"]`, **not `.get`** | not written | `KeyError`, loudly | medium |
| 5 | **`floor`** | `evaluator`: `m.get("floor", 0.0)`, defaulting to 0 → no normalisation | not written. `compute_floors` lives in `build_task.py`, not on the scoring chain | **silently cancels one of REWARD.md section 4's two defences** | **high** |
| 6 | `removed_xml` | `restore()` calls `etree.fromstring(entry["removed_xml"])` directly | present, but **truncated**: `delete` 4000 characters, `blank_slide` 1500, `outline` 600, `recolor` 800 per fill, `strip_effects` 2000 each | `XMLSyntaxError`; swallowed by `except: pass` in `verify.scripted_restore` → the probe score quietly drops | high |
| 7 | original text value | `expected.text`, the full text | `was_text[:600]` | a perfectly restored 1200-character shape gives `SequenceMatcher` ≈0.67 comparing a 600-character truncation against the full text → **scored 0** | high |
| 8 | `family` | `evaluator` uses it for the geometry guard rail and the chart branch | not written | `op is None` → 0 | medium |
| 9 | `path` semantics | `build_task.walk_elements`: iterates children directly, recognising only `SHAPE_TAGS`, and **does not descend into `mc:AlternateContent`** | `index_shapes` uses `census.shape_children`, which **does descend into `mc:AlternateContent`'s Choice/Fallback branches** | on a deck with AlternateContent, the same path points at **different shapes**, silently | high (the old pipeline has it too) |
| 10 | slide numbering base | the keys of `slides` are 0-based | the keys of `slides` are also 0-based ✓, but `"slide"` inside the `smartart_drop_nodes` / `chart_edit` entries is **1-based** | mixing them is off by one slide | medium |
| 11 | paths after a deletion | the ops operators barely delete shapes, so paths are still valid in the input | lots of `delete`, so the input's positional numbering has already shifted; delta records the **original** numbering (deliberately) | the scorer looking a shape up by path in the result file finds the wrong one | high |

### The one that matters most: a missing `expected` fails silently, in the direction of leniency

It does not raise; it gives full marks. Measured (calling `ops.py` as-is, with
`entry` in the degrade_exec shape and `rec` a shape that has been **left
completely unrepaired**):

```
strip_outershdw    (1.0, 'restored')
flatten_3d         (1.0, 'n/a')
clear_text         (1.0, 'n/a')
reset_crop         (1.0, 'n/a')
```

All four comparators contain `if not exp: return 1.0` — a branch that was meant
to say "do not dock points for something we failed to record", and that was
never once triggered in the original pipeline because `build_task` always wrote
`expected`. Swap in `degrade_exec`'s records and **that branch becomes the
default path**.

Stack item 5 on top (a missing `floor`, so no normalisation) and the result is:
**the broken file, with nothing done to it, scores 1.0 on these four
operators.**
That punches straight through both of REWARD.md section 4's sentences — "no
tolerance may let doing nothing score" and "`score(input)` must be 0 after
floor normalization" — and it is **completely invisible until a task is scored
wrongly**: `evaluator` raises nothing, `gates` are all green, `raw_score` is
1.0.

**That is the right answer to this investigation: it is not `bbox` vs `box`
(that one is loud), it is the missing `expected` plus the missing `floor`, both
of which are silent and both of which give points away.**

---

## 4. What `verify.py` has already proved, and how `accept` decides

### Four probes

| probe | how the result file is made | threshold | measured on 4 tasks |
|---|---|---|---|
| `equivalent_repr` | applied to the **gt**: `schemeClr` → resolved to `srgbClr` (keeping alpha), `outerShdw` rebuilt as an element with reordered attributes | ≥ **0.85** | 1.0 × 4 |
| `input_floor` | evaluates `input.pptx` directly | ≤ **0.30** | 0.0 × 4 |
| `scripted_restore` | calls `op.restore(el, m)` for each delta entry, stuffing the recorded XML back in; charts go through `build_chart_from_entry` to be rebuilt | ≥ **0.90** | 1.0, 1.0, 1.0, **0.375** |
| `blind_solver` | looks only at the broken file: sprays a default shadow onto every shape and snaps coordinates to an inferred grid (tolerance 0.3in) | ≤ **0.40** | 0.0 × 4 |

### The eight adversarial cases and their bands

```
perfect / writer_noise / snippet_restore / srgb_equiv   [0.85, 1.01]
half_restore                                            [0.15, 0.85]
noop                                                    [-0.01, 0.30]
wrong_params                                            [-0.01, 0.50]
paste_hack                                              [-0.01, 0.001]   ← relies on the gate
```

`wrong_params` is the most carefully made one here: the structure is fully
restored and every parameter is wrong (shadow direction flipped 180°, dashes
removed, `srgbClr` inverted, `schemeClr` inverted after resolution, chart values
`v*1.9+7`), and it **deduplicates per element** — when the same shape is named
by two directives, flipping 180° twice would cancel itself out.

### `accept`'s criteria

```python
accept = all(4 probes pass) and adversarial_pass_rate >= 0.9 and package_ok
```

`package_ok = pkg_check.check(input).ok and not leaks and not dead_rels`.

Note that `MIN_ADV = 0.9` with 8 cases: 7/8 = 0.875 < 0.9.
**What it actually means is "all eight pass", with no middle ground.**

### Against REWARD.md section 5's five probes

| REWARD.md | verify.py | equivalent? |
|---|---|---|
| `equivalent_repr` | ✅ present | **not equivalent, it covers only 1/3**. REWARD.md section 1's right-hand column names three kinds of equivalence: theme colour → sRGB (✅ covered), a rebuilt shape with different XML structure (❌ untested), a cropped picture turned into `blipFill` (❌ untested) |
| **`roundtrip_identity`** | ❌ **absent** | the closest thing is the adversarial case `writer_noise`, but that is `Presentation(gt).save()` — **a python-pptx round trip, not the application's**. What REWARD.md asks for is "the ground truth through WPS / LO once and still 1.0", which is the thing section 2 measured. And it is a case, not a probe |
| `input_floor` | ✅ present | half-equivalent. The threshold is **≤0.30**, REWARD.md says **0.0**. More importantly: `build_task.compute_floors` has already subtracted each entry's floor, so this probe is **partly a tautology** — it verifies that `compute_floors` ran, not that the tolerance is safe |
| `scripted_restore` | ✅ present | equivalent, and it really does bite: 1 of the 4 tasks measured 0.375 and was blocked with `accept=False` |
| `blind_solver` | ✅ present | equivalent. 0.0 × 4 |

**Four of the five are there, and the missing one is exactly the one REWARD.md
says to write first.**

### Three things `verify.py` does not prove but REWARD.md explicitly demands

1. **The geometry tolerance is 12× wider than REWARD.md's settled standard.**
   `pos_emu = 0.12in`, with half credit out to `0.30in`; REWARD.md section 3①
   says the default tolerance is the floating-point-noise band
   (`POS_TOL = 0.01in`), and anything wider needs measured evidence from WPS
   behind it. 0.12in is 12×, 0.30in is 30×.
   And `evaluator`'s `untouched_slides` gate uses
   `untouched_slide_ok(tol_frac=0.92)` plus a centre tolerance of **0.15in** —
   which amounts to waving through 8% of shapes and 0.15in of drift on
   undegraded slides by default.
2. **`_geom_compare` judges w/h on every shape (`size_ratio=0.12`), including
   autofit text boxes.**
   REWARD.md section 3② says this kind of size **has to be removed from the
   scored components**, not widened. The 0.600in font difference measured in
   section 2.4 lands exactly here.
3. **`_clear_text_compare` compares text directly, with no `APP_FILLED`
   exemption.**
   The trap REWARD.md section 7's first item was caught by twice (the text of
   date / slide-number / header-footer placeholders is generated by the
   application) is fixed in `roundtrip.py` and **does not exist** in `ops.py`.
   `clear_text.applies_to` does not exclude placeholders either (only
   `color_mutate` checks `semantic == "content"`).

---

## 5. Verdict, file by file

Premise: this project's settled principle is "a stage that has never been run
has no business in a pipeline other people are meant to use". So every item
below has to say **whether it has run, and on how many tasks**.

### `ops.py` — **rewrite the registry structure, cherry-pick comparator bodies**

**Evidence:** 11 ops-style deltas, 4 tasks passed `verify`. Of the 17
comparators, **12 have appeared on a real task and 5 have never run**, and
`reset_crop` among them is measurably broken. There are no unit tests at all
(`seed1_mvp/` has only `test_census.py`).

**Why it cannot be moved as-is:**
- the registry's keys (operator names) have **zero intersection** with this
  repo's 25, and 14 of the 25 have no semantic counterpart at all;
- the `Op` dataclass binds `applies_to` / `apply` / `restore` / `cost` /
  `min_area_in2` / `describe` and `compare` together. This repo's apply side is
  `degrade_exec`, which has already run on ten decks and been fixed item by item
  by `operator-audit.md`; **bringing apply along with it is a regression**. The
  reward stage needs only `compare`, and the registry should be a **pure
  mapping** of "operator name → comparator";
- `restore` is worth keeping a copy of on its own — `scripted_restore` depends
  on it — but it has to be changed to read `degrade_exec`'s record keys, and
  the truncation (section 3, item 6) has to be solved first.

**Function bodies that can be moved directly (just change the line that reads
`expected`):** `_geom_compare`, `_clear_text_compare`, `_color_compare`,
`_line_compare`, `_line_gone_compare`, `_line_color_compare`, `_grad_compare`,
the four inside `_strip_effect_factory`, `_flatten_3d_compare`,
`_chart_compare`.
`_chart_compare` is especially worth taking: "it has to be a native chart" plus
a 2% relative tolerance on values makes it the only comparator that kills
"paste in a picture" dead.

**Must be written new, and census has already prepared the facts:**
tables (`rec.table`: rows/cells/merges/col_widths), run-level text style
(`text_style.shape_text`, with inheritance resolved), z-order (`rec.z`/`top_z`),
connectors (`rec.st_cxn`/`end_cxn`), animation (`anim_steps.py`, which census
does not record), SmartArt (`rec.diagram`), and the biggest one of all:
**deletion of an arbitrary shape**.
**Group restoration is unmeasurable under the existing matching mechanism**
(`leaf_shapes` filters out groups); that is not a matter of adding a comparator,
it is a structural problem in `evaluate.match_slides`.

**The tolerances cannot be copied over.** `DEFAULT_TOL` was set against
LibreOffice's worst case, and REWARD.md section 2.2 has already demoted that
column of numbers from "source of tolerance" to "corpus-fragility signal".
Moving `DEFAULT_TOL` in means adopting the p90 of a renderer that takes no part
in scoring as the standard, and REWARD.md section 2.2 says outright that that
is a pure giveaway.

### `evaluator.py` — **move it after changes** (the skeleton is good; the contract and the gates need changing)

**Evidence:** the same 4 tasks. 171 lines, no tests.

**Worth keeping:** the skeleton "the delta record says which operator it was,
the registry gives the comparator" is exactly the shape REWARD.md wants; the
part of `_nearest_chart` that excludes surviving charts (without it, a deck with
paired charts scores 1/3 of the unit for doing nothing) could only have been
written by someone who had stepped in that hole; and the three gates
`anti_paste` / `no_stray_additions` / `untouched_slides` all point the right
way.

**Must be changed:**
1. `delta["tolerance"]` needs a default; `m["bbox"]` needs to read `box` and
   convert to a centre; `m["key"]` should be for the report only, not a hard
   dependency;
2. **floor normalization has to move onto the scoring chain**. Today
   `compute_floors` is in `build_task.py` and this repo has no equivalent — and
   without moving it in, the silent full marks in section 3, item 5 hold;
3. the `slide_count` hard gate directly conflicts with `delete_slides` /
   `reorder_slides`, and both of those operators have been used;
4. the four top-level record types `deleted_slides` / `reorder_slides` /
   `cleared_notes` / `layout_edits` currently **do not enter the scoring at
   all**;
5. `untouched_slides`' 0.15in / 92% band has to be reset per REWARD.md section
   3①;
6. `walk_elements`' path semantics have to be swapped for
   `census.shape_children` (section 3, item 9).

### `verify.py` — **move it after changes, but write `roundtrip_identity` first**

**Evidence:** 4 tasks, 3 `accept=True`, 1 `accept=False`
(`scripted_restore=0.375`). This is the **only one of the three files with a
real verdict on its record** — it has blocked something; it is not decoration.
No unit tests.

**Worth keeping:** how the four probes are constructed, the eight-case band
table, `wrong_params`' per-element deduplication, and the implicit strictness of
`MIN_ADV` with 8 cases meaning "all pass". The `pkg_check` part can be used
as-is — the two repos' `pkg_check.leak_check` logic agrees (a 50-line diff, with
`leak_check` itself identical).

**Must be changed:**
1. **write `roundtrip_identity` first**, the one REWARD.md section 5 names as
   the first to write, and this repo already has the ingredients:
   `pptxgym/roundtrip.py` (`_facts` / `compare` / `POS_TOL=0.01in` /
   `APP_FILLED` keying by role) and `pptxgym/wps_roundtrip.py` (Xvfb + xdotool,
   really opening and really saving, 0.0% across ten decks). **This is the piece
   this repo leads on, not one to be moved in.** Note that `writer_noise` cannot
   stand in for it: that is python-pptx's round trip, not the application's;
2. `equivalent_repr` has to cover the other two kinds in REWARD.md section 1's
   right-hand column (structural differences after a shape is rebuilt, a cropped
   picture turned into `blipFill`);
3. `scripted_restore` depends on `op.restore` and a complete `removed_xml`;
   until the truncation is solved this probe can only return a score lower than
   the truth, and a silently lower one, swallowed by `except: pass`;
4. `input_floor`'s threshold of 0.30 should come in to 0.0 (REWARD.md's own
   words), but floor normalization has to be moved along with it, or the probe
   becomes a tautology.

### `pkg_check.py` — **no need to move it**

This repo already has it, and has the newer copy (408 lines vs 360).
`operator-audit.md`'s "finding 2" records its known gap: `leak_check` returns
`{"applicable": false}` for `chart_edit` and `smartart_drop_nodes`.

### `styles.py` / `render.py` / `smartart.py` — **no need to move them, they are already byte-identical**

`diff` is 0. For `census.py` (56 lines apart), `charts.py` (35 lines apart) and
`anim_steps.py` (12 lines apart), this repo is the newer side.

---

## 6. The order to do it in, under REWARD.md's framework

If the goal is "one delta record → a comparator registry → a reward function",
the order is:

0. **Fill in the `expected` contract first.** Either add one uniform `prior`
   field to every operator in `degrade_exec` (without removing the existing
   `was_*`, just adding a canonical place for it), or write a layer of
   "operator name → dig the original value out of this record" adapters. The
   latter is better: **it does not touch an executor that has already run on ten
   decks and been audited item by item.** The adapter is also the natural
   landing place for "the comparator is written against operator semantics and
   may not look at the specific recipe" (REWARD.md section 7).
   At the same time, raise the truncation limits far enough to restore
   completely (or simply store a hash plus the complete XML in a file
   alongside).
1. **`roundtrip_identity`**, using this repo's `wps_roundtrip` +
   `roundtrip.compare`. It is the cheapest, and per REWARD.md it will point
   straight at which components in the comparator **should not exist at all**
   (autofit sizes, placeholder text).
2. **Move floor normalization onto the scoring chain**, rather than leaving it
   on the task-building side.
3. Add comparators in order of usage: `delete` (56%) → `set_font` (16%) → the
   three table operators → `text_runs` → animation / transitions → SmartArt.
4. Start tolerances at 0 (`POS_TOL=0.01in`), and re-run the adversarial battery
   at every notch of widening to confirm `noop` has not gone up (REWARD.md
   section 4).
5. Do not make the grouping operators (`ungroup`) into a task of their own yet —
   the missing comparator is the small part; `match_slides` not seeing groups is
   a structural problem, and `operator-audit.md`'s "finding 1" already pointed
   at the same thing independently from the solvability side.
