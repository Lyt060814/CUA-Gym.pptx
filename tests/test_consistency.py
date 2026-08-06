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
# failure 3 — the plan scores work the instruction excuses
# --------------------------------------------------------------------------- #


def _comp(op, slide, kind=None, weight=0.05, cid="c001"):
    return {"id": cid, "deg": "d1", "op": op, "slide": slide - 1,
            "weight": weight, "spec": {"kind": kind} if kind else {}}


def test_an_exemption_written_deck_wide_covers_the_whole_deck():
    """deck0002, verbatim.  The sentence names no slide and says "any", which
    is how anyone writes an amnesty; scoping it to whatever slide the previous
    sentence happened to mention would have missed all three components."""
    instruction = (
        "On slide 19 the five-stage process graphic has disappeared. "
        "The click-build animations on several of the mangled slides went out "
        "with the deleted shapes; you do not need to re-create any animation, "
        "only the artwork.")
    components = [_comp("strip_animation", 6, cid="c008", weight=0.032534),
                  _comp("strip_animation", 7, cid="c021", weight=0.016743),
                  _comp("strip_animation", 16, cid="c027", weight=0.030822),
                  _comp("delete", 19, "smartart", cid="c030", weight=0.2)]
    hits = C.excused_components(instruction, components)
    assert len(hits) == 1
    assert hits[0]["components"] == ["c008", "c021", "c027"]
    assert hits[0]["weight"] == pytest.approx(0.080099)
    assert hits[0]["slides"] is None


def test_an_exemption_that_names_no_slide_stays_on_the_one_under_discussion():
    """deck0004, verbatim, and the reason a deck-wide reading is not the
    default: "the small illustrations … are not expected back" is about slide
    9, and read deck-wide it would be an amnesty on all six of that deck's
    slide-12 picture components."""
    instruction = (
        'On slide 9 the alignment pipeline is down to its first stage. '
        'It is the stage boxes and their wording that matter there — the '
        'small illustrations that used to sit above those two stages went '
        'with them and are not expected back.')
    components = [_comp("delete", 12, "picture", cid=f"c{n:03d}")
                  for n in range(1, 7)]
    assert C.excused_components(instruction, components) == []


def test_the_nearest_noun_wins_the_exemption_or_nothing_does():
    """The attribution rule `damage_claims` already pays for: in the deck0004
    sentence above the noun immediately behind the cue is "stages", which no
    operator family is named after, so the cue is dropped rather than passed
    along to the "illustrations" further back."""
    assert C.excused_work(
        "the small illustrations that used to sit above those two stages "
        "went with them and are not expected back") == []
    assert [e["bucket"] for e in C.excused_work(
        "The pictures on slide 9 are not expected back.")] == ["picture"]


def test_a_prohibition_is_not_an_exemption():
    """Every one of these contains "not" and none of them excuses anything.
    Reading them as exemptions would refuse four of the ten decks for
    sentences that are doing their job."""
    for sentence in (
            "the missing options belong back in their slots as real boxes on "
            "the slide, not as a picture of the render laid over the page",
            "rebuild it there as a real, editable table (not a pasted picture)",
            "the picture file itself was not recovered",
            "only the first one is still there",
    ):
        assert C.excused_work(sentence) == [], sentence


def test_an_exemption_only_covers_the_operators_it_names():
    """An amnesty on the pictures is not an amnesty on the text boxes deleted
    beside them, so `delete` is additionally restricted by the shape kind the
    delta recorded."""
    instruction = "You do not need to put back any of the pictures."
    assert C.excused_components(
        instruction, [_comp("delete", 4, "textbox")]) == []
    assert C.excused_components(
        instruction, [_comp("delete", 4, "picture")])[0]["components"] == ["c001"]


def test_the_check_reads_the_plan_and_says_nothing_without_one():
    """It is a comparison between two artefacts, so with only one of them there
    is nothing to report — and a check that guessed would fire on every deck
    whose plan has not been built yet."""
    facts = _facts("You do not need to re-create any animation.")
    assert C.check_excused_work(facts) == []
    facts.plan = {"components": [_comp("strip_animation", 6)]}
    assert [f.severity for f in C.check_excused_work(facts)] == ["fail"]


# --------------------------------------------------------------------------- #
# the step count the weights come from
# --------------------------------------------------------------------------- #


