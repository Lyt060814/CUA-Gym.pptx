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
import subprocess
import sys
import time
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
    the single fact the save contract turns on. `saved_sha`, when given, is
    what it reports *after* the forced save, which is how a save that worked
    is told from a keystroke that went nowhere; `keystroke` is only what the
    script printed, and the point of the contract is that it does not decide.

    `strays` is `{path: digest}` because the scan digests what it finds on the
    machine: a stray whose bytes are the deck we uploaded, or a file we handed
    the agent ourselves, is not an answer, and the digest is what says so.

    `reference` is what the machine says it measured "newer" against, because
    the scan now resolves that on the VM: `"none"` is a real machine answer
    meaning nothing survived to date the end of setup, and it is not the same
    as finding no files. `stray_lines` is the raw scan output for the one
    question a `{path: digest}` dict cannot ask — `find` is given two
    overlapping roots and reports the same Desktop file twice.
    """

    def __init__(self, disk_sha, files, keystroke="KEYSTROKE_SENT", stray="",
                 strays=None, saved_sha=None, reference="/home/user/.setup",
                 stray_lines=None):
        self.disk_sha, self.files = disk_sha, files
        self.keystroke, self.saved_sha = keystroke, saved_sha
        self.strays = dict(strays or {})
        self.reference, self.stray_lines = reference, stray_lines
        if stray:
            self.strays.setdefault(stray, "c" * 64)
        self.commands, self.fetched = [], []

    def install(self, mod):
        def command_line(_env, config):
            command = config["command"][-1]
            kind = self.kind(command)
            self.commands.append(kind)
            if kind == "scan":
                head = f"REFERENCE {self.reference}\n" if self.reference else ""
                if self.stray_lines is not None:
                    return head + "".join(f"{line}\n" for line in self.stray_lines)
                if self.reference == "none":
                    return head
                return head + "".join(f"{digest}  {path}\n"
                                      for path, digest in self.strays.items())
            if kind == "sha":
                return (self.disk_sha or "MISSING") + "\n"
            if kind == "save":
                if self.saved_sha is not None:
                    self.disk_sha = self.saved_sha
                return self.keystroke + "\n"
            return ""

        def vm_file(_env, config):
            self.fetched.append(config["path"])
            return self.files.get(config["path"])

        mod.get_vm_command_line = command_line
        mod.get_vm_file = vm_file
        return self

    @staticmethod
    def kind(command):
        # the scan digests what it finds, so it is asked about first: it
        # contains `sha256sum` too, and the order used to be the other way.
        # Matched on what it looks for rather than on `startswith("find ")` —
        # the scan now resolves its own reference point in the shell first,
        # and a classifier keyed on the first word silently reported the whole
        # scan as an unrecognised command.
        if "-name '*.pptx'" in command:
            return "scan"
        if "sha256sum" in command:
            return "sha"
        if "ctrl+s" in command:
            return "save"
        if command.startswith("pkill"):
            return "kill"
        if ".~lock." in command:
            return "unlock"
        return command[:40]


def frozen_work() -> Path:
    """A `work/`-shaped tree of decks this suite built, planned and bundled.

    Everything below used to package *whichever* deck of `work/` came first
    with a plan that was not rejected.  That made the emitter's tests a
    function of the corpus: a repair that rejects deck0002 silently changes
    which deck fifty-odd assertions are about, and a deck that changes shape
    changes what they mean.  The decks here are built from nothing by
    `tests/fixtures/minidecks.py` and never move.
    """
    import minidecks
    return minidecks.frozen_work()


def _a_packaged_deck(tmp_path, work=None):
    """Package the first frozen deck whose plan was accepted, or skip.

    `work` defaults to the frozen tree; pass `WORK` to ask the same of the
    live corpus, which only a `@pytest.mark.corpus` test may do.
    """
    work = frozen_work() if work is None else work
    if not work.exists():
        pytest.skip("no work tree in this checkout")
    for deck in pl.decks_in(work):
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


def _accepted_decks(work=None):
    """Every deck with a plan that was not rejected, and the operators it uses."""
    work = frozen_work() if work is None else work
    if not work.exists():
        return []
    found = []
    for deck in pl.decks_in(work):
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
    """Two frozen decks with different operator mixes, one of them run-level.

    Emitting is a second per deck and every test below wants the same two, so
    this is built once. The run-level one is not optional: it is the only
    kind of deck on which a scoring runtime that has lost its run data still
    scores 1.0 on some components and so looks fine.

    `mini_plain` is `{delete, set_font, move}` and `mini_inherited` is
    `{delete, move}` — the `set_font` components of the second are dropped as
    unsatisfiable — so the pair is a run-level mix and a mix without one, by
    construction rather than by whatever the corpus happens to hold today.
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


SECRET = ("plan.json", "gt_inventory.json", "init_inventory.json")


