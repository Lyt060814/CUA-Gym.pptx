"""What the adversarial battery has to get right before it may reject anything.

Every case here is a way the battery could report a clean sweep it did not
earn: an attack that quietly built nothing, a gate that was never fired but was
counted as passed, a monotonicity check that restored zero components and then
congratulated itself for scoring like `noop`.  A battery that lies in the safe
direction is worse than no battery, because it is the thing standing between a
hackable task and the training set.

    python3 -m pytest tests/test_attacks.py -q
"""

import json
import sys
import types
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import attacks as at                                 # noqa: E402

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
SLIDE_CT = ("application/vnd.openxmlformats-officedocument."
            "presentationml.slide+xml")


# --------------------------------------------------------------------------- #
# a deck small enough to reason about
# --------------------------------------------------------------------------- #


def _sp(shape_id: int, name: str, x: int, y: int, text: str) -> str:
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/>'
            f'<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>'
            f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="900000" cy="500000"/>'
            f'</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
            f'<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{text}</a:t>'
            f'</a:r></a:p></p:txBody></p:sp>')


def _group(shape_id: int, child: str) -> str:
    return (f'<p:grpSp><p:nvGrpSpPr><p:cNvPr id="{shape_id}" name="Group"/>'
            f'<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm>'
            f'<a:off x="0" y="0"/><a:ext cx="900000" cy="500000"/>'
            f'<a:chOff x="0" y="0"/><a:chExt cx="900000" cy="500000"/>'
            f'</a:xfrm></p:grpSpPr>{child}</p:grpSp>')


def _slide(body: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"><p:cSld>'
            f'<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/>'
            f'<p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>{body}</p:spTree></p:cSld>'
            f'<p:clrMapOvr/></p:sld>').encode()


def _write_pptx(path: Path, slides: list[bytes]) -> None:
    ids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{i + 1}"/>'
                  for i in range(len(slides)))
    rels = "".join(
        f'<Relationship Id="rId{i + 1}" Type="{R}/slide" '
        f'Target="slides/slide{i + 1}.xml"/>' for i in range(len(slides)))
    overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{i + 1}.xml" '
        f'ContentType="{SLIDE_CT}"/>' for i in range(len(slides)))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml",
                   f'<Types xmlns="{CT}">'
                   f'<Default Extension="rels" ContentType="application/'
                   f'vnd.openxmlformats-package.relationships+xml"/>'
                   f'<Default Extension="xml" ContentType="application/xml"/>'
                   f'<Override PartName="/ppt/presentation.xml" ContentType='
                   f'"application/vnd.openxmlformats-officedocument.'
                   f'presentationml.presentation.main+xml"/>{overrides}</Types>')
        z.writestr("_rels/.rels",
                   f'<Relationships xmlns="{PR}"><Relationship Id="rId1" '
                   f'Type="{R}/officeDocument" Target="ppt/presentation.xml"/>'
                   f'</Relationships>')
        z.writestr("ppt/presentation.xml",
                   f'<p:presentation xmlns:p="{P}" xmlns:r="{R}">'
                   f'<p:sldIdLst>{ids}</p:sldIdLst>'
                   f'<p:sldSz cx="9144000" cy="6858000"/></p:presentation>')
        z.writestr("ppt/_rels/presentation.xml.rels",
                   f'<Relationships xmlns="{PR}">{rels}</Relationships>')
        for index, body in enumerate(slides, start=1):
            z.writestr(f"ppt/slides/slide{index}.xml", body)
            z.writestr(f"ppt/slides/_rels/slide{index}.xml.rels",
                       f'<Relationships xmlns="{PR}"></Relationships>')


def make_deck(tmp_path: Path, weights=None) -> at.Ctx:
    """A five-page deck with four one-shape degradations, one per page.

    Four so that "half" is a real choice, on four different pages so that the
    page-disjoint merge does not collapse them into one unit.
    """
    root = tmp_path / "deck9999"
    root.mkdir()
    shapes = [_sp(2, f"Box {i}", 100000 * (i + 1), 200000, f"Text{i}")
              for i in range(4)]
    intact = [_slide(_sp(2, "Keep", 500000, 500000, "Intact"))] + [
        _slide(shape + _sp(9, f"Neighbour {i}", 3000000, 3000000, f"N{i}"))
        for i, shape in enumerate(shapes)]
    broken = [intact[0]] + [
        _slide(_sp(9, f"Neighbour {i}", 3000000, 3000000, f"N{i}"))
        for i in range(4)]
    _write_pptx(root / "source.pptx", intact)
    _write_pptx(root / "input.pptx", broken)
    delta = {"gt": "source.pptx", "input": "input.pptx", "recipe": "synthetic",
             "dropped_rels": {}, "slides": {}}
    for i in range(4):
        delta["slides"][str(i + 1)] = [{
            "path": "0", "op": "delete", "shape_id": 2, "name": f"Box {i}",
            "text": f"Text{i}", "kind": "autoshape", "deg": f"d{i + 1}",
            "removed_xml": shapes[i],
            "box": [100000 * (i + 1), 200000, 900000, 500000]}]
    (root / "delta.json").write_text(json.dumps(delta))
    ctx = at.Ctx.load(root, tmp_path / "scratch")
    ctx.weights = weights or {}
    return ctx


def _filled(shape_id: int, name: str, rgb: str) -> str:
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/>'
            f'<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>'
            f'<a:xfrm><a:off x="0" y="0"/><a:ext cx="900000" cy="500000"/>'
            f'</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            f'<a:solidFill><a:srgbClr val="{rgb}"/></a:solidFill></p:spPr>'
            f'<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>T</a:t></a:r>'
            f'</a:p></p:txBody></p:sp>')


def _table(shape_id: int, rows: list[list[str]]) -> str:
    body = ""
    for row in rows:
        cells = "".join(
            f'<a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{text}'
            f'</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc>' for text in row)
        body += f'<a:tr h="200000">{cells}</a:tr>'
    grid = "".join('<a:gridCol w="300000"/>' for _ in rows[0])
    return (f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{shape_id}" '
            f'name="Table {shape_id}"/><p:cNvGraphicFramePr/><p:nvPr/>'
            f'</p:nvGraphicFramePr><p:xfrm><a:off x="0" y="0"/>'
            f'<a:ext cx="4000000" cy="2000000"/></p:xfrm><a:graphic>'
            f'<a:graphicData uri="http://schemas.openxmlformats.org/'
            f'drawingml/2006/table"><a:tbl><a:tblPr/><a:tblGrid>{grid}'
            f'</a:tblGrid>{body}</a:tbl></a:graphicData></a:graphic>'
            f'</p:graphicFrame>')