def test_the_step_count_is_no_longer_the_thing_nothing_measures():
    """This module's own docstring used to list "is the difficulty still
    right?" among the questions it declined, on the grounds that *nothing in
    the artefacts measures it*.  `solvability.json` measures it, and on the
    corpus it disagreed with the declaration by up to 8x while the weights came
    from the declaration alone."""
    facts = _facts("anything")
    facts.plan = {"weight_check": {
        "source": "steps_measured", "worst": 8.0,
        "declared": {"d1": 120}, "measured": {"d1": 15},
        "measured_from": "solvability (prose, 310 vs the probe's own total 310)"}}
    assert [f.check for f in C.check_step_estimate(facts)] == \
        ["step_estimate_measured"]

    facts.plan = {"weight_check": {
        "source": "est_steps", "worst": None, "declared": {"d1": 120},
        "measured": None, "measured_from": "no per-degradation breakdown"}}
    finding = C.check_step_estimate(facts)[0]
    assert finding.check == "step_estimate_unmeasured"
    assert finding.severity == "warn"

    facts.plan = {"weight_check": {
        "source": "est_steps", "worst": 8.0, "declared": {"d1": 120},
        "measured": {"d1": 15}, "measured_from": "incomplete"}}
    finding = C.check_step_estimate(facts)[0]
    assert finding.check == "step_estimate_contradicted"
    assert finding.severity == "fail"


# --------------------------------------------------------------------------- #
# the corpus this was measured on
# --------------------------------------------------------------------------- #


def _decks():
    if not WORK.exists():
        return []
    return [d for d in sorted(WORK.glob("deck*"))
            if (d / "task.json").exists() and (d / "source.pptx").exists()
            and (d / "input.pptx").exists()]


#: every `fail` this module raises on the corpus, and what it is about.  Pinned
#: as a *reason* rather than as a set of deck names: the decks are live and the
#: repairer moves them, which is what left the previous form of this test
#: asserting `{deck0001, deck0004, deck0006}` long after all three were fixed.
#: What has to hold is that no `fail` appears whose cause nobody has read.
KNOWN_FAILURES = {
    # the instruction excuses work its own plan scores — deck0002's three
    # `strip_animation` components against "you do not need to re-create any
    # animation", 0.0801 of the task and a 0.9199 ceiling on obedience
    "excused_work_is_scored",
    # a picture the degradation removed whose bytes are nowhere the solver can
    # reach — deck0008 withholds two original bitmaps on purpose
    "media_unreachable",
    "damage_claim_unsupported", "gt_not_a_solution",
    "named_file_absent", "listed_asset_absent", "asset_is_the_answer",
    "deg_unattributed", "deg_without_delta",
}


@pytest.mark.corpus
@pytest.mark.skipif(not _decks(), reason="no reconciled decks in work/")
def test_no_deck_fails_for_a_reason_nobody_has_read():
    reports = {d.name: C.check_deck(d) for d in _decks()}
    seen = {f["check"] for r in reports.values() for f in r["findings"]
            if f["severity"] == "fail"}
    assert seen <= KNOWN_FAILURES, sorted(seen - KNOWN_FAILURES)


@pytest.mark.corpus
@pytest.mark.skipif(not _decks(), reason="no reconciled decks in work/")
def test_the_deck_whose_instruction_excuses_its_own_plan_is_the_one_that_fails():
    """The measurement behind the claim that this check bites without crying
    wolf.  deck0002's instruction ends *"you do not need to re-create any
    animation, only the artwork"* and its plan scores three `strip_animation`
    components for 0.0801 — an obedient agent's ceiling is 0.9199, measured.
    On the other nine decks the check is silent, including deck0004, whose
    *"the small illustrations … are not expected back"* is an exemption the
    attribution rule correctly declines to blame on a picture."""
    hit = {d.name for d in _decks()
           if any(f["check"] == "excused_work_is_scored"
                  for f in C.check_deck(d)["findings"])}
    assert hit == {"deck0002"}


