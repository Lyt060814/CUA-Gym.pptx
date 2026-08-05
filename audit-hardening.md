# Audit — the `hardened` stage, ten-deck run of 2026-08-05

Read-only audit at commit `1a32068`. Nothing in the repo was modified; the two
timing measurements below were run into `/tmp/audit-attacks/` and `git status`
is clean.

Evidence read: `work/deck*/attacks.json` (8 files), `work/deck*/attack-report.md`,
`work/deck*/state.json`, `work/deck*/plan.json`, `work/deck*/delta.json`,
`pptxgym/attacks.py`, `pptxgym/pipeline.py:1631-1699`, `pptxgym/comparators.py`,
`pptxgym/wps_roundtrip.py`, `pptxgym/observe.py`, the repo-root `attack-report.md`
(previous recorded values), and the live `/tmp` display-pool state.

## Verdict

The 8 passes are passes **on the evidence**, not on an unexercised branch. Every
row in every table was reconstructed from `attacks.json` and the reject-decision
recomputed from `attacks.Report.reasons`: it returns `[]` for all 8, matching the
stored `"rejected": []`. `gt_roundtrip` genuinely fired on all 8 decks and scored
exactly `1.000000` (its expectation is `Exact(1.0)`, tolerance `1e-9`). No cheat
crossed its threshold; no legitimate variant lost a single point against `gt`.

Three things are wrong with it, in order of how much they matter under
宁愿一个task没有也不要让他被hack:

1. **`wrong_params` silently skipped a whole component on 2 of 8 decks** and the
   row still passed. deck0004 and deck0009 shipped with 12.5% and 14.8% of their
   reward mass never tested against a wrong-value restore. (Finding 1)
2. **`damage_untouched_gt` scores 0.900 on all 8 decks and would pass at 1.000.**
   Its bar is `<= gt`, so the battery asserts nothing at all about the size of the
   collateral-damage penalty. Doing the whole task and then wrecking an unrelated
   page keeps 90% of the reward. (Finding 2)
3. **`state.json` over-claims coverage.** It records `attacks: 14, variants: 6`
   for every deck; those are row counts, not executions. Actually executed:
   107/112 attack cells and 39/48 variant cells. deck0007's protection against
   being punished for legitimate work rests on 2 of its 6 variants. (Finding 3)

None of these is a task that can be hacked today. All three are places where the
record says "proved" and the evidence says "not asked".

---

## 1. Did every attack actually run on every deck?

**Yes, and the record distinguishes the four outcomes.** `Row.status` is one of
`scored | n/a | unconstructible | error | not_run`, every non-`scored` row carries
a human reason, and `Report.reasons` (attacks.py:2116-2133) rejects on
`unconstructible`, `not_run` and `error` — only `n/a` is a non-rejection, and it
always names why. No row in any of the 8 files is `error` or `not_run`.

Attack cells: **107 scored, 5 `n/a`, 0 unconstructible/error** out of 8 × 14 = 112.

| deck | attack rows not scored | reason recorded |
|---|---|---|
| deck0003, deck0006 | `native_to_picture` | no chart, table or SmartArt in the damage |
| deck0004, deck0005, deck0009 | `orphan_media` | the broken deck already holds every media part |

Both reasons check out against the recipes: deck0003's damage is 9 × `delete` and
deck0006's is `delete`/`set_font`/`outline` only — no native object to flatten.

### `gt_roundtrip` fired 8/8 — proof, not assertion

There is no fallback in the code path: `wps_pass` (attacks.py:1941-1977) calls
`wps.roundtrip_wps`, which claims a display, starts Xvfb, spawns
`/opt/kingsoft/wps-office/office6/wpp`, waits for a `Presentation` window, types
and un-types a dirty mark, clicks Save, and **raises** unless the target's
`st_mtime_ns` changes. It never returns a copy. The evidence string is then
computed by reading the produced file back.

Every one of the 8 evidence strings names a "before" size that matches that deck's
`source.pptx` byte for byte, an "after" size that differs, and a distinct
differing-part count:

