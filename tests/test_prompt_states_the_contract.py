"""A gate may not demand something no prompt asks for.

`check_reconcile` rejects a deck when `materialise` left an asset unmet and the
task record neither answers `needs_rework` nor names the asset kind in prose —
matched by substring, so the record has to contain the literal string
`reference_image`.

Nothing told the agent. The reconcile prompt handed over `manifest.json` as one
path among six and never mentioned `unmet`; the reconcile skill does not
contain the word. Four decks across three runs were rejected for failing to
meet a contract that existed only inside the check.

These tests pin the requirement to the prompt, in both directions: the prompt
must state it when there is something to state, and must not invent it when
there is not.

    python3 -m pytest tests/test_prompt_states_the_contract.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import agent, pipeline as pl                        # noqa: E402


def _deck(tmp_path, unmet=None) -> pl.Deck:
    root = tmp_path / "deck0001"
    (root / "assets").mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"name": "d.pptx", "slides": 4}))
    (root / "proposal.json").write_text(json.dumps({"tasks": [{
        "name": "t", "difficulty": "medium", "est_steps": 120,
        "instruction": "do it",
        "degradations": [{"id": "d1", "slides": [2, 3]}]}]}))
    if unmet is not None:
        (root / "assets" / "manifest.json").write_text(
            json.dumps({"task": "t", "produced": [], "unmet": unmet}))
    return pl.Deck(root)


def test_the_prompt_states_what_could_not_be_produced(tmp_path):
    deck = _deck(tmp_path, unmet=[
        {"kind": "reference_image", "slides": [3],
         "why": "masking slide 3 would cover 71% of it"}])
    text = agent.reconcile_prompt(deck)

    # the fact
    assert "reference_image" in text
    assert "slide" in text and "3" in text
    assert "71%" in text, "the reason has to travel, not just the kind"
    # and both ways out of it, because a requirement with no stated remedy is
    # the same dead end in a longer form
    assert "needs_rework" in text
    assert "notes" in text


def test_the_prompt_says_the_match_is_literal(tmp_path):
    """The gate is a substring match. An agent told only "acknowledge it" can
    write "the masked render was unavailable" and still be rejected."""
    deck = _deck(tmp_path, unmet=[
        {"kind": "reference_keyframes", "slides": [2], "why": "no animation"}])
    text = agent.reconcile_prompt(deck)
    assert "literal" in text.lower()
    assert "reference_keyframes" in text


def test_nothing_is_said_when_nothing_was_unmet(tmp_path):
    """The control. A prompt that always warns teaches its reader to skip the
    warning, and every token here competes with the rest of the task."""
    deck = _deck(tmp_path, unmet=[])
    text = agent.reconcile_prompt(deck)
    assert "COULD NOT PRODUCE" not in text


def test_a_deck_with_no_manifest_at_all_is_fine(tmp_path):
    """`materialise` may not have run. Not knowing is not the same as knowing
    there is a problem, and this must not raise either way."""
    deck = _deck(tmp_path, unmet=None)
    text = agent.reconcile_prompt(deck)
    assert "COULD NOT PRODUCE" not in text
    assert "Reconcile one degraded PPT task" in text


def test_an_unreadable_manifest_does_not_end_the_stage(tmp_path):
    """The most volatile input to a prompt must not be what kills the deck —
    the same lesson `solvability_prompt` carries about a shape-tolerant
    summary, which three decks died of in the first cold run."""
    deck = _deck(tmp_path, unmet=[])
    (deck.root / "assets" / "manifest.json").write_text("{not json")
    text = agent.reconcile_prompt(deck)
    assert "Reconcile one degraded PPT task" in text


def test_the_gate_accepts_what_the_prompt_asks_for(tmp_path):
    """End to end on the contract itself: a record that does what the prompt
    demands passes the check that motivated the prompt.

    Without this the two could drift apart and each look right alone — which
    is exactly the state that produced the bug.
    """
    deck = _deck(tmp_path, unmet=[
        {"kind": "reference_image", "slides": [3], "why": "too much masked"}])
    (deck.root / "task.json").write_text(json.dumps({
        "name": "t", "difficulty": "medium", "est_steps": 120,
        "instruction": "do it", "instruction_changed": False,
        "assets": [],
        "notes": "the reference_image for slide 3 could not be produced, so "
                 "the instruction no longer offers one",
        "degradations": [{"id": "d1", "slides": [2, 3]}]}))
    got = pl.check_reconcile(deck)
    assert got["verdict"] is None or got["verdict"] != "needs_rework"


def test_the_gate_still_refuses_a_record_that_says_nothing(tmp_path):
    """The negative control for the test above: if this passed, the gate would
    be accepting everything and the one above would prove nothing."""
    deck = _deck(tmp_path, unmet=[
        {"kind": "reference_image", "slides": [3], "why": "too much masked"}])
    (deck.root / "task.json").write_text(json.dumps({
        "name": "t", "difficulty": "medium", "est_steps": 120,
        "instruction": "do it", "instruction_changed": False,
        "assets": [], "notes": "looks fine",
        "degradations": [{"id": "d1", "slides": [2, 3]}]}))
    with pytest.raises(pl.StageError) as err:
        pl.check_reconcile(deck)
    assert "could not be produced" in str(err.value)


# --------------------------------------------------------------------------- #
# the same failure, one stage earlier
# --------------------------------------------------------------------------- #


def _proposal_deck(tmp_path, degradations) -> pl.Deck:
    root = tmp_path / "deck0002"
    root.mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"name": "d.pptx", "slides": 9}))
    (root / "proposal.json").write_text(json.dumps({"tasks": [{
        "name": "t", "difficulty": "medium", "est_steps": 200,
        "instruction": "do it", "slides": [2, 5],
        "degradations": degradations}]}))
    return pl.Deck(root)


def test_the_recipe_prompt_lists_the_ids_it_demands(tmp_path):
    """`check_recipe` requires every declared degradation id to be the `deg` of
    some step. The prompt said "Implement every one of them" and named none of
    them — the count was there, the ids were in a file. That is the most
    frequent rejection in the pipeline: five times across three runs, and twice
    the same id on the same deck after a repair had been spent on it.
    """
    deck = _proposal_deck(tmp_path, [
        {"id": "d1", "slides": [2], "anchor": "the twin chart on slide 4"},
        {"id": "d5", "slides": [5], "anchor": "the caption below it"}])
    text = agent.recipe_prompt(deck)

    assert "d1" in text and "d5" in text
    assert "the twin chart on slide 4" in text
    assert "`deg`" in text, "it has to say where the id must appear"


def test_the_recipe_prompt_survives_a_degradation_that_is_not_a_record(tmp_path):
    """A summary must not be what ends a deck. Three decks died in the first
    cold run because an agent wrote a list of strings where records were
    expected and the summariser assumed otherwise."""
    deck = _proposal_deck(tmp_path, ["d1", {"id": "d2", "slides": [5]}])
    text = agent.recipe_prompt(deck)
    assert "d1" in text and "d2" in text
