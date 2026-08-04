# Prior art: the archived hand-authoring skills

What survives from `pptx-tasks/.claude/skills-archive-2026-05-17/` for the stages this
pipeline has not built yet (reward · verification · packaging).

Sources, cited below as `impl:NN`, `spec:NN`, `test:NN`, `aws:NN`:

| key | file | lines |
|---|---|---|
| `impl` | `.../task-implementer/SKILL.md` | 634 |
| `spec` | `.../task-spec-creator/SKILL.md` | 406 |
| `test` | `.../task-tester/SKILL.md` | 252 |
| `aws`  | `.../aws-setup-smoke-test/SKILL.md` | 212 |

All four were written for a human (or an agent acting as one) authoring **one** task at a
time from a mined `(input.pptx, target.pptx)` pair. Read them as a catalogue of PowerPoint
comparison traps, not as a process.

---

## 1. Evaluator craft

### 1.1 Score shape and arithmetic (still exactly what ships)

`evaluate()` returns `{"score": float, "partial_scores": dict}`; each
`partial_scores[<id>]` is `{score: float ∈ [0,1], weight: float, description: str}` and
the total is `sum(s["score"] * s["weight"])`, rounded to 4dp (`impl:66-73`, `impl:142-145`).
The bare-float return of the OSWorld-V2 `task_2NN.py` files is explicitly called the *old*
convention (`impl:68-70`) — and indeed `benchmark/task_233/task_233.py:167` still returns a
float, while `cua-rl-scaling/.../task_08010/task.py:432` returns the dict. Verified still
current: `task_08010` matches `impl`'s template attribute-for-attribute.

**Hard gate first, partials second** (`impl:118-146`): fetch → file missing → not parseable
→ `slide_count != EXPECTED` → each returns `_fail_all(reason)`, which emits all-zero
partials *with the reason appended to every description* rather than a bare `0.0`
(`impl:148-166`). The point is that a zero in the eval log still carries its breakdown.

`WEIGHTS` / `DESCRIPTIONS` as class dicts exist so `_fail_all` can be written once and the
rubric lives in one place (`impl:560-572`).

### 1.2 Normalisation the comparators needed

- **GroupShape recursion is mandatory.** `for sh in slide.shapes` returns top-level shapes
  only; text inside a `GroupShape` (`MSO_SHAPE_TYPE.GROUP == 6`) is invisible without an
  explicit recursive visit (`impl:321-355`). Real cost: task_08002 v1 lost a partial-credit
  signal because slide 10's `Start` label was nested in `Group 6` (`impl:325-328`).
  markitdown sees these; ad-hoc walks do not.
- **Tables are one `GraphicFrame`, not cells in `slide.shapes`** (`impl:358-366`). Row
  lookup by label must tolerate whitespace: `'App 2'` and `'App2'` both match `App2`
  (`impl:374-388` — the implementation strips *all* spaces, not just the ends).