| deck | source.pptx | after WPS save | parts differing |
|---|---|---|---|
| deck0002 | 17 548 799 | 10 287 682 | 147 |
| deck0003 | 12 787 880 | 12 197 753 | 165 |
| deck0004 | 6 029 746 | 5 656 321 | 121 |
| deck0005 | 7 836 346 | 7 288 095 | 47 |
| deck0006 | 3 082 394 | 3 087 696 | 88 |
| deck0007 | 1 921 742 | 2 000 294 | 138 |
| deck0009 | 3 106 729 | 3 178 291 | 101 |
| deck0010 | 7 266 724 | 7 120 645 | 83 |

Sizes move in both directions and part counts range 47–165. That is not
reproducible without eight real re-serialisations.

### The display pool at 1 of 64 is *consistent with*, and in fact predicts, 8 real round trips

`DisplayPool.claim` (wps_roundtrip.py:156-178) walks `numbers()` **in ascending
order**, takes the first free one, and on every successful claim does
`ftruncate` + `write(pid)` — which stamps that lock file's mtime. So the lock
files are a ledger of the last time each number was handed out. Live state:

```
/tmp/pptxgym-displays/display99.lock   2026-08-05 01:58:30   (retired, see below)
/tmp/pptxgym-displays/display100.lock  2026-08-05 01:58:30   (retired)
/tmp/pptxgym-displays/display101.lock  last claim (touched again 08:01 by another process)
/tmp/pptxgym-displays/display102.lock  2026-08-05 03:38:46
/tmp/.X99-lock                         2026-08-05 01:58:30
/tmp/.X100-lock                        2026-08-05 01:58:30
```

`:99` and `:100` are **permanently retired**: `occupied()` returns true while
`/tmp/.X{n}-lock` exists, and two leaked `Xvfb` processes (pids 1124894, 1124899,
started 02:15, still alive, 12m41s of CPU between them, plus two orphan `wpp`
holding `/tmp/wpsrt-psxs3caz` and `/tmp/wpsrt-cfuzplwv`) are keeping those files
in place. So during the run the pool's first free number was always `:101`.

`:102`'s last claim is **03:38:46 — before the run started**. If any two hardening
round trips had overlapped by even a second, the second one would have taken
`:102` and stamped it 06:3x. It did not. **All eight WPS round trips serialised on
`:101`.** Peak occupancy of 1 is exactly what that predicts, and the observer's own
docstring notes that a sampled peak is "a lower bound on the true peak".

Cross-check on wall clock: the 8 `hardened` completion times are 06:26:48,
06:30:41, 06:34:14, 06:34:48, 06:35:58, 06:37:53, 06:54:34, 07:08:49. With stage
durations of 50.8–81.1 s, only deck0006 and deck0010 overlap at all (≈28 s of
stage overlap), and their WPS sub-windows sit at opposite ends of it.

**Cost of that finding:** hardening cannot go faster than ~40 s/deck no matter what
`--wps-workers` says, because 62 of the pool's 64 numbers were never reachable and
the 63rd was never needed. At 100 decks that is ~1.1 h of pure WPS wall clock in
`hardened` alone. Reclaiming a display costs one `rm /tmp/.X99-lock` and one
`kill`.

---

## 2. The four numbers, per deck

`gt_roundtrip` is included because it is the fifth number that matters.

| deck | `noop` | `gt` | `gt_roundtrip` | `wrong_params` | `rename_only` | `half_restore` | `damage_untouched_gt` | stage |
|---|---|---|---|---|---|---|---|---|
| deck0002 | 0.000 | 1.000 | 1.000 | 0.000 | 0.000 | **0.493** | 0.900 | 81.1 s |
| deck0003 | 0.000 | 1.000 | 1.000 | 0.000 | 0.000 | **0.456** | 0.900 | 57.6 s |
| deck0004 | 0.000 | 1.000 | 1.000 | **0.105** | 0.000 | **0.486** | 0.900 | 72.4 s |
| deck0005 | 0.000 | 1.000 | 1.000 | 0.000 | 0.000 | **0.506** | 0.900 | 60.0 s |
| deck0006 | 0.000 | 1.000 | 1.000 | 0.000 | 0.000 | **0.474** | 0.900 | 54.2 s |
| deck0007 | 0.000 | 1.000 | 1.000 | 0.000 | 0.000 | **0.551** | 0.900 | 50.8 s |
| deck0009 | 0.000 | 1.000 | 1.000 | **0.142** | 0.000 | **0.519** | 0.900 | 51.3 s |
| deck0010 | 0.000 | 1.000 | 1.000 | 0.000 | 0.000 | **0.510** | 0.900 | 62.0 s |

