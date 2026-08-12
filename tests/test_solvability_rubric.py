"""The solvability rubric, held to arithmetic rather than to good intentions.

`ppt-task-solvability` was rewritten into five ordered passes and a
first-match-wins verdict table after the probe gave four different verdicts on
one unchanged bundle.  Nothing enforced any of it: the gate validated the enum
and the presence of `rework` and never once compared the verdict to the
findings sitting next to it in the same file.  An archived run shipped
`leaks: 2` with a verdict of `undetermined` — against the skill's own rule —
and passed.

Everything here is about the line the checker is allowed to stand on.  What a
set of findings implies is arithmetic and is tested.  Whether a leak is
load-bearing is the probe's judgement, made with the package open, and the
tests below assert that the checker does *not* try to re-make it.

    python3 -m pytest tests/test_solvability_rubric.py -q
"""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym.orchestration.agent import (                                        # noqa: E402
    solvability_rubric_problems, verdict_from_findings)

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".claude" / "skills" / "ppt-task-solvability" / "SKILL.md"


def deg(**over) -> dict:
    """A degradation that breaks no rule, for a test to break one of."""
    d = {"id": "d1", "slides": [4],
         "end_state": "slide 4 shows the world production chart again",
         "checks": {"E1": "", "E2": "assets/p04.emf is the image itself",
                    "E3": "", "E4": "", "E5": "", "E6": ""},
         "evidence": "assets/p04.emf is the original image",
         "determinate": True, "rivals": [], "undetermined": "",
         "tolerance": [], "est_steps_measured": 60,
         "overdetermined": False}
    d.update(over)
    return d


def report(**over) -> dict:
    r = {"verdict": "solvable", "verdict_reason": "table line 5",
         "degradations": [deg()], "leaks": [], "residue": [],
         "est_steps_measured": 60, "est_steps_declared": 60, "rework": []}
    r.update(over)
    return r


def only(problems, *fragments):
    """Assert exactly one problem, and that it says what it is about."""
    assert len(problems) == 1, problems
    for f in fragments:
        assert f in problems[0], problems[0]


def test_a_clean_report_is_clean():
    assert solvability_rubric_problems(report()) == []


def test_a_non_object_degradation_is_refused_without_crashing():
    only(solvability_rubric_problems(report(degradations=["d1"])),
         "degradation #1 is not an object")


# --------------------------------------------------------------------------- #
# Pass 5: the verdict table, which is the whole point
# --------------------------------------------------------------------------- #

LEAK = {"what": "the deleted statute names are in the diagram data",
        "where": "ppt/diagrams/data5.xml",
        "load_bearing": "d3 has no E1-E6 hit for the wording; this supplies it"}


def test_the_shipped_defect_is_now_refused():
    """`leaks: 2` with a verdict of `undetermined` reached `packaged` once."""
    r = report(verdict="undetermined", leaks=[LEAK, dict(LEAK, where="x.xml")],
               degradations=[deg(determinate=False, undetermined="not pinned")],
               rework=[{"stage": "recipe", "what": "close the leak"}])
    problems = solvability_rubric_problems(r)
    assert any("line 1" in p and "'leaked'" in p for p in problems), problems


@pytest.mark.parametrize("line,over,want", [
    (1, dict(leaks=[LEAK]), "leaked"),
    (2, dict(degradations=[deg(determinate=False, undetermined="which half")]),
     "undetermined"),
    (3, dict(degradations=[deg(determinate=False, rivals=["a", "b"])]),
     "ambiguous"),
    (4, dict(degradations=[deg(overdetermined=True)]), "overdetermined"),
    (5, {}, "solvable"),
])
def test_every_line_of_the_table(line, over, want):
    got, got_line, _why = verdict_from_findings(report(**over))
    assert (got, got_line) == (want, line)


def test_precedence_is_the_walk_order_not_a_severity_ranking():
    """A report that matches all four lines is decided by the first of them.

    Each of these pairs is a real disagreement between archived runs, and the
    table's answer is the earlier line every time — `leaked` first because
    closing the leak can turn a determinate degradation indeterminate, so the
    later questions cannot be asked until it is gone.
    """
    everything = report(
        leaks=[LEAK],
        degradations=[deg(determinate=False, undetermined="which half"),
                      deg(id="d2", determinate=False, rivals=["a", "b"]),
                      deg(id="d3", overdetermined=True)])
    assert verdict_from_findings(everything)[:2] == ("leaked", 1)

    no_leak = dict(everything, leaks=[])
    assert verdict_from_findings(no_leak)[:2] == ("undetermined", 2)

    no_gap = dict(no_leak, degradations=everything["degradations"][1:])
    assert verdict_from_findings(no_gap)[:2] == ("ambiguous", 3)

    only_over = dict(no_gap, degradations=everything["degradations"][2:])
    assert verdict_from_findings(only_over)[:2] == ("overdetermined", 4)


