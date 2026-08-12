# Rollout plumbing — first real-machine evidence

Four rollouts of `muse-spark-1.1` against tasks `1100001`, `1100004`, `1100005`,
`1100006`, run 2026-08-03 18:19 → 20:42 UTC. Evidence read from the OSWorld
Monitor at `http://13.223.43.53:8080` (`/api/task/tasks/<id>` for the trajectory
JSON, `/task/tasks/<id>/screenshot/<file>.png` for frames), checked against the
local source of truth in
`/home/yitongli/XLANG/osworld2.0-rollout/evaluation_examples/task_class/task_110000{1,4,5,6}.py`
and `evaluation_examples/task_assets/task_110000N/`.

The four task files are byte-identical apart from `id`, `instruction`,
`__doc__` and the per-task `tests/assets/plan.json` — one shared evaluator
runtime, so any machinery finding applies to all four.

---

## Summary table

| task | setup landed | materials present | force-save evidence | evaluator return | crash / timeout |
|---|---|---|---|---|---|
| 1100001 | **yes** — WPS Presentation open on `pptx_task_1100001.pptx`, 19 slides, step-1 frame | **yes** — agent `ls pptx_task_1100001_materials/` at step 40, then reads all 4 files (steps 41–200) | **no trace observable**; WPS gone from screen by step 209, deck reopened in LibreOffice Impress. Force-save almost certainly a no-op | `result.txt` = `0.0`; full dict **not exposed by the monitor** | **agent-scaffold hang**: 289 empty `ASK_USER` steps (210→500), hit max steps. Not a task or infra crash |
| 1100004 | **yes** — WPS open on `pptx_task_1100004.pptx`, 20 slides, step-1 frame | **yes** — `ls ~/Desktop/pptx_task_1100004_materials/` at step 3, `reference-p10.png` opened at step 40 | **no trace observable**; agent killed WPS itself at steps 52/56/59 (`pkill -9 wps`, `killall -9 wps`) and never restarted it. Force-save had no WPS window to hit | `result.txt` = `0.0`; full dict not exposed | no crash, no timeout. Agent returned `DONE` at step 205 |
| 1100005 | **yes** — WPS open on `pptx_task_1100005.pptx`, 22 slides, step-1 frame | **yes** — `ls ~/Desktop/pptx_task_1100005_materials -l` and `cat p06-table.csv` at steps 4–5 | **no trace observable**; WPS killed by agent at step 87, nothing WPS-shaped on screen in the final frame. Force-save was a no-op — see §2 | `result.txt` = **`0.1588`**; full dict not exposed | no crash, no timeout. `DONE` at step 112 |
| 1100006 | **yes** — WPS open on `pptx_task_1100006.pptx`, 15 slides, step-1 frame | **yes** — `ls ~/Desktop/pptx_task_1100006_materials/` at step 3, five `eog reference-*.png` at steps 4–8; final frame shows the folder icon on the Desktop | **no trace observable**; final frame shows an empty desktop with only terminals — no WPS window. Force-save had nothing to hit | `result.txt` = `0.0`; full dict not exposed | no crash, no timeout. `DONE` at step 146 |

---

## 1. Did `setup()` land? — Yes, on all four. Confirmed visually.

`setup()` uploads `assets/init.pptx` to `/home/user/Desktop/pptx_task_<id>.pptx`,
uploads every file under `assets/materials/` to
`/home/user/Desktop/pptx_task_<id>_materials/`, apt-installs `xdotool`, and
`launch(["wpp", vm_pptx])`.