`noop`, `gt`, `gt_roundtrip` and `rename_only` are exactly as specified on all 8.
`half_restore` lands 0.456–0.551 — inside the code's declared `0.35..0.65` and,
bar deck0007 at 0.551, inside the tighter 0.45–0.55 you asked about. Every
`half_restore` evidence line reports the restored pages as byte-equal to the gt,
so the residual (e.g. deck0009 restored 0.54 of the mass and scored 0.519) is the
scope penalty, not partial credit loss.

### Cells that moved since the previous `attack-report.md`

Six of the eight decks reproduce the previous run **cell for cell**, including
`half_restore` to three decimals — the comparator is deterministic across runs.
Three cells moved, all on the two decks whose recipe was regenerated by a
`repair` between the two runs (deck0004 `repair-02` at 06:49:32, deck0009
`repair-02` at 06:40:32):

| deck | cell | before | after | direction | why |
|---|---|---|---|---|---|
| deck0004 | `wrong_params` | 0.000 | **0.105** | **worse** (toward the 0.300 bar) | the repaired recipe introduced `recolor`, an op `_wrong_params` has no branch for. Evidence: `not perturbed: ['d5/recolor']`. d5's plan weight is 0.125; the attack left that component at its correct value and was paid 0.125 × (1 − scope penalty) = 0.105 |
| deck0009 | `wrong_params` | 0.000 | **0.142** | **worse** | same class: `not perturbed: ['d2/table_drop_rows']`. d2's plan weight is 0.1475 → 0.142 paid out |
| deck0004 | `half_restore` | 0.524 | **0.486** | moved toward 0.5 | different recipe, different weights (5 components re-weighted by the repair); still inside band |

Nothing regressed in the comparator. Both `wrong_params` moves are the *attack*
losing coverage, which is Finding 1.

---

## 3. The six legitimate variants

**Zero variants lost credit anywhere.** All 39 variants that were built scored
exactly `1.000` against `gt = 1.000` — not "within `VARIANT_TOL` = 0.02", exactly.
Rebuilding a shape with a new id and a stock name at the end of the z-order,
wrapping the answer in a group, dissolving a group, re-typing text with different
run boundaries (65 runs split in two on deck0002), re-inserting a picture as a new
media part, and resolving 74 theme colours to sRGB all score identically to the
untouched ground truth. On the failure class you care most about after outright
cheating, this run is clean.

The caveat is coverage, not correctness — 39 of 48 cells ran:

| deck | scored | not scored |
|---|---|---|
| deck0002, 0003, 0004, 0010 | 5/6 | `ungrouped` n/a (no damage inside a group) |
| deck0005, deck0006 | **6/6** | — |
| deck0009 | 5/6 | `picture_reinserted` n/a (no picture in the damage) |
| **deck0007** | **2/6** | `regrouped` n/a, `ungrouped` n/a, `colour_written_out` n/a, `text_retyped` **unconstructible** |

deck0007's tolerance of legitimate work is attested by `rebuilt_shapes` and
`picture_reinserted` only. Its damage is 4 × `delete` + 1 × `smartart_drop_nodes`
on four pages, so the three `n/a`s are honest. `text_retyped` is recorded as
`unconstructible` with the note "no damaged shape holds text long enough to
split", which reads like an `n/a`; the two statuses mean opposite things for
attacks (`unconstructible` = reject) and the same thing for variants (neither
rejects). That inconsistency is cosmetic today but it makes one word mean two
things in the one file an auditor reads.

---

## 4. How close did anything come?

**The highest-scoring non-`gt` candidate on every single deck is
`damage_untouched_gt` at 0.900**, and it is recorded as a pass because its
expectation is `NoGain("gt")` — `score <= gt + 1e-6`. It would pass at 1.000.

What it is: `source.pptx` with one page that the task never mentions stripped to
half its shapes and the survivors shoved an inch. What it costs: exactly 0.100,
from `SCOPE_RATES["untouched_pages_unchanged"] = (0.10, 0.30)` in
comparators.py:1741. Wrecking a second and third bystander page costs nothing
more beyond the 0.30 cap; total penalty is capped at `PENALTY_CAP = 0.50`. The
rates are labelled in-source as "a policy choice… there is nothing measured to
derive them from", which is honest — but the battery never checks the policy is
in force. If someone set the rate to 0.0, all 8 decks would still pass this row.

