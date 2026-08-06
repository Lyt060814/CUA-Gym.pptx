"""A verdict only means something about the inputs it was computed from.

Reported by deck0008, through the escalation channel, an hour after it was
built — the first real use of that channel and it was right:

    ESCALATED — scoring/e493033edf9d
    "The work order comes from a stale gate verdict: plan.json
     (written 11:04 against delta.json 12f01246)"

`work/` is restored whole on a resumed run, so `plan.json` came back from the
previous run. An `agent.py` change had invalidated `recipe`, `degraded` had run
again, and `delta.json` was no longer the file that verdict was decided
against. The repairer was being asked to fix a complaint about a state that no
longer existed, and no change to the recipe could ever have satisfied it.

The same shape as the lock that outlived its machine, found the same day: a
resume restores everything, and some of it only ever meant anything inside the
run that wrote it.

    python3 -m pytest tests/test_stale_verdict.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import cli, pipeline as pl                         # noqa: E402


def _deck(tmp_path) -> pl.Deck:
    root = tmp_path / "deck0008"
    root.mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"name": "d.pptx", "slides": 4}))
    (root / "delta.json").write_text(json.dumps({"slides": {}}))
    (root / "plan.json").write_text(json.dumps({
        "rejected": ["component floor above 0.15: c001/resize floor=0.25"]}))
    return pl.Deck(root)


def _record_scored_against_current_inputs(deck):
    """Mark `scored` as having run, fingerprinting whatever is on disk now."""
    deck.mark("scored", "rejected", components=10)


def test_a_verdict_decided_against_todays_files_is_used(tmp_path):
    """The control, and it has to come first: if this failed, every test below
    would pass for the wrong reason and the repair loop would be switched off
    entirely rather than made careful."""
    deck = _deck(tmp_path)
    _record_scored_against_current_inputs(deck)

    rework, source = cli._rework_of(deck)
    assert rework, "a current verdict is still a work order"
    assert source.startswith("plan.json")


def test_a_verdict_decided_against_a_delta_that_has_since_changed_is_skipped(
        tmp_path, capsys):
    """deck0008's case, reproduced."""
    deck = _deck(tmp_path)
    _record_scored_against_current_inputs(deck)

    # `degraded` runs again and rewrites the delta, exactly as it did when an
    # `agent.py` change invalidated `recipe`
    (deck.root / "delta.json").write_text(json.dumps({"slides": {"1": ["x"]}}))

    rework, source = cli._rework_of(deck)
    assert rework is None and source is None
    # and it says so, because a deck that silently finds no work order looks
    # identical to one that had nothing wrong with it
    assert "stale plan.json" in capsys.readouterr().out


def test_a_deck_in_the_repair_loop_is_not_starved_by_this(tmp_path):
    """The failure mode to avoid, and the reason this uses the input
    fingerprints rather than `stale()`.

    `stale()` also reports an upstream stage whose status is not `ok` — and a
    deck in the repair loop is precisely a deck with a rejection upstream.
    Using it here would discard every live verdict, no repair would ever be
    ordered again, and the loop would look like it had simply stopped finding
    anything to do.
    """
    deck = _deck(tmp_path)
    _record_scored_against_current_inputs(deck)
    deck.mark("reconciled", "rejected", verdict="needs_rework")

    assert pl.verdict_superseded(deck, "scored") is None
    rework, _ = cli._rework_of(deck)
    assert rework, "an upstream rejection is not what makes a verdict stale"


def test_a_verdict_recorded_before_fingerprints_existed_is_still_believed(
        tmp_path):
    """Judged the old way rather than declared stale on sight — the reading
    `stale()` already takes of a record with no `<code>`, and the alternative
    is discarding every verdict in every work directory at once."""
    deck = _deck(tmp_path)
    state = {"scored": {"status": "rejected"}}        # no `_in`
    (deck.root / "state.json").write_text(json.dumps(state))

    assert pl.verdict_superseded(deck, "scored") is None
    assert cli._rework_of(deck)[0]


def test_a_stage_that_never_ran_supersedes_nothing(tmp_path):
    deck = _deck(tmp_path)
    assert pl.verdict_superseded(deck, "scored") is None


def test_the_next_artefact_is_still_considered_after_a_stale_one(tmp_path):
    """Skipping is not stopping. A stale `plan.json` must not hide a live
    `consistency.json` underneath it."""
    deck = _deck(tmp_path)
    _record_scored_against_current_inputs(deck)
    (deck.root / "delta.json").write_text(json.dumps({"slides": {"1": ["x"]}}))
    (deck.root / "consistency.json").write_text(json.dumps({
        "findings": [{"check": "deg_without_delta", "severity": "fail",
                      "message": "d3 is recorded as 'approximated' but the "
                                 "delta records nothing for it"}]}))
    deck.mark("packaged", "rejected")

    rework, source = cli._rework_of(deck)
    assert rework and source.startswith("consistency.json")
