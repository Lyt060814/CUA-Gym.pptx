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
import importlib
import json
import os
import shutil
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pptxgym import comparators, emit, inventory, pipeline as pl   # noqa: E402

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


# Comparators that read run-level properties. A deck whose plan uses none of
# them cannot tell the flat-namespace bug from a correct emitter, because the
# runs it drops are never looked at.
RUN_LEVEL_OPS = {"set_font", "text_runs", "recolor"}


def _accepted_decks():
    """Every deck with a plan that was not rejected, and the operators it uses."""
    if not WORK.exists():
        return []
    found = []
    for deck in pl.decks_in(WORK):
        path = deck.root / "plan.json"
        if not path.exists():
            continue
        plan = json.loads(path.read_text())
        if plan.get("rejected"):
            continue
        ops = frozenset(c["op"] for c in plan.get("components", []))
        if ops:
            found.append((deck, ops))
    return found


@pytest.fixture(scope="module")
def two_packaged_decks(tmp_path_factory):
    """Two real decks with different operator mixes, one of them run-level.

    Emitting is a second per deck and every test below wants the same two, so
    this is built once. The run-level one is not optional: it is the only
    kind of deck on which a scoring runtime that has lost its run data still
    scores 1.0 on some components and so looks fine.
    """
    decks = _accepted_decks()
    if not decks:
        pytest.skip("no deck with an accepted plan in this checkout")
    run_level = [d for d in decks if d[1] & RUN_LEVEL_OPS]
    if not run_level:
        pytest.skip(f"no accepted plan uses {sorted(RUN_LEVEL_OPS)}, so no "
                    f"deck here can see run-level divergence")
    chosen, mixes = [], set()
    for deck, ops in run_level + [d for d in decks if d not in run_level]:
        if ops in mixes:
            continue
        mixes.add(ops)
        chosen.append((deck, ops))
        if len(chosen) == 2:
            break
    if len(chosen) < 2:
        pytest.skip("only one operator mix among the accepted plans")

    root = tmp_path_factory.mktemp("two_decks")
    packaged = []
    for i, (deck, ops) in enumerate(chosen):
        out = emit.emit(deck, root, f"99001{i:02d}")
        packaged.append((deck, ops, out, _load(out)))
    return packaged


def _stable(value):
    """A value's contents as text, with the orders that are not meaningful gone.

    A `set` literal comes back from a `.pyc` in a different order than the one
    a fresh `compile()` produces, so `repr` on a set says two identical
    constants differ. Sorting the parts says what is actually being compared:
    the contents.
    """
    if isinstance(value, (set, frozenset)):
        return "{" + ",".join(sorted(_stable(v) for v in value)) + "}"
    if isinstance(value, dict):
        return "{" + ",".join(sorted(f"{_stable(k)}: {_stable(v)}"
                                     for k, v in value.items())) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_stable(v) for v in value) + "]"
    # A registry of comparators is a dict of functions, and `repr` on those is
    # a memory address: it would report every emitted file as different from
    # every other. What the registry has to hold is the same *functions*.
    if isinstance(value, types.FunctionType):
        return f"def {value.__name__}{_fingerprint(value.__code__)!r}"
    if isinstance(value, type):
        return f"class {value.__qualname__}"
    return repr(value)


def _fingerprint(code):
    """What a code object *is*, ignoring where it was compiled from.

    Comparing functions by identity is useless across two compilations of the
    same text and comparing them by name is what let the bug through, so this
    compares the bytecode, the names it reaches for and the constants it
    carries — nested code objects included, or a decorated function would
    compare equal to any other with the same wrapper.
    """
    return (
        code.co_argcount, code.co_kwonlyargcount, code.co_flags & 0x0F,
        code.co_names, code.co_varnames,
        tuple(_fingerprint(c) if isinstance(c, types.CodeType) else _stable(c)
              for c in code.co_consts),
        code.co_code,
    )


