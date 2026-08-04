# Prior art: cua-rl-scaling's skill library, and what can be brought over

What was read is `/home/yitongli/XLANG/cua-rl-scaling/.claude/skills/`, plus
`desktop_env/task_base.py` (in order to write the harness contract concretely).
**Not read:** `pptx-tasks/scaling/pipeline/ops.py` (another agent is auditing
it). Every citation is written as `file:line`.

This file answers four faces of the same question: what the harness actually
requires, how much of `ppt-pair-authoring`'s method we have already done, what
is left of each skill given that **the scorer is generated**, and **how many of
this library's assumptions have been overturned by numbers we measured**.

A prior constraint: **no VM is booted before submission**. So anything in the
library that depends on a live rollout (the smoke gate, the rollout gate,
deriving difficulty from rollout scores) is out of range — but **the files they
leave behind** still have to be produced, on which see the last item of section
5.

---

## 1. The harness contract (concrete enough to generate against)

### 1.1 Directory shape

Three skills give the same layout, with one disagreement (see 1.6):

```text
evaluation_examples/tasks-<owner>/task_<id>/
  task.py            # BaseTask subclass; TASK_CLASS = <class> at the bottom of the file
  metadata.json
  README.md
  assets/            # uploaded into the VM, visible to the agent
  tests/
    test_task.py
    spec-gate.md  implementation-gate.md  smoke-gate.md  rollout-gate.md
    qa-summary.json
    assets/          # evidence + test-only fixtures (including the GT)
```

Sources: `task-implementer/SKILL.md:16-30`, `task-spec-creator/SKILL.md:33-46`,
`ppt-pair-authoring/SKILL.md:33-44`.
`task.py` is the **only** executable task definition; JSON task configs are no
longer supported (`task-implementer/SKILL.md:32-33`).

### 1.2 What the harness calls, and in what order

`desktop_env/task_base.py:74-85`:

1. `setup(self, setup_controller, use_proxy: bool = False) -> None`
   — called once after the environment resets; responsible for uploading files
   and launching applications.
2. The agent runs.
3. `evaluate(self, env) -> float | dict`
   — returns a float (legacy) or a dict; **when it returns a dict the runner
   drops the whole payload into `result.json` and also writes the legacy
   `result.txt`** (`task_base.py:78-84`).

`TASK_CLASS = <class>` must be at the bottom of the module; the loader uses it
to find the class (`task-implementer/SKILL.md:76`,
`task-tester/SKILL.md:17-19`).
`BaseTask` is itself a subclass of `dict`, and class attributes are copied into
the instance via the `_fields()` list (`task_base.py:11-37`), so
`id / instruction / platform / related_apps / source / trajectory / proxy` can
simply be written as class attributes.

### 1.3 The shape `evaluate()` returns

```python
return {"score": round(total, 4), "partial_scores": partial_scores}
```

Each partial is `{"score": float, "weight": float, "description": str}`, and
**the weights must sum to 1.0**; `score` is the weighted sum, rounded to 4
places.
(`ppt-pair-authoring/references/authoring-guide.md:212-218`; reference
implementation `assets/pptx_helpers.py:229-242`, `assemble_score`.)

Two hard numeric contracts, `task-implementer/SKILL.md:83-87`:

> 6. Score the unchanged post-setup state with no agent action as exactly `0.0`.
>    Do not award partial credit for setup-provided files, default app state, or
>    artifacts that exist before the agent starts working.
> 7. Score the ground-truth/completed reference state as exactly `1.0`.

### 1.4 How assets get into the VM

Local paths, no remote URLs (`task-implementer/SKILL.md:80-82`):

```python
setup_controller._upload_file_setup([
    {"local_path": str(ASSETS_DIR / "template.xlsx"),
     "path": "/home/user/Desktop/template.xlsx"},
])
```

