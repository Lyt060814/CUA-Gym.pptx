"""What the scoring stage must not do.

Every test here is a failure that has actually happened, in this pipeline or
in the batch of tasks that shipped before it: a comparator handing out full
marks for a missing prior value, a gate zeroing a model that had done 43% of
the work, a stock text box collecting credit twice for being roughly the right
size.  The comments name the casualty rather than the rule.

    python3 -m pytest tests/ -q
"""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym.evaluation import comparators as C                              # noqa: E402
from pptxgym.office import degrade_exec as D                             # noqa: E402
from pptxgym.evaluation.inventory import inventory_pptx                      # noqa: E402

WORK = Path(__file__).resolve().parents[1] / "work"
DECKS = sorted(p for p in WORK.glob("deck0*") if (p / "delta.json").exists())

#: Every corpus deck `build_plan` currently refuses, and the *cue* in the
#: refusal that says why.  A refusal is a finding about that deck, so it is
#: recorded here by its reason and asserted as a set in
#: `test_the_refused_decks_are_refused_for_the_reasons_recorded_here` — which
#: is the only thing in this file that may name a deck.
#:
#: Pinning reasons rather than a list of names is not decoration.  The
#: previous form of this was a hardcoded `ACCEPTED` tuple, and it put
#: `deck0008` among the decks every rule below is asserted of.  deck0008
#: cannot be scored at all — its ground truth needs two bitmaps the task
#: deliberately withholds, so `media_not_pasted` fires on the answer itself —
#: and the consequence was **five** test failures that looked exactly like
#: five regressions in `comparators.py` and were one defect in one task.
#:
#: If a deck here is repaired, the set assertion goes red and names it: remove
#: the entry.  If a new deck breaks, the same assertion goes red and names
#: that.  Neither shows up as a rule failing.
REFUSED = {
    # `d2` is nine `set_font` components on runs whose ground truth inherits
    # the properties they ask for; all nine drop out as unsatisfiable and the
    # degradation is left with nothing anybody can score.
    "deck0001": "no scoreable component",
    # the instruction ends "you do not need to re-create any animation, only
    # the artwork" over three scored `strip_animation` components: 0.0801 of
    # the task, and an obedient agent's ceiling is 0.9199.
    "deck0002": "the instruction excuses",
    # **not a defect in this file.**  The task withholds two original bitmaps
    # on purpose and supplies only a reference render, so the ground truth
    # itself holds blobs the anti-paste gate calls intruders.  The reward
    # model cannot score this deck as authored: the fix is in the deck — ship
    # the two images as assets, or degrade something else.
    "deck0008": "media_not_pasted fires on `ground_truth`",
}

#: the decks whose plan `build_plan` accepts.  Derived, so that a deck moving
#: between the two lists is one edit above and not a silent change of subject.
ACCEPTED = tuple(p.name for p in DECKS if p.name not in REFUSED)


# --------------------------------------------------------------------------- #
# fixtures — real decks are slow to parse, so parse each one once
# --------------------------------------------------------------------------- #


_CACHE: dict[str, tuple] = {}


def _deck(name: str):
    if name not in _CACHE:
        root = WORK / name
        _CACHE[name] = (C.build_plan(root, write=False),
                        inventory_pptx(root / "source.pptx"),
                        inventory_pptx(root / "input.pptx"))
    plan, gt, init = _CACHE[name]
    return copy.deepcopy(plan), gt, init


def _specimen(name="deck0002"):
    """A real deck's plan with the `plan_accepted` gate stood down.

    For the tests whose subject is a scoring rule rather than a deck's fitness
    to ship.  deck0002 is the richest damage in the corpus and its plan is
    refused for one sentence of its instruction (see `ACCEPTED`); leaving that
    gate up would zero every candidate below and make those tests pass for a
    reason that has nothing to do with what they check.  The `attacks` report
    stands the same gate down for the same reason.
    """
    plan, gt, init = _deck(name)
    return {**plan, "rejected": []}, gt, init


def _shape(text="", box=None, kind="autoshape", name="Rectangle 1", sid=7):
    box = box or {"cx": 1000000, "cy": 1000000, "w": 900000, "h": 400000,
                  "rot": 0.0, "flip": False}
    keys = ([f"txt:{C._sha(text.encode(), 12)}"] if text else []) + [
        f"name:{name}", f"geo:{kind}:{round(box['w'] / 91440)}x"
        f"{round(box['h'] / 91440)}", f"kind:{kind}"]
    return {"_path": "0", "_id": sid, "_name": name, "_plain": text,
            "kind": kind, "z": 0, "group": None, "bbox": dict(box),
            "hidden": False, "keys": keys, "key": keys[0] + "#0",
            **({"text": {"paragraphs": [{"t": text}]}} if text else {})}


def _inv(*shapes, media=()):
    return {"format": "pptxgym.inventory/1",
            "package": {"slide_count": 1, "slide_order": ["slide1.xml"],
                        "slide_w": 12192000, "slide_h": 6858000,
                        "parts": {}, "media": sorted(media), "_media_parts": {}},
            "slides": [{"i": 0, "_part": "ppt/slides/slide1.xml", "layout": "L",
                        "master": "M", "hidden": False, "background": None,
                        "notes": None, "transition": None, "animation": None,
                        "n_shapes": len(shapes), "shapes": list(shapes)}],
            "layouts": {}, "masters": {}}


def _plan(*components, **extra):
    damage = {"slides": [], "paths": {}, "boxes": {}}
    for component in components:
        if component["slide"] not in damage["slides"]:
            damage["slides"].append(component["slide"])
        if component.get("gt_path"):
            damage["paths"].setdefault(str(component["slide"]), []).append(
                component["gt_path"])
        box = (component.get("spec") or {}).get("box")
        if box:
            damage["boxes"].setdefault(str(component["slide"]), []).append(box)
    return {"format": C.PLAN_FORMAT, "deck": "t", "task": "t",
            "assets_sha": [], "init_slide_of": None, "damage": damage,
            "degradations": [], "components": list(components),
            "unscoreable": [], "rejected": [], **extra}


def _component(op, spec, weight=1.0, path="0", slide=0):
    return {"id": "c001", "deg": "d1", "op": op, "slide": slide,
            "gt_path": path, "weight": weight, "spec": spec}


# --------------------------------------------------------------------------- #
# fail closed
# --------------------------------------------------------------------------- #


def test_a_missing_prior_value_scores_zero_not_one():
    """`pptx-tasks/scaling/pipeline/ops.py` answers `if not exp: return 1.0,
    "restored"` — feed it a shape nobody touched and it hands out full marks
    for doing nothing.  A comparator that cannot find its prior value has to
    raise, and `score` has to read the raise as zero."""
    gt = _inv(_shape("hello"))
    component = _component("smartart_drop_nodes", {"data_part": "nope"})
    result = C.score(_plan(component), gt, gt, gt)
    assert result["components"][0]["raw"] == 0.0
    assert "unscorable" in result["components"][0]["why"]


def test_every_registered_operator_has_a_comparator():
    """An operator with no comparator silently scores 0 for everyone, which is
    a task nobody can pass rather than a task nobody wrote."""
    missing = set(D.REGISTRY) - set(C.REGISTRY)
    # `delete_slide` is a registry stub that only exists to raise SystemExit
    # naming `delete_slides`; it can never reach a delta entry.
    assert missing == {"delete_slide"}


def test_an_operator_with_no_comparator_scores_zero():
    gt = _inv(_shape("hello"))
    result = C.score(_plan(_component("invented_op", {})), gt, gt, gt)
    assert result["components"][0]["raw"] == 0.0
    assert "no comparator" in result["components"][0]["why"]


def test_chart_part_disambiguates_two_charts_on_one_slide():
    """The degrader records the exact chart part, so a paired-chart slide is
    not inherently unscoreable."""
    first = _shape(kind="chart", name="Chart 1", sid=7)
    first["chart"] = {"_part": "ppt/charts/chart1.xml"}
    second = _shape(kind="chart", name="Chart 2", sid=8)
    second["_path"] = "1"
    second["chart"] = {"_part": "ppt/charts/chart2.xml"}
    slide = _inv(first, second)["slides"][0]

    assert C._find_chart(slide, "ppt/charts/chart2.xml") is second


@pytest.mark.corpus
@pytest.mark.parametrize("name", ACCEPTED)
def test_the_broken_file_scores_zero_on_every_component(name):
    """The trap in rule form: doing nothing at all must not be worth a point
    on any operator, on any deck."""
    plan, gt, init = _deck(name)
    result = C.score(plan, init, gt, init)
    assert result["score"] == 0.0
    assert all(c["score"] == 0.0 for c in result["components"])


@pytest.mark.corpus
@pytest.mark.parametrize("name", ACCEPTED)
def test_the_ground_truth_scores_one(name):
    plan, gt, init = _deck(name)
    result = C.score(plan, gt, gt, init)
    assert result["failed_gate"] is None
    assert result["score"] == pytest.approx(1.0)


@pytest.mark.corpus
@pytest.mark.parametrize("name", ACCEPTED)
def test_every_component_discriminates(name):
    """A component that scores the same on the answer and on the wreckage
    measures nothing and is weight taken away from the components that do."""
    plan, gt, init = _deck(name)
    good = C.score(plan, gt, gt, init)
    bad = C.score(plan, init, gt, init)
    dull = [c["id"] for c, b in zip(good["components"], bad["components"])
            if c["raw"] - b["raw"] <= 0]
    assert dull == []


# --------------------------------------------------------------------------- #
# floor normalisation
# --------------------------------------------------------------------------- #


def test_the_floor_is_subtracted_not_reported():
    """Without it `move` and `resize` hand out half credit for a shape that
    was never touched: a component the broken file already half-satisfies
    starts everybody at half marks."""
    gt = _inv(_shape("a"), _shape("b", name="Rectangle 2", sid=8))
    half = _inv(_shape("a"), _shape("b", name="Rectangle 2", sid=8))
    half["slides"][0]["shapes"][1]["bbox"]["cx"] += 3 * C.EMU_PER_INCH
    component = _component("clear_table_cells", {"cleared": []})
    # a comparator with a floor of 0.5 must report 0.0 for the broken file
    C.REGISTRY["_probe"] = lambda t: (0.5 if t.shape else 0.0, "probe")
    try:
        component = _component("_probe", {})
        result = C.score(_plan(component), half, gt, half)
        assert result["components"][0]["raw"] == 0.5
        assert result["components"][0]["floor"] == 0.5
        assert result["components"][0]["score"] == 0.0
    finally:
        del C.REGISTRY["_probe"]


@pytest.mark.corpus
@pytest.mark.parametrize("name", ACCEPTED)
def test_no_accepted_deck_carries_a_floor_over_the_limit(name):
    """A floor above the limit is a task to send back to `recipe`, not a
    tolerance to widen — so an accepted plan may not contain one."""
    plan, _gt, _init = _deck(name)
    assert [c["id"] for c in plan["components"] if c["floor"] > C.FLOOR_LIMIT] == []


@pytest.mark.corpus
def test_a_high_floor_rejects_the_plan_of_whichever_deck_carries_one():
    """deck0009 de-bolded a table whose runs were largely unbolded already, so
    two components were 55% and 65% satisfied by the wreckage.  The plan has
    to say so rather than quietly normalise it away.

    The deck has since been repaired, which is exactly the reason this is
    written as a search rather than as `WORK / "deck0009"`: the rule outlived
    its only specimen, and a test that names the specimen goes red when
    somebody does the right thing.  It now asserts the rule of whichever deck
    exhibits it, and says so plainly when none does — the rule itself is
    pinned against a deck the suite owns, in
    `test_a_high_floor_rejects_a_frozen_plan`.
    """
    hot_decks = {}
    for deck in DECKS:
        plan = C.build_plan(deck, write=False)
        hot = [c["id"] for c in plan["components"]
               if c["floor"] > C.FLOOR_LIMIT]
        if hot:
            hot_decks[deck.name] = (hot, plan["rejected"])
    if not hot_decks:
        pytest.skip("no deck in work/ carries a floor over the limit any more "
                    "— the frozen form is "
                    "`test_a_high_floor_rejects_a_frozen_plan`")
    for name, (hot, rejected) in hot_decks.items():
        assert any("floor above" in reason for reason in rejected), (
            f"{name} has {len(hot)} component(s) over the floor limit and its "
            f"plan does not say so: {rejected}")


# --------------------------------------------------------------------------- #
# tolerance
# --------------------------------------------------------------------------- #


def test_position_tolerance_is_float_noise():
    """WPS moves 0.0% of shapes on open-and-save across all ten decks, so
    there is no measured noise for a wider band to absorb.  A previous batch
    set the tolerance to half the actual displacement, which is fitting the
    ruler to the answer."""
    assert C.POS_TOL == 9144                              # 0.01 in


def test_a_shape_half_an_inch_out_is_not_in_position():
    gt = _inv(_shape("a"))
    moved = _inv(_shape("a"))
    moved["slides"][0]["shapes"][0]["bbox"]["cx"] += C.EMU_PER_INCH // 2
    component = _component("move", {"box": [0, 0, 900000, 400000]})
    result = C.score(_plan(component), moved, gt, moved)
    assert result["components"][0]["raw"] == 0.0


def test_group_children_are_compared_in_slide_coordinates():
    """A component that compares a group child's *local* EMU against slide EMU
    makes natural placement score 0 and makes placing the shape inside the
    group free.  `inventory._absolute` composes the group matrix, so both
    sides are already slide-absolute — this pins that they stay that way."""
    inside = _shape("a")
    inside["_path"] = "3/0"
    inside["group"] = "name:Group 3"
    gt = _inv(_shape("g", kind="group", name="Group 3", sid=3), inside)
    loose = copy.deepcopy(gt)
    loose["slides"][0]["shapes"][1]["_path"] = "4"
    loose["slides"][0]["shapes"][1]["group"] = None
    component = _component("move", {"box": [0, 0, 900000, 400000]}, path="3/0")
    result = C.score(_plan(component), loose, gt, gt)
    assert result["components"][0]["raw"] == 1.0


# --------------------------------------------------------------------------- #
# never require the absence of an attribute
# --------------------------------------------------------------------------- #


def test_an_inherited_property_is_not_scored_by_demanding_its_absence():
    """A model restored five mangled titles correctly and scored 0/5 for
    writing black *explicitly* where the answer inherited it.  No colour
    picker in any application can produce "no colour attribute", so a
    component that needs one is unsatisfiable, and unsatisfiable is worse than
    missing."""
    gt = _inv(_shape("Title"))
    gt["slides"][0]["shapes"][0]["text"] = {
        "paragraphs": [{"t": "Title", "runs": [{"t": "Title"}]}]}
    with pytest.raises(C.Unscorable):
        C._facet_run_props(gt["slides"][0]["shapes"][0], None, ("color",))