def _defined_here(module):
    """Top-level names a module binds, as `{name: comparable fingerprint}`."""
    out = {}
    for name, value in vars(module).items():
        if name.startswith("__"):
            continue
        if isinstance(value, types.FunctionType):
            out[name] = ("function", _fingerprint(value.__code__))
        elif isinstance(value, type):
            out[name] = ("class", tuple(
                (attr, _fingerprint(fn.__code__))
                for attr, fn in sorted(vars(value).items())
                if isinstance(fn, types.FunctionType)))
        elif isinstance(value, (int, float, str, bytes, bool, type(None),
                                tuple, frozenset, set, list, dict)):
            out[name] = ("value", _stable(value))
    return out


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
# the emitted file is the same scorer, not a similar one
# --------------------------------------------------------------------------- #
#
# The whole reason a task carries its evaluator inside it is that a task which
# imports one can drift away from what it was calibrated against. Embedding
# does not make drift impossible, it makes it silent: the runtime used to be
# two modules concatenated into one namespace, both define `_para_runs`,
# `_sha` and `main`, the later copy won, and inventory's calls to its own
# `_para_runs` reached comparators' — which takes a dict, so
# `Element.get("text")` returned None instead of raising and every run in the
# deck fell out of the inventory computed on the VM. The ground truth beside
# it is baked by the real module and still had them, so an agent that restored
# `deck0010` perfectly scored 0.8235.
#
# Nothing raised, no test failed, and no amount of testing `comparators`
# directly could have found it. So what these check is not "the emitted file
# works" but "the emitted file *is* `pptxgym.inventory` and
# `pptxgym.comparators`" — by name, by bytecode, and by result on real decks.


def test_the_emitted_file_defines_no_name_twice(tmp_path):
    """A second definition of a top-level name is not an error in Python; it
    is an assignment. Concatenating two modules into one file makes that the
    normal case rather than the exception, and the loser is whichever module
    was written first — silently, at import, with no traceback anywhere."""
    _, out = _a_packaged_deck(tmp_path)
    tree = ast.parse(Path(out["py"]).read_text())
    seen, twice = {}, []
    for node in tree.body:
        names = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        for name in names:
            if name in seen and name != "_EMBEDDED_SOURCE":
                twice.append(f"{name}: line {seen[name]} then line {node.lineno}")
            seen[name] = node.lineno
    assert not twice, ("the generated file defines these names twice, and the "
                       "second definition wins: " + "; ".join(twice))


def test_the_two_embedded_modules_do_not_share_a_namespace(tmp_path):
    """The specific accident, stated as an invariant.

    `_para_runs` is the one that bit: inventory's takes an XML element,
    comparators' takes a dict. Either can shadow the other and neither
    raises.
    """
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    assert hasattr(mod, "inventory") and hasattr(mod, "comparators"), (
        "the runtime is not carried as two modules, so a name defined by both "
        "of them resolves to whichever was written last")
    assert mod.inventory.__dict__ is not mod.comparators.__dict__
    for name in ("_para_runs", "_sha", "main"):
        assert vars(mod.inventory)[name] is not vars(mod.comparators)[name], (
            f"{name} is one object in the emitted file and two in the library")


def test_every_name_the_runtime_defines_means_the_same_thing_here(tmp_path):
    """Name by name, against the modules the pipeline scores with.

    This is the check that generalises: it does not know which three names
    collided, only that a name `pptxgym.inventory` defines has to be, in the
    emitted file, the function `pptxgym.inventory` defines — same bytecode,
    same constants, same names reached for. Any future helper that two
    embedded modules happen to share fails here on the day it is added.
    """
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    for name in emit.EMBEDDED_MODULES:
        real = importlib.import_module(f"pptxgym.{name}")
        # `mod` itself is the fallback for a runtime that is still flat, so
        # this reports the shadowing rather than an AttributeError about it.
        here = getattr(mod, name, mod)
        expected, actual = _defined_here(real), _defined_here(here)
        wrong = sorted(key for key, value in expected.items()
                       if actual.get(key) != value)
        assert not wrong, (
            f"in the emitted file these names do not mean what they mean in "
            f"pptxgym.{name}: {wrong}"
            + "".join(f"\n  {key}: missing" if key not in actual else ""
                      for key in wrong))