(`task-implementer/SKILL.md:145-155`; template
`ppt-pair-authoring/assets/task_template.py:66-69`.)
Files come back via
`getters.get_vm_file(env, {"path": ..., "dest": "task_<ID>_result.pptx"})`
(`task-implementer/SKILL.md:166-170`).

### 1.5 A forced save before scoring (this one changes `setup()`)

`ppt-pair-authoring/SKILL.md:109-117`, expanded at
`authoring-guide.md:263-291`:

> The save is the evaluator's responsibility, not the agent's. […] If the
> evaluator pulls the file without first forcing a save, it reads the bytes that
> were on disk before the agent's edits and scores the untouched baseline — a
> false negative that looks exactly like an agent that did nothing. This is the
> most consequential PPT-specific evaluator bug.

Three paths, all implemented in `ppt-pair-authoring/assets/persist_deck.py`:

| application | how | what it forces on the setup side |
|---|---|---|
| LibreOffice Impress (Linux) | UNO socket `doc.store()` → `pkill -f soffice` → sleep 3 → `rm .~lock.<name>#` | `setup_controller.launch(["soffice","--norestore", "--accept=socket,host=localhost,port=2002;urp;", path])`. **`_open_setup` cannot be used**: it goes through xdg-open and cannot inject `--accept` (`persist_deck.py:29-33`) |
| WPS (Linux) | lazily install xdotool/wmctrl/pyautogui → `xdotool key ctrl+s`, falling back to wmctrl+pyautogui on failure → `pkill -f wpp/wps` | `setup_controller.launch(["wpp", path])`, again not `_open_setup` (`persist_deck.py:88-95`) |
| Windows | COM `ActivePresentation.Save()` + `taskkill` | `persist_deck.py:128-142` |

**This is a requirement at evaluate time that constrains how setup is written**,
so the packaging stage has to generate them together; they cannot be considered
separately. Neither Linux helper raises; both return a status token for the
log.

### 1.6 `metadata.json`

`task-spec-creator/SKILL.md:193-207`:

```json
{
  "id": "<id>",
  "instruction": "...",
  "domain": "...",
  "platform": "linux",
  "uses_user_simulator": false,
  "tags": ["..."],
  "related_apps": ["..."],
  "task_path": "task.py",
  "review": {"status": "pending"}
}
```

Optional fields: `owner` / `source` / `difficulty` / `expected_artifacts` /
`notes`.
`review.status` starts at `pending` and is changed to `pass` / `fail` by
`task-reviewer` (`task-spec-creator/SKILL.md:209-212`,
`task-reviewer/SKILL.md:172-217`).

**A disagreement inside the library; a side has to be picked:** where the GT
deck goes.
`ppt-pair-authoring/SKILL.md:38` and `scripts/calibrate.py:79-87` use
`assets/test/gt_*.pptx`; but `task-spec-creator/SKILL.md:49-50` states
explicitly that they "belong under `tests/assets/`, not `assets/test/`", and
`:102` says it again — "Do not use `assets/test/` for new tasks";
`task-tester/SKILL.md:104` marks `assets/test` as `legacy_test_assets_dir`
(for old tasks only).
**Conclusion: `ppt-pair-authoring` is the stale one on this point. Always
generate into `tests/assets/`, and have the calibration script take an explicit
path rather than inheriting its directory-guessing logic.**

### 1.7 The runner contract for `tests/test_task.py`

The runner is `task-tester/scripts/run_task_tests.py`: import the task module →
import the test file → run every top-level `test_*` function **in source
order** → catch exceptions → write JSON/markdown results.
It does not validate case types, expected scores, partial arithmetic or the
result schema; **a function that does not raise has passed**
(`task-tester/SKILL.md:57-62`).

The runner can inject these kwargs by name (`task-tester/SKILL.md:97-106`):
`task_module` / `task_path` / `task_dir` / `tests_dir` / `assets_dir` /
`test_assets_dir` / `legacy_test_assets_dir` / `work_dir`.

