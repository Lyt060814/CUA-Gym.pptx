# Operator audit — the seventeen that had never run

Eight of the twenty-five operators registered in `degrade_exec.py` had ever
executed. This exercises the other seventeen, plus the deck-level `chart` op
and the two asset producers that had never produced anything, against the ten
decks in `work/` — reading only, everything written to a temp directory.

Each was checked four ways: **(a)** does it run, **(b)** does the output pass
`pkg_check.check` and `pkg_check.leak_check`, **(c)** does the `delta.json`
entry record the prior value the reward stage will be built on, **(d)** is the
damage visible in a render of the page.

**Twelve of the seventeen were broken in at least one of those four ways.**
None of them leaked. The most dangerous class was not the crashes — it was
the ops that produced a file every existing gate calls healthy and PowerPoint
does not.

## What the gates could not see

`pkg_check` validates the *reference graph*: content types, r:id targets,
reachability, duplicate shape ids. It is explicitly not a schema validation,
and the docstring says so. That is the right trade for the failures it was
built for, but it means three separate faults here were invisible to it:

* **Element order.** OOXML content models are sequences. `a:ln` comes before
  `a:effectLst`; a fill comes before `a:latin`; `a:srcRect` comes *after*
  `a:blip`. Four ops wrote all of them backwards, because `etree.SubElement`
  appends. LibreOffice renders it, `pkg_check` has no opinion, and a pixel
  diff shows the intended damage — so all three said fine while the file was
  one PowerPoint open away from a repair prompt.
* **Merge arithmetic.** A table cell spanning N columns is written once with
  `gridSpan="N"` and stood in for by `hMerge="1"` cells. Delete one of the
  stand-ins and the tc count still matches the gridCol count, the reference
  graph is untouched, and the anchor now claims more columns than exist.
* **A record that records nothing.** `recolor` serialised the whole `spPr`
  and truncated at 600 characters; three namespace declarations and an
  `a:xfrm` ate the budget every time, so the fill it replaced was never
  actually in the delta.

## Per-operator

Legend: **ran** = executed without raising · **pkg** = `check` + `leak_check`
on the output · **record** = the delta entry carries the prior value ·
**visible** = pixel-diff of the before/after page render (`assets.render_page`
at 110 dpi, `render.pixel_diff_ratio`) · fixes are code, findings are prose.

