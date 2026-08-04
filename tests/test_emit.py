"""What the packaged task must be true of before anyone runs it.

Three things matter: it works without the library it was built from, it cannot
hand the agent its own answer key, and **the harness accepts it**. `score` is
always a number in 0-1 — the diagnostics beside it explain a zero, they never
replace it.

The third one used to be satisfied by imitation. `BaseTask` was stubbed as
`object`, so every assertion about the class attributes was asserting against
a class the benchmark had never seen. The real `BaseTask` is a `dict`
subclass whose `__init__` copies a fixed field list into the dict, and the
runner reads the task through that dict — a class attribute outside the list
is invisible to it. Nothing about that is visible when the base class is
`object`, so this file now imports the genuine one whenever the benchmark
repo is sitting next to this one, and skips the harness tests when it is not.

    python3 -m pytest tests/test_emit.py -q
"""

import ast
import json
import os
import shutil
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pptxgym import emit, pipeline as pl                       # noqa: E402

WORK = ROOT / "work"

# The benchmark checkout. `desktop_env.task_base` is stdlib-only, so the real
# `BaseTask` can be imported from it without the machine's worth of
# dependencies the rest of `desktop_env` needs.
HARNESS = Path(os.environ.get("OSWORLD_HARNESS_REPO",
                              ROOT.parent / "osworld2.0-rollout"))
# The validated Linux/WPS task this one is shaped after.
REFERENCE_TASK = (HARNESS / "evaluation_examples" / "task_class"
                  / "task_1170003.py")

_BASE_TASK = None


def _harness_repo():
    return HARNESS if (HARNESS / "desktop_env" / "task_base.py").exists() else None


def _stub_harness():
    """Put the names a generated task imports at module level into `sys.modules`.

    `desktop_env.evaluators.getters` is stubbed either way: importing it for
    real drags in lxml, selenium and a dozen more, and the two functions a
    generated task uses are the seam a test drives `evaluate` through anyway.
    `BaseTask` is *not* stubbed when the real one is reachable.
    """
    global _BASE_TASK
    if _BASE_TASK is None:
        repo = _harness_repo()
        if repo is not None:
            if str(repo) not in sys.path:
                sys.path.insert(0, str(repo))
            import desktop_env.task_base as task_base           # noqa: PLC0415
            _BASE_TASK = task_base.BaseTask
        else:
            for name in ("desktop_env", "desktop_env.task_base"):
                sys.modules.setdefault(name, types.ModuleType(name))
            sys.modules["desktop_env.task_base"].BaseTask = object
            _BASE_TASK = object
        sys.modules.setdefault("desktop_env.evaluators",
                               types.ModuleType("desktop_env.evaluators"))
        getters = types.ModuleType("desktop_env.evaluators.getters")
        getters.get_vm_file = lambda *a, **k: None
        getters.get_vm_command_line = lambda *a, **k: ""
        sys.modules["desktop_env.evaluators.getters"] = getters
    return _BASE_TASK


def _requires_harness():
    if _harness_repo() is None:
        pytest.skip(f"no benchmark checkout at {HARNESS}")


