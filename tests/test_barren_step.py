"""A recipe step that runs and changes nothing.

deck0004 escalated this rather than spend its last repair attempt on it:

    ESCALATED — other/526cda09f06f
    "consistency.py fails the deck with 'd4 is recorded as approximated but
     the delta records nothing for it'"

The executor's loop is where it happens:

    entries += _stamp(fn(slide, shapes, step, rng), step.get("deg"))
    ...
    if entries: delta["slides"][str(idx)] = entries

An operator that finds nothing to act on returns nothing, contributes nothing,
and the loop moves on. `check_recipe` has already passed — the step is there —
and the package gate passes too, because a file nobody changed is a valid file.
The first thing to notice is `consistency`, six stages and two agent runs
later, by which point reconcile has read the *recipe* and written
`implemented: "approximated"` for a degradation that touched nothing.

Same class as an attack branch returning `False` on a placeholder, found the
same day on the other side of the pipeline: an operator that ran and did
nothing, with nobody checking.

    python3 -m pytest tests/test_barren_step.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import pipeline as pl                              # noqa: E402


def _deck(tmp_path, degradations=("d1", "d4")) -> pl.Deck:
    root = tmp_path / "deck0004"
    root.mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"name": "d.pptx", "slides": 8}))
    (root / "proposal.json").write_text(json.dumps({"tasks": [{
        "name": "t", "difficulty": "medium", "est_steps": 200,
        "instruction": "do it",
        "degradations": [{"id": d, "slides": [2]} for d in degradations]}]}))
    (root / "recipe.json").write_text(json.dumps({"slides": {}}))
    return pl.Deck(root)


def _run(deck, delta, monkeypatch):
    """Drive `degrade` with a delta the executor is pretending to have made."""
    from pptxgym import degrade_exec, pkg_check

    monkeypatch.setattr(degrade_exec, "run", lambda *a, **k: delta)
    monkeypatch.setattr(pkg_check, "check",
                        lambda *a, **k: {"problems": [], "duplicate_ids": []})
    monkeypatch.setattr(pkg_check, "leak_check",
                        lambda *a, **k: {"leaks": [], "dead_rels": []})
    return pl.degrade(deck)


def test_a_degradation_whose_step_changed_nothing_is_refused(tmp_path,
                                                             monkeypatch):
    """deck0004's case: d1 landed, d4 ran and produced nothing."""
    deck = _deck(tmp_path)
    delta = {"slides": {"1": [{"path": "0", "op": "resize", "deg": "d1"}]}}

    with pytest.raises(pl.StageError) as err:
        _run(deck, delta, monkeypatch)
    assert "'d4'" in str(err.value)
    assert "changed nothing" in str(err.value)
    assert deck.state()["degraded"]["status"] == "rejected"


def test_it_is_caught_at_degrade_not_six_stages_later(tmp_path, monkeypatch):
    """The point of moving it. `consistency` runs at `packaged`, after
    `materialise`, `reconcile`, `solvable`, `scored` and `hardened` — two of
    them agent stages — and the repair it then orders is against a recipe
    nobody can see the fault in."""
    deck = _deck(tmp_path)
    with pytest.raises(pl.StageError):
        _run(deck, {"slides": {"1": [{"op": "resize", "deg": "d1"}]}},
             monkeypatch)
    assert "degraded" in deck.state()
    for later in ("materialised", "reconciled", "scored", "hardened"):
        assert later not in deck.state()


def test_a_delta_that_covers_everything_passes(tmp_path, monkeypatch):
    """The control. If this failed, the check would be refusing every deck and
    the tests above would pass for the wrong reason."""
    deck = _deck(tmp_path)
    delta = {"slides": {"1": [{"op": "resize", "deg": "d1"},
                              {"op": "recolor", "deg": "d4"}]}}
    got = _run(deck, delta, monkeypatch)
    assert got["gate"] == "ok"


# --------------------------------------------------------------------------- #
# the deck-level operators, which do not put their entries under `slides`
# --------------------------------------------------------------------------- #


def test_a_degradation_implemented_by_a_reorder_is_not_barren(tmp_path,
                                                              monkeypatch):
    """Reading only `delta["slides"]` would fail a perfectly good deck whose
    degradation is a slide reorder — the check would then be a new way to
    lose decks rather than a way to catch a defect."""
    deck = _deck(tmp_path)
    delta = {"slides": {"1": [{"op": "resize", "deg": "d1"}]},
             "reorder_slides": {"from": [1, 2], "to": [2, 1], "deg": "d4"}}
    assert _run(deck, delta, monkeypatch)["gate"] == "ok"


