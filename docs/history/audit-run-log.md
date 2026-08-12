# Audit — the ten-deck run that ended 2026-08-05 07:49

Read-only audit at commit `1a32068`. Evidence: `/tmp/arun.log` (147 lines),
`/tmp/arun-timeline.jsonl` (1 header + 530 samples), 150 `*.jsonl` agent logs
under `work/deck*/`, `work/deck*/attempts/*/state.json` (the per-stage state
snapshots `archive_attempt` writes), `work/deck*/retries/`, and the ten
`state.json` files. No repo file was modified.

Run boundaries, from the timeline header and the run's own lock records:

```
"started": 1785881956.35, "iso": "2026-08-05T06:19:16"
"argv": "…observe.py watch --pid 1572914 --interval 10 --out /tmp/arun-timeline.jsonl"
  roots: pid 1572914  "python -m pptxgym.cli run --workers 8 --cpu-workers 6
                       --wps-workers 2 --api-retries 3 --until packaged"
  first deck lock at 2026-08-05T06:18:44 · last sample 2026-08-05T07:49:06
```

So the run is 06:18:44 → 07:49, 1h30m. 8 packaged, 2 parked (deck0001,
deck0008). That part of `status` is correct.

---

## 1. The question that matters most: did a stage pass on an unfinished answer?

**No. Not one, in this run.** The failure is real and it is in the record — but
it happened on 08-04, before today's fix, and nothing it produced survives into
this run's output.

### Method

Two independent passes.

**(a) Every stage record that is standing now, against the log that produced
it.** For each deck × each agent stage I read the live log (`proposed.jsonl`,
`recipe.jsonl`, `reconciled.jsonl`, `solvable.jsonl`, highest `repair-NN.jsonl`)
and pulled its `type:result` record, then compared `terminal_reason` with the
status in `state.json`. All 10 decks × 5 stages:

> every live log reads `terminal_reason: "completed"`, `is_error: false`,
> `subtype: "success"`.

The stages recorded `ok` or `rejected` were all recorded off a completed
session. There is no `api_error`, `aborted_streaming`, `max_turns`,
`max_tokens` or `refusal` anywhere in the set of artefacts this run is standing
on.

**(b) Every session this run ran, whether or not it survived.** 45 sessions
have a log mtime ≥ 06:18:44. Of those, **44 ended `completed`**; the 45th is
`work/deck0008/retries/repair-03-try-01/repair-03.jsonl`, which ended

```
terminal_reason: "api_error"   is_error: true   num_turns: 40
result: "API Error: Stream idle timeout - no chunks received"
```

and was classified `infra`, moved aside, and retried — which is the machinery
working, not a stage passing. `deck0008/state.json` records `"api_attempts": 2`
on the repair record.

**(c) The pairing test on history.** `archive_attempt` copies the stage's log
*and* a snapshot of the state record it earned into the same
`attempts/<stage>-NN/` directory, so each archived directory is a matched
pair — this session, and the status it was given. I ran that pairing over all
96 archived attempts. Eleven pairs have an errored log:

| deck | attempt | log terminal | status it got | stamped |
|---|---|---|---|---|
| deck0002 | solvable-03 | api_error 403 | `failed` ("no solvability.json") | 08-04 04:34:42 |
| deck0002 | solvable-07 | api_error 429 | `infra` | 08-04 18:07:13 |
| deck0002 | solvable-11 | aborted_streaming | `ok` — **but stamped 08-04 19:11:34**, i.e. never re-marked | — |
| deck0003 | reconciled-08 | aborted_streaming | `ok` — **stamped 08-04 17:53:42**, never re-marked | — |
| deck0005 | solvable-03 | api_error 403 | `failed` | 08-04 04:34:42 |
| deck0005 | solvable-04 | api_error 403 | `needs_human` | 08-04 04:34:50 |
| deck0007 | reconciled-03 | api_error 429 | `infra` | 08-04 18:07:26 |
| **deck0009** | **solvable-03** | **api_error 403** | **`ok`, stamped 08-04 04:34:42** | **← the laundering** |
| deck0009 | solvable-04 | api_error 403 | `infra` | 08-04 16:33:00 |
| deck0009 | solvable-06 | api_error 429 | `infra` | 08-04 18:08:09 |
| deck0009 | solvable-07 | api_error 429 | `infra` | 08-04 18:08:12 |

