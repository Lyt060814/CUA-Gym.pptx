"""The fast profile: one owner, declared work, every measurement kept.

What these pin down is the boundary. `fast` lets the owner write the three
judgement artefacts, but only through `adopt`, which runs the same checker
and says who wrote it — and `full` cannot adopt at all. The sealed probe is
outside the concession under every profile, because a witness that the deck
can write is not a witness.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import cli                                          # noqa: E402
from pptxgym import foreman                                      # noqa: E402
from pptxgym import pipeline as pl                               # noqa: E402
from pptxgym import profiles                                     # noqa: E402


GOOD_TASK = {
    "name": "restore-the-quarterly-chart",
    "difficulty": "medium",
    "est_steps": 120,
    "instruction": "Slide 4's revenue chart is gone and its caption is "
                   "wrong. Rebuild the chart and fix the caption.",
    "degradations": [{
        "id": "d1", "scope": "slide", "slides": [4],
        "what_breaks": "the chart is deleted",
        "agent_will_do": "rebuild it from the table on slide 3",
        "anchor": "the revenue table on slide 3 carries every value",
        "disclosure": "named",
        "disclosure_detail": "the instruction names slide 4 and the chart",
    }],
}


class Args:
    def __init__(self, **kw):
        self.__dict__.update({"stage": "proposed", "force": False,
                              "profile": profiles.FAST, **kw})


def _deck(tmp_path, state=None, proposal=None):
    root = tmp_path / "deck0001"
    root.mkdir(parents=True, exist_ok=True)
    (root / "digest.json").write_text('{"slides": []}')
    (root / "state.json").write_text(json.dumps(state or {}))
    if proposal is not None:
        (root / "proposal.json").write_text(json.dumps(proposal))
    return pl.Deck(root)


# --------------------------------------------------------------------------- #
# which profile is in force
# --------------------------------------------------------------------------- #


def test_the_profile_comes_from_the_flag_then_the_environment(monkeypatch):
    monkeypatch.delenv(profiles.PROFILE_ENV, raising=False)
    assert profiles.profile(Args(profile=None)) == profiles.FULL
    monkeypatch.setenv(profiles.PROFILE_ENV, profiles.FAST)
    assert profiles.profile(Args(profile=None)) == profiles.FAST
    assert profiles.profile(Args(profile=profiles.FULL)) == profiles.FULL


def test_an_unknown_profile_is_refused(monkeypatch):
    monkeypatch.setenv(profiles.PROFILE_ENV, "quick")
    with pytest.raises(ValueError, match="unknown profile"):
        profiles.profile(Args(profile=None))


# --------------------------------------------------------------------------- #
# adopt
# --------------------------------------------------------------------------- #


def test_the_full_profile_cannot_adopt(tmp_path):
    """The concession belongs to one profile. Without this, `fast` would not
    be a profile at all — it would be a permission every deck has."""
    deck = _deck(tmp_path)
    with pytest.raises(pl.StageError, match="fast-profile verb"):
        cli._adopt_one(deck, Args(profile=profiles.FULL))


def test_the_probe_is_never_adoptable(tmp_path):
    deck = _deck(tmp_path)
    with pytest.raises(pl.StageError, match="sealed on purpose"):
        cli._adopt_one(deck, Args(stage="solvable"))


def test_adopt_runs_the_checker_and_refuses_rubbish(tmp_path):
    """File-existence as a success test is how a batch fills up with
    plausible rubbish — `adopt` is not a shortcut past that."""
    deck = _deck(tmp_path)                       # no proposal.json at all
    line = cli._adopt_one(deck, Args())
    assert "REJECTED" in line and "no proposal.json" in line
    assert deck.state()["proposed"]["status"] == "rejected"


def test_an_adopted_record_carries_fingerprints_and_its_author(tmp_path):
    """The hand-written record's two defects, both fixed here: `mark` stamps
    `_in`, so the stage is not stale the moment it is written, and the record
    says who wrote it, so nobody has to guess later."""
    deck = _deck(tmp_path, proposal={"deck_read": "x", "tasks": [GOOD_TASK]})
    line = cli._adopt_one(deck, Args())
    assert "adopted proposed" in line

    rec = deck.state()["proposed"]
    assert rec["status"] == "ok"
    assert rec["adopted"] is True and rec["by"] == "orchestrator"
    assert rec["profile"] == profiles.FAST
    assert rec["_in"]["digest.json"]                  # a real fingerprint
    assert deck.done("proposed")                      # therefore not stale
    assert not deck.stale("proposed")


def test_adopt_will_not_quietly_replace_a_specialists_run(tmp_path):
    """deck0004's actual failure: the specialist had succeeded four minutes
    earlier with four degradations, and the owner wrote three over the top."""
    deck = _deck(tmp_path,
                 state={"proposed": {"status": "ok", "tasks": 1,
                                     "log": "/w/deck0001/proposed.jsonl",
                                     "_in": {}}},
                 proposal={"deck_read": "x", "tasks": [GOOD_TASK]})
    line = cli._adopt_one(deck, Args())
    assert "refused" in line and "not yours to replace" in line
    assert deck.state()["proposed"]["log"]            # untouched


# --------------------------------------------------------------------------- #
# the witness gate, under both profiles
# --------------------------------------------------------------------------- #


def test_an_adopted_stage_passes_the_witness_gate(tmp_path):
    deck = _deck(tmp_path, state={
        "proposed": {"status": "ok", "adopted": True,
                     "profile": profiles.FAST, "by": "orchestrator"},
        "recipe": {"status": "ok", "adopted": True,
                   "profile": profiles.FAST, "by": "orchestrator"},
        "reconciled": {"status": "ok", "adopted": True,
                       "profile": profiles.FAST, "by": "orchestrator"},
        "solvable": {"status": "ok", "model_asked": "haiku"},
    })
    assert foreman.unwitnessed(deck) == ""


def test_the_adopted_flag_alone_does_not_launder_a_full_profile_deck(tmp_path):
    """Both halves are required. Otherwise a full-profile deck launders a
    hand-written stage by adding one key to it."""
    deck = _deck(tmp_path, state={
        "proposed": {"status": "ok", "adopted": True},        # no profile
        "reconciled": {"status": "ok", "model_asked": "opus"},
        "solvable": {"status": "ok", "model_asked": "sonnet"},
    })
    assert "proposed" in foreman.unwitnessed(deck)


def test_an_adopted_probe_is_still_refused(tmp_path):
    """`adopt` will not produce this record, but the gate does not rely on
    that: the sealed witness is checked on its own terms."""
    deck = _deck(tmp_path, state={
        "solvable": {"status": "ok", "adopted": True,
                     "profile": profiles.FAST, "by": "orchestrator"},
    })
    assert "solvable" in foreman.unwitnessed(deck)


# --------------------------------------------------------------------------- #
# the brief
# --------------------------------------------------------------------------- #


def test_the_fast_brief_names_what_changes_and_what_does_not(tmp_path):
    deck = _deck(tmp_path)
    (deck.root / "meta.json").write_text('{"slides": 12}')
    brief = foreman.mission(deck, tmp_path, 220, {}, profile=profiles.FAST)
    assert "fast profile" in brief
    assert "adopt --deck" in brief
    assert "ppt-task-proposal" in brief          # the design manual, by name
    assert "solvable" in brief                   # the witness that stays
    assert "fourteen-attack battery" in brief    # nothing relaxed to save time

    full = foreman.mission(deck, tmp_path, 220, {})
    assert "fast profile" not in full


def test_a_checker_rejection_is_recorded_against_its_own_stage(tmp_path):
    """deck0003's real state.json grew a `stage: crashed` entry reading
    "says easy but 120 steps is medium" — a checker doing its job, dressed up
    as the pipeline falling over, because `adopt` was the one stage verb that
    let its StageError reach `_guarded`."""
    thin = dict(GOOD_TASK, difficulty="easy", est_steps=120)
    deck = _deck(tmp_path, proposal={"deck_read": "x", "tasks": [thin]})
    line = cli._adopt_one(deck, Args())

    assert line.startswith("deck0001  REJECTED")
    st = deck.state()
    assert "stage" not in st                      # no bogus key
    assert st["proposed"]["status"] == "rejected"
    assert "medium" in st["proposed"]["error"]
    assert not deck.done("proposed")              # downstream still blocked


# --------------------------------------------------------------------------- #
# what a deck still owes
# --------------------------------------------------------------------------- #


def _run_deck_outstanding(deck):
    """The closure `run_deck` builds, exercised without spawning anything."""
    import json as _json

    ok, why = foreman.shipped(deck)
    if ok:
        return None
    try:
        declined = not (_json.loads(deck.proposal.read_text()).get("tasks"))
    except (OSError, ValueError):
        declined = False
    if declined and (deck.root / "REVIEW.md").exists():
        return None
    return why


def test_a_deck_that_stopped_mid_run_is_handed_back(tmp_path):
    """deck0030's real ending: seven stages paid for, the probe launched in
    the background, and "I'm pausing here to await the notification" — with a
    REVIEW.md written as the brief asks. Under the old test that read as a
    finished argument and the deck was parked."""
    deck = _deck(tmp_path,
                 state={"reconciled": {"status": "ok", "log": "/w/r.jsonl"}},
                 proposal={"deck_read": "x", "tasks": [GOOD_TASK]})
    (deck.root / "REVIEW.md").write_text("# notes so far\nthe probe is running")

    assert _run_deck_outstanding(deck)          # owes something


def test_a_deck_that_argued_a_no_is_left_alone(tmp_path):
    """deck0005: an empty proposal with a written reason, in 2.7 minutes.
    Pushing an agent that has argued its case buys the argument again."""
    deck = _deck(tmp_path, proposal={"deck_read": "x", "tasks": [],
                                     "no_task_reason": "no usable structure"})
    (deck.root / "REVIEW.md").write_text("# no\nevery candidate rejected")

    assert _run_deck_outstanding(deck) is None