def one_op_deck(tmp_path: Path, name: str, body: str, entries: list[dict],
                plan_components=None) -> at.Ctx:
    """A one-page deck whose ground truth holds `body` and whose delta says
    `entries` were done to it.  `input.pptx` is the same page emptied, which is
    enough for `wrong_params`: it builds off the ground truth and never reads
    the broken file."""
    root = tmp_path / name
    root.mkdir()
    _write_pptx(root / "source.pptx", [_slide(body)])
    _write_pptx(root / "input.pptx", [_slide("")])
    (root / "delta.json").write_text(json.dumps({"slides": {"0": entries}}))
    ctx = at.Ctx.load(root, tmp_path / f"scratch-{name}")
    ctx.plan_components = plan_components or []
    return ctx


# --------------------------------------------------------------------------- #
# the endpoints have to be the endpoints
# --------------------------------------------------------------------------- #


def test_noop_is_the_broken_file_byte_for_byte(tmp_path):
    """`noop` is the calibration point the whole floor rests on.  Rebuilding it
    through any library rewrites every part, and then a `noop` that scores
    0.002 is indistinguishable from a comparator with a real floor."""
    ctx = make_deck(tmp_path)
    built = at.ATTACKS["noop"].build(ctx, tmp_path / "noop.pptx")
    assert built.path.read_bytes() == ctx.input_path.read_bytes()


def test_gt_is_the_ground_truth_byte_for_byte(tmp_path):
    """Same reason at the other end: a `gt` that has been through a serialiser
    tests the serialiser as much as the comparator."""
    ctx = make_deck(tmp_path)
    built = at.ATTACKS["gt"].build(ctx, tmp_path / "gt.pptx")
    assert built.path.read_bytes() == ctx.gt_path.read_bytes()


def test_a_package_read_and_written_keeps_every_other_part(tmp_path):
    """A candidate must differ from its base only where the attack touched it.
    Otherwise every failure needs an argument about who changed what."""
    ctx = make_deck(tmp_path)
    pkg = at.Pkg(ctx.input_path)
    out = pkg.save(tmp_path / "copy.pptx")
    before, after = at.Pkg(ctx.input_path), at.Pkg(out)
    assert before.names() == after.names()
    assert all(before.read(n) == after.read(n) for n in before.names())


# --------------------------------------------------------------------------- #
# an attack that did nothing must not look like an attack that failed to score
# --------------------------------------------------------------------------- #


def test_page_delete_really_removes_the_pages(tmp_path):
    """The whole point of the attack is that the damaged page is gone.  An
    attack that edits `sldIdLst` and leaves the part behind still has the page
    in the inventory, and then a comparator that would have missed the deletion
    is recorded as having caught it."""
    ctx = make_deck(tmp_path)
    built = at.ATTACKS["page_delete"].build(ctx, tmp_path / "pd.pptx")
    assert built.facts == {"before": 5, "after": 1}
    got = at.Pkg(built.path)
    assert len(got.slide_parts()) == 1
    assert not [n for n in got.names() if n.endswith("slide2.xml")]


def test_clone_spam_puts_a_clone_in_every_hole(tmp_path):
    """If the clone lands anywhere but the hole, the `no_extra_shapes` gate
    catches it for the wrong reason and the attack proves nothing about whether
    filling a hole with junk earns credit."""
    ctx = make_deck(tmp_path)
    built = at.ATTACKS["clone_spam"].build(ctx, tmp_path / "cs.pptx")
    assert built.facts["filled"] == 4
    got = at.Pkg(built.path)
    boxes = {tuple(e["box"]) for e in ctx.deletions()}
    placed = {at._get_box(shape)
              for part in got.slide_parts()
              for shape in at._shape_children(got.sp_tree(part))}
    assert boxes <= placed


def test_a_pasted_picture_lands_on_top_of_the_page(tmp_path):
    """`screenshot_paste` is only a screenshot paste if it is *over* the page.
    Appended anywhere but last in the spTree it sits under the survivors, and a
    comparator's overlay gate is never actually fired."""
    ctx = make_deck(tmp_path)
    pkg = ctx.open_input()
    part = pkg.slide_parts()[1]
    before = len(at._shape_children(pkg.sp_tree(part)))
    at.add_picture(pkg, part, b"\x89PNG\r\n\x1a\n", (0, 0, 9144000, 6858000),
                   "overlay")
    out = pkg.save(tmp_path / "shot.pptx")
    got = at.Pkg(out)
    kids = at._shape_children(got.sp_tree(part))
    assert len(kids) == before + 1
    assert kids[-1].tag == at.q("p:pic")
    assert at._get_box(kids[-1]) == (0, 0, 9144000, 6858000)
    assert any("/media/" in n for n in got.names())
    assert 'Extension="png"' in got.read("[Content_Types].xml").decode()


def test_damage_untouched_breaks_a_page_the_task_never_mentions(tmp_path):
    """Aimed at a damaged page it is not this attack at all — it is a worse
    `noop`, and "breaking a bystander earns nothing" would go untested."""
    ctx = make_deck(tmp_path)
    built = at.ATTACKS["damage_untouched"].build(ctx, tmp_path / "du.pptx")
    assert built.facts["page"] not in ctx.damaged_slides()
    assert built.facts["after"] < built.facts["before"]


# --------------------------------------------------------------------------- #
# `wrong_params` may not be paid for ground it did not touch
#
# The attack builds the *ground truth* and then puts every graded value wrong.
# An operator it has no branch for is therefore left at its correct value: that
# component scores 1.0 and pays the attack its full weight, inside a row whose
# verdict says the task cannot be cheated.  deck0004 scored 0.105 and deck0009
# 0.142 against a 0.300 bar for exactly that reason — 12.5% and 14.8% of those
# decks' reward mass never tested by the attack that exists to test it — the
# evidence string said `not perturbed: [...]` and nothing read it, and both
# decks shipped.
# --------------------------------------------------------------------------- #


