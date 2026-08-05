"""The step count, and the three documents that state it.

Reward is apportioned by how much work each degradation is, so the step count
is not a label on the task — it is the denominator of every weight in
`plan.json`.  Three documents state it and they are not the same statement:

| document | field | what reads it |
|---|---|---|
| `proposal.json` | per-degradation `est_steps` | **the weights**, via `comparators._est_steps` |
| `task.json` | one `est_steps` total | the difficulty band, the probe's `est_steps_declared`, the text of every gate message |
| `solvability.json` | `est_steps_measured`, per degradation and in total | the weights, where it is complete enough |

The defect these tests pin shut is that **nothing compared the first two**.
`pipeline.check_proposal` computes the sum of the parts, records it as
`sum_of_parts`, and never looks at it again; `pipeline.check_reconcile` does
not read `est_steps` at all, so reconcile may rewrite the total and leave the
parts where they lay.  Measured on the ten-deck corpus at the time of writing,
**9 of 10 decks declare a total that is not the sum of its own parts**, and on
three of them the gap crosses a difficulty band.

deck0010 is the case that exposed it.  Its gate refused quoting *"measured 480,
declared 280"* — and 280 was a headline no weight had ever read: the parts
summed to 255.  The complaint was about the wrong document.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from pptxgym import agent as A
from pptxgym import comparators as C

WORK = Path(__file__).resolve().parents[1] / "work"


# --------------------------------------------------------------------------- #
# a deck whose declaration can be edited
# --------------------------------------------------------------------------- #


def _fork(mini, tmp_path, *, parts: dict[str, int], total: int,
          solvability: dict | None = None) -> Path:
    """A copy of `mini_plain` that declares its work however the test needs.

    Copied rather than built, because what is under test is arithmetic over
    three JSON documents and none of it depends on what is in the `.pptx`.
    """
    root = tmp_path / "deck9910"
    shutil.copytree(mini.root("mini_plain"), root)

    proposal = json.loads((root / "proposal.json").read_text())
    for candidate in proposal["tasks"]:
        candidate["degradations"] = [{"id": deg, "est_steps": steps}
                                     for deg, steps in parts.items()]
    (root / "proposal.json").write_text(json.dumps(proposal, indent=1))

    task = json.loads((root / "task.json").read_text())
    task["est_steps"] = total
    (root / "task.json").write_text(json.dumps(task, indent=1))

    if solvability is not None:
        (root / "solvability.json").write_text(json.dumps(solvability, indent=1))
    return root


#: deck0010's declaration, in miniature: five degradations' worth of work
#: written down twice, 1.714x apart.
SPLIT_PARTS = {"d1": 240, "d2": 144, "d3": 96}          # 480
SPLIT_TOTAL = 280
WHOLE_TOTAL = 480


def _step_refusals(plan: dict) -> list[str]:
    return [r for r in plan["rejected"] if "est_steps" in r]


# --------------------------------------------------------------------------- #
# the parts against their own total
# --------------------------------------------------------------------------- #


def test_a_plan_may_not_split_reward_by_a_breakdown_that_contradicts_its_total(
        mini, tmp_path):
    """The refusal.  The weights come from the parts, the difficulty band and
    the probe's `est_steps_declared` come from the total, and here the two
    cannot both be the size of this task — so the reward is being split by a
    document that contradicts itself about how much work it is.

    No third number exists to arbitrate: this is the case where the solvability
    probe measured nothing, which is 3 of the 10 corpus decks.
    """
    plan = C.build_plan(_fork(mini, tmp_path, parts=SPLIT_PARTS,
                              total=SPLIT_TOTAL), write=False)
    assert plan["weight_source"] == "est_steps"
    refusal = _step_refusals(plan)
    assert len(refusal) == 1, plan["rejected"]
    assert "480" in refusal[0] and "280" in refusal[0] and "1.714" in refusal[0]


def test_a_declaration_that_adds_up_is_not_refused(mini, tmp_path):
    """The false-positive control, and the only difference from the test above
    is the one number.  A gate that fires on a deck whose two statements agree
    would refuse the whole corpus."""
    plan = C.build_plan(_fork(mini, tmp_path, parts=SPLIT_PARTS,
                              total=WHOLE_TOTAL), write=False)
    assert plan["weight_source"] == "est_steps"
    assert _step_refusals(plan) == []
    assert plan["weight_check"]["declared_split"] == 1.0


def test_rounding_in_the_declaration_is_not_a_contradiction(mini, tmp_path):
    """deck0001 declares 225 in parts against a 200 total and deck0005 415
    against 400 — a proposer rounding its own headline, not a document at war
    with itself.  Both are inside the band and neither may be refused."""
    for parts, total in (({"d1": 120, "d2": 60, "d3": 45}, 200),
                         ({"d1": 200, "d2": 130, "d3": 85}, 400)):
        plan = C.build_plan(_fork(mini, tmp_path / str(total), parts=parts,
                                  total=total), write=False)
        assert _step_refusals(plan) == [], (parts, total, plan["rejected"])


def test_the_plan_records_the_split_even_where_it_does_not_refuse(
        mini, tmp_path):
    """`weight_check` carries both numbers and their ratio whatever the
    verdict, so `consistency` and a human reading `plan.json` can see that the
    difficulty band was read off a total the breakdown disputes.  Recording it
    is the half of this that has no blast radius."""
    plan = C.build_plan(_fork(mini, tmp_path, parts=SPLIT_PARTS,
                              total=SPLIT_TOTAL), write=False)
    check = plan["weight_check"]
    assert check["declared_parts"] == 480
    assert check["declared_total"] == 280
    assert check["declared_split"] == 1.714


def test_a_measurement_that_wins_makes_a_stale_total_a_record_not_a_refusal(
        mini, tmp_path):
    """The proportionality rule.  When the probe's breakdown is complete it
    becomes the weights, the disputed total is merely stale, and refusing would
    send the deck back to `recipe` — a full re-degradation — to fix an
    arithmetic slip in a field no weight reads.

    This is deck0010 exactly: 480 measured, 280 declared, and the weights come
    out of the measurement either way.
    """
    root = _fork(mini, tmp_path, parts=SPLIT_PARTS, total=SPLIT_TOTAL,
                 solvability={"verdict": "solvable", "est_steps_measured": 300,
                              "est_steps_declared": SPLIT_TOTAL,
                              "degradations": [
                                  {"id": "d1", "est_steps_measured": 150},
                                  {"id": "d2", "est_steps_measured": 90},
                                  {"id": "d3", "est_steps_measured": 60}]})
    plan = C.build_plan(root, write=False)
    assert plan["weight_source"] == "steps_measured"
    assert _step_refusals(plan) == []
    assert plan["weight_check"]["declared_split"] == 1.714
    weights = {d["id"]: d["weight"] for d in plan["degradations"]}
    assert weights["d1"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# the breakdown against the probe's own total
# --------------------------------------------------------------------------- #


def _probe(tmp_path, **report) -> Path:
    root = tmp_path / "deck9911"
    root.mkdir(parents=True, exist_ok=True)
    (root / "solvability.json").write_text(json.dumps(report))
    return root


def test_a_structured_breakdown_that_does_not_add_up_is_not_weighted_by(
        tmp_path):
    """The check the structured path did not have.  It applied to the prose
    parse — the weaker source — and not to the field the weights actually come
    from on 7 of the 10 decks.

    A breakdown that does not add up to the total written beside it is one
    self-contradicting statement whichever field it arrived in.  It is still
    returned, because a measurement too self-contradictory to redistribute
    reward can still contradict the *declaration*, and `STEP_DISAGREEMENT`
    refuses on that.
    """
    root = _probe(tmp_path, est_steps_measured=300, degradations=[
        {"id": "d1", "est_steps_measured": 10},
        {"id": "d2", "est_steps_measured": 10}])
    steps, why, ok = C._measured_steps(root, ["d1", "d2"])
    assert not ok, why
    assert "does not agree" in why and "20" in why and "300" in why
    assert steps == {"d1": 10, "d2": 10}


def test_a_structured_breakdown_that_adds_up_is_weighted_by(tmp_path):
    """The control: the same shape, the arithmetic sound.  All four corpus
    decks that carry a structured breakdown sum to their own total exactly, so
    the check above costs the corpus nothing."""
    root = _probe(tmp_path, est_steps_measured=300, degradations=[
        {"id": "d1", "est_steps_measured": 200},
        {"id": "d2", "est_steps_measured": 100}])
    steps, why, ok = C._measured_steps(root, ["d1", "d2"])
    assert ok and steps == {"d1": 200, "d2": 100}, why


def test_the_structured_and_prose_paths_are_held_to_the_same_band(tmp_path):
    """One band, two syntaxes.  A breakdown 20% under its own total passes
    both; 40% under fails both.  Before this, the same numbers were read one
    way as prose and another as JSON."""
    for slack, want in ((0.80, True), (0.60, False)):
        structured = _probe(tmp_path / f"s{slack}", est_steps_measured=100,
                            degradations=[{"id": "d1",
                                           "est_steps_measured": int(100 * slack)}])
        prose = _probe(tmp_path / f"p{slack}", est_steps_measured=100,
                       notes=[f"Step estimate: d1 about {int(100 * slack)}."])
        assert C._measured_steps(structured, ["d1"])[2] is want
        assert C._measured_steps(prose, ["d1"])[2] is want


# --------------------------------------------------------------------------- #
# the two thresholds
# --------------------------------------------------------------------------- #


def test_the_declaration_band_is_the_solvability_rubrics_band(tmp_path):
    """`DECLARATION_SPLIT` and `agent.STEP_BAND` are the same fact — *how far
    two statements of one step total may sit apart* — asked of one document
    and of two.  Pinned together so they cannot drift into two opinions."""
    assert C.DECLARATION_SPLIT == A.STEP_BAND == 0.25


def test_step_disagreement_is_not_a_second_opinion_about_the_same_fact():
    """The question deck0010 raised: does `STEP_DISAGREEMENT = 3.0` disagree
    with a gate that refused at 1.7x?

    It does not, because the two never meet.  `STEP_BAND` is a fraction of a
    **deck total** and decides whether the probe owed a `rework` note;
    `STEP_DISAGREEMENT` is a ratio on **one degradation** and decides whether
    reward may still be split by a breakdown the probe has contradicted — and
    it is only consulted when the measurement is too partial to weight by, so
    it never arbitrates between two usable numbers.  Different denominators,
    different units, different consequences.

    The numbers below are deck0010's own: 480 measured against 280 declared is
    71% out, past the 25% band, so the note was owed and was filed — while the
    plan's step gates stay silent because the measurement was complete and
    simply became the weights.
    """
    assert abs(480 - 280) / 280 > A.STEP_BAND
    assert 480 / 280 < C.STEP_DISAGREEMENT
    # and the ratio the plan would have to compute is per degradation, where
    # deck0010's largest was 3.75x — over the threshold the deck total is
    # nowhere near.
    assert 150 / 40 > C.STEP_DISAGREEMENT


def test_the_step_disagreement_threshold_still_separates_the_corpus():
    """Its comment used to justify 3.0 with four figures, three of which no
    longer reproduce.  These are the per-degradation worst ratios measured on
    the corpus as it stands; 3.0 has to sit in the gap between the seventh and
    the eighth, and there is only one gap wide enough to sit in."""
    worst = [1.000, 1.600, 1.857, 1.867, 1.889, 1.917, 8.000]
    below = [r for r in worst if r < C.STEP_DISAGREEMENT]
    assert below == worst[:-1]
    assert max(below) < C.STEP_DISAGREEMENT < min(r for r in worst
                                                  if r >= C.STEP_DISAGREEMENT)


# --------------------------------------------------------------------------- #
# the live corpus
# --------------------------------------------------------------------------- #


@pytest.mark.corpus
def test_every_corpus_plan_can_see_its_own_declaration_split():
    """`plan.json` now carries both statements and their ratio on every deck,
    which is the half of this with no blast radius: before it, the split was
    invisible to `consistency`, to `status`, and to anyone reading the plan.

    When first measured, 9 of the 10 decks declared a total that was not the
    sum of its own parts (deck0010 1.714x, deck0007 1.393x, deck0003 1.357x,
    deck0006 1.333x, then five inside the band).  Repairs move that number, so
    it is not asserted; what is asserted is that it is always *there*.
    """
    for root in sorted(WORK.glob("deck*")):
        if not (root / "task.json").exists():
            continue
        check = C.build_plan(root, write=False)["weight_check"]
        assert check["declared_parts"], root.name
        assert check["declared_total"], root.name
        assert check["declared_split"] >= 1.0, (root.name, check)


@pytest.mark.corpus
def test_no_corpus_deck_splits_reward_by_a_total_it_disputes():
    """The invariant, on the live tree: a deck whose two statements disagree
    past the band has either a measurement arbitrating between them or a
    refusal.  What may not happen is the third thing — the plan quietly
    splitting reward by a breakdown its own task record contradicts.

    This is also the blast-radius control.  The decks past the band at the time
    of writing were deck0006 (parts 380, total 285) and deck0007 (390 / 280),
    both of which carry a complete measurement, so neither is refused and
    neither is sent back to `recipe` for an arithmetic slip.
    """
    for root in sorted(WORK.glob("deck*")):
        if not (root / "task.json").exists():
            continue
        plan = C.build_plan(root, write=False)
        check = plan["weight_check"]
        if check["declared_split"] <= 1 + C.DECLARATION_SPLIT:
            assert _step_refusals(plan) == [], (root.name, plan["rejected"])
        else:
            assert (plan["weight_source"] == "steps_measured"
                    or _step_refusals(plan)), (root.name, check)