def test_a_degradation_implemented_by_clearing_notes_is_not_barren(
        tmp_path, monkeypatch):
    deck = _deck(tmp_path)
    delta = {"slides": {"1": [{"op": "resize", "deg": "d1"}]},
             "cleared_notes": [{"slide": 3, "deg": "d4"}]}
    assert _run(deck, delta, monkeypatch)["gate"] == "ok"


def test_a_degradation_implemented_by_a_layout_edit_is_not_barren(
        tmp_path, monkeypatch):
    deck = _deck(tmp_path)
    delta = {"slides": {"1": [{"op": "resize", "deg": "d1"}]},
             "layout_edits": [{"layout": "Title", "deg": "d4"}]}
    assert _run(deck, delta, monkeypatch)["gate"] == "ok"


def test_a_malformed_delta_entry_does_not_end_the_stage(tmp_path, monkeypatch):
    """A string where a record was expected has killed three decks once
    already. It must read as "this entry attributes nothing", not as a crash."""
    deck = _deck(tmp_path, degradations=("d1",))
    delta = {"slides": {"1": ["not a record", {"op": "resize", "deg": "d1"}]}}
    assert _run(deck, delta, monkeypatch)["gate"] == "ok"


# --------------------------------------------------------------------------- #
# the same blind spot in the gate that actually rejects the deck
# --------------------------------------------------------------------------- #


def _facts(delta, degradations):
    from pptxgym import consistency as cs

    f = cs.DeckFacts()
    f.delta = delta
    f.task = {"degradations": degradations}
    return f


def _names(findings):
    return sorted(x.check for x in findings)


def test_consistency_reads_the_deck_level_keys_too():
    """deck0004 escalated this with the line number.

    An hour earlier `pipeline.degrade` had been taught to fold in the
    deck-level delta keys — and `consistency.check_deg_attribution`, the gate
    that was actually rejecting the deck, was left reading `slides` alone. The
    fix moved the copy and left the original, for the third time in one day.
    """
    from pptxgym import consistency as cs

    delta = {"slides": {"1": [{"op": "resize", "deg": "d1"}]},
             "reorder_slides": {"from": [1, 2], "to": [2, 1], "deg": "d4"}}
    got = cs.check_deg_attribution(_facts(delta, [
        {"id": "d1", "implemented": "as_proposed"},
        {"id": "d4", "implemented": "approximated", "slides": [1, 2]}]))
    assert "deg_without_delta" not in _names(got)


def test_a_degradation_located_nowhere_is_not_warned_about_per_slide():
    """A reorder changes the deck, not a page. Asking "did anything change on
    page N" of it would warn about every page it names, for a degradation
    implemented correctly."""
    from pptxgym import consistency as cs

    delta = {"slides": {}, "reorder_slides": {"deg": "d4"}}
    got = cs.check_deg_attribution(_facts(delta, [
        {"id": "d4", "implemented": "approximated", "slides": [3, 7, 9]}]))
    assert got == []


def test_a_degradation_nothing_implemented_is_still_caught():
    """The control. If the widening switched the check off, every test above
    would pass for the wrong reason and deck0004's original defect — an
    instruction describing damage the file does not carry — would ship."""
    from pptxgym import consistency as cs

    delta = {"slides": {"1": [{"op": "resize", "deg": "d1"}]}}
    got = cs.check_deg_attribution(_facts(delta, [
        {"id": "d1", "implemented": "as_proposed"},
        {"id": "d4", "implemented": "approximated"}]))
    assert "deg_without_delta" in _names(got)


def test_a_skipped_degradation_is_still_exempt():
    from pptxgym import consistency as cs

    got = cs.check_deg_attribution(_facts(
        {"slides": {"1": [{"op": "resize", "deg": "d1"}]}},
        [{"id": "d1", "implemented": "as_proposed"},
         {"id": "d4", "implemented": "skipped"}]))
    assert got == []


def test_cleared_notes_attribute_and_locate():
    from pptxgym import consistency as cs

    delta = {"slides": {}, "cleared_notes": [{"slide": 4, "deg": "d2"}]}
    got = cs.check_deg_attribution(_facts(delta, [
        {"id": "d2", "implemented": "as_proposed", "slides": [4]}]))
    assert got == []