def test_an_operator_with_no_perturbation_branch_rejects_the_deck(tmp_path):
    """The headline case.  `rotate` stands in for `recolor` and
    `table_drop_rows` as they were: an op in the executor's registry with no
    branch here."""
    ctx = one_op_deck(
        tmp_path, "deck9001",
        _sp(2, "Box", 0, 0, "A") + _filled(3, "Dial", "FF0000"),
        [{"path": "0", "op": "delete", "deg": "d1", "kind": "autoshape",
          "box": [0, 0, 900000, 500000]},
         {"path": "1", "op": "rotate", "deg": "d2", "kind": "autoshape",
          "box": [0, 0, 900000, 500000]}],
        plan_components=[
            {"id": "c001", "deg": "d1", "op": "delete", "slide": 0,
             "gt_path": "0", "weight": 0.7, "spec": {"path": "0"}},
            {"id": "c002", "deg": "d2", "op": "rotate", "slide": 0,
             "gt_path": "1", "weight": 0.3, "spec": {"path": "1"}}])
    assert "rotate" not in at.PERTURB, "pick another op for this test"
    with pytest.raises(at.Unconstructible) as error:
        at.ATTACKS["wrong_params"].build(ctx, tmp_path / "wp.pptx")
    why = str(error.value)
    assert "d2/rotate" in why and "0.3000" in why, why
    assert "no perturbation branch" in why


def test_an_unperturbable_operator_is_a_rejection_and_not_a_footnote(tmp_path):
    """`unconstructible` is the verdict, so the deck is rejected — the same
    outcome as any other gate that was never fired.  Previously the row scored,
    passed its `<= 0.300` bar with the untested weight in it, and the skip
    survived only as prose."""
    ctx = one_op_deck(
        tmp_path, "deck9002", _sp(2, "Box", 0, 0, "A") + _filled(3, "D", "FF0000"),
        [{"path": "0", "op": "delete", "deg": "d1", "kind": "autoshape",
          "box": [0, 0, 900000, 500000]},
         {"path": "1", "op": "rotate", "deg": "d2", "kind": "autoshape"}],
        plan_components=[
            {"id": "c001", "deg": "d1", "op": "delete", "slide": 0,
             "gt_path": "0", "weight": 0.7, "spec": {"path": "0"}},
            {"id": "c002", "deg": "d2", "op": "rotate", "slide": 0,
             "gt_path": "1", "weight": 0.3, "spec": {"path": "1"}}])
    built = at.build_all(ctx, tmp_path / "out", ["wrong_params"])
    row = built["wrong_params"]
    assert row.status == "unconstructible"
    assert at.Report(ctx.name, [], [row]).rejected


def test_a_component_the_plan_dropped_does_not_reject_the_deck(tmp_path):
    """The other direction, and the reason this is weight-aware rather than a
    count.  `build_plan` drops components the ground truth itself cannot
    satisfy — six of deck0004's `set_font` entries — and those carry no reward,
    so failing to put a wrong value in one costs nobody anything.  Rejecting on
    them would be the "good deck rejected" failure this battery also has to
    avoid."""
    ctx = one_op_deck(
        tmp_path, "deck9003", _sp(2, "Box", 0, 0, "A") + _filled(3, "D", "FF0000"),
        [{"path": "0", "op": "delete", "deg": "d1", "kind": "autoshape",
          "box": [0, 0, 900000, 500000]},
         {"path": "1", "op": "rotate", "deg": "d2", "kind": "autoshape"}],
        plan_components=[
            {"id": "c001", "deg": "d1", "op": "delete", "slide": 0,
             "gt_path": "0", "weight": 1.0, "spec": {"path": "0"}}])
    built = at.ATTACKS["wrong_params"].build(ctx, tmp_path / "wp.pptx")
    assert built.facts["untested_weight"] == 0.0
    assert [m["op"] for m in built.facts["unperturbed"]] == ["rotate"]
    assert "unscoreable" in built.evidence


def test_with_no_plan_every_entry_counts_as_graded(tmp_path):
    """`--no-score`, a unit test, a plan that failed to load: not knowing which
    entries carry reward may not read as "none of them do"."""
    ctx = one_op_deck(
        tmp_path, "deck9004", _sp(2, "Box", 0, 0, "A") + _filled(3, "D", "FF0000"),
        [{"path": "0", "op": "delete", "deg": "d1", "kind": "autoshape",
          "box": [0, 0, 900000, 500000]},
         {"path": "1", "op": "rotate", "deg": "d2", "kind": "autoshape"}])
    with pytest.raises(at.Unconstructible) as error:
        at.ATTACKS["wrong_params"].build(ctx, tmp_path / "wp.pptx")
    assert "weight unknown" in str(error.value)


def test_wrong_params_gives_a_recolour_a_colour_the_answer_is_not(tmp_path):
    """deck0004's gap.  `_cmp_recolor` compares the fill and nothing else, so
    the wrong value is any fill the ground truth's is not — and it must not be
    the colour the *degradation* painted either, or the attack rebuilds the
    broken file and tests the floor instead of the comparator."""
    ctx = one_op_deck(
        tmp_path, "deck9005",
        _filled(3, "Panel", "FF0000") + _sp(4, "Box", 0, 0, "A"),
        # the `delete` is what deck0004 had beside its `recolor`: it is what
        # made the attack constructible, so the old code produced a candidate,
        # scored it, and passed with the recolour still at its correct value
        [{"path": "0", "op": "recolor", "deg": "d1", "kind": "autoshape",
          "to": "#00FF00", "was_fill_xml": [], "box": None},
         {"path": "1", "op": "delete", "deg": "d2", "kind": "autoshape",
          "box": [0, 0, 900000, 500000]}],
        plan_components=[{"id": "c001", "deg": "d1", "op": "recolor",
                          "slide": 0, "gt_path": "0", "weight": 0.5,
                          "spec": {"path": "0"}},
                         {"id": "c002", "deg": "d2", "op": "delete",
                          "slide": 0, "gt_path": "1", "weight": 0.5,
                          "spec": {"path": "1"}}])
    built = at.ATTACKS["wrong_params"].build(ctx, tmp_path / "wp.pptx")
    got = at.Pkg(built.path)
    shape = at.resolve_path(got.sp_tree("ppt/slides/slide1.xml"), "0")
    fills = [n.get("val") for n in shape.find("p:spPr", at.NS)
             .iter(at.q("a:srgbClr"))]
    assert fills and "FF0000" not in fills, fills      # not the ground truth
    assert "00FF00" not in fills, fills                # not the broken file
    assert built.facts["untested_weight"] == 0.0


