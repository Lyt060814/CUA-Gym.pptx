# attack battery — 10 decks, 14 attacks

Built by `pptxgym/attacks.py`; tests in `tests/test_attacks.py` and
`tests/test_comparators.py`.

```bash
python3 -m pptxgym.attacks work/deck00*/ -o attack-report.md --wps-workers 2
```

Each attack constructs a candidate deck that **should not score** and scores it
through `comparators.score`.  An attack over its threshold rejects the task.  So
does an attack that **applies and cannot be built** — a gate you never fired is
not a gate.  `n/a` is the only thing that is not a rejection, and it always
carries the reason.

`half_restore` is not an attack.  It restores half the reward mass and must land
near 0.5: an earlier batch shipped tasks that scored all-or-nothing across five
components, and a task that cannot pay for partial work is as unusable as one
that can be cheated.

**Provenance.**  The run below judged `comparators.py` at
`sha256:8dbab6fcdd33`, ten decks in 412 s, every WPS round trip really taken
(no `—` in the `gt_roundtrip` row: the earlier attempt at three concurrent
displays had WPS killed for memory on three decks, which is an unproven gate,
not a pass).  The previous run of this file judged `sha256:77401f9847c6`; that
revision was re-run first, before anything was changed, and reproduced its
reported numbers exactly — so nothing in the table below had already been
fixed by somebody else.

---

## Verdict

**7 of 10 decks survive the battery**, up from 0.  **No attack beats its
threshold on any deck.**  The three decks still rejected are rejected by the
plan builder for what the *task* is, not by anything a cheat earned:

| deck | why it is still rejected |
|---|---|
| `deck0001` | its `delta.json` predates the `deg` field, so no entry can be attributed to a degradation; `media_not_pasted` also fires on its own ground truth, so `gt` = 0.000 and `half_restore` = 0.000 follow from that |
| `deck0004` | `d5` recolours runs whose ground truth states no colour at all — six unsatisfiable components dropped, leaving a degradation nobody scores |
| `deck0009` | two `set_font` components sit at floor 0.55 / 0.65 — a task to send back to `recipe`, never a tolerance to widen |

Every applicable attack was built on every deck.  **No unproven gates.**

| attack | expect | 01 | 02 | 03 | 04 | 05 | 06 | 07 | 08 | 09 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `noop` | = 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| `gt` | = 1.000 | **0.000** | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| `gt_roundtrip` | = 1.000 | **0.000** | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| `screenshot_paste` | ≤ 0.05 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| `clone_spam` | ≤ 0.05 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| `native_to_picture` | ≤ 0.05 | n/a | 0.000 | n/a | 0.000 | 0.000 | n/a | 0.000 | 0.000 | 0.000 | 0.000 |
| `page_delete` | ≤ 0.05 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| `rename_only` | ≤ 0.05 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| `orphan_media` | ≤ 0.05 | 0.000 | 0.000 | 0.000 | n/a | n/a | 0.000 | 0.000 | 0.000 | n/a | 0.000 |
| `duplicate_gt_slide` | ≤ 0.05 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| `wrong_params` | ≤ 0.30 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| `damage_untouched` | ≤ noop | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| `damage_untouched_gt` | ≤ gt | 0.000 | 0.900 | 0.900 | 0.900 | 0.900 | 0.900 | 0.900 | 0.900 | 0.900 | 0.900 |
| `half_restore` | 0.35–0.65 | **0.000** | 0.493 | 0.456 | 0.524 | 0.506 | 0.474 | 0.551 | 0.514 | 0.519 | 0.510 |

Bold = over threshold, and every bold cell is on `deck0001`, whose ground truth
scores 0.000 for reasons of its own.  Previous run, same table:

| attack | before | after |
|---|---|---|
| `wrong_params` | 0.283–0.590, over on 8 decks | **0.000 on all ten** |
| `rename_only` | 0.005–0.124, over on 3 decks | **0.000 on all ten** |
| `gt_roundtrip` | 0.700–0.850, failing on 9 decks | **1.000 on all ten that have a scoring gt** |
| `half_restore` | 0.456–0.551 | 0.456–0.551 (unchanged) |
| `gt` / `noop` / the seven zero attacks | 1.000 / 0.000 / 0.000 | unchanged |

---

## What was wrong, and where it was fixed

### 1. `wrong_params` — `present` was a third of every restoration

`_cmp_restored_shape` blended four facets, and **two of them could not be
wrong once the shape existed**: `present` (3 of 9) is 1.0 by construction, and
the picture blob — the very key the pairing was made on — comes back with the
shape, as does the SmartArt node list inside its data part.  Geometry, the only
thing "put it back" asks for, was worth a third.  A deleted shape returned
0.75 in out of place, 1.3× too large, repainted and re-worded scored **0.667**
every time.

Fixed in `comparators._cmp_restored_shape`.  **Existence is a precondition,
not a component.**  The comparator now asks two questions separately and
multiplies them:

* **what it is** — image, words, table cells, diagram nodes, chart series, fill
* **where it is** — centre (weight 2) and extent (weight 1)

Wrong in every measurable respect is 0.  Right in every respect is 1.  Partial
work still pays in both directions, which is the failure mode that matters more
than this one: three of five nodes back in the right place is 0.60, everything
back at the wrong size is 0.67, half the words back is 0.50.  Pinned by
`test_a_restoration_still_earns_partial_credit_for_partial_work` and
`test_half_the_words_back_in_the_right_place_is_worth_about_half`, with
`test_a_restoration_that_is_wrong_in_every_respect_scores_nearly_nothing` as
the negative control.

The `half_restore` row is the deck-level evidence that nothing was traded away:
identical to the digit on eight of the nine decks whose plan is accepted, with
deck0009 at 0.519 where it was 0.541.  A rubric made strict enough to fail
legitimate partial work would have shown up there first.

### 2. `rename_only` — a name is not evidence

Two changes, both in `comparators.py`, neither of them in the key list itself:

* `name:` joins `geo:` and `kind:` in `_WEAK_KEY_PREFIXES`.  A pairing made on
  a label earns no credit for a shape merely existing — an agent can type any
  name it likes, which is precisely what this attack does.
* `pair_slide_detail` **refuses a weak-key pairing whose boxes do not meet
  anywhere on the page**.  A name, a rounded size class or a shape type is a
  claim about identity that geometry can flatly contradict, and the attack's
  survivor was five inches away.  Strong keys — placeholder role, image blob,
  the words the shape holds — still pair at any distance, or a `move`
  component would have nothing to grade
  (`test_a_moved_shape_is_still_paired_by_a_strong_key`).

