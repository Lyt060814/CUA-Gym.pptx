"""The previous answer must not be on the desk when the question is asked again.

`archive_attempt`'s docstring says "Move a stage's artefacts into attempts/
before it runs again". It copies them. So on every repair-driven re-run the
old `recipe.json` was still there when the agent started, and an agent asked to
write a recipe that finds a complete, valid one already present reasonably
leaves it alone. `_left_over` then reads the unchanged mtime as "the agent
wrote nothing" and fails the deck.

Four times in run 11, costing deck0003 and deck0007 — both of which had
produced perfectly good recipes on earlier passes in the same run. Only ever on
a re-run, because only then is there a good answer already on disk.

    python3 -m pytest tests/test_previous_answer_removed.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import pipeline as pl                              # noqa: E402


def _deck(tmp_path) -> pl.Deck:
    root = tmp_path / "deck0007"
    root.mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"name": "d.pptx", "slides": 9}))
    return pl.Deck(root)


def test_archive_keeps_the_bytes(tmp_path):
    """The control, and the reason the copy was there in the first place: a
    re-run must not erase what the last one decided, or "it was fixed" and
    "the verdict was laundered" become indistinguishable afterwards."""
    deck = _deck(tmp_path)
    (deck.root / "recipe.json").write_text('{"slides": {"1": []}}')

    kept = pl.archive_attempt(deck, "recipe")
    assert kept
    saved = deck.root / kept / "recipe.json"
    assert saved.exists()
    assert json.loads(saved.read_text()) == {"slides": {"1": []}}


def test_the_live_file_is_what_the_agent_stage_clears(tmp_path):
    """`archive_attempt` itself still only copies — other callers rely on the
    artefact surviving. Clearing is the agent stage's decision about its own
    declared outputs, which is the narrowest place it can be made."""
    deck = _deck(tmp_path)
    (deck.root / "recipe.json").write_text('{"slides": {}}')
    pl.archive_attempt(deck, "recipe")
    assert (deck.root / "recipe.json").exists(), (
        "archive_attempt is a copy; the agent stage clears what it asks for")


def test_nothing_is_cleared_on_a_first_attempt(tmp_path):
    """`kept` is None when there was no previous artefact, and the clearing is
    conditioned on it. A first run must not be made to look like a re-run."""
    deck = _deck(tmp_path)
    assert pl.archive_attempt(deck, "recipe") is None


def test_a_stage_with_nothing_archived_returns_none(tmp_path):
    deck = _deck(tmp_path)
    (deck.root / "recipe.json").write_text("{}")
    assert pl.archive_attempt(deck, "materialised") is None