def test_a_property_the_answer_states_is_still_scored():
    gt = _inv(_shape("Title"))
    gt["slides"][0]["shapes"][0]["text"] = {
        "paragraphs": [{"t": "Title", "runs": [{"t": "Title", "color": "srgb:FF0000"}]}]}
    other = copy.deepcopy(gt)
    other["slides"][0]["shapes"][0]["text"]["paragraphs"][0]["runs"][0]["color"] = \
        "srgb:00FF00"
    shape = gt["slides"][0]["shapes"][0]
    assert C._facet_run_props(shape, shape, ("color",))[0] == 1.0
    assert C._facet_run_props(shape, other["slides"][0]["shapes"][0],
                              ("color",))[0] == 0.0


@pytest.mark.corpus
def test_an_unsatisfiable_component_is_dropped_not_left_to_punish():
    """deck0004 recolours three bodies whose ground truth carries no explicit
    colour at all.  Those six components cannot be passed by anyone, so they
    are removed from the plan and named rather than left in to take marks off
    work that was done right.

    (The deck has since been repaired so that `d5` keeps three `recolor`
    components and is no longer emptied by the drop, which is why the
    "no scoreable component" rejection this used to assert is gone.  What the
    drop costs `d5` is asserted in
    `test_a_dropped_components_weight_is_forfeited_not_paid_to_its_siblings`.)
    """
    plan = C.build_plan(WORK / "deck0004", write=False)
    dropped = {u["id"] for u in plan["unscoreable"]}
    assert len(dropped) == 6
    assert all(u["op"] == "set_font" for u in plan["unscoreable"])
    assert not (dropped & {c["id"] for c in plan["components"]})
    assert all(u["deg"] == "d5" for u in plan["unscoreable"])


@pytest.mark.corpus
def test_a_dropped_components_weight_is_forfeited_not_paid_to_its_siblings():
    """The half of the drop that was wrong.  deck0004's `d5` declares nine
    components, six of which the ground truth itself cannot satisfy (it
    inherits the fonts rather than stating them).  They were removed — right —
    and then the degradation's share was divided among the *survivors*, so
    **an agent that fixed the three fills and none of the six fonts scored
    100% of d5**.  Work nobody can earn must not become free marks: the share
    is scaled by the fraction that survives and the rest falls to the other
    degradations, which are work that is still asked for and still scored.

    It cannot instead be left unreachable — `score_task` requires the ground
    truth to total exactly 1.000, and a plan with a hole in it does not.
    """
    plan = C.build_plan(WORK / "deck0004", write=False)
    d5 = next(d for d in plan["degradations"] if d["id"] == "d5")
    assert d5["components_unscoreable"] == 6
    assert len(d5["components"]) == 3
    assert d5["share_forfeited"] == pytest.approx(6 / 9)
    others = [d for d in plan["degradations"] if d["id"] != "d5"]
    steps = _steps_used(plan)
    # d5 is paid a third of what its step count alone would have earned it,
    # and the forfeited two thirds went to the other four degradations.
    unforfeited = d5["weight"] / (1.0 - d5["share_forfeited"])
    assert unforfeited / steps["d5"] == pytest.approx(
        others[0]["weight"] / steps[others[0]["id"]], rel=1e-6)
    assert sum(d["weight"] for d in plan["degradations"]) == pytest.approx(1.0)


@pytest.mark.corpus
def test_a_composite_the_answer_cannot_disambiguate_never_reaches_a_score():
    """A component with no `gt_path` — SmartArt, charts — is resolved by its
    data part, and a slide the *answer* holds two of makes the resolution
    ambiguous.  Zero would be the wrong verdict for that: it is
    indistinguishable from an agent that did nothing, which is the failure this
    whole file is arranged against.

    It never gets as far as a zero.  `build_plan` runs every component against
    the ground truth before it weighs anything, and one that cannot pass its
    own answer is removed from the plan and named in `unscoreable` — so the
    surviving weights are computed over the survivors alone.  On deck0007 the
    SmartArt is the only work `d4` asks for, so removing it leaves a
    degradation nobody scores, and the plan is refused outright rather than
    shipped with a component that pays 0 for everybody."""
    def ambiguous(slide, data_part):
        raise C.Unscorable("cannot identify the SmartArt (2 on the slide)")

    plan = C.build_plan(WORK / "deck0007", write=False)
    assert [c["op"] for c in plan["components"]].count("smartart_drop_nodes") == 1
    assert not plan["rejected"]

    original = C._find_smartart
    C._find_smartart = ambiguous
    try:
        refused = C.build_plan(WORK / "deck0007", write=False)
    finally:
        C._find_smartart = original

    assert [u["op"] for u in refused["unscoreable"]] == ["smartart_drop_nodes"]
    assert refused["unscoreable"][0]["gt_scores"] == 0.0
    assert "cannot identify the SmartArt" in refused["unscoreable"][0]["why"]
    assert all(c["op"] != "smartart_drop_nodes" for c in refused["components"])
    assert abs(sum(c["weight"] for c in refused["components"]) - 1.0) < 1e-9
    assert any("no scoreable component" in reason and "d4" in reason
               for reason in refused["rejected"])


@pytest.mark.corpus
def test_a_second_smartart_the_agent_adds_does_not_unscore_the_component():
    """The other half of it, and the reason the drop above is enough: the
    resolution is done against the **ground truth's** slide, which no agent can
    change, using the data part `degrade_exec` recorded out of the same
    package.  A solver that legitimately adds a second SmartArt to the page
    cannot make the component unidentifiable — it is paired by shape, not
    re-resolved by part."""
    plan, gt, init = _deck("deck0007")
    component = next(c for c in plan["components"]
                     if c["op"] == "smartart_drop_nodes")
    page = component["slide"]

    candidate = copy.deepcopy(gt)
    shapes = candidate["slides"][page]["shapes"]
    theirs = copy.deepcopy(next(s for s in shapes if s["kind"] == "smartart"))
    theirs["_path"], theirs["_id"], theirs["_name"] = "zz9", 9001, "Diagram 99"
    theirs["bbox"] = dict(theirs["bbox"], cx=theirs["bbox"]["cx"] + 2000000)
    theirs["diagram"] = dict(theirs["diagram"], _data_part="ppt/diagrams/data99.xml")
    shapes.append(theirs)

    result = C.score(plan, candidate, gt, init)
    scored = next(c for c in result["components"] if c["id"] == component["id"])
    assert scored["raw"] == 1.0
    assert "unscorable" not in scored["why"]


def test_table_cell_runs_are_visible_to_the_run_comparator():
    """`set_font` reaches every `a:rPr` in the shape, and a table cell's runs
    live in `a:tc/a:txBody` — reading only `text.paragraphs` reported "no
    formatted runs" for the two deck0009 components that de-bold two codes
    inside one table."""
    shape = {"kind": "table", "table": {"n_rows": 1, "n_cols": 1, "rows": [
        {"h": 0, "cells": [{"text": "X", "runs": [{"t": "X", "b": "1"}]}]}]}}
    assert C._run_groups(shape) == {"c0,0": [{"t": "X", "b": "1"}]}


# --------------------------------------------------------------------------- #
# weights
# --------------------------------------------------------------------------- #


def _steps_used(plan):
    """The step count a plan says its weights came from, per degradation."""
    key = ("est_steps_measured" if plan["weight_source"] == "steps_measured"
           else "est_steps")
    return {d["id"]: d[key] for d in plan["degradations"]}


def _kept(plan):
    """The share of each degradation's work that survived, exactly.

    Not `1.0 - share_forfeited`.  That field is rounded to six decimals in
    `plan.json`, so a degradation that forfeited two thirds carries 0.666667
    rather than 0.6666…, and reconstructing the weights through it drifts by
    ~1e-8 — an order of magnitude past the 1e-9 the weights themselves are
    stored to.  deck0004 is the specimen, and the tolerance was never the
    problem: `components` and `components_unscoreable` carry the same fact as
    two integers, and integers do not round.
    """
    return {d["id"]: (len(d["components"])
                      / float(len(d["components"]) + d["components_unscoreable"])
                      if d["components"] else 0.0)
            for d in plan["degradations"]}


@pytest.mark.corpus
@pytest.mark.parametrize("name", ACCEPTED + ("deck0002",))
def test_weight_follows_the_steps_not_the_number_of_entries(name):
    """A batch weighted by delta entry put 53% of one rubric on its cheapest
    sub-goal: recolouring 3 of 28 label groups was worth 0.40 while the
    headline rebuild was worth 0.95%.  A degradation's weight is *split*
    among its entries, never multiplied by them.

    Stated as the exact proportionality rather than as an ordering, because
    there are now two ways a weight moves away from the raw step count and both
    have to stay visible: the steps may be the solvability probe's measurement
    rather than the proposer's declaration, and a degradation forfeits the
    share of its work that turned out to be unscoreable.
    """
    plan, _gt, _init = _deck(name)
    steps = _steps_used(plan)
    weights = {d["id"]: d["weight"] for d in plan["degradations"]}
    kept = _kept(plan)
    assert sum(weights.values()) == pytest.approx(1.0)
    want = {d: steps[d] * kept[d] for d in steps}
    scale = sum(want.values())
    for deg in steps:
        assert weights[deg] == pytest.approx(want[deg] / scale, abs=1e-9), deg


@pytest.mark.corpus
@pytest.mark.parametrize("name", ACCEPTED + ("deck0002",))
def test_the_biggest_job_is_never_worth_less_than_the_smallest(name):
    plan, _gt, _init = _deck(name)
    steps = _steps_used(plan)
    order = sorted(plan["degradations"], key=lambda d: steps[d["id"]])
    assert order[0]["weight"] <= order[-1]["weight"]


@pytest.mark.corpus
@pytest.mark.parametrize("name", ACCEPTED + ("deck0002",))
def test_reward_per_step_is_flat_where_the_steps_were_measured(name):
    """The defect this pins shut: deck0006's cheapest job — one bitmap pasted
    onto one page, which the solvability probe measures at ~15 GUI steps —
    carried **0.3158** of the reward while its most expensive (twenty shapes,
    ~140 steps) carried **0.2368**.  12.4x more reward per step for the trivial
    one, which points an agent that maximises reward per step at exactly the
    work these tasks are not for.  deck0007 ran the same way, milder.

    Only a deck whose probe actually measured the work can be checked, so this
    is a property of those, not of every plan.  Degradations that forfeited
    part of their share are excluded: for them the departure from
    proportionality is the other fix, and it is asserted above.
    """
    plan, _gt, _init = _deck(name)
    if plan["weight_source"] != "steps_measured":
        pytest.skip("nothing in this deck's artefacts measured its step counts")
    steps = _steps_used(plan)
    per_step = [d["weight"] / steps[d["id"]] for d in plan["degradations"]
                if not d["share_forfeited"]]
    assert max(per_step) / min(per_step) == pytest.approx(1.0, abs=1e-6)


@pytest.mark.corpus
def test_component_weights_sum_to_one():
    for name in ACCEPTED:
        plan, _gt, _init = _deck(name)
        assert sum(c["weight"] for c in plan["components"]) == pytest.approx(1.0)


@pytest.mark.corpus
def test_the_measured_step_count_is_preferred_to_the_declared_one():
    """`est_steps` is the proposer's declaration and nothing validated it.  The
    solvability probe measures the same work independently and disagrees per
    degradation by up to 8x — and only the *totals* were ever compared, loosely
    ("agrees with the declared 285 within a band"), which is why it survived:
    the per-degradation errors cancel in the sum.

    The measurement is prose, and this is what makes reading it safe: a figure
    is taken only where it is marked as one, the parse is discarded unless
    every declared degradation is matched, and the total has to agree with the
    probe's own `est_steps_measured`, which is written by the same agent in the
    same file and is not derived from the breakdown.
    """
    plan = C.build_plan(WORK / "deck0006", write=False)
    check = plan["weight_check"]
    assert plan["weight_source"] == "steps_measured"
    assert check["measured"] == {"d1": 15, "d2": 140, "d3": 60,
                                 "d4": 45, "d5": 50}
    assert check["declared"]["d1"] == 120 and check["worst"] == 8.0
    assert "310" in check["measured_from"]
    weights = {d["id"]: d["weight"] for d in plan["degradations"]}
    assert weights["d2"] > weights["d1"] * 5


@pytest.mark.corpus
def test_a_number_that_could_be_a_slide_is_not_read_as_a_step_count():
    """"d1 rebuild row on slide 12 ~55" — the first number after `d1` is 12.
    A parse that took bare numbers would have weighted deck0004's biggest job
    at twelve steps."""
    steps, why, ok = C._measured_steps(WORK / "deck0004", ["d1", "d2", "d3",
                                                          "d4", "d5"])
    assert ok and steps["d1"] == 55, why


def test_a_breakdown_that_does_not_add_up_is_not_used(tmp_path):
    """The self-check.  A parse that half-worked would be worse than no parse:
    it would move reward onto whichever degradation the regex happened to
    read."""
    (tmp_path / "solvability.json").write_text(json.dumps({
        "est_steps_measured": 300,
        "notes": ["Step estimate: d1 ~10; d2 ~10."]}))
    steps, why, ok = C._measured_steps(tmp_path, ["d1", "d2"])
    assert not ok and "does not agree" in why
    (tmp_path / "solvability.json").write_text(json.dumps({
        "est_steps_measured": 300,
        "notes": ["Step estimate: d1 ~100; d2 ~180."]}))
    steps, why, ok = C._measured_steps(tmp_path, ["d1", "d2"])
    assert ok and steps == {"d1": 100, "d2": 180}
    # incomplete: d3 never appears, so nothing is weighted by it
    steps, why, ok = C._measured_steps(tmp_path, ["d1", "d2", "d3"])
    assert not ok and steps == {"d1": 100, "d2": 180}


def test_a_plan_may_not_weight_by_a_number_a_measurement_contradicts(tmp_path):
    """The backstop for what the preference above cannot fix: a measurement
    exists, it says the declaration is out by more than a factor of
    `STEP_DISAGREEMENT`, and it is too partial to weight by — so the plan would
    distribute reward by a number the pipeline has already contradicted."""
    root = tmp_path / "deck9999"
    root.mkdir()
    (root / "solvability.json").write_text(json.dumps({
        "degradations": [{"id": "d1", "est_steps_measured": 10}]}))
    steps, why, ok = C._measured_steps(root, ["d1", "d2"])
    assert not ok and steps == {"d1": 10} and "incomplete" in why


@pytest.mark.corpus
def test_a_plan_that_scores_work_the_instruction_excuses_is_refused():
    """deck0002's instruction ends *"you do not need to re-create any
    animation, only the artwork"* — and its plan scores three `strip_animation`
    components worth **0.0801** between them.  The candidate that does exactly
    what it was told scored **0.919901**: 8% of that task was unreachable by
    obedience, and neither existing gate could see it.  The coherence probe
    scores the ground truth at 1.0 because `source.pptx` still has its
    animations, and nothing anywhere compared the prose to the plan.

    It refuses rather than zeroing the three components, because which of the
    two is wrong is not decidable here — on this deck the sentence is right and
    the components seal a leak another degradation opened, but on a deck whose
    animation genuinely is the job the components are right and the sentence
    has to go.
    """
    plan = C.build_plan(WORK / "deck0002", write=False)
    refusal = [r for r in plan["rejected"] if "excuses" in r]
    assert len(refusal) == 1, plan["rejected"]
    assert "c008" in refusal[0] and "animation" in refusal[0]
    assert [c["id"] for c in plan["components"]
            if c["op"] == "strip_animation"] == ["c008", "c021", "c027"]