Everything else, with headroom to its own bar:

| deck | highest cheat below the fixed bars | bar | headroom |
|---|---|---|---|
| deck0002, 0003, 0005, 0006, 0007, 0010 | 0.000 (all eleven cheats) | 0.050 / 0.300 | full |
| deck0004 | `wrong_params` 0.105 | 0.300 | 0.195 |
| deck0009 | `wrong_params` 0.142 | 0.300 | 0.158 |

The eight `AtMost(0.05)` gates — `screenshot_paste`, `clone_spam`,
`native_to_picture`, `page_delete`, `rename_only`, `orphan_media`,
`duplicate_gt_slide` — returned **0.000, not 0.04**, on every deck where they
applied, and most of them tripped a named hard gate on the way (`gate:
no_full_page_overlay — slide 7: new picture covers 100% of the page`;
`gate: no_cloned_shapes`; `gate: native_objects_preserved`; `gate:
slide_count_and_order`). There is no cheat sitting just under a threshold.

The two 0.1x readings are **not** free money for an agent: the `wrong_params`
candidate is built from the ground truth, so the component it failed to perturb
is genuinely restored, and an agent would have to actually restore it to collect
that weight. The defect is that we do not know what a wrongly-restored table or
recolour scores, on two decks that shipped.

---

## 5. The stage's own judgement

`pipeline.harden` (pipeline.py:1631-1699) rejects on `report.reasons`, which fires
for: any comparator-rejected plan, any attack row that is `unconstructible`,
`not_run` or `error`, any attack scored `ok=False`, and any variant that is
`error` or scored `ok=False`. I recomputed that list from each `attacks.json`
independently: **all 8 recompute to `[]`, matching the stored `rejected: []`.**
The passes are earned, not defaulted.

**Is there a path where a missing `gt_roundtrip` reads clean with `--no-wps`
absent?** Inside `pipeline.harden`, no. The chain is closed:

- `build_all` skips `gt_roundtrip` entirely (attacks.py:2144).
- `wps_pass` returns `Built(..., "WPS unavailable: …")` if preflight fails and
  `Built("/dev/null", "FAILED …")` if the round trip throws; `score_all`
  (attacks.py:2194) turns both prefixes into `unconstructible`.
- `run()` `setdefault`s an `unconstructible` row for any deck `wps_pass` returned
  nothing for (attacks.py:2266).
- `unconstructible` is a rejection reason. So is `error`.

Three real gaps, none of which the 8 passes used:

1. **`names` filtering bypasses the row entirely.** attacks.py:2261 —
   `asked_for_roundtrip = names is None or "gt_roundtrip" in names`. If neither
   holds, **no row is emitted at all**, and a table with no row has nothing to
   fail. `pipeline.harden` never passes `names`, so this is unreachable from the
   pipeline; it *is* reachable from `python3 -m pptxgym.attacks --only …`, which
   is the documented way the repo-root `attack-report.md` gets regenerated. The
   belt-and-braces check at pipeline.py:1668 is guarded by `if not wps` and would
   not catch it either.
2. **`"wps": true` in `attacks.json` is the flag, not the fact.** It is literally
   `bool(wps)` from the CLI. The only artefact that proves WPS ran is the free-text
   evidence string; nothing asserts that string is well-formed, that "before" equals
   the ground truth's size, or that "parts differ" is non-zero. A future evidence
   string reading `(N -> N bytes, 0 parts differ)` would read exactly as clean as
   the eight real ones. The invariant holds today only because `roundtrip_wps`
   raises instead of returning a copy.
3. **The candidate decks are deleted** (`keep=False`, pipeline.py:1662), so
   `gt_roundtrip.pptx` cannot be re-inspected after the fact. The evidence string is
   the whole record. That is a deliberate trade (documented in the docstring) and it
   is the reason gaps 1 and 2 matter: the string is unfalsifiable after the run.

**Where the record is insufficient:** `state.json`'s `hardened` block records
`attacks: 14, variants: 6` — `len(report.rows)` and `len(report.variants)`, i.e.
rows, not executions. On the actual run those numbers should read 14/13/12 attacks
and 6/5/2 variants depending on the deck. A reader of `state.json` alone cannot
tell that deck0007 proved 2 variants and that deck0009 never fired `orphan_media`.
The information is one file away in `attacks.json`, but the stage's own summary
line over-claims, and it is the summary line the pipeline and any future dashboard
read.