@pytest.mark.corpus
@pytest.mark.skipif(not _decks(), reason="no reconciled decks in work/")
def test_the_report_is_json_serialisable_for_every_deck():
    """`reconcile` reads this before it writes `task.json`, and a report that
    cannot be handed to an agent as JSON is a report nobody acts on."""
    for d in _decks():
        json.dumps(C.check_deck(d), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# the same three questions, of decks the suite owns
# --------------------------------------------------------------------------- #


def test_no_frozen_deck_fails_for_a_reason_nobody_has_read(mini, mini_work):
    """The same rule as its corpus twin, pinned as a *reason* rather than as a
    set of deck names, and asked of decks that do not move."""
    seen = {f["check"] for name in sorted(mini.roots)
            for f in C.check_deck(mini.root(name))["findings"]
            if f["severity"] == "fail"}
    assert seen <= KNOWN_FAILURES, sorted(seen - KNOWN_FAILURES)


def test_the_frozen_deck_whose_instruction_excuses_its_own_plan_is_the_one_that_fails(
        mini, mini_work):
    """The measurement behind the claim that this check bites without crying
    wolf.  `mini_excused` says *"You do not need to put back any of the fonts
    or styling anywhere in the deck"* over a plan that scores a `set_font`;
    on the six decks beside it the check is silent, including `mini_picture`,
    whose instruction discusses a picture the plan does score.

    deck0002 was the corpus specimen and still is — see the `corpus` twin
    above — but "the check fires on the deck that earned it and on no other"
    stopped being a fact about deck0002 the moment it had a second specimen.
    """
    # `mini_work` is required rather than decorative: the check reads
    # `plan.json`, which only exists once the deck has been planned.
    hit = {name for name in sorted(mini.roots)
           if any(f["check"] == "excused_work_is_scored"
                  for f in C.check_deck(mini.root(name))["findings"])}
    assert hit == {"mini_excused"}


def test_the_frozen_report_is_json_serialisable(mini, mini_work):
    """`reconcile` reads this before it writes `task.json`, and a report that
    cannot be handed to an agent as JSON is a report nobody acts on."""
    for name in sorted(mini.roots):
        json.dumps(C.check_deck(mini.root(name)), ensure_ascii=False)


def test_the_frozen_deck_whose_delta_carries_no_deg_is_the_one_that_fails(
        mini, mini_work):
    """The other `fail` these decks can raise, and the reason `mini_no_deg`
    exists: a delta nothing can be attributed from is a task nobody can be
    scored against, and `check_deck` has to say so before `build_plan` does."""
    hit = {name for name in sorted(mini.roots)
           if any(f["check"] == "deg_unattributed"
                  for f in C.check_deck(mini.root(name))["findings"])}
    assert hit == {"mini_no_deg"}


# --------------------------------------------------------------------------- #
# nested assets — deck0005 escalated this rather than spend its last attempts
# --------------------------------------------------------------------------- #


def _deck_with_assets(tmp_path, files: dict, task: dict) -> C.DeckFacts:
    root = tmp_path / "deck0005"
    (root / "assets").mkdir(parents=True)
    for name, body in files.items():
        p = root / "assets" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    (root / "task.json").write_text(json.dumps(task))
    return C.read_facts(root)


def test_an_asset_in_a_subdirectory_is_found(tmp_path):
    """run 14, deck0005:

        REJECTED — listed_asset_absent: task.json lists
                   'build-p03/build.json', which is not in assets/

    four times, against a file that was there the whole time. The scan read
    `iterdir()` and keyed on the basename, so the directory was skipped for
    not being a file and everything under it was invisible. `emit_assets`
    states the opposite in as many words: names are relative to `assets/`.
    """
    facts = _deck_with_assets(
        tmp_path,
        {"build-p03/build.json": b"{}", "build-p03/f01.png": b"\x89PNG",
         "data.csv": b"a,b\n"},
        {"instruction": "do it",
         "assets": [{"file": "build-p03/build.json"},
                    {"file": "build-p03/f01.png"},
                    {"file": "data.csv"}]})
    assert _named(C.check_assets(facts), "listed_asset_absent") == []
    assert "build-p03/build.json" in facts.assets


def test_a_listed_asset_that_really_is_missing_still_fails(tmp_path):
    """The control. Widening the scan must not turn the check off — a
    `task.json` promising the solver a file that is not there is the defect
    this check exists for."""
    facts = _deck_with_assets(
        tmp_path, {"data.csv": b"a,b\n"},
        {"instruction": "do it",
         "assets": [{"file": "build-p03/build.json"}, {"file": "data.csv"}]})
    hits = _named(C.check_assets(facts), "listed_asset_absent")
    assert len(hits) == 1 and "build-p03/build.json" in hits[0].message


def test_the_instruction_may_name_an_asset_by_its_bare_name(tmp_path):
    """An instruction writes what a person would type. Keying the table on the
    relative path without allowing a tail match would have swapped one false
    failure for another, on decks that were passing before."""
    facts = _deck_with_assets(
        tmp_path, {"build-p03/build.json": b"{}"},
        {"instruction": "open build.json and follow it", "assets": []})
    assert _named(C.check_assets(facts), "named_file_absent") == []


def test_a_bare_name_matching_nothing_is_still_a_broken_promise(tmp_path):
    """The control for the control."""
    facts = _deck_with_assets(
        tmp_path, {"build-p03/build.json": b"{}"},
        {"instruction": "open frames.csv and follow it", "assets": []})
    assert _named(C.check_assets(facts), "named_file_absent")


def test_a_nested_asset_is_no_longer_exempt_from_the_anti_cheat_check(tmp_path):
    """The quieter half, and the reason this is worth a test of its own.
    `asset_is_the_answer` compares supplied bytes against the pictures the
    degradation removed and reads the same table, so every nested asset was
    exempt from an anti-cheat check without anybody choosing that."""
    root = tmp_path / "deck0005"
    (root / "assets" / "build-p03").mkdir(parents=True)
    (root / "assets" / "build-p03" / "f01.png").write_bytes(b"\x89PNGthepic")
    (root / "task.json").write_text(json.dumps({"instruction": "x"}))
    facts = C.read_facts(root)
    assert "build-p03/f01.png" in facts.assets
    sha = facts.assets["build-p03/f01.png"]
    facts.have_decks = True
    facts.slides = [C.SlideFacts(collections.Counter([sha]),
                                 collections.Counter(), collections.Counter())]
    assert _named(C.check_assets(facts), "asset_is_the_answer")
