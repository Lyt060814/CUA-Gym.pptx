# Does the harness accept what `emit` generates?

`emit.py` already proved the *scoring* half: the generated file scores the
ground truth 1.000 and the damaged file 0.000, executed with `python-pptx`
blocked, so the stdlib-only claim holds. What was never checked is the other
half — whether the benchmark can **load and run** the thing. That half was
satisfied by imitation: `tests/test_emit.py` stubbed `BaseTask` as `object`,
which means every assertion about class attributes was made against a class
the benchmark has never seen.

This document is that half, done for real, against
`/home/yitongli/XLANG/osworld2.0-rollout` (read-only) with
`/home/yitongli/XLANG/OSWorld-V2`'s `task_077` / `task_087` / `task_090` as
the authority above it.

The task under test was emitted from `work/deck0002` as `task_9900001`
(28 components).

---

## 0. What the harness actually does with a task

Read rather than guessed, because two of the four contract violations below
are invisible from an example:

| step | code | what it does |
|---|---|---|
| locate | `task_loader.find_task_class_path` | `<root>/task_class/<domain>/task_<id>.py`, then `<root>/task_class/task_<id>.py` |
| import | `task_loader._load_task_module` | `spec_from_file_location` on the `.py`, no package context |
| instantiate | `_instantiate_task_from_module` | `get_task()` → `TASK_CLASS` → `Task` → any real `issubclass(BaseTask)` |
| read fields | `DesktopEnv._task_get` | **`task_config.get(key, default)`** — a *dict* lookup |
| dispatch setup | `DesktopEnv._call_task_setup` | inspects the signature; passes `use_proxy=` only if the parameter exists |
| dispatch eval | `DesktopEnv._call_task_evaluate` | `eval_fn(self)`; `dict` returned as-is, anything else `float()`-ed |
| persist | `lib_run_single._persist_evaluation_result` | `float(result["score"])` → `result.txt`, whole dict → `result.json` |

**The one that matters.** `BaseTask` subclasses `dict`, and `__init__` copies
exactly the names in `BaseTask._fields()` into the dict. `_task_get` prefers
`.get`. So a class attribute **outside that list is invisible to the runner**
no matter how it is spelled — `fixed_ip` and `possibility_of_env_change` are
carried by every shipped WPS task and read by nothing. Conversely a field
*inside* the list that the task does not set silently takes the `BaseTask`
default, which is how `intermediate_eval_safe` was quietly wrong.

---

## 1. It imports and instantiates against the genuine `BaseTask`

```
find_task_class_path("9900001", base_dir=out, domain="tasks")
    -> /tmp/hcheck/out/task_class/task_9900001.py
load_task_from_file(...)            -> Task9900001
isinstance(task, BaseTask) = True   isinstance(task, dict) = True
mro: ['Task9900001', 'BaseTask', 'dict', 'object']

_has_custom_setup     = True
_has_custom_evaluate  = True
setup    signature = (setup_controller: 'SetupController', use_proxy: bool = False) -> None
evaluate signature = (env: 'DesktopEnv') -> dict[str, Any]
_call_task_setup will pass use_proxy as a keyword: True
```

Every key the runner reads, fetched through `DesktopEnv._task_get` itself
(values after the fixes in §3):

| key | value | type | who reads it |
|---|---|---|---|
| `id` | `'9900001'` | str | `_set_task_info` → `os.path.join(cache_dir_base, id)` — a non-str crashes the reset |
| `instruction` | `'You are getting this wearables talk …'` | str | `run.py: example["instruction"]` (subscript, not `getattr`) |
| `config` | `[]` | list | `_set_task_info`; also the `BaseTask.setup` fallback we override |
| `proxy` | `False` | bool | `reset()` proxy negotiation |
| `disable_vnc` / `disable_recording` | `False` | bool | `_apply_task_runtime_overrides` |
| `intermediate_eval_safe` | `False` | bool | `lib_run_single._task_allows_intermediate_eval` |
| `snapshot` | `'wps'` | str | image selection (runner-side) |
| `volume_size` | `60` | int | `DesktopEnv(volume_size=)` |
| `platform` | `'linux'` | str | runner-side |
| `related_apps` | `['wps']` | list | `monitor/main.py` listing |
| `source` / `trajectory` | `'pptxgym/deck0002'` / `'trajectories/'` | str | listing |
| `user_simulator` / `evaluator` | `None` | — | `_set_task_info`; non-None would divert scoring to the config evaluator |

`instance_type` and `image` are `None`, matching every reference task.

The result dict survives the runner's persistence path unchanged:
`_persist_evaluation_result` → `1.0`, `result.txt` = `1.0`, `result.json`
parses (the runner dumps with `default=str`, so the evidence block is safe
whatever ends up in it).

---

## 2. Field-by-field against `task_1170003` and `task_087`