@pytest.mark.corpus
@pytest.mark.parametrize("name", ACCEPTED)
def test_no_other_deck_is_refused_for_a_sentence_it_did_not_write(name):
    """The false-positive control.  A check on prose that fires on nine decks
    is a check nobody can act on."""
    plan, _gt, _init = _deck(name)
    assert [r for r in plan["rejected"] if "excuses" in r] == []


# --------------------------------------------------------------------------- #
# traceability
# --------------------------------------------------------------------------- #


@pytest.mark.corpus
@pytest.mark.parametrize("name", ACCEPTED)
def test_every_component_names_a_degradation_the_task_declares(name):
    plan, _gt, _init = _deck(name)
    declared = {d["id"] for d in plan["degradations"]}
    assert {c["deg"] for c in plan["components"]} <= declared


@pytest.mark.corpus
@pytest.mark.parametrize("name", ACCEPTED)
def test_every_degradation_owns_a_component(name):
    plan, _gt, _init = _deck(name)
    owned = {c["deg"] for c in plan["components"]}
    assert {d["id"] for d in plan["degradations"]} <= owned


@pytest.mark.corpus
def test_a_delta_without_deg_is_refused_on_whichever_deck_has_one():
    """A delta that predates the `deg` field cannot attribute anything in it
    to anything the task asks for.  That is a rejection, not something to work
    around.

    deck0001 was the specimen and a repair has since given its delta the
    field, so this asks the question of whichever deck still has the problem
    and says so when none does.  The rule is pinned against a deck the suite
    owns, in `test_a_frozen_delta_without_deg_is_refused`.
    """
    unattributed = {}
    for deck in DECKS:
        plan = C.build_plan(deck, write=False)
        if any(not c.get("deg") for c in plan["components"]):
            unattributed[deck.name] = plan["rejected"]
    if not unattributed:
        pytest.skip("every delta in work/ carries `deg` now — the frozen form "
                    "is `test_a_frozen_delta_without_deg_is_refused`")
    for name, rejected in unattributed.items():
        assert any("no `deg`" in reason for reason in rejected), (
            f"{name} scores components nothing attributes and its plan does "
            f"not refuse it: {rejected}")


@pytest.mark.corpus
def test_a_rejected_plan_cannot_be_scored_above_zero():
    plan, gt, init = _deck("deck0002")
    plan["rejected"] = ["invented"]
    result = C.score(plan, gt, gt, init)
    assert result["failed_gate"] == "plan_accepted"
    assert result["score"] == 0.0
    assert result["components"], "the breakdown survives a failed gate"


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #


def test_a_stock_text_box_earns_nothing_for_being_roughly_the_right_size():
    """A stock `Emu(1000000) x Emu(300000)` text box collected 0.4 twice on a
    previous batch by falling inside a size tolerance at entirely the wrong
    position.  Size is not identity."""
    gt_shape = _shape("Real content", kind="textbox")
    gt = _inv(gt_shape)
    stock = _shape("", kind="textbox", name="TextBox 9", sid=99)
    stock["bbox"]["cx"] += 4 * C.EMU_PER_INCH
    stock["bbox"]["w"], stock["bbox"]["h"] = 900000, 400000
    component = _component("delete", {"box": [0, 0, 900000, 400000]})
    result = C.score(_plan(component), _inv(stock), gt, _inv())
    assert result["components"][0]["raw"] == 0.0


def _picture(name="Picture 4", sid=3, blob="aaaa", box=None):
    shape = _shape("", kind="picture", name=name, sid=sid, box=box)
    w, h = shape["bbox"]["w"], shape["bbox"]["h"]
    shape["keys"] = [f"pic:{blob}", f"name:{name}",
                     f"geo:picture:{round(w / 91440)}x{round(h / 91440)}",
                     "kind:picture"]
    shape["key"] = shape["keys"][0] + "#0"
    shape["picture"] = {"blob": blob}
    return shape


def _restored(candidate, gt_shape):
    """What one `delete` component scores for `candidate`, floor included."""
    component = _component("delete", {"box": [0, 0, 900000, 400000]})
    result = C.score(_plan(component), _inv(candidate), _inv(gt_shape), _inv())
    return result["components"][0]


def test_a_restoration_that_is_wrong_in_every_respect_scores_nearly_nothing():
    """`wrong_params` put every deleted shape back 0.75 in out of place, 1.3x
    too large, repainted and re-worded, and collected **0.667** a time —
    0.28–0.59 on eight of ten decks.  `present` was 3 of 9 and the picture blob
    another 3, and neither of them can be wrong once the shape is there: the
    blob is the key the shape was paired on.  Pasting something roughly there
    has to be worth less than restoring the thing, or it is the move a training
    run finds first."""
    gt_shape = _picture()
    wrong = _picture()
    wrong["bbox"]["cx"] += int(0.75 * C.EMU_PER_INCH)
    wrong["bbox"]["cy"] += int(0.75 * C.EMU_PER_INCH)
    wrong["bbox"]["w"] = int(wrong["bbox"]["w"] * 1.3)
    wrong["bbox"]["h"] = int(wrong["bbox"]["h"] * 1.3)
    assert _restored(wrong, gt_shape)["raw"] == 0.0


def test_a_restoration_still_earns_partial_credit_for_partial_work():
    """The failure mode on the other side, and the worse one: a rollout of the
    previous batch recorded 0.0 for a model that had done real work on three of
    four tasks.  Right thing, wrong size still pays; right size, wrong place
    still pays; neither is zero."""
    gt_shape = _picture()
    sized_wrong = _picture()
    sized_wrong["bbox"]["w"] = int(sized_wrong["bbox"]["w"] * 1.3)
    placed_wrong = _picture()
    placed_wrong["bbox"]["cx"] += int(0.75 * C.EMU_PER_INCH)
    assert _restored(sized_wrong, gt_shape)["raw"] == pytest.approx(2 / 3)
    assert _restored(placed_wrong, gt_shape)["raw"] == pytest.approx(1 / 3)
    assert _restored(_picture(), gt_shape)["raw"] == 1.0


def test_half_the_words_back_in_the_right_place_is_worth_about_half():
    gt_shape = _shape("alpha\nbeta")
    gt_shape["text"] = {"paragraphs": [{"t": "alpha"}, {"t": "beta"}]}
    half = copy.deepcopy(gt_shape)
    half["text"] = {"paragraphs": [{"t": "alpha"}]}
    assert _restored(half, gt_shape)["raw"] == pytest.approx(0.5)


def test_renaming_a_survivor_to_a_deleted_shapes_name_earns_nothing():
    """`rename_only` renames shapes and changes nothing else.  It paid
    0.005–0.124 across nine decks and broke the 0.05 threshold on three,
    because a name made the matcher pair a deleted picture with a survivor
    five inches away and `present` then bought half the component."""
    gt_shape = _picture()
    liar = _shape("some other content", name="Picture 4", sid=9)
    liar["bbox"]["cx"] += 5 * C.EMU_PER_INCH
    assert _restored(liar, gt_shape)["raw"] == 0.0


def test_a_name_cannot_pair_shapes_that_are_nowhere_near_each_other():
    here = _shape("", name="Rectangle 1", sid=1)
    there = _shape("", name="Rectangle 1", sid=2)
    there["bbox"]["cx"] += 5 * C.EMU_PER_INCH
    assert C.pair_slide([here], [there])["0"] is None
    near = _shape("", name="Rectangle 1", sid=3)
    near["bbox"]["cx"] += C.EMU_PER_INCH // 4
    assert C.pair_slide([here], [near])["0"] is not None


def test_a_moved_shape_is_still_paired_by_a_strong_key():
    """The distance rule is about weak keys only.  A shape a `move` step
    displaced by an inch has to keep pairing with its original, or the
    component that grades the move has nothing to grade."""
    gt_shape = _shape("Card A")
    moved = _shape("Card A")
    moved["bbox"]["cx"] += 5 * C.EMU_PER_INCH
    assert C.pair_slide([gt_shape], [moved])["0"] is not None


def test_the_deleted_shape_does_not_steal_a_surviving_twin(tmp_path):
    """`inventory.match_shapes` walks in document order, so a deleted picture
    considered first falls through to `geo:` and takes its neighbour's
    counterpart — after which the *neighbour* is reported missing and the
    component is scored on the wrong shape."""
    gone = _shape("", kind="picture", name="Picture 1", sid=1)
    gone["picture"] = {"blob": "aaaa"}
    gone["keys"] = ["pic:aaaa", "name:Picture 1", "geo:picture:10x4",
                    "kind:picture"]
    kept = _shape("", kind="picture", name="Picture 2", sid=2)
    kept["_path"] = "1"
    kept["picture"] = {"blob": "bbbb"}
    kept["keys"] = ["pic:bbbb", "name:Picture 2", "geo:picture:10x4",
                    "kind:picture"]
    pairs = C.pair_slide([gone, kept], [copy.deepcopy(kept)])
    assert pairs["0"] is None
    assert pairs["1"] is not None


# --------------------------------------------------------------------------- #
# cheats are zero; over-eagerness is a penalty
# --------------------------------------------------------------------------- #


def test_a_full_page_overlay_is_a_zero():
    gt = _inv(_shape("content"))
    cheat = _inv(_shape("content"),
                 {**_shape("", kind="picture", name="Cover", sid=50),
                  "picture": {"blob": "zzz"},
                  "bbox": {"cx": 6096000, "cy": 3429000, "w": 12192000,
                           "h": 6858000, "rot": 0.0, "flip": False},
                  "keys": ["pic:zzz", "name:Cover", "geo:picture:133x75",
                           "kind:picture"]})
    component = _component("move", {"box": [0, 0, 900000, 400000]})
    result = C.score(_plan(component), cheat, gt, gt)
    assert result["failed_gate"] == "no_full_page_overlay"
    assert result["score"] == 0.0


@pytest.mark.corpus
def test_touching_a_page_nobody_asked_about_is_a_penalty_not_a_zero():
    """A model that added a logo to two pages whose ground truth has none had
    done 43% of the work; a hard gate recorded 0.0, which is indistinguishable
    from doing nothing and destroys the training signal."""
    plan, gt, init = _specimen()
    keen = copy.deepcopy(gt)
    spare = next(i for i in range(len(gt["slides"]))
                 if i not in set(plan["damage"]["slides"]))
    keen["slides"][spare]["shapes"].append(_shape("helpfully added"))
    result = C.score(plan, keen, gt, init)
    assert result["failed_gate"] is None
    assert 0.0 < result["score"] < 1.0
    assert "untouched_pages_unchanged" in result["scope_violations"]


def _untouched_page(plan, inv):
    damaged = set(plan["damage"]["slides"])
    return next(i for i, page in enumerate(inv["slides"])
                if i not in damaged
                and any(_norm_runs(s) for s in page["shapes"]))


def _norm_runs(shape):
    return [p for p in ((shape.get("text") or {}).get("paragraphs") or [])
            if p.get("runs")]


@pytest.mark.corpus
def test_what_the_application_writes_by_itself_is_not_a_scope_violation():
    """`roundtrip_identity`, docs/design/reward.md §5's cheapest probe: the ground truth
    put through the grading application must still score 1.000.  It scored
    0.700–0.850 on nine of ten decks, every point of it the capped untouched-
    page penalty and not one of it damage — WPS materialises `a:endParaRPr` in
    full on paragraphs that had none, rounds `marL` from 381000 to 380990, and
    on deck0003 invented a `fade` transition on a page that had none.  Noise is
    subtracted, never tolerated: a band wide enough to hold it is a band open
    to everybody."""
    plan, gt, init = _specimen()
    index = _untouched_page(plan, gt)
    for name, mutate in (
            ("endParaRPr", lambda p: p["runs"].append(
                {"t": "", "end": True, "b": "1", "sz": 3200, "font": "Calibri"})),
            ("marL", lambda p: p.update(marL=380990)),
    ):
        rewritten = copy.deepcopy(gt)
        page = rewritten["slides"][index]
        para = next(p for s in page["shapes"] for p in _norm_runs(s))
        mutate(para)
        result = C.score(plan, rewritten, gt, init)
        assert result["scope_violations"] == {}, name
        assert result["score"] == pytest.approx(1.0), name
    invented = copy.deepcopy(gt)
    invented["slides"][index]["transition"] = {
        "type": "fade", "detail": None, "speed": "med", "duration_ms": 700,
        "advance_ms": None, "on_click": True}
    assert C.score(plan, invented, gt, init)["score"] == pytest.approx(1.0)


@pytest.mark.corpus
def test_a_real_edit_to_an_untouched_page_is_still_caught():
    """The negative control for the one above: narrowing what the gate looks at
    is only safe while it still sees the thing it was written for."""
    plan, gt, init = _specimen()
    index = _untouched_page(plan, gt)
    for name, mutate in (
            ("moved", lambda page: page["shapes"][0]["bbox"].update(
                cx=page["shapes"][0]["bbox"]["cx"] + C.EMU_PER_INCH)),
            ("added", lambda page: page["shapes"].append(_shape("brand new"))),
            ("deleted", lambda page: page["shapes"].pop(0)),
            ("re-worded", lambda page: next(
                p for s in page["shapes"] for p in _norm_runs(s)).update(t="ZZZ")),
    ):
        meddled = copy.deepcopy(gt)
        mutate(meddled["slides"][index])
        result = C.score(plan, meddled, gt, init)
        assert "untouched_pages_unchanged" in result["scope_violations"], name
        assert 0.0 < result["score"] < 1.0, name


def _picture_pages(plan, inv):
    """The untouched pages of `inv` that draw at least one picture."""
    damaged = set(plan["damage"]["slides"])
    return [i for i, page in enumerate(inv["slides"])
            if i not in damaged
            and any((s.get("picture") or {}).get("blob") for s in page["shapes"])]


def _re_encode(inv, pages):
    """Every picture on `pages` re-compressed: new bytes, everything else the
    same.  The identity moves with the bytes — `inventory._keys` gives a
    picture `pic:<digest>` as its strongest key — which is the whole point."""
    out = copy.deepcopy(inv)
    media = list(out["package"]["media"])
    for index in pages:
        for shape in out["slides"][index]["shapes"]:
            blob = (shape.get("picture") or {}).get("blob")
            if not blob:
                continue
            fresh = "reenc" + blob[5:]
            shape["picture"]["blob"] = fresh
            shape["keys"] = [k.replace(blob, fresh) for k in shape["keys"]]
            shape["key"] = shape["key"].replace(blob, fresh)
            media = [fresh if b == blob else b for b in media]
    out["package"] = dict(out["package"], media=sorted(media))
    return out