**Deck + WPS.** The step-1 screenshot of every task shows WPS Presentation
maximised, tab titled `pptx_task_<id>.pptx`, correct deck content, correct slide
count in the status bar (`Slide 1 / 19`, `1 / 20`, `1 / 22`, `1 / 15` — matching
`gt_inventory.json`'s `n_slides` of 19 / 20 / 22 / 15). No first-launch dialog
is blocking input in any of the four frames — whatever WPS shows on a cold
start had already been dismissed or did not appear.

**Materials.** Every agent found and read the folder, in the first few steps,
by name:

- 1100004 step 3: `ls ~/Desktop/pptx_task_1100004_materials/ && echo --- && ls ~/Desktop/*.pptx`
- 1100005 step 4: `ls ~/Desktop/pptx_task_1100005_materials -l`, step 5 `cat .../p06-table.csv`
- 1100006 step 3: `ls ~/Desktop/pptx_task_1100006_materials/`, steps 4–8 open five reference PNGs in `eog`
- 1100001 step 40: `ls pptx_task_1100001_materials/`; steps 41/63/96/128/170/200 open
  `reference-p04-masked.png`, `p03-Image-4.jpg`, `reference-p13.png`,
  `p04-Picture-3.emf` by path

Two independent confirmations from the pixels rather than the commands:
1100004's final frame shows `ls -lh ~/Desktop/` printing
`drwxr-xr-x 2 user user 4.0K Aug 4 02:18 pptx_task_1100004_materials` alongside
the deck; 1100006's final frame shows the `pptx_task_1100006_mater...` folder
icon and the `pptx_task_1100006.pptx` icon on the Desktop.

**No downstream conclusion is affected by missing materials.** The agents saw
everything they were given.

---

## 2. Did the force-save work? — It almost certainly never ran meaningfully, and that is the *lucky* outcome.

`evaluate()` calls `_persist_open_deck(env)`, which runs `_SAVE_SH`: apt-get
`xdotool` if absent, `xdotool search --name 'WPS'`, `windowactivate --sync`,
`key ctrl+s`, sleep 3; if that fails, `wmctrl -a WPS` plus a `pyautogui`
`ctrl+s`; then `pkill -f wpp; pkill -f wps`.

### What the monitor can and cannot tell us

The `force_save` status string is put into
`result["runtime_evidence"]["force_save"]`, and the `logger.info("task %s:
force-save -> %s")` line goes to `runtime.log`. **The monitor exposes neither.**
`monitor/main.py:596-611` reads only `result.txt` into the `result` field, and
its `runtime.log` parser (lines 542-577) extracts only `Responses: [` lines and
exit conditions — `log_data` came back
`{"agent_responses": [], "exit_condition": null, "last_message": null}` for all
four. There is no route that serves `result.json`; the route list is
`/api/tasks`, `/api/tasks/brief`, `.../screenshot/<file>`, `.../recording`,
`.../analysis`, `/api/task/<type>/<id>`, plus config endpoints. `/analysis`
returns `{"content": null}` and `/recording` 404s. **So: no direct observation
of the force-save exists anywhere the monitor reaches.** Recovering it means
reading `result.json` / `runtime.log` on the rollout server's results
directory.

### What can be established indirectly

Every agent abandoned WPS almost immediately and worked in a GNOME Terminal with
`python-pptx` instead, writing the deck directly on disk:

- **1100004** killed WPS at step 52 (`pkill -9 soffice.bin; pkill -9 wps`), step 56
  (`pkill -9 et; pkill -9 wpsoffice; pkill -9 wps; pkill -9 soffice`) and step 59
  (`killall -9 wps; killall -9 et`). WPS never came back.
- **1100005** killed WPS at step 87 (`pkill -f wps; sleep 1; ps aux | grep wps`).
  Step 91's `wps ~/Desktop/pptx_task_1100005.pptx &` was an attempt to reopen it,
  but the agent's own step-92 narration is "Navigating to slides 6, 7, 8, 10 and
  19 **in LibreOffice Impress**".
- **1100006** never named WPS at all; it used `xdg-open ~/Desktop/pptx_task_1100006.pptx`
  seven times (steps 19, 55, 111, 129, 131, 133, 136), which on this AMI opens
  **LibreOffice Impress**, not WPS.
- **1100001** used `xdg-open` on the deck at steps 180–181. Its last real frame
  (step 209) shows the deck open in a window titled
  `pptx_task_1100001.pptx - LibreOffice Impress`.

The final frames confirm the endpoint state: 1100005's and 1100006's last
screenshots show a desktop with only terminals (and a Chrome tab), no WPS window
and no WPS entry in the dock's running set; 1100004's shows three terminals and
nothing else. So at `evaluate()` time `xdotool search --name 'WPS'` had nothing
to find, `XOK` stayed `0`, and the `wmctrl` fallback sent `ctrl+s` into a
terminal.

The one positive score corroborates this. `evaluate_candidate` returns
`round(mean, 4) if passed else 0.0`, so **1100005's `0.1588` proves all five hard
gates passed**, including `untouched_slides_intact` — which compares content
keys (image sha256, normalised-text sha256, or geometry) shape by shape on every
slide the plan did not touch. The last write to that file was the agent's
`prs.save(path)` at step 106; `python-pptx` copies media parts and text runs
through unchanged, so those keys survive. Had WPS actually re-saved the deck on
top, the gate would have had to survive an office re-serialisation as well.

### Why the no-op is the lucky outcome

`gate_untouched_slides` is incompatible with any office round-trip. Measured, not
argued — running the *real* evaluator from `task_1100006.py` over
`task_1100006/assets/init.pptx` and over the same file after a single
`soffice --headless --convert-to pptx` round-trip:

```
--- init (untouched, as uploaded) ---
  n_slides: 15 gt: 15
  hard_gates: {'untouched_slides_intact': True, 'no_full_page_paste': True,
               'no_stray_additions': True, 'deck_shape_preserved': True,
               'native_objects_present': True}
  score: 0.0   mean_component: 0.0        <- correct: doing nothing is zero

--- after LibreOffice pptx round-trip ---
  hard_gates: {'untouched_slides_intact': False, ...}
  changed untouched slides: [1, 2, 4, 5, 7, 9, 10, 12]     <- 8 of 10
  score: 0.0   (forced by the gate)
```

The breakage is **text**, not images: on slide 1 both `('txt', ...)` keys change
while shape counts stay identical; the untouched slides carry 293 `txt` keys, 198
`geo`, 21 `img`. `_content_key` hashes `_text_of()`, which is exact after
whitespace collapse, and no office app guarantees identical run-splitting on
re-serialisation.

WPS is not cleared by the existing `wps-verification.md` result ("WPS changes
0.0% of shapes"). That was measured with `pptxgym`'s comparator, which applies a
`TEXT_TOL` and placeholder exemptions and which that same document records as
having "one genuine new blind spot, in placeholder *text*, affecting 40 shapes".
This gate has no tolerance at all. **I have not tested a WPS round-trip against
this gate — that is the missing experiment.**

So the design has the evaluator deliberately forcing a WPS save immediately
before a gate that a save can break. In these four rollouts the force-save
silently did nothing, so we did not find out. It is untested, in the sense the
brief expected, and it is worse than untested — the one path we most need to
verify is the one most likely to zero every task.

---

## 3. What did the evaluator return?

**The monitor exposes only the scalar.** For each task, `result.txt` held a
parseable float, which means `evaluate()` ran to completion and returned a dict
with a numeric `score` — had the runner died or `evaluate()` failed to produce
one, the monitor would show `"Result file not found"` or `"Task not completed"`
(`monitor/main.py:596-602`). The per-component breakdown, `hard_gates`,
`hard_gate_detail`, `violations` and `runtime_evidence` are all in `result.json`
next to it on the rollout server and are **not retrievable through this
monitor**.

| task | score | components in plan | what can be said about the breakdown |
|---|---|---|---|
| 1100001 | `0.0` | 23, all `restored_shape`, 11 affected pages | **A hard gate fired — observed, not inferred.** The step-209 frame's status bar reads `Slide 18 of 61` (slides 19–22 visibly blank) against a ground truth of 19. `gate_deck_shape` requires `cand["n_slides"] == gt["n_slides"]`, so the score was forced to 0 no matter what the components said. |
| 1100004 | `0.0` | 28 = 21 `restored_shape` + 6 `text_style` + 1 `diagram_nodes`, 7 affected pages | **A hard gate almost certainly fired (inference).** From step 188 the agent rebuilt the whole deck from scratch — `prs = Presentation()` on `python-pptx`'s default template, 20 title-only layouts, media re-inserted from `/tmp/media`. The final frame shows the result at 1.8 MB against a 5.57 MB `init.pptx`. `gate_untouched_slides` compares 13 untouched pages against the real deck's content keys; a from-scratch rebuild cannot match them. Which components scored is unrecoverable. |
| 1100005 | **`0.1588`** | 34 = 29 `restored_shape` + 5 `text_style`, 9 affected pages | **All five gates passed** (score > 0 implies `passed`), and the mean of the 34 component progresses was 0.1588 — i.e. the progresses summed to ≈ 5.40 out of 34. Which of the 34 scored is not exposed. The deck was intact: the final frame's own check prints `Slides 22`, matching GT. |
| 1100006 | `0.0` | 105 = 49 `restored_shape` + 28 `outline` + 28 `text_style`, 5 affected pages | **A hard gate probably fired (inference, and the most interesting case).** At step 144 the agent pressed `ctrl+s` then `ctrl+q` in LibreOffice Impress — exactly the round-trip measured above to break `untouched_slides_intact` on this deck. The agent had done real work (rebuilt the slide-6 table, restored panel-C strips, added lane labels and group colours, all verified by its own `python-pptx` checks at step 145), so 105 components all landing at exactly 0.0 would be surprising. Cannot be confirmed without `result.json`. |

Note that the evaluator's own floor normalisation is sound: the untouched
`init.pptx` scores `0.0` with all gates green in the run above, so an agent that
does nothing gets nothing.

---

## 4. Crashes and timeouts

No infrastructure failure in any of the four. All four VMs booted, `setup()`
completed, the agent ran, `evaluate()` returned a float, and the trajectory,
screenshots and result were all persisted.

**1100001 is an agent-scaffold failure, not a task failure.** At step 209 it was
mid-way through typing a `python3 << 'PY'` heredoc into the terminal. From step
210 it emitted `ASK_USER` with an **empty question string** and did so 289 times,
consuming steps 210–500 with only three non-`ASK_USER` actions in between (step
370 `ctrl+q` then `n`; step 478 an attempt to open a Terminal). Those steps have
`screenshot_file: null`, so the agent received no new observation on any of them
— it was answered each time by the canned user simulator reply ("I have no
further information to provide… DO NOT ask me any more questions") and asked
again. Status `Done (Max Steps)`, 500/500. The other three trajectories contain
zero `ASK_USER` steps.

**A second, shared hazard: the deck multiplies slides.** 1100001 ended at 61
slides (from 19); 1100004 discovered at step 50 that its deck had gone from 20 to
50 and spent steps 50–61 investigating ("Investigating why slide count increased
from 20 to 50"), at step 56 handling a WPS *unsaved-changes* dialog, and at step
167 digging through `~/.local/share/Kingsoft/office6/data/backup/`. 1100005 hit a
milder version at steps 101–102 ("slide count mismatch between Python and
Impress").

This is **not** `python-pptx`. Round-tripping each of the four `init.pptx` files
through `Presentation(p); prs.save(p)` twice leaves the slide counts exactly at
19 / 20 / 22 / 15 and the byte size unchanged. The multiplication happens when
the deck is simultaneously held by WPS (from `setup()`) and by LibreOffice
Impress (from the agent's `xdg-open`), with agents repeatedly `pkill`-ing
`soffice.bin` and then re-opening into LibreOffice's Document Recovery flow —
1100006 steps 134–135 dismiss a recovery dialog explicitly, 1100005 step 110
"handling document state after force-closing soffice", 1100004 step 200 a save
prompt on `ctrl+q`. Whichever of those writes wins, the result trips
`gate_deck_shape` and the task is unrecoverable from that point.

The enabling machinery fact: **the AMI's default `.pptx` handler is LibreOffice
Impress, not WPS.** `setup()`'s comment anticipates this ("the AMI's .pptx
handler is not guaranteed to be WPS") and works around it by calling `wpp`
directly — but nothing stops the agent from `xdg-open`-ing the same file, and all
four did. The task is nominally a WPS task; in practice three of four agents did
their verification in Impress and one never touched WPS after step 3.

---

## 5. Scores, and what each zero actually means

| task | score | classification |
|---|---|---|
| 1100001 | 0.0 | **agent-scaffold failure** (empty-`ASK_USER` loop, 290 wasted steps) compounded by a **deck-corruption gate failure** (61 slides vs 19) |
| 1100004 | 0.0 | **model failure** — rebuilt the deck from a blank template after the slide count corrupted |
| 1100005 | 0.1588 | **genuine partial credit.** The only end-to-end clean run: gates green, 34 components, ≈5.4 of them worth of progress |
| 1100006 | 0.0 | **most likely evaluator/machinery failure** — real restoration work, then a single Impress `ctrl+s` that measurably breaks `untouched_slides_intact` on this deck |

---

## 6. The single thing most likely to be broken

`gate_untouched_slides` keys on an exact hash of shape text, and `evaluate()`
deliberately forces a WPS save immediately before running it. A measured
LibreOffice round-trip of the 1100006 deck changes 8 of 10 untouched slides and
forces the score to 0 with the agent's work untouched. In these four rollouts
the force-save silently no-opped — no WPS window survived to receive the
`ctrl+s` — so the gate was never put under the stress the evaluator itself
creates. Fix the force-save without first loosening the gate's text comparison
(and measuring a real WPS save against it) and every task will start scoring 0
for a reason that looks identical to an agent that did nothing.

## Open items

- `result.json` and `runtime.log` for these four runs are on the rollout
  server's results directory and hold the whole answer to §3 and the direct
  answer to §2 (`runtime_evidence.force_save`). Fetch them off the box; the
  monitor will never show them.
- The untested experiment: a real WPS open-and-save of one of these decks,
  scored through `inventory_pptx` + `gate_untouched_slides`.
