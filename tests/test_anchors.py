"""A component may not score a property the bundle never discloses.

`materialise` used to decide what to hand over from `proposal.json` — an
agent's judgement about what the task needs.  `scored` decides what to grade
from `delta.json`.  Two sources, and they drift in one direction: the plan
grades a coordinate, the bundle discloses it nowhere, and a solver doing the
work correctly cannot know what to aim for.  deck0009's `c016` puts 39.3% of
that deck's reward on a table centre nothing in the bundle states.

The rule here closes it from the producer's side, and the half worth reading
twice is *how it decides what is scored*: it does not consult a table of
operators, it moves the ground-truth shape four tolerances and asks the real
comparator whether the score falls.  A table would drift from
`comparators.py` in silence.  A measurement cannot.

    python3 -m pytest tests/test_anchors.py -q
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import assets                                        # noqa: E402
from pptxgym import comparators as C                              # noqa: E402
from pptxgym import pipeline as pl                                # noqa: E402


def _deck(mini, name):
    return pl.Deck(mini.root(name))


def _delta(deck):
    return json.loads((deck.root / "delta.json").read_text())


# --------------------------------------------------------------------------- #
# the seam with `build_plan`
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["mini_plain", "mini_picture", "mini_measured"])
def test_the_components_measured_are_the_components_scored(mini, name):
    """The one thing `assets` reimplements, pinned so it cannot drift.

    `_graded_components` walks `comparators._entries` to build the same list
    `build_plan` builds, because materialise runs at stage 6 and `plan.json` is
    not written until stage 9 — there is no plan to read.  If these two ever
    disagree, the anchor rule is anchoring something other than what is scored,
    which is the exact failure it exists to prevent.
    """
    deck = _deck(mini, name)
    plan, _gt, _init = mini(name)
    mine = assets._graded_components(_delta(deck))
    theirs = plan["components"]

    # `build_plan` drops the components even the ground truth cannot satisfy,
    # so its list is a subset — every one it keeps has to be here, identical.
    mine_by_id = {c["id"]: c for c in mine}
    assert theirs, "the fixture deck scores nothing; the pin would be vacuous"
    for want in theirs:
        got = mine_by_id[want["id"]]
        for field in ("op", "slide", "gt_path", "deg", "spec"):
            assert got[field] == want[field], f"{want['id']}.{field}"


# --------------------------------------------------------------------------- #
# what counts as scored
# --------------------------------------------------------------------------- #


def test_a_restored_shape_is_scored_on_where_it_ends_up(mini):
    """`_cmp_restored_shape` is `content × position`, so the coordinate is not
    a detail of the mark — it is half of it."""
    deck = _deck(mini, "mini_picture")
    graded, _gt = assets.graded_geometry(deck)
    ops = {g["op"] for g in graded}
    assert "delete" in ops
    assert all(g["scores_at_2_tol"] < 1.0 for g in graded)


def test_a_component_that_does_not_care_where_the_shape_is_is_not_listed(mini):
    """Recolouring a shape scores its fill, wherever the shape sits.  Listing
    it would demand an anchor for a coordinate nobody grades, and every
    unnecessary disclosure is difficulty given away."""
    deck = _deck(mini, "mini_picture")
    graded, _gt = assets.graded_geometry(deck)
    listed = {g["op"] for g in graded}
    assert "set_font" not in listed
    assert "clear_text" not in listed


def test_the_verdict_comes_from_the_comparator_and_not_from_a_table(
        mini, monkeypatch):
    """The anti-drift property, demonstrated rather than asserted: make the
    comparator indifferent to position and the rule stops asking for anchors,
    with no edit here."""
    deck = _deck(mini, "mini_picture")
    assert assets.graded_geometry(deck)[0], "nothing to lose in the first place"

    monkeypatch.setattr(C, "_facet_centre",
                        lambda gt_shape, shape: (1.0, "position ignored"))
    monkeypatch.setattr(C, "_facet_extent",
                        lambda gt_shape, shape: (1.0, "size ignored"))
    assert assets.graded_geometry(deck)[0] == []


# --------------------------------------------------------------------------- #
# what counts as disclosed
# --------------------------------------------------------------------------- #


BOX = {"cx": 4_000_000, "cy": 2_000_000, "w": 1_000_000, "h": 500_000,
       "rot": 0.0, "flip": False}


def _survivor(path, **over):
    return {"_path": path, "bbox": {**BOX, **over}, "keys": ["kind:x"]}


def test_a_surviving_shape_in_the_same_box_is_an_anchor():
    """deck0001's logo sits in the identical slot on eight surviving slides;
    deck0009's slide 12 is an undamaged twin of slide 11's annotation layer."""
    how = assets.anchor_for(BOX, [_survivor("2")], C.POS_TOL)
    assert how and "twin box" in how


def test_a_shape_a_hair_out_of_place_is_not_a_twin():
    off = _survivor("2", cx=BOX["cx"] + 3 * C.POS_TOL)
    assert assets.anchor_for(BOX, [off], C.POS_TOL) is None


def test_four_coordinates_from_four_neighbours_are_an_anchor():
    """deck0003's `d4`: its own column's surviving boxes fix `cx`, `w` and
    `h`, and the parallel column's row at the same index fixes `cy`.  No single
    survivor reproduces the box; between them they state every number in it.
    The solvability probe reached this in prose and called the degradation
    determinate."""
    # its own column: same `cx`, same `w`/`h`, a row further down.  Three of
    # the four numbers, and `cy` is the one nothing here states.
    same_column = _survivor("2", cy=BOX["cy"] + 900_000)
    assert assets.anchor_for(BOX, [same_column], C.POS_TOL) is None

    # the parallel column's row at the same index: a different `cx` and a
    # different width, and exactly the `cy` that was missing.
    parallel_row = _survivor("3", cx=BOX["cx"] + 3_000_000,
                             w=BOX["w"] + 400_000, h=BOX["h"] + 100_000)
    how = assets.anchor_for(BOX, [same_column, parallel_row], C.POS_TOL)
    assert how and "every coordinate" in how


def test_a_size_nothing_reproduces_is_not_anchored():
    """deck0003's `d1`, in miniature: the row twins fix everything but the two
    bottom cells' extent, and the one bottom-row survivor is a different size —
    so 'copy a sibling' names three different answers."""
    elsewhere = _survivor("2", cx=BOX["cx"] + 5_000_000,
                          cy=BOX["cy"] + 3_000_000)
    assert assets.anchor_for(BOX, [elsewhere], C.POS_TOL) is None


# --------------------------------------------------------------------------- #
# what gets shipped
# --------------------------------------------------------------------------- #


def test_an_unanchored_coordinate_is_shipped_as_a_frame(mini, tmp_path):
    deck = _deck(mini, "mini_picture")
    audit = assets.anchor_pass(deck, _delta(deck), [], tmp_path)
    assert audit["graded"] > 0
    assert audit["shipped"], "a graded coordinate went out with no anchor"
    for item in audit["shipped"]:
        assert (tmp_path / item["file"]).exists()
        assert item["kind"] == "frames"


def test_a_slide_that_already_has_a_reference_render_is_left_alone(
        mini, tmp_path):
    """Anchoring an anchored slide is difficulty given away for nothing."""
    deck = _deck(mini, "mini_picture")
    bare = assets.anchor_pass(deck, _delta(deck), [], tmp_path)
    pages = {s["slide"] for s in bare["shipped"]}
    assert pages

    supplied = [{"kind": "reference_image", "slide": p, "file": f"r{p}.png"}
                for p in pages]
    after = assets.anchor_pass(deck, _delta(deck), supplied, tmp_path)
    assert after["shipped"] == []
    by_page = {a["slide"]: a["by"] for a in after["anchored"]}
    assert {by_page[p] for p in pages} == {"reference render"}


def test_the_frame_is_exact_enough_to_earn_the_mark_it_unlocks(mini, tmp_path):
    """The reason this ships numbers and not a masked render.  The mask is
    padded 0.06in and drawn at 130 dpi, so a hatch box read off one is good to
    roughly 0.06in — six times `POS_TOL`, which is the tolerance the position
    facet is binary at.  An anchor coarser than the tolerance it exists to let
    somebody hit is not an anchor.
    """
    deck = _deck(mini, "mini_picture")
    graded, gt_inv = assets.graded_geometry(deck)
    audit = assets.anchor_pass(deck, _delta(deck), [], tmp_path)

    wrote = {}
    for item in audit["shipped"]:
        with open(tmp_path / item["file"], newline="") as fh:
            for row in list(csv.DictReader(fh)):
                wrote[(item["slide"], row["frame"])] = row

    want = []
    for g in graded:
        box = assets._box_of(gt_inv, g["slide"], g["gt_path"])
        if box and any(s["slide"] == g["slide"] for s in audit["shipped"]):
            want.append((g["slide"], box))
    assert want

    for page, box in want:
        rows = [r for (p, _f), r in wrote.items() if p == page]
        best = min(rows, key=lambda r: abs(float(r["left_in"]) * assets.EMU
                                           - (box["cx"] - box["w"] / 2)))
        assert abs(float(best["left_in"]) * assets.EMU
                   - (box["cx"] - box["w"] / 2)) < C.POS_TOL
        assert abs(float(best["top_in"]) * assets.EMU
                   - (box["cy"] - box["h"] / 2)) < C.POS_TOL
        assert abs(float(best["width_in"]) * assets.EMU - box["w"]) < C.POS_TOL
        assert abs(float(best["height_in"]) * assets.EMU - box["h"]) < C.POS_TOL


def test_the_frame_says_where_and_never_what(mini, tmp_path):
    """The geometry half is the half nothing else discloses.  The content half
    — which element belongs in which frame — is the work, and handing it over
    would trade one unearnable task for one not worth setting."""
    deck = _deck(mini, "mini_picture")
    audit = assets.anchor_pass(deck, _delta(deck), [], tmp_path)
    delta = _delta(deck)
    words = {str(e.get("name") or "") for page in (delta.get("slides") or {}).values()
             for e in page}
    words |= {str(e.get("text") or "") for page in (delta.get("slides") or {}).values()
              for e in page}
    words = {w for w in words if len(w) > 3}

    for item in audit["shipped"]:
        body = (tmp_path / item["file"]).read_text()
        assert "frame-1" in body
        for word in words:
            assert word not in body, f"{item['file']} names {word!r}"


def test_materialise_records_the_audit_where_the_next_gate_reads_it(
        mini, tmp_path, monkeypatch):
    """A silent producer is how the last one of these went unnoticed for a
    corpus.  The manifest carries the count, what anchored each component, and
    what had to be shipped."""
    deck = _deck(mini, "mini_picture")
    manifest = assets.materialise(deck)
    assert "anchors" in manifest
    audit = manifest["anchors"]
    assert audit["graded"] >= 1
    assert not audit.get("error"), audit.get("error")
    names = {p.get("file") for p in manifest["produced"]}
    for item in audit["shipped"]:
        assert item["file"] in names, "shipped but not in the delivery list"


def test_an_audit_that_cannot_run_is_recorded_as_unmet_not_swallowed(
        mini, monkeypatch):
    """It needs both inventories and the whole comparator stack.  A failure
    must not stop the stage — and must not pass silently either, or the deck
    ships with nobody having asked the question."""
    deck = _deck(mini, "mini_picture")
    monkeypatch.setattr(assets, "anchor_pass",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    manifest = assets.materialise(deck)
    assert manifest["anchors"]["error"].startswith("RuntimeError")
    assert any(u["kind"] == "anchor" for u in manifest["unmet"])