The two `aborted_streaming` rows that read `ok` are **not** laundering: the
status timestamps predate the aborted session by hours, which means the
pipeline never got to mark them — the parent process was killed (see §5).

### The one real case, and it is the one already written down

`work/deck0009/attempts/solvable-03/` is the incident the fix commit describes,
and the artefacts are still on disk:

- `solvable.jsonl` ends `terminal_reason: "api_error"`, `api_error_status: 403`,
  `result: "Failed to authenticate. API Error: 403 Request not allowed"`,
  22 turns, $1.27.
- `solvability.json` — written 04:26, before the 403 at 04:34 — reads
  `"verdict": "solvable"`, `"leaks": []`.
- `state.json` in the same directory: `"status": "ok"`, `"verdict": "solvable",
  "leaks": 0, "steps_measured": 300, "undetermined": 0`, stamped
  `2026-08-04T04:34:42` — one second after the 403.
- The finished re-run, `attempts/solvable-05/`, reads `"verdict":
  "undetermined"` with **2 leaks**, the first being run-boundary residue in
  `ppt/slides/slide7.xml` / `slide8.xml` that "tells a solver which rows were
  highlighted on which slide".

`solvable, no leaks` → `undetermined, 2 leaks`, exactly as reported. It is
fixed: `agent._infra_failure` at 9ac93b2 (08-05 05:55) now names
`aborted_streaming`, treats any 403 as `auth_error` whatever the CLI labels it,
and reads the result record from a 32000-character tail instead of 4000. That
commit landed **23 minutes before this run started**, and this run contains no
recurrence.

deck0009 re-ran `solvable` in this run (06:46:12 → 06:53:31, completed,
`solvable / 0 leaks / 300 steps`) and packaged off that, so nothing shipped on
the laundered verdict.

---

## 2. The 13 errors, individually

First correction, and it matters for reading every number in the token report:
**`observe.deck_tokens` walks `deck_dir.rglob("*.jsonl")` and dedups by
`session_id`. It counts every session the work directory has ever held, over
three days and several runs — not this run.** I reproduced its tally exactly:

```
sessions by stage: proposed 2, recipe 14, reconciled 47, solvable 53, repair 20
                   TOTAL 136        errors 13
```

136 and 13 are lifetime figures. **This run ran 45 sessions and had 1 error.**
Same for the money: lifetime `$227.72`, of which `reconciled $76.09 + solvable
$77.13 = $153.22` — the `$153` in the summary. **This run cost `$87.85`**
(proposed $1.75, recipe $11.49, reconciled $32.66, solvable $19.48, repair
$22.48), and reconciled + solvable were `$52.14` of it.

The 13, in time order:

| # | when | deck / stage | kind | turns | cost | what happened next |
|---|---|---|---|---|---|---|
| 1 | 08-04 04:34:41 | deck0005 solvable | 403 `Failed to authenticate` | 18 | $0.71 | marked `failed` (no file written); re-run |
| 2 | 08-04 04:34:41 | deck0002 solvable | 403 | 32 | $1.73 | marked `failed`; re-run |
| 3 | 08-04 04:34:41 | deck0009 solvable | 403 | 22 | $1.27 | **marked `ok`** — the laundering above |
| 4 | 08-04 04:34:49 | deck0005 solvable | 403, 1 turn, 0.4s | 1 | $0.00 | the immediate retry, also 403 → `needs_human` |
| 5 | 08-04 18:07:13 | deck0002 solvable | 429 `session limit · resets 8:40pm` | 28 | $1.19 | `infra` (correct) |
| 6 | 08-04 18:07:19 | deck0007 **repair** | 429, 1 turn, 1.0s | 1 | $0.00 | **marked `ok`** by `_repair_one` — spent a repair slot on an outage |
| 7 | 08-04 18:07:25 | deck0007 reconciled | 429, 1 turn | 1 | $0.00 | `infra` (correct) |
| 8 | 08-04 18:08:09 | deck0009 solvable | 429 | 21 | $1.78 | `infra` |
| 9 | 08-04 18:08:12 | deck0009 solvable | 429, 1 turn | 1 | $0.00 | `infra` |
| 10 | 08-05 02:05:13 | deck0002 solvable | `aborted_streaming` | 11 | $0.32 | never marked — process killed |
| 11 | 08-05 02:05:13 | deck0003 reconciled | `aborted_streaming` | 45 | $1.69 | never marked — process killed |
| 12 | 08-05 02:05:13 | deck0008 **repair** | `aborted_streaming`, 27.6s | 10 | $0.22 | never marked, **but `repair-01.jsonl` stayed in the deck root** |
| 13 | **08-05 07:12:19** | **deck0008 repair** | `api_error` "Stream idle timeout" | 40 | $2.85 | **the only one in this run** — retried, recovered |

