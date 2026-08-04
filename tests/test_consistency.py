"""What the reconcile gate let through, written down as assertions.

Two tasks out of the four whose trajectories were read had a defect this gate
exists to catch, and the gate passed both.  Every test here is one of those
two, a near neighbour that must *not* fire, or the parsing bug that would have
made the check useless in practice.

    python3 -m pytest tests/test_consistency.py -q
"""

import collections
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import consistency as C                              # noqa: E402

WORK = Path(__file__).resolve().parents[1] / "work"


def _facts(instruction="", *, slides=None, assets=None, task=None, delta=None,
           drawn=None, have_decks=True) -> C.DeckFacts:
    """A deck expressed as facts, so a check can be exercised without a 4 MB
    `.pptx` behind it.  `slides` is a list of (lost_blobs, gt_kinds, in_kinds)
    one per slide, 0-based."""
    f = C.DeckFacts(deck="deckTEST")
    f.task = dict(task or {})
    f.task.setdefault("instruction", instruction)
    f.delta = delta or {}
    f.assets = assets or {}
    f.have_decks = have_decks
    for lost, gt, init in (slides or []):
        f.slides.append(C.SlideFacts(collections.Counter(lost),
                                     collections.Counter(gt),
                                     collections.Counter(init)))
    f.drawn_in_input = collections.Counter(drawn or {})
    return f


def _named(findings, check):
    return [f for f in findings if f.check == check]


# --------------------------------------------------------------------------- #
# failure 1 — the instruction described damage that was never applied
# --------------------------------------------------------------------------- #


def test_a_picture_the_instruction_says_is_gone_has_to_be_gone():
    """deck0004 slide 9: *"the illustrations that sat above the other two went
    with them"*.  Both of slide 9's pictures were present in the degraded
    file; only SmartArt nodes had been dropped.  The model hunted the phantom
    for dozens of steps, deleted the SmartArt to make the page make sense, and
    scored 0 on work worth 0.63."""
    facts = _facts(
        'On slide 2 the pipeline is down to its first stage — the '
        'illustrations that sat above the other two went with them.',
        slides=[({}, {}, {}),
                ({}, {"picture": 2, "smartart": 1}, {"picture": 2})])
    hits = _named(C.check_damage_claims(facts), "damage_claim_unsupported")
    assert [h.slide for h in hits] == [2]
    assert hits[0].severity == "fail"


def test_a_picture_that_really_did_go_is_not_flagged():
    """Negative control for the check above.  If it fired on every mention of
    a missing picture it would be noise, and noise is how the real one gets
    scrolled past."""
    facts = _facts(
        'On slide 2 the pipeline is down to its first stage — the '
        'illustrations that sat above the other two went with them.',
        slides=[({}, {}, {}),
                ({"beef": 1}, {"picture": 2}, {"picture": 1})])
    assert not _named(C.check_damage_claims(facts), "damage_claim_unsupported")


def test_a_loss_is_blamed_on_the_nearest_noun_even_when_it_is_unscoreable():
    """deck0009 slide 11: *"the montage has lost its annotation layer — the
    ring that singles out one of the six scans and the labelled pointers into
    it are gone"*.  Every scan is still there; what went is the annotation.
    Reaching past `pointers` to the nearest *picture* noun — or reading
    `scan` as one — failed a deck that is correct on this point."""
    facts = _facts(
        'On slide 1 the montage has lost its annotation layer — the ring '
        'that singles out one of the six scans and the labelled pointers '
        'into it are gone.',
        slides=[({}, {"picture": 6}, {"picture": 6})])
    assert not _named(C.check_damage_claims(facts), "damage_claim_unsupported")


def test_a_transitive_loss_takes_the_object_that_follows_it():
    """deck0001 slide 6: *"the table screenshot no longer has the green,
    unfilled box"*.  The screenshot is intact — the box is what went.  Reading
    `no longer has` backwards blamed the screenshot and produced a fail on a
    deck that is correct on that point."""
    facts = _facts(
        'On slide 1 the table screenshot no longer has the green, unfilled '
        'box that framed the deposit sections.',
        slides=[({}, {"picture": 1}, {"picture": 1})])
    assert not _named(C.check_damage_claims(facts), "damage_claim_unsupported")


def test_a_claim_lands_on_the_slide_named_before_it_not_after():
    """deck0002: *"Slides 11 and 12 now have blank gaps …, even though the
    rest of the build-up across slides 8-13 is intact."*  Taking every number
    in the sentence put a damage claim on slides 8 and 13, which are the two
    the sentence says are fine."""
    claims = C.damage_claims(
        'On slides 2 and 3 the photos are gone, even though the rest of the '
        'build-up across slides 4-9 is intact.')
    assert sorted({c["slide"] for c in claims}) == [2, 3]