**This is the shape our probe battery has to be produced in**: one
`tests/test_task.py` containing a few flat `test_*` functions, each preparing
its own fake env and mocks. See section 4, item 3.

---

## 2. `ppt-pair-authoring`'s method, and how much of it we have already done

Its process is 8 ordered steps (`SKILL.md:53-165`). Item by item against ours:

| its step | our counterpart | where the gap is |
|---|---|---|
| **2.1 audit the pair** — a three-layer diff (markitdown → soffice+pdftoppm → python-pptx) that **reconstructs** the differences between input and target | **not needed**. `delta.json` is the **cause**, not the effect: every change together with its prior value is something we wrote down | it is doing archaeology; we have a birth certificate. Everything this step produces, we already possess at `degrade` time |
| **2.2 instruction: goal, not procedure** + `lint_instruction.py` | the `ppt-task-proposal` skill writes the instruction; there is no linter | **we are missing a linter**, and our current instructions would probably fail it. See section 5, item 6 |
| **2.3 supporting assets must look real** + `realism_check.py` | the `materialise` stage (reference images, masked reference images, CSVs, keyframes) | we currently produce PNG/CSV; its checks target PDF/docx. **The rules are dormant for us, but they take effect the moment materialise starts producing memos** |
| **2.4 evaluator: hard-gate first, independent partials** | not yet (the first thing to be built this time) | **all of it is to be copied**, see section 4 |
| **2.5 force-save before reading** | **entirely absent**. `grep -riE "force-save\|persist_deck\|get_vm_file" *.md pptxgym/` hits nothing in this repo but an unrelated Ctrl+S at REWARD.md:49 | **this is the biggest gap**, see section 6 |
| **2.6 calibrate (GT=1.0, input≈0) + variant tests against GT-overfit** | `scripted_restore` / `input_floor` / `equivalent_repr` among REWARD.md section 5's five probes are these three things | **we have the concept, not the construction.** It gives a concrete recipe: reorder shapes / paraphrase / re-encode an image (`SKILL.md:124-129`) |
| **2.7 feasibility check (look only at what the agent can see, dispatch a sub-agent, never look at the GT)** | the `solvable` stage + the structural information barrier of `bundle/` | **we are stronger.** It relies on "dispatch a sub-agent and tell it not to look at the GT"; we rely on directory isolation, and we already know that log scanning by filename substring voids 40% of the probes (README:47-52) |
| **2.8 QA gates + submission** | the `reconcile` / `solvable` verdicts + the `attempts/` archive | of its 5 gates, spec/implementation can be generated; smoke/rollout need a VM |

**In one sentence: its first half (2.1) we do not need, its second half
(2.4/2.5) we do not have at all, and in the middle (2.2/2.3/2.6/2.7) we have
equivalents but lack its executable floor.**

### What it does, we do not, and **should** adopt

1. **The forced save, and the way it constrains `setup()` in reverse.**
   (section 1.5)
2. **The hard gate has to be narrow.** It covers only "will not open" and
   "wrong slide count"; everything else goes into a weighted partial
   (`pitfalls.md:48-51`). For a scorer **generated from `delta.json`** this is
   structural: delta has N entries, and a naive generator turns them all into
   gates, so any one thing not fixed means a score of 0.
   **N delta entries → N partials, not N gates.**
3. **The three variant constructions against GT-overfit** (reorder / paraphrase
   / re-encode).
4. **The exact shape of the returned dict** (section 1.3) — this is not a design
   choice, it is the harness's interface.

### What it does that we should not adopt (because it assumes a human author)

- The three-layer audit (2.1) — we have delta.
- Every rule of the form "evidence images must come with explanatory prose, no
  galleries, do not repeat the same set of screenshots in every file"
  (`task-spec-creator/SKILL.md:106-129`, `task-qa-backfill/SKILL.md:47-71`) —
  this is layout discipline written for **human reviewers**, and nobody is going
  to read a thousand tasks one by one. Keep its **judgement** (the evidence must
  be real, no placeholder images); drop its **layout procedure**.
