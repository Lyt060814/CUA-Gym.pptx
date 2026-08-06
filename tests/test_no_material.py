"""A check the deck offers no ground for is a gap, not a verdict on the deck.

deck0003 is a single-page task. `damage_untouched` needs a page outside the
task to break and there isn't one; `half_restore` needs a page-disjoint half
to restore and there isn't one. Neither says anything is wrong with the deck,
and both rejected it:

    damage_untouched:    unproven gate — every page of this deck is part of the task
    damage_untouched_gt: unproven gate — every page of this deck is part of the task
    half_restore:        unproven gate — 5 components collapse into 1 page-disjoint unit(s)

Under that rule no task concentrated on a few pages can ever ship — the
pipeline refusing decks for its own shape, which is the thing the front end
exists to stop.

The distinction is deliberately narrow. `Unconstructible` still means "this
applies here and we could not build it" and still rejects: an unproven gate is
indistinguishable from one that would have failed. `NoMaterial` means the deck
has no ground of that shape at all. "No damaged page could be covered", "no
hole had a surviving shape to clone" and "no shape could be renamed" describe
damage we *had* and could not use, and are left alone.

And the gap is never silent — it is counted, it is named, and a battery where
*nothing* scored still rejects, because zero coverage is not proof.

Measured on the real deck, whole sweep:

    deck0003  5 rejection reasons -> 1 (`noop`, a recipe that under-damages)
              9/14 attacks scored, 3 no_material, named in the coverage line

    python3 -m pytest tests/test_no_material.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import attacks as at                                # noqa: E402


def _report(*rows) -> at.Report:
    return at.Report("deck0003", ["c1"], list(rows))


def _row(name, status, note="why") -> at.Row:
    return at.Row(name, "what", "expect", status, note=note)


def test_a_check_with_no_material_does_not_reject_the_deck():
    """deck0003's three. A single-page task is a task."""
    rep = _report(_row("noop", "scored"),
                  _row("damage_untouched", "no_material"),
                  _row("half_restore", "no_material"))
    assert rep.reasons == []
    assert rep.rejected is False


def test_a_check_that_had_material_and_could_not_use_it_still_rejects():
    """The line that must not move. `Unconstructible` is why the battery is
    worth anything: a gate that never fired is indistinguishable from one that
    would have failed."""
    rep = _report(_row("noop", "scored"),
                  _row("clone_spam", "unconstructible",
                       "no hole had a surviving shape to clone"))
    assert any("unproven gate" in why for why in rep.reasons)


def test_a_battery_where_nothing_scored_rejects_anyway():
    """The floor under the whole rule. One check with nothing to work on is a
    gap; a battery with nothing to work on is a deck nothing was proved about,
    and shipping it for having asked no answerable question is a worse failure
    than the one this rule fixes."""
    rep = _report(_row("damage_untouched", "no_material"),
                  _row("half_restore", "no_material"))
    assert rep.rejected is True
    assert any("nothing in the battery could be scored" in why
               for why in rep.reasons)


def test_the_gap_is_counted_and_named():
    """No silent caps. A reader of the summary has to be able to tell a deck
    that survived fourteen checks from one that survived nine and had three
    that could not be asked."""
    rep = _report(_row("noop", "scored"),
                  _row("damage_untouched", "no_material"))
    cov = rep.coverage()
    assert cov["attacks_no_material"] == 1
    assert "damage_untouched (no_material)" in cov["attacks_not_scored"]
    assert "damage_untouched (no_material)" in rep.coverage_line()


def test_the_gap_travels_with_the_emitted_task():
    """`harden` writes a caveat for it, the same way it does for a sweep run
    without WPS. A task proved by nine checks and one proved by fourteen must
    not read the same downstream."""
    import inspect

    from pptxgym import pipeline as pl

    src = inspect.getsource(pl.harden)
    assert 'r.status == "no_material"' in src
    assert "caveats.append" in src.split('r.status == "no_material"')[1]


def test_no_material_is_a_kind_of_unconstructible():
    """Every `except Unconstructible` in the tree still catches it — the
    builder loop tells them apart by type, and a handler that does not care
    about the difference must not have to know there is one."""
    assert issubclass(at.NoMaterial, at.Unconstructible)


# --------------------------------------------------------------------------- #
# the two raise sites, so a later edit cannot quietly demote a real rejection
# --------------------------------------------------------------------------- #


def test_a_deck_with_no_bystander_page_raises_no_material(tmp_path):
    from test_attacks import make_deck

    ctx = make_deck(tmp_path)
    ctx.untouched_slide = lambda pkg: None
    for name in ("damage_untouched", "damage_untouched_gt"):
        with pytest.raises(at.NoMaterial):
            at.ATTACKS[name].build(ctx, tmp_path / f"{name}.pptx")


def test_a_deck_with_no_page_disjoint_half_raises_no_material(tmp_path):
    from test_attacks import make_deck

    ctx = make_deck(tmp_path)
    ctx.component_slides = lambda: {"d1": {0}, "d2": {0}, "d3": {0}}
    with pytest.raises(at.NoMaterial):
        at.ATTACKS["half_restore"].build(ctx, tmp_path / "half.pptx")


def test_the_checks_that_had_damage_and_failed_still_raise_the_parent():
    """`wrong_params` reporting "this attack was credited for ground it did
    not touch" is the pipeline's own gap and rejects — demoting it would hide
    exactly the finding the escalation channel was built to carry."""
    import inspect

    src = inspect.getsource(at)
    for message in ("no hole had a surviving shape to clone",
                    "no shape could be renamed",
                    "no damaged page could be covered",
                    "this attack was credited for ground it did not touch"):
        line = next(l for l in src.splitlines() if message in l)
        start = src.index(line)
        assert "NoMaterial" not in src[max(0, start - 200):start + len(line)], \
            f"{message!r} was demoted to a coverage gap"