---

## 6. Cost — what the minute buys

Stage durations: 50.8, 51.3, 54.2, 57.6, 60.0, 62.0, 72.4, 81.1 s (median 58.8 s).
I re-ran the non-WPS half of the battery for two decks into `/tmp` and timed each
attack:

| phase | deck0007 (1.9 MB gt, 22 slides) | deck0002 (17.5 MB gt, 22 slides) |
|---|---|---|
| `Ctx.load` + `build_plan` | 0.2 s | 0.8 s |
| build 13 attack candidates | 13.9 s | 31.6 s |
| build 6 variant candidates | 0.3 s | 4.1 s |
| score 20 candidates | 1.3 s | 8.7 s |
| **non-WPS total** | **15.8 s** | **45.2 s** |
| recorded stage total | 50.8 s | 81.1 s |
| **⇒ WPS round trip** | **≈35 s** | **≈36 s** |

That 35–36 s matches the module's own measured 38.9 s/deck serial figure. Of it,
20 s is the fixed `LOAD_WAIT` constant and roughly 15 s more is scripted
`time.sleep` around the click-type-undo-save sequence; the docstring measures the
actual CPU at 3.9 s. So **~60% of the minute is a sleep loop in front of a GUI**,
serialised onto one display number by the leak in Finding 4.

The remaining ~15–45 s is real, distinct work — it is not rebuilding the same
candidate:

```
screenshot_paste  11.8 / 19.2 s     <- one full soffice+pdftoppm render of the gt
clone_spam         0.7 /  4.2 s
native_to_picture  0.4 /  1.1 s
page_delete        0.1 /  0.8 s
rename_only        0.1 /  0.8 s
orphan_media       0.2 /  1.0 s
duplicate_gt_slide 0.2 /  0.9 s
wrong_params       0.1 /  0.8 s
damage_untouched   0.1 /  0.9 s
damage_untouched_gt 0.1 / 0.9 s
half_restore       0.2 /  0.9 s
noop / gt          0.0 /  0.0 s     (file copies)
variants (6)       0.3 /  4.1 s
```

`screenshot_paste` alone is 75% of the non-WPS time on deck0007 and 60% on
deck0002, because it renders the whole ground-truth deck to PNG. The other twelve
attacks are 0.1–4.2 s each, and what dominates *those* is re-zipping a full copy
of the package: 19 candidates × 9.4 MB ≈ 170 MB written per harden on deck0002.
So: fourteen genuinely different pieces of work, with one shared fixed cost (the
package re-serialisation) that is paid nineteen times and could be paid once.

---

## Recommendations, ranked by the standing rule

1. **Make a partially-built `wrong_params` a rejection, not a pass.** It already
   collects the list (`notes` → `not perturbed: [...]`); nothing gates on it. Add
   `recolor` and `table_drop_rows` branches (2 of the 11 ops in the corpus have
   none), and until then treat any non-empty `not perturbed` as `unconstructible`.
   deck0004 and deck0009 should not have shipped without it.
2. **Give `damage_untouched_gt` a real bar** — e.g. `AtMost(gt − 0.25)` or an
   explicit assertion that `result["penalty"] > 0`. Today the row cannot fail
   unless the comparator *rewards* collateral damage.
3. **Record executions, not rows, in `state.json`**: `attacks_scored`,
   `attacks_na`, `variants_scored`, `variants_na`. A stage whose summary says
   "14" when it ran 12 is a stage whose evidence has to be re-derived by hand,
   which is what this audit had to do.
4. **Assert the shape of the `gt_roundtrip` evidence** (before-bytes == the
   ground truth's size, after-bytes ≠ before, parts-differing > 0). It is the only
   surviving proof that WPS ran and it is currently free text.
5. **Reclaim the leaked displays** (`rm /tmp/.X99-lock /tmp/.X100-lock`, kill pids
   1124894/1124899 and the two orphan `wpp`) and add a reaper: a run that dies
   without `atexit` permanently burns a display number, and at 100 decks that is
   the difference between 1.1 h and 5 min in this stage.