- The AskUserQuestion opening of `task-spec-creator` (`SKILL.md:12-27`).
- The **content-location** family in `pptx_helpers.py`: `find_idx_with_all`,
  `find_slide_by_standalone_texts`, `find_row_by_label`
  (`pptx_helpers.py:76-127`). These exist because "a human writing a scorer by
  hand does not know where the shape is and can only find it by its text". Our
  delta carries `path` / `shape_id` / `name` / `box`.
  **But the two judgements behind them stay**: groups have to be recursed
  (`iter_shapes_recursive:30-38`), and a table cell is not a slide shape
  (`find_table:103-109`) — because the comparator reads **the file the solver
  saved back**, and we did not write that file.

---

## 3. Verdict, skill by skill

| skill | verdict | reasoning |
|---|---|---|
| **ppt-pair-authoring** (overall) | **adapt** | its methodological skeleton is exactly the three pieces we are missing; but its directory layout uses the old `assets/test/` (1.6), its first half (audit) is redundant for us, and its QA section depends on a VM |
| ├ `assets/persist_deck.py` | **reuse as-is** | copy it word for word. The skill says so itself: "copy verbatim — they encode bug fixes, local edits reintroduce the bugs" (`SKILL.md:50-51`). What we want is the WPS path (`:96-125`) |
| ├ `assets/task_template.py` | **adapt** | turn it into the packaging stage's **emission template**: copy the structure (UNO/wpp launch → force-save → narrow gate → `assemble_score`), and fill `WEIGHTS` / `DESCRIPTIONS` / the partial function bodies from delta + the comparator registry |
| ├ `assets/pptx_helpers.py` | **partly obsolete** | reuse: `build_fail_all` / `assemble_score` (:214-242), `iter_shapes_recursive` (:30-38), `word_bounded` / `whitespace_flexible` (:187-201). Obsolete: the content-location family (we have paths). **Doubtful**: `check_image_present`'s `dim_tol=0.05` (:145), see section 5, item 1 |
| ├ `scripts/audit_pair.py` | **obsolete** | delta.json leaves it with no use on the input side. Only as an after-the-fact check when "I want to verify independently that the broken file really is broken that way" |
| ├ `scripts/lint_instruction.py` | **adapt** | the rule table (:31-61) can be run directly; but it will produce a lot of both false and true positives on our current instructions, so we have to decide first whether to obey it (section 5, item 6) |
| ├ `scripts/realism_check.py` | **reuse as-is (dormant)** | the thresholds are explicit: one of 12 library names appearing in Producer, embedded fonts ≤ 1, file < 20000 bytes — any one of the three is a fail (:27-31, :81-86). Plug it in the day materialise starts producing PDF memos |
| ├ `scripts/calibrate.py` | **adapt** | its stubbing technique (`_stub_desktop_env:32-55`, replacing `get_vm_file` with a local fixture and neutralising the persist helper) is exactly what our probe battery needs; but the directory guessing (:77-87) has to become an explicit path, and the criteria have to be tightened (section 4, item 3) |
| **task-implementer** | **adapt** | the seven requirements on `task.py` (:74-87) **are** the harness contract; generate against them. The asset rules (:128-138) and the upload/retrieve idioms (:140-184) carry over as they are. **The evidence/screenshot section (:219-278) is entirely obsolete without a VM.** The `_open_setup(path=...)` example at `:156` **is wrong for us** (section 5, item 3) |
| **task-spec-creator** | **reuse the judgement, discard the procedure** | keep: the `metadata.json` field table (:193-207), the required README sections and the partial-score table (:214-247), the spec-gate's mandatory questions (:145-164). Discard: the AskUserQuestion opening, the evidence-image layout discipline. Its spec-gate checklist is in fact the set of questions our `reconcile` + `solvable` already answer, so it can serve as an **output mapping table**: reconcile's verdict → the spec-gate alignment row, solvable's verdict → the environment sufficiency + feasibility rows |
| **task-tester** | **reusable with changes** | **the runner contract (:57-62, :97-106) is the concrete shape our probe battery lands in** — flat `test_*`, injected kwargs, not raising counts as passing. The three cases it mandates (:141-151: do-nothing exactly 0.0, GT exactly 1.0, partials itemised) map one-to-one onto three of our five probes. **Adopt the runner, generate the tests** |
| **task-reviewer** | **reuse the judgement, discard the procedure** | the checklist (:98-152) is the best written-down version of "what a human reviewer will pick on"; turn it into acceptance assertions in the packaging stage. Especially `:119`: "The evaluator rewards the requested user outcome rather than an incidental file, no-op path, hidden answer leak, or brittle formatting artifact". **Its difficulty thresholds (:66-77) depend on rollout scores plus step counts, and we do not run rollouts → we can only use the proposal's `difficulty`/`est_steps`, and must state that source honestly in the metadata** |
| **task-qa-backfill** | **obsolete (with one discipline excepted)** | it is a one-off tool for backfilling evidence onto existing tasks. The only thing to take away is `:120-127` and `:186-188`: an audit may not quietly fix the task on the way past; when a gate does not pass, write down the blocked reason and **never mark placeholder evidence as passed**. Our `reconcile`/`solvable` already implement this via "the orchestrator may not write `task.json`", so it is **mutual corroboration, nothing to import** |
| **ppt-pair-sourcing** | **obsolete (with one exception)** | the corpus question is settled. The only thing to take away is gate 3's **coding floor** (`:120-123`): "reject a pair where a content-blind script that never renders the slide gets full reward", and the boundary "**manipulate, don't construct**". This is **not** a property of the source, it is a property of the **scorer** — see section 4, item 5 |
| analyze-traj / request-rollout / task-rollout-gate / remote-vm / website-use / distractor-gen | **obsolete** | all of them need a live rollout or have nothing to do with PPT. The one residue: the existence of `task-rollout-gate` is the reason the file `tests/rollout-gate.md` has to be produced at all — the packaging stage has to ship one marked `pending` |