Three of these are findings in their own right:

**#6 and #12 — an outage spent a repair budget.** `pipeline.repairs_done`
counts `repair-*.jsonl` in the deck root. Error #12 is a 27-second aborted
stream that did nothing, and `work/deck0008/repair-01.jsonl` is still sitting in
the deck root, so it permanently counts as 1 of deck0008's `MAX_REPAIRS = 3`.
deck0008's park line reads `PARKED after 3 repair attempts — needs a human`;
one of those three was 27 seconds of weather. Same shape for deck0007
(`repair-02.jsonl`, a 0.98-second 429 that `_repair_one` recorded as `ok` and
then used to invalidate downstream stages and retire the verdict that had
ordered the repair). deck0007 packaged anyway; deck0008 did not. Both predate
9ac93b2, which added the `infra` early return to `_repair_one` — but the orphan
logs it left behind still count today, and no cleanup removes them.

**#10–#12 all carry the same second, 02:05:13.** Three different decks, three
in-flight streams, one timestamp: the previous run's process was killed. That
is corroborated by the state snapshots — `deck0003/attempts/reconciled-08/state.json`
still reads the *17:53:42* record, i.e. the 02:05 session was never marked at
all.

### The retry machinery, checked against its own spec

Error #13 is the only in-run infra failure and it exercised every part:

| claim | evidence |
|---|---|
| backoff `min(30*attempt, 120)` | attempt 1 log mtime **07:12:19**; the observer's three "hung" samples at 07:12:26 / :36 / :46 show the stage still held; attempt 2 ran 07:12:49 → 07:26:44. Stage total 1647s ≈ 776s + **30s** + 835s. Attempt 1 → 30s, as specified. |
| logs preserved under `retries/` | `work/deck0008/retries/repair-03-try-01/` holds `repair-03.jsonl` (3.96 MB) and `repair-03.stderr.log`. Nothing was left in the deck root, so `repairs_done` was not inflated. |
| `api_attempts` recorded | `deck0008/state.json` → `repair: {"api_attempts": 2, …}`; `status` printed `1 deck(s) needed an API retry … deck0008×2`. Correct. |
| half-written artefacts moved aside | **not exercised, and it could not have been.** `_repair_one` (cli.py:872) builds `AgentRun("orchestrator", …, max_turns=60)` with **no `outputs=`**, so `_keep_attempt` has nothing to move — the guarantee in the `AgentRun.outputs` docstring does not apply to the repair stage. I checked the dead attempt's 3.96 MB log for `Write`/`Edit`/redirect calls: 13 tool calls, all read-only inspection plus a `/tmp/d8` scratch dir. Nothing was half-written this time. But repair is the one stage whose entire job is rewriting `proposal.json` / `recipe.json` / `repair.md`, and it is the one stage that names no outputs. |

`_retries_allowed` also behaved: #13 was labelled `api_error` (not a 403), so it
got the full budget; the 403s at 04:34 got the `AUTH_RETRIES = 1` cap (error #4
is that single retry, 0.4s, and then it stopped).

---

## 3. The repair rounds

**The summary's "4 repairs" is 4 *decks*, not 4 rounds. Six repair rounds ran**
(seven sessions counting the retry), and I can reconcile the reported timings:
the observer is summing per deck.

```
per-deck repair time   deck0001 278s   deck0009 717s   deck0004 760s (551+209)   deck0008 2351s (703+1648)
median of the four     (717+760)/2 = 738.5s = 12m19s      ← reported "median 12m27s"
largest                2351s = 39m11s                     ← reported "longest 39m26s"
```

So "longest 39m26s" is deck0008's **two** repair rounds plus nothing in between
being subtracted — no single repair round took 39 minutes. The longest single
round was deck0008's third, 27m28s, and 13 of those minutes were the failed API
attempt and its backoff.