- **Column x-ranges are computed from the saved deck itself** by accumulating
  `col.width` from `table_shape.left` (`impl:390-399`), so a positional check ("marker
  inside the H.L.E. column") needs no ground-truth lookup (`impl:401-403`). This is the
  archive's one genuinely self-referential comparator and the closest ancestor of
  REWARD.md §3③ ("判关系不判绝对值").
- **Merged cells: wrap every cell access in try/except** and return 0 rather than crash if
  the agent rebuilt the table with a different merge structure (`impl:405-407`).
- **Word boundaries.** `"AI" in text` is true for "rain", "Britain", "available"; use
  `re.search(r"\bAI\b", text, re.IGNORECASE)` (`impl:482-494`, `spec:331-335`). Same for
  "ML", "VR", "OS".
- **Whitespace-flexible matching.** Source text `"Getting  Help"` (double space) does not
  match the substring `"Getting Help"`; use `r"Getting\s+Help"` (`spec:329-331`).
- **Case sensitivity is a per-check decision**, documented in the rubric description:
  brand names ("ChatGPT", "Excel") usually matter, topic words usually do not
  (`spec:336-339`).
- **Content-based slide identification over positional indexing** (`impl:456-478`): find
  the slide where *two distinct shapes* carry expected standalone texts (a shape whose
  text `== "Pros"` **and** one `== "Cons"`), not a substring hit anywhere on the slide.

### 1.3 Tolerances, with their numbers and reasons

The archive has only three numeric tolerances, and all three are semantic, not geometric:

1. **Image match, two tiers** (`impl:424-453`): tier 1 = md5 of `shape.image.blob` equals
   the shipped asset; tier 2 = Pillow dimensions within **5%**
   (`abs(w - ref_w)/ref_w < 0.05`). Reason: LibreOffice Impress may re-encode PNG → JPEG or
   recompress on save, especially with "Compress Picture" enabled (`spec:341-345`), which
   destroys the md5 without the agent doing anything wrong. `spec:344-345` also suggests an
   approximate colour-histogram fallback; no implementation was written.
2. **Image position/size is not scored at all** unless the instruction is explicit —
   placement is deliberately left as the task's open-ended axis (`spec:346-348`).
3. **Model-eval partial ≤ 0.15 of total** (`impl:409-417`, `spec:229-231`), with
   `max_tokens=5`, `temperature=0.0`, a strict YES/NO rubric, and `try/except → False`.
   Rule-based partials must dominate the RL gradient.

Notably **no positional/EMU tolerance appears anywhere in the archive** — the hand-authored
tasks avoided geometry comparisons entirely rather than tolerancing them. That is a
consistent, and in hindsight correct, avoidance (REWARD.md §3).

### 1.4 Partial credit and the binary-instead-of-count rule

Date fields are the worked example (`impl:496-508`, `spec:322-328`): footer dates live in
`<a:fld type="datetime1">` with a cached `<a:t>`. The check is
`new_date in deck_text AND old_date not in deck_text` — deliberately **binary rather than a
count**, because the Header & Footer dialog updates every slide at once while python-pptx
field manipulation updates per-slide, and both are legitimate routes. Generalised rule:
when two legitimate solution mechanisms produce different *counts* of the same correct
outcome, score the outcome, not the count.

### 1.5 Calibration (the archive's whole verification story)

Two endpoints, run by a scratch script before declaring done (`impl:510-541`, `spec:245-262`,
`test:75-92`):

- ground-truth deck → **exactly 1.0**, and every individual partial 1.0. Less than 1.0
  means the eval is buggy — wrong selector, keyword miss, threshold off (`impl:537-541`).
- untouched input deck → **the documented small baseline**, e.g. `0.05` for task_08001
  ("250 points"/"500 points" trivially present so `points_preserved` passes), `0.04` for
  task_08002 v2 after weight redistribution across 6 new partials (`aws:178-181`).
  The archive requires you to *document what the baseline represents*, not remove it.

The scratch harness stubs `desktop_env` with fake modules so `task.py` imports without the
framework (`impl:516-526`) — still the cheapest way to exercise a generated evaluator
offline.

---

## 2. Failure modes ("this will bite you")

Ordered by how expensive they were to learn.

1. **THE pptx pitfall: force-save before `get_vm_file`.** The agent edits in a GUI app;
   without an evaluator-driven save, `get_vm_file` reads the pre-edit bytes and silently
   returns the *baseline* score for good work (`impl:210-218`). Applies unconditionally to
   every PPT task. A silently no-op save is indistinguishable from "agent did nothing" in
   the eval output (`impl:309`).
2. **`_open_setup` cannot be used for LibreOffice tasks** — it goes through `xdg-open`,
   which swallows the `--accept=socket,...` flag, so no UNO socket exists at eval time
   (`impl:167-175`). Launch `soffice` yourself.
3. **The save dance is three steps, not one**: UNO `doc.store()` → `pkill -f soffice` →
   `sleep 3` → `rm -f '<dir>/.~lock.<fname>#'` (`impl:255-270`). Skipping the lock removal
   or the sleep leaves a file the getter cannot read cleanly. The helper never raises; it
   returns a status string.
4. **`xdotool` is not on the base OSWorld AMI** — the `apt-get install -y -qq xdotool` in
   the WPS-Linux save path is load-bearing (`impl:308`).
5. **`title == 'X'` selectors break on decks without a real Title placeholder.** Found in
   the task_08001 audit: the eval keyed on `title == "Pros"` while the slide's actual
   placeholder text was `"Ok, it's awesome but… "` — the eval would have failed *a correct
   agent*, permanently (`spec:286-289`, `spec:310-313`). Verify `ph type=` in the XML.
6. **Whitespace noise gives free credit on the untouched input.** `"Getting  Help"` vs
   `"Getting Help"` inflated the input baseline in task_08001 (`spec:293-296`). Note the
   direction: this class of bug shows up as a *too-high floor*, not a failure.
7. **Find & Replace does not touch `<a:fld>` date fields**, so an instruction that
   recommends F&R sets the agent up to fail (`spec:290-292`). Flag the trap in the
   instruction; loosen the check to binary.
8. **Slide-count hard gates do not prevent reordering** — `prs.slides[2]` is unsafe even
   with `slide_count == N` enforced (`spec:306-309`).
9. **`get_vm_file` looks at one fixed path.** If the agent saves to `/tmp/`, the result is
   simply not found; the instruction must state the save path explicitly, and the rubric
   must not depend on the agent's save discipline at all (`spec:350-366`).
10. **On-VM score diverging from the local calibration baseline means file corruption in
    transit** — an unintended save, an encoding conversion, or a `client_password` mismatch
    that made setup a no-op (`aws:129-136`, `aws:196-200`). This equality is the smoke
    test's actual pass criterion, and the single thing lost by not booting a VM.
11. **The test runner enforces nothing.** "A test passes if the function returns without
    raising" — no case types, no expected scores, no partial-score math, no result schema
    (`test:55-58`). Green tests mean only "did not throw".
12. **`patch.object(task_module, "get_vm_file")` works only because `task.py` imports the
    getter at module level** (`test:134-138`). Any generated evaluator that switches to
    `from desktop_env.evaluators import getters; getters.get_vm_file(...)` silently breaks
    every test's mock — tests then hit a real VM getter and return `None`, i.e. all zeros.
13. **`_persist_open_deck` must be stubbed in tests** via an `ExitStack` helper, or the
    unit tests try to talk to a real VM (`impl:592-609`). Test the helper itself for call
    order and status propagation (`impl:611-613`).
14. **Background bash has no profile**, so `uv` is not on PATH and exits 127; use
    `/home/yitongli/.local/bin/uv` (`test:186-190`, `aws:202-203`).
15. **Credentials passed in-session land in the tool log and the session JSONL**
    (`aws:41-50`). Source `.env_vars`; never write credentials into any artifact.

---

## 3. Task layout and the setup/eval contract

Verified still current against `cua-rl-scaling/evaluation_examples/tasks-yitongli/task_08010/`.

### 3.1 Directory

```text
evaluation_examples/tasks-<owner>/task_<id>/
|-- assets/            agent-visible; uploaded to the VM
|   `-- test/          eval/test-only ground truth; NOT uploaded
|-- tests/
|   |-- test_task.py
|   |-- task-test-report-<id>.md    (committed)
|   |-- task-test-results.{json,md} (regenerated; gitignore)
|   `-- test-work/                  (regenerated; gitignore)
|-- task.py            the only executable definition — JSON task configs unsupported
|-- README.md
`-- metadata.json
```

`impl:15-26`, `test:40-51`, `test:196-201`. `task.py` **must not reference `assets/test/`**
(`impl:580-583`, `spec:160-163`) — see §5.

`metadata.json`: `{id, instruction, domain, platform, uses_user_simulator, tags,
related_apps, task_path}`, optionally `owner, source, difficulty, expected_artifacts, notes`
(`spec:178-192`).

`README.md` must carry the partial-score table `| partial id | description | weight |
full-credit condition |` with weights summing to 1.0 (`spec:194-215`), and that table is
declared **the contract `task.py` implements** (`impl:34-37`).

### 3.2 Signatures

```python
class Task<ID>(BaseTask):
    id, instruction, source, trajectory, related_apps, platform,
    proxy, fixed_ip, possibility_of_env_change      # impl:96-116, 545-556

    def setup(self, setup_controller: "SetupController", use_proxy: bool = False) -> None
    def evaluate(self, env: "DesktopEnv") -> dict

TASK_CLASS = Task<ID>                                # module bottom, mandatory
```

### 3.3 What the harness calls, in order

From the smoke-test runner (`aws:106-118`): load task → resolve AMI → construct
`DesktopEnv(provider_name="aws", ...)` → **`env.reset(task_config=task)` runs `setup()` on
the VM** → (agent acts) → **`env.evaluate()`** → `env.close()`. With no agent actions,
`env.evaluate()` must not raise and must return the untouched-input baseline.

### 3.4 How assets reach the VM

```python
setup_controller._upload_file_setup([
    {"local_path": str(ASSETS_DIR / "input_deck.pptx"), "path": "/home/user/Desktop/input_deck.pptx"},
])
setup_controller.launch(["soffice", "--norestore",
                         "--accept=socket,host=localhost,port=2002;urp;",
                         "/home/user/Desktop/input_deck.pptx"])
```

(`impl:176-190`.) Everything lives under `assets/` and is referenced via `ASSETS_DIR`; no
remote assets unless explicitly requested (`impl:74-75`, `spec:158-168`, >50 MB per file
warrants external hosting). Note the shipped WPS tasks violate this and use
`setup_controller.download([{ "url": f"{HF_BASE}/..."}])`
(`benchmark/task_233/task_233.py:131-153`) — for a 10-slide reference-keyframe set that is
the only practical route.

### 3.5 Saving the deck before scoring — three platform paths

- **Linux LibreOffice**: the `_UNO_SAVE_SCRIPT` + `_persist_open_deck` pair, verbatim at
  `impl:228-270`. Retries the UNO resolve 20× at 0.5 s.
- **Linux WPS**: `_persist_open_wps_deck` — apt-get xdotool, `DISPLAY=${DISPLAY:-:0}`,
  `xdotool search --name <kw> windowactivate --sync`, `sleep 0.5`, `ctrl+s`, then
  `pkill -f wpp; pkill -f wps` (`impl:284-306`). **This helper was never shipped** — every
  WPS task in `pptx-tasks/benchmark/` is `platform = "windows"`, `snapshot = "wps"`.
- **Windows WPS/PowerPoint COM**: `Kwpp.Application` → `GetActiveObject` falling back to
  `Dispatch` → `ActivePresentation.Save()` → `taskkill /IM wpp.exe /F` → `sleep 3` → reopen
  `WithWindow=False` and `Slides(i).Export(...,'PNG')` for rendering
  (`impl:311-314`; live code at `benchmark/task_233/task_233.py:167-199`). This is the
  "well-trodden" WPS code the no-VM decision leans on.

---

## 4. What is obsolete

### Obsolete because it assumed a human author

- **`AskUserQuestion` before starting** — task ID, target collection, source pair location,
  difficulty target, delivery mechanism, whether to allow model-eval (`spec:44-62`). All of
  these are now either config or decided by the `proposal` stage.
- **"Ground the spec in a real `(input, target)` pair" + deep-diff to recover the semantic
  changes** (`spec:25-33`, `spec:381-383`). Inverted here: `source.pptx` *is* the target,
  `input.pptx` is manufactured, and `delta.json` states the change list exactly instead of
  reconstructing it from a diff. The pipeline's version is strictly better — the archive's
  diff step was a lossy recovery of information we now author.
- **The Input Design table** (`spec:64-80`) and the **delivery-mechanism trade-off table**
  (`spec:82-90`) — superseded by `materialise` + `assets/`, which derives required assets
  from `delta.json` (README:30-33) rather than from a judgement call. The acid test at
  `spec:78-80` ("could I complete every change without guessing?") survives as the
  underdetermination check inside `solvable`.
- **Instruction wording principles** (`spec:92-129`) — good advice, but it now lives in
  `ppt-task-proposal`, not here. The Task-004 anecdote (agent missed 4 of 7 axes because
  "match the style" was too vague) is still the best worked example of underspecification.
- **Difficulty anchoring to OSWorld-V2 Task 004, "~100-200 GUI steps"** (`spec:54-57`) —
  replaced by the measured step count the `solvable` probe reports.
- **"Skill evolution: add each new pitfall here"** (`impl:630-634`, `spec:396-406`,
  `test:248-252`) — the accumulation target is now a comparator in the registry plus a case
  in the adversarial battery, which is executable rather than prose.

### Obsolete because of the no-VM decision

- The whole `aws-setup-smoke-test` invocation machinery (`aws:54-100`), flags (`aws:151-172`),
  and the keep-running debugging loop.
- **AMI IDs are stale**: `us-east-1 × 1920×1080 → ami-01017272139e01feb` is stamped "as of
  2026-05" (`aws:138-147`); today is 2026-08. Do not copy these into anything.
- **The cost figures argue against the decision, mildly**: ~$0.005 and ~3-4 min for a simple
  PPT task, ~$0.02-0.04 and 10-15 min for a heavy one (`aws:16-18`). Skipping the VM is a
  throughput and complexity decision, not a cost one — worth stating honestly.
- **What must be preserved from it**: the pass criterion. `evaluate.result.score` on the VM
  must equal the local-calibration baseline, and divergence means transit corruption
  (`aws:129-136`). Without a VM this invariant is untested; the nearest substitute is a
  `roundtrip_identity` probe run through the *actual* scoring application, which is what
  REWARD.md §5 already asks for.

### Obsolete because we have since measured it differently

- **`spec:368-375`: "For rule-based eval that reads OOXML directly, engine differences
  usually don't matter — you're reading the saved bytes, not re-rendering." This is false
  for geometry and now measured to be false.** REWARD.md §2.4: renaming a font on a run
  changes the *saved* height of an autofit textbox by **0.600 in** (1.898 → 1.298 for Arial
  Narrow), and a missing Hangul glyph moves a shape centre by **0.150 in** — 60× and 15×
  `POS_TOL = 0.01in`. The saved bytes are engine- and font-set-dependent. The archive's
  advice is safe only because its evaluators never compared geometry.
- **LibreOffice-specific save behaviour is not the scoring environment.** The two-tier image
  match exists because *Impress* re-encodes on save (`impl:424-453`, `spec:341-345`); under
  WPS the measured shape drift is 0.0% across 10 decks (REWARD.md §2.1) even though WPS
  re-serialises the whole package (4.04 MB → 3.81 MB, 81 parts byte-different, `customXml/`
  dropped). Keep the two-tier idea; re-derive the 5% threshold against WPS rather than
  inheriting it.
- **"Untouched input ≈ a small documented baseline (0.05)"** (`spec:255-262`) is superseded
  by floor normalisation: `score(input)` must be **0**, and any tolerance that raises the
  `noop` score is by definition wrong (REWARD.md §4). Documenting a non-zero floor is the
  old, weaker discipline.
- **Model-eval partials** (`impl:409-417`, `spec:229-241`) sit badly with the probe battery:
  the probes assert exact 1.0/0.0 outcomes and a judge cannot deliver them deterministically,
  and 1000 tasks × N rollouts makes the API cost real. The archive's own stated reasons for
  preferring rule-based — deterministic, cheap, auditable, reproducible (`spec:233-241`) —
  now rule out its own escape hatch.

### Not obsolete, and easy to mistake for obsolete

- **`spec:16-22`: feasibility > difficulty.** "A medium-difficulty task that is cleanly
  completable is far more valuable than a hard task with hidden ambiguity."
- **`spec:264-302`: the feasibility check**, including "dispatch a fresh subagent that reads
  only spec + assets, NOT the ground truth". This is the direct ancestor of `solvable` — and
  the archive's version relies on a *requested* barrier ("pretend you don't have the ground
  truth", `spec:268-270`), which this pipeline has already measured to fail: 4 of 10 probes
  invalidated under filename-substring leak detection (README:42-53). The structural
  `bundle/` barrier is the correction, not a reinvention.
- Everything in §1.2 (comparator normalisation) and §2 (failure modes) — these are
  properties of OOXML and of python-pptx, not of the authoring process.

---

## 5. Where the archive conflicts with the derived-reward plan

1. **The README partial table is declared the contract.** `impl:34-37` — "the partial-score
   table — this is the contract `task.py` must implement"; `impl:40-44` — if README and
   metadata conflict, fix the spec first, "implementation should not silently re-interpret
   the rubric". Under the derived plan there is no per-task rubric to implement: weights
   come from the delta units. The README table becomes a *rendering* of `delta.json`, and
   the direction of authority reverses — if the table and the delta disagree, the delta wins.
   Any generated README must be emitted from the delta, never edited.

2. **`task.py` must NOT reference `assets/test/`** (`impl:36-38`, `impl:580-583`), because
   ground truth is test-only. A delta-derived evaluator *needs* ground truth at scoring time
   — REWARD.md §7 already concedes "不能对评分器做完全的信息屏障". So the packaging stage
   must break this rule deliberately and replace it with a sharper one: the evaluator may
   read `delta.json` / GT values, but **nothing the evaluator reads may be uploaded by
   `setup()`**. The archive's agent-visible/eval-only split is right; its implementation
   (`assets/` vs `assets/test/`, enforced by "don't import from test") is the wrong
   mechanism once the evaluator is generated. Get this wrong and the answer key ships to the
   VM — the same class of leak as the SmartArt `data*.xml` leak the pipeline already gates.

3. **Weights are hand-assigned to sum to 1.0** (`spec:213-215`, `test:66-67` asserts the
   sum). The derived evaluator scores `mean(floor_normalised per-unit)` across delta units
   (`scaling/pipeline/evaluator.py:1-9`), i.e. uniform weight per unit. The `test:66-67`
   sanity assertion still works, but "design the weights to reflect importance"
   (`spec:226-228`) has no operator to hang on. If differential weighting is wanted it must
   be a property of the *operator family* in the registry, not of the task.

4. **Calibration is two points; the plan needs five.** The archive checks GT→1.0 and
   input→baseline (`impl:537-541`). It has no `roundtrip_identity`, no `equivalent_repr`, no
   `blind_solver`. REWARD.md §5 names `roundtrip_identity` as the one to write first because
   it is agent-free and it identifies comparators that *should not exist* — a check the
   archive's method structurally cannot perform, since it never opens the GT in the
   application before comparing.

5. **"Each partial should have a clear pass/fail condition writable in 1-3 lines of Python"**
   (`spec:243-245`). Several registry comparators will not be — `_nearest_chart`
   (`scaling/pipeline/evaluator.py:33-52`) is ~20 lines and carries an anti-hacking exclusion
   (never credit a *surviving* neighbour chart, or a deck with look-alike chart pairs scores
   a third of the unit for doing nothing). Comparator complexity is fine when it is written
   25 times instead of 1000; the archive's brevity rule was a hedge against per-task code.

6. **`_fail_all` semantics vs floor normalisation.** `impl:148-166` zeroes every partial on
   a hard-gate failure. Under floor normalisation, "0" already means "no better than the
   damaged input", so a gate failure and a genuine no-op are indistinguishable in the score
   and separable only by the reason string. Keep the reason annotation — it is the only
   remaining discriminator, and `impl:163-166` gives the exact reason for keeping it.
