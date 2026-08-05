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


def _frozen_work():
    import minidecks
    return minidecks.frozen_work()


def _the_frozen_deck():
    """The deck the generated suite is exercised on: `mini_picture`.

    It used to be *the cheapest deck in `work/` with an accepted plan*, which
    made every assertion below a fact about the corpus rather than about the
    generator: a repair that rejects a deck, or one that trims another's
    components, silently changes which task these twenty tests are generated
    from.  Two of them then failed for reasons nobody had touched.

    `mini_picture` is built to carry exactly what the generated suite needs
    and is documented in `tests/fixtures/minidecks.py`: four components, so
    the 25/50/75% calibration ladder has three distinct rungs, and one of them
    restoring a supplied picture, so the picture finding has something to be
    about.
    """
    import minidecks
    work = minidecks.frozen_work()
    return pl.Deck(work / minidecks.DECK_IDS["mini_picture"])


def _smallest_accepted_deck(work):
    """The cheapest deck of a live tree with an accepted plan.

    Cheapest by component count: the generator scores a dozen constructed
    states per task, and a deck with a hundred components turns a unit test
    into a coffee break without testing anything the small one does not.
    Corpus-only now — see `_the_frozen_deck`.
    """
    best = None
    if not work.exists():
        return None
    for deck in pl.decks_in(work):
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


def _package(deck, out):
    emitted = emit.emit(deck, out, TASK_ID)
    return emitted, emit_tests.for_emitted(emitted)


@pytest.fixture(scope="module")
def packaged(tmp_path_factory):
    """One packaged task with its generated suite, built once for this file."""
    return _package(_the_frozen_deck(), tmp_path_factory.mktemp("packaged"))


@pytest.fixture(scope="module")
def packaged_corpus(tmp_path_factory):
    """The same, from the live corpus.  Only `corpus`-marked tests may use it."""
    deck = _smallest_accepted_deck(WORK)
    if deck is None:
        pytest.skip("no deck with an accepted plan in this checkout")
    try:
        return _package(deck, tmp_path_factory.mktemp("packaged_corpus"))
    except emit.EmitError as error:
        pytest.skip(f"{deck.id} could not be packaged: {error}")


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


def _a_plan(**over):
    """The smallest plan `report` and `_issues` will accept, for the checks
    that are about the report rather than about a deck."""
    plan = {"deck": "deck9999", "weight_source": "est_steps",
            "damage": {"slides": [0]}, "degradations": [], "unscoreable": [],
            "init_slide_of": None,
            "components": [{"id": "c1", "op": "restore_shape", "weight": 1.0,
                            "slide": 0, "floor": 0.0, "gt_path": "0"}]}
    plan.update(over)
    return plan


def _a_calibration():
    return {"states": {"untouched input (nothing done)":
                       {"score": 0.0, "gate": None, "penalty": 0.0}},
            "per_degradation": {}}


def test_no_named_finding_still_describes_something_that_now_passes(packaged):
    """The entry that went stale, as a standing check.

    `KNOWN_FINDINGS` said the picture problem had two halves. One of them was
    fixed — the addresses in `_page_facts` no longer come from the blob
    digest — and the entry went on describing it, with the numbers it had
    measured before the fix, for as long as nobody read the report next to the
    code. A finding is a claim that an assertion fails; an assertion that
    passes withdraws it, and this is where that gets noticed.
    """
    _emitted, generated = packaged
    stale = generated.get("stale_findings") or []
    assert not stale, (
        f"{stale} pass on this task but are still listed in KNOWN_FINDINGS as "
        f"failing assertions — move them to FIXED_FINDINGS, where a later "
        f"failure reads as a regression instead of being excused")
    ran = {row["test"] for row in generated["results"]}
    for name in emit_tests.FIXED_FINDINGS:
        assert name in ran, f"{name} is recorded as fixed but no longer runs"
        assert name not in emit_tests.KNOWN_FINDINGS, (
            f"{name} is in both tables, so a regression in it would be "
            f"excused as a known finding")


def test_a_caveat_fires_only_on_the_deck_it_is_true_of():
    """`init_slide_of`, as the case that showed what a stale caveat costs.

    The old bullet fired when the mapping was `None` and said the function
    returned `None` on both of its branches, so a deck that moves no page —
    which is every deck here — was told about a landmine that had been
    removed. `None` now *means* the identity and the identity is right; what
    is worth a caveat is the other case, where a real mapping is being
    replayed and a floor read against the wrong page would be silent.
    """
    still = emit_tests._issues(_a_plan(), _a_calibration(), [])
    assert not any("init_slide_of" in item for item in still), (
        "a deck that moves no page is being warned about page mapping")

    moved = emit_tests._issues(_a_plan(init_slide_of=[1, 0]),
                               _a_calibration(), [])
    assert any("init_slide_of" in item for item in moved), (
        "the deck whose floors actually go through the mapping is told nothing")