def _reference_attrs():
    """The class attributes of a shipped, validated Linux/WPS task."""
    tree = ast.parse(REFERENCE_TASK.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(isinstance(b, ast.Name) and b.id == "BaseTask"
                   for b in node.bases):
            continue
        out = {}
        for item in node.body:
            if (isinstance(item, ast.Assign) and len(item.targets) == 1
                    and isinstance(item.targets[0], ast.Name)):
                try:
                    out[item.targets[0].id] = ast.literal_eval(item.value)
                except ValueError:
                    pass
        return out
    raise AssertionError(f"no BaseTask subclass in {REFERENCE_TASK}")


class FakeController:
    """A `SetupController` that records instead of touching a machine."""

    def __init__(self):
        self.calls = []

    def execute(self, command, stdout="", stderr="", shell=False, until=None,
                quiet=False, timeout=120):
        self.calls.append(("execute", command))

    def launch(self, command, shell=False):
        self.calls.append(("launch", command))

    def download(self, files):
        self.calls.append(("download", files))

    def _upload_file_setup(self, files):
        self.calls.append(("upload", files))

    def of(self, kind):
        return [payload for name, payload in self.calls if name == kind]


class FakeEnv:
    """Serves one file per VM path and records every command `evaluate` runs.

    `disk_sha` is what `sha256sum` reports for the deck at the pinned path —
    the single fact the save contract turns on.
    """

    def __init__(self, disk_sha, files, save="SAVED", stray=""):
        self.disk_sha, self.files, self.save, self.stray = disk_sha, files, save, stray
        self.commands, self.fetched = [], []

    def install(self, mod):
        def command_line(_env, config):
            command = config["command"][-1]
            self.commands.append(self.kind(command))
            if "sha256sum" in command:
                return (self.disk_sha or "MISSING") + "\n"
            if "SAVE_FAILED" in command:
                return self.save + "\n"
            if command.startswith("find "):
                return self.stray + "\n"
            return ""

        def vm_file(_env, config):
            self.fetched.append(config["path"])
            return self.files.get(config["path"])

        mod.get_vm_command_line = command_line
        mod.get_vm_file = vm_file
        return self

    @staticmethod
    def kind(command):
        if "sha256sum" in command:
            return "sha"
        if "SAVE_FAILED" in command:
            return "save"
        if command.startswith("pkill"):
            return "kill"
        if ".~lock." in command:
            return "unlock"
        if command.startswith("find "):
            return "scan"
        return command[:40]


def _a_packaged_deck(tmp_path):
    """Package the first deck whose plan was accepted, or skip."""
    if not WORK.exists():
        pytest.skip("no work/ in this checkout")
    for deck in pl.decks_in(WORK):
        plan = deck.root / "plan.json"
        if not plan.exists() or json.loads(plan.read_text()).get("rejected"):
            continue
        try:
            return deck, emit.emit(deck, tmp_path, "9900001")
        except emit.EmitError:
            continue
    pytest.skip("no deck with an accepted plan")


def _load(out: dict):
    """Import the generated file the way `task_loader._load_task_module` does."""
    import importlib.util as u
    _stub_harness()
    spec = u.spec_from_file_location("packaged_task", out["py"])
    mod = u.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# self-containment
# --------------------------------------------------------------------------- #


def test_the_task_runs_without_python_pptx(tmp_path):
    """The harness runs this file where `pptxgym` does not exist and
    `python-pptx` may not either; that is why the runtime is embedded rather
    than imported, and an import that sneaks back in breaks it only in the
    place nobody can debug."""
    _, out = _a_packaged_deck(tmp_path)
    saved = sys.modules.get("pptx")
    sys.modules["pptx"] = None
    try:
        mod = _load(out)
        assert hasattr(mod, "inventory_pptx") and hasattr(mod, "score")
    finally:
        if saved is None:
            sys.modules.pop("pptx", None)
        else:
            sys.modules["pptx"] = saved


def test_the_ground_truth_scores_one_and_the_broken_file_zero(tmp_path):
    """Both ends of the scale, through the emitted file rather than the
    library it came from — an emitter that drops a field would still pass a
    test run against `comparators` directly."""
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    plan = json.loads((mod.TEST_ASSETS / "plan.json").read_text())
    gt = json.loads((mod.TEST_ASSETS / "gt_inventory.json").read_text())
    init = json.loads((mod.TEST_ASSETS / "init_inventory.json").read_text())
    assert mod.score(plan, gt, gt, init)["score"] == pytest.approx(1.0)
    assert mod.score(plan, init, gt, init)["score"] == pytest.approx(0.0)


def test_weights_sum_to_one(tmp_path):
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    task = mod.TASK_CLASS()
    assert sum(task.WEIGHTS.values()) == pytest.approx(1.0)
    assert set(task.WEIGHTS) == set(task.DESCRIPTIONS)


# --------------------------------------------------------------------------- #
# the answer key
# --------------------------------------------------------------------------- #


def test_the_answer_key_is_never_uploaded(tmp_path):
    """A delta-derived evaluator has to read the ground truth, so the usual
    rule — "the evaluator must not touch test fixtures" — cannot apply. The
    invariant that replaces it runs the other way: nothing the evaluator
    reads may reach the machine the agent works on."""
    _, out = _a_packaged_deck(tmp_path)
    assert emit.check_package(Path(out["py"]), Path(out["assets"])) == []


def test_an_answer_key_in_the_agent_folder_is_caught(tmp_path):
    _, out = _a_packaged_deck(tmp_path)
    adir = Path(out["assets"])
    shutil.copy2(adir / "tests" / "assets" / "plan.json", adir / "assets" / "plan.json")
    problems = emit.check_package(Path(out["py"]), adir)
    assert problems and "plan.json" in problems[0]


def test_a_setup_that_uploads_the_ground_truth_is_caught(tmp_path):
    _, out = _a_packaged_deck(tmp_path)
    py = Path(out["py"])
    bad = tmp_path / "bad_task.py"
    bad.write_text(py.read_text().replace('AGENT_ASSETS / "init.pptx"',
                                          'AGENT_ASSETS / "gt_inventory.json"'))
    problems = emit.check_package(bad, Path(out["assets"]))
    assert problems and "gt_inventory.json" in problems[0]


# --------------------------------------------------------------------------- #
# a zero has to be explicable
# --------------------------------------------------------------------------- #


def test_a_failure_keeps_its_breakdown_and_its_reason(tmp_path):
    """`score` is always a number in 0-1, and nothing sits beside it competing
    for that meaning: the harness reads that field, so a status invented to
    qualify it protects nothing and only invites the belief that a problem is
    handled. What the extra keys buy is a zero somebody can explain later, so
    the thing to prevent is a bare 0.0 with no reason and no breakdown."""
    _, out = _a_packaged_deck(tmp_path)
    task = _load(out).TASK_CLASS()
    r = task._fail_all("the deck was never written to disk", {"scored_file": "x"})
    assert r["score"] == 0.0 and isinstance(r["score"], float)
    assert r["failure_reason"]
    assert r["evidence"]["scored_file"] == "x"
    assert set(r["partial_scores"]) == set(task.WEIGHTS)
    assert all("failed:" in p["description"] for p in r["partial_scores"].values())


def test_a_scored_run_carries_its_diagnostics(tmp_path):
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    task = mod.TASK_CLASS()
    plan = json.loads((mod.TEST_ASSETS / "plan.json").read_text())
    gt = json.loads((mod.TEST_ASSETS / "gt_inventory.json").read_text())
    init = json.loads((mod.TEST_ASSETS / "init_inventory.json").read_text())
    r = task._render(mod.score(plan, gt, gt, init), {"scored_file": "x"})
    assert r["score"] == pytest.approx(1.0)
    assert r["evidence"]["scored_file"] == "x"
    assert "hard_gates" in r and "failed_gate" in r


# --------------------------------------------------------------------------- #
# the instruction carries its own constraints
# --------------------------------------------------------------------------- #


def test_the_instruction_pins_the_application_and_the_path(tmp_path):
    """Each sentence is here because its absence cost a rollout: a second
    application on the same deck turned 19 slides into 61, and work left in a
    GUI is invisible to an evaluator that reads the file."""
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    text = mod.TASK_CLASS().instruction
    assert "WPS Presentation only" in text
    assert mod.DECK_VM_PATH in text
    assert "Do not rename or move it." in text


# --------------------------------------------------------------------------- #
# the harness
# --------------------------------------------------------------------------- #


def test_the_generated_task_is_a_real_basetask(tmp_path):
    """Loaded the way the benchmark loads it, with the genuine base class.

    `task_loader._instantiate_task_from_module` looks for `get_task()`, then
    `TASK_CLASS`, then any `BaseTask` subclass — and the fallback is a real
    `issubclass` check, so a class that only *looks* like a task is not one.
    """
    _requires_harness()
    _, out = _a_packaged_deck(tmp_path)
    base = _stub_harness()
    assert base is not object, "the real BaseTask should be in use here"
    task = _load(out).TASK_CLASS()
    assert isinstance(task, base)
    assert isinstance(task, dict), "the runner reads the task as a mapping"


def test_every_field_the_harness_reads_is_present_and_typed(tmp_path):
    """The runner reads the task through `dict.get`, not through `getattr`.

    `BaseTask.__init__` copies exactly `_fields()` into the dict, so a class
    attribute outside that list is invisible to `DesktopEnv._task_get` no
    matter how it is spelled. These are the keys the runner actually looks
    up, with the type each is used as.
    """
    _requires_harness()
    _, out = _a_packaged_deck(tmp_path)
    task = _load(out).TASK_CLASS()
    expected = {
        "id": str,                       # joined into the cache dir path
        "instruction": str,              # run.py: example["instruction"]
        "config": list,                  # _set_task_info
        "proxy": bool,                   # reset(): proxy negotiation
        "platform": str,
        "related_apps": list,
        "snapshot": str,                 # names the image the task needs
        "trajectory": str,
        "source": str,
        "disable_vnc": bool,             # _apply_task_runtime_overrides
        "disable_recording": bool,
        "intermediate_eval_safe": bool,
        "volume_size": int,
    }
    for key, kind in expected.items():
        assert key in task, f"{key} is not in the dict the runner reads"
        assert isinstance(task[key], kind), f"{key} is {type(task[key]).__name__}"
    assert task["id"] and task["instruction"]
    assert task["user_simulator"] is None and task["evaluator"] is None


def test_the_class_attributes_match_the_validated_wps_reference(tmp_path):
    """Field by field against a task that has actually been run.

    Only the runtime-relevant ones: `id`, `instruction` and `source` are
    per-task by construction, and everything else describes how the task
    wants to be run — which is the part it is not allowed to invent.
    """
    _requires_harness()
    if not REFERENCE_TASK.exists():
        pytest.skip(f"no reference task at {REFERENCE_TASK}")
    _, out = _a_packaged_deck(tmp_path)
    task = _load(out).TASK_CLASS()
    reference = _reference_attrs()
    for key in ("snapshot", "related_apps", "platform", "proxy", "trajectory",
                "fixed_ip", "possibility_of_env_change",
                "intermediate_eval_safe", "volume_size"):
        assert key in reference, f"{key} missing from the reference task"
        assert getattr(task, key) == reference[key], (
            f"{key}: {getattr(task, key)!r} != reference {reference[key]!r}")


def test_evaluate_is_marked_unsafe_to_run_mid_episode(tmp_path):
    """`evaluate` force-saves and then kills WPS.

    `lib_run_single._run_inline_checkpoint_eval` runs the task's own
    `evaluate` at whatever steps `--checkpoint_eval_mode` names, and honours
    exactly one flag before doing so. Leaving the `BaseTask` default of True
    means the first checkpointed run closes the application the agent is
    working in and the rollout is spent on nothing.
    """
    _requires_harness()
    _, out = _a_packaged_deck(tmp_path)
    task = _load(out).TASK_CLASS()
    assert task["intermediate_eval_safe"] is False


def test_setup_uploads_the_deck_and_the_materials_and_nothing_else(tmp_path):
    """The upload list is the whole answer-key question, restated.

    A delta-derived evaluator must read the ground truth, so the usual "no
    fixtures in the evaluator" rule cannot hold; what replaces it is that
    nothing the evaluator reads may reach the machine the agent works on.
    Checking the string `check_package` greps for is not the same as checking
    what `setup` actually hands the controller.
    """
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    controller = FakeController()
    mod.TASK_CLASS().setup(controller, use_proxy=False)

    uploads = [f for batch in controller.of("upload") for f in batch]
    assert uploads, "setup uploaded nothing"
    assert uploads[0]["path"] == mod.DECK_VM_PATH
    assert Path(uploads[0]["local_path"]).name == "init.pptx"

    secret = {"plan.json", "gt_inventory.json", "init_inventory.json"}
    for f in uploads:
        local = Path(f["local_path"])
        assert local.name not in secret, f"{local.name} was uploaded"
        assert "tests" not in local.parts, f"{local} came from the test assets"
        assert local.exists(), f"{local} does not exist to upload"
        assert f["path"].startswith("/home/user/Desktop/")


def test_setup_pins_the_file_association_and_launches_wps(tmp_path):
    """A second application on the same deck is how one rollout turned 19
    slides into 61, so `setup` both points `xdg-open` at WPS and opens the
    deck with `wpp` directly rather than through `_open_setup`."""
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    controller = FakeController()
    mod.TASK_CLASS().setup(controller, use_proxy=False)

    assert controller.of("launch") == [["wpp", mod.DECK_VM_PATH]]
    commands = " ".join(controller.of("execute"))
    assert "xdg-mime default wps-office-wpp.desktop" in commands
    assert "presentationml.presentation" in commands
    assert f"rm -f {mod.DECK_VM_PATH}" in commands


def test_a_save_is_forced_only_when_the_disk_still_holds_what_setup_uploaded(tmp_path):
    """Both halves of the save contract, as command sequences.

    Saving unconditionally overwrites work the agent already wrote out;
    never saving loses work that is still only in the GUI. The hash decides,
    and the two branches have to differ in what they *do*, not only in what
    they score — so the assertion is on the sequence.
    """
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    task = mod.TASK_CLASS()
    init = str(Path(out["assets"]) / "assets" / "init.pptx")

    untouched = FakeEnv(mod.INIT_SHA256, {mod.DECK_VM_PATH: init}).install(mod)
    result = task.evaluate(untouched)
    assert untouched.commands == ["sha", "save", "kill", "unlock", "sha", "scan"]
    assert result["evidence"]["save_attempted"] is True
    assert result["score"] == 0.0, "byte-identical to setup is worth nothing"

    moved = FakeEnv("b" * 64, {mod.DECK_VM_PATH: init}).install(mod)
    task.evaluate(moved)
    assert moved.commands == ["sha", "kill", "unlock", "sha"], (
        "a deck that already moved must not be saved over")
    assert "save" not in moved.commands


def test_a_deck_saved_under_another_name_is_found_and_scored(tmp_path):
    """Losing a whole result to a Save-As is a scoring artefact, not a
    capability signal: the work was done, only the filename was wrong."""
    deck, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    stray = "/home/user/Desktop/my copy.pptx"
    env = FakeEnv(mod.INIT_SHA256,
                  {mod.DECK_VM_PATH: str(Path(out["assets"]) / "assets" / "init.pptx"),
                   stray: str(deck.source)},
                  save="SAVE_FAILED", stray=stray).install(mod)
    result = mod.TASK_CLASS().evaluate(env)
    assert env.fetched == [mod.DECK_VM_PATH, stray]
    assert result["evidence"]["scored_file"] == stray
    assert result["score"] == pytest.approx(1.0)


def test_the_metadata_carries_the_instruction_the_agent_receives(tmp_path):
    """Nothing in the harness reads `metadata.json`, which is exactly why it
    drifts: it is what a reviewer reads, so an instruction there that is
    missing the constraints the task actually carries gets a task approved on
    a description of itself that is not true."""
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    meta = json.loads((Path(out["assets"]) / "metadata.json").read_text())
    assert meta["instruction"] == mod.TASK_CLASS().instruction
    assert meta["input_file"] == mod.DECK_VM_PATH
    assert meta["related_apps"] == mod.TASK_CLASS().related_apps
    assert meta["evaluator"] == mod.EVALUATOR_ID
    assert meta["requires_image"]


def test_a_rejected_plan_is_never_packaged(tmp_path):
    """`scored` and `hardened` exist to reject; packaging one of their
    rejects anyway would make both of them decorative."""
    if not WORK.exists():
        pytest.skip("no work/ in this checkout")
    for deck in pl.decks_in(WORK):
        plan = deck.root / "plan.json"
        if plan.exists() and json.loads(plan.read_text()).get("rejected"):
            with pytest.raises(emit.EmitError):
                emit.emit(deck, tmp_path, "9900002")
            return
    pytest.skip("no rejected plan in this checkout")