def test_a_degradation_with_no_delta_entry_fails():
    """The coarse form of the same failure: the recipe skipped a degradation
    and the instruction still describes it.  No language model needed."""
    facts = _facts(task={"degradations": [
        {"id": "d1", "slides": [4], "implemented": "as_proposed"}]},
        delta={"slides": {"3": [{"op": "delete", "deg": "d2"}]}})
    hits = _named(C.check_deg_attribution(facts), "deg_without_delta")
    assert len(hits) == 1 and hits[0].deg == "d1" and hits[0].severity == "fail"


def test_a_degradation_the_recipe_declared_skipped_is_not_flagged():
    """Negative control: `skipped` is an honest answer that reconcile already
    has to deal with; flagging it would bury the dishonest one."""
    facts = _facts(task={"degradations": [
        {"id": "d1", "slides": [4], "implemented": "skipped"}]},
        delta={"slides": {"3": [{"op": "delete", "deg": "d2"}]}})
    assert not _named(C.check_deg_attribution(facts), "deg_without_delta")


def test_a_delta_that_names_no_degradation_at_all_fails_once():
    """deck0001's delta carries 23 entries and no `deg` field.  Nothing then
    connects a sentence of the instruction to a line of the file, so every
    claim in it is unfalsifiable — and the reward stage scores changes nobody
    asked for.  One finding, not twenty-three."""
    facts = _facts(delta={"slides": {"3": [{"op": "delete"}, {"op": "move"}]}},
                   task={"degradations": [{"id": "d1", "slides": [4],
                                           "implemented": "as_proposed"}]})
    hits = C.check_deg_attribution(facts)
    assert [h.check for h in hits] == ["deg_unattributed"]


def test_a_slide_listed_only_as_a_reference_is_a_warning_not_a_failure():
    """deck0003's d2 lists slide 1 because that is where the two maps still
    are; deck0005's d3 lists slide 18 as the surviving twin.  Both are correct
    bookkeeping.  Failing them would have cost the check its credibility on
    the run where it mattered."""
    facts = _facts(task={"degradations": [
        {"id": "d1", "slides": [18, 1], "implemented": "as_proposed"}]},
        delta={"slides": {"17": [{"op": "delete", "deg": "d1"}]}})
    hits = _named(C.check_deg_attribution(facts), "deg_slide_without_delta")
    assert [h.slide for h in hits] == [1]
    assert hits[0].severity == "warn"


# --------------------------------------------------------------------------- #
# failure 2 — the ground truth is not a legal solution to its own task
# --------------------------------------------------------------------------- #


def test_a_demanded_table_the_ground_truth_does_not_have_fails():
    """deck0006 slide 6: *"rebuild it there as a real, editable table (not a
    pasted picture)"* — and the ground truth for that page is a picture.
    Obeying the instruction scores 0; the only winning move is the one the
    instruction forbids.  The model obeyed and got 0."""
    facts = _facts(
        'On slide 1 the entire table is gone — rebuild it there as a real, '
        'editable table (not a pasted picture).',
        slides=[({"beef": 1}, {"picture": 1, "textbox": 1}, {"textbox": 1})])
    hits = _named(C.check_ground_truth_is_a_solution(facts),
                  "gt_not_a_solution")
    assert len(hits) == 1 and hits[0].slide == 1 and hits[0].severity == "fail"


def test_a_demanded_chart_carries_over_from_the_previous_sentence():
    """deck0001 slide 4 names the slide in one sentence — *"Slide 4 has lost
    its 'World production' figure"* — and states the demand in the next: *"as
    a real editable chart … rather than a pasted picture"*.  A demand check
    that only looked inside one sentence would have missed it."""
    facts = _facts(
        "Slide 1 has lost its production figure completely. It needs to come "
        "back, but this time as a real editable chart built inside the "
        "presentation rather than a pasted picture.",
        slides=[({"beef": 1}, {"picture": 2}, {"picture": 1})])
    hits = _named(C.check_ground_truth_is_a_solution(facts),
                  "gt_not_a_solution")
    assert [h.slide for h in hits] == [1]


def test_a_demand_the_ground_truth_satisfies_is_not_flagged():
    """Negative control.  "Rebuild the table" on a page whose ground truth is
    a real table is the ordinary case and has to stay silent, or the two decks
    that are genuinely broken drown."""
    facts = _facts(
        'On slide 1 the entire table is gone — rebuild it as a real, '
        'editable table (not a pasted picture).',
        slides=[({}, {"table": 1, "textbox": 1}, {"textbox": 1})])
    assert not _named(C.check_ground_truth_is_a_solution(facts),
                      "gt_not_a_solution")


def test_a_demand_for_editable_boxes_is_not_read_as_an_object_kind():
    """deck0007 slide 4 says the block "has to come back as real editable
    boxes on the slide".  A box is not a `graphicFrame`; there is nothing in
    the ground truth to compare it against, so the check must say nothing
    rather than guess."""
    assert C.native_demands(
        "On slide 4 the block has to come back as real editable boxes on the "
        "slide, matching what the render shows.") == []