def test_a_setup_that_delivers_the_ground_truth_is_caught(tmp_path):
    """The list `setup` puts on the machine used to be an upload of
    `AGENT_ASSETS / "init.pptx"`; it is now the `FETCH` table, because the
    materials moved to a dataset.  The invariant did not move with it, and the
    stakes went up when the mechanism did: a name in `FETCH` is not only
    copied onto the agent's Desktop, it is a file `publish` pushes to a
    **public** HuggingFace repository (`HF_ASSET_REPO`, overridable through
    `PPTXGYM_ASSET_BASE`).  A plan or a ground-truth inventory that reaches
    that list is not one leaked rollout, it is a permanently published answer
    key for every rollout after it.
    """
    _, out = _a_packaged_deck(tmp_path)
    py = Path(out["py"])
    for name in SECRET:
        bad = tmp_path / f"bad_task_{name}.py"
        bad.write_text(py.read_text().replace("/init.pptx'", f"/{name}'"))
        problems = emit.check_package(bad, Path(out["assets"]))
        assert problems and any(name in p for p in problems), name


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


def test_the_instruction_names_the_folder_the_materials_are_actually_in(tmp_path):
    """The per-deck prose is written by the proposal stage and names the
    folder however that deck's sentence came out — "the assets folder" on
    task_1100011, and there is no folder called `assets` anywhere on that
    Desktop. An agent that goes looking for it by name finds nothing, which
    costs steps on the one kind of run that exists to tell a task defect from
    an agent failure. The prose is not ours to write; the placement is, so the
    truth is stated once here, in full."""
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    materials = Path(out["assets"]) / "assets" / "materials"
    if not any(f.is_file() for f in materials.iterdir()):
        pytest.skip("this deck ships no materials")
    text = mod.TASK_CLASS().instruction
    assert mod.MATERIALS_VM_DIR in text, (
        "the instruction never says where the supplied files are, and the "
        "per-deck prose above it may call the folder anything at all")
    assert text.index(mod.MATERIALS_VM_DIR) < text.index("WPS Presentation only")


def _deck_with_no_materials(tmp_path):
    """A real accepted deck, repackaged with nothing in `bundle/assets`.

    Copied file by file rather than by `shutil.copytree`: a deck directory
    carries renders, attempts and both digests, and this needs six files.
    """
    deck, _ = _a_packaged_deck(tmp_path / "probe")
    root = tmp_path / "deck_no_materials"
    (root / "bundle").mkdir(parents=True)
    for name in ("plan.json", "task.json", "source.pptx", "input.pptx"):
        shutil.copy2(deck.root / name, root / name)
    shutil.copy2(deck.root / "bundle" / "input.pptx", root / "bundle" / "input.pptx")
    return pl.Deck(root)


def test_a_task_with_no_materials_promises_no_folder(tmp_path):
    """Worse than the wrong name would be a sentence pointing at a directory
    `setup` never creates: the agent has no way to conclude it is not there
    except by looking for it."""
    deck = _deck_with_no_materials(tmp_path)
    out = emit.emit(deck, tmp_path / "out", "9900003")
    mod = _load(out)
    text = mod.TASK_CLASS().instruction

    assert mod.MATERIALS_VM_DIR not in text
    assert "The files supplied with this task" not in text
    assert "WPS Presentation only" in text, "the constraints still have to be there"
    assert mod.MATERIAL_SHA256 == frozenset()
    controller = FakeController()
    mod.TASK_CLASS().setup(controller, use_proxy=False)
    delivered = [f for batch in controller.of("download") for f in batch]
    assert [f["path"] for f in delivered] == [mod.DECK_VM_PATH], (
        "nothing was delivered to the folder the instruction would have named")
    assert mod.MATERIALS_VM_DIR not in " ".join(controller.of("execute")), (
        "an empty materials folder was created on the Desktop for the agent "
        "to find and open")


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