`inventory._keys` keeps its order: within one file's lineage a name really is
more specific than a rounded size, and that list is what the round-trip drift
reports pair on.  What a name is *worth to somebody being scored* is a
scoring question, so it is answered in the comparator, and `_keys` now says so.

### 3. `gt_roundtrip` — the noise was subtracted, not tolerated

`untouched_pages_unchanged` compared `flatten(page)` minus a short exclusion
list, which means it compared every serialisation habit the grading application
has.  Measured over every untouched page of all ten ground truths after a real
WPS open-and-save:

| hits | what changed | whose doing |
|---|---|---|
| 2457 | run properties — `a:endParaRPr` written out in full (`{"end": true, "b": "1", "sz": 3200, "font": "Calibri"}`) where the original said nothing | WPS |
| 12 | paragraph `marL` / `indent`, 381000 EMU written back as 380990 | WPS |
| 7 | a `fade` transition invented on deck0003 p5, a page that had none | WPS |
| 2 | a `lum` picture recolor dropped | WPS |

Fixed in `comparators._page_facts`, which is now a **named projection instead
of a subtraction**: a fact is compared only if some comparator scores it *and*
WPS was measured not to rewrite it.  Shapes are compared on kind, geometry,
visible text, fill, outline, whether they draw an image, table cells, diagram
nodes and chart series; pages on notes, layout, background and the build
sequence.  Run properties, paragraph indents, transitions and image *bytes* are
the application's and are not looked at here — they are still scored, on the
damaged pages, by the components that were written for them.

The inventory was **not** changed to stop recording `endParaRPr`: a solvability
probe found a real degradation hiding in one, and an inventory that cannot see
it cannot score it.  What changed is what the scope gate looks at.

Two more things came out of the same measurement:

* `input_media_preserved` charged 0.15 of the remaining loss on deck0004 and
  deck0005.  **WPS re-encodes some images on open-and-save** — four PNGs on
  deck0004, two on deck0005, one of them 161755 bytes down to 105850 — which
  contradicts the "preserves every byte" claim in the media gate's own
  docstring.  The check now counts image parts instead of digests: bytes are
  the application's, the number of pictures in the package is the agent's.
* `_page_facts` therefore records *that* a shape draws an image, not *which*
  bytes.

Negative controls, both directions:
`test_what_the_application_writes_by_itself_is_not_a_scope_violation` (an added
`endParaRPr`, a rounded `marL` and an invented transition each leave the score
at exactly 1.000) and `test_a_real_edit_to_an_untouched_page_is_still_caught`
(moved, added, deleted, re-worded — each still a penalty, and still only a
penalty).  `damage_untouched_gt` scoring 0.900 rather than 1.000 on all nine
decks is the same control at deck scale.

### 4. Two attacks were not attacking

Found while chasing the last payouts, and they matter because an attack that
reports "28 values wrong across 6/6 components" while a component scores 1.00 is
claiming a gate it never fired:

* `_wrong_animation` hard-coded `presetID="1"`, and preset 1 (`appear`) is what
  a deck built with PowerPoint's default entrance already uses — all 18 effects
  on deck0002 were already `('entr', '1', '0')`.  The "wrong" value was the
  right one, and three `strip_animation` components scored 1.00.
* `_wrong_params` recoloured and resized runs for every `set_font` step, but
  the comparator only looks at the properties the operator named: a step that
  set **bold** and **underline** was untouched by a wrong colour, and
  deck0009's `b+u` component scored a full 1.00.  `_wrong_run_props` now gives
  each property the step actually changed a value different from the one that
  is there.

With the comparator fixed but the attacks still weak, `wrong_params` read
0.000–0.142 (deck0009 0.142, deck0002 0.064, deck0005 0.051, deck0010 0.039);
with both fixed it is 0.000 everywhere.  So part of what the previous report
attributed to the comparator was the battery's own blind spot.

---

## What the previous report got wrong

* **The notes corruption is already fixed.**  §3 said `wps_roundtrip`
  backspaces `len(DIRTY_MARK) + 2` times and eats two characters of every
  deck's speaker notes.  The `+ 2` is gone from `wps_roundtrip.py` (with a
  comment naming the casualty), and across all ten round trips in this run the
  notes of every slide of every deck come back **identical** — 0 differences in
  195 pages.
* **The `gt_roundtrip` loss was not entirely the untouched-page penalty.**  On
  deck0004 and deck0005 the whole 0.15 was `input_media_preserved`, i.e. WPS
  re-encoding images — a class the report did not have, and one that
  contradicts a measurement the comparator quotes as settled.
* **`endParaRPr` was not the only noise.**  It is the bulk of it (2457 of 2478
  differing keys) but not all: paragraph indent rounding and an invented slide
  transition are each enough to fail a deck on their own.
* **Some of `wrong_params` was the attack, not the comparator** — see §4 above.
* Everything else reproduced exactly, including all fourteen rows on all ten
  decks.

---

## What holds

* **`noop` = 0.000 on all ten.**  Floor normalisation works.
* **`gt` = 1.000 on the nine decks whose ground truth is scoreable**, and
  `gt_roundtrip` now equals it — REWARD.md §5's `roundtrip_identity` probe,
  passing, on a file that has really been through the grading application.
* **`half_restore` = 0.456–0.551 on all nine decks whose plan is accepted**,
  unchanged by any of this.  The split is chosen by **reward mass**, not
  component count.
* **`damage_untouched` never raises a score**, and `damage_untouched_gt` still
  costs its capped 0.10 from the ground truth: over-eagerness costs, it does
  not pay, and it is not a zero.
* **The overlay, clone, native-object, slide-order and media gates all fire**,
  each confirmed by the gate name and reason in the per-deck evidence column,
  on every deck where the attack applies.

---

## Evidence that the attacks do what they claim

Every candidate is described by a post-condition read back **out of the produced
file**, not out of the builder's intent — the `evidence` column of every table
below.  A silent no-op cannot pass itself off as a clean sweep.  Spot checks:

* `screenshot_paste`, deck0003 — `p7:full-bleed@z11 p12:full-bleed@z4 …`: the
  last child of each damaged page's `spTree` is a `p:pic` whose box is exactly
  the slide box.
