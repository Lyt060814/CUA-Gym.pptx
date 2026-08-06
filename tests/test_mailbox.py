"""The reply channel, and mostly what it refuses.

This file is read by a process holding write tokens for three repositories and
a `GH_TOKEN` it uses to check out whatever the reply names. So the interesting
cases are not the ones that work — they are the malformed, the ambiguous and
the repeated.

    python3 -m pytest tests/test_mailbox.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import mailbox as mb                              # noqa: E402

FIX = {"signature": "attack/wrong_params/no-branch/text_runs",
       "verdict": "fixed", "commit": "2af5e09293ca",
       "note": "branch added, 27 of 30 operators covered now"}


# --------------------------------------------------------------------------- #
# what it accepts
# --------------------------------------------------------------------------- #


def test_a_fix_names_a_commit_and_a_defect():
    got = mb.parse({"replies": [FIX]})
    assert got[0]["verdict"] == "fixed"
    assert got[0]["commit"] == "2af5e09293ca"


def test_a_reply_may_name_decks_instead_of_a_signature():
    got = mb.parse({"replies": [{"decks": ["deck0007"], "verdict": "stop",
                                 "note": "looping"}]})
    assert got[0]["decks"] == ["deck0007"]


# --------------------------------------------------------------------------- #
# what it refuses, which is the part that matters
# --------------------------------------------------------------------------- #


def test_an_unknown_verdict_is_refused():
    """`do` is a closed set. The reader holds three write tokens, and "run
    this" is not a thing this channel is allowed to say."""
    with pytest.raises(mb.BadReply):
        mb.parse({"replies": [{"signature": "x", "verdict": "run",
                               "commit": "deadbeef"}]})


def test_a_reply_addressed_to_nobody_is_refused():
    with pytest.raises(mb.BadReply):
        mb.parse({"replies": [{"verdict": "wontfix"}]})


def test_fixed_without_a_commit_is_refused():
    """There would be nothing to check out, and the run would carry on against
    the code that produced the defect while recording that it was fixed."""
    with pytest.raises(mb.BadReply):
        mb.parse({"replies": [{"signature": "x", "verdict": "fixed"}]})


@pytest.mark.parametrize("bad", [
    "main", "HEAD", "../../etc/passwd", "; rm -rf /", "2af5e09; echo",
    "not-a-hash", "abc",
])
def test_only_a_commit_id_is_a_commit_id(bad):
    """It reaches `git checkout` inside a container holding a `GH_TOKEN`."""
    with pytest.raises(mb.BadReply):
        mb.parse({"replies": [{"signature": "x", "verdict": "fixed",
                               "commit": bad}]})


def test_a_missing_replies_list_is_refused():
    with pytest.raises(mb.BadReply):
        mb.parse({"at": "now"})


def test_a_reply_that_is_not_a_record_is_refused():
    with pytest.raises(mb.BadReply):
        mb.parse([FIX])


# --------------------------------------------------------------------------- #
# reading is never fatal
# --------------------------------------------------------------------------- #


def test_an_unreadable_reply_is_a_frontend_that_did_not_answer(tmp_path):
    """Which is the state the run was already in. Dying of a malformed reply
    would turn the supervision channel into a way to lose the batch."""
    p = tmp_path / mb.FILENAME
    p.write_text("{ half written")
    assert mb.read(p) == []


def test_a_reply_that_does_not_exist_reads_as_nothing(tmp_path):
    assert mb.read(tmp_path / mb.FILENAME) == []


def test_one_bad_entry_does_not_smuggle_the_others_through(tmp_path):
    """`parse` is all-or-nothing on purpose: a file where entry 2 is malformed
    is a file somebody got wrong, and acting on entries 1 and 3 of it is
    guessing at what they meant."""
    p = tmp_path / mb.FILENAME
    p.write_text(json.dumps({"replies": [FIX, {"verdict": "nonsense"}]}))
    assert mb.read(p) == []


# --------------------------------------------------------------------------- #
# applying it once
# --------------------------------------------------------------------------- #


def test_a_reply_is_not_acted_on_twice(tmp_path):
    """The frontend cannot know when a run picked its reply up, so the file
    stays published. A run that polls twice would check out the same commit
    again and count a `wontfix` against a deck's budget a second time."""
    applied = tmp_path / mb.APPLIED
    replies = mb.parse({"replies": [FIX]})

    fresh = mb.unapplied(replies, applied)
    assert len(fresh) == 1
    mb.mark_applied(fresh, applied)

    assert mb.unapplied(replies, applied) == []


def test_a_corrupt_applied_file_means_starting_over_not_crashing(tmp_path):
    applied = tmp_path / mb.APPLIED
    applied.write_text("[[[")
    assert len(mb.unapplied(mb.parse({"replies": [FIX]}), applied)) == 1


def test_a_new_reply_still_gets_through_after_an_old_one(tmp_path):
    applied = tmp_path / mb.APPLIED
    mb.mark_applied(mb.parse({"replies": [FIX]}), applied)
    second = mb.parse({"replies": [{"signature": "scoring/e493033edf9d",
                                    "verdict": "fixed",
                                    "commit": "627d92a1bb00"}]})
    assert len(mb.unapplied(second, applied)) == 1


# --------------------------------------------------------------------------- #
# one fix, every deck behind it
# --------------------------------------------------------------------------- #


def test_a_signature_resumes_every_deck_standing_behind_it():
    """The economic argument for the whole channel. Ten decks blocked by one
    missing branch is one check, one fix and ten resumptions — not ten
    investigations."""
    escalations = [
        {"deck": "deck0001", "signature": FIX["signature"]},
        {"deck": "deck0004", "signature": FIX["signature"]},
        {"deck": "deck0009", "signature": "something/else"},
    ]
    got = mb.targets(mb.parse({"replies": [FIX]})[0], escalations)
    assert got == ["deck0001", "deck0004"]


def test_named_decks_beat_the_signature():
    """A reply that names decks is being deliberately narrow — usually
    "this one is different" — and must not be widened back out."""
    reply = mb.parse({"replies": [{**FIX, "decks": ["deck0004"]}]})[0]
    escalations = [{"deck": "deck0001", "signature": FIX["signature"]},
                   {"deck": "deck0004", "signature": FIX["signature"]}]
    assert mb.targets(reply, escalations) == ["deck0004"]


def test_publishing_validates_before_writing(tmp_path):
    """Catch a malformed reply on the side that can still do something about
    it, rather than in a container an hour later."""
    with pytest.raises(mb.BadReply):
        mb.publish(tmp_path / mb.FILENAME,
                   [{"signature": "x", "verdict": "fixed", "commit": "main"}])
    assert not (tmp_path / mb.FILENAME).exists()


def test_a_published_reply_reads_back(tmp_path):
    p = mb.publish(tmp_path / mb.FILENAME, [FIX], run="brun-20260806T132024Z")
    assert mb.read(p)[0]["commit"] == "2af5e09293ca"
    assert json.loads(p.read_text())["run"] == "brun-20260806T132024Z"