def test_setup_delivers_the_deck_and_the_materials_and_nothing_else(tmp_path):
    """The delivery list is the whole answer-key question, restated.

    A delta-derived evaluator must read the ground truth, so the usual "no
    fixtures in the evaluator" rule cannot hold; what replaces it is that
    nothing the evaluator reads may reach the machine the agent works on.
    Checking the list `check_package` reads is not the same as checking what
    `setup` actually hands the controller — which is the point of doing it
    here as well as there.

    The materials moved from an upload beside the task file to a fetch from
    the dataset, so the verb is `download` and the local half of each entry is
    a repository path rather than a file on this disk.  What must still hold
    is unchanged: the deck first, nothing the evaluator reads among them, and
    every destination on the agent's Desktop.
    """
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    controller = FakeController()
    mod.TASK_CLASS().setup(controller, use_proxy=False)

    delivered = [f for batch in controller.of("download") for f in batch]
    assert delivered, "setup delivered nothing"
    assert delivered[0]["path"] == mod.DECK_VM_PATH
    assert [f["path"] for f in delivered] == [vm for _, vm, _ in mod.FETCH], (
        "what setup hands the controller is not the list the package records")
    assert Path(mod.FETCH[0][0]).name == "init.pptx"

    staged = Path(out["assets"]) / "assets"
    for (repo_path, vm_path, digest), f in zip(mod.FETCH, delivered):
        name = repo_path.rsplit("/", 1)[-1]
        assert name not in SECRET, (
            f"{name} is in the fetch list, so it would be published to the "
            f"dataset as well as put on the agent's machine")
        assert "tests" not in Path(repo_path).parts, \
            f"{repo_path} came from the test assets"
        assert vm_path.startswith("/home/user/Desktop/")
        assert len(digest) == 64, f"{repo_path} carries no usable sha256"
        # and the bytes that will be published under that name are here
        local = staged / repo_path.split("/", 1)[1]
        assert local.exists(), f"nothing at {local} to publish as {repo_path}"
        assert f["url"].endswith(repo_path)


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
                  keystroke="KEYSTROKE_NOT_SENT", stray=stray).install(mod)
    result = mod.TASK_CLASS().evaluate(env)
    assert env.fetched == [mod.DECK_VM_PATH, stray]
    assert result["evidence"]["scored_file"] == stray
    assert result["score"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# which stray, and whether it is anybody's answer
# --------------------------------------------------------------------------- #
#
# The recovery above exists so a Save-As does not destroy a whole result, and
# that is right. What it must not become is "score the newest .pptx near the
# home directory": two files are eligible by construction and neither is the
# agent's work — a `.pptx` this task uploaded itself (newer than the pinned
# deck, because setup wrote it afterwards) and a copy of the untouched input
# saved out under another name, which is the one thing `evaluate` already
# refuses to score in place. Both are settled by the digest; the task knows
# exactly what it put on the machine.


def test_the_digests_of_everything_setup_uploads_are_baked_into_the_task(tmp_path):
    """The discrimination is only as good as this list, and the list is
    generated. A material added to the bundle and not digested here is a file
    the stray scan would happily score as the agent's answer."""
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    materials = Path(out["assets"]) / "assets" / "materials"
    expected = {emit._sha256(f) for f in sorted(materials.iterdir()) if f.is_file()}
    assert set(mod.MATERIAL_SHA256) == expected
    assert isinstance(mod.MATERIAL_SHA256, frozenset)
    assert mod.INIT_SHA256 not in mod.MATERIAL_SHA256


def test_a_pptx_this_task_supplied_is_never_scored_as_the_answer(tmp_path):
    """The worst shape this bug could take: it is *stable*. Every rollout of
    the task would score the same file we handed the agent, so it would read
    as a capability floor rather than as an evaluator that never looked at the
    agent's work at all."""
    deck, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    material = f"/home/user/Desktop/task_{mod.TASK_CLASS().id}_materials/deck.pptx"
    digest = "d" * 64
    mod.MATERIAL_SHA256 = frozenset({digest})
    init = str(Path(out["assets"]) / "assets" / "init.pptx")
    env = FakeEnv(mod.INIT_SHA256,
                  {mod.DECK_VM_PATH: init, material: str(deck.source)},
                  strays={material: digest}).install(mod)

    result = mod.TASK_CLASS().evaluate(env)
    assert env.fetched == [mod.DECK_VM_PATH], (
        "the file the task uploaded itself was fetched to be scored")
    assert result["score"] == 0.0
    assert any("materials folder" in note
               for note in result["evidence"]["stray_rejected"])


def test_an_unedited_copy_of_the_input_is_not_an_answer_under_another_name(tmp_path):
    """`Save As` with nothing done to the file. Byte-identical to what setup
    uploaded scores zero at the pinned path, and a rename cannot be worth
    more than that."""
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    init = str(Path(out["assets"]) / "assets" / "init.pptx")
    copy_path = "/home/user/Desktop/untitled.pptx"
    env = FakeEnv(mod.INIT_SHA256,
                  {mod.DECK_VM_PATH: init, copy_path: init},
                  strays={copy_path: mod.INIT_SHA256}).install(mod)

    result = mod.TASK_CLASS().evaluate(env)
    assert env.fetched == [mod.DECK_VM_PATH]
    assert result["score"] == 0.0
    assert "byte-identical" in result["failure_reason"]
    assert any("setup uploaded" in note
               for note in result["evidence"]["stray_rejected"])


def test_two_candidate_files_are_not_free_retries(tmp_path):
    """Scoring the best stray is a strictly better strategy than doing the
    work once: leave three attempts on the Desktop and be graded on the
    luckiest. Recovering *the* result is what the scan is for, and with two
    equally eligible files there is no evidence for which that is — so the
    instruction stands and neither is scored, with the reason kept."""
    deck, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    init = str(Path(out["assets"]) / "assets" / "init.pptx")
    first = "/home/user/Desktop/attempt one.pptx"
    second = "/home/user/Desktop/attempt two.pptx"
    env = FakeEnv(mod.INIT_SHA256,
                  {mod.DECK_VM_PATH: init,
                   first: str(deck.source), second: init},
                  strays={first: "a" * 64, second: "b" * 64}).install(mod)

    result = mod.TASK_CLASS().evaluate(env)
    assert env.fetched == [mod.DECK_VM_PATH], (
        "one of two indistinguishable candidates was picked and scored")
    assert result["score"] == 0.0
    note = " ".join(result["evidence"]["stray_rejected"])
    assert "2 files" in note and first in note and second in note


def test_the_only_file_nobody_here_supplied_is_the_one_that_is_scored(tmp_path):
    """All three rules at once: the material and the unedited copy are set
    aside, and what is left — one file whose bytes nobody on this side put
    there — is the agent's result and is scored."""
    deck, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    init = str(Path(out["assets"]) / "assets" / "init.pptx")
    material, copied = "/home/user/materials/deck.pptx", "/home/user/Desktop/copy.pptx"
    answer = "/home/user/Desktop/final version.pptx"
    mod.MATERIAL_SHA256 = frozenset({"d" * 64})
    env = FakeEnv(mod.INIT_SHA256,
                  {mod.DECK_VM_PATH: init, answer: str(deck.source)},
                  strays={material: "d" * 64, copied: mod.INIT_SHA256,
                          answer: "e" * 64}).install(mod)

    result = mod.TASK_CLASS().evaluate(env)
    assert env.fetched == [mod.DECK_VM_PATH, answer]
    assert result["evidence"]["scored_file"] == answer
    assert result["score"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# what the save contract reports
# --------------------------------------------------------------------------- #


def test_the_save_status_reports_the_disk_and_not_the_keystroke(tmp_path):
    """`xdotool` succeeds once a key has been *sent*, and the fallback sends
    `ctrl+s` through pyautogui to whatever has focus — so the script's own
    verdict is consistent with the deck being saved, another window being
    saved, and nothing happening. The digests either side of the attempt are
    the evidence, and `evaluate` already records them; both directions are
    checked here because either alone would pass on a status that just echoed
    the other field."""
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    init = str(Path(out["assets"]) / "assets" / "init.pptx")

    sent_nothing_written = FakeEnv(mod.INIT_SHA256, {mod.DECK_VM_PATH: init},
                                   keystroke="KEYSTROKE_SENT").install(mod)
    result = mod.TASK_CLASS().evaluate(sent_nothing_written)
    evidence = result["evidence"]
    assert evidence["keystroke"] == "KEYSTROKE_SENT"
    assert evidence["save_status"].startswith("NOT_SAVED"), evidence["save_status"]
    assert evidence["disk_sha_after"] == mod.INIT_SHA256

    written_anyway = FakeEnv(mod.INIT_SHA256, {mod.DECK_VM_PATH: init},
                             keystroke="KEYSTROKE_NOT_SENT",
                             saved_sha="f" * 64).install(mod)
    evidence = mod.TASK_CLASS().evaluate(written_anyway)["evidence"]
    assert evidence["keystroke"] == "KEYSTROKE_NOT_SENT"
    assert evidence["save_status"].startswith("SAVED"), evidence["save_status"]
    assert evidence["disk_sha_after"] == "f" * 64


def test_a_deck_that_was_never_saved_over_says_so_without_claiming_a_save(tmp_path):
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    init = str(Path(out["assets"]) / "assets" / "init.pptx")
    env = FakeEnv("b" * 64, {mod.DECK_VM_PATH: init}).install(mod)
    evidence = mod.TASK_CLASS().evaluate(env)["evidence"]
    assert evidence["save_attempted"] is False
    assert evidence["keystroke"] == "not sent"
    assert evidence["save_status"].startswith("not needed")


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


def _rejected_decks(work):
    return [deck for deck in pl.decks_in(work)
            if (deck.root / "plan.json").exists()
            and json.loads((deck.root / "plan.json").read_text()).get("rejected")]


def test_a_rejected_plan_is_never_packaged(tmp_path):
    """`scored` and `hardened` exist to reject; packaging one of their
    rejects anyway would make both of them decorative."""
    rejected = _rejected_decks(frozen_work())
    assert rejected, "the frozen tree must hold a refused plan to refuse"
    for deck in rejected:
        with pytest.raises(emit.EmitError):
            emit.emit(deck, tmp_path, "9900002")


@pytest.mark.corpus
def test_no_rejected_plan_in_the_live_corpus_is_packaged(tmp_path):
    """The same question of the ten decks currently in `work/`."""
    rejected = _rejected_decks(WORK)
    if not rejected:
        pytest.skip("no rejected plan in this checkout")
    for deck in rejected:
        with pytest.raises(emit.EmitError):
            emit.emit(deck, tmp_path, "9900002")


# --------------------------------------------------------------------------- #
# where the package came from
# --------------------------------------------------------------------------- #
#
# `work/emitted/` is one flat directory. After a ten-deck run it held nine
# tasks: eight freshly packaged and one built hours earlier from a deck the
# pipeline has since *rejected* — promising two assets that deck's own
# proposal now forbids by name, one of them the deleted picture's own bytes,
# and pointing at a leak that has since been closed. It was byte-for-byte the
# same kind of artefact as the good ones and was nearly pushed.
#
# So the questions below are the ones nobody could answer about that file:
# which deck, what state that deck was in when this was written, when, from
# which commit, from which run — and, the one that matters most, has the deck
# moved on since. The last is deliberately the *same* content-hash comparison
# `Deck.stale` already makes for a stage, because a second staleness
# mechanism is a second thing that can be right when the first is wrong.


def test_a_package_says_which_deck_and_which_build_it_came_from(tmp_path):
    deck, out = _a_packaged_deck(tmp_path)
    rec = emit.read_provenance(Path(out["assets"]))
    assert rec is not None, "the package cannot say where it came from"

    assert rec["deck"] == deck.id
    assert rec["task_id"] == "9900001"
    assert rec["evaluator"] == emit.EVALUATOR_ID
    assert rec["emitted_at"], "no emission time"
    assert rec["deck_stage"], "no record of how far the deck had got"
    assert rec["deck_state"], "no record of the deck's verdict on itself"
    # The commit, when this is a git tree at all. `dirty` is a fact about
    # reproducibility and must be stated rather than assumed either way.
    assert set(rec["code"]) == {"commit", "dirty"}
    # The run id is the slot, not the value: another module is growing one and
    # this is shaped so it drops in. Present-and-null is "nobody told us";
    # absent would be "this build predates the idea", and they differ.
    assert "run" in rec and rec["run"] is None
    assert rec["inputs"], "no digests of what the emitter read"


def test_the_task_file_itself_carries_the_record_not_only_the_folder(tmp_path):
    """The `.py` is the thing somebody copies into a benchmark repo, and it
    was the thing that was indistinguishable. A record that only lives beside
    the assets does not travel with it."""
    deck, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    beside = emit.read_provenance(Path(out["assets"]))
    assert mod.PROVENANCE == beside, "the two copies disagree about the build"
    head = Path(out["py"]).read_text()[:4000]
    for fact in (deck.id, beside["emitted_at"]):
        assert fact in head, f"{fact!r} is not readable at the top of the file"


def test_a_run_id_can_be_recorded_when_the_caller_knows_one(tmp_path):
    """`--run-id` / `run_id=` is the whole integration surface for the
    run-level log: nothing else has to change shape when it lands."""
    deck, _ = _a_packaged_deck(tmp_path)
    out = emit.emit(deck, tmp_path / "with-run", "9900009",
                    run_id="20260805-101112")
    rec = emit.read_provenance(Path(out["assets"]))
    assert rec["run"] == "20260805-101112"
    assert emit.provenance_problems(rec, deck) == []


def test_a_package_whose_deck_has_moved_on_is_detectably_stale(tmp_path):
    """The failure this exists for, reproduced: emit, let the deck move, and
    the package must stop claiming to be current. The deck is copied first —
    a test that edits `work/` is a test that breaks the next run."""
    deck, _ = _a_packaged_deck(tmp_path)
    copy_root = tmp_path / "moved-deck" / deck.id
    shutil.copytree(deck.root, copy_root)
    copied = pl.Deck(copy_root)
    out = emit.emit(copied, tmp_path / "ship", "9900010")
    rec = emit.read_provenance(Path(out["assets"]))
    assert emit.provenance_problems(rec, copied) == [], (
        "a package emitted from this deck a moment ago is not current")

    plan = copy_root / "plan.json"
    plan.write_text(plan.read_text() + "\n")          # same plan, new bytes
    problems = emit.provenance_problems(rec, copied)
    assert any("plan.json has changed" in p for p in problems), problems


def test_a_deck_whose_own_verdict_turned_against_it_is_stale_too(tmp_path):
    """The stale artefact's deck was rejected by a *gate*, and a gate that
    says no usually changes nothing on disk — so the digests alone cannot see
    it. `Deck.stale` can, which is the reason to reuse it rather than invent
    a second check."""
    deck, _ = _a_packaged_deck(tmp_path)
    copy_root = tmp_path / "rejected-deck" / deck.id
    shutil.copytree(deck.root, copy_root)
    copied = pl.Deck(copy_root)
    out = emit.emit(copied, tmp_path / "ship2", "9900011")
    rec = emit.read_provenance(Path(out["assets"]))
    assert emit.provenance_problems(rec, copied) == []

    state = json.loads((copy_root / "state.json").read_text())
    state["reconciled"] = dict(state.get("reconciled", {}), status="rejected")
    (copy_root / "state.json").write_text(json.dumps(state))
    problems = emit.provenance_problems(rec, copied)
    assert any("reconciled:rejected" in p for p in problems), problems
    assert any("packaged stage now reads" in p for p in problems), problems

    plan = json.loads((copy_root / "plan.json").read_text())
    plan["rejected"] = ["a gate said no after this was packaged"]
    (copy_root / "plan.json").write_text(json.dumps(plan))
    assert any("plan is now rejected" in p
               for p in emit.provenance_problems(rec, copied))


def test_a_package_with_no_record_is_reported_rather_than_passed(tmp_path):
    """"We cannot tell" is the state the whole ship directory was in, and it
    is not the same as "fine".

    The two questions the deck can answer without a record are still asked,
    because that is the exact shape of the artefact this exists for: no
    provenance *and* a deck whose plan has since been rejected. `--check`
    falls back to `metadata.json`'s `source_deck` to find the deck at all,
    which is how the nine already in `work/emitted/` get judged.
    """
    deck, out = _a_packaged_deck(tmp_path)
    assert emit.provenance_problems(None, deck) != []
    (Path(out["assets"]) / emit.PROVENANCE_FILE).unlink()
    assert emit.read_provenance(Path(out["assets"])) is None
    assert emit.check_package(Path(out["py"]), Path(out["assets"])) != []

    copy_root = tmp_path / "no-record" / deck.id
    shutil.copytree(deck.root, copy_root)
    copied = pl.Deck(copy_root)
    plan = json.loads((copy_root / "plan.json").read_text())
    plan["rejected"] = ["a gate said no after this was packaged"]
    (copy_root / "plan.json").write_text(json.dumps(plan))
    problems = emit.provenance_problems(None, copied)
    assert any("no provenance.json" in p for p in problems), problems
    assert any("plan is now rejected" in p for p in problems), problems


def test_the_record_is_never_uploaded_to_the_machine(tmp_path):
    """It names the deck, its stage and its verdicts. None of that is the
    agent's business, so it sits beside `assets/` and not inside it."""
    _, out = _a_packaged_deck(tmp_path)
    adir = Path(out["assets"])
    assert (adir / emit.PROVENANCE_FILE).exists()
    assert not (adir / "assets" / emit.PROVENANCE_FILE).exists()

    mod = _load(out)
    controller = FakeController()
    mod.TASK_CLASS().setup(controller, use_proxy=False)
    uploaded = [Path(f["local_path"]).name
                for batch in controller.of("upload") for f in batch]
    assert emit.PROVENANCE_FILE not in uploaded

    shutil.copy2(adir / emit.PROVENANCE_FILE,
                 adir / "assets" / emit.PROVENANCE_FILE)
    assert any(emit.PROVENANCE_FILE in p
               for p in emit.check_package(Path(out["py"]), adir))


def test_the_check_names_the_stale_package_among_the_current_ones(tmp_path):
    """The scan a human runs before pushing. One package from a deck that has
    moved, sitting in a directory of packages that have not — which is the
    exact shape of the directory that nearly shipped."""
    deck, _ = _a_packaged_deck(tmp_path)
    work = tmp_path / "work"
    ship = tmp_path / "ship3"
    good = work / "deck0001"
    stale = work / "deck0002"
    shutil.copytree(deck.root, good)
    shutil.copytree(deck.root, stale)
    emit.emit(pl.Deck(good), ship, "9900020")
    emit.emit(pl.Deck(stale), ship, "9900021")

    rows = {r["task"]: r for r in emit.check_emitted(ship, work)}
    assert set(rows) == {"task_9900020", "task_9900021"}
    assert all(r["current"] for r in rows.values()), rows

    plan = stale / "plan.json"
    plan.write_text(plan.read_text() + "\n")
    rows = {r["task"]: r for r in emit.check_emitted(ship, work)}
    assert rows["task_9900020"]["current"] is True
    assert rows["task_9900021"]["current"] is False
    assert rows["task_9900021"]["deck"] == "deck0002"
    assert rows["task_9900021"]["problems"]
    assert emit.main(["--check", str(ship), "--work", str(work)]) == 1


def test_two_bundle_files_with_one_name_are_refused_not_silently_merged(tmp_path):
    """`setup` uploads one flat folder, so the bundle tree is flattened onto
    basenames. Two files with the same basename in different subdirectories
    would overwrite each other and the package would ship one file where the
    instruction names two. No bundle in the last batch had a subdirectory;
    the `keyframes` producer writes into `build-pNN/`, which is where this
    fires first."""
    deck, _ = _a_packaged_deck(tmp_path)
    copy_root = tmp_path / "colliding" / deck.id
    shutil.copytree(deck.root, copy_root)
    assets = copy_root / "bundle" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for sub in ("build-p01", "build-p02"):
        (assets / sub).mkdir(exist_ok=True)
        (assets / sub / "frame.png").write_bytes(b"not the same bytes " + sub.encode())

    with pytest.raises(emit.EmitError, match="two files called 'frame.png'"):
        emit.emit(pl.Deck(copy_root), tmp_path / "ship4", "9900012")


def test_material_names_the_manifest_never_claimed_are_written_down(tmp_path):
    """`bundle()` copies the assets directory rather than the manifest's
    `produced` list, so a leftover from an earlier `materialise` run ships
    under its old name — deck0006 carries eight blot strips where four exist,
    five of them byte-identical duplicates, under an instruction that says
    "the strip images are in the assets folder".

    Fixing that belongs in `bundle()`: it is the same directory the
    solvability probe was shown, and a fix here would leave the probe judging
    a delivery that is not the one shipped. What belongs here is the fact,
    recorded where a reader of the package finds it instead of having to diff
    two directories."""
    deck, _ = _a_packaged_deck(tmp_path)
    copy_root = tmp_path / "leftovers" / deck.id
    shutil.copytree(deck.root, copy_root)
    assets = copy_root / "bundle" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "p03--4-superseded.png").write_bytes(b"a duplicate under an old name")

    out = emit.emit(pl.Deck(copy_root), tmp_path / "ship5", "9900013")
    rec = emit.read_provenance(Path(out["assets"]))
    assert "p03--4-superseded.png" in rec["materials"]
    assert rec["materials_not_in_manifest"] == ["p03--4-superseded.png"], (
        "a file the deck's own proposal never decided to produce shipped "
        "with nothing recording that it did")
    assert "p03--4-superseded.png" in (Path(out["assets"]) / "README.md").read_text()


# --------------------------------------------------------------------------- #
# what the stray scan measures "newer" against
# --------------------------------------------------------------------------- #
#
# The recovery ran `find ... -newer '<the pinned deck>'`, and it is only ever
# called when that deck is *missing*. `find -newer` needs its reference to
# exist: with it gone `find` errors out, prints nothing, the `2>/dev/null`
# swallows it, and the recovery returns empty. So the commonest shape of the
# mistake this path exists to forgive — save somewhere else, then remove or
# rename the original — got 0.0 with the machinery to rescue it sitting right
# there. Save-As proper, which leaves the original in place, worked, which is
# why it read as tested.
#
# The reference is now resolved on the machine: the marker `setup` writes when
# it finishes, else the materials folder, else the pinned deck. What it must
# not become is "no reference at all" — every .pptx that shipped with the
# image would be a candidate, and one of those is indistinguishable from a
# lone Save-As.


def test_setup_leaves_a_marker_that_dates_the_end_of_setup(tmp_path):
    """The reference point the scan needs, written where losing the deck
    cannot take it with them, and after the delivery so the scan's window
    starts where the agent's work does."""
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    controller = FakeController()
    mod.TASK_CLASS().setup(controller, use_proxy=False)

    kinds = [name for name, _ in controller.calls]
    commands = controller.of("execute")
    stamped = [i for i, c in enumerate(commands) if mod.SETUP_STAMP in c]
    assert stamped, f"setup never writes {mod.SETUP_STAMP}"
    # the position of the stamp among *all* the calls, not among the executes:
    # "after the delivery" is a claim about the transcript, and comparing two
    # indices into two different lists never was one
    marked = next(i for i, (name, payload) in enumerate(controller.calls)
                  if name == "execute" and mod.SETUP_STAMP in payload)
    assert kinds.index("download") < marked, (
        "the marker must be written after the delivery, not before")
    assert mod.SETUP_STAMP.startswith("/home/user/."), (
        "the marker is not the agent's business and must not be on the Desktop")
    assert mod.SETUP_STAMP not in mod.TASK_CLASS().instruction


def test_the_scan_does_not_use_the_missing_deck_as_its_own_reference(tmp_path):
    """The defect, read off the command itself: the pinned deck may be *a*
    reference but it can never be the only one, because the scan runs
    precisely when it is gone."""
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    assert mod._SCAN_REFERENCES[0] == mod.SETUP_STAMP
    assert mod.DECK_VM_PATH in mod._SCAN_REFERENCES
    assert mod._SCAN_REFERENCES.index(mod.DECK_VM_PATH) == 2
    # and the script picks the first that exists rather than trusting any one
    assert "-newer \"$REF\"" in mod._SCAN_SCRIPT
    assert f"-newer '{mod.DECK_VM_PATH}'" not in mod._SCAN_SCRIPT


def test_a_deck_moved_away_rather_than_copied_is_still_recovered(tmp_path):
    """The case that was dead. The agent saved under another name and the
    original is gone — so `disk_sha` is MISSING, `get_vm_file` returns nothing
    at the pinned path, and everything now rests on the scan having a
    reference that is not the deck."""
    deck, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    moved = "/home/user/Desktop/restored deck.pptx"
    env = FakeEnv("", {moved: str(deck.source)},
                  keystroke="KEYSTROKE_NOT_SENT",
                  strays={moved: "f" * 64},
                  reference=mod.SETUP_STAMP).install(mod)

    result = mod.TASK_CLASS().evaluate(env)
    assert result["evidence"]["stray_reference"] == mod.SETUP_STAMP
    assert result["evidence"]["scored_file"] == moved
    assert result["score"] == pytest.approx(1.0), (
        "an agent who saved elsewhere and removed the original got nothing")


def test_the_scan_runs_the_shell_finds_it_when_the_deck_is_gone(tmp_path):
    """The same case again, but against a real `find` on a real tree rather
    than a fake that answers whatever it is told. The generated script is
    executed verbatim with the paths rebound to a scratch directory: this is
    the assertion that would have failed before the fix and passes after."""
    mod = _load(_a_packaged_deck(tmp_path)[1])
    home = tmp_path / "vm" / "home" / "user"
    desktop = home / "Desktop"
    materials = desktop / "task_materials"
    (materials).mkdir(parents=True)
    deck, stamp = desktop / "deck.pptx", home / ".setup"

    deck.write_bytes(b"the deck setup uploaded")
    (materials / "reference.png").write_bytes(b"a supplied material")
    time.sleep(1.1)
    stamp.write_bytes(b"")                       # setup finishes
    time.sleep(1.1)
    saved = desktop / "my version.pptx"          # the agent's Save-As
    saved.write_bytes(b"the agent's work")
    deck.unlink()                                # ...and it removed the original

    def run(references):
        script = mod._SCAN_SCRIPT
        for was, now in ((mod.SETUP_STAMP, str(stamp)),
                         (mod.MATERIALS_VM_DIR, str(materials)),
                         (mod.DECK_VM_PATH, str(deck)),
                         ("'/home/user/Desktop'", f"'{desktop}'"),
                         (" /home/user ", f" {home} ")):
            script = script.replace(was, now)
        keep = {"stamp": str(stamp), "materials": str(materials),
                "deck": str(deck)}
        script = script.replace(
            "for c in '%s' '%s' '%s'" % (keep["stamp"], keep["materials"],
                                         keep["deck"]),
            "for c in " + " ".join(f"'{keep[r]}'" for r in references)
            if references else "for c in ''")
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True).stdout

    old = subprocess.run(
        ["bash", "-c", f"find '{desktop}' '{home}' -maxdepth 2 -name '*.pptx' "
                       f"-type f -newer '{deck}' -not -path '{deck}' "
                       f"-print0 2>/dev/null | xargs -0 -r sha256sum"],
        capture_output=True, text=True).stdout
    assert old.strip() == "", (
        "the old reference is supposed to be dead here; if this finds "
        "something the premise of the fix is wrong")

    out = run(["stamp", "materials", "deck"])
    assert f"REFERENCE {stamp}" in out
    assert str(saved) in out, out
    assert str(materials) not in out, "a supplied material is inside the window"

    # deck only, the old behaviour, kept as the negative control
    assert run(["deck"]).strip() == f"REFERENCE none"