* `clone_spam`, deck0002 — `23 holes filled with clones; slide shapes 234 →
  257`, and `tests/test_attacks.py::test_clone_spam_puts_a_clone_in_every_hole`
  asserts every recorded hole box is occupied in the output.
* `page_delete`, deck0003 — `slides 20 → 15`, verified by re-opening the
  package and counting `sldIdLst`, not by trusting the edit.
* `half_restore`, every deck — `N/N pages now byte-equal to the gt`.

---

## Adding an attack

One function, one declared expectation, one line in the registry:

```python
@attack("my_cheat", "what it does", AtMost(0.05),
        applies=lambda ctx: None if ctx.deletions() else "nothing was deleted")
def _my_cheat(ctx, out):
    pkg = ctx.open_input()
    ...
    return Built(out, "what the produced file actually contains", {"facts": 1})
```

`applies` returns `None` or a human sentence.  `raise Unconstructible(...)` means
"this should have been possible and was not" — a rejection, not a skip.  And an
attack that builds but does not perturb is the third failure mode, worse than
either: §4 above is two of them, found only because the comparator stopped
paying for the things they left alone.

---

## Per-deck tables

### deck0001 — 5 degradations (d1, d2, d3, d4, d5)

**The comparator rejects this task's plan outright** — every candidate below, the ground truth included, scores 0.0 through the `plan_accepted` gate.  The table is therefore produced with that one gate stood down, so the attacks are actually exercised; the deck is rejected either way.

- 23 delta entr(ies) carry no `deg` (c001…): scoring work nobody asked for
- degradation(s) with no scoreable component: ['d1', 'd2', 'd3', 'd4', 'd5'] (asking for work nobody scores)
- coherence: media_not_pasted fires on `ground_truth`, which is correct work: 1 original media part(s) pasted back
- coherence: media_not_pasted fires on `half_restore`, which is correct work: 1 original media part(s) pasted back
- coherence: media_not_pasted fires on `rebuilt_by_hand`, which is correct work: 1 original media part(s) pasted back
- coherence: media_not_pasted fires on `over_eager`, which is correct work: 1 original media part(s) pasted back
- coherence: over-eagerness alone zeroes the score; a scope violation must cost a fraction, never everything

| attack | what it does | expect | score | verdict | evidence |
|---|---|---|---|---|---|
| `noop` | the untouched broken file | = 0.000 | 0.000 | pass | byte-identical to input.pptx (3760240 bytes) |
| `gt` | the ground truth | = 1.000 | 0.000 | **FAIL** | 0.000 != 1.000 — byte-identical to source.pptx (4043728 bytes) | gate:media_not_pasted — 1 original media part(s) pasted back | paid out: c001/delete 1.00×0.04 (image=1.00 × position=1.00 · size=1.00); c002/delete 1.00×0.04 (image=1.00 × position=1.00 · size=1.00) |
| `gt_roundtrip` | the ground truth after a real WPS open-and-save | = 1.000 | 0.000 | **FAIL** | 0.000 != 1.000 — WPS re-serialised the package (4043728 -> 3808208 bytes, 103 parts differ) | gate:media_not_pasted — 1 original media part(s) pasted back | paid out: c001/delete 1.00×0.04 (image=1.00 × position=1.00 · size=1.00); c002/delete 1.00×0.04 (image=1.00 × position=1.00 · size=1.00) |
| `screenshot_paste` | render the original page and lay it over the damaged page | <= 0.050 | 0.000 | pass | 11 pages covered — p3:full-bleed@z3 p4:full-bleed@z3 p5:full-bleed@z4 p6:full-bleed@z4 p7:full-bleed@z5 p8:full-bleed@z9 | gate:no_full_page_overlay — slide 5: new picture covers 100% of the page |
| `clone_spam` | duplicate surviving shapes to fill the holes | <= 0.050 | 0.000 | pass | 13 holes filled with clones; slide shapes 79 -> 92 | gate:no_cloned_shapes — slide 3: a surplus copy of pic:6ce77d237fcf14cf fills the damaged region |
| `native_to_picture` | replace a chart / table / SmartArt with a rendered image of it | <= 0.050 | — | n/a | no chart, table or SmartArt in the damage |
| `page_delete` | delete the damaged page outright | <= 0.050 | 0.000 | pass | slides 19 -> 8, removed pages [3, 4, 5, 6, 7, 8, 11, 13] | gate:slide_count_and_order — 8 slides, expected 19 |
| `rename_only` | rename shapes to the ground truth's names, change nothing else | <= 0.050 | 0.000 | pass | 40 shapes renamed, geometry untouched — p3: 'Titre 1'->'Image 4', p3: 'Espace réservé du contenu 2'->'Titre 1', p3: 'Picture 4'->'Espace réservé du contenu 2' |
| `orphan_media` | copy the withheld media blobs into the package without placing them | <= 0.050 | 0.000 | pass | 1 media parts restored as orphans (image9.emf); slide shapes unchanged (79) | gate:media_not_pasted — 1 original media part(s) pasted back |
| `duplicate_gt_slide` | append an untouched copy of the original page, leave the broken one | <= 0.050 | 0.000 | pass | 30 pages (was 19); originals of [3, 4, 5, 6, 7, 8] appended, broken pages kept | gate:slide_count_and_order — 30 slides, expected 19 |
| `wrong_params` | restore each damaged thing with a wrong value | <= 0.300 | 0.000 | pass | 23 values wrong across 5/5 components | gate:media_not_pasted — 1 original media part(s) pasted back |
| `damage_untouched` | break a page nobody was asked to touch | <= noop | 0.000 | pass | input.pptx: page 1 (never in the task) 8 -> 4 shapes, survivors moved 1in |
| `damage_untouched_gt` | the ground truth with an unrelated page broken | <= gt | 0.000 | pass | source.pptx: page 1 (never in the task) 8 -> 4 shapes, survivors moved 1in | gate:media_not_pasted — 1 original media part(s) pasted back |
| `half_restore` | restore half the reward mass | 0.35..0.65 | 0.000 | **FAIL** | 0.000 outside 0.35..0.65 — restored ['d1', 'd2'] = 0.40 of the reward mass on pages [4, 8]; 2/2 pages now byte-equal to the gt | gate:media_not_pasted — 1 original media part(s) pasted back | paid out: c002/delete 1.00×0.04 (image=1.00 × position=1.00 · size=1.00); c007/delete 1.00×0.04 (position=1.00 · size=1.00) |