| operator | ran | pkg gate | leak | delta record | visible | what I did |
|---|---|---|---|---|---|---|
| `text_runs` | ✅ | ✅ / **invalid XML** | none | **✗ no prior style** | 0.032 (deck0008 p3) | **fixed ×3**: `a:solidFill` was appended after `a:latin` in `a:rPr` → inserted in sequence; deleting the last paragraph left `a:txBody` with zero `a:p` (schema requires ≥1) → empties it instead; restyle now records `was_props` (size/bold/italic/underline/font/colour) |
| `clear_text` | ✅ | ✅ | none | ✅ `was_text` | 0.017 | nothing — clean first time |
| `set_text` | ✅ | ✅ | none | ✅ `was_text`/`now_text` | 0.003 | nothing — clean first time |
| `table_drop_rows` | ✅ | ✅ | none | ✅ removed cells | 0.036 (deck0009 p5) | **fixed**: ignored `rowSpan`/`vMerge`. deck0009's table has no merges so it did not surface there; reproduced on a merged table and mended both directions (decrement the anchor, or promote the row below and carry its text) |
| `table_drop_cols` | ✅ | **✅ but corrupt** | none | ✅ removed cells | 0.048 (deck0002 p8) | **fixed** — see "worst" below |
| `strip_effects` | ✅ | ✅ | none | **✗ names only** | 0.009 / **0.573** | **fixed ×3**: only `a:srgbClr` gradient stops were read, so a theme-coloured gradient fell to a hardcoded `BFBFBF` and turned 57% of deck0010 p3 into a flat grey slab (now 0.077, keeping the first stop's colour element whatever its form); records `was_xml` per removed element; also strips a themed shadow via `a:effectRef`, which it previously left on screen while reporting success |
| `crop` | ✅ | ✅ / **invalid XML** | none | ✅ `was_srcRect` | 0.076 set / 0.021 reset | **fixed ×2**: `bf.insert(0, sr)` put `a:srcRect` *before* `a:blip`; `mode: reset` on the empty `<a:srcRect/>` PowerPoint writes on uncropped pictures was a silent no-op that still filed a delta entry |
| `swap` | ✅ | ✅ | none | **✗ half of it** | 0.011 (deck0009 p4) | **fixed**: one entry per *pair*, but `box` is what a masked reference covers — so the reference masked where one shape came from and left the other's correct position on display. Now one entry per shape; verified the masked render covers both (`assets/swap2-masked.png`) |
| `recolor` | ✅ | ✅ | none | **✗ truncated away** | 0.105 | **fixed**: records the removed fill elements themselves plus the theme `fillRef` idx, instead of the first 600 characters of `spPr` |
| `rotate` | ✅ | ✅ | none | ✅ `was_deg` | 0.023 | nothing — clean first time |
| `zorder` | ✅ | ✅ | none | ✅ `was_index` | 0.114 | **fixed**: filed an entry when the shape was already at that end of the tree — a change for the reward to look for that never happened |
| `ungroup` | ✅ | ✅ | none | **✗ unnamed** | **0.000** | **fixed** (record) + **finding** (visibility). Entry carried a positional path and nothing else; now labelled with shape id / name / kind and the member list. Geometry is *exactly* right — nested groups and a 270°-rotated group all render identically — which is also the problem: see findings |
| `detach_connector` | ✅ | ✅ | none | ✅ `was_attachments` | 0.009 real / 0.0004 unattached | nothing — clean. Note the deck0002 p5 connectors carry no `a:stCxn` at all, so there the op is only a nudge; deck0004 p3 has real attachments |
| `blank_slide` | ✅ | ✅ (leak check **applies** and is clean) | none | ✅ `removed_xml` + `box` | 0.091 | **fixed docstring**. The rel sweep is correct: deck0009 p5 dropped image, vmlDrawing and oleObject rels with no orphans. The docstring claimed it keeps placeholders empty; it removes them |
| `delete_slide` | **✗ TypeError** | ✅ (deck-level form) | none | ✅ `deleted_slides` | n/a | **fixed**: the name is in the registry, so the unknown-op guard passed it and the recipe writer got `_delete_slide() takes 3 positional arguments but 4 were given` several frames deep. Now a `SystemExit` naming `delete_slides` |
| `anim_drop_steps` | ✅ | ✅ | none | **✗ spids only** | n/a by design | **fixed**: recorded target spids and nothing else, but the task is graded on *which object appears at which click with what effect* — the reward would have had to re-derive that from a file no longer containing it. Now records class / preset / name / subtype / trigger / duration per removed step |
| `strip_transition` | ✅ | ✅ | none | **✗ `["fade","fade"]`** | n/a by design | **fixed ×2**: PowerPoint 2010+ writes the transition twice (`mc:Choice` + `mc:Fallback`), so the record was the localname twice and lost the speed, the `p14:dur` and the `@prst` that names the whole `prstTrans` family. Now records the resolved transition via `anim_steps.slide_transition`, and removes the empty `<mc:AlternateContent>` both branches leave behind |
| **`chart`** (deck-level) | **✗ IndexError ×3** | ✅ | none (workbook severed) | **✗ no prior value** | 0.002–0.13 | **fixed ×3** in `charts.py` — see below |
| **`mask_regions`** / `reference_image_masked` | ✅ | n/a | n/a | n/a | ✅ | nothing wrong with it. Its only fault was inherited from `swap`; fixed at the source |
| **`keyframes`** / `reference_keyframes` | ✅ | n/a | n/a | **✗ manifest** | ✅ | **fixed ×2** in `assets.py` — see below |

No deck in `work/` contains a native chart (`ppt/charts/chart*.xml`: zero across
all ten), so the chart op was exercised against a deck built with python-pptx
carrying a three-series clustered column chart with a title, data labels, a
legend, gridlines and axis titles.

## The worst one: `table_drop_cols`

It was the only op that produced a **visibly wrong file that every automated
check passes**. deck0002 p8's header is three column groups written as
`gridSpan` anchors followed by `hMerge` stand-ins. Dropping columns 1 and 3 —
both stand-ins inside the first group — removed the cells and left the anchor
still claiming five of what was now a twelve-column grid.

`pkg_check` passes it: the reference graph is untouched. The `tc` count still
equals the `gridCol` count, so a cell-count check passes too. The render is
where it shows — a stray empty blue header cell appears between the groups and
every group label sits a column right of the data it labels. A merged header
that no GUI action could have produced, in a task about repairing a table.

It is fixed on both axes and both cases: dropping a *covered* column
decrements the owning anchor's span; dropping the *anchor* promotes its first
stand-in and carries the group label into it, so the header survives losing the
column it happened to be written in. `merges_mended` goes into the delta.
`tests/test_operators.py` asserts the full invariant after each drop — cell
count, span coverage, and a stand-in behind every span.

## `charts.py`

```python
def _q(path):
    return "/".join(p if p.startswith(".") else f"{{{C}}}{p.split(':')[1]}" ...)
```

`".//c:dLbls".split("/")` is `['.', '', 'c:dLbls']`. The empty middle segment
does not start with `.`, so it fell into the qualifying branch, where
`"".split(":")[1]` raises `IndexError`. Every descendant-axis entry in
`STRIPPABLE` goes through it, so **three of the five things the module can
strip — `data_labels`, `gridlines`, `axis_titles` — crashed the first time a
recipe asked for one.** `legend` and `title` use direct paths and worked.

Also fixed: an unrecognised strip name was silently ignored (now refused by
name, listing the five), and a stripped title recorded the bare word `"title"`
— so a task that says "the chart title is gone, put it back" had no record of
what it said. `stripped_detail` now carries the text and the XML. Series
dropping already worked and correctly severs the embedded workbook: no
`ppt/embeddings/` part survives, which is the leak the module was written to
avoid.

## The two asset producers

**Masked reference renders work.** Driven the way `materialise` drives them,
against four deltas: a dropped table region masks to 41% of the page, two
deleted pictures to 23%, a swap to 10%, and `blank_slide` to 71% — which the
55% guard correctly refuses, exactly as intended. The mask lands on the right
regions and the hatch stays inside its rectangle. The only defect found was
upstream, in what `swap` records.

**Keyframes work.** deck0002 p6 (8 build steps) rendered 9 frames with the
full step description; deck0009 p4 and p5 likewise. Two defects, both fixed:

* On a slide with **no build sequence** it returned zero frames and no error,
  and `materialise` filed that as a *produced* asset. The instruction went on
  promising the solver a reference showing the order things appear in, and
  nothing showed it. Now raises `AssetError`, which routes it to `unmet`,
  where `pipeline`'s "an unmet asset must be dealt with" gate can see it.
* The manifest listed **bare filenames for files one directory down**
  (`step-00.png`, actually at `assets/build-p06/step-00.png`), and carried no
  `file` key — so `pipeline`'s "does every declared asset exist" check, which
  skips entries without one, never checked a keyframe asset at all. Paths are
  now relative to the assets folder, `file` points at `build.json`, and the
  manifest carries the step-by-step sequence (`"3:entr/appear"`), which the
  frames alone cannot convey.

## Findings I did not fix

These are design questions, not bugs. Guessing at them would be worse than
saying them.

1. **`ungroup` is invisible.** Its render diff is 0.000 — on a flat group, on
   a nested group, and on a group containing a 270°-rotated subgroup. That is
   the op working correctly: dissolving a group is defined to leave every
   child where it was. But it means a task whose disclosure tier is a
   reference image is **unsolvable** — the agent has no way to see that
   anything changed, and nothing to compare against. `ungroup` needs either a
   `deck_anchor` disclosure ("the diagram on slide 4 was a single object like
   the ones on slides 5 and 6") or to be paired with an op that does show. It
   should probably not be offered as a standalone degradation at all. Worth a
   line in the proposal skill; that file is not mine.

2. **`leak_check` never applies to `chart_edit` or `smartart_drop_nodes`.**
   It derives `removed_kinds` from op names ending in `_delete`, plus
   `delete` and `blank_slide`. A chart series drop and a SmartArt node drop
   both remove real content and both report `{"applicable": false}` — so the
   answer-leak check is skipped entirely for the two ops most likely to strand
   a data part. No actual leak was observed (`charts.rewrite` severs the
   workbook itself and `smartart` is guarded by `_rels_named_by_live_parts`),
   but it is unchecked rather than checked-and-clean. The fix belongs in
   `pkg_check.leak_check`, which another agent holds.

3. **`text_runs` cannot reach table text.** It walks `p:txBody`; a table cell's
   text is in `a:txBody` inside `a:tc`. So "the emphasis was stripped from two
   rows of the table" is not expressible — `clear_table_cells` empties cells
   but cannot restyle them. A real gap in coverage, but adding cell addressing
   to `text_runs` is a schema decision (does `paragraphs` mean paragraphs
   within a cell, or rows?) rather than a repair.

4. **`detach_connector` on an unattached connector is nearly a no-op.** Three
   of deck0002 p5's connectors carry no `a:stCxn`/`a:endCxn` at all — they are
   drawn lines, not attached ones — so the op records
   `was_attachments: []` and only applies the nudge, for a 0.0004 render diff.
   It does not lie about it, and a recipe writer picking from the census can
   see which connectors are attached. Whether the op should refuse an
   unattached connector outright is a proposal-side judgement.

5. **`table_drop_cols` does not redistribute column widths**, so the table
   ends up narrower than its frame. That is what PowerPoint does when you
   delete a column, so it is left alone.

## Regression

All ten shipped `recipe.json` files were re-run through the changed executor:
identical op counts in every delta, `pkg_check.check` clean, `leak_check` clean,
no dead rels. The eight operators that were already in service — `delete`,
`move`, `set_font`, `resize`, `strip_animation`, `scatter`,
`clear_table_cells`, `outline` — are unchanged in behaviour, except that
`set_font` and `outline` picked up the sequence fix and `set_font` now records
`was_props` as well as `was_sizes`.

`tests/test_operators.py` adds 31 tests, one per fault. `python -m pytest
tests/ -q` → 147 passed.