@pytest.mark.parametrize("wrote", ["solvable", "leaked", "ambiguous",
                                   "overdetermined"])
def test_a_verdict_the_findings_do_not_support_is_refused(wrote):
    r = report(verdict=wrote,
               degradations=[deg(determinate=False, undetermined="which half")],
               rework=[{"stage": "proposed", "what": "name the anchor"}])
    problems = solvability_rubric_problems(r)
    assert any("Pass 5 table" in p for p in problems), problems


def test_the_verdict_the_table_gives_is_accepted():
    r = report(verdict="undetermined",
               degradations=[deg(determinate=False, undetermined="which half")],
               rework=[{"stage": "proposed", "what": "name the anchor"}])
    assert solvability_rubric_problems(r) == []


def test_an_unknown_verdict_stops_everything_else():
    only(solvability_rubric_problems(report(verdict="probably fine")),
         "unknown verdict")


# --------------------------------------------------------------------------- #
# Pass 2: determinacy, and the field that used to absorb every caveat
# --------------------------------------------------------------------------- #

def test_determinate_with_a_caveat():
    """25 of 30 archived degradations did exactly this."""
    only(solvability_rubric_problems(
        report(degradations=[deg(undetermined="only the object type is loose")])),
        "`determinate: true`", "non-empty `undetermined`")


def test_determinate_with_rivals():
    r = report(degradations=[deg(rivals=["blue then red", "red then blue"])])
    problems = solvability_rubric_problems(r)
    assert any("names rivals" in p for p in problems), problems


def test_indeterminate_with_neither_rivals_nor_a_reason():
    r = report(verdict="undetermined", degradations=[deg(determinate=False)],
               rework=[{"stage": "proposed", "what": "name the anchor"}])
    only(solvability_rubric_problems(r), "`determinate: false`",
         "which part is not pinned")


def test_determinate_with_no_evidence_and_no_citation():
    r = report(degradations=[deg(evidence="", checks=dict.fromkeys(
        ("E1", "E2", "E3", "E4", "E5", "E6"), ""))])
    problems = solvability_rubric_problems(r)
    assert len(problems) == 2, problems
    assert any("guess wearing a verdict" in p for p in problems), problems
    assert any("every E1-E6 empty" in p for p in problems), problems


def test_an_indeterminate_degradation_owes_no_citation():
    """Only `determinate: true` has to be paid for.  Reporting that nothing
    pins the end state is the most valuable output this step has."""
    r = report(verdict="undetermined",
               degradations=[deg(determinate=False, evidence="",
                                 undetermined="no reference render, no twin",
                                 checks=dict.fromkeys(
                                     ("E1", "E2", "E3", "E4", "E5", "E6"), ""))],
               rework=[{"stage": "materialise", "what": "ship a reference"}])
    assert solvability_rubric_problems(r) == []


# --------------------------------------------------------------------------- #
# shape: a missing key is not an empty one
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("key", ["id", "end_state", "checks", "determinate",
                                 "rivals", "undetermined", "tolerance",
                                 "est_steps_measured", "overdetermined"])
def test_every_degradation_key_is_required(key):
    d = deg()
    d.pop(key)
    problems = solvability_rubric_problems(report(degradations=[d]))
    assert any(f"`{key}`" in p for p in problems), problems


def test_a_missing_rivals_key_would_otherwise_read_as_no_rivals():
    d = deg(determinate=False, undetermined="which half")
    d.pop("rivals")
    # the table cannot see the difference — which is exactly why the key is
    # required rather than defaulted
    assert verdict_from_findings(report(degradations=[d]))[0] == "undetermined"
    assert any("`rivals`" in p
               for p in solvability_rubric_problems(report(degradations=[d])))


def test_checks_must_carry_all_six_evidence_items():
    only(solvability_rubric_problems(
        report(degradations=[deg(checks={"E2": "assets/p04.emf"})])),
        "missing E1, E3, E4, E5, E6")


def test_checks_may_not_invent_an_evidence_item():
    r = report(degradations=[deg(checks={**deg()["checks"], "E7": "vibes"})])
    only(solvability_rubric_problems(r), "E7")