@pytest.mark.corpus
def test_an_image_the_application_re_encoded_does_not_read_as_a_deleted_shape():
    """`_page_facts` deliberately records *that* a shape draws an image and
    never which bytes, "because the blob is the application's to change" — and
    then filed every fact it recorded under `shape["key"]`, which for a picture
    **is** that blob (`shapes[pic:279bd563df27c9f8#0].bbox.cx`).  Re-encoding
    moved the address rather than the value, one untouched picture read as a
    deletion plus an addition, and thirteen such pages on deck0003 and six on
    deck0008 took a perfect deck to 0.393 and 0.450 through the 0.30 cap.

    LibreOffice re-encodes every image it saves, and a rollout has already been
    seen with the `.pptx` handler bound to Impress.
    """
    # every accepted deck that has enough illustrated pages to reach the cap,
    # rather than two named ones: deck0008 was one of the two and cannot be
    # scored at all (see `REFUSED`), which turned a defect in a task into a
    # failure of this assertion.
    checked = []
    for name in ACCEPTED:
        plan, gt, init = _deck(name)
        pages = _picture_pages(plan, gt)
        # three untouched pages is already the 0.30 cap at 0.10 apiece, so
        # this is the whole penalty, not a slice of it
        if len(pages) < 3:
            continue
        checked.append(name)
        result = C.score(plan, _re_encode(gt, pages), gt, init)
        assert result["scope_violations"] == {}, (name, result["scope_violations"])
        assert result["score"] == pytest.approx(1.0), name
    assert checked, ("no accepted deck has three untouched illustrated pages, "
                     "so the corpus cannot show this any more — the frozen "
                     "form is "
                     "`test_an_image_the_application_re_encoded_on_a_frozen_"
                     "deck_is_not_a_deletion`")


@pytest.mark.corpus
def test_a_real_edit_to_a_picture_on_an_untouched_page_is_still_caught():
    """The negative control for the one above.  Addressing a shape by the
    pairing instead of by its blob must not make the shape invisible: what it
    drops is the *bytes*, and every other fact about the picture — that it is
    there at all, where it is, how big it is — is still compared."""
    plan, gt, init = _deck("deck0003")
    index = _picture_pages(plan, gt)[0]

    def a_picture(page):
        return next(s for s in page["shapes"]
                    if (s.get("picture") or {}).get("blob"))

    for name, mutate in (
            ("moved", lambda page: a_picture(page)["bbox"].update(
                cx=a_picture(page)["bbox"]["cx"] + C.EMU_PER_INCH)),
            ("resized", lambda page: a_picture(page)["bbox"].update(
                w=a_picture(page)["bbox"]["w"] * 2)),
            ("deleted", lambda page: page["shapes"].remove(a_picture(page))),
            ("hidden", lambda page: a_picture(page).update(hidden=True)),
    ):
        meddled = copy.deepcopy(gt)
        mutate(meddled["slides"][index])
        result = C.score(plan, meddled, gt, init)
        assert "untouched_pages_unchanged" in result["scope_violations"], name
        assert 0.0 < result["score"] < 1.0, name


def test_no_amount_of_over_eagerness_costs_more_than_half():
    """The penalty is a fraction of what was earned, so correct partial work
    always outranks doing nothing."""
    assert C.PENALTY_CAP <= 0.5
    assert sum(cap for _rate, cap in C.SCOPE_RATES.values()) >= C.PENALTY_CAP


def _native_table():
    table = _shape("", kind="table", name="Table 1", sid=4)
    table["table"] = {"n_rows": 1, "n_cols": 2, "rows": [
        {"h": 0, "cells": [{"text": "left"}, {"text": "right"}]}]}
    table["keys"] = ["table", "name:Table 1", "geo:table:10x4", "kind:table"]
    return table


def test_a_hand_rebuilt_native_object_is_the_work_not_a_cheat():
    """A native-objects gate zeroed a model for rebuilding a SmartArt by hand
    in an evaluator whose own diagram component was at that moment awarding
    0.7 for that same rebuild.  A gate must never overrule a component that is
    handing out credit: the cheat is a *picture* of the object, not an
    equivalent built out of ordinary shapes."""
    table = _native_table()
    gt = _inv(table)
    broken = _inv(table)
    hand = _inv(_shape("left", name="Rect A", sid=11),
                _shape("right", name="Rect B", sid=12))
    component = _component("clear_table_cells",
                           {"cleared": [{"at": [0, 0]}],
                            "box": [550000, 800000, 900000, 400000]})
    result = C.score(_plan(component), hand, gt, broken)
    assert result["failed_gate"] is None


#: Operators for which a composite re-made out of ordinary shapes is **not**
#: the same answer, so the component is right to pay nothing for it.
#:
#: `table_drop_rows` asks for rows back *in a table*; five text boxes laid out
#: where the rows were is not a table, and paying for it would make "type the
#: values next to each other" cheaper than restoring the object.  That is the
#: same priced decision as the picture-bytes one, and it is recorded here so
#: that the assertion below can be universal about everything else instead of
#: being about two decks somebody chose.
REBUILD_IS_NOT_EQUIVALENT = {"table_drop_rows": "no table"}


@pytest.mark.corpus
@pytest.mark.parametrize("name", ACCEPTED)
def test_the_hand_rebuild_of_a_real_deck_is_not_gated(name):
    """The universal half, and the one the defect was in: a gate must never
    overrule a component that is handing out credit.  True of every accepted
    deck, not of the one it was found on."""
    plan, gt, init = _deck(name)
    result = C.score(plan, C._state_rebuilt(plan, gt), gt, init)
    assert result["failed_gate"] is None
    assert result["score"] > 0.0


@pytest.mark.corpus
def test_the_hand_rebuild_of_a_real_deck_is_also_paid_for():
    """No gate fired on deck0007's hand rebuild and it still scored **0.4103**
    — on the one deck whose instruction asks for a rebuild in as many words.
    `_gate_native_not_flattened` had been narrowed on the stated grounds that
    "whether the rebuild is good enough is the component's judgement", and the
    component's judgement was 0: pairing is one-to-one, a rebuild is
    one-to-many, and a SmartArt redrawn as five boxes matched nothing at all.
    """
    for name in ACCEPTED:
        plan, gt, init = _deck(name)
        result = C.score(plan, C._state_rebuilt(plan, gt), gt, init)
        assert result["failed_gate"] is None, name
        for component in result["components"]:
            expected = REBUILD_IS_NOT_EQUIVALENT.get(component["op"])
            if expected is None:
                assert component["score"] == pytest.approx(1.0), (
                    name, component["id"], component["op"], component["why"])
            else:
                # not a pass: the exception is asserted too, so a comparator
                # that quietly started paying for a flattened table would go
                # red here rather than look like an improvement
                assert component["score"] == 0.0, (name, component["id"])
                assert expected in component["why"], (name, component["why"])


def _diagram_shape(nodes, box=None, path="0"):
    box = box or {"cx": 3000000, "cy": 2000000, "w": 2000000, "h": 1600000,
                  "rot": 0.0, "flip": False}
    shape = _shape("", box=box, kind="smartart", name="Diagram 9", sid=4)
    shape["_path"] = path
    shape["diagram"] = {"nodes": list(nodes), "_data_part": "d1.xml"}
    shape["keys"] = ["smartart", "name:Diagram 9", "kind:smartart"]
    shape["key"] = "smartart#0"
    return shape


def _box_at(text, cx, cy, path, sid):
    shape = _shape(text, box={"cx": cx, "cy": cy, "w": 600000, "h": 300000,
                              "rot": 0.0, "flip": False},
                   name=f"Rectangle {sid}", sid=sid)
    shape["_path"] = path
    return shape


NODES = ("alpha", "beta", "gamma", "delta")
#: the region `_diagram_shape` occupies, as the executor records it
DIAGRAM_BOX = [2000000, 1200000, 2000000, 1600000]


def _rebuild_case(*shapes):
    """gt holds one SmartArt; the broken file has lost it; `shapes` is the
    answer somebody built in its place."""
    gt = _inv(_diagram_shape(NODES))
    broken = _inv()
    component = _component("delete", {"box": DIAGRAM_BOX})
    return C.score(_plan(component), _inv(*shapes), gt, broken)


def _group_case(*candidate_shapes, gt_extra=(), member_text=("alpha", "beta")):
    """gt holds a two-member group; the broken file has lost the lot."""
    members = []
    for n, text in enumerate(member_text):
        member = _box_at(text, 2400000 + 700000 * n, 1600000, f"3/{n}", 20 + n)
        member["group"] = "3"
        members.append(member)
    group = _shape("", kind="group", name="Group 3", sid=3,
                   box={"cx": 2750000, "cy": 1600000, "w": 2000000,
                        "h": 1600000, "rot": 0.0, "flip": False})
    group["_path"] = "3"
    group["keys"] = ["name:Group 3", "geo:group:22x17", "kind:group"]
    group["key"] = "name:Group 3#0"
    gt = _inv(group, *members, *gt_extra)
    component = _component("delete", {"box": [1750000, 800000, 2000000, 1600000]},
                           path="3")
    return C.score(_plan(component), _inv(*candidate_shapes), gt, _inv())


def test_an_empty_group_of_the_right_size_in_the_right_place_scores_nothing():
    """The cheapest cheat in the corpus, and the one the user ranked above
    everything: **draw an empty group at the right coordinates.**

    `_cmp_restored_shape` drew its `what` facets from the ground-truth shape's
    own content, and a group has none — its content is its children — so the
    component fell through to a pure bounding-box test.  44 of the 229 `delete`
    components across the packaged decks were in that state, their ground-truth
    `why` string literally `position=1.00 · size=1.00`.  Scoring `input.pptx`
    plus one empty shape of the right kind at the right box per such component:
    **deck0003 0.4035** (two whole groups, one of them 26.3% of the deck on its
    own, its picture never compared), **deck0005 0.3906** (a sixteen-member
    group carrying that deck's largest declared job), deck0004 0.1889,
    deck0006 0.1875, deck0009 0.1279, deck0002 0.0685 — and `failed_gate` was
    `None` on every one of them.
    """
    hollow = _shape("", kind="group", name="Group 3", sid=99,
                    box={"cx": 2750000, "cy": 1600000, "w": 2000000,
                         "h": 1600000, "rot": 0.0, "flip": False})
    hollow["_path"] = "0"
    result = _group_case(hollow)
    assert result["score"] == 0.0, result["components"][0]["why"]
    # and no better than an absent shape, which is the rule
    assert _group_case()["score"] == 0.0


def test_a_groups_members_are_what_a_restored_group_is_scored_on():
    members = []
    for n, text in enumerate(("alpha", "beta")):
        member = _box_at(text, 2400000 + 700000 * n, 1600000, f"3/{n}", 20 + n)
        member["group"] = "3"
        members.append(member)
    group = _shape("", kind="group", name="Group 3", sid=3,
                   box={"cx": 2750000, "cy": 1600000, "w": 2000000,
                        "h": 1600000, "rot": 0.0, "flip": False})
    group["_path"] = "3"
    group["keys"] = ["name:Group 3", "geo:group:22x17", "kind:group"]
    assert _group_case(group, *members)["score"] == pytest.approx(1.0)
    assert _group_case(group, members[0])["score"] == pytest.approx(0.5)


def test_the_members_put_back_without_the_group_are_still_the_work():
    """The mirror image, and the `ungrouped` legitimate variant: a solver who
    restores every member but never re-groups them has done strictly more work
    than one who restores the group, and the gt group pairs with nothing.  That
    used to score 0 — `_composite_texts` has no words for a container — so the
    route that is harder was also the route that paid less."""
    members = []
    for n, text in enumerate(("alpha", "beta")):
        member = _box_at(text, 2400000 + 700000 * n, 1600000, f"3/{n}", 20 + n)
        members.append(member)                       # loose: no `group` key
    assert _group_case(*members)["score"] == pytest.approx(1.0)


def test_a_shape_with_no_content_is_still_not_scored_by_its_box_alone():
    """The same fall-through on the other kinds it reaches: the corpus's 38
    deleted connectors and 7 deleted autoshapes hold no picture, words, table,
    diagram, chart or fill either.  What they *are* — the preset geometry, the
    outline that draws them, the effect style — is compared instead.  None of
    those is `kind` or the box, on purpose: a facet the empty shape already
    satisfies would pay the cheat a share rather than deny it one."""
    line = _shape("", kind="connector", name="Straight Arrow 5", sid=5)
    line["geom"] = {"prst": "straightConnector1"}
    line["line"] = {"fill": "solid", "color": "srgb:FE6060", "dash": None,
                    "head": None, "tail": "triangle"}
    gt = _inv(line)
    component = _component("delete", {"box": [550000, 800000, 900000, 400000]})

    blank = _shape("", kind="connector", name="Straight Arrow 5", sid=9)
    assert C.score(_plan(component), _inv(blank), gt, _inv())["score"] == 0.0

    styled = copy.deepcopy(line)
    styled["_id"] = 9
    assert C.score(_plan(component), _inv(styled), gt,
                   _inv())["score"] == pytest.approx(1.0)

    # right preset, wrong colour: one of the two facets this shape offers
    recoloured = copy.deepcopy(styled)
    recoloured["line"] = {**recoloured["line"], "color": "srgb:000000"}
    assert C.score(_plan(component), _inv(recoloured), gt,
                   _inv())["score"] == pytest.approx(0.5)


def test_a_theme_colour_written_out_is_the_same_outline():
    """docs/design/reward.md §1's equivalence, on the facet the fix above just made
    load-bearing.  The `colour_written_out` legitimate variant — the same
    answer with every theme colour written as the sRGB a colour picker reports
    — dropped deck0002 to 0.951 and deck0009 to 0.947 the moment a restored
    connector's outline started being scored, because `scheme:TX1` and
    `srgb:000000` are the same black.  `_facet_fill` had resolved the theme
    since the beginning; `_facet_line` had not."""
    theme = {"tx1": "000000"}
    named = _shape("")
    named["line"] = {"fill": "solid", "color": "scheme:TX1", "dash": None,
                     "head": None, "tail": "triangle"}
    written = copy.deepcopy(named)
    written["line"] = {**written["line"], "color": "srgb:000000"}
    assert C._facet_line(named, written, theme)[0] == 1.0
    assert C._facet_line(named, written, {})[0] == 0.0
    other = copy.deepcopy(named)
    other["line"] = {**other["line"], "color": "srgb:FF0000"}
    assert C._facet_line(named, other, theme)[0] == 0.0


def test_a_composite_rebuilt_out_of_ordinary_shapes_is_paid_for():
    """The words, in boxes, where the object stood — which is what deck0007's
    instruction asks for."""
    built = [_box_at(text, 2400000 + 100000 * n, 1600000, str(n), 10 + n)
             for n, text in enumerate(NODES)]
    assert _rebuild_case(*built)["score"] == pytest.approx(1.0)
    assert _rebuild_case(*built[:2])["score"] == pytest.approx(0.5)