**verdict: REJECT** — the comparator rejects the plan: 23 delta entr(ies) carry no `deg` (c001…): scoring work nobody asked for; the comparator rejects the plan: degradation(s) with no scoreable component: ['d1', 'd2', 'd3', 'd4', 'd5'] (asking for work nobody scores); the comparator rejects the plan: coherence: media_not_pasted fires on `ground_truth`, which is correct work: 1 original media part(s) pasted back; the comparator rejects the plan: coherence: media_not_pasted fires on `half_restore`, which is correct work: 1 original media part(s) pasted back; the comparator rejects the plan: coherence: media_not_pasted fires on `rebuilt_by_hand`, which is correct work: 1 original media part(s) pasted back; the comparator rejects the plan: coherence: media_not_pasted fires on `over_eager`, which is correct work: 1 original media part(s) pasted back; the comparator rejects the plan: coherence: over-eagerness alone zeroes the score; a scope violation must cost a fraction, never everything; gt: 0.000 != 1.000; gt_roundtrip: 0.000 != 1.000; half_restore: 0.000 outside 0.35..0.65

### deck0002 — 6 degradations (d1, d2, d3, d4, d5, d6)

| attack | what it does | expect | score | verdict | evidence |
|---|---|---|---|---|---|
| `noop` | the untouched broken file | = 0.000 | 0.000 | pass | byte-identical to input.pptx (9401506 bytes) |
| `gt` | the ground truth | = 1.000 | 1.000 | pass | byte-identical to source.pptx (17548799 bytes) |
| `gt_roundtrip` | the ground truth after a real WPS open-and-save | = 1.000 | 1.000 | pass | WPS re-serialised the package (17548799 -> 10287682 bytes, 147 parts differ) |
| `screenshot_paste` | render the original page and lay it over the damaged page | <= 0.050 | 0.000 | pass | 6 pages covered — p6:full-bleed@z32 p7:full-bleed@z42 p11:full-bleed@z4 p12:full-bleed@z4 p16:full-bleed@z10 p19:full-bleed@z8 | gate:no_full_page_overlay — slide 7: new picture covers 100% of the page |
| `clone_spam` | duplicate surviving shapes to fill the holes | <= 0.050 | 0.000 | pass | 23 holes filled with clones; slide shapes 234 -> 257 | gate:no_cloned_shapes — slide 6: a surplus copy of pic:10943bc9a46b4981 fills the damaged region |
| `native_to_picture` | replace a chart / table / SmartArt with a rendered image of it | <= 0.050 | 0.000 | pass | 3 native objects replaced by pictures — p11:table(frame removed) p12:table(frame removed) p19:smartart | gate:native_objects_preserved — slide 11: the table is now a picture of one |
| `page_delete` | delete the damaged page outright | <= 0.050 | 0.000 | pass | slides 22 -> 16, removed pages [6, 7, 11, 12, 16, 19] | gate:slide_count_and_order — 16 slides, expected 22 |
| `rename_only` | rename shapes to the ground truth's names, change nothing else | <= 0.050 | 0.000 | pass | 100 shapes renamed, geometry untouched — p6: 'Date Placeholder 3'->'Oval 27', p6: 'Slide Number Placeholder 4'->'Picture 4', p6: 'Title 1'->'Picture 6' |
| `orphan_media` | copy the withheld media blobs into the package without placing them | <= 0.050 | 0.000 | pass | 4 media parts restored as orphans (image15.tiff, image14.jpeg, image11.jpeg, image10.jpeg); slide shapes unchanged (234) |
| `duplicate_gt_slide` | append an untouched copy of the original page, leave the broken one | <= 0.050 | 0.000 | pass | 28 pages (was 22); originals of [6, 7, 11, 12, 16, 19] appended, broken pages kept | gate:slide_count_and_order — 28 slides, expected 22 |
| `wrong_params` | restore each damaged thing with a wrong value | <= 0.300 | 0.000 | pass | 28 values wrong across 6/6 components |
| `damage_untouched` | break a page nobody was asked to touch | <= noop | 0.000 | pass | input.pptx: page 5 (never in the task) 22 -> 11 shapes, survivors moved 1in |
| `damage_untouched_gt` | the ground truth with an unrelated page broken | <= gt | 0.900 | pass | source.pptx: page 5 (never in the task) 22 -> 11 shapes, survivors moved 1in |
| `half_restore` | restore half the reward mass | 0.35..0.65 | 0.493 | pass | restored ['d1', 'd6'] = 0.49 of the reward mass on pages [6, 19]; 2/2 pages now byte-equal to the gt |

**verdict: survives the battery**

### deck0003 — 5 degradations (d1, d2, d3, d4, d5)

| attack | what it does | expect | score | verdict | evidence |
|---|---|---|---|---|---|
| `noop` | the untouched broken file | = 0.000 | 0.000 | pass | byte-identical to input.pptx (11550846 bytes) |
| `gt` | the ground truth | = 1.000 | 1.000 | pass | byte-identical to source.pptx (12787880 bytes) |
| `gt_roundtrip` | the ground truth after a real WPS open-and-save | = 1.000 | 1.000 | pass | WPS re-serialised the package (12787880 -> 12197753 bytes, 165 parts differ) |
| `screenshot_paste` | render the original page and lay it over the damaged page | <= 0.050 | 0.000 | pass | 5 pages covered — p7:full-bleed@z11 p12:full-bleed@z4 p17:full-bleed@z6 p18:full-bleed@z5 p19:full-bleed@z12 | gate:no_full_page_overlay — slide 12: new picture covers 100% of the page |
| `clone_spam` | duplicate surviving shapes to fill the holes | <= 0.050 | 0.000 | pass | 9 holes filled with clones; slide shapes 123 -> 132 | gate:no_cloned_shapes — slide 7: a surplus copy of txt:2c1d1b75a07d fills the damaged region |
| `native_to_picture` | replace a chart / table / SmartArt with a rendered image of it | <= 0.050 | — | n/a | no chart, table or SmartArt in the damage |
| `page_delete` | delete the damaged page outright | <= 0.050 | 0.000 | pass | slides 20 -> 15, removed pages [7, 12, 17, 18, 19] | gate:slide_count_and_order — 15 slides, expected 20 |
| `rename_only` | rename shapes to the ground truth's names, change nothing else | <= 0.050 | 0.000 | pass | 38 shapes renamed, geometry untouched — p7: 'TextBox 2'->'Picture 2', p7: 'TextBox 13'->'Picture 6', p7: 'TextBox 15'->'Picture 3' |
| `orphan_media` | copy the withheld media blobs into the package without placing them | <= 0.050 | 0.000 | pass | 4 media parts restored as orphans (image28.jpeg, image29.png, image34.png, image49.png); slide shapes unchanged (123) |
| `duplicate_gt_slide` | append an untouched copy of the original page, leave the broken one | <= 0.050 | 0.000 | pass | 25 pages (was 20); originals of [7, 12, 17, 18, 19] appended, broken pages kept | gate:slide_count_and_order — 25 slides, expected 20 |
| `wrong_params` | restore each damaged thing with a wrong value | <= 0.300 | 0.000 | pass | 9 values wrong across 5/5 components |
| `damage_untouched` | break a page nobody was asked to touch | <= noop | 0.000 | pass | input.pptx: page 8 (never in the task) 12 -> 6 shapes, survivors moved 1in |
| `damage_untouched_gt` | the ground truth with an unrelated page broken | <= gt | 0.900 | pass | source.pptx: page 8 (never in the task) 12 -> 6 shapes, survivors moved 1in |
| `half_restore` | restore half the reward mass | 0.35..0.65 | 0.456 | pass | restored ['d2', 'd4', 'd5'] = 0.46 of the reward mass on pages [12, 18, 19]; 3/3 pages now byte-equal to the gt |