def test_verdict_reason_is_required():
    only(solvability_rubric_problems(report(verdict_reason="  ")),
         "no `verdict_reason`")


def test_a_report_with_no_degradations_is_not_a_report():
    only(solvability_rubric_problems(report(degradations=[])),
         "no per-degradation findings")


# --------------------------------------------------------------------------- #
# Pass 2's tolerance, and Pass 3's leaks
# --------------------------------------------------------------------------- #

def test_a_tolerance_must_name_t1_or_t2():
    """"Residual", "non-blocking", "sub-gradeable detail": five severities,
    invented five times, and the deck's verdict swung on which got picked."""
    r = report(degradations=[deg(tolerance=[
        {"what": "frame position", "rule": "non-blocking", "why": "small"}])])
    only(solvability_rubric_problems(r), "under T1 or T2")


def test_a_tolerance_must_cite_what_makes_it_one():
    r = report(degradations=[deg(tolerance=[
        {"what": "frame position", "rule": "T2", "why": ""}])])
    only(solvability_rubric_problems(r), "cites what makes it one")


def test_a_leak_must_say_why_it_is_load_bearing():
    r = report(verdict="leaked",
               leaks=[{"what": "the statute names", "where": "ppt/x.xml"}],
               rework=[{"stage": "recipe", "what": "strip it"}])
    only(solvability_rubric_problems(r), "no `load_bearing`")


def test_whether_the_load_bearing_reason_is_true_is_not_checked():
    """The boundary.  The probe read the package; the checker did not.  It may
    insist the reason was written down and may not grade it."""
    r = report(verdict="leaked",
               leaks=[dict(LEAK, load_bearing="it just feels like one")],
               rework=[{"stage": "recipe", "what": "strip it"}])
    assert solvability_rubric_problems(r) == []


@pytest.mark.parametrize("where", ["assets/reference-p04.png",
                                   "bundle/assets/p04.emf",
                                   "delta.json", "renders/p-04.png",
                                   "digest.json"])
def test_a_leak_outside_the_bundle_is_refused(where):
    r = report(verdict="leaked", leaks=[dict(LEAK, where=where)],
               rework=[{"stage": "materialise", "what": "redo the asset"}])
    problems = solvability_rubric_problems(r)
    assert len(problems) == 1, problems
    assert "leak #1" in problems[0]


@pytest.mark.parametrize("where", [
    "ppt/diagrams/data5.xml",
    "ppt/slides/slide4.xml — the same bytes as renders/p-04.png show",
    "docProps/thumbnail.jpeg",
])
def test_a_leak_inside_the_package_is_a_leak(where):
    """Only the first token of `where` is read.  A finding that points at a
    package part and mentions a render in passing is a true finding, and
    refusing it for its prose costs a probe round for nothing."""
    r = report(verdict="leaked", leaks=[dict(LEAK, where=where)],
               rework=[{"stage": "recipe", "what": "strip it"}])
    assert solvability_rubric_problems(r) == []


def test_residue_must_say_why_it_is_not_a_leak():
    r = report(residue=[{"what": "slide 7's rels lost rId1"}])
    only(solvability_rubric_problems(r), "why_not_a_leak")


# --------------------------------------------------------------------------- #
# Pass 4: the two sums
# --------------------------------------------------------------------------- #

def test_the_total_is_the_sum_of_the_parts():
    r = report(degradations=[deg(est_steps_measured=60),
                             deg(id="d2", est_steps_measured=90)],
               est_steps_measured=200, est_steps_declared=200)
    only(solvability_rubric_problems(r), "add up to 150")


def test_a_step_count_is_owed_per_degradation():
    r = report(degradations=[deg(est_steps_measured=None)])
    problems = solvability_rubric_problems(r)
    assert any("est_steps_measured" in p for p in problems), problems


def test_outside_the_band_owes_a_proposed_rework_note():
    r = report(degradations=[deg(est_steps_measured=185)],
               est_steps_measured=185, est_steps_declared=275)
    only(solvability_rubric_problems(r), "33% out", "`proposed`")


def test_outside_the_band_with_the_note_is_fine():
    r = report(degradations=[deg(est_steps_measured=185)],
               est_steps_measured=185, est_steps_declared=275,
               rework=[{"stage": "proposed", "what": "285 was optimistic"}])
    assert solvability_rubric_problems(r) == []


def test_inside_the_band_owes_nothing():
    r = report(est_steps_measured=60, est_steps_declared=70)
    assert solvability_rubric_problems(r) == []


