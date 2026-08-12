# Audit: the failures and the near-failures of the ten-deck run

Repo at `1a32068`. Read-only audit; nothing in `pptxgym/`, `work/` or `.claude/` was
modified. 8 decks packaged, deck0001 and deck0008 parked.

The subject is the pipeline, not the two decks. The short version:

> **Neither parked deck is a bad deck.** Both are correct tasks that the pipeline
> cannot express, cannot fix, or cannot score. Of the six repairs spent across
> them, **one produced no work at all** (an infra abort that still consumed a
> third of the budget), **two carried a primary order the repairer had no legal
> route to execute**, and **zero were spent on the defect that actually parked
> deck0001** — that order arrived after the budget was already gone.

---

## 0. Three corrections to the brief

1. **deck0001 did not spend three repairs on the thumbnail.** Its three repairs
   were spent on three genuinely different, genuinely fixable defects, and all
   three were fixed. The thumbnail work order was the *fourth* order; it arrived
   at a deck with `repairs_done() == 3` and was never handed to a repairer. The
   deck was parked on first contact with the defect, not after three failed
   attempts at it.
2. **deck0008's last order came from `reconciled`, not from `scored` directly.**
   `scored` rejected the plan at 07:41:19; reconcile then re-ran (07:41→07:48)
   and translated that rejection into the `proposed`/`materialise` work order
   quoted in the brief. That re-run is itself a bug — see §4.6.
3. **`consistency` never ran on the two parked decks.** They have no
   `consistency.json` at all. The run is 6 `ok` + 2 `suspect` out of 8 packaged,
   not 8 of 10.

---

# 1. The two parked decks

## 1.1 deck0001 — parked correctly, for a defect nobody could have fixed at any layer the repairer owns

### The trace

| # | log | at | ordered by | orders | outcome |
|---|---|---|---|---|---|
| R1 | `repair-01.jsonl` | 08-04 04:26 | `solvability.json:ambiguous` | 2, both `materialise` | **both done** (pinned the slide-8 LREE/MREE/HREE boundaries into the instruction; removed 5 leaked logo copies from `assets/`) |
| R2 | `repair-02.jsonl` | 08-04 17:06 | `task.json:needs_rework` | 3 (2 + a fallback) | **done, but only by editing `pptxgym/assets.py`** — see below |
| R3 | `repair-03.jsonl` | 08-05 06:48 | `task.json:needs_rework` | 2 | **done, with two documented deviations**, both because the literal order needed a change inside `pptxgym/assets.py` |
| — | *(never ran)* | 08-05 07:08 | `solvability.json:leaked` | 1, `materialise` | **PARKED**, `repairs_done()==3` |

All three repairs succeeded — 45, 61 and 25 turns, `end_turn`, $3.29 / $4.40 / $1.56.
Verdict history confirms real movement: `reconciled` went
`ready → needs_rework → ready → needs_rework → ready → needs_rework`, i.e. each
repair cleared its complaint and a *new*, different complaint was found next round.

### Was the park right?

**Yes — and that is the problem.** The final work order
(`solvability.json`, verdict `leaked`) is:

> `materialise` — Strip or regenerate `docProps/thumbnail.jpeg` when building
> `input.pptx` … It is a generic packaging bug that will leak slide 1 on every
> deck built this way.

The probe is right about the leak (verified in §2) and right that it is generic.
But the order is filed against a stage that cannot execute it, and **there is no
stage the repairer is allowed to touch that can**:

- `assets.materialise` writes only into `assets/`. It never opens `input.pptx`.
  (`pptxgym/assets.py`; `pipeline.materialise` at `pipeline.py:1129`.)
- `recipe.json` cannot express it: `degrade_exec`'s operator registry is
  shape-scoped. There is no operator that touches a package part outside
  `ppt/`, and no deck-level operator for `docProps/` at all
  (`degrade_exec.py:1128-1180` — ops run per-slide, then `prs.save(out_path)`).
- The place it belongs is `degrade_exec` itself, immediately after
  `prs.save(out_path)` (`degrade_exec.py:1169`) — and `pptxgym/` is off limits
  to a repair by red line 3, enforced by `pipeline.revert_tool_changes`
  (`pipeline.py:1281`), which reverts the edit and marks the deck `needs_human`
  (`cli.py:885-891`).

So the loop's only possible outcome on this order was to spend a repair
producing nothing, or to produce a red-line violation. Parking was the correct
outcome; **arriving at it after three unrelated repairs, rather than
immediately, is the waste.**

### The repairer already hit this wall twice on this deck

R2's own log states it edited `pptxgym/assets.py` (adding `literal_data`, so a
`data` asset carrying `rows` lands as a CSV). R3's log states it deviated from
its order twice — shipping `.jpeg` instead of `.png`, and *withdrawing* a
reference render instead of re-cutting its mask — because both literal fixes
"would have needed a change inside `pptxgym/assets.py`, which is shared by every
deck". R3 obeyed the red line. R2 did not, and **nothing stopped it**:

- there is no `repair-02-tool-change.diff` anywhere under `work/`;
- the deck was not marked `needs_human`; it went on to R3;
- `literal_data` and `media_still_in_deck` are both in `HEAD`, committed by a
  human in `2beda3e` (08-04 **17:28**) — 22 minutes after R2 finished;
- the guard itself landed in `6e242f1` (08-04 **17:37**), **nine minutes after
  the commit that carried the repair's edit**.

`revert_tool_changes` also has a standing hole independent of that timing: it
only reverts paths that were **clean before the run** (`pipeline.py:1298`,
`was_dirty`). During a live run with a human editing the tree — exactly the
state on 08-04 — the guard is silently disabled for every file the human has
open.

### Is it a bad deck?

No. The final probe rates d1, d2, d4 and d5 `determinate: true` with concrete
evidence, `est_steps_measured` 185 vs 220 declared (ratio 0.84, the median of
the run). Its only open defects are (a) the thumbnail, a packaging bug affecting
8 of 10 decks, and (b) a **false** rework item: the probe's second order asks for
the LREE/MREE/HREE boundaries, which R1 had already written into the instruction
— see §4.7.

---

## 1.2 deck0008 — a good task blocked by the reward model, then parked for a defect the repairer had already solved

### The trace