def test_the_inventory_the_task_computes_is_the_one_the_ground_truth_was_baked_with(
        two_packaged_decks):
    """The two must be the same function, or the answer key describes a deck
    the evaluator cannot see.

    `gt_inventory.json` is written here by `pptxgym.inventory`; the candidate
    is read on the VM by the copy inside the task. A field the embedded copy
    drops is a field present on one side of every comparison and absent from
    the other, which reads as an agent that did not do the work.
    """
    for deck, ops, out, mod in two_packaged_decks:
        for label, path in (("the restored deck", deck.source),
                            ("the damaged deck", deck.input_pptx)):
            mine = mod.inventory_pptx(str(path))
            theirs = inventory.inventory_pptx(str(path))
            if mine != theirs:
                missing = [k for k in inventory.flatten(theirs)
                           if k not in inventory.flatten(mine)]
                raise AssertionError(
                    f"{deck.id} {sorted(ops)}: the inventory the task computes "
                    f"for {label} is not the one the pipeline computes — "
                    f"{len(missing)} leaves missing, e.g. {missing[:3]}")


def test_the_score_the_task_computes_is_the_pipeline_s(two_packaged_decks):
    """End to end on real decks, through the path a rollout takes: the task
    reads a `.pptx` off the VM with its own inventory and scores it with its
    own comparators. Feeding both sides a candidate that was built by the
    *library* would hide exactly the bug this exists for."""
    for deck, ops, out, mod in two_packaged_decks:
        assets = Path(out["assets"]) / "tests" / "assets"
        plan = json.loads((assets / "plan.json").read_text())
        gt = json.loads((assets / "gt_inventory.json").read_text())
        init = json.loads((assets / "init_inventory.json").read_text())
        for label, path, expected in (
                ("the restored deck", deck.source, 1.0),
                ("the damaged deck", deck.input_pptx, 0.0)):
            mine = mod.score(plan, mod.inventory_pptx(str(path)), gt, init)
            theirs = comparators.score(
                plan, inventory.inventory_pptx(str(path)), gt, init)
            assert mine == theirs, (
                f"{deck.id} {sorted(ops)}: on {label} the task scores "
                f"{mine['score']:.4f} where the pipeline scores "
                f"{theirs['score']:.4f}")
            assert mine["score"] == pytest.approx(expected), (
                f"{deck.id}: {label} should score {expected}")


def test_the_two_decks_cover_different_operators_and_one_reads_runs(
        two_packaged_decks):
    """What the two tests above are worth depends entirely on this.

    Run-level styling is where the collision showed, and a plan that never
    reads a run scores 1.0 either way — so a suite that happened to pick two
    such decks would be green against the broken emitter.
    """
    mixes = [ops for _, ops, _, _ in two_packaged_decks]
    assert len(set(mixes)) == len(mixes), "both decks exercise the same mix"
    assert any(ops & RUN_LEVEL_OPS for ops in mixes), (
        "neither deck reads run-level properties, so neither can see a "
        "runtime that has lost them")


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


def test_a_bug_in_the_evaluator_is_not_returned_as_a_zero(tmp_path):
    """A zero says "the agent did not do the work". Nothing else may say it.

    `evaluate` used to wrap its whole body in `except Exception` and return
    `_fail_all`, so an internal error — a getter that could not reach the
    machine, an answer key that was not beside the task, a comparator that
    tripped over its own input — arrived downstream as a well-formed 0.0 with
    a breakdown and a reason, indistinguishable from an agent that opened
    nothing. That is the worst possible shape for training data: it is not
    noise, it is a confident wrong label.

    Running the previous suite with `-W error::DeprecationWarning` is how it
    surfaced. The shadowed `_para_runs` was calling `bool()` on an XML
    element, `ElementTree` warns that this will raise one day, and with the
    warning promoted to an error the task reported 0.0 on a *perfect* deck.
    """
    deck, out = _a_packaged_deck(tmp_path)
    mod = _load(out)

    def explode(*_a, **_k):
        raise RuntimeError("a comparator tripped over its own input")

    mod.score = explode
    env = FakeEnv("b" * 64, {mod.DECK_VM_PATH: str(deck.source)}).install(mod)
    with pytest.raises(RuntimeError, match="tripped over its own input"):
        mod.TASK_CLASS().evaluate(env)