**verdict: survives the battery**

### deck0004 — 5 degradations (d1, d2, d3, d4, d5)

**The comparator rejects this task's plan outright** — every candidate below, the ground truth included, scores 0.0 through the `plan_accepted` gate.  The table is therefore produced with that one gate stood down, so the attacks are actually exercised; the deck is rejected either way.

- degradation(s) with no scoreable component: ['d5'] (asking for work nobody scores)

| attack | what it does | expect | score | verdict | evidence |
|---|---|---|---|---|---|
| `noop` | the untouched broken file | = 0.000 | 0.000 | pass | byte-identical to input.pptx (5570071 bytes) |
| `gt` | the ground truth | = 1.000 | 1.000 | pass | byte-identical to source.pptx (6029746 bytes) |
| `gt_roundtrip` | the ground truth after a real WPS open-and-save | = 1.000 | 1.000 | pass | WPS re-serialised the package (6029746 -> 5656321 bytes, 121 parts differ) |
| `screenshot_paste` | render the original page and lay it over the damaged page | <= 0.050 | 0.000 | pass | 7 pages covered — p7:full-bleed@z10 p9:full-bleed@z5 p10:full-bleed@z14 p12:full-bleed@z2 p14:full-bleed@z4 p16:full-bleed@z4 | gate:no_full_page_overlay — slide 7: new picture covers 100% of the page |
| `clone_spam` | duplicate surviving shapes to fill the holes | <= 0.050 | 0.000 | pass | 5 holes filled with clones; slide shapes 119 -> 124 | gate:no_cloned_shapes — slide 10: a surplus copy of pic:bd69f71b14566feb fills the damaged region |
| `native_to_picture` | replace a chart / table / SmartArt with a rendered image of it | <= 0.050 | 0.000 | pass | 1 native objects replaced by pictures — p9:smartart_drop_nodes(frame removed) | gate:native_objects_preserved — slide 9: the smartart is now a picture of one |
| `page_delete` | delete the damaged page outright | <= 0.050 | 0.000 | pass | slides 20 -> 13, removed pages [7, 9, 10, 12, 14, 16, 18] | gate:slide_count_and_order — 13 slides, expected 20 |
| `rename_only` | rename shapes to the ground truth's names, change nothing else | <= 0.050 | 0.000 | pass | 29 shapes renamed, geometry untouched — p7: 'Title 1'->'Picture 4', p7: 'Slide Number Placeholder 3'->'Picture 4', p7: 'Picture 4'->'Picture 5' |
| `orphan_media` | copy the withheld media blobs into the package without placing them | <= 0.050 | — | n/a | the broken deck already holds every media part — there is nothing to smuggle back |
| `duplicate_gt_slide` | append an untouched copy of the original page, leave the broken one | <= 0.050 | 0.000 | pass | 27 pages (was 20); originals of [7, 9, 10, 12, 14, 16] appended, broken pages kept | gate:slide_count_and_order — 27 slides, expected 20 |
| `wrong_params` | restore each damaged thing with a wrong value | <= 0.300 | 0.000 | pass | 28 values wrong across 5/5 components |
| `damage_untouched` | break a page nobody was asked to touch | <= noop | 0.000 | pass | input.pptx: page 6 (never in the task) 10 -> 5 shapes, survivors moved 1in |
| `damage_untouched_gt` | the ground truth with an unrelated page broken | <= gt | 0.900 | pass | source.pptx: page 6 (never in the task) 10 -> 5 shapes, survivors moved 1in |
| `half_restore` | restore half the reward mass | 0.35..0.65 | 0.524 | pass | restored ['d1', 'd2'] = 0.52 of the reward mass on pages [7, 12]; 2/2 pages now byte-equal to the gt |

**verdict: REJECT** — the comparator rejects the plan: degradation(s) with no scoreable component: ['d5'] (asking for work nobody scores)

### deck0005 — 6 degradations (d1, d2, d3, d4, d5, d6)