def test_the_supplied_reference_being_the_answer_fails_under_a_demand():
    """deck0006 ships `p06--2.png`, which is the removed table bitmap byte for
    byte, while telling the solver to build a table instead.  Pasting the file
    is what the reward pays for and what the instruction forbids."""
    facts = _facts(
        'On slide 1 the entire table is gone — rebuild it as a real, '
        'editable table (not a pasted picture).',
        slides=[({"beef": 1}, {"picture": 1}, {})],
        assets={"p01--2.png": "beef"})
    hits = _named(C.check_assets(facts), "asset_is_the_answer")
    assert len(hits) == 1 and hits[0].severity == "fail"


def test_the_supplied_original_bitmap_is_only_informational_on_its_own():
    """Negative control, and the common case: seven of the ten decks ship the
    removed bitmap because a cropped scan cannot be redrawn.  That is the
    design, not a defect — it only becomes one when the instruction also
    forbids using it."""
    facts = _facts(
        "On slide 1 the survey figure is gone. The original figure is "
        "supplied alongside the deck.",
        slides=[({"beef": 1}, {"picture": 1}, {})],
        assets={"p01-Picture-8.png": "beef"})
    hits = _named(C.check_assets(facts), "asset_is_the_answer")
    assert [h.severity for h in hits] == ["info"]


def test_a_removed_picture_nobody_can_reach_fails():
    """deck0001 slide 4 deletes an EMF that is in no asset and drawn nowhere
    in the degraded file.  Only `source.pptx` holds those bytes, so the ground
    truth is the one candidate that can produce them and the media gate fires
    on correct work."""
    facts = _facts(slides=[({"beef": 1}, {"picture": 1}, {})], drawn={})
    hits = _named(C.check_assets(facts), "media_unreachable")
    assert len(hits) == 1 and hits[0].severity == "fail"


def test_a_removed_picture_still_used_elsewhere_is_reachable():
    """Negative control.  deck0003 slide 18 loses two maps that slide 1 still
    draws — the instruction says so, and copying them is the task.  Calling
    that unreachable would reject a working deck."""
    facts = _facts(slides=[({"beef": 1}, {"picture": 1}, {})],
                   drawn={"beef": 1})
    assert not _named(C.check_assets(facts), "media_unreachable")


# --------------------------------------------------------------------------- #
# promises
# --------------------------------------------------------------------------- #


def test_a_filename_written_into_the_instruction_has_to_exist():
    """"the image file (p05-Picture-4.png) is in the same folder" is a promise
    to the solver.  A promise with nothing behind it costs the whole task, and
    it is one `listdir` to check."""
    facts = _facts("On slide 5 a screenshot is missing — the image file "
                   "(p05-Picture-4.png) is in the same folder as the deck.",
                   assets={"reference-p05.png": "beef"}, have_decks=False)
    hits = _named(C.check_assets(facts), "named_file_absent")
    assert len(hits) == 1 and "p05-Picture-4.png" in hits[0].message


def test_a_filename_that_is_there_is_not_flagged():
    """Negative control: all ten decks pass this one, and a check that never
    passes is a check nobody reads."""
    facts = _facts("the image file (p05-Picture-4.png) is in the same folder.",
                   assets={"p05-Picture-4.png": "beef"}, have_decks=False)
    assert not _named(C.check_assets(facts), "named_file_absent")


def test_a_sentence_does_not_end_inside_a_filename():
    """`reference-p10.png shows where they belong` split into "reference-p10."
    and "png shows where they belong", which detached every claim from its
    slide number on the three decks that name their assets inline."""
    got = C.sentences("reference-p10.png shows where they belong. It is 0.06 "
                      "in outside the true edges.")
    assert got == ["reference-p10.png shows where they belong.",
                   "It is 0.06 in outside the true edges."]


# --------------------------------------------------------------------------- #
# the corpus this was measured on
# --------------------------------------------------------------------------- #


def _decks():
    if not WORK.exists():
        return []
    return [d for d in sorted(WORK.glob("deck*"))
            if (d / "task.json").exists() and (d / "source.pptx").exists()
            and (d / "input.pptx").exists()]


@pytest.mark.skipif(not _decks(), reason="no reconciled decks in work/")
def test_the_two_decks_that_shipped_broken_are_the_two_that_fail():
    """The measurement behind the claim that this check bites without crying
    wolf: on the ten decks that shipped, the only `fail` verdicts are
    deck0001 (a chart demanded of a ground truth that is a picture, plus an
    EMF nobody can reach), deck0004 (the phantom slide-9 illustrations) and
    deck0006 (the editable table the ground truth is not).  deck0004 and
    deck0006 are the two whose trajectories were read and scored 0."""
    verdicts = {d.name: C.check_deck(d)["verdict"] for d in _decks()}
    assert {k for k, v in verdicts.items() if v == "fail"} == {
        "deck0001", "deck0004", "deck0006"}


@pytest.mark.skipif(not _decks(), reason="no reconciled decks in work/")
def test_the_report_is_json_serialisable_for_every_deck():
    """`reconcile` reads this before it writes `task.json`, and a report that
    cannot be handed to an agent as JSON is a report nobody acts on."""
    for d in _decks():
        json.dumps(C.check_deck(d), ensure_ascii=False)