def test_a_machine_that_does_not_answer_is_not_returned_as_a_zero(tmp_path):
    """The same rule one stage earlier. A VM that cannot be reached is an
    infrastructure failure; scoring it 0.0 files it as an agent failure, and
    the file it lands in is the one the reward is computed from."""
    deck, out = _a_packaged_deck(tmp_path)
    mod = _load(out)

    def unreachable(*_a, **_k):
        raise ConnectionError("no route to the machine")

    mod.get_vm_command_line = unreachable
    with pytest.raises(ConnectionError):
        mod.TASK_CLASS().evaluate(object())


def _not_a_pptx(tmp_path, kind):
    """The two shapes a Save-As can leave behind that are not a presentation."""
    path = tmp_path / f"saved_as_a_pptx_but_is_not_one_{kind}.pptx"
    if kind == "not a zip":                    # e.g. Save As -> .ppt, binary
        path.write_bytes(b"this is not a zip container")
    else:                                      # e.g. Save As -> .odp, a zip
        import zipfile as zf
        with zf.ZipFile(path, "w") as z:
            z.writestr("mimetype", "application/vnd.oasis.opendocument.presentation")
            z.writestr("content.xml", "<office:document-content/>")
    return path


@pytest.mark.parametrize("kind", ["not a zip", "a zip that is not a deck"])
def test_a_file_that_is_not_a_deck_scores_zero_with_a_reason(tmp_path, kind):
    """And the narrowing has to stop somewhere true.

    Bytes at the pinned path that are not a presentation *are* the agent's
    doing and have a real answer: zero, with the reason kept. `_not_a_deck`
    settles both shapes of that before the inventory runs, which is precisely
    what lets the exception list below stay as short as it is — the
    `ValueError` the inventory raises from its own `is_zipfile` guard never
    has to be caught, because this decides first.
    """
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    junk = _not_a_pptx(tmp_path, kind)
    env = FakeEnv("b" * 64, {mod.DECK_VM_PATH: str(junk)}).install(mod)

    result = mod.TASK_CLASS().evaluate(env)                  # raises nothing
    assert result["score"] == 0.0 and isinstance(result["score"], float)
    assert "not a readable .pptx" in result["failure_reason"]
    assert result["failure_reason"].rsplit("—", 1)[-1].strip(), (
        "a zero has to say why, and this one says nothing after the dash")
    assert set(result["partial_scores"]) == set(mod.TASK_CLASS().WEIGHTS)


@pytest.mark.parametrize("error", [
    ValueError("not a pptx package: /home/user/Desktop/task.pptx"),
    KeyError("ppt/slides/slide1.xml"),
])
def test_the_exceptions_evaluate_catches_stay_narrow(tmp_path, error):
    """The other direction, and the one that actually holds the line.

    `ValueError` and `KeyError` are the two most tempting additions to
    `UNREADABLE_DECK` — the inventory raises the first from its own
    `is_zipfile` guard and the second for a part that is not in the package —
    and they are also the two commonest signatures of an ordinary bug: a
    dict lookup that missed, an int that would not parse. Catch them and a
    comparator tripping over its own input becomes a plausible zero nobody
    can tell from an agent who did nothing.

    `_not_a_deck` already answers the cases that made them tempting, so
    widening the tuple buys nothing and costs the distinction. This test
    turns red the day somebody adds them back.
    """
    deck, out = _a_packaged_deck(tmp_path)
    mod = _load(out)

    def raiser(_path):
        raise error

    mod.inventory_pptx = raiser
    env = FakeEnv("b" * 64, {mod.DECK_VM_PATH: str(deck.source)}).install(mod)
    with pytest.raises(type(error)):
        mod.TASK_CLASS().evaluate(env)


def test_a_score_outside_zero_to_one_is_refused_rather_than_returned(tmp_path):
    """`score` is the only verdict this returns, so the one thing it must be
    is a number in 0-1. A runtime that hands back anything else is broken in
    a way nothing downstream can detect — the harness would happily record
    1.4 — so it stops here, loudly, instead of being rounded and passed on."""
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    task = mod.TASK_CLASS()
    out_of_range = {"score": 1.4, "components": [], "hard_gates": []}
    with pytest.raises(AssertionError, match="not a score in 0-1"):
        task._render(out_of_range, {})


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
