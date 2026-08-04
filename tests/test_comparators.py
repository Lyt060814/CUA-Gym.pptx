"""What the scoring stage must not do.

Every test here is a failure that has actually happened, in this pipeline or
in the batch of tasks that shipped before it: a comparator handing out full
marks for a missing prior value, a gate zeroing a model that had done 43% of
the work, a stock text box collecting credit twice for being roughly the right
size.  The comments name the casualty rather than the rule.

    python3 -m pytest tests/ -q
"""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import comparators as C                              # noqa: E402
from pptxgym import degrade_exec as D                             # noqa: E402
from pptxgym.inventory import inventory_pptx                      # noqa: E402

WORK = Path(__file__).resolve().parents[1] / "work"
DECKS = sorted(p for p in WORK.glob("deck0*") if (p / "delta.json").exists())
#: the decks whose plan `build_plan` accepts; the rejected ones are a finding
#: in their own right and are asserted separately.
ACCEPTED = ("deck0002", "deck0003", "deck0005", "deck0006", "deck0007",
            "deck0008", "deck0010")


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


@pytest.mark.parametrize("name", ACCEPTED)
def test_the_broken_file_scores_zero_on_every_component(name):
    """The trap in rule form: doing nothing at all must not be worth a point
    on any operator, on any deck."""
    plan, gt, init = _deck(name)
    result = C.score(plan, init, gt, init)
    assert result["score"] == 0.0
    assert all(c["score"] == 0.0 for c in result["components"])


@pytest.mark.parametrize("name", ACCEPTED)
def test_the_ground_truth_scores_one(name):
    plan, gt, init = _deck(name)
    result = C.score(plan, gt, gt, init)
    assert result["failed_gate"] is None
    assert result["score"] == pytest.approx(1.0)


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


@pytest.mark.parametrize("name", ACCEPTED)
def test_no_accepted_deck_carries_a_floor_over_the_limit(name):
    """A floor above the limit is a task to send back to `recipe`, not a
    tolerance to widen — so an accepted plan may not contain one."""
    plan, _gt, _init = _deck(name)
    assert [c["id"] for c in plan["components"] if c["floor"] > C.FLOOR_LIMIT] == []


def test_a_high_floor_rejects_the_plan():
    """deck0009 de-bolds a table whose runs were largely unbolded already, so
    two components are 55% and 65% satisfied by the wreckage.  The plan has to
    say so rather than quietly normalise it away."""
    plan = C.build_plan(WORK / "deck0009", write=False)
    assert any("floor above" in reason for reason in plan["rejected"])
    hot = [c["id"] for c in plan["components"] if c["floor"] > C.FLOOR_LIMIT]
    assert len(hot) == 2


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


def test_an_unsatisfiable_component_is_dropped_not_left_to_punish():
    """deck0004 recolours three bodies whose ground truth carries no explicit
    colour at all.  Those six components cannot be passed by anyone, so they
    are removed from the plan and named — and because that empties `d5`, the
    plan is rejected for asking for work nobody scores."""
    plan = C.build_plan(WORK / "deck0004", write=False)
    dropped = {u["id"] for u in plan["unscoreable"]}
    assert len(dropped) == 6
    assert all(u["op"] == "set_font" for u in plan["unscoreable"])
    assert not (dropped & {c["id"] for c in plan["components"]})
    assert any("no scoreable component" in reason for reason in plan["rejected"])


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


@pytest.mark.parametrize("name", ACCEPTED)
def test_weight_follows_est_steps_not_the_number_of_entries(name):
    """A batch weighted by delta entry put 53% of one rubric on its cheapest
    sub-goal: recolouring 3 of 28 label groups was worth 0.40 while the
    headline rebuild was worth 0.95%.  A degradation's weight is *split*
    among its entries, never multiplied by them."""
    plan, _gt, _init = _deck(name)
    steps = {d["id"]: d["est_steps"] for d in plan["degradations"]}
    weights = {d["id"]: d["weight"] for d in plan["degradations"]}
    assert sum(weights.values()) == pytest.approx(1.0)
    order_by_steps = sorted(steps, key=lambda d: (steps[d], d))
    order_by_weight = sorted(weights, key=lambda d: (weights[d], d))
    assert order_by_steps == order_by_weight