def test_with_nothing_to_date_setup_by_no_file_is_scored(tmp_path):
    """Dropping the `-newer` clause instead would admit every deck that came
    with the image, and one of those is indistinguishable from a lone
    Save-As. "There was no reference point" is the honest answer, and it is
    one line in the evidence."""
    deck, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    env = FakeEnv("", {"/home/user/Desktop/sample.pptx": str(deck.source)},
                  keystroke="KEYSTROKE_NOT_SENT",
                  strays={"/home/user/Desktop/sample.pptx": "f" * 64},
                  reference="none").install(mod)

    result = mod.TASK_CLASS().evaluate(env)
    assert env.fetched == [mod.DECK_VM_PATH], "a file was scored anyway"
    assert result["score"] == 0.0
    assert result["evidence"]["stray_reference"] == "none"
    assert any("date the end of setup" in note
               for note in result["evidence"]["stray_rejected"])


def test_one_file_found_down_two_roots_is_one_candidate(tmp_path):
    """`find /home/user/Desktop /home/user -maxdepth 2` reports every Desktop
    deck twice — Desktop is itself at depth 1 under /home/user. Undeduplicated
    that reads as "2 files could each be the result and nothing distinguishes
    them", so a lone Save-As on the Desktop, the commonest case of all, was
    never scored. Invisible to a fake serving strays out of a dict, which is
    why every branch could be driven and this still shipped."""
    deck, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    saved = "/home/user/Desktop/my version.pptx"
    env = FakeEnv("", {saved: str(deck.source)},
                  keystroke="KEYSTROKE_NOT_SENT",
                  reference=mod.SETUP_STAMP,
                  stray_lines=[f"{'f' * 64}  {saved}",
                               f"{'f' * 64}  {saved}"]).install(mod)

    result = mod.TASK_CLASS().evaluate(env)
    assert result["evidence"]["scored_file"] == saved
    assert result["score"] == pytest.approx(1.0)
    assert "stray_rejected" not in result["evidence"], (
        result["evidence"].get("stray_rejected"))