def test_the_rebuild_route_pays_for_nothing_it_should_not():
    """The negative controls, one per clause of `_facet_rebuilt_composite`.

    Every one of these is cheaper than rebuilding the object, and a route that
    is cheaper than the work is the route a training run finds first.
    """
    far = [_box_at(text, 11000000, 6000000, str(n), 10 + n)
           for n, text in enumerate(NODES)]
    assert _rebuild_case(*far)["score"] == 0.0, "words typed off in a corner"

    one = _box_at(" ".join(NODES), 2400000, 1600000, "0", 10)
    assert _rebuild_case(one)["score"] == 0.0, "one box holding the lot"

    lump = [_box_at(NODES[0], 2400000, 1600000, str(n), 10 + n)
            for n in range(len(NODES))]
    assert _rebuild_case(*lump)["score"] == pytest.approx(0.25), (
        "one word copied four times")

    empty = [_box_at("", 2400000 + 100000 * n, 1600000, str(n), 10 + n)
             for n in range(len(NODES))]
    assert _rebuild_case(*empty)["score"] == 0.0, "empty boxes in the hole"

    assert _rebuild_case()["score"] == 0.0, "nothing at all"


def test_one_box_does_not_satisfy_a_word_the_composite_repeats():
    """A table's cells and a diagram's nodes repeat their words — "Total"
    twice, "N/A" six times — and the four boxes such a table needs are four
    pieces of work.  Each want is **consumed** by the box that answers it, so
    drawing one and stopping scores one."""
    gt = _inv(_diagram_shape(("alpha", "beta", "alpha", "gamma")))
    broken = _inv()
    component = _component("delete", {"box": DIAGRAM_BOX})
    built = [_box_at(text, 2400000 + 100000 * n, 1600000, str(n), 10 + n)
             for n, text in enumerate(("alpha", "beta", "gamma"))]
    result = C.score(_plan(component), _inv(*built), gt, broken)
    assert result["score"] == pytest.approx(0.75), result["components"][0]["why"]


def test_a_missing_picture_is_not_rebuildable_out_of_words():
    """The route is for native composites, whose content *is* text.  A deleted
    picture is not put back by typing its name where it stood — and the only
    thing that says so is `_composite_texts` reading the diagram, the table and
    the chart, and nothing else about the shape."""
    picture = _shape("", kind="picture", name="Picture 3", sid=4)
    picture["picture"] = {"blob": "aaaa"}
    picture["keys"] = ["pic:aaaa", "name:Picture 3", "kind:picture"]
    picture["key"] = "pic:aaaa#0"
    gt, broken = _inv(picture), _inv()
    component = _component("delete", {"box": [550000, 800000, 900000, 400000]})
    cand = _inv(_box_at("Picture 3", 1000000, 1000000, "0", 10))
    assert C.score(_plan(component), cand, gt, broken)["score"] == 0.0
    assert C._composite_texts(picture) == []


def test_the_rebuild_route_may_not_re_use_a_shape_already_scored_as_itself():
    """A survivor that pairs with a gt shape of its own is that shape, not a
    box somebody drew: counting it here would pay twice for one object and let
    a page's existing furniture stand in for the missing one.

    Asserted on `raw`, not on `score`.  Floor normalisation subtracts the same
    survivor from the broken file and hides the double payment behind a 0.0 —
    which is the right verdict reached for the wrong reason, and it stops being
    right the moment the survivor is one the damage also moved."""
    survivor = _box_at(NODES[0], 2400000, 1600000, "1", 20)
    gt = _inv(_diagram_shape(NODES), survivor)
    broken = _inv(copy.deepcopy(survivor))
    component = _component("delete", {"box": DIAGRAM_BOX})
    kept = C.score(_plan(component), _inv(copy.deepcopy(survivor)), gt, broken)
    assert kept["components"][0]["raw"] == 0.0, kept["components"][0]["why"]
    assert kept["components"][0]["floor"] == 0.0
    assert kept["score"] == 0.0


def test_a_native_object_replaced_by_a_picture_of_one_is_a_cheat():
    table = _shape("", kind="table", name="Table 1", sid=4)
    table["table"] = {"n_rows": 1, "n_cols": 1,
                      "rows": [{"h": 0, "cells": [{"text": "v"}]}]}
    table["keys"] = ["table", "name:Table 1", "geo:table:10x4", "kind:table"]
    shot = _shape("", kind="picture", name="Screenshot", sid=5)
    shot["picture"] = {"blob": "shot"}
    shot["keys"] = ["pic:shot", "name:Screenshot", "geo:picture:10x4",
                    "kind:picture"]
    gt, broken = _inv(table), _inv(table)
    component = _component("clear_table_cells", {"cleared": [{"at": [0, 0]}],
                                                 "box": [0, 0, 900000, 400000]})
    result = C.score(_plan(component), _inv(shot), gt, broken)
    assert result["failed_gate"] == "native_objects_preserved"


def test_cloning_a_surviving_shape_into_the_hole_is_a_cheat():
    survivor = _shape("Card A")
    survivor["_path"] = "0"
    gt = _inv(survivor, _shape("Card B", name="Rectangle 2", sid=8))
    gt["slides"][0]["shapes"][1]["_path"] = "1"
    clone = _shape("Card A", name="Rectangle 9", sid=9)
    clone["bbox"] = {"cx": 1000000, "cy": 1000000, "w": 900000, "h": 400000,
                     "rot": 0.0, "flip": False}
    component = _component("delete", {"box": [550000, 800000, 900000, 400000]},
                           path="1")
    result = C.score(_plan(component), _inv(survivor, clone), gt, _inv(survivor))
    assert result["failed_gate"] == "no_cloned_shapes"


def test_new_content_in_the_hole_is_not_a_clone():
    """Five hand-built boxes carrying five law names is what deck0007's
    instruction asks for in as many words; the first version of the clone gate
    fired on exactly that."""
    survivor = _shape("Card A")
    gt = _inv(survivor, _shape("Card B", name="Rectangle 2", sid=8))
    gt["slides"][0]["shapes"][1]["_path"] = "1"
    fresh = _shape("Card B", name="Rectangle 9", sid=9)
    component = _component("delete", {"box": [550000, 800000, 900000, 400000]},
                           path="1")
    result = C.score(_plan(component), _inv(survivor, fresh), gt, _inv(survivor))
    assert result["failed_gate"] is None


# --------------------------------------------------------------------------- #
# media
# --------------------------------------------------------------------------- #


def test_media_is_compared_as_a_multiset_of_content_not_by_part_name():
    """WPS renames and renumbers every media part and adds a thumbnail — 51
    blobs in, 52 out, no original lost.  A gate keyed on `part name -> digest`
    therefore fails on a file WPS did nothing to."""
    gt = _inv(_shape("a"), media=("aa", "bb"))
    broken = _inv(_shape("a"), media=("aa", "bb"))
    wps = _inv(_shape("a"), media=("aa", "bb", "thumb"))
    component = _component("move", {"box": [0, 0, 900000, 400000]})
    result = C.score(_plan(component), wps, gt, broken)
    assert result["hard_gates"]["media_not_pasted"]
    assert result["hard_gates"]["input_media_preserved"]


def test_pasting_the_original_back_is_a_cheat():
    gt = _inv(_shape("a"), media=("aa", "secret"))
    broken = _inv(_shape("a"), media=("aa",))
    pasted = _inv(_shape("a"), media=("aa", "secret"))
    component = _component("delete", {"box": [0, 0, 900000, 400000]})
    result = C.score(_plan(component), pasted, gt, broken)
    assert result["failed_gate"] == "media_not_pasted"


def test_a_supplied_asset_is_not_a_pasted_original():
    """Nine of the ten decks hand the deleted bitmap over in `assets/` byte
    for byte, because "put this picture back" is the instruction."""
    gt = _inv(_shape("a"), media=("aa", "secret"))
    broken = _inv(_shape("a"), media=("aa",))
    pasted = _inv(_shape("a"), media=("aa", "secret"))
    component = _component("delete", {"box": [0, 0, 900000, 400000]})
    plan = _plan(component, assets_sha=["secret"])
    assert C.score(plan, pasted, gt, broken)["hard_gates"]["media_not_pasted"]


def test_a_re_encoded_image_is_not_a_lost_one():
    """The ground truth, opened and saved in WPS, came back with four PNGs
    re-encoded on deck0004 and two on deck0005 — one of them 161755 bytes down
    to 105850 — and was charged 0.15 for images nobody touched.  LibreOffice
    re-encodes *every* image.  Bytes belong to the application; the number of
    pictures in the package belongs to the agent."""
    gt = _inv(_shape("a"), media=("aa",))
    broken = _inv(_shape("a"), media=("aa",))
    reencoded = _inv(_shape("a"), media=("aa-recoded",))
    component = _component("move", {"box": [0, 0, 900000, 400000]})
    result = C.score(_plan(component), reencoded, gt, broken)
    assert result["failed_gate"] is None
    assert result["scope_violations"] == {}


def test_losing_the_inputs_media_is_a_penalty_not_a_zero():
    """Deleting a picture out of the package is still worth a penalty — and
    only a penalty: a check an application can fail on the agent's behalf must
    never be a zero."""
    gt = _inv(_shape("a"), media=("aa", "bb"))
    broken = _inv(_shape("a"), media=("aa", "bb"))
    stripped = _inv(_shape("a"), media=("aa",))
    component = _component("move", {"box": [0, 0, 900000, 400000]})
    result = C.score(_plan(component), stripped, gt, broken)
    assert result["failed_gate"] is None
    assert "input_media_preserved" in result["scope_violations"]


# --------------------------------------------------------------------------- #
# coherence
# --------------------------------------------------------------------------- #


@pytest.mark.corpus
@pytest.mark.parametrize("name", ACCEPTED)
def test_no_gate_fires_on_work_a_component_would_reward(name):
    """The check that nothing else performs: a gate and a component in one
    file disagreeing about whether the same state is correct."""
    plan, _gt, _init = _deck(name)
    assert plan["coherence"]["failures"] == []
    for state, report in plan["coherence"]["states"].items():
        assert report["failed_gate"] is None, state


@pytest.mark.corpus
@pytest.mark.parametrize("name", ACCEPTED)
def test_partial_work_scores_between_nothing_and_everything(name):
    plan, _gt, _init = _deck(name)
    assert 0.0 < plan["coherence"]["states"]["half_restore"]["score"] < 1.0


@pytest.mark.corpus
@pytest.mark.parametrize("name", ACCEPTED + ("deck0002",))
def test_how_much_of_a_deck_rides_on_a_coordinate_is_measured(name):
    """deck0009's `c016` is a deleted table worth **0.3934** whose 27 cells the
    instruction gives verbatim — and whose centre the bundle discloses nowhere.
    `_facet_centre` is binary, so a perfect table placed **0.02 in** out scores
    the deck 0.7377, and so does one placed a foot out.  The probe recorded the
    freedom in prose — *"any sensible placement in the empty left half must be
    accepted"* — marked the degradation determinate and passed it, and the deck
    shipped.

    The instrument is not the defect: binary is what stops "paste it roughly
    there" being cheaper than restoring the thing, and on thirteen of the
    fourteen components scored `content × position` the coordinate *is*
    disclosed.  What was missing is the number, so it is now a state in every
    plan, and it is diagnostic rather than a failure — a deck reading low here
    is a question about its assets, not a rubric to loosen.
    """
    plan, _gt, _init = _deck(name)
    states = plan["coherence"]["states"]
    slip = states.get("position_slip")
    if slip is None:
        pytest.skip("no restored shape carries a box on this deck")
    if states["ground_truth"]["failed_gate"]:
        pytest.skip("a gate already zeroes every state on this deck")
    assert slip["failed_gate"] is None
    assert 0.0 <= slip["score"] < 1.0


# --------------------------------------------------------------------------- #
# determinism
# --------------------------------------------------------------------------- #


@pytest.mark.corpus
def test_the_plan_is_deterministic():
    """The battery scores against a plan on disk; a plan that changes between
    builds makes every stored result unreproducible."""
    import json
    first = json.dumps(C.build_plan(WORK / "deck0008", write=False),
                       sort_keys=True, default=str)
    second = json.dumps(C.build_plan(WORK / "deck0008", write=False),
                        sort_keys=True, default=str)
    assert first == second


def test_score_takes_inventories_not_paths():
    """The battery scores many constructed decks against one plan and must not
    re-parse the ground truth for each."""
    import inspect
    args = list(inspect.signature(C.score).parameters)
    assert args == ["plan", "candidate_inv", "gt_inv", "init_inv"]


# --------------------------------------------------------------------------- #
# the twenty-one comparators that had never scored a real deck
#
# Every test below is a defect the audit found by applying the operator with
# `degrade_exec` to a deck in `work/` and scoring the file it produced.  Each
# one either handed out credit the broken file could collect (a floor, which
# rejects the task) or refused credit for work that was really done (a zero
# for a partial restoration, which is the failure that recorded 0.0 for a
# model who had done 43%, 53% and 63%).
# --------------------------------------------------------------------------- #


def _para(text, runs):
    return {"t": text, "runs": runs}


def _text_runs_shape(paragraphs):
    shape = _shape("x")
    shape["text"] = {"paragraphs": paragraphs}
    shape["_plain"] = " ".join(p["t"] for p in paragraphs)
    return shape


def test_a_restyle_the_answer_inherits_is_unscoreable_not_free():
    """Measured on deck0008 p3: `text_runs` restyled three paragraphs whose
    ground truth states neither colour nor weight, the comparator returned 1.0
    for everybody, the floor was therefore 1.0, and `score` reported "nothing
    to discriminate" — **the ground truth scored 0.000**.  A property the
    answer inherits is not decidable; the honest answer is `Unscorable`, which
    drops the component and rejects the task for asking for work nobody
    scores."""
    gt = _inv(_text_runs_shape([_para("A", [{"t": "A"}])]))
    spec = {"touched": [{"paragraph": 0, "action": "restyled"}],
            "params": {"bold": False, "color": "999999"}}
    component = _component("text_runs", spec)
    scene = C.Scene(gt, gt)
    with pytest.raises(C.Unscorable):
        C._cmp_text_runs(C.Target(scene, component))


def test_a_restyle_is_not_paid_for_leaving_the_text_alone():
    """The same comparator paid 0.5 for a paragraph whose *text* was present
    and whose properties were wrong.  A restyle never touches the text, so
    that half is collected by the broken file: floor 0.50 on deck0008, over
    `FLOOR_LIMIT` by itself."""
    gt = _inv(_text_runs_shape([_para("A", [{"t": "A", "sz": 2000}])]))
    broken = _inv(_text_runs_shape([_para("A", [{"t": "A", "sz": 1100}])]))
    spec = {"touched": [{"paragraph": 0, "action": "restyled"}],
            "params": {"size_pt": 11}}
    component = _component("text_runs", spec)
    assert C._cmp_text_runs(C.Target(C.Scene(gt, gt), component))[0] == 1.0
    assert C._cmp_text_runs(C.Target(C.Scene(gt, broken), component))[0] == 0.0


