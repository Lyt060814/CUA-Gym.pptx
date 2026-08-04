"""What the packaged task must be true of before anyone runs it.

The three that matter are: it works without the library it was built from,
it cannot hand the agent its own answer key, and a run the machinery could
not judge does not come back looking like an agent that failed.

    python3 -m pytest tests/test_emit.py -q
"""

import json
import shutil
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pptxgym import emit, pipeline as pl                       # noqa: E402

WORK = ROOT / "work"


def _stub_harness():
    """The two `desktop_env` names a generated task imports at module level."""
    for name, attrs in (
        ("desktop_env", {}),
        ("desktop_env.task_base", {"BaseTask": object}),
        ("desktop_env.evaluators", {}),
        ("desktop_env.evaluators.getters",
         {"get_vm_file": lambda *a, **k: None,
          "get_vm_command_line": lambda *a, **k: ""}),
    ):
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod


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
# a zero has to mean something
# --------------------------------------------------------------------------- #


def test_unjudgeable_is_not_a_zero(tmp_path):
    """A score of 0.0 says the agent did not do the work. When the machinery
    could not find out, saying the same thing teaches a model that correct
    behaviour earns nothing — three of four tasks in one rollout recorded 0.0
    for work that was measurably done."""
    _, out = _a_packaged_deck(tmp_path)
    task = _load(out).TASK_CLASS()
    r = task._unscoreable("the deck was never written to disk", {})
    assert r["outcome"] == "unscoreable"
    assert r["unscoreable_reason"]
    assert set(r["partial_scores"]) == set(task.WEIGHTS)
    assert all("unscoreable" in p["description"] for p in r["partial_scores"].values())


def test_a_scored_run_says_so(tmp_path):
    _, out = _a_packaged_deck(tmp_path)
    mod = _load(out)
    task = mod.TASK_CLASS()
    plan = json.loads((mod.TEST_ASSETS / "plan.json").read_text())
    gt = json.loads((mod.TEST_ASSETS / "gt_inventory.json").read_text())
    init = json.loads((mod.TEST_ASSETS / "init_inventory.json").read_text())
    r = task._render(mod.score(plan, gt, gt, init), {"scored_file": "x"})
    assert r["outcome"] == "scored" and r["score"] == pytest.approx(1.0)
    assert r["evidence"]["scored_file"] == "x"


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
