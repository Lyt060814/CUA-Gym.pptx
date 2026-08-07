"""The foreman, held to its own claim: it judges nothing.

Every case is a boundary of the mechanical verdict. A deck ships on the
record — `packaged: ok`, a REVIEW.md, gt=1/input=0, a whole bundle — and
parks on anything else, with the reason named. The one thing the foreman is
allowed to *do* to a deck's content is revert an edit to the shared tools,
and that path is tested the way the repair loop's was: the guard fires, the
deck parks, shipping state notwithstanding.

    python3 -m pytest tests/test_foreman.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import foreman as fm                                # noqa: E402
from pptxgym import pipeline as pl                               # noqa: E402
from pptxgym import agent as agentmod                            # noqa: E402


def _args(**over):
    base = dict(workers=1, max_turns=150, timeout=300, model="opus",
                effort="high", specialist_model="opus",
                specialist_effort="high", roundtrip=False, force=False,
                deck=None)
    base.update(over)
    return Namespace(**base)


def _deck(tmp_path, state=None, review=False, slides=5) -> pl.Deck:
    root = tmp_path / "deck0001"
    root.mkdir(parents=True, exist_ok=True)
    (root / "meta.json").write_text(json.dumps({"name": "t.pptx",
                                                "slides": slides}))
    (root / "state.json").write_text(json.dumps(state or {}))
    if review:
        (root / "REVIEW.md").write_text("# REVIEW\n")
    return pl.Deck(root)


#: The record of a deck that really shipped, as trial 1 wrote it.
SHIPPED = {
    "inspected": {"status": "ok"},
    "scored": {"status": "ok", "gt": 1.0, "input": 0.0},
    "packaged": {"status": "ok", "task_id": "abc123"},
}


def _run(deck, tmp_path, args, monkeypatch, result=None, on_spawn=None):
    """Drive run_deck with the agent and the guard both stubbed out."""
    async def fake_agent(spec):
        if on_spawn:
            on_spawn(spec)
        return dict(result or {"status": "exited", "returncode": 0})

    monkeypatch.setattr(agentmod, "run_agent", fake_agent)
    monkeypatch.setattr(pl, "tool_tree_state", lambda: "clean")
    monkeypatch.setattr(pl, "revert_tool_changes", lambda d, b, l: None)
    monkeypatch.setattr(pl, "bundle_problems", lambda d: [])
    return asyncio.run(fm.run_deck(deck, tmp_path, args))


# --------------------------------------------------------------------------- #
# the mission says what the run needs and nothing it must not
# --------------------------------------------------------------------------- #


def test_mission_names_the_deck_the_budget_and_the_boundaries(tmp_path):
    deck = _deck(tmp_path, SHIPPED, review=True, slides=19)
    text = fm.mission(deck, tmp_path, 150, "opus", "high")
    assert deck.id in text
    assert str(tmp_path.resolve()) in text
    assert "--model opus --effort high" in text
    assert "150" in text
    assert "not yours to change" in text          # the no-code-edit clause
    assert "do not read them" in text             # other decks are off limits
    assert "REVIEW.md" in text


def test_mission_carries_the_specialist_assignment_it_was_given(tmp_path):
    deck = _deck(tmp_path, SHIPPED)
    text = fm.mission(deck, tmp_path, 80, "sonnet", "max")
    assert "--model sonnet --effort max" in text


# --------------------------------------------------------------------------- #
# the mechanical verdict
# --------------------------------------------------------------------------- #


def test_a_deck_with_the_full_record_ships(tmp_path, monkeypatch):
    deck = _deck(tmp_path, SHIPPED, review=True)
    rec = _run(deck, tmp_path, _args(), monkeypatch)
    assert rec["outcome"] == "shipped"
    assert rec["task"] == "abc123"
    assert json.loads((deck.root / "foreman.json").read_text())["outcome"] \
        == "shipped"


def test_no_review_md_means_parked_and_says_so(tmp_path, monkeypatch):
    deck = _deck(tmp_path, SHIPPED, review=False)
    rec = _run(deck, tmp_path, _args(), monkeypatch)
    assert rec["outcome"] == "parked"
    assert "REVIEW.md" in rec["why"]


def test_a_recorded_score_short_of_the_floor_parks(tmp_path, monkeypatch):
    state = dict(SHIPPED, scored={"status": "ok", "gt": 1.0, "input": 0.35})
    deck = _deck(tmp_path, state, review=True)
    rec = _run(deck, tmp_path, _args(), monkeypatch)
    assert rec["outcome"] == "parked"
    assert "input=0.35" in rec["why"]


def test_not_packaged_parks_with_the_orchestrators_last_words(tmp_path,
                                                              monkeypatch):
    deck = _deck(tmp_path, {"inspected": {"status": "ok"}}, review=True)
    rec = _run(deck, tmp_path, _args(), monkeypatch,
               result={"status": "exited", "returncode": 0,
                       "why": "no task worth shipping on this deck"})
    assert rec["outcome"] == "parked"
    assert "packaged" in rec["why"]
    assert "no task worth shipping" in rec.get("last", "")


def test_a_truncated_orchestrator_is_parked_as_such(tmp_path, monkeypatch):
    deck = _deck(tmp_path, SHIPPED, review=True)
    rec = _run(deck, tmp_path, _args(), monkeypatch,
               result={"status": "truncated", "kind": "max_turns",
                       "why": "the run stopped on max_turns"})
    assert rec["outcome"] == "parked"
    assert "max_turns" in rec["why"]


# --------------------------------------------------------------------------- #
# the guard: tools are everybody's, and shipping does not launder an edit
# --------------------------------------------------------------------------- #


def test_a_tool_edit_parks_the_deck_even_when_the_record_ships(tmp_path,
                                                               monkeypatch):
    deck = _deck(tmp_path, SHIPPED, review=True)

    async def fake_agent(spec):
        return {"status": "exited", "returncode": 0}

    monkeypatch.setattr(agentmod, "run_agent", fake_agent)
    monkeypatch.setattr(pl, "tool_tree_state", lambda: "before")
    monkeypatch.setattr(pl, "revert_tool_changes",
                        lambda d, b, l: "1 tool file(s): 1 reverted "
                                        "(pptxgym/comparators.py)")
    monkeypatch.setattr(pl, "bundle_problems", lambda d: [])
    rec = asyncio.run(fm.run_deck(deck, tmp_path, _args()))
    assert rec["outcome"] == "parked"
    assert "shared tools" in rec["why"]
    assert "comparators.py" in rec["why"]


# --------------------------------------------------------------------------- #
# the request loop: a broken verb is asked about, not died of
# --------------------------------------------------------------------------- #


def test_an_unanswered_toolfix_parks_as_tool_defect(tmp_path, monkeypatch):
    deck = _deck(tmp_path, {"inspected": {"status": "ok"}}, review=True)
    (deck.root / "toolfix.json").write_text(json.dumps(
        {"verb": "score", "what": "gt component crashes on rotated group",
         "repro": "python3 -m pptxgym.cli --work w score --deck d"}))
    rec = _run(deck, tmp_path, _args(), monkeypatch)
    assert rec["outcome"] == "parked"
    assert rec["kind"] == "tool_defect"
    assert rec["why"].startswith("tool_defect: score — gt component crashes")


def test_an_answered_toolfix_does_not_relabel_the_park(tmp_path, monkeypatch):
    deck = _deck(tmp_path, {"inspected": {"status": "ok"}}, review=True)
    (deck.root / "toolfix.json").write_text(json.dumps(
        {"verb": "score", "what": "crash"}))
    (deck.root / "toolfix-answer.json").write_text(json.dumps(
        {"fixed": True, "head": "abc"}))
    rec = _run(deck, tmp_path, _args(), monkeypatch)
    assert rec["outcome"] == "parked"                # still not packaged
    assert rec.get("kind") != "tool_defect"          # but not the verb's fault
    assert not rec["why"].startswith("tool_defect")


def test_an_authorized_head_move_is_the_fix_landing_not_a_violation(
        tmp_path, monkeypatch):
    deck = _deck(tmp_path, SHIPPED, review=True)
    (tmp_path / "authorized-heads.jsonl").write_text(
        json.dumps({"head": "fix1sha", "why": "mid-run toolfix"}) + "\n")

    async def fake_agent(spec):
        return {"status": "exited", "returncode": 0}

    monkeypatch.setattr(agentmod, "run_agent", fake_agent)
    monkeypatch.setattr(pl, "tool_tree_state", lambda: "before")
    monkeypatch.setattr(pl, "revert_tool_changes", lambda d, b, l: "HEAD moved")
    monkeypatch.setattr(pl, "code_version",
                        lambda: {"commit": "fix1sha", "dirty": False})
    monkeypatch.setattr(pl, "bundle_problems", lambda d: [])
    rec = asyncio.run(fm.run_deck(deck, tmp_path, _args()))
    assert rec["outcome"] == "shipped"


def test_an_unauthorized_head_move_still_parks(tmp_path, monkeypatch):
    deck = _deck(tmp_path, SHIPPED, review=True)

    async def fake_agent(spec):
        return {"status": "exited", "returncode": 0}

    monkeypatch.setattr(agentmod, "run_agent", fake_agent)
    monkeypatch.setattr(pl, "tool_tree_state", lambda: "before")
    monkeypatch.setattr(pl, "revert_tool_changes", lambda d, b, l: "HEAD moved")
    monkeypatch.setattr(pl, "code_version",
                        lambda: {"commit": "roguesha", "dirty": False})
    monkeypatch.setattr(pl, "bundle_problems", lambda d: [])
    rec = asyncio.run(fm.run_deck(deck, tmp_path, _args()))
    assert rec["outcome"] == "parked"
    assert "shared tools" in rec["why"]


# --------------------------------------------------------------------------- #
# prep: deterministic work happens before the owner is spawned
# --------------------------------------------------------------------------- #


def test_an_uninspected_deck_is_inspected_first(tmp_path, monkeypatch):
    deck = _deck(tmp_path, {}, review=True)
    calls = []
    monkeypatch.setattr(pl, "inspect",
                        lambda d, roundtrip=False: calls.append(d.id))
    rec = _run(deck, tmp_path, _args(), monkeypatch)
    assert calls == ["deck0001"]
    assert rec["outcome"] == "parked"        # nothing packaged; that is fine


def test_inspect_failure_parks_without_spawning(tmp_path, monkeypatch):
    deck = _deck(tmp_path, {})
    spawned = []

    def broken(d, roundtrip=False):
        raise pl.StageError("render produced 0 pages")

    monkeypatch.setattr(pl, "inspect", broken)
    rec = _run(deck, tmp_path, _args(), monkeypatch,
               on_spawn=lambda s: spawned.append(s.name))
    assert rec["outcome"] == "parked"
    assert "inspect failed" in rec["why"]
    assert spawned == []


# --------------------------------------------------------------------------- #
# picking decks: shipped ones stay shipped unless somebody insists
# --------------------------------------------------------------------------- #


def test_pick_decks_skips_shipped_unless_forced(tmp_path, monkeypatch):
    _deck(tmp_path, SHIPPED, review=True)
    monkeypatch.setattr(pl, "bundle_problems", lambda d: [])
    assert fm.pick_decks(tmp_path, _args()) == []
    picked = fm.pick_decks(tmp_path, _args(force=True))
    assert [d.id for d in picked] == ["deck0001"]


def test_pick_decks_honours_an_explicit_list(tmp_path):
    picked = fm.pick_decks(tmp_path, _args(deck=["deck0042"]))
    assert [d.id for d in picked] == ["deck0042"]