def test_a_deleted_paragraph_is_scored_on_the_words_that_went_missing():
    """The opposite case, and it must keep working: where the step deleted the
    paragraph there is no style to compare and the text *is* the damage."""
    gt = _inv(_text_runs_shape([_para("A", [{"t": "A"}]),
                                _para("B", [{"t": "B"}])]))
    half = _inv(_text_runs_shape([_para("A", [{"t": "A"}])]))
    spec = {"touched": [{"paragraph": 0, "action": "deleted"},
                        {"paragraph": 1, "action": "deleted"}],
            "params": {"delete": True}}
    component = _component("text_runs", spec)
    assert C._cmp_text_runs(C.Target(C.Scene(gt, gt), component))[0] == 1.0
    assert C._cmp_text_runs(C.Target(C.Scene(gt, half), component))[0] == 0.5


def test_runs_are_paired_by_their_words_not_only_by_their_index():
    """Text re-entered in the application comes back split differently.  An
    index-only pairing reads a paragraph whose visible state is exactly right
    as every property missing."""
    want = [{"t": "A", "sz": 2000}, {"t": "B", "sz": 1200}]
    reordered = [{"t": "B", "sz": 1200}, {"t": "A", "sz": 2000}]
    assert C._runs_match(want, reordered, ("sz",)) == (2, 2)
    # the negative control: pairing by words does not forgive a wrong value
    wrong = [{"t": "A", "sz": 1100}, {"t": "B", "sz": 1200}]
    assert C._runs_match(want, wrong, ("sz",)) == (1, 2)


def _table_shape(rows):
    shape = _shape("t", kind="table")
    shape["table"] = {"n_rows": len(rows), "n_cols": len(rows[0]),
                      "rows": [{"h": 0, "cells": [{"text": c} for c in row]}
                               for row in rows]}
    return shape


def test_one_of_two_dropped_rows_back_is_worth_half_not_nothing():
    """`if n_rows != want: return 0` scored a table with one of its two lost
    rows restored **exactly the same as doing nothing** — and absolute indices
    are no better, because the row still missing shifts the one that came back
    one place early."""
    gt = _inv(_table_shape([["a"], ["b"], ["c"], ["d"]]))
    broken = _inv(_table_shape([["a"], ["d"]]))
    half = _inv(_table_shape([["a"], ["b"], ["d"]]))
    spec = {"removed": [{"row": 1}, {"row": 2}]}
    component = _component("table_drop_rows", spec)
    assert C._cmp_drop_rows(C.Target(C.Scene(gt, gt), component))[0] == 1.0
    assert C._cmp_drop_rows(C.Target(C.Scene(gt, half), component))[0] == 0.5
    assert C._cmp_drop_rows(C.Target(C.Scene(gt, broken), component))[0] == 0.0


def test_a_row_put_back_in_the_wrong_place_is_not_put_back():
    """The anti-cheat half of dropping the count guard: appending the missing
    row at the bottom is the cheapest wrong answer there is."""
    gt = _inv(_table_shape([["a"], ["b"], ["c"]]))
    shuffled = _inv(_table_shape([["a"], ["c"], ["b"]]))
    component = _component("table_drop_rows", {"removed": [{"row": 1}]})
    assert C._cmp_drop_rows(C.Target(C.Scene(gt, shuffled), component))[0] == 0.0


def test_a_table_that_lost_a_surviving_row_scores_nothing():
    gt = _inv(_table_shape([["a"], ["b"], ["c"]]))
    lost = _inv(_table_shape([["b"]]))
    component = _component("table_drop_rows", {"removed": [{"row": 1}]})
    assert C._cmp_drop_rows(C.Target(C.Scene(gt, lost), component))[0] == 0.0


def test_one_of_two_dropped_columns_back_is_worth_half():
    gt = _inv(_table_shape([["a", "b", "c"], ["d", "e", "f"]]))
    broken = _inv(_table_shape([["a"], ["d"]]))
    half = _inv(_table_shape([["a", "b"], ["d", "e"]]))
    component = _component("table_drop_cols", {"removed": [{"col": 1},
                                                           {"col": 2}]})
    assert C._cmp_drop_cols(C.Target(C.Scene(gt, gt), component))[0] == 1.0
    assert C._cmp_drop_cols(C.Target(C.Scene(gt, half), component))[0] == 0.5
    assert C._cmp_drop_cols(C.Target(C.Scene(gt, broken), component))[0] == 0.0


def _stack(n):
    shapes = []
    for index in range(n):
        shape = _shape(f"s{index}", name=f"Rect {index}", sid=index + 1)
        shape["_path"] = str(index)
        shape["z"] = index
        shapes.append(shape)
    return shapes


def test_zorder_scores_only_the_peers_the_move_passed():
    """Sending a shape to the back inverts its order with the shapes it was in
    front of and leaves every other pair alone — and those untouched pairs are
    satisfied by the broken file.  Measured floor on deck0001 p8: **0.27**,
    which rejects the task for a component that discriminates perfectly."""
    gt = _inv(*_stack(4))
    broken = copy.deepcopy(gt)
    moved = broken["slides"][0]["shapes"][2]
    moved["z"] = -1                                # sent to the back
    component = _component("zorder", {"to": "back"}, path="2")
    assert C._cmp_zorder(C.Target(C.Scene(gt, gt), component))[0] == 1.0
    assert C._cmp_zorder(C.Target(C.Scene(gt, broken), component))[0] == 0.0


def _connector(attached_end_only=True):
    shape = _shape("", kind="connector", name="Connector 5", sid=5)
    shape["connector"] = {"start": None, "end": {"id": 9, "idx": 0}}
    return shape


def test_a_connector_end_nobody_detached_is_not_scored():
    """deck0004 p3's connectors are attached at one end.  Scoring both ends
    awards the untouched one 0.5 on the broken file — a measured floor of
    0.375, which rejects the task."""
    target = _shape("box", name="Box 9", sid=9)
    target["_path"] = "1"
    gt = _inv(_connector(), target)
    detached = copy.deepcopy(gt)
    detached["slides"][0]["shapes"][0]["connector"] = {"start": None,
                                                       "end": None}
    spec = {"was_attachments": [{"a:endCxn": {"id": "9", "idx": "0"}}],
            "nudge_in": 0}
    component = _component("detach_connector", spec)
    assert C._cmp_detach(C.Target(C.Scene(gt, gt), component))[0] == 1.0
    assert C._cmp_detach(C.Target(C.Scene(gt, detached), component))[0] == 0.0


def test_a_connector_that_was_attached_nowhere_is_unscoreable():
    """`detach_connector` on a drawn line records `was_attachments: []` and
    only nudges it.  With the nudge switched off there is nothing left that
    was damaged, and inventing a number for it is how a floor is born."""
    gt = _inv(_connector())
    component = _component("detach_connector",
                           {"was_attachments": [], "nudge_in": 0})
    with pytest.raises(C.Unscorable):
        C._cmp_detach(C.Target(C.Scene(gt, gt), component))


def test_slide_order_scores_only_the_pages_the_edit_displaced():
    """Swapping two pages of nineteen leaves seventeen where they were, and
    the broken file collects all seventeen: measured floor **0.89** on
    deck0001."""
    assert C._displaced({"swapped": [[2, 3]]}, 19) == [1, 2]
    # a deletion displaces the deleted page and everything behind it
    assert C._displaced({"pages": [3]}, 5) == [2, 3, 4]
    # nothing recorded: judge the whole deck rather than guess
    assert C._displaced({}, 3) == [0, 1, 2]


def test_a_page_too_thin_to_tell_apart_is_not_judged():
    """It answers `True` for everybody, which is free credit by another
    route."""
    thin = _inv(_shape(""))
    component = _component("reorder_slides", {"swapped": [[1, 1]]}, path=None)
    with pytest.raises(C.Unscorable):
        C._cmp_slide_order(C.Target(C.Scene(thin, thin), component))


# --------------------------------------------------------------------------- #
# the page a floor is measured against
# --------------------------------------------------------------------------- #
#
# `_init_slide_of` used to `return None` on *both* of its branches: it worked
# the answer out from `deleted_slides` and `swapped`, and then threw it away.
# A deck that moved a page therefore got the identity mapping, and every floor
# on it was measured against the wrong page — silently, with nothing in the
# plan to show for it.


def test_a_deck_that_moves_no_page_keeps_the_identity_mapping():
    """`None` *is* the identity, and it is what the ten shipped decks get.  A
    mapping written out for a deck that never moved a page would be noise in
    every `plan.json` and a diff in all ten of them."""
    assert C._init_slide_of({}, 5) is None
    assert C._init_slide_of({"slides": {"0": [{"op": "delete"}]}}, 5) is None


def test_a_deleted_page_maps_to_no_page_at_all_and_the_rest_shift_up():
    """gt pages 1..5 with page 2 deleted: the broken file's four pages are gt
    1, 3, 4, 5 — so the mapping is [0, absent, 1, 2, 3]."""
    assert C._init_slide_of({"deleted_slides": [2]}, 5) == [0, None, 1, 2, 3]


def test_two_deletions_are_replayed_in_order_not_struck_out_together():
    """`degrade_exec` deletes one page at a time, each number read against the
    deck as it stands at that moment: `delete_slides: [2, 5]` on a six-page
    deck removes the original pages 2 and **6**.  Sorting the numbers and
    striking them out together — the obvious reading — names page 5."""
    got = C._init_slide_of({"deleted_slides": [2, 5]}, 6)
    assert got == [0, None, 1, 2, 3, None]
    assert got != [0, None, 1, 2, None, 3]          # the obvious reading


def test_a_swap_maps_each_page_to_where_the_swap_put_it():
    assert C._init_slide_of(
        {"reorder_slides": {"swapped": [[2, 4]]}}, 5) == [0, 3, 2, 1, 4]


def test_a_swap_is_replayed_against_what_the_deletions_left():
    """`degrade_exec.run` deletes first and reorders second, so the swap's page
    numbers count pages in the *shortened* deck.  Deleting page 1 of five
    leaves gt 2,3,4,5; swapping that deck's pages 1 and 3 gives gt 4,3,2,5."""
    delta = {"deleted_slides": [1], "reorder_slides": {"swapped": [[1, 3]]}}
    assert C._init_slide_of(delta, 5) == [None, 2, 1, 0, 3]


def test_the_floor_is_measured_against_the_page_the_mapping_names():
    """The whole point of the mapping, and the damage the `None` did.  Two
    pages swapped and gt page 1's shape deleted: without the mapping the floor
    for the page nobody damaged is measured against the page that *was*
    damaged, and reports a floor of 0 for work that is already done."""
    gt = _inv(_shape("alpha"))
    gt["slides"].append(copy.deepcopy(gt["slides"][0]))
    gt["slides"][1]["i"] = 1
    gt["slides"][1]["_part"] = "ppt/slides/slide2.xml"
    gt["slides"][1]["shapes"] = [_shape("beta")]
    gt["package"]["slide_count"] = 2
    gt["package"]["slide_order"] = ["slide1.xml", "slide2.xml"]

    init = copy.deepcopy(gt)
    init["slides"] = [copy.deepcopy(gt["slides"][1]),      # the pages swapped
                      copy.deepcopy(gt["slides"][0])]
    init["slides"][1]["shapes"] = []                       # and gt p1 emptied

    mapping = C._init_slide_of({"reorder_slides": {"swapped": [[1, 2]]}}, 2)
    assert mapping == [1, 0]

    damaged = _component("delete", {}, path="0", slide=0)
    intact = _component("delete", {}, path="0", slide=1)
    assert C._run_component(damaged, C.Scene(gt, init, mapping))[0] == 0.0
    assert C._run_component(intact, C.Scene(gt, init, mapping))[0] == 1.0
    # the identity, which is what the `None` handed every such deck
    assert C._run_component(intact, C.Scene(gt, init))[0] == 0.0


@pytest.mark.corpus
def test_a_record_that_cannot_be_replayed_rejects_the_plan(monkeypatch):
    """Falling back on the identity is not a fallback here: the identity is a
    claim that no page moved, and the record has just said one did."""
    with pytest.raises(C.Unscorable):
        C._init_slide_of({"deleted_slides": [9]}, 3)
    with pytest.raises(C.Unscorable):
        C._init_slide_of({"reorder_slides": {"swapped": [[1, 9]]}}, 3)

    monkeypatch.setattr(C, "_init_slide_of",
                        lambda delta, n: (_ for _ in ()).throw(
                            C.Unscorable("page 9 of 3")))
    plan = C.build_plan(WORK / "deck0003", write=False)
    assert plan["init_slide_of"] is None
    assert any("cannot map the broken file's pages" in reason
               for reason in plan["rejected"])
    _, gt, init = _deck("deck0003")
    assert C.score(plan, gt, gt, init)["score"] == 0.0


# --------------------------------------------------------------------------- #
# equivalence: the same answer written another way
# --------------------------------------------------------------------------- #


THEME = {"accent1": "4472C4", "tx1": "000000"}


def test_a_theme_colour_written_out_as_srgb_is_the_same_colour():
    """docs/design/reward.md §1's first example of equivalence, and a measured one: the
    `colour_written_out` variant scored **0.902** on deck0010 for writing the
    sRGB a colour picker reports for the theme colour it was told to
    restore."""
    assert C._same_colour("scheme:ACCENT1", "srgb:4472C4", THEME)
    assert C._same_colour("srgb:4472C4", "scheme:ACCENT1", THEME)


def test_a_different_colour_is_still_a_different_colour():
    """The negative control.  `wrong_params` repaints in 7F007F and must stay
    at zero."""
    assert not C._same_colour("scheme:ACCENT1", "srgb:7F007F", THEME)
    assert not C._same_colour("scheme:ACCENT1", "scheme:ACCENT2", THEME)
    # a colour the theme does not name is not resolved to anything
    assert not C._same_colour("scheme:UNKNOWN", "srgb:4472C4", THEME)
    # and with no dictionary at all nothing is resolved
    assert not C._same_colour("scheme:ACCENT1", "srgb:4472C4", {})


def test_a_theme_colour_carrying_modifiers_is_never_resolved():
    """`lumMod` and `shade` are arithmetic the renderer does.  Guessing at it
    would make two different colours look the same, which is the direction
    that gives a cheat somewhere to hide."""
    assert C._resolve_colour("scheme:ACCENT1+lumMod", THEME) == \
        "scheme:ACCENT1+lumMod"
    assert not C._same_colour("scheme:ACCENT1+lumMod", "srgb:4472C4", THEME)