---

## 4. The scoring-quality rules worth keeping (with numbers)

### 1. Structure: a narrow gate + independent weighted partials

`references/pitfalls.md:48-51`:

> **Over-broad hard gate zeroes correct work.** A locked-region gate that covers
> more than the structural invariants will zero a rollout that did substantial
> correct work but incidentally touched the region. Keep the gate to "won't open"
> and "wrong slide count"; everything else is a weighted partial.

`authoring-guide.md:245-247` gives the empirical basis:
"tasks with broad gates produce 0.0 at the step cap, while tasks with narrow
gates bank partial credit for the same quality of work."

When the hard gate fails, use `build_fail_all`: set every partial to 0 **but
keep the weights and descriptions, appending the failure reason to each
description**, so the breakdown is still visible in the log instead of a bare 0
(`pptx_helpers.py:214-226`).

### 2. The four numbers for model-eval

`ppt-pair-authoring/SKILL.md:106-107`:

> **Model-eval ≤ 0.15**, strict YES/NO, `temperature=0`, `max_tokens=5`,
> try/except → False.

The reasoning (`authoring-guide.md:249-254`): use it only when the thing is
inherently visual and no structural check can cover it, and hold the weight
under 0.15 so the rule-based components dominate the gradient; any exception
returns False, because the scorer must never crash on a model timeout.

### 3. Calibration criteria — the library contradicts itself; take the strict side

- `ppt-pair-authoring/SKILL.md:121-123`: GT **exactly 1.0, every partial 1.0**;
  the untouched input at its **documented near-zero baseline**.
- `scripts/calibrate.py:135-141`: GT ≠ 1.0 → FAIL; input > **0.3** → only a
  WARN.
