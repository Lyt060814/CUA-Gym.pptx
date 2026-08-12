"""A legitimate variant that destroys the thing being graded.

Run 12 reported four variants losing credit on two decks, and every one of them
was read the same way at first: the comparator refuses to pay for correct work.
Three of the four were the opposite — the *variant* was wrong.

    deck0001  rebuilt_shapes  0.975   lost: d3/zorder 0.25×0.03 (order 2/8)
    deck0001  ungrouped       0.933   lost: d2/ungroup 0.00×0.07 (group absent)
    deck0003  ungrouped       0.975   lost: d5/ungroup 0.00×0.03 (group absent)
    deck0003  picture_reinserted 0.915 lost: d5/move 0.00×0.03 (off by 2.39in)

`rebuilt_shapes` redraws last in the z-order, which is not a different way of
doing the work when the z-order *is* the work.  `ungrouped` dissolved the very
group an `ungroup` component asks the solver to put back.  Both are the same
mistake: a variant is legitimate because what it changes is not what is being
measured, and that is a claim about a *deck*, not a claim in general.

`picture_reinserted` was a plain bug, and one this tree has now made four
times: it resolved positional paths against a tree it was halfway through
rearranging, and it lifted pictures out of their group into slide coordinates
without the group's matrix. deck0003's five damaged pictures are all children
of one group; two came back "off by 2.39in", which is a displacement the
variant introduced.

Measured on the two real decks, after the fix:

    deck0001  rebuilt_shapes  0.975 -> 1.000    ungrouped 0.933 -> n/a
    deck0003  picture_reinserted 0.915 -> 1.000 ungrouped 0.975 -> n/a

and on the local corpus, `ungrouped` still applies and still scores 1.000 on
deck0005 and deck0006, so the exclusion did not switch the variant off.

    python3 -m pytest tests/test_variant_contradiction.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym.evaluation import attacks as at                                # noqa: E402
from pptxgym.evaluation.inventory import inventory_pptx                     # noqa: E402
from test_attacks import _one_variant, _slide, _sp, _write_pptx                # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def _pic(shape_id: int, rid: str, x: int, y: int) -> str:
    return (f'<p:pic><p:nvPicPr><p:cNvPr id="{shape_id}" name="Pic{shape_id}"/>'
            f'<p:cNvPicPr/><p:nvPr/></p:nvPicPr><p:blipFill>'
            f'<a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch>'
            f'</p:blipFill><p:spPr><a:xfrm><a:off x="{x}" y="{y}"/>'
            f'<a:ext cx="400000" cy="300000"/></a:xfrm>'
            f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>')


def _shifted_group(shape_id: int, children: str) -> str:
    """A group whose child space is *offset* from where it draws.

    `chOff` at the origin and `off` four inches across is the whole point: a
    child lifted out of this group without the matrix applied lands four
    inches from where it belongs, which is deck0003's 2.39in in miniature.
    """
    return (f'<p:grpSp><p:nvGrpSpPr><p:cNvPr id="{shape_id}" name="Group"/>'
            f'<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm>'
            f'<a:off x="4000000" y="1000000"/><a:ext cx="3000000" cy="1000000"/>'
            f'<a:chOff x="0" y="0"/><a:chExt cx="3000000" cy="1000000"/>'
            f'</a:xfrm></p:grpSpPr>{children}</p:grpSp>')


def _deck_with_grouped_pictures(tmp_path: Path, entries: list[dict]) -> at.Ctx:
    """A one-page deck: three pictures inside an offset group, plus a
    bystander shape ahead of it so the group is not at path 0."""
    root = tmp_path / "deck9997"
    root.mkdir()
    kids = "".join(_pic(10 + i, f"rIdP{i}", 100000 + i * 500000, 100000)
                   for i in range(3))
    body = _sp(2, "Bystander", 200000, 200000, "B") + _shifted_group(9, kids)
    _write_pptx(root / "source.pptx", [_slide(body)])
    _write_pptx(root / "input.pptx", [_slide(_sp(2, "Bystander", 200000,
                                                 200000, "B"))])
    # the media the blips point at, added the way production adds it
    pkg = at.Pkg(root / "source.pptx")
    part = pkg.slide_parts()[0]
    pkg.ensure_default("png", "image/png")
    for i in range(3):
        name = f"ppt/media/image{i}.png"
        pkg.put(name, PNG + bytes([i]))
        rid = pkg.add_rel(part, at.IMAGE_REL, f"../media/image{i}.png")
        xml = pkg.read(part).decode().replace(f'r:embed="rIdP{i}"',
                                              f'r:embed="{rid}"')
        pkg.put(part, xml.encode())
    pkg.save(root / "source.pptx")
    (root / "delta.json").write_text(json.dumps({"slides": {"0": entries}}))
    return at.Ctx.load(root, tmp_path / "scratch")


def _pictures(path: Path):
    shapes = inventory_pptx(path)["slides"][0]["shapes"]
    return [s for s in shapes if s["kind"] == "picture"]


# --------------------------------------------------------------------------- #
# picture_reinserted — the plain bug
# --------------------------------------------------------------------------- #

MOVES = [{"path": f"1/{i}", "op": "move", "deg": "d1", "kind": "picture",
          "box": [100000 + i * 500000, 100000, 400000, 300000]}
         for i in range(3)]


def test_a_reinserted_picture_stays_inside_its_group(tmp_path):
    """The 2.39in. A group child's coordinates are in the group's `chOff`
    space; appended to the slide tree unchanged it draws somewhere else, and
    every `move` component on it reads as work not done."""
    ctx = _deck_with_grouped_pictures(tmp_path, MOVES)
    at.LEGITIMATE_VARIANTS["picture_reinserted"].build(ctx, tmp_path / "v.pptx")
    want = sorted(tuple(sorted(p["bbox"].items()))
                  for p in _pictures(ctx.gt_path))
    have = sorted(tuple(sorted(p["bbox"].items()))
                  for p in _pictures(tmp_path / "v.pptx"))
    assert want == have
    assert all(p.get("group") for p in _pictures(tmp_path / "v.pptx"))


def test_every_damaged_picture_is_reinserted_not_just_the_first(tmp_path):
    """Index once, before anything moves. Removing `1/0` renumbers `1/1` and
    `1/2`, so the second path named a shape the recipe never damaged and the
    third named one already dealt with: deck0003 reported three re-insertions
    for five damaged pictures and nobody could tell from the evidence."""
    ctx = _deck_with_grouped_pictures(tmp_path, MOVES)
    built = at.LEGITIMATE_VARIANTS["picture_reinserted"].build(
        ctx, tmp_path / "v.pptx")
    assert len(built.facts["reinserted"]) == 3
    fresh = [n for n in at.Pkg(tmp_path / "v.pptx").names() if "inserted" in n]
    assert len(fresh) == 3


def test_a_top_level_picture_still_goes_last(tmp_path):
    """The control for the fix above. `picture_reinserted` exists to prove a
    matcher does not depend on part name, shape id or tree position; keeping a
    grouped picture in its group must not quietly stop it moving the ones that
    are free to move."""
    root = tmp_path / "deck9996"
    root.mkdir()
    body = _pic(10, "rIdP0", 500000, 500000) + _sp(2, "After", 3000000, 0, "A")
    _write_pptx(root / "source.pptx", [_slide(body)])
    _write_pptx(root / "input.pptx", [_slide(_sp(2, "After", 3000000, 0, "A"))])
    pkg = at.Pkg(root / "source.pptx")
    part = pkg.slide_parts()[0]
    pkg.ensure_default("png", "image/png")
    pkg.put("ppt/media/image0.png", PNG)
    rid = pkg.add_rel(part, at.IMAGE_REL, "../media/image0.png")
    pkg.put(part, pkg.read(part).decode()
            .replace('r:embed="rIdP0"', f'r:embed="{rid}"').encode())
    pkg.save(root / "source.pptx")
    (root / "delta.json").write_text(json.dumps({"slides": {"0": [
        {"path": "0", "op": "move", "deg": "d1", "kind": "picture",
         "box": [500000, 500000, 400000, 300000]}]}}))
    ctx = at.Ctx.load(root, tmp_path / "scratch")
    at.LEGITIMATE_VARIANTS["picture_reinserted"].build(ctx, tmp_path / "v.pptx")
    got = at.Pkg(tmp_path / "v.pptx")
    kids = at._shape_children(got.sp_tree(got.slide_parts()[0]))
    assert kids[-1].tag == at.q("p:pic")


# --------------------------------------------------------------------------- #
# ungrouped — a variant that undoes the component
# --------------------------------------------------------------------------- #


def test_ungrouped_leaves_alone_the_group_an_ungroup_component_grades(tmp_path):
    """deck0001 and deck0003. `_cmp_ungroup` answers "is the group back", the
    variant dissolves it, and the sweep called 0.00 a comparator bug."""
    ctx = _deck_with_grouped_pictures(tmp_path, MOVES + [
        {"path": "1", "op": "ungroup", "deg": "d1", "kind": "group",
         "box": [4000000, 1000000, 3000000, 1000000]}])
    assert at.LEGITIMATE_VARIANTS["ungrouped"].applies(ctx)


def test_ungrouped_still_dissolves_a_group_nobody_is_grading(tmp_path):
    """The control. Widening the skip until the variant never runs would make
    every test above pass while the equivalence it was written to prove — that
    dissolving a group moves nothing — went unchecked."""
    ctx = _deck_with_grouped_pictures(tmp_path, MOVES)
    assert at.LEGITIMATE_VARIANTS["ungrouped"].applies(ctx) is None
    built = at.LEGITIMATE_VARIANTS["ungrouped"].build(ctx, tmp_path / "v.pptx")
    assert built.facts["dissolved"] == ["p1"]
    want = sorted(tuple(sorted(p["bbox"].items()))
                  for p in _pictures(ctx.gt_path))
    have = sorted(tuple(sorted(p["bbox"].items()))
                  for p in _pictures(tmp_path / "v.pptx"))
    assert want == have


def test_one_graded_group_does_not_protect_another(tmp_path):
    """The skip is per group, not per deck: a deck that asks for one group
    back still has to survive the other being taken apart."""
    ctx = _deck_with_grouped_pictures(tmp_path, MOVES + [
        {"path": "7", "op": "ungroup", "deg": "d2", "kind": "group"}])
    assert at.LEGITIMATE_VARIANTS["ungrouped"].applies(ctx) is None
    assert at._dissolvable(ctx)[0] == ["1"]


# --------------------------------------------------------------------------- #
# rebuilt_shapes — the same mistake about z-order
# --------------------------------------------------------------------------- #


def _deck_for_rebuild(tmp_path: Path, entries) -> at.Ctx:
    root = tmp_path / "deck9995"
    root.mkdir()
    body = "".join(_sp(2 + i, f"Box {i}", 500000 * (i + 1), 500000, f"T{i}")
                   for i in range(3))
    _write_pptx(root / "source.pptx", [_slide(body)])
    _write_pptx(root / "input.pptx", [_slide("")])
    (root / "delta.json").write_text(json.dumps({"slides": {"0": entries}}))
    return at.Ctx.load(root, tmp_path / "scratch")


def test_rebuilt_shapes_does_not_redraw_a_shape_whose_order_is_graded(tmp_path):
    """`d3/zorder 0.25×0.03 (order 2/8)` on deck0001. The variant appends what
    it redraws, so redrawing the shape the task asks to be moved *in the
    z-order* is not an alternative solution."""
    ctx = _deck_for_rebuild(tmp_path, [
        {"path": "0", "op": "zorder", "deg": "d1", "kind": "autoshape"},
        {"path": "1", "op": "resize", "deg": "d2", "kind": "autoshape"},
        {"path": "2", "op": "delete", "deg": "d3", "kind": "autoshape"}])
    assert at._rebuildable(ctx)[0] == ["1", "2"]
    built = at.LEGITIMATE_VARIANTS["rebuilt_shapes"].build(ctx,
                                                           tmp_path / "v.pptx")
    assert built.facts["rebuilt"] == 2
    got = at.Pkg(tmp_path / "v.pptx")
    first = at._shape_children(got.sp_tree(got.slide_parts()[0]))[0]
    assert first.find(".//p:cNvPr", at.NS).get("name") == "Box 0"


def test_a_deck_that_only_grades_z_order_reports_the_variant_inapplicable(
        tmp_path):
    """Not "passed". A variant with nothing left to change proves nothing, and
    saying so is the difference between a swept deck and an unswept one."""
    ctx = _deck_for_rebuild(tmp_path, [
        {"path": "0", "op": "zorder", "deg": "d1", "kind": "autoshape"}])
    assert at.LEGITIMATE_VARIANTS["rebuilt_shapes"].applies(ctx)


def test_rebuilt_shapes_still_redraws_when_no_order_is_graded(tmp_path):
    """The control."""
    ctx = _deck_for_rebuild(tmp_path, [
        {"path": "0", "op": "resize", "deg": "d1", "kind": "autoshape"},
        {"path": "1", "op": "recolor", "deg": "d2", "kind": "autoshape"}])
    assert at.LEGITIMATE_VARIANTS["rebuilt_shapes"].applies(ctx) is None
    assert at.LEGITIMATE_VARIANTS["rebuilt_shapes"].build(
        ctx, tmp_path / "v.pptx").facts["rebuilt"] == 2


# --------------------------------------------------------------------------- #
# the evidence the sweep prints about a loss
# --------------------------------------------------------------------------- #


def _row(tmp_path, components):
    class _Scorer:
        def score(self, plan, cand, gt, init):
            return {"score": 0.90, "components": components,
                    "failed_gate": None, "gate_reasons": {}, "penalty": 0.0,
                    "scope_violations": {}}
    return at.score_variants(_one_variant(tmp_path), _Scorer(),
                             {}, {}, {}, 1.0)[0]


def test_the_lost_list_names_only_components_that_lost_something(tmp_path):
    """deck0001's row read `lost: d3/zorder 0.25×0.03; d4/resize 1.00×0.07`.
    The second one lost nothing — the list was the top two by weight whether
    or not there was anything to explain — and a reader chasing it is worse
    off than a reader given one name."""
    row = _row(tmp_path, [{"id": "c1", "deg": "d3", "op": "zorder", "weight": 0.03,
                 "score": 0.25, "why": "order 2/8"},
                {"id": "c2", "deg": "d4", "op": "resize", "weight": 0.07,
                 "score": 1.0, "why": "size=1.00 · position=1.00"}])
    assert row.ok is False
    assert "d3/zorder" in row.note
    assert "d4/resize" not in row.note


def test_every_variant_declares_what_it_contradicts():
    """The question nobody asked three times in run 12, made unavoidable for
    the next variant: an empty set is a claim with evidence beside it in
    `VARIANT_CONTRADICTS`, not the default you get by not thinking."""
    assert set(at.VARIANT_CONTRADICTS) == set(at.LEGITIMATE_VARIANTS)


def test_a_declared_contradiction_names_a_real_operator():
    """A typo here switches the protection off silently — the filter simply
    matches nothing and the variant goes on destroying the component."""
    from pptxgym.evaluation import comparators as cmp

    known = set(cmp.REGISTRY)
    for name, ops in at.VARIANT_CONTRADICTS.items():
        assert ops <= known, f"{name} names an operator nothing grades: " \
                             f"{sorted(ops - known)}"


def test_a_loss_with_no_losing_component_still_reports_the_number(tmp_path):
    """A variant that falls short with every component at 1.00 is a defect in
    the *plan* arithmetic, and the row must still say so rather than print an
    empty list and read as a pass."""
    row = _row(tmp_path, [{"id": "c1", "deg": "d1", "op": "resize", "weight": 0.5,
                 "score": 1.0, "why": "ok"}])
    assert row.ok is False and "0.900 < gt 1.000" in row.note