def test_no_caveat_describes_a_defect_that_was_never_there():
    """The data-part caveat claimed an unidentifiable component "scores 0
    rather than being skipped". It never could: `build_plan` scores every
    component against the ground truth as its own candidate before weighing
    anything, drops what cannot reach 1.0 into `unscoreable`, and rejects the
    plan outright if that empties a degradation — so the ambiguity is refused
    when the plan is built and never reaches a rollout. A caveat nobody can
    trust is worse than none: it teaches the reader to skim the section the
    real findings live in."""
    plan = _a_plan()
    plan["components"] = [dict(plan["components"][0], gt_path=None,
                               op="smartart_drop_nodes")]
    text = " ".join(emit_tests._issues(plan, _a_calibration(), []))
    assert "name no shape path" in text
    assert "scores 0 rather than being skipped" not in text
    assert "unidentifiable" not in text.replace(
        "does not make the component unidentifiable", "")
    assert "build_plan" in text and "unscoreable" in text


def test_a_finding_that_passes_is_filtered_and_the_report_says_so():
    """Two properties, and the second is the one that does the work.

    A passing assertion must not be counted as a failure — that part is easy
    and was already true. The part that was missing is that nothing said it:
    the report listed the test as a plain pass among thirty others, so an
    entry describing a defect that no longer exists could sit in the table for
    as long as anyone cared to leave it.
    """
    plan, cal = _a_plan(), _a_calibration()
    name = next(iter(emit_tests.KNOWN_FINDINGS))
    results = [{"test": name, "ok": True, "error": ""},
               {"test": "test_something_else", "ok": True, "error": ""}]

    text = emit_tests.report("9900042", Path("task.py"), Path("."), plan, cal,
                             results)
    assert "**verdict** **pass**" in text
    assert "2/2 passing" in text
    assert f"| `{name}` | pass |" in text
    assert "fail (finding)" not in text
    assert "## The failing assertions, and why they stay" not in text
    assert "## Findings that pass here" in text
    section = text.split("## Findings that pass here", 1)[1]
    assert name in section and "stale" in section


def test_a_finding_that_fails_is_still_a_finding_and_not_a_defect():
    """The other side of the same switch, so the test above cannot be passed
    by a report that has simply stopped telling findings from defects."""
    plan, cal = _a_plan(), _a_calibration()
    name = next(iter(emit_tests.KNOWN_FINDINGS))
    results = [{"test": name, "ok": False, "error": "AssertionError: 0.5614"}]

    text = emit_tests.report("9900042", Path("task.py"), Path("."), plan, cal,
                             results)
    assert "a finding about the reward rather than a bug in the test" in text
    assert "## The failing assertions, and why they stay" in text
    assert "0.5614" in text
    assert "## Findings that pass here" not in text


def test_a_regression_in_something_recorded_as_fixed_is_a_defect():
    """`FIXED_FINDINGS` is a record, not an excuse. A test named there is
    absent from `KNOWN_FINDINGS` precisely so that its failure is reported as
    what it would be — a defect — rather than filed beside the assertions that
    are expected to fail."""
    plan, cal = _a_plan(), _a_calibration()
    name = next(iter(emit_tests.FIXED_FINDINGS))
    results = [{"test": name, "ok": False, "error": "AssertionError: 0.393"}]

    text = emit_tests.report("9900042", Path("task.py"), Path("."), plan, cal,
                             results)
    assert "these are defects, not findings" in text
    assert f"**{name} failed unexpectedly:**" in text


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


# --------------------------------------------------------------------------- #
# and the same question of the corpus
# --------------------------------------------------------------------------- #


@pytest.mark.corpus
def test_a_live_deck_still_generates_a_suite_with_no_unexpected_failures(
        packaged_corpus):
    """Everything above is generated from `mini_picture`, which is built to
    exercise the generator rather than to be a real deck.  This asks the same
    of whatever `work/` currently holds: the generated suite still runs, and
    every failure in it is still a named finding rather than a surprise.

    It is the arm that would notice a generator which quietly depends on
    something only a synthetic deck has.
    """
    _emitted, generated = packaged_corpus
    assert not generated.get("unexpected"), "\n".join(
        f"{row['test']}: {row['error']}"
        for row in generated["unexpected"])
    assert not generated.get("stale_findings")