`*` marks a difference; the last column says whether it matters.

| attribute | ours (after fix) | `task_1170003` | `task_087` | matters? |
|---|---|---|---|---|
| `id` | `'9900001'` | `'1170003'` | `'087'` | * per-task by construction; must equal the filename suffix or `find_task_class_path` misses |
| `snapshot` | `'wps'` | `'wps'` | `'ubuntu'` | **was missing — fixed.** See §3 |
| `instruction` | generated + suffix | per-task | per-task | * per-task |
| `source` | `'pptxgym/deck0002'` | *absent* | `''` | * no — provenance only, read by `monitor` for display |
| `trajectory` | `'trajectories/'` | same | same | matches |
| `related_apps` | `['wps']` | `['wps']` | `['wps']` | **was `['wps_office']` — fixed.** See §3 |
| `platform` | `'linux'` | same | same | matches |
| `proxy` | `False` | same | same | matches |
| `fixed_ip` | `False` | same | same | matches — but outside `_fields()`, so decorative in all three |
| `possibility_of_env_change` | `'low'` | `'low'` | *absent* | no — outside `_fields()`, read by nothing |
| `intermediate_eval_safe` | `False` | `False` | *absent* | **was missing — fixed.** See §3 |
| `volume_size` | `60` | `60` | *absent* | **was missing — fixed.** See §3 |
| `WEIGHTS` / `DESCRIPTIONS` | dicts | — | — | ours only; class attrs, reachable by normal lookup, never by `_task_get` |

`task_087` omits `intermediate_eval_safe` and `volume_size` — it predates
both. The 117 series is the current shape and is what we match.

### `metadata.json`

Nothing in the benchmark reads `task_assets/*/metadata.json` (grepped:
only `scripts/scaling/domain4/export_tasks.py` *writes* it, and a handful of
tasks read their own). So none of these differences can break a run; they are
differences in what a reviewer is told.

| key | ours | `task_1170003` | matters? |
|---|---|---|---|
| `instruction` | **now the instruction the agent receives, suffix included** | *absent* | **was the pre-suffix instruction — fixed.** A reviewer was approving a task on a description missing its own constraints |
| `input_file` | `/home/user/Desktop/task_9900001.pptx` | same shape | added — where the deck lands |
| `requires_image` | `'OSWorld Linux WPS snapshot'` | `'OSWorld V2 Linux WPS snapshot'` | added — which image to boot |
| `evaluator` | `'pptxgym.delta-derived.v1'` | `'domain7.chart-…-v1'` | added — one source of truth with the emitted `EVALUATOR_ID` |
| `environment` | `'WPS Presentation'` | same | added |
| `related_apps` | `['wps']` | *absent* | consistent with the class now |
| `domain` | `'office'` | `'07_chart_degradation'` | no — free-form; the runner's domain comes from the meta JSON key, not from here |
| `difficulty` / `est_steps` / `components` / `source_deck` | ours | `difficulty` / `estimated_gui_actions` / `mutation_count` / `original_task_id` | no — same information, different vocabulary |
| `source_url`, `source_license` | *absent* | `doi.org/…`, `cc0-1.0` | **no, but worth adding**: the corpus is Zenodo10K and its licences are filtered by us, so the licence belongs with the task |
| `full_credit_contract`, `affected_slides`, `mutation_families` | *absent* | present | no — documentation |

### Directory layout

```
ours                          task_1170003
  README.md                     README.md
  metadata.json                 metadata.json
  assets/init.pptx              assets/init.pptx
  assets/materials/…            —
  tests/assets/plan.json        tests/assets/mutation_plan.json
  tests/assets/gt_inventory     tests/assets/gt_ooxml_inventory.json
  tests/assets/init_inventory   tests/assets/init_ooxml_inventory.json
  —                             asset_manifest.json
  —                             instruction.txt
```

The layout the evaluator resolves against matches exactly: ours computes
`TASK_DIR.parent / "task_assets" / f"task_{id}"`, the reference computes
`TASK_CLASS_DIR.parent / "task_assets"`. `asset_manifest.json` (per-file
sha256) and `instruction.txt` are conventions of the 117 packaging that
nothing reads; ours pins `INIT_SHA256` inside the task file instead, which is
strictly stronger for the one file that is uploaded but leaves `tests/assets/`
uncovered. Not fixed — flagged.

---

## 3. Contract violations found, and fixed

All four were in `pptxgym/emit.py`, all four in the class header.

1. **`intermediate_eval_safe` defaulted to `True`.** This is the one that
   could have cost the rollout outright. `evaluate()` force-saves and then
   `pkill`s `wpp`/`wps`/`soffice`.
   `lib_run_single._run_inline_checkpoint_eval` calls the task's own
   `evaluate` at whatever steps `--checkpoint_eval_mode` names, and honours
   exactly one flag before doing so — this one. Left at the `BaseTask`
   default, the first checkpointed evaluation closes the application the
   agent is working in and every subsequent step acts on an empty desktop.
   All thirteen validated 117 tasks set it `False`. Now `False`.