| # | deck / log | window | work order (`rejected_by`) | what the repairer changed | the next gate said |
|---|---|---|---|---|---|
| 1 | deck0004 `repair-01` | 06:25:00–06:34:12 (551s, 57 turns, $3.75) | `plan.json:rejected` → re-run from `['recipe']` | `recipe.json`: left d5's three broken `set_font` steps alone and **added** a `recolor` step on slides 14/16/18 so d5 gains three scoreable components. "a scratch rebuild of the plan comes back with `rejected: []`, d5 weight 0.125, GT 1.000 / broken 0.000" | degrade `gate=ok` (31 changes / 7 slides), reconcile **ready** — then `solvable` said **ambiguous** (1 undetermined). Complaint fixed; a different gate below it failed. |
| 2 | deck0009 `repair-02` | 06:28:28–06:40:25 (717s, 60 turns, $4.08) | `plan.json:rejected` → `['recipe']` | `recipe.json`: replaced d2's two whole-shape `set_font(bold/underline=false)` steps on slides 7/8, "whose floors were 0.55/0.65 because a uniform…" | **accepted all the way**: reconcile ready, solvable solvable, `24 component(s) gt=1.000 input=0.000`, 14 attacks, `consistency=ok`, packaged. |
| 3 | deck0008 `repair-02` | 06:35:51–06:47:34 (702s, 62 turns, $4.63) | `solvability.json:leaked` → `['materialise','proposed']` | `proposal.json`: added an instruction clause, **re-costed hard/350 → medium/197**, dropped the redundant `p11-table.csv`, converted an unproducible icon request into an "ungraded" decision. Its own reply: *"but I do **not** expect it to pass: rework [1], the `ppt/media/image3.png` leak, cannot be closed"* | reconcile **ready** at est_steps 197, `instruction_changed: False` — then `solvable` said **leaked**, 2 leaks, again. |
| 4 | deck0001 `repair-03` | 06:43:26–06:48:04 (278s, 25 turns, $1.56) | `task.json:needs_rework` → `['materialise']` | `proposal.json`: slide 1's asset request changed from the unproducible `image_assets` to `image` ("dry-run confirms the extractor s…") | reconcile **ready** (was `needs_rework` — the complaint *was* addressed) — then `solvable` said **leaked**, 1 leak. → PARKED. |
| 5 | deck0004 `repair-02` | 06:45:58–06:49:27 (209s, 27 turns, $1.14) | `solvability.json:ambiguous` → `['materialise']` | `proposal.json`: the slide-12 instruction sentence now pins the three measurement images to the set slides 6 and 7 use | **accepted all the way**: reconcile ready, solvable solvable, `25 component(s) gt=1.000` (6 unscoreable), packaged, `consistency=ok`. |
| 6 | deck0008 `repair-03` | 06:59:07–07:26:35 (1647s incl. retry, 57 turns, $4.47 + $2.85 dead) | `solvability.json:leaked` → `['materialise','recipe']` | `recipe.json` + `proposal.json`: added a `delete` of slide 3's "Picture 2" so **both** references to `ppt/media/image3.png` go; replaced the leaking image asset with an unmasked slide-5 render. Declared steps **197 → 228**, d1 90 → 115, d5 12 → 18 | reconcile ready, **`solvable` = solvable, 0 leaks — the leak was actually closed** — then `scored` **REJECTED**. → PARKED. |

### Did any repair change nothing, or shrink the task?

**Changed nothing: no.** All six edited a named upstream artefact, and the
change is independently visible: each one that touched `proposal.json` made
`recipe` go stale through `STAGE_INPUTS` and re-run on its own (deck0001
06:48:10, deck0004 06:49:32, deck0008 06:47:41 and 07:26:44), and each one that
touched `recipe.json` moved the degrade footprint.

**Shrank the task: one, and the pipeline caught it.** Round 3 (deck0008
`repair-02`) is the case the red lines exist for — it re-costed the task from
`hard / 350 steps` to `medium / 197 steps`, dropped an asset, and *by its own
statement* did not close the leak that ordered the repair. The next gate
rejected it (`leaked`, 2 leaks), so nothing shipped. Round 6 then did it
properly, and left an unusually good audit trail in `work/deck0008/repair.md`
under a heading literally called **"Why this is not shrinking the task"**:

> - **Nothing was dropped.** All five degradations survive on the same objects.
> - **The task got bigger, in the two places the rework's own option [2] would
>   have made it smaller.** Sites: 5 → 5, but slides touched 6 → 7; declared
>   steps 197 → 228; d1 90 → 115; d5 12 → 18. […] That trade is dilution, and
>   I did not take it.
> - **The leak that made d1 answerable in ~15 steps is gone**, so the honest
>   cost of this deck rises by far more than the 31 declared steps

and a "What is not fixed" section that flags the one clause of the work order
it deliberately did not satisfy. That is the behaviour you want; it is worth
knowing it happened.

**No repair edited the shared tools.** `revert_tool_changes` never fired — the
string `STOPPED — the repair edited` does not appear in `/tmp/arun.log`, and
there are no `*-tool-change.diff` files under `work/`.

### Two decks were parked on things a repairer cannot fix

- **deck0008 was ultimately parked by a comparator bug, not a deck defect.**
  After round 6 closed the leak, `scored` rejected with (state.json, `scored.problems`):

  > `coherence: media_not_pasted fires on 'ground_truth', which is correct work:
  > 2 original media part(s) pasted back` — and the same line for `half_restore`,
  > `rebuilt_by_hand` and `over_eager`; plus `coherence: over-eagerness alone
  > zeroes the score; a scope violation must cost a fraction, never everything`
  > … `the ground truth scores 0.000, not 1.000`

  Every probe including ground truth fails the check. That is `comparators.py`
  disagreeing with a legitimate repair strategy, and the repair skill's own red
  line ("Change what is broken, not the comparator") means no repairer is
  allowed to fix it. The deck burned its whole budget and parked on it.

- **deck0001 was parked on a leak that lives in the pipeline's own package
  builder.** The park reason: *"Strip or regenerate `docProps/thumbnail.jpeg`
  when building input."* `pkg_check.ALWAYS_KEEP` deliberately preserves
  `docProps/thumbnail.jpeg`, and no module strips it — I confirmed the part is
  present in `deck0001/input.pptx`, `deck0002/input.pptx` and
  `deck0006/input.pptx`. It is only a *leak* when slide 1 is degraded, and
  deck0001's d5 is on slide 1 while deck0002 and deck0006 degrade no slide 1 —
  so the probe was right in all three cases and the two packaged decks are
  clean. But the fix is a one-line change to the builder that a repairer is not
  permitted to make, so the loop spent three rounds and a park on it.

---

## 4. The stage arithmetic — `reconciled ×9`, `solvable ×10`, `proposed ×1`, `recipe ×3`

That table is `_models_used` (cli.py:1211), which iterates
`deck.state().items()` and counts **one record per stage per deck** — the
surviving one — and only where `"model_asked" in rec`, a field that did not
exist before 9ac93b2 at 05:55 today. So the table means *"how many decks have a
current record for this stage that was made in this run"*, and it silently
drops every superseded re-run. It is not a session count.

Read that way it is exact:

- `proposed ×1` — only **deck0001** re-proposed (06:18:44 → 06:24:54). The other
  nine still carry their 08-03 03:56:38 proposal records.
- `recipe ×3` — **deck0001, deck0004, deck0008**, the three decks whose repairs
  edited `proposal.json` and so invalidated the recipe.
- `reconciled ×9` — nine decks re-reconciled. The one that did not is
  **deck0002**, whose reconcile completed at `02:04:03` in the previous run;
  it resumed this run at `solvable`.
- `solvable ×10` — all ten.

The **session** counts, which are the ones that cost money, are higher. Three
independent sources agree — `/tmp/arun.log` output lines, my segmentation of the
530 timeline samples, and the 45 in-window logs:

| stage | decks (the table) | sessions actually run | in-run cost |
|---|---|---|---|
| proposed | 1 | 1 | $1.75 |
| recipe | 3 | 6 | $11.49 |
| reconciled | 9 | **18** | $32.66 |
| solvable | 10 | **13** | $19.48 |
| repair | 4 | 6 (+1 dead retry) | $22.48 |
| | | **45** | **$87.85** |

Every one of those re-runs has a reason I can point at, except three:

**Explained.** Repairs invalidate downstream stages, and every re-run below a
repair is accounted for by `invalidate_from` plus `STAGE_INPUTS` staleness —
e.g. deck0004 ran reconcile at 06:18:52, 06:34:22, 06:55:41, 07:01:12: initial,
after `repair-01`, after `repair-02` re-ran the recipe, and once more after
materialise. deck0008 ran reconcile five times across two repair rounds. All
visible in `/tmp/arun.log` and the timeline.

