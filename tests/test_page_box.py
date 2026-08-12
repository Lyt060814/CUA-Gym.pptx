"""A group child's box, mapped to the page instead of lied about.

The delta's `box` is the shape's own xfrm — child space, for a group member —
and the masked reference render consumed it as if it were page space. On the
first orchestrator-run deck that put three 8×8px hatch specks at the image
origin while the labels they were meant to hide stayed fully visible. The fix
is a second field, `page_box`, written only when it differs, so every
operator keeps its local-coordinate semantics and only the consumers that
mean "where on the page" change what they read.

    python3 -m pytest tests/test_page_box.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym.office import census                               # noqa: E402
from pptxgym.tasks import assets                                # noqa: E402
from pptxgym.office import degrade_exec as dx                           # noqa: E402

q = census.q

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS = f'xmlns:a="{A}" xmlns:p="{P}"'


def _sp(x, y, cx, cy, sid="7", name="Label"):
    return f"""<p:sp>
      <p:nvSpPr><p:cNvPr id="{sid}" name="{name}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
      <p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm></p:spPr>
    </p:sp>"""


def _grp(inner, off=(1000, 2000), ext=(4000, 6000), ch_off=(100, 200),
         ch_ext=(2000, 3000), attrs=""):
    return f"""<p:grpSp>
      <p:nvGrpSpPr><p:cNvPr id="2" name="Group"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr><a:xfrm {attrs}>
        <a:off x="{off[0]}" y="{off[1]}"/><a:ext cx="{ext[0]}" cy="{ext[1]}"/>
        <a:chOff x="{ch_off[0]}" y="{ch_off[1]}"/><a:chExt cx="{ch_ext[0]}" cy="{ch_ext[1]}"/>
      </a:xfrm></p:grpSpPr>
      {inner}
    </p:grpSp>"""


def _tree(body):
    return etree.fromstring(f"<p:spTree {NS}>{body}</p:spTree>")


def _child(tree):
    return tree.findall(f".//{q('p:sp')}")[0]


# --------------------------------------------------------------------------- #
# the mapping
# --------------------------------------------------------------------------- #


def test_an_ungrouped_shape_maps_to_itself_and_carries_no_extra_field():
    tree = _tree(_sp(150, 250, 500, 600))
    sp = _child(tree)
    assert dx._page_box(sp) == dx._box(sp) == (150, 250, 500, 600)
    assert "page_box" not in dx._label(sp)


def test_a_group_child_is_scaled_and_offset_into_page_space():
    # child window 2000x3000 drawn into 4000x6000 at (1000, 2000): 2x both axes
    tree = _tree(_grp(_sp(150, 250, 500, 600)))
    box = dx._page_box(_child(tree))
    assert box == (1000 + 50 * 2, 2000 + 50 * 2, 1000, 1200)


def test_the_label_records_page_box_exactly_when_it_differs():
    tree = _tree(_grp(_sp(150, 250, 500, 600)))
    label = dx._label(_child(tree))
    assert label["page_box"] == [1100, 2100, 1000, 1200]


def test_nested_groups_compose():
    inner = _grp(_sp(150, 250, 500, 600))
    # outer maps the inner group's parent space [0,0,8000,12000] onto itself
    # shifted by (10000, 0) — a pure translation
    outer = _grp(inner, off=(10000, 0), ext=(8000, 12000),
                 ch_off=(0, 0), ch_ext=(8000, 12000))
    box = dx._page_box(_child(_tree(outer)))
    assert box == (11100, 2100, 1000, 1200)


def test_a_flipped_group_mirrors_the_box_inside_the_child_window():
    tree = _tree(_grp(_sp(150, 250, 500, 600), attrs='flipH="1"'))
    box = dx._page_box(_child(tree))
    # mirrored x inside [100, 2100]: 2*100 + 2000 - 150 - 500 = 1550
    assert box == (1000 + (1550 - 100) * 2, 2100, 1000, 1200)


def test_a_rotated_ancestor_falls_back_to_the_whole_groups_box():
    # any axis-aligned answer inside a rotated group is a lie; for a mask,
    # covering the group is honest and a computed sliver of the wrong place
    # is not
    tree = _tree(_grp(_sp(150, 250, 500, 600), attrs='rot="600000"'))
    assert dx._page_box(_child(tree)) == (1000, 2000, 4000, 6000)


def test_a_group_with_no_transform_yields_no_box_rather_than_a_wrong_one():
    body = f"""<p:grpSp>
      <p:nvGrpSpPr><p:cNvPr id="2" name="G"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      {_sp(150, 250, 500, 600)}
    </p:grpSp>"""
    assert dx._page_box(_child(_tree(body))) is None


# --------------------------------------------------------------------------- #
# the consumer
# --------------------------------------------------------------------------- #


def test_boxes_for_prefers_page_box_and_falls_back_to_box():
    delta = {"slides": {"0": [
        {"box": [150, 250, 500, 600], "page_box": [1100, 2100, 1000, 1200]},
        {"box": [7, 7, 900, 900]},
        {"box": [0, 0, 0, 0]},                 # degenerate: still dropped
    ]}}
    assert assets._boxes_for(delta, 1) == [(1100, 2100, 1000, 1200),
                                           (7, 7, 900, 900)]