def test_a_group_around_restored_shapes_is_not_extra_furniture():
    """Grouping the shapes you have just put back draws nothing — the group's
    box is its children's extent and every child is scored on its own.
    Charging for the container cost the `regrouped` variant 0.12 on deck0003
    with every restored shape exact."""
    child = _shape("a")
    child["_path"] = "0/0"
    child["group"] = "name:Group 1"
    group = _shape("", kind="group", name="Group 1", sid=1)
    group["_path"] = "0"
    gt = _inv(_shape("a"))
    grouped = _inv(group, child)
    component = _component("delete", {"box": [0, 0, 900000, 400000]})
    result = C.score(_plan(component), grouped, gt, gt)
    assert result["scope_violations"] == {}
    assert result["penalty"] == 0.0


def test_a_group_holding_something_new_still_pays():
    """The negative control: hiding new furniture inside a group must not make
    it free."""
    junk = _shape("nobody asked for this", name="Junk", sid=42)
    junk["_path"] = "0/0"
    junk["group"] = "name:Group 1"
    junk["bbox"] = {"cx": 9000000, "cy": 5000000, "w": 500000, "h": 200000,
                    "rot": 0.0, "flip": False}
    group = _shape("", kind="group", name="Group 1", sid=1)
    group["_path"] = "0"
    group["bbox"] = dict(junk["bbox"])
    gt = _inv(_shape("a"))
    grouped = _inv(_shape("a"), group, junk)
    component = _component("delete", {"box": [0, 0, 900000, 400000]})
    result = C.score(_plan(component), grouped, gt, gt)
    assert "no_extra_shapes" in result["scope_violations"]


def test_dissolving_a_group_is_not_losing_a_survivor():
    """Ungrouping draws exactly the same page.  Charging for the vanished
    container cost the `ungrouped` variant the full 0.30 cap on deck0006 — 28
    groups dissolved — with every component still scoring 1.00."""
    child = _shape("a")
    child["_path"] = "0/0"
    child["group"] = "name:Group 1"
    group = _shape("", kind="group", name="Group 1", sid=1)
    group["_path"] = "0"
    damaged = _shape("b", name="Box 2", sid=2)
    damaged["_path"] = "1"
    gt = _inv(group, child, damaged)
    loose = _inv(_shape("a"), damaged)
    component = _component("delete", {"box": [0, 0, 900000, 400000]}, path="1")
    result = C.score(_plan(component), loose, gt, gt)
    assert result["scope_violations"] == {}


def test_a_group_that_took_its_children_with_it_is_still_a_loss():
    """The negative control: deleting the group *and* its contents is not
    ungrouping."""
    child = _shape("a")
    child["_path"] = "0/0"
    child["group"] = "name:Group 1"
    group = _shape("", kind="group", name="Group 1", sid=1)
    group["_path"] = "0"
    damaged = _shape("b", name="Box 2", sid=2)
    damaged["_path"] = "1"
    gt = _inv(group, child, damaged)
    stripped = _inv(damaged)
    component = _component("delete", {"box": [0, 0, 900000, 400000]}, path="1")
    result = C.score(_plan(component), stripped, gt, gt)
    assert "survivors_intact" in result["scope_violations"]


# --------------------------------------------------------------------------- #
# the same rules, against decks the suite owns
#
# Everything above marked `@pytest.mark.corpus` reads `work/`, which is the
# live pipeline directory: those tests answer *are these ten decks shippable*,
# and they move when the pipeline moves.  Everything below asks the same
# questions of `tests/fixtures/minidecks.py` — decks built from nothing by the
# real degrader, so a failure here is a regression in `comparators.py` and can
# be nothing else.
#
# What the miniature decks cannot carry is listed in that module's docstring;
# the two properties that need what they lack are named at the bottom of this
# section.
# --------------------------------------------------------------------------- #


#: the frozen decks whose plan `build_plan` accepts.  `mini_plain` is weighted
#: from the proposer's declaration and `mini_measured` from the solvability
#: probe's measurement, so the pair covers both arms of the weighting.
MINI_ACCEPTED = ("mini_plain", "mini_measured")
#: and the ones whose refusal is the point of the deck
MINI_REFUSED = ("mini_no_deg", "mini_high_floor", "mini_excused")


# ---- fail closed ----------------------------------------------------------- #


@pytest.mark.parametrize("name", MINI_ACCEPTED)
def test_the_broken_frozen_file_scores_zero_on_every_component(name, mini):
    plan, gt, init = mini(name)
    result = C.score(plan, init, gt, init)
    assert result["score"] == 0.0
    assert all(c["score"] == 0.0 for c in result["components"])


@pytest.mark.parametrize("name", MINI_ACCEPTED)
def test_the_ground_truth_of_a_frozen_deck_scores_one(name, mini):
    plan, gt, init = mini(name)
    result = C.score(plan, gt, gt, init)
    assert result["failed_gate"] is None
    assert result["score"] == pytest.approx(1.0)


@pytest.mark.parametrize("name", MINI_ACCEPTED)
def test_every_frozen_component_discriminates(name, mini):
    plan, gt, init = mini(name)
    good = C.score(plan, gt, gt, init)
    bad = C.score(plan, init, gt, init)
    dull = [c["id"] for c, b in zip(good["components"], bad["components"])
            if c["raw"] - b["raw"] <= 0]
    assert dull == []


# ---- floor normalisation --------------------------------------------------- #


@pytest.mark.parametrize("name", MINI_ACCEPTED)
def test_no_accepted_frozen_deck_carries_a_floor_over_the_limit(name, mini):
    plan, _gt, _init = mini(name)
    assert [c["id"] for c in plan["components"]
            if c["floor"] > C.FLOOR_LIMIT] == []


def test_a_high_floor_rejects_a_frozen_plan(mini):
    """`mini_high_floor` de-bolds two blocks of four runs of which the answer
    bolds exactly one, so each component is 75% satisfied by the wreckage
    before anybody touches it.  The plan has to say so rather than quietly
    normalise it away.

    deck0009 was the corpus specimen and a repair has since mended it, which
    is the reason this deck exists: the rule outlived its only example.
    """
    plan, _gt, _init = mini("mini_high_floor")
    assert any("floor above" in reason for reason in plan["rejected"])
    hot = [c["id"] for c in plan["components"] if c["floor"] > C.FLOOR_LIMIT]
    assert len(hot) == 2
    assert all(plan["components"][i]["floor"] == pytest.approx(0.75)
               for i, c in enumerate(plan["components"]) if c["id"] in hot)


def test_the_floor_of_a_frozen_deck_is_measured_not_assumed(mini):
    """The other half: a component the wreckage does *not* satisfy reads 0.0,
    so a nonzero floor is a measurement and never a default."""
    plan, _gt, _init = mini("mini_plain")
    assert [c["floor"] for c in plan["components"]] == [0.0, 0.0, 0.0]


# ---- unscoreable components and the weight they forfeit -------------------- #


def test_an_unsatisfiable_frozen_component_is_dropped_not_left_to_punish(mini):
    """`mini_inherited`'s slides 5 and 6 state no run properties at all — they
    inherit them — so a `set_font` component asking for a *value* of one
    cannot be passed by the ground truth itself.  Three such components are
    removed from the plan and named rather than left in to take marks off work
    that was done right.  deck0004 was the corpus specimen, with six.
    """
    plan, _gt, _init = mini("mini_inherited")
    dropped = {u["id"] for u in plan["unscoreable"]}
    assert len(dropped) == 3
    assert all(u["op"] == "set_font" for u in plan["unscoreable"])
    assert all(u["deg"] == "d2" for u in plan["unscoreable"])
    assert all(u["gt_scores"] == 0.0 for u in plan["unscoreable"])
    assert not (dropped & {c["id"] for c in plan["components"]})


def test_a_dropped_frozen_components_weight_is_forfeited_not_paid_to_siblings(mini):
    """The half of the drop that was wrong.  `d2` declares four components,
    three of which the answer cannot satisfy.  They are removed — right — and
    then the degradation's share used to be divided among the *survivors*, so
    an agent that fixed the one survivor and none of the three scored 100% of
    `d2`.  Work nobody can earn must not become free marks.
    """
    plan, _gt, _init = mini("mini_inherited")
    d2 = next(d for d in plan["degradations"] if d["id"] == "d2")
    assert d2["components_unscoreable"] == 3
    assert len(d2["components"]) == 1
    assert d2["share_forfeited"] == pytest.approx(3 / 4)
    steps = _steps_used(plan)
    other = next(d for d in plan["degradations"] if d["id"] != "d2")
    unforfeited = d2["weight"] / (1.0 - d2["share_forfeited"])
    assert unforfeited / steps["d2"] == pytest.approx(
        other["weight"] / steps[other["id"]], rel=1e-6)
    assert sum(d["weight"] for d in plan["degradations"]) == pytest.approx(1.0)
    assert sum(c["weight"] for c in plan["components"]) == pytest.approx(1.0)


def test_a_frozen_degradation_the_drop_empties_refuses_the_plan(monkeypatch, mini):
    """A component that cannot pass its own answer never reaches a score — it
    is dropped and named — and a degradation the drop leaves with nothing is
    a task refused outright, not one shipped with work nobody scores.

    The corpus version of this is deck0007's SmartArt, resolved by data part
    and made ambiguous by a second diagram on the page; the miniature decks
    hold no SmartArt, so the arm exercised here is the rule and not the
    resolver.  See `test_a_composite_the_answer_cannot_disambiguate_never_
    reaches_a_score`, which stays corpus-bound for that reason.
    """
    def refuse(target):
        raise C.Unscorable("the answer cannot disambiguate this")

    root = mini.root("mini_inherited")
    monkeypatch.setitem(C.REGISTRY, "move", refuse)
    refused = C.build_plan(root, write=False)
    assert [u["op"] for u in refused["unscoreable"]].count("move") == 1
    assert all(c["op"] != "move" for c in refused["components"])
    assert any("no scoreable component" in reason and "d2" in reason
               for reason in refused["rejected"])
    assert abs(sum(c["weight"] for c in refused["components"]) - 1.0) < 1e-9


# ---- weights --------------------------------------------------------------- #


@pytest.mark.parametrize("name", MINI_ACCEPTED + ("mini_inherited",))
def test_frozen_weight_follows_the_steps_not_the_number_of_entries(name, mini):
    """A degradation's weight is *split* among its entries, never multiplied
    by them — stated as the exact proportionality, because both ways a weight
    legitimately moves off the raw step count (a measurement replacing the
    declaration, and a forfeited share) have to stay visible."""
    plan, _gt, _init = mini(name)
    steps = _steps_used(plan)
    weights = {d["id"]: d["weight"] for d in plan["degradations"]}
    kept = _kept(plan)
    assert sum(weights.values()) == pytest.approx(1.0)
    want = {d: steps[d] * kept[d] for d in steps}
    scale = sum(want.values())
    for deg in steps:
        assert weights[deg] == pytest.approx(want[deg] / scale, abs=1e-9), deg


@pytest.mark.parametrize("name", MINI_ACCEPTED)
def test_the_biggest_frozen_job_is_never_worth_less_than_the_smallest(name, mini):
    plan, _gt, _init = mini(name)
    steps = _steps_used(plan)
    order = sorted(plan["degradations"], key=lambda d: steps[d["id"]])
    assert steps[order[0]["id"]] < steps[order[-1]["id"]], "a degenerate deck"
    assert order[0]["weight"] < order[-1]["weight"]


def test_reward_per_step_is_flat_on_the_frozen_deck_that_measured_them(mini):
    """The defect this pins shut: deck0006's cheapest job carried 0.3158 of
    the reward and its most expensive 0.2368 — 12.4x more reward per step for
    the trivial one, which points an agent that maximises reward per step at
    exactly the work these tasks are not for."""
    plan, _gt, _init = mini("mini_measured")
    assert plan["weight_source"] == "steps_measured"
    steps = _steps_used(plan)
    per_step = [d["weight"] / steps[d["id"]] for d in plan["degradations"]
                if not d["share_forfeited"]]
    assert max(per_step) / min(per_step) == pytest.approx(1.0, abs=1e-6)


@pytest.mark.parametrize("name", MINI_ACCEPTED)
def test_frozen_component_weights_sum_to_one(name, mini):
    plan, _gt, _init = mini(name)
    assert sum(c["weight"] for c in plan["components"]) == pytest.approx(1.0)


def test_the_measured_step_count_is_preferred_to_the_declared_one_frozen(mini):
    """`est_steps` is the proposer's declaration and nothing validated it.
    `mini_measured` declares `d1` at 120 and its probe measures the same work
    at 50; the measurement wins, and the disagreement is recorded rather than
    absorbed."""
    plan, _gt, _init = mini("mini_measured")
    check = plan["weight_check"]
    assert plan["weight_source"] == "steps_measured"
    assert check["measured"] == {"d1": 50, "d2": 30, "d3": 10}
    assert check["declared"] == {"d1": 120, "d2": 18, "d3": 12}
    assert check["worst"] == 2.4
    assert "90" in check["measured_from"]
    weights = {d["id"]: d["weight"] for d in plan["degradations"]}
    assert weights["d1"] == pytest.approx(5 * weights["d3"])


def test_a_number_that_could_be_a_slide_is_not_read_as_a_step_count_frozen(mini):
    """"d1 rebuild the box on slide 4 ~50" — the first number after `d1` is 4.
    A parse that took bare numbers would weight the biggest job at four
    steps."""
    steps, why, ok = C._measured_steps(mini.root("mini_measured"),
                                       ["d1", "d2", "d3"])
    assert ok and steps["d1"] == 50, why


# ---- traceability, both directions ----------------------------------------- #


def test_a_frozen_delta_without_deg_is_refused(mini):
    """A delta that predates the `deg` field: nothing in it can be attributed
    to anything the task asks for, so the plan is refused rather than weighted
    by guesswork.  deck0001 was the corpus specimen until a repair gave its
    delta the field."""
    plan, gt, init = mini("mini_no_deg")
    assert any("no `deg`" in reason for reason in plan["rejected"])
    assert all(c.get("deg") is None for c in plan["components"])
    # and the refusal is what stops it: the plan is otherwise perfectly good
    assert C.score(plan, gt, gt, init)["failed_gate"] == "plan_accepted"
    assert C.score(plan, gt, gt, init)["score"] == 0.0


@pytest.mark.parametrize("name", MINI_ACCEPTED)
def test_every_frozen_component_names_a_degradation_the_task_declares(name, mini):
    plan, _gt, _init = mini(name)
    declared = {d["id"] for d in plan["degradations"]}
    assert {c["deg"] for c in plan["components"]} <= declared


@pytest.mark.parametrize("name", MINI_ACCEPTED)
def test_every_frozen_degradation_owns_a_component(name, mini):
    plan, _gt, _init = mini(name)
    owned = {c["deg"] for c in plan["components"]}
    assert {d["id"] for d in plan["degradations"]} <= owned


@pytest.mark.parametrize("name", MINI_REFUSED)
def test_a_rejected_frozen_plan_cannot_be_scored_above_zero(name, mini):
    plan, gt, init = mini(name)
    result = C.score(plan, gt, gt, init)
    assert result["failed_gate"] == "plan_accepted"
    assert result["score"] == 0.0
    assert result["components"], "the breakdown survives a failed gate"