# --------------------------------------------------------------------------- #
# rework, which is what the pipeline actually re-runs
# --------------------------------------------------------------------------- #

def test_a_non_passing_verdict_needs_a_work_order():
    r = report(verdict="undetermined",
               degradations=[deg(determinate=False, undetermined="which half")])
    only(solvability_rubric_problems(r), "with no `rework`")


def test_rework_names_a_stage_the_pipeline_can_re_run():
    r = report(verdict="leaked", leaks=[LEAK],
               rework=[{"stage": "reconcile", "what": "look again"}])
    only(solvability_rubric_problems(r), "'reconcile'")


def test_rework_must_say_something():
    r = report(verdict="leaked", leaks=[LEAK],
               rework=[{"stage": "recipe", "what": "   "}])
    only(solvability_rubric_problems(r), "says nothing in `what`")


def test_a_non_object_rework_is_refused_without_crashing():
    r = report(verdict="leaked", leaks=[LEAK], rework=["retry recipe"])
    only(solvability_rubric_problems(r), "rework #1 is not an object")


# --------------------------------------------------------------------------- #
# the skill and the checker are one contract, so they are read together
# --------------------------------------------------------------------------- #

def test_the_skill_states_the_rules_the_checker_enforces():
    """A checker stricter than the prompt parks good decks.  Every rule here
    costs a probe round when it fires, so the skill has to say it outright."""
    text = SKILL.read_text()
    for phrase in ("`est_steps_measured` at the top level is **the sum of "
                   "them**",
                   'add a `rework` entry with `"stage": "proposed"`',
                   "**Write all six keys into `checks`**",
                   "every degradation carries all nine keys",
                   "The pipeline walks\nthis table over the findings"):
        assert phrase in text, f"the skill no longer says: {phrase}"


def test_the_schema_example_in_the_skill_passes_its_own_checker():
    """The example is what a run copies.  One that the gate would refuse is a
    functional bug in a prompt."""
    text = SKILL.read_text()
    start = text.index("## Output: `solvability.json`")
    block = text[text.index("```json", start) + 7:]
    example = json.loads(block[:block.index("```")])
    problems = solvability_rubric_problems(example)
    assert problems == [], problems


# --------------------------------------------------------------------------- #
# what the archived runs would do against it
# --------------------------------------------------------------------------- #

ATTEMPTS = sorted((ROOT / "work" / "deck0009" / "attempts").glob(
    "solvable-*/solvability.json"))


@pytest.mark.parametrize("f", ATTEMPTS, ids=lambda p: p.parent.name)
def test_the_archived_probe_runs_are_all_refused(f):
    """Every one of the ten runs that produced four verdicts on one bundle.

    They predate the rewrite, so they carry none of `checks`, `rivals`,
    `tolerance` or a per-degradation step count — and the one that shipped two
    leaks under a verdict of `undetermined` is refused on the table itself.
    """
    problems = solvability_rubric_problems(json.loads(f.read_text()))
    assert problems, f"{f.parent.name} passes the rubric checker"


def test_the_run_that_shipped_two_leaks_as_undetermined_is_caught_by_the_table():
    f = ROOT / "work" / "deck0009" / "attempts" / "solvable-05" / \
        "solvability.json"
    if not f.exists():
        pytest.skip("archived run not present")
    r = json.loads(f.read_text())
    assert (r["verdict"], len(r["leaks"])) == ("undetermined", 2)
    assert verdict_from_findings(r)[:2] == ("leaked", 1)


def test_a_repaired_version_of_an_archived_run_passes():
    """The checker refuses these for what they say, not for being old: the one
    that already agrees with the table needs only the fields the rewrite
    added.  A checker no report can satisfy is a checker that parks the
    corpus."""
    f = ROOT / "work" / "deck0009" / "attempts" / "solvable-10" / \
        "solvability.json"
    if not f.exists():
        pytest.skip("archived run not present")
    r = copy.deepcopy(json.loads(f.read_text()))
    for d in r["degradations"]:
        d.update({"checks": {**dict.fromkeys(E := ("E1", "E2", "E3", "E4",
                                                   "E5", "E6"), ""),
                             "E5": "the surviving sibling pins it"},
                  "rivals": [], "tolerance": [], "overdetermined": False,
                  "est_steps_measured": 75, "undetermined": ""})
    r["est_steps_measured"] = 75 * len(r["degradations"])
    r["est_steps_declared"] = r["est_steps_measured"]
    r.setdefault("verdict_reason", "table line 5")
    assert solvability_rubric_problems(r) == [], solvability_rubric_problems(r)