| # | log | at | ordered by | orders | outcome |
|---|---|---|---|---|---|
| R1 | `repair-01.jsonl` | 08-05 02:05 | — | — | **aborted after 10 turns**, `error_during_execution`, $0.22, 27.6 s. No `repair.md` entry, no artefact change. **Budget consumed: 1 of 3.** |
| R2 | `repair-02.jsonl` | 08-05 06:47 | `solvability.json:leaked` | 4 | orders [2][3][4] done; **[1] refused as impossible**, with a written tooling proposal (`bake_crop`) and a recommendation to park |
| R3 | `repair-03.jsonl` | 08-05 07:26 | `solvability.json:leaked` | 2 | **[1] impossible again, [2] forbidden** (it was "drop d1", i.e. shrink the task). The repairer found a third route the order did not offer — delete slide 3's reference too, so the shared media part is pruned — and **closed the leak**: `solvability.json` is now `solvable`, `leaks: 0`. |
| — | *(never ran)* | 08-05 07:48 | `task.json:needs_rework` | 2 | **PARKED**, `repairs_done()==3` |

R3 worked. The deck is `solvable`, `est_steps_measured` 270 vs 228 declared, five
degradations intact, `d1` grown from 90 to 115 steps. It then hit `scored`.

### What actually blocks it

`plan.json` → `coherence`:

```
ground_truth    unweighted 1.000   score 0.0   failed_gate media_not_pasted
rebuilt_by_hand unweighted 1.000   score 0.0   failed_gate media_not_pasted
half_restore    unweighted 0.507   score 0.0   failed_gate media_not_pasted
over_eager      unweighted 1.000   score 0.0   failed_gate media_not_pasted
```

**The rubric is perfect.** `unweighted == 1.000` for the ground truth and for a
hand-rebuilt answer. The only thing zeroing them is
`_gate_media_not_pasted` (`comparators.py:2035`):

```python
gt_only  = set(gt media digests) - set(input media digests)
supplied = set(plan["assets_sha"])
intruder = (gt_only - supplied) & set(candidate media digests)
```

The gate's own docstring states its premise out loud: *"the asset exemption is
not a loophole: nine of the ten decks supply every gt-only blob byte for byte in
`assets/`, because 'put this picture back' is the instruction."* deck0008 is the
tenth. R3 deliberately did **not** ship the bytes — shipping them would have
moved the leak from the zip into `assets/`, because
`assets.extract_deleted_images` runs against the whole delta, not the entry that
requested it. So deck0008 holds two gt-only, unsupplied blobs, and the ground
truth — which is `source.pptx`, i.e. the original file — trips a gate whose
premise is that the original bytes are always obtainable.

**A task whose design is "you cannot get the original pixels back, reconstruct
them" is unrepresentable.** Not hard to score: unrepresentable.

Three secondary defects compound it:

- **One root cause is reported as five problems.** All four coherence states
  fail the same gate for the same reason, producing four near-identical lines,
  plus a fifth — *"over-eagerness alone zeroes the score; a scope violation must
  cost a fraction, never everything"* — which is **false**. Scope violations are
  penalties capped at `PENALTY_CAP = 0.50` (`comparators.py:1747`) and can never
  zero anything; `over_eager` scored 0 because of `media_not_pasted`, and the
  check at `comparators.py:2664` reads the post-gate `score` instead of
  `unweighted`. The work order the human reads is 80% noise and 20% misdirection.
- **A sixth, self-referential problem.** `pipeline.py:1608` re-scores the ground
  truth with the already-rejected plan; a non-empty `plan["rejected"]` fails the
  `plan_accepted` gate, so `good["score"] == 0.0`, and the stage appends *"the
  ground truth scores 0.000, not 1.000"*. It scores 0.000 **because the plan was
  rejected**.
- **The floor check is masked.** When `plan["rejected"]` is non-empty,
  `plan_accepted` zeroes *both* calibration probes, so the
  `bad["score"] > CALIBRATION_TOL` check at `pipeline.py:1615` — the one that
  catches a task where doing nothing pays — can never fire on a rejected plan.

### Is it a bad deck?

No. It is a **better** deck than it was: R3 closed the leak and grew the task
from 197 to 228 declared steps without dropping a degradation. It is blocked by
a reward-model limitation, and the human decision it needs is a one-line change
to `_gate_media_not_pasted`, not a decision about the deck.

---

## 1.3 The answer to the question asked: how many of the six repairs?

| repair | order executable by the repairer? | work produced |
|---|---|---|
| deck0001 R1 | yes | **full** |
| deck0001 R2 | yes *in substance*, **no within the permitted surface** — it needed `pptxgym/assets.py` and took it | full (via a red-line violation the guard did not catch) |
| deck0001 R3 | order [1] **partly not** (a wider mask needs the shared mask producer) | partial + two documented deviations |
| deck0008 R1 | n/a — aborted at turn 10 | **none** |
| deck0008 R2 | order [1] **no** (`bake_crop` does not exist and may not be added) | orders [2][3][4] only |
| deck0008 R3 | order [1] **no** (same), order [2] **forbidden** (shrinks the task) | **the leak closed, by a route neither order offered** |

- **Repairs spent on an order with no legal route: 2 of 6** (deck0008 R2 and R3,
  both order [1], the same `bake_crop`).
- **Repairs that produced nothing at all: 1 of 6** (deck0008 R1, an infra abort
  that still burned a third of the budget).
- **Repairs spent on the defect that actually parked deck0001: 0 of 6.**
- **Repairs that had to deviate from, or work around, a shared-tooling gap: 4 of 6**
  (deck0001 R2 and R3, deck0008 R2 and R3).

The last row is the finding. Two thirds of the repair effort on the parked decks
went into negotiating with the operator vocabulary, not into fixing decks. The
repair logs name four missing capabilities explicitly — and a fifth turns up on
a deck that passed:

| gap | named in | what it blocks |
|---|---|---|
| strip/regenerate a `docProps/` part | deck0001 final order | every deck damaged on slide 1 |
| `pad_in` / explicit mask box on a reference-render asset | deck0001 `repair.md` R3 | any deck whose delta box is tighter than the region worth disclosing |
| `bake_crop` — rewrite a media part to its visible crop | deck0008 `repair.md` R2 | **any deck where one bitmap is drawn on two slides with different crops and only one is deleted** |
| per-slide resolution in `assets.extract_deleted_images` | deck0008 `repair.md` R2 | shipping one deleted picture without shipping all of them |
| run-level restyle inside a table cell (`_text_runs` stops at `p:txBody`, while `comparators._run_groups` already reads `a:tc/a:txBody`) | deck0009 `repair.md` | any "the emphasis inside a table was wiped" proposal — it hit `FLOOR_LIMIT` and had to be re-expressed as a deletion |