- `task-implementer/SKILL.md:83-85`: no-agent-action **exactly `0.0`**, and
  explicitly no partial credit for setup artefacts, default app state, or
  anything that exists before the agent starts.

**A WARN at 0.3 and exactly 0.0 are 0.3 apart. Take `task-implementer`'s exactly
0.0**; REWARD.md section 4's floor normalization is precisely the mechanism by
which a delta-driven scorer reaches it (subtract the broken file's own score).
The library has no concept of floor normalization; **on this point REWARD.md is
stronger than the skill library**.

### 4. Concrete patterns for equivalence tolerance (`pptx_helpers.py`)

| case | how | lines |
|---|---|---|
| the picture may be re-encoded on save (PNG→JPEG) | compare the blob md5 first; on failure compare dimensions with Pillow, `dim_tol=0.05` | `:144-175` (**doubtful for us, see 5.1**) |
| dates/footers are often an `<a:fld>` field rather than text | **a binary judgement**: the new value appears and the old value disappears. Do not count occurrences | `:204-207`, `pitfalls.md:22-25` |
| keywords of ≤2 letters | a word-boundary regex `\bXX\b` (`"AI" in text` matches "rain"/"available") | `:187-195`, `pitfalls.md:27-29` |
| the source string has a double space | a whitespace-flexible regex (`"Getting  Help"`) | `:198-201`, `pitfalls.md:31-33` |
| text inside a group | recurse `GroupShape` | `:30-38` |
| a table cell | go through the table API, do not iterate `slide.shapes` | `:103-109`, `pitfalls.md:17-20` |

### 5. Anti-hacking: the three the library has

1. **The narrow gate** (item 1 above).
2. **`task.py` must never read `assets/test/` at runtime** (`SKILL.md:107`,
   `authoring-guide.md:256-258`) — "an evaluator that reads the GT at runtime is
   leaking the answer into the VM".
   Our version of this is in REWARD.md section 7: the scorer **may** read
   `delta.json` (floor normalization depends on it), and the barrier becomes
   "the comparator is written against **operator semantics** and may not look at
   the specific recipe".
3. **The coding floor** (`ppt-pair-sourcing/SKILL.md:120-123`):
   > reject a pair where a content-blind script that never renders the slide gets
   > full reward (e.g. replace every date)

   **This is the specification for a sixth probe.** Among our existing five,
   `blind_solver` judges "can it still be done without seeing the reference"
   (answer leaks); the coding floor judges "can a **pure scripted heuristic that
   never looks at the render** get full marks". They are different, and we do
   not have the latter.
   The same item also gives the task boundary: **manipulate, don't construct** —
   edit the rich content already on the slide; do not require building SmartArt
   / morph / equations from nothing.

### 6. Variant constructions against GT-overfit

`ppt-pair-authoring/SKILL.md:124-129` / `pitfalls.md:53-57`:

> Run it against 2-3 variant decks that are correct but differ from GT (reorder
> shapes, paraphrase where allowed, re-encode an image); a correct variant scoring
> low means the rubric keys on GT implementation detail, not intent.

This is the **executable recipe** for the `equivalent_repr` probe; copy it
directly.

### 7. What the library lacks and REWARD.md has

floor normalization, the adversarial battery (`noop`≈0 / `wrong_params` low),
"if widening some tolerance makes the `noop` score go up, that tolerance is
wrong", and "anti-hacking over covering equivalent solutions". **These four are
our net addition to the library, and they must not be diluted by the library's
'widen the tolerance' reflex.**

---

## 5. Contradictions: what this library assumes, overturned by our measurements

### 1. "Saving re-encodes pictures" → the free 5% band that is `dim_tol=0.05`

`pitfalls.md:35-37` together with `pptx_helpers.py:144-175` make picture
comparison fall back to a **±5% dimension match** once md5 fails. Its basis is
that **LibreOffice** recompresses PNG into JPEG.

What we measured is: **on open-and-save, WPS moved 0.0% of shapes in 10/10
decks** (REWARD.md §2.1). Tasks are solved and scored in WPS; LibreOffice's 33%
is **the proxy's own behaviour, not the environment's** (§2.2). By REWARD.md §3①
— "any tolerance wider than floating-point noise needs measured evidence from
WPS behind it, and LO's p90 is not evidence" — **this 5% band is an unproven
giveaway in our environment**.

But **that is not a reason to replace it with "compare md5 only"**: REWARD.md
§2.1 also records that WPS rewrote deck0001's package from 4.04MB to 3.81MB,
**81 parts have different bytes and `customXml/` was dropped entirely**.
**Whether the md5 of a media blob is stable across a WPS save is something we
have not measured.**
So **both layers** of `check_image_present` are unproven, not just the second.
→ **To do: run an md5 comparison of `ppt/media/*` before and after a WPS
round trip on the 10 decks. Until then, keep picture comparisons out of the
scoring.**

### 2. "Environment differences = save-time normalisation differences" — a whole mechanism missing

`pitfalls.md:7-11` and `authoring-guide.md:310-316` attribute every local/image
difference to "save-time normalization" (theme colour `schemeClr` vs
`srgbClr`), and put the only defence on a **smoke run on the real image**.

What we measured is a different mechanism: **which face a font name resolves
to**. The same text in the same 4-inch-wide 18pt autofit box, changing only the
font name on the run, comes back at 1.898 / 1.598 / **1.298** inches — **not one
character changed, and the height differs by 0.600 inches**; a missing glyph
adds a further 0.150 inches of centre displacement (REWARD.md §2.4).
0.600 inches is **60×** `POS_TOL = 0.01in`.
The key one is the Arial Narrow row: **this box does not have that file, yet it
resolves to a face with different metrics** — which is to say **comparing the
font file lists at both ends is not enough**; you have to run `fc-match` on
every font name occurring in the corpus and compare **what it resolves to**
(REWARD.md §2.4, step 2).

**Not one pitfall in this library mentions fonts.** And its only means of
dealing with environment drift (the smoke run) is unavailable under "no VM
before submission".
→ **The substitute defence is not a wider tolerance, it is REWARD.md §3② +
§3③: remove the size of an autofit text box from the scored components (not
widen it), and always judge relations rather than absolute coordinates for
positions.** The second of those is naturally immune to font differences;
absolute coordinates are not.

### 3. `_open_setup` — two skills flatly contradict each other

`task-implementer/SKILL.md:156` writes
`setup_controller._open_setup(path="/home/user/Desktop/template.xlsx")`
as the **standard idiom** for upload-and-open.
`persist_deck.py:29-33` and `:88-90` say that for any GUI document task
requiring a forced save, `_open_setup` **must not be used**: it goes through
xdg-open and cannot inject `--accept` (LibreOffice), and on a generic AMI it may
fall `.pptx` back to LibreOffice instead of WPS.

→ **Do not follow `task-implementer`'s example when generating `setup()`.**
Our path is `setup_controller.launch(["wpp", DECK_VM_PATH])`.

### 4. GT in `assets/test/` or `tests/assets/`

See 1.6. `ppt-pair-authoring` is on the old side. **Pick `tests/assets/`.**
Knock-on effect: `calibrate.py:77-87`'s directory-guessing logic has to be
thrown out wholesale.

### 5. The QA gates' pass condition cannot be satisfied without a VM

`task-spec-creator/SKILL.md:179-180`: `overall_status` is `pass` **only when
spec / implementation / smoke all pass**; `smoke` is defined as "runs on a real
AWS image" (`authoring-guide.md:361-369`).
`task-reviewer/SKILL.md:66-77`'s three `difficulty` thresholds (score ≤0.3 →
hard; ≤0.5 and steps ≤150 → medium; ≤0.7 and steps ≤200 → medium; ≤1.0 and
steps ≤200 → easy) all read rollout results.