| attack | what it does | expect | score | verdict | evidence |
|---|---|---|---|---|---|
| `noop` | the untouched broken file | = 0.000 | 0.000 | pass | byte-identical to input.pptx (7258615 bytes) |
| `gt` | the ground truth | = 1.000 | 1.000 | pass | byte-identical to source.pptx (7836346 bytes) |
| `gt_roundtrip` | the ground truth after a real WPS open-and-save | = 1.000 | 1.000 | pass | WPS re-serialised the package (7836346 -> 7288095 bytes, 47 parts differ) |
| `screenshot_paste` | render the original page and lay it over the damaged page | <= 0.050 | 0.000 | pass | 9 pages covered — p4:full-bleed@z10 p6:full-bleed@z3 p7:full-bleed@z3 p8:full-bleed@z4 p10:full-bleed@z9 p11:full-bleed@z2 | gate:no_full_page_overlay — slide 4: new picture covers 100% of the page |
| `clone_spam` | duplicate surviving shapes to fill the holes | <= 0.050 | 0.000 | pass | 23 holes filled with clones; slide shapes 113 -> 136 | gate:no_cloned_shapes — slide 6: a surplus copy of pic:9333e601812623fa fills the damaged region |
| `native_to_picture` | replace a chart / table / SmartArt with a rendered image of it | <= 0.050 | 0.000 | pass | 1 native objects replaced by pictures — p6:table |
| `page_delete` | delete the damaged page outright | <= 0.050 | 0.000 | pass | slides 22 -> 13, removed pages [4, 6, 7, 8, 10, 11, 12, 16] | gate:slide_count_and_order — 13 slides, expected 22 |
| `rename_only` | rename shapes to the ground truth's names, change nothing else | <= 0.050 | 0.000 | pass | 35 shapes renamed, geometry untouched — p4: 'TextBox 4'->'Title 1', p4: 'TextBox 5'->'TextBox 4', p4: 'TextBox 6'->'TextBox 5' |
| `orphan_media` | copy the withheld media blobs into the package without placing them | <= 0.050 | — | n/a | the broken deck already holds every media part — there is nothing to smuggle back |
| `duplicate_gt_slide` | append an untouched copy of the original page, leave the broken one | <= 0.050 | 0.000 | pass | 31 pages (was 22); originals of [4, 6, 7, 8, 10, 11] appended, broken pages kept | gate:slide_count_and_order — 31 slides, expected 22 |
| `wrong_params` | restore each damaged thing with a wrong value | <= 0.300 | 0.000 | pass | 34 values wrong across 6/6 components |
| `damage_untouched` | break a page nobody was asked to touch | <= noop | 0.000 | pass | input.pptx: page 22 (never in the task) 16 -> 8 shapes, survivors moved 1in |
| `damage_untouched_gt` | the ground truth with an unrelated page broken | <= gt | 0.900 | pass | source.pptx: page 22 (never in the task) 16 -> 8 shapes, survivors moved 1in |
| `half_restore` | restore half the reward mass | 0.35..0.65 | 0.506 | pass | restored ['d1', 'd3', 'd5', 'd6'] = 0.51 of the reward mass on pages [4, 8, 10, 11, 12, 16, 19]; 7/7 pages now byte-equal to the gt |

**verdict: survives the battery**

### deck0006 — 5 degradations (d1, d2, d3, d4, d5)

| attack | what it does | expect | score | verdict | evidence |
|---|---|---|---|---|---|
| `noop` | the untouched broken file | = 0.000 | 0.000 | pass | byte-identical to input.pptx (2990947 bytes) |
| `gt` | the ground truth | = 1.000 | 1.000 | pass | byte-identical to source.pptx (3082394 bytes) |
| `gt_roundtrip` | the ground truth after a real WPS open-and-save | = 1.000 | 1.000 | pass | WPS re-serialised the package (3082394 -> 3087696 bytes, 88 parts differ) |
| `screenshot_paste` | render the original page and lay it over the damaged page | <= 0.050 | 0.000 | pass | 5 pages covered — p3:full-bleed@z6 p6:full-bleed@z1 p8:full-bleed@z49 p11:full-bleed@z60 p13:full-bleed@z66 | gate:no_full_page_overlay — slide 8: new picture covers 100% of the page |
| `clone_spam` | duplicate surviving shapes to fill the holes | <= 0.050 | 0.000 | pass | 49 holes filled with clones; slide shapes 638 -> 687 | gate:no_cloned_shapes — slide 3: a surplus copy of ole fills the damaged region |
| `native_to_picture` | replace a chart / table / SmartArt with a rendered image of it | <= 0.050 | — | n/a | no chart, table or SmartArt in the damage |
| `page_delete` | delete the damaged page outright | <= 0.050 | 0.000 | pass | slides 15 -> 10, removed pages [3, 6, 8, 11, 13] | gate:slide_count_and_order — 10 slides, expected 15 |
| `rename_only` | rename shapes to the ground truth's names, change nothing else | <= 0.050 | 0.000 | pass | 182 shapes renamed, geometry untouched — p3: '개체 1'->'TextBox 13', p3: '개체 2'->'TextBox 14', p3: 'TextBox 28'->'TextBox 15' |
| `orphan_media` | copy the withheld media blobs into the package without placing them | <= 0.050 | 0.000 | pass | 5 media parts restored as orphans (image16.png, image9.png, image10.png, image11.png); slide shapes unchanged (638) |
| `duplicate_gt_slide` | append an untouched copy of the original page, leave the broken one | <= 0.050 | 0.000 | pass | 20 pages (was 15); originals of [3, 6, 8, 11, 13] appended, broken pages kept | gate:slide_count_and_order — 20 slides, expected 15 |
| `wrong_params` | restore each damaged thing with a wrong value | <= 0.300 | 0.000 | pass | 105 values wrong across 5/5 components |
| `damage_untouched` | break a page nobody was asked to touch | <= noop | 0.000 | pass | input.pptx: page 9 (never in the task) 77 -> 38 shapes, survivors moved 1in |
| `damage_untouched_gt` | the ground truth with an unrelated page broken | <= gt | 0.900 | pass | source.pptx: page 9 (never in the task) 77 -> 38 shapes, survivors moved 1in |
| `half_restore` | restore half the reward mass | 0.35..0.65 | 0.474 | pass | restored ['d1', 'd3'] = 0.47 of the reward mass on pages [6, 11]; 2/2 pages now byte-equal to the gt |

**verdict: survives the battery**

### deck0007 — 4 degradations (d1, d2, d3, d4)

