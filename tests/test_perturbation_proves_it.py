"""Every `wrong_params` branch has to make its own comparator say no.

A branch that returns `True` without moving anything the comparator reads is
worse than no branch at all. With no branch, `wrong_params` reports
`ops_without_a_branch`, the gate goes `unproven`, and the deck is rejected —
the safe direction. With a branch that changes the wrong thing, the entry is
counted as perturbed, the component scores 1.00 because its graded value was
never touched, and the attack certifies a task it did not test. That is the
failure this whole battery exists to prevent, committed by the battery.

So the test is not "did the XML change". It is:

    build the attack deck, run the real comparator on it, and require the
    score to fall — with a control on the same component proving the score
    would have been 1.0 had the branch left it alone.

Three branches were written, looked right, and did nothing the comparator
could see. One of them this file caught:

  * `_wrong_effects` replaced an `outerShdw` with a differently-parameterised
    `outerShdw`. The inventory records effects as a sorted list of *tag names*,
    so the two are the same value. Measured here at 1.00, on the first run.
  * `_wrong_z` sent the shape to the front when the graded peers were the ones
    below it, leaving every graded pair as the ground truth has them — caught
    by reading `_cmp_zorder` while writing this file, not by running it.
  * `_repaint_runs` painted text `7F007F` whatever colour it already wore —
    the same bug `_wrong_fill` and `_wrong_animation` each carry a paragraph
    about, still live in a third place.

Two of the three were found by inspection, which is worth saying plainly: this
file is not what makes the branches correct, it is what stops a wrong one from
passing silently. The control below is the part that does that work.

    python3 -m pytest tests/test_perturbation_proves_it.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import attacks as at                                 # noqa: E402
from pptxgym import comparators as C                              # noqa: E402
from test_attacks import _group, _sp, one_op_deck                 # noqa: E402

A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _styled(shape_id: int, name: str, text: str, *, rgb="FF0000", sz=1800,
            effects: bool = False) -> str:
    """A shape whose runs state their colour and size, so `text_runs` and
    `set_font` have something to grade rather than something inherited."""
    fx = (f'<a:effectLst><a:outerShdw blurRad="50800" dist="38100" '
          f'dir="2700000"><a:srgbClr val="000000"/></a:outerShdw>'
          f'</a:effectLst>' if effects else "")
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/>'
            f'<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>'
            f'<a:xfrm><a:off x="0" y="0"/><a:ext cx="900000" cy="500000"/>'
            f'</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            f'<a:solidFill><a:srgbClr val="00A0FF"/></a:solidFill>{fx}'
            f'</p:spPr>'
            f'<p:txBody><a:bodyPr/><a:lstStyle/><a:p>'
            f'<a:r><a:rPr lang="en-US" sz="{sz}" b="1">'
            f'<a:solidFill><a:srgbClr val="{rgb}"/></a:solidFill></a:rPr>'
            f'<a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>')


def _placeholder(shape_id: int, name: str) -> str:
    """A shape with **no `a:xfrm`**, the way a real placeholder is written.

    Position and size come from the layout. Every other fixture in this file
    states a transform, which is how a whole class of no-op perturbation
    survived a passing suite.
    """
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/>'
            f'<p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
            f'<p:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
            f'<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Title</a:t>'
            f'</a:r></a:p></p:txBody></p:sp>')


#: name -> (slide body, delta entry, plan component spec)
#:
#: One page each, because the question is per-operator and a bigger deck only
#: adds ways for a failure to be about something else.
CASES: dict[str, tuple] = {
    "rotate": (
        _sp(2, "Box", 0, 0, "A") + _sp(3, "Other", 100000, 0, "B"),
        {"path": "0", "op": "rotate", "deg": "d1", "kind": "autoshape"},
        {"path": "0"}),
    "zorder": (
        _sp(2, "Bottom", 0, 0, "A") + _sp(3, "Middle", 10000, 0, "B")
        + _sp(4, "Top", 20000, 0, "C"),
        {"path": "2", "op": "zorder", "deg": "d1", "kind": "autoshape",
         "to": "back"},
        {"path": "2", "to": "back"}),
    "clear_text": (
        _sp(2, "Box", 0, 0, "Hello") + _sp(3, "Other", 100000, 0, "B"),
        {"path": "0", "op": "clear_text", "deg": "d1", "kind": "autoshape"},
        {"path": "0"}),
    "set_text": (
        _sp(2, "Box", 0, 0, "Hello") + _sp(3, "Other", 100000, 0, "B"),
        {"path": "0", "op": "set_text", "deg": "d1", "kind": "autoshape"},
        {"path": "0"}),
    "swap": (
        _sp(2, "Box", 0, 0, "A") + _sp(3, "Other", 4000000, 0, "B"),
        {"path": "0", "op": "swap", "deg": "d1", "kind": "autoshape",
         "box": [0, 0, 900000, 500000]},
        {"path": "0"}),
    "text_runs": (
        _styled(2, "Box", "Hello") + _sp(3, "Other", 100000, 0, "B"),
        {"path": "0", "op": "text_runs", "deg": "d1", "kind": "autoshape",
         "touched": [{"paragraph": 0, "action": "restyled"}],
         "params": {"color": "00FF00", "size_pt": 40}},
        {"path": "0", "touched": [{"paragraph": 0, "action": "restyled"}],
         "params": {"color": "00FF00", "size_pt": 40}}),
    "text_runs_deleted": (
        _styled(2, "Box", "Hello") + _sp(3, "Other", 100000, 0, "B"),
        {"path": "0", "op": "text_runs", "deg": "d1", "kind": "autoshape",
         "touched": [{"paragraph": 0, "action": "deleted"}]},
        {"path": "0", "touched": [{"paragraph": 0, "action": "deleted"}]}),
    "ungroup": (
        _group(2, _sp(3, "Inner", 0, 0, "A") + _sp(4, "Inner2", 10000, 0, "B"))
        + _sp(5, "Outside", 4000000, 0, "C"),
        {"path": "0", "op": "ungroup", "deg": "d1", "kind": "group"},
        {"path": "0"}),
    "strip_effects": (
        _styled(2, "Box", "A", effects=True) + _sp(3, "Other", 100000, 0, "B"),
        {"path": "0", "op": "strip_effects", "deg": "d1", "kind": "autoshape",
         "removed": ["outerShdw"]},
        {"path": "0", "removed": ["outerShdw"]}),
}

#: The operators whose branch is not measured here, and why. Every one is
#: covered by `test_every_operator_is_either_perturbable_or_exempt_on_the_record`
#: — the branch exists — but "the branch exists" is the weaker claim, and the
#: gap between the two claims belongs on the record rather than in a silence.
NOT_MEASURED_HERE = {
    "crop": "needs a real image part; `_facet_crop` refuses a non-picture",
    "detach_connector": "needs a `p:cxnSp` pair with resolvable endpoints",
    "strip_transition": "needs a slide carrying `p:transition`",
    "anim_drop_steps": "needs a timing tree with build steps",
    "blank_slide": "shares `_perturb_delete`, measured through `delete`",
    "delete": "measured by the battery's own suite",
    "move": "measured by the battery's own suite",
    "scatter": "measured by the battery's own suite",
    "resize": "measured by the battery's own suite",
    "recolor": "measured by the battery's own suite",
    "outline": "measured by the battery's own suite",
    "set_font": "measured by the battery's own suite",
    "clear_table_cells": "measured by the battery's own suite",
    "table_drop_rows": "measured by the battery's own suite",
    "table_drop_cols": "measured by the battery's own suite",
    "strip_animation": "measured by the battery's own suite",
    "smartart_drop_nodes": "measured by the battery's own suite",
    "clear_notes": "needs a notesSlide part on the fixture deck",
    "chart_edit": "needs a chart part on the fixture deck",
}


def _perturbed_score(tmp_path, name: str) -> tuple[float, float, str]:
    """Returns (score against the attack deck, score against the ground truth).

    The second is the control: it is what the component scores when nothing was
    perturbed at all, and if it is not 1.0 the case is unscoreable and any
    drop in the first number means nothing.
    """
    body, entry, spec = CASES[name]
    op = entry["op"]
    component = {"id": "c001", "deg": "d1", "op": op, "slide": 0,
                 "gt_path": spec["path"], "weight": 1.0, "spec": spec}
    ctx = one_op_deck(tmp_path, f"deck-{name}", body, [entry],
                      plan_components=[component])
    built = at.ATTACKS["wrong_params"].build(ctx, tmp_path / f"{name}.pptx")

    gt_inv = C.inventory_pptx(ctx.gt_path)
    got, why = C._run_component(component, C.Scene(gt_inv,
                                                   C.inventory_pptx(built.path)))
    control, _ = C._run_component(component, C.Scene(gt_inv, gt_inv))
    return got, control, why


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_branch_makes_its_own_comparator_say_no(name, tmp_path):
    got, control, why = _perturbed_score(tmp_path, name)
    assert control == 1.0, (
        f"{name}: the control failed — the ground truth scores {control} "
        f"against itself, so this component is unscoreable and the case proves "
        f"nothing either way")
    assert got < 1.0, (
        f"{name}: `wrong_params` reported this entry as perturbed and its "
        f"comparator still scores it {got} ({why}). The attack would certify a "
        f"component it never tested — the exact failure the battery exists to "
        f"prevent, committed by the battery.")


def test_the_unmeasured_list_names_only_operators_that_exist():
    """A stale name here reads as coverage that was considered and is not."""
    from pptxgym import comparators

    # against the *comparator* registry: `smartart_drop_nodes` and friends are
    # graded ops that no `@op` produces, so the executor's registry is not the
    # set of things that can be a component.
    unknown = sorted(set(NOT_MEASURED_HERE) - set(comparators.REGISTRY))
    assert not unknown, f"not gradeable operators: {unknown}"


def test_every_branch_is_either_measured_here_or_listed_as_not():
    """The gap between "a branch exists" and "the branch works" is real, and
    this is what keeps it from being silent: a new branch must arrive with a
    measurement or with a written reason it has none."""
    measured = {CASES[name][1]["op"] for name in CASES}
    missing = sorted(set(at.PERTURB) - measured - set(NOT_MEASURED_HERE))
    assert not missing, (
        f"{missing} have a `wrong_params` branch that nothing measures. Add a "
        f"case to CASES, or a reason to NOT_MEASURED_HERE.")


# --------------------------------------------------------------------------- #
# the case every fixture above quietly avoided by stating `a:xfrm`
# --------------------------------------------------------------------------- #


def test_a_shape_that_inherits_its_position_can_still_be_moved():
    """A placeholder states no transform and takes its box from the layout.

    `_get_box` answered `None` for one, so `_perturb_move` returned `False` and
    `wrong_params` recorded "the branch for this operator changed nothing" — a
    gate it could not fire on a deck whose only fault was using placeholders.
    deck0003 was rejected for exactly this **one run after** the missing
    `text_runs` branch was fixed: the next thing underneath was a branch that
    existed and could not act. Two different failures behind one symptom, and
    only the second one is visible once the first is gone.

    This is asserted on the branch rather than through a comparator, and the
    reason is worth keeping. Scoring it needs the placeholder's *effective*
    geometry, which the inventory resolves from the layout part — and these
    fixture decks have no layouts, so `_cmp_position` scores such a shape 0.0
    even against itself. The measurement above would fail its own control and
    prove nothing. The real deck0003 component was graded, so its geometry did
    resolve; a comparator-level proof of this case needs a fixture deck with a
    layout, which is worth building and is not built.
    """
    from lxml import etree

    # the fixtures are fragments; the namespaces live on the slide element
    shape = etree.fromstring(
        f'<p:sld xmlns:p="{at.NS["p"]}" xmlns:a="{at.NS["a"]}">'
        f'{_placeholder(2, "Title 1")}</p:sld>'.encode())[0]
    assert at._get_box(shape) is None, "fixture must state no transform"

    entry = {"op": "move", "box": [1000000, 800000, 900000, 500000]}
    assert at._perturb_move(shape=shape, entry=entry) is True

    box = at._get_box(shape)
    assert box is not None, "the branch must write the transform it needs"
    assert box[0] != 1000000 and box[1] != 800000, (
        f"the shape is still where the ground truth had it: {box}")


def test_a_transform_with_nothing_in_it_is_not_a_successful_perturbation():
    """`_set_box` returned `True` unconditionally once it found an `a:xfrm`.

    An `a:xfrm` carrying neither `a:off` nor `a:ext` therefore reported a
    perturbation that changed nothing at all — the same lie as a missing
    branch, told in the affirmative.
    """
    from lxml import etree

    xml = (f'<p:sp xmlns:p="{at.NS["p"]}" xmlns:a="{at.NS["a"]}">'
           f'<p:spPr><a:xfrm/></p:spPr></p:sp>')
    shape = etree.fromstring(xml.encode())
    assert at._set_box(shape, x=5, y=5) is False