---

# 2. The thumbnail leak, as a pipeline defect

## 2.1 What is there

`docProps/thumbnail.jpeg` in `input.pptx`, **byte-identical to `source.pptx`**, on
8 of 10 decks. Nothing in the degrade path touches `docProps/`; `prs.save()`
carries it through verbatim.

| deck | present | px | px/in | slide 1 damaged? |
|---|---|---|---|---|
| deck0001 | yes | 256×192 | **25.6** | **YES** |
| deck0002 | yes | 256×144 | 19.2 | no |
| deck0003 | yes | 256×192 | 25.6 | no |
| deck0004 | yes | 256×144 | 19.2 | no |
| **deck0005** | yes | **768×432** | **57.6** | no |
| deck0006 | yes | 256×144 | 19.2 | no |
| deck0007 | **absent** | — | — | no |
| **deck0008** | yes | **768×432** | **57.6** | no |
| deck0009 | **absent** | — | — | no |
| deck0010 | yes | 256×144 | 19.2 | no |

It is **always slide 1 only**, never a contact sheet — verified by normalised
cross-correlation of each thumbnail against `renders/p-01..05.png` (slide 1 wins
on all 8; the three lower scores are font substitution between PowerPoint's
render and LibreOffice's, confirmed by eye).

## 2.2 Can a solver recover the damage from it?

On deck0001, **yes, comfortably.** Cropping the deleted logo's exact EMU box out
of each image:

| image | non-white fraction in the box |
|---|---|
| `input.pptx` thumbnail | **0.179 — logo present** |
| source render, intact | 0.131 — logo present |
| render of `input.pptx` slide 1 | **0.000 — logo gone** |

At 25.6 px/in, 1 px = 0.039 in = 35,719 EMU. Reading each edge to ±1 px gives
±0.039 in on position and ±2% on width. The deck's own stated grading tolerance
for this class of restoration is ~0.1 in. **The leak is ~2.5× finer than the
tolerance** — and it discloses precisely the one thing d5's proposal declares is
"given nowhere", the reason R3 withdrew the masked render.

It is worse than deck0001 makes it look. On a 768-wide thumbnail (deck0005,
deck0008) the resolution is 57.6 px/in = 15,875 EMU/px, only **1.7× coarser than
the pipeline's own `POS_TOL` of 9,144 EMU**. The next deck damaged on slide 1
with a 768px thumbnail leaks its geometry essentially exactly.

## 2.3 Why nothing caught it

`pkg_check` is the deterministic, free, per-deck answer-leak gate, run on every
`input.pptx` at `pipeline.py:1116` and hard-failing the `degraded` stage. It is
structurally blind here, in two independent ways:

- `LEAK_PREFIXES` (`pkg_check.py:268-273`) is `ppt/charts/`, `ppt/embeddings/`,
  `ppt/media/`, `ppt/diagrams/`. **No `docProps/`.**
- `ALWAYS_KEEP` (`pkg_check.py:44-52`) whitelists `"docProps/thumbnail.jpeg"`
  out of the orphan check — in a module whose docstring calls orphan parts
  "an *answer leak*". (It would have been reachable via `_rels/.rels` anyway.)

`python -m pptxgym.pkg_check work/deck0001/input.pptx` prints `OK`. The defect
was instead found by an LLM probe at $1.64/deck, on the one deck in ten where it
happened to matter.

## 2.4 Is stripping it unconditionally safe?

**Yes. Measured, not inferred** — both editors are installed on this machine.
Test pair: deck0001 `input.pptx` as shipped, and a copy with the part **and** its
`metadata/thumbnail` Relationship in `_rels/.rels` removed (129 → 128 parts).

| route | thumbnail in the output |
|---|---|
| LibreOffice `--convert-to pptx` (roundtrip.py's own invocation), thumb present | **gone** (157 parts, `docProps/{core,app,custom}.xml`) |
| LibreOffice, thumb stripped | **gone** — identical |
| LibreOffice GUI Ctrl+S under Xvfb | **gone** — identical |
| **WPS** GUI save (`wps_roundtrip.roundtrip_wps`), thumb present | **present**, 300×225, md5 `956ca7b3…` — **a fresh render of the *damaged* slide 1** (deleted-logo crop non-white = **0.000**) |
| **WPS**, thumb stripped | **absent**; no `metadata/thumbnail` rel |

Two facts that settle the question:

1. **WPS mirrors the input.** It writes a thumbnail only if the file it loaded
   had one. Strip it and it stays stripped — the ground-truth-vs-solved part
   asymmetry the brief worried about **does not occur**.
2. **Even if it did, no grader can see it.** `inventory.py:1487/1491` builds
   `media` from `"/media/" in name`; `_categorise()` (`inventory.py:1408-1428`)
   returns `None` for anything under `docProps/`, so `package["parts"]` excludes
   it too. `_gate_media_not_pasted` and `_scope_media_lost` are `/media/`-scoped.
   `emit.py:506` asks only whether `ppt/presentation.xml` exists. All 9 emitted
   evaluators under `work/emitted/task_class/` mention `thumbnail` on exactly one
   line — an inlined docstring. **The change is score-neutral by construction.**

Also worth recording: WPS regenerating a *correct* thumbnail is itself the
cleanest possible fix for a solved file, and confirms there is no path by which
the stale image comes back.

## 2.5 The fix, precisely enough to implement

Three changes, none of them in the recipe or asset layer:

1. **`degrade_exec.run`, immediately after `prs.save(out_path)`
   (`degrade_exec.py:1169`)** — before the SmartArt post-save zip rewrite, since
   that already reopens the archive. Rewrite the package dropping:
   - the part `docProps/thumbnail.*` (match on the `Target` of the
     `.../metadata/thumbnail` relationship, not on a hardcoded filename —
     PowerPoint writes `.jpeg`, other producers write `.png`/`.emf`);
   - the `<Relationship Type=".../metadata/thumbnail">` element from `_rels/.rels`;
   - any `<Override PartName="/docProps/thumbnail.*">` in `[Content_Types].xml`.
     Do **not** touch `<Default Extension="jpeg">` — it covers `ppt/media/*.jpeg`
     as well, and removing it corrupts the package.

   Prefer stripping to regenerating: LibreOffice does not write one on export,
   so "replace it with a render of the degraded slide 1" would need a new
   rendering dependency in the degrade path for zero benefit. WPS will write a
   correct one on the solver's first save.

   Record it in `delta.json` as a package-level entry (e.g.
   `delta["package_parts_removed"]`) so `reconciled` and the probe can see that
   the difference is deliberate, and so no future part-list comparator reads it
   as damage.

2. **`pkg_check.leak_check`** — add a package-level clause that does not depend
   on `LEAK_PREFIXES`: *if `input.pptx` contains any `docProps/thumbnail.*` that
   is byte-identical to `source.pptx`'s and the delta damages page 0, that is a
   leak.* Better still, make it unconditional — the thumbnail can never be
   anything but a stale render of slide 1, and slide 1 is a legitimate damage
   site. Remove `"docProps/thumbnail.jpeg"` from `ALWAYS_KEEP`
   (`pkg_check.py:44`) so the orphan check stops exempting it.

3. **A regression test** in `tests/` that degrades a fixture damaged on slide 1
   and asserts `docProps/` holds no thumbnail and the package still opens.

This is the highest-value item in the audit: **it converts a $1.64/deck
probabilistic LLM catch into a free deterministic one, and it closes a leak that
is 1-in-10 today only because slide 1 is rarely the damage site.**

---

# 3. The two `suspect` consistency verdicts

Both are **true**, both are **correctly rated `warn`**, and **neither should
block**. They are the same finding three times over.

## 3.1 Mechanics

`consistency.py:44-51` — severities are `fail` / `warn` / `info`; there is no
`block`. Roll-up at `consistency.py:531`:

```python
worst = "fail" if any fail else "suspect" if any warn else "ok"
```

`deg_slide_without_delta` (`consistency.py:378-385`) is **the only check in the
module that can emit `warn`**. Therefore `verdict == "suspect"` is exactly
equivalent to "that one check fired"; it carries no other information.

Only `fail` blocks (`pipeline.py:1768`); `warn` is recorded and dropped
(`pipeline.py:1783`, *"recorded, not gated: a warn is for a reader"*), and
`cli._rework_from_consistency` (`cli.py:812`) discards the warn list, so a
`suspect` deck never generates a work order. That is deliberate
(`pipeline.py:1729`: *"blocking on it would train everyone to disable the
check"*).

## 3.2 The findings, and whether they are true

- **deck0003**, 2 warn: *"d2 lists slide 1 but nothing on that slide changed"*;
  *"d5 lists slide 13 but nothing on that slide changed"*.
- **deck0005**, 1 warn: *"d3 lists slide 18 but nothing on that slide changed"*.

Verified independently by shape-tree diff of `source.pptx` vs `input.pptx`
(`inventory_pptx`, comparing kind / name / picture blob / text / bbox):

| deck | slide | gt shapes | input shapes | identical |
|---|---|---|---|---|
| deck0003 | 1 | 8 | 8 | **yes** |
| deck0003 | 13 | 14 | 14 | **yes** |
| deck0005 | 18 | 9 | 9 | **yes** |

All three warnings are true. All three are the **benign** case the check's own
evidence string names: the slide is listed as the *surviving reference*, not as
a damage site.

- deck0003 d2: the two picture blobs slide 18 lost
  (`2d3e11c9…`, `279bd563…`) are drawn byte-identically on **slide 1 of
  `input.pptx`**, and were deliberately withheld from `assets/`. Slide 1 is the
  only reachable source; removing it from `d2.slides` would erase the record of
  why the deck is solvable.
- deck0003 d5: the instruction says outright *"Slide 13 still shows the arrow,
  in the same place."*
- deck0005 d3: instruction — *"Nothing was actually thrown away — the same two
  plots still exist somewhere else in this deck."*

## 3.3 Is `warn` the right severity? Yes.

The test is mis-scoring, and it does not bite:

- **No scoring component touches the reference slides.** deck0003's
  `plan.damage.slides` (0-based) is `[6, 11, 16, 17, 18]`; deck0005's is
  `[3, 5, 6, 7, 9, 10, 11, 15, 18]`. Slides 1 / 13 / 18 carry **zero weight**.
  `unscoreable` and `rejected` are empty in both plans.
- Coherence passes cleanly in both: `ground_truth` 1.0, `rebuilt_by_hand` 1.0,
  `over_eager` 0.9, `failures: []`.
- **Being outside `plan.damage` actively protects them**:
  `_scope_untouched_pages` (`comparators.py:2072`) charges 0.10/page, capped at
  0.30, if a solver *moves* the maps off slide 1 instead of copying them. The
  hole the warning points at is already plugged.
- Both were already adjudicated upstream, in writing:
  `work/deck0005/task.json` d3 `note` quotes the warning verbatim and explains
  it; deck0003's d2 `note` does the same for slide 1.

**Neither should block.** Blocking here rejects three correctly-built
degradations — the "good deck rejected" failure mode. The root cause is that
`deg.slides` is overloaded: it usually means "slides this degradation damaged",
and in these three cases means "slides this degradation involves". The clean fix
is a schema change — a separate `reference_slides` field — not a gate.

## 3.4 Is the check noisy? No — this one is well targeted

Census across the 8 packaged decks, 18 findings total:

| check | severity | findings | decks |
|---|---|---|---|
| `asset_is_the_answer` | info | 15 | 5/8 |
| `deg_slide_without_delta` | **warn** | 3 | 2/8 |
| everything else (7 checks) | fail | **0** | 0/8 |

**No `fail` fired anywhere in the run.** I checked all 40 degradations across
the 8 packaged decks: five others list multiple slides (deck0002 d5, deck0004
d5, deck0005 d6, deck0007 d3, deck0009 d2) and **every one touched every slide
it claimed**. The check has no false positives in this run. `asset_is_the_answer`
is the high-frequency one but is `info` and cannot move a verdict.

---

# 4. The near-failures

Ranked by whether they would **mis-score a real agent** (worst), **block a good
deck**, or are **cosmetic**.

## 4.1 [MIS-SCORE] A hollow shape of the right kind in the right box is worth up to 40% of a deck

`_cmp_restored_shape` (`comparators.py:986`, the comparator for `delete`) asks
*what it is* × *where it is*. The `what` facets are drawn from the ground-truth
shape's **own** `picture.blob` / paragraphs / table / diagram / chart / fill
(`comparators.py:1032-1044`). A **group** shape carries none of them — its
content lives in its children — so `what` is empty and the function falls
through to `return _blend(where)` (`comparators.py:1053`). The component becomes
a pure bounding-box test.

**44 of the 229 `delete` components across the 8 packaged decks are in this
state**; their GT `why` string is literally `position=1.00 · size=1.00`.

Scoring `input.pptx` + one empty shape per such component (same `kind`, same
bbox, new name, no text, no fill, **no children**):

| deck | hollow-shape score | what they are |
|---|---|---|
| **deck0003** | **0.4035** | 2 whole groups |
| **deck0005** | **0.3906** | 1 group + 2 autoshapes + 9 connectors |
| deck0004 | 0.1889 | 4 connectors |
| deck0006 | 0.1875 | 17 connectors |
| deck0009 | 0.1279 | 1 oval + 4 arrow connectors |
| deck0002 | 0.0685 | 4 arrow connectors |

No hard gate fires (`failed_gate = None` in every case); `no_extra_shapes` costs
deck0009 0.04 and the others nothing.

The two worst are the most valuable components in their decks — verified
directly in the plans:

- `deck0003/plan.json` `c005`, `deg d3`, **weight 0.2632**, slide 17,
  `spec.kind = "group"`, `spec.text = ""`. Its 4 members (picture, autoshape,
  2 connectors) are **not components** — 0 of 4 appear as `gt_path`. 26.3% of
  deck0003 is "is there a group-shaped thing whose centre is within tolerance
  and whose w/h match". The picture inside it is never compared.
- `deck0003/plan.json` `c004`, `deg d5`, weight 0.1404 — group of 3.
  c004 + c005 = **40.4% of deck0003**.
- `deck0005/plan.json` `c003`, `deg d2`, **weight 0.2651**, slide 7, group with
  **16 members** including 5 text boxes. Its degradation's `est_steps` is **110,
  the largest single chunk of work in the deck**, scored by one bbox comparison.

There is a partial brake: `comparators.py:1024` requires either a strong
identity key or an *exact* centre match, so the hollow shape must be placed
precisely. That is a far cheaper action than restoring 16 shapes, and it is
exactly the move a training run finds first — which is the failure the
comparator's own docstring says it was rewritten to prevent.

**Fix:** when the ground-truth shape is a composite with children and no own
content facet, either recurse (score the children as sub-facets, weighted by
count) or emit one component per child. A group must not be scoreable by its
bounding box alone.

## 4.2 [MIS-SCORE] `wrong_params` silently skips two of the operators in use, and the skip pays out

`_wrong_params` (`attacks.py:1061`) dispatches on `entry["op"]` with branches for
`delete / move / scatter / resize / set_font / outline / clear_table_cells /
strip_animation / smartart_drop_nodes`. Anything else falls through with
`hit = False` — so the attack candidate keeps the **correct** ground-truth value
for that component, which then scores 1.0 and pays the attacker.

Recorded in the run:

- `deck0009/attacks.json` → `wrong_params.score = 0.1416`,
  `"22 values wrong across 3/4 components; not perturbed: ['d2/table_drop_rows']"`
  — d2's weight is 0.1475. The score **is** that component paying out.
- `deck0004/attacks.json` → `0.105`, `"not perturbed: ['d5/recolor']"` — d5's
  weight is 0.125.

So `table_drop_rows` and `recolor` have **zero adversarial coverage** in this
run. Nothing has ever shown that those comparators reject a wrong value. The
`AtMost(0.300)` expectation absorbed it. Two uncovered ops summing past 0.30 on
a future deck will fail the battery for a reason unrelated to comparator quality.

**Fix:** make the fall-through loud — if an op has no perturbation branch, mark
the attack `inconclusive` rather than scoring it, and fail `harden` on
"operator with no adversarial coverage".

## 4.3 [MIS-SCORE] `noop ≈ 0` is an algebraic identity, and `damage_untouched_gt` is unfalsifiable

Two of the run's headline safety numbers measure nothing.

- **`noop`.** `score()` measures `floor` from `init_inv` on every call
  (`comparators.py:2244`), and the `noop` candidate **is** `init_inv`
  (`attacks.py:736`, evidence *"byte-identical to input.pptx"*). So
  `raw == floor` and `value = (raw-floor)/(1-floor) = 0` unconditionally.
  Confirmed: untouched `input.pptx` scores **exactly 0.000000 on all 10 decks**,
  and `floor > 0` for **0 of 229** components. That is the number the brief asked
  for — and it is worth nothing, because no comparator change, widened tolerance
  or free point could ever make it non-zero. **The real do-nothing surface is
  §4.1, which `noop` structurally cannot see.**
- **`damage_untouched_gt`.** Declared `NoGain("gt")` (`attacks.py:1296`), i.e.
  `score ≤ gt`. Since `gt == 1.0`, **any** score passes. It recorded 0.900 with
  `ok: true` on all 8 decks; 0.05 would also have been `ok: true`. What it is
  actually reporting is that `SCOPE_RATES["untouched_pages_unchanged"]` is
  `(0.10, 0.30)` — an agent that trashes five bystander pages while doing the
  task perfectly still scores **0.70**.

**Fix:** replace `noop` with a candidate that is *plausibly* idle (input plus
harmless edits — a save round trip, a renamed shape) so the floor cannot absorb
it; give `damage_untouched_gt` an absolute `AtMost` threshold.

## 4.4 [MIS-SCORE] The picture comparator is byte-exact, and the eval VM assumption is unverified

`_facet_picture` (`comparators.py:742`) compares **exact blob bytes** and is
*multiplied* into geometry. Its own docstring notes a rollout has already been
seen with `.pptx` bound to Impress. Simulating an image re-encode on a perfect
answer (rewrite every picture blob digest, change nothing else):

| deck | perfect answer after re-encode |
|---|---|
| deck0003 | **0.5614** |
| deck0006 | **0.6368** |
| deck0007 | 0.7051 |
| deck0010 | 0.7843 |
| deck0004 | 0.8611 |
| deck0002 | 0.8699 |
| deck0005 | 0.8916 |
| deck0009 | 1.0000 |

The only thing standing between the batch and an 11–44% haircut on correct work
is the assumption that the scoring environment is WPS. `REWARD.md` §2.4 already
says the 0.0% WPS result is *"a result for this set of fonts on this machine"*,
and its own step 3, "re-confirm on the VM", is **not done**. Related: only
**deck0001** has a `roundtrip_wps` measurement (`stable`, 0.0%); the other nine
were judged only by LibreOffice, where **all ten** decks are `fragile`. The
editor the tasks actually run in was measured once.

**Fix:** a perceptual hash fallback for `_facet_picture` (the `media_not_pasted`
docstring already identifies the same gap from the other direction), plus
running the WPS round trip on all decks, not one.

## 4.5 [MIS-SCORE] `unscoreable` components are dropped but their weight is paid to the survivors

`build_plan` drops components the ground truth itself cannot satisfy
(`comparators.py:2417`), then splits each degradation's weight **equally among
the survivors** (`comparators.py:2452`, `weight = share / len(members)`).

- `deck0004/plan.json`: `unscoreable` holds **6 `d5/set_font`** components
  (*"the ground truth states none of [color] explicitly on this shape — it
  inherits them"*). d5 keeps its full 0.1250 weight, now carried by its 3
  surviving `recolor` components. **An agent that fixes the 3 fills and none of
  the 6 fonts scores 100% of d5.**
- `deck0006/plan.json`: same pattern, 7 `d5/set_font` dropped.

**Fix:** either forfeit the dropped share (renormalise across degradations) or
reject the plan — silently redistributing it converts unscoreable work into free
credit.

## 4.6 [BLOCKS A GOOD DECK] The post-park loop re-runs stages on a deck it has already parked, and the guard against it cannot fire

`cli.py:1037-1049`:

```python
for _ in range(pl.MAX_REPAIRS):
    if not _rework_of(deck)[0]: break
    await step("repair", cmd_repair, ns)          # PARKED → mark reconciled "needs_human"
    for s2 in pl.STAGES[pl.STAGES.index("recipe"):]:
        if not deck.promoted(s2) ...:
            await step(s2, ...)                   # reconcile re-runs, overwriting "needs_human"
    if deck.state()["reconciled"]["status"] == "needs_human":
        break                                     # unreachable
```

`promoted("reconciled")` requires status `"ok"` (`pipeline.py:299`), and
`needs_human` is not `ok` — so the inner loop re-runs the reconcile agent, which
overwrites the status with `rejected`, so the `break` never fires and the outer
loop goes round again.

Observed: deck0001 ran reconcile **twice** after being parked ($1.88 + $1.99,
694 s of Opus time); deck0008 once ($2.34, 424 s). ~**$6 and ~19 agent-minutes
spent on decks the pipeline had already given up on.**

It also leaves the deck internally inconsistent. deck0001's `bundle/` (the
deliverable) was built at 07:02 from reconcile run 5; the live `task.json` is run
7's output with **different instruction text**. `bundle.json` records
`task.json: 1a2705ff…`; the live file hashes `83ab010f…`. Only the two parked
decks are affected — I checked all ten, and every packaged deck's
`bundle/instruction.md` is an exact match for its `task.json`.

**Fix:** check `needs_human` immediately after `cmd_repair` returns, before the
stage-rerun loop; and have parking `return` from `_run_one` outright.

## 4.7 [BLOCKS A GOOD DECK] The work-order reader prefers the older artefact, and re-issues fixed complaints

deck0001's final `solvability.json` carries **two** rework entries. The second
asks for the slide-8 LREE/MREE/HREE boundaries — the exact thing repair 1 fixed.
The current `task.json` instruction contains *"which ran La to Nd, Pm to Tb and
Dy to Lu"*.

The probe was not wrong about what it saw: it read
`bundle/instruction.md` (hash `8b31ea3e…`, built from reconcile run 5's
`task.json`, hash `1a2705ff…`), which lacks the clause; the clause was added by
reconcile run 6 **after** the probe ran. But `_rework_of` (`cli.py:841`) walks
`GATE_ARTEFACTS` in a **fixed order** with `solvability.json` **first**
(`cli.py:832`), and a gate artefact is only retired after a *successful* repair
(`cli.py:919-924`). So the order that would have gone to a fourth repair is the
**stale** one, including an item that was already fixed — while the fresher,
correct `task.json` order (thumbnail only) sits second in line.

Compounding it: the solvability probe is **non-deterministic and flips on an
unchanged bundle**. Verdict histories:

- **deck0009**: `leaked → solvable → solvable → n/a → undetermined → leaked →
  leaked → undetermined → n/a → solvable → solvable` — 4 distinct verdicts over
  10 runs. `attempts/solvable-06` and `-07` (both 08-04 18:08) report three
  specific leaks including run-boundary residue in `slide7.xml`/`slide8.xml`;
  `-08` (18:52) reports `leaks: []`. **`repair-01.jsonl` is timestamped 18:59** —
  the leak vanished from the report before any repair ran.
- **deck0007**: `ambiguous → undetermined ×5 → solvable`.
- **deck0002**: `solvable → undetermined ×4 → solvable ×2`.

And **only repairs are capped; stage re-runs are not.** deck0002 ran the probe
**11** times on 2 of 3 repairs; deck0007 and deck0009 ran it 10 times each.
"Re-roll the probe until it says solvable" is an available, unmetered move.

**Fix:** order `GATE_ARTEFACTS` by artefact mtime / stage fingerprint rather than
by a static list; retire a gate artefact whose `_in` fingerprint no longer
matches the deck. Cap probe re-runs, and record each attempt's `_in` in
`attempts/*/state.json` (it currently is not — which is exactly what you would
need to adjudicate the deck0009 flip from the record alone).

## 4.8 [BLOCKS A GOOD DECK] Two packaged decks went green on their final allowed repair

`MAX_REPAIRS = 3` (`pipeline.py:118`), enforced at `cli.py:864`. The counter is
`len(glob("repair-*.jsonl"))` (`pipeline.py:1310`) — a filesystem glob, never
reset, and **it counts an aborted run**.

| deck | repairs used | outcome |
|---|---|---|
| **deck0003** | **3/3** | packaged — `solvable` first arrived on `attempts/solvable-04`, after `leaked` and `ambiguous` |
| **deck0007** | **3/3** | packaged — `solvable` first arrived on `attempts/solvable-10`, after `ambiguous` + five consecutive `undetermined` |
| deck0001, deck0008 | 3/3 | **parked** |
| deck0002, deck0004, deck0009 | 2/3 | packaged |
| deck0005 | 1/3 | packaged |
| deck0006, deck0010 | 0/3 | packaged |

**deck0003 and deck0007 are one unlucky verdict from being parked**, and
deck0007's `repair.md` records the same complaint coming back four times without
being acted on. Combined with deck0008 R1 — 10 turns, `error_during_execution`,
27.6 s, **zero artefact change, one third of the budget** — the budget is being
spent on things that are not repairs.

**Fix:** do not count a repair whose agent run ended `is_error` / `infra` /
`timeout` (the code already returns early on `infra` without invalidating —
it should also unlink or not create the log). Consider making the budget
per-distinct-complaint rather than per-deck.

## 4.9 [BLOCKS A GOOD DECK] `media_not_pasted` has literally zero margin on five packaged decks

The gate that parked deck0008 is exempted only by **byte identity** against
`assets/`:

| deck | gt-only media blobs | covered by `assets_sha` | margin |
|---|---|---|---|
| deck0002 | 4 | 4 | **0** |
| deck0003 | 4 | 4 | **0** |
| deck0006 | 5 | 5 | **0** |
| deck0007 | 1 | 1 | **0** |
| deck0010 | 1 | 1 | **0** |
| deck0004/0005/0009 | 0 | — | n/a |
| deck0001 | 1 | **0** | parked |
| deck0008 | 2 | **0** | parked |

One byte of re-encoding in one asset file — a converter added to
`assets.py`, a producer that normalises a JPEG — and the ground truth scores 0
and the deck is parked. Five of the eight packaged decks are in that position.
This is the same gate, the same failure mode, on both parked decks.

**Fix (also the deck0008 unblock):** record in the plan the set of gt-only
digests that are *deliberately* unsupplied, and exempt the four gt-derived
coherence states (`ground_truth`, `half_restore`, `rebuilt_by_hand`,
`over_eager`) from `media_not_pasted` — they are constructed from the ground
truth by definition, so applying a cheat gate to them is a category error. Keep
the gate for real candidates, where it is correct. And back it with a perceptual
hash so byte-identity is not the only exemption.

## 4.10 [BLOCKS A GOOD DECK] `materialised: partial` shipped on 4 of 8 decks, one of them for a non-request

`PROMOTES = {"materialised": ("ok", "partial")}` (`pipeline.py:107`) lets an
unmet asset through on the promise that reconcile addresses it. Recorded reasons:

- **deck0002**: `"none: no producer for asset kind 'none'"` — the proposal used
  `kind: "none"` to record a *decision* not to ship a file. `assets.py:69`
  `NOT_A_REQUEST` now handles exactly this, and `assets.py:50-67` names deck0002
  as the motivating case — but the fix landed in `9c2272a` (**08-05 05:41**) and
  deck0002's materialise ran at **01:58:52** and was never re-run. The manifest
  on disk still reports the false unmet, and the deck packaged on it.
- deck0003, deck0004: `"data: no chart or table found on slides […]"`.
- deck0005: `"the 'data' asset for slide 19 was never produced … what came back
  instead was p06-table.csv"` — genuinely reconciled, and documented in
  `task.json`.

deck0002 exposes a general staleness hole: `STAGE_INPUTS` fingerprints
*artefacts* and `_model_changed` catches a model swap, but **nothing invalidates
a stage when the tool that produced it changes.** A mid-run fix to `pptxgym/`
does not re-run anything.

**Fix:** include `pipeline.tool_tree_state()` (or the git SHA of `pptxgym/`) in
each stage's `_in` fingerprint.

## 4.11 [BLOCKS A GOOD DECK / DELIVERY RISK] A parked deck's obsolete task is sitting in the delivery directory

`work/emitted/task_class/` holds **nine** task files for **eight** packaged
decks. The extra is `task_1100013.py` → **deck0008**, emitted 08-05 **05:34**,
during the window when its reconcile verdict was `ready`. `packaged` was later
invalidated and the deck parked; **the emitted artefact was never retracted.**

It is not a harmless duplicate. Its `metadata.json` instruction is the
**pre-repair, leaking** version:

> *"the same figure still appears, cropped, inside the screenshot on slide 3 if
> you need to see what it looked like"* — the leak repair 3 closed —
> and it promises `p05-Picture-4.png` and `p11-table.csv`, both of which repairs
> 2 and 3 **deleted**.

I checked all nine: the other eight are byte-exact matches for their decks' live
`task.json` (`live_instruction in emitted_instruction` → True, ratio 1.000).
Only deck0008's is stale.

**Fix:** make `emitted/` transactional — key each emission to the deck's
`packaged` fingerprint, and have any invalidation of `packaged` remove or quarantine
the emitted directory. At minimum, `status` should report an emitted task whose
deck is no longer `packaged`.

## 4.12 [MIS-SCORE, LATENT] Weights track `est_steps` exactly — and `est_steps` is an unvalidated guess

`weight_source: "est_steps"` on 9 of 10 decks (deck0001 is
`"equal (est_steps unusable)"`). The derivation at `comparators.py:2452` is
faithful: I verified every degradation's weight equals its `est_steps` share to
4 decimals on every deck. **There is no deg-level mismatch.** The breakdowns are
one level up and one level down.

**Down — equal split inside a degradation.** `weight/comp = est_steps/(n·Σ)`,
which asserts every component of a degradation costs the same:

| deck | most steps/comp | fewest steps/comp | ratio |
|---|---|---|---|
| deck0006 | d1 = 120.0 (1 comp, w = 0.3158) | d5 = 1.12 (49 comps, w = 0.00295) | **107×** |
| deck0005 | d2 = 110.0 (w = 0.2651) | d1 = 4.74 (19 comps, w = 0.0114) | 23× |
| deck0009 | d3 = 120.0 (w = 0.3934) | d1 = 5.77 (13 comps, w = 0.0189) | 21× |

Components below 2% of total (below which a component cannot move a 2-decimal
reward, and is smaller than the 0.04 `no_extra_shapes` tick):

| deck | comps < 2% | mass they hold | min weight |
|---|---|---|---|
| **deck0006** | **97 of 98** | **0.684** | 0.00295 |
| deck0005 | 30 of 34 | 0.398 | 0.0114 |
| deck0009 | 13 of 24 | 0.246 | 0.0189 |
| deck0002 | 13 of 28 | 0.219 | 0.0167 |
| deck0004 | 10 of 25 | 0.181 | 0.0181 |
| deck0003 / 0007 / 0010 | 0 | 0 | ≥ 0.0784 |

deck0006 is the shape to worry about: **68% of the reward smeared over 97
components none of which can move the score by 0.3 pp, while 31.6% sits on a
single all-or-nothing `delete`** (`c021`, GT `why` = `image=1.00 ×
position=1.00 · size=1.00`). deck0009 `c003` is 39.3% on one component;
deck0007's d1 (29.5%) and d2 (25.6%) are one component each. The gradient a
training run sees is one or two binary bits, not a curve.

**Up — nothing validates `est_steps`.** `solvability.json` reports
`est_steps_measured` at **deck level only**; the per-degradation numbers that set
the weights are an LLM guess in `proposal.json` that no stage checks. On the one
deck where a probe broke it down (deck0002) it disagrees badly:

| deg | declared → weight | probe-measured | implied weight |
|---|---|---|---|
| d1 | 95 → **0.2603** | ~60 | 0.197 |
| **d5** | 60 → **0.1644** | **~110** | **0.361** |
| d6 | 85 → **0.2329** | ~45 | 0.148 |

d5 (48 table cells) carries **2.2× less weight than the only measurement anyone
made of it** — an absolute error of **19.7 percentage points**. The probe's note
concluded no rework was warranted *on the deck total*; it was not asked about
weights.

Deck-level ratios, all ten: 0.61 (deck0010), 0.67 (deck0007), 0.78, 0.82, 0.82,
0.84, 0.88, 0.91, 1.09, 1.18 (deck0008). Worth noting that
`est_steps_measured` takes only **five distinct values across ten decks**
— {175, 185, 270, 300, 310}, with 185 and 310 appearing three times each. That is
not the distribution of an independent measurement.

## 4.13 [COSMETIC] Smaller items

- `rebuilt_by_hand` scores **0.836** (deck0002) and **0.852** (deck0009) — a
  legitimate rebuild of a native composite losing 15% — and `_coherence`
  (`comparators.py:2640`) raises nothing, because it only reports on hard-gate
  failures, `ground_truth != 1.0`, or `over_eager <= 0`. Both decks shipped with
  `coherence.failures == []`. Add a floor on `rebuilt_by_hand`.
- `attacks.json` truncates every attack's `detail` to **2 components**
  (`attacks.py:2205`). For deck0006 that is 2 of 98 — per-component
  discrimination evidence is not persisted anywhere.
- Archived `attempts/*/state.json` do not carry the archived stage's `_in`
  block, so per-attempt input fingerprints are unrecoverable — precisely what
  §4.7's probe-flip needs.
- The `repair` entry is missing from `state.json` on deck0003 and deck0007 even
  though both have three `repair-*.jsonl` logs (the mark-repair feature post-dates
  their last repair). `repairs_done()` reads the glob, so the budget was still
  enforced, but `status` under-reports those two decks.
- `archive_attempt` copies the same log twice when a stage fails its checker and
  takes the "one clean retry" path — deck0001 `solvable-01`/`-02` are identical
  ($1.63 / 402 s), as are deck0008 `solvable-02`/`-03`. Attempt counts overstate
  distinct runs.

---

# 5. What to fix, in order

| # | fix | why first |
|---|---|---|
| 1 | **Strip `docProps/thumbnail.*` in `degrade_exec` after `prs.save`, and teach `pkg_check.leak_check` about `docProps/`** (§2.5) | Closes a live answer leak, unparks deck0001, and converts a $1.64/deck probabilistic catch into a free deterministic one. Measured safe: LO never writes one, WPS mirrors the input, no grader can see the part. |
| 2 | **Exempt the four gt-derived coherence states from `media_not_pasted`, and record deliberately-unsupplied gt-only digests in the plan** (§1.2, §4.9) | Unparks deck0008 — whose rubric already scores the ground truth 1.000 — and removes a zero-margin trip-wire from five packaged decks. |
| 3 | **Score composites by their children, not their bounding box** (§4.1) | The largest live mis-scoring surface: 40.4% of deck0003 and 39.1% of deck0005 buyable with an empty box, no gate firing. |
| 4 | **Fail `harden` when an operator has no `_wrong_params` branch; replace `noop` with a plausibly-idle candidate; give `damage_untouched_gt` an absolute threshold** (§4.2, §4.3) | Three of the battery's safety numbers currently cannot fail. |
| 5 | **Fix the post-park loop (`cli.py:1037`) and stop counting aborted repairs against `MAX_REPAIRS`** (§4.6, §4.8) | Two packaged decks had zero budget left; one parked deck lost a third of its budget to a 27-second abort; ~$6/run is spent on decks already given up on. |
| 6 | **Order `GATE_ARTEFACTS` by freshness and retire fingerprint-stale gate artefacts; cap probe re-runs** (§4.7) | Stops the loop re-issuing complaints that are already fixed, and closes the unmetered "re-roll until solvable" path. |
| 7 | **Make `emitted/` transactional against `packaged`** (§4.11) | A parked deck's obsolete, leaking task is on disk in the delivery directory right now. |
| 8 | **Fold the tool-tree SHA into every stage's `_in` fingerprint; close the `was_dirty` hole in `revert_tool_changes`** (§1.1, §4.10) | A mid-run `pptxgym/` fix currently invalidates nothing, and the red-line guard is disabled for any file a human has open. |
| 9 | **Renormalise (or reject) when components go `unscoreable`; add per-component `est_steps` and validate them** (§4.5, §4.12) | Free credit today; a 20-point weight error on deck0002 d5; deck0006's gradient is one binary bit carrying 31.6%. |
| 10 | **Add `reference_slides` to the degradation schema** (§3.3) | Retires all three `suspect` warnings without weakening the check. |
| 11 | **Perceptual-hash fallback for `_facet_picture`; run the WPS round trip on every deck, not one** (§4.4) | An 11–44% haircut on a perfect answer if the eval VM's `.pptx` handler is not WPS — an assumption `REWARD.md` §2.4 already flags as unverified. |