| attack | what it does | expect | score | verdict | evidence |
|---|---|---|---|---|---|
| `noop` | the untouched broken file | = 0.000 | 0.000 | pass | byte-identical to input.pptx (1793153 bytes) |
| `gt` | the ground truth | = 1.000 | 1.000 | pass | byte-identical to source.pptx (1921742 bytes) |
| `gt_roundtrip` | the ground truth after a real WPS open-and-save | = 1.000 | 1.000 | pass | WPS re-serialised the package (1921742 -> 2000295 bytes, 138 parts differ) |
| `screenshot_paste` | render the original page and lay it over the damaged page | <= 0.050 | 0.000 | pass | 5 pages covered — p2:full-bleed@z4 p4:full-bleed@z4 p12:full-bleed@z5 p16:full-bleed@z5 p19:full-bleed@z6 | gate:no_full_page_overlay — slide 4: new picture covers 100% of the page |
| `clone_spam` | duplicate surviving shapes to fill the holes | <= 0.050 | 0.000 | pass | 3 holes filled with clones; slide shapes 115 -> 118 | gate:no_cloned_shapes — slide 2: a surplus copy of pic:c2784e78e7cb4da1 fills the damaged region |
| `native_to_picture` | replace a chart / table / SmartArt with a rendered image of it | <= 0.050 | 0.000 | pass | 4 native objects replaced by pictures — p4:smartart p12:smartart p16:smartart p19:smartart_drop_nodes(frame removed) | gate:native_objects_preserved — slide 19: the smartart is now a picture of one |
| `page_delete` | delete the damaged page outright | <= 0.050 | 0.000 | pass | slides 22 -> 17, removed pages [2, 4, 12, 16, 19] | gate:slide_count_and_order — 17 slides, expected 22 |
| `rename_only` | rename shapes to the ground truth's names, change nothing else | <= 0.050 | 0.000 | pass | 18 shapes renamed, geometry untouched — p2: 'Date Placeholder 3'->'Picture 8', p2: 'Footer Placeholder 4'->'Date Placeholder 3', p2: 'Slide Number Placeholder 5'->'Footer Placeholder 4' |
| `orphan_media` | copy the withheld media blobs into the package without placing them | <= 0.050 | 0.000 | pass | 1 media parts restored as orphans (image6.png); slide shapes unchanged (115) |
| `duplicate_gt_slide` | append an untouched copy of the original page, leave the broken one | <= 0.050 | 0.000 | pass | 27 pages (was 22); originals of [2, 4, 12, 16, 19] appended, broken pages kept | gate:slide_count_and_order — 27 slides, expected 22 |
| `wrong_params` | restore each damaged thing with a wrong value | <= 0.300 | 0.000 | pass | 5 values wrong across 4/4 components |
| `damage_untouched` | break a page nobody was asked to touch | <= noop | 0.000 | pass | input.pptx: page 6 (never in the task) 6 -> 3 shapes, survivors moved 1in |
| `damage_untouched_gt` | the ground truth with an unrelated page broken | <= gt | 0.900 | pass | source.pptx: page 6 (never in the task) 6 -> 3 shapes, survivors moved 1in |
| `half_restore` | restore half the reward mass | 0.35..0.65 | 0.551 | pass | restored ['d1', 'd2'] = 0.55 of the reward mass on pages [2, 4]; 2/2 pages now byte-equal to the gt |

**verdict: survives the battery**

### deck0008 — 5 degradations (d1, d2, d3, d4, d5)

| attack | what it does | expect | score | verdict | evidence |
|---|---|---|---|---|---|
| `noop` | the untouched broken file | = 0.000 | 0.000 | pass | byte-identical to input.pptx (9496504 bytes) |
| `gt` | the ground truth | = 1.000 | 1.000 | pass | byte-identical to source.pptx (10553740 bytes) |
| `gt_roundtrip` | the ground truth after a real WPS open-and-save | = 1.000 | 1.000 | pass | WPS re-serialised the package (10553740 -> 10468061 bytes, 41 parts differ) |
| `screenshot_paste` | render the original page and lay it over the damaged page | <= 0.050 | 0.000 | pass | 6 pages covered — p5:full-bleed@z6 p7:full-bleed@z4 p8:full-bleed@z3 p10:full-bleed@z4 p11:full-bleed@z5 p14:full-bleed@z4 | gate:no_full_page_overlay — slide 7: new picture covers 100% of the page |
| `clone_spam` | duplicate surviving shapes to fill the holes | <= 0.050 | 0.000 | pass | 7 holes filled with clones; slide shapes 86 -> 93 | gate:no_cloned_shapes — slide 7: a surplus copy of pic:e55d09db3f612d7b fills the damaged region |
| `native_to_picture` | replace a chart / table / SmartArt with a rendered image of it | <= 0.050 | 0.000 | pass | 2 native objects replaced by pictures — p8:smartart_drop_nodes(frame removed) p11:table | gate:native_objects_preserved — slide 8: the smartart is now a picture of one |
| `page_delete` | delete the damaged page outright | <= 0.050 | 0.000 | pass | slides 15 -> 9, removed pages [5, 7, 8, 10, 11, 14] | gate:slide_count_and_order — 9 slides, expected 15 |
| `rename_only` | rename shapes to the ground truth's names, change nothing else | <= 0.050 | 0.000 | pass | 23 shapes renamed, geometry untouched — p5: 'Title 1'->'Picture 4', p5: 'Content Placeholder 2'->'Title 1', p5: 'Rectangle 17'->'Content Placeholder 2' |
| `orphan_media` | copy the withheld media blobs into the package without placing them | <= 0.050 | 0.000 | pass | 1 media parts restored as orphans (image4.png); slide shapes unchanged (86) |
| `duplicate_gt_slide` | append an untouched copy of the original page, leave the broken one | <= 0.050 | 0.000 | pass | 21 pages (was 15); originals of [5, 7, 8, 10, 11, 14] appended, broken pages kept | gate:slide_count_and_order — 21 slides, expected 15 |
| `wrong_params` | restore each damaged thing with a wrong value | <= 0.300 | 0.000 | pass | 8 values wrong across 5/5 components |
| `damage_untouched` | break a page nobody was asked to touch | <= noop | 0.000 | pass | input.pptx: page 15 (never in the task) 14 -> 7 shapes, survivors moved 1in |
| `damage_untouched_gt` | the ground truth with an unrelated page broken | <= gt | 0.900 | pass | source.pptx: page 15 (never in the task) 14 -> 7 shapes, survivors moved 1in |
| `half_restore` | restore half the reward mass | 0.35..0.65 | 0.514 | pass | restored ['d2', 'd3'] = 0.51 of the reward mass on pages [7, 10, 11]; 3/3 pages now byte-equal to the gt |

**verdict: survives the battery**

### deck0009 — 4 degradations (d1, d2, d3, d4)

**The comparator rejects this task's plan outright** — every candidate below, the ground truth included, scores 0.0 through the `plan_accepted` gate.  The table is therefore produced with that one gate stood down, so the attacks are actually exercised; the deck is rejected either way.

- component floor above 0.15 — send the task back to `recipe`, do not widen a tolerance: c014/set_font floor=0.55, c015/set_font floor=0.65