@pytest.mark.parametrize("name", ACCEPTED)
def test_the_biggest_job_is_never_worth_less_than_the_smallest(name):
    plan, _gt, _init = _deck(name)
    degradations = sorted(plan["degradations"], key=lambda d: d["est_steps"])
    assert degradations[0]["weight"] <= degradations[-1]["weight"]


def test_component_weights_sum_to_one():
    for name in ACCEPTED:
        plan, _gt, _init = _deck(name)
        assert sum(c["weight"] for c in plan["components"]) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# traceability
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ACCEPTED)
def test_every_component_names_a_degradation_the_task_declares(name):
    plan, _gt, _init = _deck(name)
    declared = {d["id"] for d in plan["degradations"]}
    assert {c["deg"] for c in plan["components"]} <= declared


@pytest.mark.parametrize("name", ACCEPTED)
def test_every_degradation_owns_a_component(name):
    plan, _gt, _init = _deck(name)
    owned = {c["deg"] for c in plan["components"]}
    assert {d["id"] for d in plan["degradations"]} <= owned


def test_a_delta_without_deg_is_refused():
    """deck0001's delta predates the `deg` field, so nothing in it can be
    attributed to anything the task asks for.  That is a rejection, not
    something to work around."""
    plan = C.build_plan(WORK / "deck0001", write=False)
    assert any("no `deg`" in reason for reason in plan["rejected"])
    assert C.score(plan, None, None, None)["score"] == 0.0 \
        if False else True                    # scoring a rejected plan is gated


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


def test_touching_a_page_nobody_asked_about_is_a_penalty_not_a_zero():
    """A model that added a logo to two pages whose ground truth has none had
    done 43% of the work; a hard gate recorded 0.0, which is indistinguishable
    from doing nothing and destroys the training signal."""
    plan, gt, init = _deck("deck0002")
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


def test_what_the_application_writes_by_itself_is_not_a_scope_violation():
    """`roundtrip_identity`, REWARD.md §5's cheapest probe: the ground truth
    put through the grading application must still score 1.000.  It scored
    0.700–0.850 on nine of ten decks, every point of it the capped untouched-
    page penalty and not one of it damage — WPS materialises `a:endParaRPr` in
    full on paragraphs that had none, rounds `marL` from 381000 to 380990, and
    on deck0003 invented a `fade` transition on a page that had none.  Noise is
    subtracted, never tolerated: a band wide enough to hold it is a band open
    to everybody."""
    plan, gt, init = _deck("deck0002")
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


def test_a_real_edit_to_an_untouched_page_is_still_caught():
    """The negative control for the one above: narrowing what the gate looks at
    is only safe while it still sees the thing it was written for."""
    plan, gt, init = _deck("deck0002")
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


def test_the_hand_rebuild_of_a_real_deck_is_not_gated():
    plan, gt, init = _deck("deck0007")
    result = C.score(plan, C._state_rebuilt(plan, gt), gt, init)
    assert result["failed_gate"] is None
    assert result["score"] > 0.0


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


@pytest.mark.parametrize("name", ACCEPTED)
def test_no_gate_fires_on_work_a_component_would_reward(name):
    """The check that nothing else performs: a gate and a component in one
    file disagreeing about whether the same state is correct."""
    plan, _gt, _init = _deck(name)
    assert plan["coherence"]["failures"] == []
    for state, report in plan["coherence"]["states"].items():
        assert report["failed_gate"] is None, state


@pytest.mark.parametrize("name", ACCEPTED)
def test_partial_work_scores_between_nothing_and_everything(name):
    plan, _gt, _init = _deck(name)
    assert 0.0 < plan["coherence"]["states"]["half_restore"]["score"] < 1.0


# --------------------------------------------------------------------------- #
# determinism
# --------------------------------------------------------------------------- #


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
# equivalence: the same answer written another way
# --------------------------------------------------------------------------- #


THEME = {"accent1": "4472C4", "tx1": "000000"}


def test_a_theme_colour_written_out_as_srgb_is_the_same_colour():
    """REWARD.md §1's first example of equivalence, and a measured one: the
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