def test_the_discrimination_that_was_there_before_still_holds(tmp_path):
    """Fixing the reference point must not widen what counts as an answer:
    the deck we uploaded and the files we supplied are still not answers, and
    two live candidates are still not free retries."""
    deck, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    init = str(Path(out["assets"]) / "assets" / "init.pptx")
    mod.MATERIAL_SHA256 = frozenset({"d" * 64})
    material = f"{mod.MATERIALS_VM_DIR}/supplied.pptx"
    copied = "/home/user/Desktop/untouched copy.pptx"

    env = FakeEnv("", {material: init, copied: init},
                  keystroke="KEYSTROKE_NOT_SENT",
                  strays={material: "d" * 64, copied: mod.INIT_SHA256},
                  reference=mod.SETUP_STAMP).install(mod)
    result = mod.TASK_CLASS().evaluate(env)
    assert env.fetched == [mod.DECK_VM_PATH]
    assert result["score"] == 0.0
    notes = " ".join(result["evidence"]["stray_rejected"])
    assert "materials folder" in notes and "setup uploaded" in notes

    first, second = "/home/user/Desktop/a.pptx", "/home/user/Desktop/b.pptx"
    env = FakeEnv("", {first: str(deck.source), second: init},
                  keystroke="KEYSTROKE_NOT_SENT",
                  strays={first: "a" * 64, second: "b" * 64},
                  reference=mod.SETUP_STAMP).install(mod)
    result = mod.TASK_CLASS().evaluate(env)
    assert env.fetched == [mod.DECK_VM_PATH]
    assert result["score"] == 0.0
    assert "2 files" in " ".join(result["evidence"]["stray_rejected"])