# ---- work the instruction excuses ------------------------------------------ #


def test_a_frozen_plan_that_scores_work_the_instruction_excuses_is_refused(mini):
    """deck0002's instruction ends *"you do not need to re-create any
    animation, only the artwork"* while its plan scores three
    `strip_animation` components worth 0.0801 between them: 8% of that task
    was unreachable by obedience and neither existing gate could see it.

    `mini_excused` is the same shape in the typography bucket — *"You do not
    need to put back any of the fonts or styling anywhere in the deck"*
    against a scored `set_font`.
    """
    plan, _gt, _init = mini("mini_excused")
    hit = [r for r in plan["rejected"] if "the instruction excuses" in r]
    assert len(hit) == 1
    assert "typography" in hit[0] and "the whole deck" in hit[0]
    excused = {c["id"] for c in plan["components"] if c["op"] == "set_font"}
    assert all(c in hit[0] for c in excused)


@pytest.mark.parametrize("name", MINI_ACCEPTED + ("mini_inherited",))
def test_no_frozen_deck_is_refused_for_a_sentence_it_did_not_write(name, mini):
    """The negative control: a check that cried wolf on every instruction
    would be worse than no check."""
    plan, _gt, _init = mini(name)
    assert [r for r in plan["rejected"] if "the instruction excuses" in r] == []


# ---- scope: pages nobody asked about --------------------------------------- #


def test_touching_a_frozen_page_nobody_asked_about_is_a_penalty_not_a_zero(mini):
    """A model that added a logo to two pages whose ground truth has none had
    done 43% of the work; a hard gate recorded 0.0, which is indistinguishable
    from doing nothing and destroys the training signal."""
    plan, gt, init = mini("mini_plain")
    keen = copy.deepcopy(gt)
    spare = next(i for i in range(len(gt["slides"]))
                 if i not in set(plan["damage"]["slides"]))
    keen["slides"][spare]["shapes"].append(_shape("helpfully added"))
    result = C.score(plan, keen, gt, init)
    assert result["failed_gate"] is None
    assert 0.0 < result["score"] < 1.0
    assert "untouched_pages_unchanged" in result["scope_violations"]


def test_what_the_application_writes_by_itself_is_not_a_frozen_scope_violation(mini):
    """`roundtrip_identity`, docs/design/reward.md §5's cheapest probe: the ground truth
    put through the grading application must still score 1.000.  It scored
    0.700–0.850 on nine of ten decks, every point of it the capped
    untouched-page penalty and not one of it damage — WPS materialises
    `a:endParaRPr` on paragraphs that had none, rounds `marL` from 381000 to
    380990, and invents a `fade` transition on a page that had none."""
    plan, gt, init = mini("mini_plain")
    index = _untouched_page(plan, gt)
    for name, mutate in (
            ("endParaRPr", lambda p: p["runs"].append(
                {"t": "", "end": True, "b": "1", "sz": 3200, "font": "Calibri"})),
            ("marL", lambda p: p.update(marL=380990)),
    ):
        rewritten = copy.deepcopy(gt)
        page = rewritten["slides"][index]
        para = next(p for s in page["shapes"] for p in _norm_runs(s))
        mutate(para)
        result = C.score(plan, rewritten, gt, init)
        assert result["scope_violations"] == {}, name
        assert result["score"] == pytest.approx(1.0), name
    invented = copy.deepcopy(gt)
    invented["slides"][index]["transition"] = {
        "type": "fade", "detail": None, "speed": "med", "duration_ms": 700,
        "advance_ms": None, "on_click": True}
    assert C.score(plan, invented, gt, init)["score"] == pytest.approx(1.0)


def test_a_real_edit_to_an_untouched_frozen_page_is_still_caught(mini):
    """The negative control for the one above: narrowing what the gate looks
    at is only safe while it still sees the thing it was written for."""
    plan, gt, init = mini("mini_plain")
    index = _untouched_page(plan, gt)
    for name, mutate in (
            ("moved", lambda page: page["shapes"][0]["bbox"].update(
                cx=page["shapes"][0]["bbox"]["cx"] + C.EMU_PER_INCH)),
            ("added", lambda page: page["shapes"].append(_shape("brand new"))),
            ("deleted", lambda page: page["shapes"].pop(0)),
            ("re-worded", lambda page: next(
                p for s in page["shapes"] for p in _norm_runs(s)).update(t="ZZZ")),
    ):
        meddled = copy.deepcopy(gt)
        mutate(meddled["slides"][index])
        result = C.score(plan, meddled, gt, init)
        assert "untouched_pages_unchanged" in result["scope_violations"], name
        assert 0.0 < result["score"] < 1.0, name


def test_an_image_the_application_re_encoded_on_a_frozen_deck_is_not_a_deletion(mini):
    """`_page_facts` deliberately records *that* a shape draws an image and
    never which bytes, "because the blob is the application's to change" — and
    then filed every fact it recorded under `shape["key"]`, which for a
    picture **is** that blob.  Re-encoding moved the address rather than the
    value, one untouched picture read as a deletion plus an addition, and
    thirteen such pages on deck0003 and six on deck0008 took a perfect deck to
    0.393 and 0.450 through the 0.30 cap.

    LibreOffice re-encodes every image it saves, and a rollout has already
    been seen with the `.pptx` handler bound to Impress.
    """
    plan, gt, init = mini("mini_plain")
    pages = _picture_pages(plan, gt)
    # three untouched pages is already the 0.30 cap at 0.10 apiece, so this is
    # the whole penalty, not a slice of it
    assert len(pages) >= 3
    result = C.score(plan, _re_encode(gt, pages), gt, init)
    assert result["scope_violations"] == {}, result["scope_violations"]
    assert result["score"] == pytest.approx(1.0)


def test_a_real_edit_to_a_picture_on_an_untouched_frozen_page_is_caught(mini):
    """The negative control.  Addressing a shape by the pairing instead of by
    its blob must not make the shape invisible: what it drops is the *bytes*,
    and every other fact about the picture is still compared."""
    plan, gt, init = mini("mini_plain")
    page = _picture_pages(plan, gt)[0]
    for name, mutate in (
            ("moved", lambda s: s["bbox"].update(
                cx=s["bbox"]["cx"] + C.EMU_PER_INCH)),
            ("resized", lambda s: s["bbox"].update(w=s["bbox"]["w"] // 2)),
    ):
        meddled = copy.deepcopy(gt)
        shape = next(s for s in meddled["slides"][page]["shapes"]
                     if (s.get("picture") or {}).get("blob"))
        mutate(shape)
        result = C.score(plan, meddled, gt, init)
        assert "untouched_pages_unchanged" in result["scope_violations"], name
        assert 0.0 < result["score"] < 1.0, name
    gone = copy.deepcopy(gt)
    gone["slides"][page]["shapes"] = [
        s for s in gone["slides"][page]["shapes"]
        if not (s.get("picture") or {}).get("blob")]
    result = C.score(plan, gone, gt, init)
    assert "untouched_pages_unchanged" in result["scope_violations"]


# ---- gates may not overrule a component ------------------------------------ #


@pytest.mark.parametrize("name", MINI_ACCEPTED)
def test_the_hand_rebuild_of_a_frozen_deck_is_not_gated_and_is_paid_for(name, mini):
    """A native-objects gate zeroed a model for rebuilding a SmartArt by hand
    in an evaluator whose own diagram component was at that moment awarding
    0.7 for the same rebuild — and no gate fired on deck0007's hand rebuild
    while it still scored 0.4103, because pairing is one-to-one and a rebuild
    is one-to-many."""
    plan, gt, init = mini(name)
    result = C.score(plan, C._state_rebuilt(plan, gt), gt, init)
    assert result["failed_gate"] is None
    assert result["score"] == pytest.approx(1.0), [
        (c["id"], c["op"], c["score"], c["why"])
        for c in result["components"] if c["score"] < 1.0]


# ---- coherence -------------------------------------------------------------- #


@pytest.mark.parametrize("name", MINI_ACCEPTED)
def test_no_gate_fires_on_frozen_work_a_component_would_reward(name, mini):
    """The check that nothing else performs: a gate and a component in one
    file disagreeing about whether the same state is correct."""
    plan, _gt, _init = mini(name)
    assert plan["coherence"]["failures"] == []
    for state, report in plan["coherence"]["states"].items():
        assert report["failed_gate"] is None, state


@pytest.mark.parametrize("name", MINI_ACCEPTED)
def test_partial_frozen_work_scores_between_nothing_and_everything(name, mini):
    plan, _gt, _init = mini(name)
    assert 0.0 < plan["coherence"]["states"]["half_restore"]["score"] < 1.0


@pytest.mark.parametrize("name", MINI_ACCEPTED)
def test_how_much_of_a_frozen_deck_rides_on_a_coordinate_is_measured(name, mini):
    """deck0009's `c016` is a deleted table worth 0.3934 whose 27 cells the
    instruction gives verbatim and whose centre the bundle discloses nowhere;
    `_facet_centre` is binary, so a perfect table placed 0.02 in out scored
    the deck 0.7377 and so did one placed a foot out.  The instrument is not
    the defect — binary is what stops "paste it roughly there" being cheaper
    than restoring the thing — so the exposure is a number in every plan
    rather than a rubric to loosen."""
    plan, _gt, _init = mini(name)
    slip = plan["coherence"]["states"].get("position_slip")
    assert slip is not None, "the frozen deck must carry a restored box"
    assert slip["failed_gate"] is None
    assert 0.0 <= slip["score"] < 1.0


# ---- determinism ------------------------------------------------------------ #


@pytest.mark.parametrize("name", MINI_ACCEPTED + MINI_REFUSED)
def test_the_frozen_plan_is_deterministic(name, mini):
    """The battery scores against a plan on disk; a plan that changes between
    builds makes every stored result unreproducible."""
    root = mini.root(name)
    first = json.dumps(C.build_plan(root, write=False), sort_keys=True,
                       default=str)
    second = json.dumps(C.build_plan(root, write=False), sort_keys=True,
                        default=str)
    assert first == second


def test_a_frozen_record_that_cannot_be_replayed_rejects_the_plan(monkeypatch,
                                                                 mini):
    """Falling back on the identity is not a fallback here: the identity is a
    claim that no page moved, and the record has just said one did."""
    with pytest.raises(C.Unscorable):
        C._init_slide_of({"deleted_slides": [9]}, 3)
    with pytest.raises(C.Unscorable):
        C._init_slide_of({"reorder_slides": {"swapped": [[1, 9]]}}, 3)

    root = mini.root("mini_plain")
    monkeypatch.setattr(C, "_init_slide_of",
                        lambda delta, n: (_ for _ in ()).throw(
                            C.Unscorable("page 9 of 3")))
    plan = C.build_plan(root, write=False)
    assert plan["init_slide_of"] is None
    assert any("cannot map the broken file's pages" in reason
               for reason in plan["rejected"])
    _, gt, init = mini("mini_plain")
    assert C.score(plan, gt, gt, init)["score"] == 0.0


# --------------------------------------------------------------------------- #
# what stays corpus-bound, and why
#
# `test_a_composite_the_answer_cannot_disambiguate_never_reaches_a_score` and
# `test_a_second_smartart_the_agent_adds_does_not_unscore_the_component` are
# about `_find_smartart`: a component with no `gt_path`, resolved through the
# diagram's data part in `ppt/diagrams/`.  `python-pptx` cannot author a
# SmartArt graphicFrame, so no deck this suite builds has one to resolve, and
# a fake `diagram` dict in an inventory would exercise the comparator while
# skipping the resolver — which is the half that broke.  The *rule* underneath
# them (a component that cannot pass its own answer is dropped and named, and
# a degradation the drop empties refuses the plan) is pinned frozen by
# `test_a_frozen_degradation_the_drop_empties_refuses_the_plan`.
#
# The remaining corpus tests are corpus questions on purpose: whether deck0004
# still drops exactly six components, whether deck0006's probe still
# contradicts its proposer by 8x, whether deck0002 is still the only deck
# whose instruction excuses its own plan.  Those are measurements of the ten
# decks, not of `comparators.py`, and `--corpus` is how to ask them.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# the corpus's own state, pinned as reasons rather than as names
# --------------------------------------------------------------------------- #


@pytest.mark.corpus
def test_the_refused_decks_are_refused_for_the_reasons_recorded_here():
    """`REFUSED` is a claim about the ten decks in `work/`, and this is the
    only test allowed to check it.

    It replaces a hardcoded `ACCEPTED` tuple that listed deck0008 among the
    decks every rule in this file is asserted of.  deck0008 cannot be scored:
    its own answer trips `media_not_pasted`, because the task withholds the
    two original bitmaps and supplies a reference render instead.  That is one
    defect in one task, and it was arriving as five failures that read like
    five regressions in `comparators.py`.

    Now it arrives here, once, by name and by reason — and if somebody fixes
    the deck, this is what tells them to delete the entry.
    """
    refused = {}
    for deck in DECKS:
        rejected = C.build_plan(deck, write=False)["rejected"]
        if rejected:
            refused[deck.name] = rejected
    assert set(refused) == set(REFUSED), (
        f"refused now: {sorted(refused)}; recorded: {sorted(REFUSED)} — "
        f"update REFUSED, do not widen anything")
    for name, cue in REFUSED.items():
        assert any(cue in reason for reason in refused[name]), (
            f"{name} is still refused but no longer for {cue!r}: "
            f"{refused[name]}")


@pytest.mark.corpus
def test_the_deck_the_reward_model_cannot_score_is_named_and_understood():
    """deck0008 in full, because it is the one entry in `REFUSED` that is not
    a task somebody can simply rewrite a sentence of.

    The recipe deletes two shapes that draw `image3.png` and `image4.png`.
    `assets/` supplies `reference-p05.png` — a *render* of the page — and not
    those two files.  So the only way to reach the ground truth is to
    reintroduce blobs the broken file does not have and the task did not
    supply, which is exactly what `_gate_media_not_pasted` exists to refuse.
    Nine of the ten decks ship every gt-only blob byte for byte; this one does
    not, and no tolerance can bridge it — the gate is right and the deck is
    wrong.
    """
    root = WORK / "deck0008"
    if not (root / "delta.json").exists():
        pytest.skip("deck0008 is not in this checkout")
    plan = C.build_plan(root, write=False)
    gt = inventory_pptx(root / "source.pptx")
    init = inventory_pptx(root / "input.pptx")
    withheld = (set(gt["package"]["media"]) - set(init["package"]["media"])
                - set(plan["assets_sha"] or ()))
    assert withheld, (
        "deck0008 now supplies every blob its answer needs — the deck has been "
        "repaired, so drop it from REFUSED and from this test")
    assert C.score({**plan, "rejected": []}, gt, gt,
                   init)["failed_gate"] == "media_not_pasted"