**Not explained by any record — wasted:** *three reconcile sessions ran on decks
that had already been parked.* The proof is in the state snapshots, which record
what the stage's status was at the moment it was told to run again:

```
work/deck0001/attempts/reconciled-05/state.json  "status": "needs_human", at 2026-08-05T07:08:50
work/deck0001/attempts/reconciled-06/state.json  "status": "needs_human", at 2026-08-05T07:14:26
work/deck0008/attempts/reconciled-06/state.json  "status": "needs_human", at 2026-08-05T07:41:19
```

The cause is in `cli.py:1037-1047`. When `repairs_done >= MAX_REPAIRS`,
`_repair_one` marks `reconciled` as `needs_human` and prints `PARKED`. Control
returns to the loop, which then runs `for s2 in STAGES[recipe:]` — and
`reconciled` is not `promoted`, so it runs again and **overwrites the
`needs_human` mark with `rejected`**. The `if … == "needs_human": break` guard
on the next line therefore never sees it, and the loop goes round again. In
`/tmp/arun.log` this is visible as the same block printed twice:

```
 90   deck0001  PARKED after 3 repair attempts — needs a human
 92   deck0001  {'assets': 2, 'verdict': 'needs_rework', …, 'est_steps': 200}
 93-96 deck0001  skipped — not reconciled / not through the solvability gate / …
 97   deck0001  PARKED after 3 repair attempts — needs a human
 98   deck0001  {'assets': 2, 'verdict': 'needs_rework', …, 'est_steps': 200}
 99-102 …
```

Cost: deck0001 07:08:50–07:14:18 ($1.88) and 07:14:26–07:20:37 ($1.99),
deck0008 07:41:19–07:48:25 ($2.34). **$6.21 and 19 minutes on decks already
declared a human's problem** — and the last 8 minutes of the run were nothing
else. On a 450-deck batch at the same park rate that is real money, and it
scales with `MAX_REPAIRS`.

---

## 5. `/tmp/arun.log` and the timeline — anything that reads like a defect

**No crashes.** No traceback, no `BUSY —`/`DeckBusy`, no `TIMEOUT`, no
`INFRA`/`TRUNCATED` stage line, no `STOPPED — the repair edited`, no refusal, no
`crashed` status anywhere in the ten `state.json` files. `grep -l -iE
"traceback|Error:|DeckBusy" work/deck*/*.stderr.log` returns nothing.

**Latent, one turn away from firing.** `_repair_one` maps the agent result with
`{"timeout": "failed", "infra": "infra"}.get(res["status"], "ok")` (cli.py:895).
A `truncated` status — which is what `_infra_failure` returns for `max_turns`,
`max_tokens` and `refusal` — falls into the default and is recorded **`ok`**,
after which the function falls through to invalidate downstream stages and
retire the verdict. `_agent_stage` handles `truncated` correctly (cli.py:522);
`_repair_one` does not. Repair runs at `max_turns=60`, and this run's repairs
reported 57, 60, 62 and 57 turns — the tightest ceiling of any stage. Nothing
terminated on `max_turns`, so it did not fire, but it is the same class of bug
as the one just fixed and it lives in the same function.

**A false "hung" during the backoff.** The observer's only three `hung` records
are:

```
{"id":"deck0008","stage":"repair","why":"no log output for 1484.9s","since_s":798.9}   07:12:26
{… 1495.0s … 808.9}                                                                     07:12:36
{… 1505.0s … 819.0}                                                                     07:12:46
```

The log age (24m45s) exceeds the age of the stage itself (13m19s), which is
impossible. The sample shows why: `"log": "repair-02.jsonl"`. During the 30-second
backoff, `_keep_attempt` had already moved `repair-03.jsonl` into `retries/`, so
the observer picked the newest remaining `repair-*.jsonl` — the previous
round's — and reported its age. A retry always looks like a 20-plus-minute
stall. The accompanying `agent locks say 2 running, 1 claude process(es)
attributed` on those same three samples is the same event seen from the process
side.

**Two persistent observer warnings**, both benign: `2 claude process(es) on this
box are not part of the run and were not counted` on all 530 samples (other
sessions on the machine), and `no pptxgym process visible in /proc` on the last
4 samples (the run had exited).