| attack | what it does | expect | score | verdict | evidence |
|---|---|---|---|---|---|
| `noop` | the untouched broken file | = 0.000 | 0.000 | pass | byte-identical to input.pptx (3090193 bytes) |
| `gt` | the ground truth | = 1.000 | 1.000 | pass | byte-identical to source.pptx (3106729 bytes) |
| `gt_roundtrip` | the ground truth after a real WPS open-and-save | = 1.000 | 1.000 | pass | WPS re-serialised the package (3106729 -> 3178291 bytes, 101 parts differ) |
| `screenshot_paste` | render the original page and lay it over the damaged page | <= 0.050 | 0.000 | pass | 5 pages covered — p4:full-bleed@z24 p7:full-bleed@z7 p8:full-bleed@z7 p10:full-bleed@z10 p11:full-bleed@z14 | gate:no_full_page_overlay — slide 4: new picture covers 100% of the page |
| `clone_spam` | duplicate surviving shapes to fill the holes | <= 0.050 | 0.000 | pass | 9 holes filled with clones; slide shapes 141 -> 150 | gate:no_cloned_shapes — slide 10: a surplus copy of ole fills the damaged region |
| `native_to_picture` | replace a chart / table / SmartArt with a rendered image of it | <= 0.050 | 0.000 | pass | 3 native objects replaced by pictures — p7:table(frame removed) p8:table(frame removed) p10:table | gate:native_objects_preserved — slide 7: the table is now a picture of one |
| `page_delete` | delete the damaged page outright | <= 0.050 | 0.000 | pass | slides 16 -> 11, removed pages [4, 7, 8, 10, 11] | gate:slide_count_and_order — 11 slides, expected 16 |
| `rename_only` | rename shapes to the ground truth's names, change nothing else | <= 0.050 | 0.000 | pass | 62 shapes renamed, geometry untouched — p4: 'Slide Number Placeholder 1'->'Rectangle 11', p4: 'Right Arrow 31'->'Rectangle 15', p4: 'Freeform 32'->'Rectangle 17' |
| `orphan_media` | copy the withheld media blobs into the package without placing them | <= 0.050 | — | n/a | the broken deck already holds every media part — there is nothing to smuggle back |
| `duplicate_gt_slide` | append an untouched copy of the original page, leave the broken one | <= 0.050 | 0.000 | pass | 21 pages (was 16); originals of [4, 7, 8, 10, 11] appended, broken pages kept | gate:slide_count_and_order — 21 slides, expected 16 |
| `wrong_params` | restore each damaged thing with a wrong value | <= 0.300 | 0.000 | pass | 24 values wrong across 4/4 components |
| `damage_untouched` | break a page nobody was asked to touch | <= noop | 0.000 | pass | input.pptx: page 13 (never in the task) 16 -> 8 shapes, survivors moved 1in |
| `damage_untouched_gt` | the ground truth with an unrelated page broken | <= gt | 0.900 | pass | source.pptx: page 13 (never in the task) 16 -> 8 shapes, survivors moved 1in |
| `half_restore` | restore half the reward mass | 0.35..0.65 | 0.519 | pass | restored ['d2', 'd3'] = 0.54 of the reward mass on pages [7, 8, 10]; 3/3 pages now byte-equal to the gt |

**verdict: REJECT** — the comparator rejects the plan: component floor above 0.15 — send the task back to `recipe`, do not widen a tolerance: c014/set_font floor=0.55, c015/set_font floor=0.65

### deck0010 — 5 degradations (d1, d2, d3, d4, d5)

| attack | what it does | expect | score | verdict | evidence |
|---|---|---|---|---|---|
| `noop` | the untouched broken file | = 0.000 | 0.000 | pass | byte-identical to input.pptx (6929726 bytes) |
| `gt` | the ground truth | = 1.000 | 1.000 | pass | byte-identical to source.pptx (7266724 bytes) |
| `gt_roundtrip` | the ground truth after a real WPS open-and-save | = 1.000 | 1.000 | pass | WPS re-serialised the package (7266724 -> 7120645 bytes, 83 parts differ) |
| `screenshot_paste` | render the original page and lay it over the damaged page | <= 0.050 | 0.000 | pass | 5 pages covered — p15:full-bleed@z1 p16:full-bleed@z3 p17:full-bleed@z2 p20:full-bleed@z2 p22:full-bleed@z2 | gate:no_full_page_overlay — slide 15: new picture covers 100% of the page |
| `clone_spam` | duplicate surviving shapes to fill the holes | <= 0.050 | 0.000 | pass | 5 holes filled with clones; slide shapes 71 -> 76 | gate:no_full_page_overlay — slide 17: new autoshape covers 100% of the page |
| `native_to_picture` | replace a chart / table / SmartArt with a rendered image of it | <= 0.050 | 0.000 | pass | 2 native objects replaced by pictures — p15:smartart p16:smartart |
| `page_delete` | delete the damaged page outright | <= 0.050 | 0.000 | pass | slides 24 -> 19, removed pages [15, 16, 17, 20, 22] | gate:slide_count_and_order — 19 slides, expected 24 |
| `rename_only` | rename shapes to the ground truth's names, change nothing else | <= 0.050 | 0.000 | pass | 8 shapes renamed, geometry untouched — p15: 'Título 3'->'Marcador de contenido 4', p16: 'Rectangle 9'->'Marcador de contenido 2', p16: 'Straight Connector 11'->'Rectangle 9' |
| `orphan_media` | copy the withheld media blobs into the package without placing them | <= 0.050 | 0.000 | pass | 1 media parts restored as orphans (image14.jpeg); slide shapes unchanged (71) |
| `duplicate_gt_slide` | append an untouched copy of the original page, leave the broken one | <= 0.050 | 0.000 | pass | 29 pages (was 24); originals of [15, 16, 17, 20, 22] appended, broken pages kept | gate:slide_count_and_order — 29 slides, expected 24 |
| `wrong_params` | restore each damaged thing with a wrong value | <= 0.300 | 0.000 | pass | 7 values wrong across 5/5 components |
| `damage_untouched` | break a page nobody was asked to touch | <= noop | 0.000 | pass | input.pptx: page 2 (never in the task) 7 -> 3 shapes, survivors moved 1in |
| `damage_untouched_gt` | the ground truth with an unrelated page broken | <= gt | 0.900 | pass | source.pptx: page 2 (never in the task) 7 -> 3 shapes, survivors moved 1in |
| `half_restore` | restore half the reward mass | 0.35..0.65 | 0.510 | pass | restored ['d1', 'd2'] = 0.51 of the reward mass on pages [15, 16]; 2/2 pages now byte-equal to the gt |

**verdict: survives the battery**