**Under "no VM before submission", `overall_status` can never be `pass`.**
This cannot be worked around; it has to be acknowledged explicitly, following
the library's own discipline (`task-qa-backfill/SKILL.md:120-127`, `:186-188`):
**when a gate does not pass, write down the blocked reason and the plan to
measure it later, and never mark placeholder evidence as passed**.
→ When the packaging stage emits `smoke-gate.md` / `rollout-gate.md`, the status
is `blocked`, `blocked_reason` reads "pre-submission VM boot out of scope by
decision; capture plan: <…>", and `difficulty` states that its source is the
proposal's `est_steps` and not a rollout. **Do not lie to turn a validation
script green.**

### 6. The instruction linter will fire all over our current instructions

Among `lint_instruction.py:31-61`'s rules, "positional slide reference"
(`\bslides?\s+\d+`) and the "verbatim value" family will hit deck0001's
`task.json` instruction repeatedly: "Slide 4 has lost…", "Slides 6 and 8", "On
slide 13", as well as `y-axis labelled 'Production (kt)'` and `La–Nd / Sm–Tb /
Dy–Lu`, which are **determinate sources that exist only in the instruction**.

This is not the linter being broken, it is **a head-on collision between two
designs**:
- the library's assumption: concrete values move into a memo the agent has to
  read, and the instruction keeps only "the goal + where to find it"
  (`authoring-guide.md:114-168`);
- our situation: the `solvable` probe requires each degradation's end state to
  be **determinate**, and the reference image is masked, so the geometry and the
  numbers ended up written back into the instruction.

**There are two roads, one has to be chosen, and it has to be written into the
`ppt-task-proposal` skill:**
(a) adopt the memo pattern — `materialise` produces one more asset, "a
colleague's annotations / handover note", moving the locations and values into
it and pulling the instruction back to the goal level; `realism_check.py` then
becomes useful.
(b) explicitly reject the linter's location rules and say why (the masked
reference image already carries the "you must go and look at the environment"
function, and another layer of indirection only adds steps).

**Not choosing is defaulting to (b), and defaulting to it unconsciously.** The
library's **judgement** on this point is right (concrete values should be read
from the environment, not from the prompt) and is worth keeping; its **regexes**
need not be swallowed whole.

---

## 6. Three stages still to build, and which part each should copy

| stage | what to copy | source |
|---|---|---|
| **the scorer (delta → comparator registry)** | the returned dict shape, `assemble_score` / `build_fail_all`, the **narrow gate**, model-eval ≤0.15, group recursion + the table API, the binary judgement on dates, the word-boundary / whitespace-flexible regexes | `authoring-guide.md:212-258`, all of `pptx_helpers.py`, `pitfalls.md:48-51` |
| **the probe battery** | `task-tester`'s runner contract (flat `test_*` + injected kwargs), `calibrate.py`'s stubbing technique, the three GT-overfit variant recipes, **plus a new coding-floor probe** | `task-tester/SKILL.md:57-62,97-106,141-151`, `calibrate.py:32-101`, `SKILL.md:124-129`, `ppt-pair-sourcing/SKILL.md:120-123` |
| **packaging** | the directory layout (GT under `tests/assets/`), the `metadata.json` fields, the required README sections, `task.py`'s seven requirements, `_upload_file_setup`, **`launch` + `persist_deck` copied word for word**, gate files emitted honestly as `blocked` | `task-implementer/SKILL.md:16-30,74-87,140-184`, `task-spec-creator/SKILL.md:193-247`, all of `persist_deck.py`, `task-qa-backfill/SKILL.md:120-127` |

**The single line of code most worth writing first**: `persist_open_wps_deck` —
because it simultaneously determines how `setup()` is written, and because
**not one of the five probes we planned can detect that it was left out** (all
five run on file bytes, and none of them passes through the GUI save step).
Leave it out and the scorer reads the bytes from **before** the agent touched
anything; the score is the broken file's score, and it looks exactly like "the
agent did nothing".