2. **`snapshot` was unset (`''`).** No runner *in the benchmark repo* reads
   it — `run_multienv.py` takes the AMI from `IMAGE_ID_MAP[REGION]` keyed by
   screen size — but it is the only field on a task that says which image it
   needs, every shipped WPS task sets it, and `''` names no image at all. If
   the rollout server keys on it, an unset value is a boot onto the wrong
   image and a wasted run. Now `'wps'`.

3. **`related_apps` was `['wps_office']`.** That token appears in zero of the
   177 shipped tasks; the convention is `['wps']`. Only `monitor/main.py`
   reads it, so this is a value-domain divergence rather than a crash — but a
   value domain nobody else uses is exactly what a filter will silently drop.
   Now `['wps']`.

4. **`volume_size` was unset.** The 117 series asks for 60 GB; the provider
   default is 30 (`resolve_aws_root_volume_size`, `default_size=30`, raised
   only to the AMI's own minimum). Now `60`.

Plus one that is not a harness contract but is a correctness bug:

5. **`metadata.json`'s `instruction` was the pre-suffix text**, so the file a
   reviewer reads omitted "work in WPS Presentation only", "save in place at
   `<path>`" and "do not rename or move it" — the three sentences the module
   docstring says each cost a real rollout. Now it is the exact string the
   class carries.

`EVALUATOR_ID` also moved from a literal buried in the template to a module
constant used by both the template and the metadata, so the two cannot drift.

Nothing was changed in `pipeline.py`, `cli.py`, `comparators.py`,
`attacks.py`, `consistency.py`, in `work/`, or in either benchmark checkout.

---

## 4. `setup()` against a recording controller

`DesktopEnv._call_task_setup(holder, task, use_proxy=False)` — the harness's
own dispatch — driven into a `FakeController` that records instead of
touching a machine.

```
execute  'rm -f /home/user/Desktop/task_9900001.pptx'                     shell=True
execute  'mkdir -p /home/user/Desktop/task_9900001_materials'             shell=True
_upload_file_setup
    init.pptx            -> /home/user/Desktop/task_9900001.pptx
    p06-Picture-12.jpeg  -> /home/user/Desktop/task_9900001_materials/p06-Picture-12.jpeg
    p06-Picture-17.tiff  -> /home/user/Desktop/task_9900001_materials/p06-Picture-17.tiff
    p06-Picture-4.jpeg   -> /home/user/Desktop/task_9900001_materials/p06-Picture-4.jpeg
    p06-Picture-6.jpeg   -> /home/user/Desktop/task_9900001_materials/p06-Picture-6.jpeg
    reference-p19.png    -> /home/user/Desktop/task_9900001_materials/reference-p19.png
execute  'xdg-mime default wps-office-wpp.desktop
          application/vnd.openxmlformats-officedocument.presentationml.presentation
          2>/dev/null || true'                                            shell=True
launch   ['wpp', '/home/user/Desktop/task_9900001.pptx']

ANSWER KEY IN THE UPLOAD LIST: none
evaluator reads:  TEST_ASSETS/plan.json, TEST_ASSETS/gt_inventory.json,
                  TEST_ASSETS/init_inventory.json
setup uploads:    AGENT_ASSETS/init.pptx, AGENT_ASSETS/materials/*
```

The inversion is intact and it is the load-bearing one: a delta-derived
evaluator **must** read the ground truth, so the usual "the evaluator must not
touch test fixtures" rule cannot apply. What replaces it runs the other way —
nothing the evaluator reads may reach the machine the agent works on — and
that is a property of the *upload list*, which is what is asserted here (the
existing `check_package` test asserts a property of the source text, which is
not the same thing).

Signature note: `execute(command=<str>, shell=True)` passes a string where the
annotation says `List[str]`. `_execute_setup` handles both
(`" ".join(command) if isinstance(command, list) else command`), and
`task_1170003` does exactly the same, so this is the shipped convention rather
than a violation.

---

## 5. `evaluate()` against a fake env — the save contract

Both branches asserted as **command sequences**, not just outcomes.

**A — the deck on disk is byte-identical to what setup uploaded.** Nothing has
been written out, so forcing a save can only help; there is nothing to
overwrite.

```
1. sha256sum the deck              -> == INIT_SHA256
2. FORCE SAVE   xdotool ctrl+s, falling back to wmctrl + pyautogui
3. pkill wpp / wps / soffice
4. rm the .~lock file
5. sha256sum the deck              -> still == INIT_SHA256
6. find a stray .pptx newer than the deck   -> none
   get_vm_file: /home/user/Desktop/task_9900001.pptx
   save_attempted = True   save_status = SAVED
   score = 0.0   failure_reason = "byte-identical to the one supplied"
```

**B — the deck on disk has changed.** Somebody already wrote the agent's work
out; the only thing a save could do is undo it, so there is no save at all —
just the close.

```
1. sha256sum the deck              -> != INIT_SHA256
2. pkill wpp / wps / soffice
3. rm the .~lock file
4. sha256sum the deck
   get_vm_file: /home/user/Desktop/task_9900001.pptx
   save_attempted = False   save_status = "not needed — the file on disk already moved"
   score = 1.0   (fed the ground truth, through the emitted file)
```

The asserted difference is `["sha","save","kill","unlock","sha","scan"]` vs
`["sha","kill","unlock","sha"]`. This is the branch that a rollout measured
0.53 on and recorded 0.0 when the save was unconditional.

**C — unchanged at the pinned path, but a Save-As exists.** The recovery path,
included because it is the part of branch A that is not a dead end:

```
… as branch A through step 6, but the scan returns /home/user/Desktop/my copy.pptx
   get_vm_file: task_9900001.pptx, then 'my copy.pptx'
   scored_file = /home/user/Desktop/my copy.pptx   score = 1.0
```

---

## 6. What is now enforced by tests

`tests/test_emit.py` grew from 10 to 19. The stub is gone: `_stub_harness()`
now imports the **real** `desktop_env.task_base` whenever the benchmark
checkout is beside this repo (override with `OSWORLD_HARNESS_REPO`), and stubs
only `desktop_env.evaluators.getters` — importing that for real drags in lxml,
selenium and a dozen more, and its two functions are the seam a test drives
`evaluate` through anyway. Without a checkout the harness tests skip rather
than pass vacuously.

New: real-`BaseTask` load and instantiate · every runner-read field present
and correctly typed · class attributes diffed against `task_1170003` parsed
from source · `intermediate_eval_safe is False` · the upload list contains no
answer key and every local path exists · the `xdg-mime` and `launch(["wpp",…])`
calls · both save branches as command sequences · the Save-As recovery ·
`metadata.json` carries the shipped instruction.

`uv run --with pytest python -m pytest tests/ -q` → **393 passed**.
(Baseline at the start of this work was 382 with one pre-existing failure in
`tests/test_consistency.py`, a file another agent was editing concurrently;
it is green now and was never touched here.)

---

## 7. What only a VM can settle

Short and exact, because this is what the one rollout has to spend itself on.

1. **`wpp` opens the deck.** `_launch_setup` is fire-and-forget — it logs a
   non-200 and returns, and `setup` reports success either way. A missing or
   renamed binary produces a rollout with no deck on screen and no error
   anywhere. `task_087` and the 117 series both use `launch(["wpp", …])`, so
   the precedent is good, but the precedent is not the same image.
2. **`wps-office-wpp.desktop` is a real desktop entry on that image.** The
   `xdg-mime` call ends in `|| true`; if the name is wrong the association is
   silently not set and a stray `xdg-open` still routes to LibreOffice — the
   failure mode that turned a 19-slide deck into 61.
3. **The forced save lands.** Three unknowns stacked: `xdotool` is not on the
   base AMI so the script `apt-get install`s it (needs passwordless sudo and
   network); `xdotool search --name 'task_9900001.pptx'` must match WPS's
   actual window title (whether WPS includes the extension is unverified);
   and if it misses, the fallback types ctrl+s into whatever happens to be
   focused. Branch A of the save contract is worth nothing if this misses —
   check `evidence.save_status` for `SAVED` and `evidence.disk_sha_after`.
4. **WPS's save-time rewrite does not trip our own hard gates.** A correct
   deck opened in WPS and saved must still produce an inventory that passes;
   WPS injects `mc:AlternateContent` wrappers on save (`task_087` carries a
   ±1 shape tolerance for exactly this). The cheapest read is
   `hard_gates` / `failed_gate` in `result.json` on a run that is visually
   correct.
5. **The image has WPS at all, at 60 GB.** `snapshot="wps"` is declarative;
   the runner in this repo takes its AMI from `IMAGE_ID_MAP` keyed by screen
   size and never looks at the field.
6. **`get_vm_file` moves a deck this size.** Measured ~68.6 MB per deck in
   this pipeline; the reference decks are ~340 KB.

Not a VM question but a prerequisite for the run: **the task id has to be
registered.** `run.py` selects examples from a meta JSON
(`{"tasks": ["9900001"]}`, matching `test_cua_scaling.json`'s shape) and
`find_task_class_path` then resolves `task_class/task_9900001.py`. `emit`
writes into a staging `out_root` and does not touch the benchmark's meta
files, so that entry is added by hand at integration time.