def test_wrong_params_retypes_the_rows_a_table_lost(tmp_path):
    """deck0009's gap.  `_cmp_drop_rows` matches the dropped rows by their
    text, so a row that is back and says the wrong thing is the wrong value —
    and it is the realistic wrong answer: a solver retyping from memory."""
    ctx = one_op_deck(
        tmp_path, "deck9006",
        _table(18, [["A1", "A2"], ["B1", "B2"], ["C1", "C2"]])
        + _sp(4, "Box", 0, 0, "A"),
        [{"path": "0", "op": "table_drop_rows", "deg": "d1", "kind": "table",
          "removed": [{"row": 1, "cells": ["B1", "B2"]}],
          "box": [0, 0, 4000000, 2000000]},
         {"path": "1", "op": "delete", "deg": "d2", "kind": "autoshape",
          "box": [0, 0, 900000, 500000]}],
        plan_components=[{"id": "c001", "deg": "d1", "op": "table_drop_rows",
                          "slide": 0, "gt_path": "0", "weight": 0.5,
                          "spec": {"path": "0"}},
                         {"id": "c002", "deg": "d2", "op": "delete",
                          "slide": 0, "gt_path": "1", "weight": 0.5,
                          "spec": {"path": "1"}}])
    built = at.ATTACKS["wrong_params"].build(ctx, tmp_path / "wp.pptx")
    got = at.Pkg(built.path)
    frame = at.resolve_path(got.sp_tree("ppt/slides/slide1.xml"), "0")
    rows = frame.find("a:graphic/a:graphicData/a:tbl", at.NS).findall(
        "a:tr", at.NS)
    texts = [[t.text for t in row.iter(at.q("a:t"))] for row in rows]
    assert texts[1] == ["WRONG", "WRONG"], texts      # the dropped row
    assert texts[0] == ["A1", "A2"] and texts[2] == ["C1", "C2"], texts
    assert built.facts["untested_weight"] == 0.0


def test_a_blank_dropped_row_is_not_claimed_as_perturbed(tmp_path):
    """The comparator calls an all-empty dropped row `Unscorable`, so claiming
    it here would be the same lie in the other direction: a branch that returns
    "perturbed" having changed nothing graded defeats the coverage gate that
    now trusts it."""
    ctx = one_op_deck(
        tmp_path, "deck9007",
        _table(18, [["A1"], [""], ["C1"]]) + _sp(4, "Box", 0, 0, "A"),
        [{"path": "0", "op": "table_drop_rows", "deg": "d1", "kind": "table",
          "removed": [{"row": 1, "cells": [""]}], "box": None},
         {"path": "1", "op": "delete", "deg": "d2", "kind": "autoshape",
          "box": [0, 0, 900000, 500000]}],
        plan_components=[{"id": "c002", "deg": "d2", "op": "delete",
                          "slide": 0, "gt_path": "1", "weight": 1.0,
                          "spec": {"path": "1"}}])
    built = at.ATTACKS["wrong_params"].build(ctx, tmp_path / "wp.pptx")
    got = at.Pkg(built.path)
    frame = at.resolve_path(got.sp_tree("ppt/slides/slide1.xml"), "0")
    texts = [t.text or "" for t in frame.iter(at.q("a:t"))]
    assert texts == ["A1", "", "C1"], texts        # nothing claimed, nothing hit
    assert [m["op"] for m in built.facts["unperturbed"]] == ["table_drop_rows"]
    assert built.facts["untested_weight"] == 0.0


def test_a_shape_is_never_repainted_the_colour_it_already_wears(tmp_path):
    """`7F007F` was hard-coded, which is `_wrong_animation`'s bug waiting to
    happen: a shape already wearing it would be "perturbed" to its own correct
    value and score 1.00 inside an attack claiming the value is wrong."""
    path = tmp_path / "p.pptx"
    _write_pptx(path, [_slide(_filled(3, "Panel", "7F007F"))])
    pkg = at.Pkg(path)
    shape = at.resolve_path(pkg.sp_tree("ppt/slides/slide1.xml"), "0")
    assert at._wrong_fill(shape)
    vals = [n.get("val") for n in shape.find("p:spPr", at.NS)
            .iter(at.q("a:srgbClr"))]
    assert "7F007F" not in vals, vals


def test_the_perturbation_registry_is_the_coverage_record():
    """Which operators this attack covers has to be a set difference a test can
    compute.  As an `elif` chain it was something a reader had to reconstruct
    by eye, and two operators in daily use turned out to have no branch."""
    assert at.PERTURB.keys() >= {"delete", "move", "scatter", "resize",
                                 "set_font", "outline", "recolor",
                                 "clear_table_cells", "table_drop_rows",
                                 "table_drop_cols", "strip_animation",
                                 "smartart_drop_nodes"}


# --------------------------------------------------------------------------- #
# the monotonicity check
# --------------------------------------------------------------------------- #


def test_half_restore_splits_the_reward_mass_not_the_component_count(tmp_path):
    """Components are weighted by estimated steps, so three cheap ones and one
    expensive one are not two halves.  Splitting by count on deck0001 would
    have restored 2 of 5 degradations worth 0.30 of the reward and called the
    task non-monotonic when it was the check that was wrong."""
    ctx = make_deck(tmp_path, weights={"d1": 0.5, "d2": 0.2, "d3": 0.2,
                                       "d4": 0.1})
    built = at.ATTACKS["half_restore"].build(ctx, tmp_path / "hr.pptx")
    assert built.facts["restored"] == ["d1"]
    assert built.facts["mass"] == pytest.approx(0.5)


def test_half_restore_never_restores_nothing(tmp_path):
    """A plan whose weights are all zero made the search pick the empty subset,
    which turns the monotonicity check into a second `noop` that then passes
    every threshold by scoring 0.0."""
    ctx = make_deck(tmp_path, weights={"d1": 0.0, "d2": 0.0, "d3": 0.0,
                                       "d4": 0.0})
    built = at.ATTACKS["half_restore"].build(ctx, tmp_path / "hr.pptx")
    assert 0 < len(built.facts["restored"]) < 4


def test_half_restore_restores_those_pages_exactly(tmp_path):
    """Half-restored is not half-attempted: if the page is not byte-identical
    to the ground truth, a score below 0.5 measures the restore, not the
    comparator."""
    ctx = make_deck(tmp_path, weights={"d1": 0.5, "d2": 0.2, "d3": 0.2,
                                       "d4": 0.1})
    built = at.ATTACKS["half_restore"].build(ctx, tmp_path / "hr.pptx")
    assert built.facts["exact"] == len(built.facts["pages"])


def test_a_part_another_page_still_needs_is_not_overwritten(tmp_path):
    """Restoring one component by copying its page must not restore a second
    one that happens to share a media part, or half a restore quietly becomes
    a whole one and the 0.5 is meaningless."""
    ctx = make_deck(tmp_path)
    dst, src = ctx.open_input(), ctx.open_gt()
    shared = "ppt/slides/slide5.xml"
    dst.put(shared, b"<kept/>")
    at.copy_slides(dst, src, [1])
    assert dst.read(shared) == b"<kept/>"
    assert dst.read("ppt/slides/slide2.xml") == src.read("ppt/slides/slide2.xml")