**Quality flags that were carried through anyway** — not defects in the run, but
they shipped:

- `deck0003  task_755e402204c5  9 component(s)  consistency=suspect  (2 warn)`
  and `deck0005  task_de4bf5d6c094  34 component(s)  consistency=suspect
  (1 warn)`. Both are `deg_slide_without_delta` — "d2 lists slide 1 but nothing
  on that slide changed", whose own evidence field says *"benign when the slide
  is named as the surviving reference; a defect when the instruction tells the
  solver that slide is damaged"*. Nobody adjudicated that; `packaged` accepted
  `suspect` without comment.
- Unscoreable components passed silently: `deck0006  98 component(s) … (7
  unscoreable)`, `deck0004  25 component(s) … (6 unscoreable)`.
- Four decks reported `(1 unmet)` assets at materialise.
- `work/emitted/task_class/` holds **9** files for 8 packaged decks;
  `task_1100013.py` is a stray from 05:34, before the run.
- `work/deck0008/repair.md`, written by an agent in this run, contains a Chinese
  string (`图表重建`) — the commit before this run was `chore: the last Chinese
  string, now that cli.py is free`. Generated artefacts are outside that
  cleanup's reach.

---

## 6. What the record cannot answer

Three things, stated plainly.

1. **`/tmp/arun.log` has no timestamps and no stage names.** Every line is
   `  deckNNNN  <detail>`; I checked the raw bytes with `cat -v` for stripped
   escapes and there are none. Which stage produced a line is inferable only
   from the shape of the detail dict, and *when* it happened is not in the file
   at all. Every timing in this report comes from the timeline or from file
   mtimes, not from the run's own log. For a 450-deck run that is not going to
   be recoverable.

2. **`state.json` keeps one record per stage, so the run's history is only as
   good as `attempts/`.** It held up here — `archive_attempt` writing a state
   snapshot next to the artefacts is what made §1(c) and the parked-deck finding
   possible, and it is the single most valuable thing in the work directory.
   But 46 of the 96 archived snapshots are `{}` (the state had already been
   popped by `invalidate_from` when the archive was taken), so for those
   attempts the artefacts survive and the verdict they earned does not.

3. **Why nine decks entered this run needing a fresh reconcile, I cannot show
   from the record.** The state that would say so was overwritten at 06:18:44,
   and there is no log from the previous run on disk. The best-supported
   inference — three streams dying in the same second at 02:05:13, `scored`
   having been added to the pipeline at 01:14 that morning, and every deck
   carrying an `attempts/scored-01/` whose state snapshot is `{}` — is that the
   02:0x run reached the new `scored` gate on all ten decks, entered repair
   loops that called `invalidate_from`, and was then killed. That is an
   inference, not a record.

---

## Bottom line

- **No stage in this run passed on an unfinished answer.** 44 of 45 sessions
  ended `completed`; the 45th was correctly caught as `infra`, moved to
  `retries/`, backed off 30s and retried, and the recovery is recorded as
  `api_attempts: 2`. The failure mode described in the brief is real and I found
  it in the archive — `deck0009/attempts/solvable-03`, 08-04 04:34, `solvable /
  no leaks` from a session killed by a 403, where the finished re-run read
  `undetermined / 2 leaks` — and 9ac93b2 closed it 23 minutes before this run
  began.
- **The retry machinery behaved exactly as designed**, with one gap: the repair
  stage names no `outputs`, so the "half-written artefact moved aside"
  guarantee does not cover the only stage whose job is writing artefacts. It
  did not bite this time (the dead attempt made 13 read-only calls).
- **The 13 errors are lifetime, not this run.** So are the 136 sessions and the
  $227.72. This run: 45 sessions, 1 error, $87.85.
- **Six repair rounds, not four.** Two accepted straight through, one was
  rejected for shrinking the task and the gate caught it, one closed its leak
  and then hit a comparator bug, two decks parked on defects that live in
  `comparators.py` and `pkg_check.py` and that no repairer is allowed to touch.
- **The clearest waste is the park loop**: `reconciled` re-running on decks
  already marked `needs_human`, because the re-run overwrites the mark the break
  condition is looking for. $6.21 and 19 minutes here; it scales with the park
  rate and with `MAX_REPAIRS`.
