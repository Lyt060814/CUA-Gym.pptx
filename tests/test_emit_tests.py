"""What the per-task test generator has to produce before anyone trusts it.

The suite it writes is the only thing standing between a packaged task and a
rollout, and the part that matters is the part no previous suite had: what the
reward pays for a deck that is *partly* repaired.  Three of the four tasks a
model attempted on the last rollout scored 0.0 having done 43%, 53% and 63% of
the work, and every one of those evaluators was correct at both endpoints.

So the assertions here are about the middle of the range, not the ends:

* the generated suite runs, and every failure in it is either absent or a
  named finding about the reward — never an unexplained red;
* the score rises with the work done, measured on a real packaged task;
* the report carries the section the archived skill calls its most valuable
  output, populated rather than stubbed.

An emitted package is deliberately built fresh into `tmp_path` on every run.
`emit.py` is under active edit, and a package left in /tmp is a snapshot of
whatever it looked like an hour ago — which is how a stale artefact once got
reported as a live defect.

    python3 -m pytest tests/test_emit_tests.py -q
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pptxgym import emit, emit_tests, pipeline as pl              # noqa: E402

WORK = ROOT / "work"
TASK_ID = "9900042"


def _smallest_accepted_deck():
    """The cheapest deck with an accepted plan.

    Cheapest by component count: the generator scores a dozen constructed
    states per task, and a deck with a hundred components turns a unit test
    into a coffee break without testing anything the small one does not.
    """
    best = None
    if not WORK.exists():
        return None
    for deck in pl.decks_in(WORK):
        plan_file = deck.root / "plan.json"
        if not plan_file.exists():
            continue
        plan = json.loads(plan_file.read_text())
        if plan.get("rejected") or not plan.get("components"):
            continue
        n = len(plan["components"])
        if best is None or n < best[0]:
            best = (n, deck)
    return best[1] if best else None


@pytest.fixture(scope="module")
def packaged(tmp_path_factory):
    """One packaged task with its generated suite, built once for this file."""
    deck = _smallest_accepted_deck()
    if deck is None:
        pytest.skip("no deck with an accepted plan in this checkout")
    out = tmp_path_factory.mktemp("packaged")
    try:
        emitted = emit.emit(deck, out, TASK_ID)
    except emit.EmitError as error:
        pytest.skip(f"{deck.id} could not be packaged: {error}")
    generated = emit_tests.for_emitted(emitted)
    return emitted, generated


# --------------------------------------------------------------------------- #
# the shape the archived skill prescribes
# --------------------------------------------------------------------------- #


def test_the_generator_writes_the_files_the_skill_asks_for(packaged):
    emitted, generated = packaged
    tests = Path(emitted["assets"]) / "tests"
    assert (tests / "test_task.py").exists()
    assert (tests / f"task-test-report-{TASK_ID}.md").exists()
    assert generated["tests"] == str(tests / "test_task.py")

    ignore = (tests / ".gitignore").read_text()
    for name in ("task-test-results.json", "task-test-results.md",
                 "test-work/"):
        assert name in ignore, f"{name} is a runner artefact and must not ship"
    assert "task-test-report" not in ignore, "the report is the committed one"


def test_the_generated_suite_is_importable_python(packaged):
    emitted, generated = packaged
    import ast

    source = Path(generated["tests"]).read_text()
    tree = ast.parse(source)
    names = [n.name for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    assert len(names) >= 20, names
    assert len(set(names)) == len(names), "two tests share a name"
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            assert not node.args.args, (
                f"{node.name} takes arguments; the runner passes kwargs by "
                f"name and pytest would read them as fixtures")


def test_generating_the_suite_does_not_leak_the_answer_key(packaged):
    """The suite reads the plan and both inventories — it has to, that is what
    the evaluator compares against — so the invariant is the other way round:
    nothing it reads may land where `setup` uploads from."""
    emitted, _generated = packaged
    adir = Path(emitted["assets"])
    assert emit.check_package(Path(emitted["py"]), adir) == []
    secret = {"plan.json", "gt_inventory.json", "init_inventory.json"}
    for f in (adir / "assets").rglob("*"):
        assert f.name not in secret, f"{f} is answer-key material"


# --------------------------------------------------------------------------- #
# the suite itself
# --------------------------------------------------------------------------- #


def test_every_failure_is_a_named_finding_and_not_a_surprise(packaged):
    """A red suite is only useful if it says which kind of red it is.

    A failing category-5 assertion is a statement about the reward and stays
    exactly as written; anything else is a defect and must not be filed
    alongside it.
    """
    _emitted, generated = packaged
    unexpected = generated.get("unexpected") or []
    assert not unexpected, "\n".join(
        f"{row['test']}: {row['error']}" for row in unexpected)
    assert generated["passed"] >= 20
    for name in generated["findings"]:
        assert name in emit_tests.KNOWN_FINDINGS
        assert emit_tests.KNOWN_FINDINGS[name].strip()


def test_the_five_categories_are_all_present(packaged):
    _emitted, generated = packaged
    ran = {row["test"] for row in generated["results"]}
    for name in (
        "test_the_weights_sum_to_one_and_match_the_descriptions",   # 1
        "test_a_missing_deck_scores_zero_with_a_reason",            # 2
        "test_the_ground_truth_scores_one_with_every_partial_one",  # 3
        "test_a_name_is_not_an_identity",                           # 4
        "test_half_the_damage_repaired_lands_strictly_between_the_endpoints",
        "test_one_degradation_fixed_and_another_missed_pays_for_the_one",
        "test_a_deck_saved_under_another_name_is_scored_not_zeroed",
        "test_a_re_encoded_image_the_task_asked_to_restore_costs_nothing",
        "test_a_save_is_forced_only_when_nothing_has_been_written_out",
        "test_a_deck_already_written_to_disk_is_never_saved_over",
    ):
        assert name in ran, name


def test_the_reconstruction_used_for_partial_states_is_faithful(packaged):
    """Every component repaired has to be worth exactly 1.0, or the numbers
    in between mean nothing at all."""
    _emitted, generated = packaged
    row = next(r for r in generated["results"]
               if r["test"] == "test_the_reconstruction_is_faithful")
    assert row["ok"], row["error"]
    states = generated["calibration"]["states"]
    assert states["every component repaired"]["score"] == pytest.approx(1.0)
    assert states["ground truth"]["score"] == pytest.approx(1.0)


def test_the_score_rises_with_the_work_done(packaged):
    """The measurement the last rollout needed and nothing produced."""
    _emitted, generated = packaged
    states = generated["calibration"]["states"]
    ladder = [row["score"] for label, row in states.items() if "% of" in label]
    assert len(ladder) == 3, states
    nothing = states["untouched input (nothing done)"]["score"]
    assert nothing == 0.0
    assert nothing < ladder[0] < ladder[1] < ladder[2] < 1.0, ladder


def test_over_eagerness_costs_a_fraction_and_a_gate_fires_on_nothing_correct(
        packaged):
    _emitted, generated = packaged
    states = generated["calibration"]["states"]
    for label, row in states.items():
        if label.startswith(("ground truth", "every component", "25%", "50%",
                             "75%")):
            assert row["gate"] is None, (
                f"a cheat gate fired on `{label}`, which is correct work: "
                f"{row['gate']}")
    eager = next(row for label, row in states.items() if "over_eager" in label)
    assert 0.0 < eager["score"] < 1.0, eager


def test_one_degradation_repaired_is_worth_its_declared_weight(packaged):
    _emitted, generated = packaged
    per_deg = generated["calibration"]["per_degradation"]
    assert per_deg, "no degradation could be scored on its own"
    for deg, row in per_deg.items():
        assert row["score"] > 0.0, f"{deg} repaired alone pays nothing"
        assert row["score"] < 1.0, f"{deg} alone is worth the whole task"
        assert abs(row["score"] - row["declared"]) < 1e-3, (deg, row)


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #


def test_the_report_says_what_the_reward_pays_between_the_endpoints(packaged):
    emitted, generated = packaged
    text = Path(generated["report"]).read_text()
    assert f"# task_{TASK_ID}" in text
    assert "**verdict**" in text
    assert emitted["py"] in text
    for heading in ("## What the reward pays for a partly finished deck",
                    "## Tests",
                    "## Issues noticed while writing the tests",
                    "## What these tests cannot reach"):
        assert heading in text, heading


def test_the_issues_section_is_populated_rather_than_stubbed(packaged):
    """The archived skill calls this the most valuable output of a test pass,
    and it is right: everything above it is a number somebody expected."""
    _emitted, generated = packaged
    text = Path(generated["report"]).read_text()
    section = text.split("## Issues noticed while writing the tests", 1)[1]
    section = section.split("## What these tests cannot reach", 1)[0]
    items = [line for line in section.splitlines() if line.startswith("- ")]
    assert len(items) >= 3, section
    assert all(len(line) > 80 for line in items), "an issue nobody can act on"


def test_a_known_finding_is_reproduced_verbatim_in_the_report(packaged):
    _emitted, generated = packaged
    if not generated["findings"]:
        return
    text = Path(generated["report"]).read_text()
    assert "## The failing assertions, and why they stay" in text
    for name in generated["findings"]:
        assert name in text
        assert emit_tests.KNOWN_FINDINGS[name].split(".")[0] in text


# --------------------------------------------------------------------------- #
# refusals
# --------------------------------------------------------------------------- #


def test_a_package_with_no_plan_beside_it_is_refused(tmp_path):
    (tmp_path / "task_class").mkdir()
    py = tmp_path / "task_class" / "task_0.py"
    py.write_text("TASK_CLASS = None\n")
    adir = tmp_path / "task_assets" / "task_0"
    (adir / "tests" / "assets").mkdir(parents=True)
    with pytest.raises(emit_tests.EmitTestsError):
        emit_tests.emit_tests(py, adir, "0")


def test_a_missing_task_file_is_refused(tmp_path):
    with pytest.raises(emit_tests.EmitTestsError):
        emit_tests.emit_tests(tmp_path / "nope.py", tmp_path, "0")