# --------------------------------------------------------------------------- #
# paths, because a path that misses is an attack that misses
# --------------------------------------------------------------------------- #


def test_a_group_path_is_walked_the_way_the_inventory_walks_it(tmp_path):
    """`19/0` addresses a child inside a group.  Counting the `p:grpSpPr` a
    slide's spTree starts with, or not descending at all, aims every geometry
    perturbation at the wrong shape — and `wrong_params` then reports "all
    components perturbed" having perturbed none of them."""
    path = tmp_path / "g.pptx"
    _write_pptx(path, [_slide(_sp(2, "Solo", 0, 0, "A")
                              + _group(3, _sp(4, "Inner", 111111, 0, "B")))])
    tree = at.Pkg(path).sp_tree("ppt/slides/slide1.xml")
    inner = at.resolve_path(tree, "1/0")
    assert inner is not None
    assert inner.find(".//p:cNvPr", at.NS).get("name") == "Inner"
    assert at._get_box(inner)[0] == 111111


# --------------------------------------------------------------------------- #
# verdict semantics
# --------------------------------------------------------------------------- #


def test_an_attack_that_cannot_be_built_rejects_the_task(tmp_path, monkeypatch):
    """An unproven gate is indistinguishable from a gate that would have
    failed.  Reporting it as a skip is how a hackable task ships."""
    ctx = make_deck(tmp_path)

    def explode(ctx, out):
        raise at.Unconstructible("no chart to flatten")

    monkeypatch.setitem(at.ATTACKS, "boom",
                        at.Attack("boom", "explodes", at.AtMost(0.05), explode,
                                  lambda ctx: None, order=99))
    built = at.build_all(ctx, tmp_path / "out", ["boom"])
    report = at.Report(ctx.name, [], [built["boom"]])
    assert built["boom"].status == "unconstructible"
    assert report.rejected


def test_an_attack_that_does_not_apply_is_not_a_rejection(tmp_path, monkeypatch):
    """`native_to_picture` on a deck with no chart, table or SmartArt is a gate
    with nothing to guard, not a gate that failed.  Conflating the two rejects
    every deck and the battery stops carrying information."""
    ctx = make_deck(tmp_path)
    monkeypatch.setitem(at.ATTACKS, "boom",
                        at.Attack("boom", "explodes", at.AtMost(0.05),
                                  lambda ctx, out: None,
                                  lambda ctx: "nothing to attack", order=99))
    built = at.build_all(ctx, tmp_path / "out", ["boom"])
    assert built["boom"].status == "n/a"
    assert not at.Report(ctx.name, [], [built["boom"]]).rejected


def test_the_attack_nobody_ran_is_a_row_and_not_an_absence(tmp_path):
    """`--no-wps` used to leave `gt_roundtrip` out of the battery entirely.

    No row is worse than a failing row: there is nothing to fail, so the table
    comes back a clean sweep and the summary counts thirteen attacks passed —
    having never once asked whether the application these tasks are graded in
    returns the ground truth unchanged. That is the exact shape of clean sweep
    this module exists to disbelieve, and it is the one it was producing.
    """
    make_deck(tmp_path)
    report = at.run([tmp_path / "deck9999"], tmp_path / "out", None,
                    ["noop", "gt_roundtrip"], wps=False)[0]

    rows = {row.attack: row for row in report.rows}
    assert "gt_roundtrip" in rows, (
        f"the battery has no gt_roundtrip row at all, so there is nothing for "
        f"it to fail and the table reports a clean sweep of {sorted(rows)}")
    row = rows["gt_roundtrip"]
    assert row.status == "not_run"
    assert "--no-wps" in row.note
    assert report.rejected, "a battery that never ran an attack swept clean"
    assert any("gt_roundtrip" in why and "never fired" in why
               for why in report.reasons), report.reasons

    table = at.table(report)
    assert "REJECT (never run)" in table
    assert "verdict: REJECT" in table
    assert "`gt_roundtrip` | 1" in at.summary([report])


def test_not_run_is_not_the_same_verdict_as_unbuildable(tmp_path):
    """Both reject, and the reader needs to know which: one says this deck has
    nothing to attack with, the other says run it somewhere else."""
    make_deck(tmp_path)
    report = at.run([tmp_path / "deck9999"], tmp_path / "out", None,
                    ["noop", "gt_roundtrip"], wps=False)[0]
    rows = {row.attack: row for row in report.rows}
    assert "gt_roundtrip" in rows, sorted(rows)
    assert at._mark(rows["gt_roundtrip"]) == "REJECT (never run)"
    assert at._mark(at.Row("x", "", "", "unconstructible")) == "REJECT (unproven)"


def test_an_attack_the_caller_did_not_ask_for_is_simply_absent(tmp_path):
    """`--only noop` is a request for one attack, not a battery that failed to
    run the other twelve; `not_run` is for an attack that was asked for and
    skipped, and widening it to every unselected name would make every partial
    run a rejection."""
    make_deck(tmp_path)
    report = at.run([tmp_path / "deck9999"], tmp_path / "out", None,
                    ["noop"], wps=False)[0]
    assert [r.attack for r in report.rows] == ["noop"]
    assert not report.rejected


def test_a_no_gain_expectation_fails_when_its_reference_is_missing():
    """`damage_untouched` is judged against `noop`.  If `noop` did not produce a
    score, "not higher than nothing" is vacuously true and the attack passes
    without ever having been compared to anything."""
    ok, why = at.NoGain("noop").check(0.4, {})
    assert not ok and "noop" in why


def test_every_registered_attack_declares_a_threshold_it_can_fail():
    """An attack with no expectation is a candidate deck nobody looks at.  The
    registry is the list, so the list is what gets checked."""
    for name, atk in at.ATTACKS.items():
        assert atk.expect.kind in ("exact", "at_most", "between", "no_gain",
                                   "costs", "nothing_already_right"), name
        assert atk.what and atk.expect.label(), name


#: for each expectation kind, an input that must make it go red.  Adding a kind
#: without adding a line here fails `test_every_expectation_can_actually_fail`,
#: which is the point: the kind cannot enter the registry until somebody has
#: written down what failing it looks like.
_DEFEATS = {
    "exact": lambda e: (e.lo + 1.0, {}, None),
    "at_most": lambda e: (e.hi + 0.5, {}, None),
    "between": lambda e: (e.hi + 1.0, {}, None),
    "no_gain": lambda e: (1.0, {e.ref: 0.0}, None),
    "costs": lambda e: (1.0, {e.ref: 1.0}, None),
    "nothing_already_right": lambda e: (0.0, {}, {"components": [
        {"id": "c001", "deg": "d1", "op": "move", "weight": 0.5, "score": 0.0,
         "raw": 0.5, "floor": 0.5, "why": "already half there"}]}),
}


def test_every_expectation_can_actually_fail():
    """The defect this whole file exists for, applied to the expectations.

    `noop`'s `= 0.000` was an algebraic identity — the floor is measured from
    the very candidate being scored, so `(raw - floor) / (1 - floor)` is zero
    however the comparator is written — and `damage_untouched_gt`'s
    `NoGain("gt")` would have passed at 1.000, i.e. at *no penalty at all*.
    Both rows were recorded as passes on all eight decks and neither could have
    gone red.  So every kind in the registry has to have a defeating input, and
    every registered attack has to be judged by one of those kinds.
    """
    for name, atk in at.ATTACKS.items():
        score, others, result = _DEFEATS[atk.expect.kind](atk.expect)
        ok, why = atk.expect.check(score, others, result)
        assert not ok, f"{name} ({atk.expect.kind}) cannot be failed"
        assert why, f"{name} fails without saying why"


def test_noop_passing_at_zero_is_not_what_noop_asserts():
    """`noop` still scores 0.000 when the broken file already satisfies a
    graded component — that is the identity.  The row has to go red anyway, on
    the number underneath it."""
    expect = at.ATTACKS["noop"].expect
    clean = {"components": [{"id": "c001", "deg": "d1", "op": "delete",
                             "weight": 1.0, "score": 0.0, "raw": 0.0,
                             "floor": 0.0, "why": "gone"}]}
    ok, _ = expect.check(0.0, {}, clean)
    assert ok

    already = {"components": [{"id": "c001", "deg": "d1", "op": "move",
                               "weight": 0.4, "score": 0.0, "raw": 1.0,
                               "floor": 1.0, "why": "in the right place"}]}
    ok, why = expect.check(0.0, {}, already)          # same 0.000 as above
    assert not ok
    assert "0.400" in why and "d1/move" in why, why

    # the negative control: the expectation this replaced passes on the same
    # evidence, which is how the row shipped saying nothing.
    assert at.Exact(0.0).check(0.0, {}, already)[0]


def test_a_score_that_came_back_without_components_is_not_a_pass():
    """`= 0.000` on its own is unfalsifiable, so a scorer that returns no
    per-component breakdown leaves `noop` with nothing to assert."""
    ok, why = at.ATTACKS["noop"].expect.check(0.0, {}, None)
    assert not ok and "identity" in why


def test_collateral_damage_must_cost_something_not_merely_not_pay():
    """`damage_untouched_gt` recorded 0.900 with a `<= gt` bar on all eight
    decks; 1.000 would have been recorded the same way.  The 0.100 comes
    entirely from `comparators.SCOPE_RATES`, a policy constant the battery
    never asserted was non-zero — set the rate to 0 and every deck still
    passed."""
    expect = at.ATTACKS["damage_untouched_gt"].expect
    assert expect.check(0.900, {"gt": 1.0})[0]        # today's run
    ok, why = expect.check(1.000, {"gt": 1.0})        # the rate turned off
    assert not ok and "0.05" in why
    assert at.NoGain("gt").check(1.000, {"gt": 1.0})[0]      # negative control
    assert at.COLLATERAL_MIN_COST > 0


def test_a_costs_expectation_fails_when_its_reference_is_missing():
    ok, why = at.Costs("gt", 0.05).check(0.4, {})
    assert not ok and "gt" in why


def test_a_reference_that_scored_nothing_leaves_the_row_unjudgeable():
    """deck0008's plan is rejected, so `gt` itself scores 0.000 and there is no
    room below it to lose 0.05 in.  Reading that as a pass is the same defect
    one level down: a row nobody could have failed reported as evidence."""
    ok, why = at.Costs("gt", 0.05).check(0.0, {"gt": 0.0})
    assert not ok
    assert "cannot be judged" in why and "0.000" in why


# --------------------------------------------------------------------------- #
# what the record says happened
#
# `attacks.json` records `"wps": true`, which is `bool(wps)` off the command
# line — the flag, not the fact.  The candidate decks are deleted once scored,
# so a free-text sentence was the only surviving proof that a WPS window ever
# opened, and an audit could confirm the eight round trips only by matching
# byte sizes inside that prose.  And `state.json` recorded `attacks: 14,
# variants: 6` for every deck — row counts, while the run executed 107/112 and
# 39/48.
# --------------------------------------------------------------------------- #


def _roundtripped(tmp_path: Path):
    """A ground truth and a plausible re-serialisation of it."""
    src = tmp_path / "source.pptx"
    saved = tmp_path / "saved.pptx"
    _write_pptx(src, [_slide(_sp(2, "Box", 0, 0, "A"))])
    pkg = at.Pkg(src)
    pkg.put("ppt/slides/slide1.xml", _slide(_sp(2, "Box", 0, 0, "A")) + b" ")
    pkg.save(saved)
    return src, saved


def test_the_round_trip_evidence_is_numbers_and_not_a_sentence(tmp_path):
    src, saved = _roundtripped(tmp_path)
    facts = at.roundtrip_facts(src, saved)
    assert at.roundtrip_problems(facts, src) == []
    assert facts["before_bytes"] == src.stat().st_size
    assert facts["before_sha256"] != facts["after_sha256"]
    assert facts["parts_differing"] > 0
    # every claim the audit had to recover by reading English is a key
    assert {"editor", "before_bytes", "after_bytes", "before_sha256",
            "after_sha256", "parts_differing"} <= set(facts)


def test_a_copy_cannot_pass_itself_off_as_a_round_trip(tmp_path):
    """The failure the shape check exists for: `roundtrip_wps` raises rather
    than returning a copy *today*, and that is the only reason the invariant
    holds.  A future evidence line reading `(N -> N bytes, 0 parts differ)`
    would have read exactly as clean as the eight real ones."""
    src, _ = _roundtripped(tmp_path)
    copy = tmp_path / "copy.pptx"
    copy.write_bytes(src.read_bytes())
    problems = at.roundtrip_problems(at.roundtrip_facts(src, copy), src)
    assert any("byte-identical" in p for p in problems), problems
    assert any("0 parts differ" in p for p in problems), problems


def test_round_tripping_something_other_than_the_ground_truth_is_caught(tmp_path):
    src, saved = _roundtripped(tmp_path)
    other = tmp_path / "other.pptx"
    _write_pptx(other, [_slide(_sp(2, "Different", 0, 0, "Z"))])
    problems = at.roundtrip_problems(at.roundtrip_facts(other, saved), src)
    assert problems and "ground truth" in problems[0], problems


def test_prose_alone_does_not_prove_the_round_trip_ran(tmp_path):
    """A `Built` with a convincing sentence and no facts is exactly the record
    that shipped."""
    assert at.roundtrip_problems({}, None)
    assert at.roundtrip_problems(None, None)


def test_an_unverifiable_round_trip_rejects_the_deck(tmp_path):
    """End to end: the row is `unconstructible`, which is a rejection reason,
    so a battery whose WPS evidence does not stand up cannot report a sweep."""
    ctx = make_deck(tmp_path)
    copy = tmp_path / "copy.pptx"
    copy.write_bytes(ctx.gt_path.read_bytes())

    class _Scorer:
        def plan(self, ctx):
            return {"components": [], "degradations": [], "rejected": []}

        def score(self, plan, cand, gt, init):
            return {"score": 1.0, "components": [], "failed_gate": None}

    built = at.build_all(ctx, tmp_path / "out", ["gt"])
    built["gt_roundtrip"] = at.Built(
        copy, "WPS re-serialised the package (looks great)",
        at.roundtrip_facts(ctx.gt_path, copy))
    report = at.score_all(ctx, built, _Scorer())
    row = next(r for r in report.rows if r.attack == "gt_roundtrip")
    assert row.status == "unconstructible"
    assert report.rejected
    assert any("round-trip evidence" in why for why in report.reasons)


def test_a_rows_facts_reach_the_record(tmp_path):
    """`attacks.json` is written with `dataclasses.asdict(row)`, so a fact that
    is not a field of `Row` does not survive the run that produced it."""
    import dataclasses

    ctx = make_deck(tmp_path)
    built = at.build_all(ctx, tmp_path / "out", ["half_restore"])
    row = at.Row("half_restore", "", "", "scored",
                 facts=dict(built["half_restore"].facts))
    assert "facts" in dataclasses.asdict(row)
    assert dataclasses.asdict(row)["facts"]["restored"]


def test_the_summary_counts_executions_and_not_rows(tmp_path):
    """`attacks: 14 / variants: 6` was `len(rows)` and `len(variants)`.  A
    reader cannot tell a battery that ran everything from one that found no
    material for a third of it — and one deck's whole protection against being
    punished for legitimate work rested on 2 of its 6 variant rows."""
    rows = [at.Row("a", "", "", "scored", score=0.0, ok=True),
            at.Row("native_to_picture", "", "", "n/a", note="no chart"),
            at.Row("orphan_media", "", "", "n/a", note="nothing withheld")]
    variants = [at.Row("rebuilt_shapes", "", "", "scored", score=1.0, ok=True),
                at.Row("text_retyped", "", "", "unconstructible", note="no text"),
                at.Row("ungrouped", "", "", "n/a", note="no group")]
    report = at.Report("deck0007", [], rows, variants=variants)
    cov = report.coverage()
    assert (cov["attacks_total"], cov["attacks_scored"]) == (3, 1)
    assert (cov["variants_total"], cov["variants_scored"]) == (3, 1)
    assert cov["attacks_na"] == 2 and cov["variants_na"] == 2
    assert "native_to_picture (n/a)" in cov["attacks_not_scored"]
    assert "1/3 attacks and 1/3 legitimate variants" in report.coverage_line()
    assert "coverage: 1/3 attacks" in at.table(report)
    assert "1/3 attack cells and 1/3 variant cells" in at.summary([report])


def test_a_row_that_could_not_be_built_is_not_counted_as_run(tmp_path):
    """The distinction the whole summary exists to preserve: `unconstructible`
    and `error` are rejections, not coverage."""
    report = at.Report("d", [], [at.Row("x", "", "", "unconstructible"),
                                 at.Row("y", "", "", "error"),
                                 at.Row("z", "", "", "not_run")])
    assert report.coverage()["attacks_scored"] == 0
    assert report.coverage()["attacks_unproven"] == 3


# --------------------------------------------------------------------------- #
# the comparator bridge
# --------------------------------------------------------------------------- #


def test_a_comparator_without_a_plan_builder_says_which_names_it_tried():
    """The plan builder's name was never part of the agreed interface.  Failing
    with `AttributeError: plan` sends the reader to the wrong module."""
    fake = types.SimpleNamespace(score=lambda *a: {}, __name__="fake")
    with pytest.raises(at.ScorerUnavailable) as error:
        at.Scorer(fake)
    assert "build_plan" in str(error.value)


def test_a_rejected_plan_still_lets_every_attack_be_exercised(tmp_path):
    """A plan the comparator rejects fails its first gate and every candidate,
    the ground truth included, scores 0.0.  Read literally that is thirteen
    attacks "passing" without one of them having been run — the exact clean
    sweep this module exists to disbelieve."""
    ctx = make_deck(tmp_path)

    def score(plan, cand, gt, init):
        if plan.get("rejected"):
            return {"score": 0.0, "components": [], "hard_gates": {},
                    "failed_gate": "plan_accepted"}
        return {"score": 0.7, "components": [], "hard_gates": {},
                "failed_gate": None}

    module = types.SimpleNamespace(
        __name__="fake", score=score,
        build_plan=lambda deck: {"rejected": ["no deg on any entry"],
                                 "components": [], "degradations": []})
    scorer = at.Scorer(module)
    built = at.build_all(ctx, tmp_path / "out", ["noop", "clone_spam"])
    report = at.score_all(ctx, built, scorer)
    assert report.plan_rejected == ["no deg on any entry"]
    assert [row.score for row in report.rows] == [0.7, 0.7]
    assert report.rejected                       # the plan rejection stands


# --------------------------------------------------------------------------- #
# the other direction: `legitimate_variant`
#
# The attacks above all ask "can this be cheated?".  Nothing above asks whether
# work that deserves credit gets it — and that is the failure that actually
# happened: three of four rollouts recorded 0.0 while having done 43%, 53% and
# 63% of the work.  These tests pin the properties that make the second class a
# second class rather than fourteen more rows.
# --------------------------------------------------------------------------- #


def _one_variant(tmp_path: Path) -> dict:
    """One built variant pointing at a real package, for the stub scorers."""
    path = tmp_path / "variant.pptx"
    _write_pptx(path, [_slide(_sp(2, "Box", 0, 0, "T"))])
    return {list(at.LEGITIMATE_VARIANTS)[0]: at.Built(path, "built")}


def test_the_two_classes_are_kept_apart():
    """One shared table would need one shared threshold, lenient enough for
    the attacks and strict enough for the variants at once."""
    assert at.LEGITIMATE_VARIANTS
    assert not (set(at.ATTACKS) & set(at.LEGITIMATE_VARIANTS))


def test_every_variant_declares_when_it_applies():
    for name, item in at.LEGITIMATE_VARIANTS.items():
        assert item.what and callable(item.build) and callable(item.applies), name


def test_a_variant_with_no_material_is_not_a_rejection(tmp_path):
    """The opposite default from an attack.  An attack that applies and cannot
    be built leaves a gate unproven, which is a rejection; a variant that
    cannot be built means this deck offers no such route, and a route nobody
    can take proves nothing either way."""
    ctx = make_deck(tmp_path)
    built = at.build_variants(ctx, tmp_path / "out")
    rows = [row for row in built.values() if isinstance(row, at.Row)]
    assert any(row.status in ("n/a", "unconstructible") for row in rows)
    report = at.Report("deck9999", [], [], variants=[
        row for row in rows if row.status in ("n/a", "unconstructible")])
    assert report.reasons == []


def test_a_gate_firing_on_a_variant_rejects_the_task(tmp_path):
    """A hard gate that fires on correct work rejects the task exactly as a
    successful attack does — that is the 1100001 casualty in one line."""
    class _Scorer:
        def score(self, plan, cand, gt, init):
            return {"score": 0.0, "components": [], "failed_gate": "no_cloned_shapes",
                    "gate_reasons": {"no_cloned_shapes": "a surplus copy"},
                    "penalty": 0.0, "scope_violations": {}}
    built = _one_variant(tmp_path)
    rows = at.score_variants(built, _Scorer(), {}, {}, {}, 1.0)
    assert rows[0].ok is False and "GATE" in rows[0].note
    assert at.Report("d", [], [], variants=rows).rejected


def test_a_variant_that_scores_like_the_ground_truth_passes(tmp_path):
    class _Scorer:
        def score(self, plan, cand, gt, init):
            return {"score": 1.0, "components": [], "failed_gate": None,
                    "gate_reasons": {}, "penalty": 0.0, "scope_violations": {}}
    built = _one_variant(tmp_path)
    rows = at.score_variants(built, _Scorer(), {}, {}, {}, 1.0)
    assert rows[0].ok is True and rows[0].note == ""


def test_a_variant_that_loses_credit_is_a_defect_not_a_threshold(tmp_path):
    """The number that must not be tuned: a variant scoring below `gt` is
    reported as a failure with the components that lost the credit, so the
    fix goes into the comparator rather than into `VARIANT_TOL`."""
    class _Scorer:
        def score(self, plan, cand, gt, init):
            return {"score": 0.90, "components": [
                        {"id": "c001", "deg": "d1", "op": "set_font",
                         "weight": 0.1, "score": 0.0, "why": "runs 0/1"}],
                    "failed_gate": None, "gate_reasons": {}, "penalty": 0.0,
                    "scope_violations": {}}
    built = _one_variant(tmp_path)
    rows = at.score_variants(built, _Scorer(), {}, {}, {}, 1.0)
    assert rows[0].ok is False
    assert "d1/set_font" in rows[0].note and "runs 0/1" in rows[0].note


def test_the_variant_tolerance_is_not_a_place_to_hide_a_comparator_bug():
    """0.02 is rounding in the last place of a 98-component deck, not room for
    an equivalence nobody implemented."""
    assert at.VARIANT_TOL <= 0.05


def test_a_regrouped_variant_keeps_every_child_where_it_was(tmp_path):
    """The third production failure in miniature — a component comparing a
    group child's local coordinates against slide-level EMU.  The variant
    leaves `chOff`/`chExt` equal to `off`/`ext`, so the child's own numbers do
    not change and only the matrix handling can make the score move."""
    root = tmp_path / "deck9998"
    root.mkdir()
    body = (_sp(2, "Box A", 1000000, 1000000, "A")
            + _sp(3, "Box B", 3000000, 1000000, "B"))
    _write_pptx(root / "source.pptx", [_slide(body)])
    _write_pptx(root / "input.pptx", [_slide("")])
    (root / "delta.json").write_text(json.dumps({"slides": {"0": [
        {"path": "0", "op": "delete", "deg": "d1", "kind": "autoshape",
         "box": [1000000, 1000000, 900000, 500000]},
        {"path": "1", "op": "delete", "deg": "d1", "kind": "autoshape",
         "box": [3000000, 1000000, 900000, 500000]}]}}))
    ctx = at.Ctx.load(root, tmp_path / "scratch")
    at.LEGITIMATE_VARIANTS["regrouped"].build(ctx, tmp_path / "v.pptx")
    from pptxgym.inventory import inventory_pptx
    before = inventory_pptx(root / "source.pptx")["slides"][0]["shapes"]
    after = inventory_pptx(tmp_path / "v.pptx")["slides"][0]["shapes"]
    assert [s["kind"] for s in after][0] == "group"
    want = {s["_plain"]: s["bbox"] for s in before}
    have = {s["_plain"]: s["bbox"] for s in after if s.get("group")}
    assert want == have


def test_a_group_does_not_inherit_its_childs_placeholder_role(tmp_path):
    """A group drawn around a title claimed the title's `ph:title#0` key, so
    the slide held two shapes claiming the same placeholder and the clone gate
    read the container as a surplus copy of its own child."""
    from pptxgym import inventory as iv
    child = ('<p:sp><p:nvSpPr><p:cNvPr id="4" name="Title"/><p:cNvSpPr/>'
             '<p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:spPr>'
             '<a:xfrm><a:off x="0" y="0"/><a:ext cx="900000" cy="500000"/>'
             '</a:xfrm></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p>'
             '<a:r><a:t>T</a:t></a:r></a:p></p:txBody></p:sp>')
    root = tmp_path / "d.pptx"
    _write_pptx(root, [_slide(_group(9, child))])
    shapes = iv.inventory_pptx(root)["slides"][0]["shapes"]
    group = next(s for s in shapes if s["kind"] == "group")
    title = next(s for s in shapes if s["kind"] == "placeholder")
    assert not any(k.startswith("ph:") for k in group["keys"])
    assert "ph:title#0" in title["keys"]        # the negative control
