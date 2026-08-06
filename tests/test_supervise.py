"""The supervisor, tested the way its predecessor was not.

Every case here is a way the old shell watcher misled its reader, plus the one
that matters most and is easiest to skip: **what it does when nothing is
wrong**. A detector that has never been run against a healthy input is not a
detector, and today's shell version alarmed on `429` matched inside `wget`'s
byte counts (`42900K`) during a perfectly good download.

The log excerpts are verbatim from runs 7 to 11.

    python3 -m pytest tests/test_supervise.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import supervise as sv                              # noqa: E402


# A run going well: eight decks moving, three of them finished. Verbatim.
HEALTHY = """
### run — eleven stages, ten decks, no WPS round trip  [10:29:04]
run 20260806-102904-10663 — work/runs/20260806-102904-10663/events.jsonl
  deck0001  digest 71KB (min 40KB)  13 renders
  deck0003  {'tasks': 1, 'detail': [{'name': 'repair-fairness-poster'}]}
  deck0005  8 change(s) on 4 slide(s)  gate=ok
  deck0005  4 asset(s): frames, reference_keyframes
  deck0001  {'verdict': 'solvable', 'leaks': 0, 'steps_measured': 290}
  deck0001  15 component(s)  gt=1.000  input=0.000  weights=steps_measured
  deck0001  12/14 attack(s), 3/6 variant(s)
  deck0001  task_4bd34b71bf33  15 component(s)  consistency=ok
  deck0005  task_4673005f2e99  8 component(s)  consistency=ok
  deck0007  task_8cf41208274f  9 component(s)  consistency=ok
"""

# The download whose byte counts the old watcher read as a rate limit.
NOISY_BUT_FINE = """
  /tmp/final.tar.gz           :  93%|=========.|  134MB /  144MB
     42900K .......... .......... .......... .......... 84% 12.5M 0s
New Data Upload               : 100%|==========|  249MB /  249MB, 21.0MB/s
"""


def _fold(text, now=1000.0):
    states, seen = sv.update({}, text, now=now)
    return states, seen


# --------------------------------------------------------------------------- #
# the control: a healthy run has to come back quiet, and say so
# --------------------------------------------------------------------------- #


def test_a_healthy_run_raises_nothing():
    states, _ = _fold(HEALTHY)
    assert sv.diagnose(states, now=1000.0) == []


def test_a_healthy_run_still_produces_a_report_that_says_what_it_checked():
    """"no alerts" printed alone is what a dead monitor prints too."""
    states, _ = _fold(HEALTHY)
    text = sv.report(states, [], now=1000.0, log_age_s=90)
    assert "nothing alarming" in text
    assert "3/4 packaged" in text          # 4 decks appear, 3 finished
    assert "1.5 minutes old" in text, "the age of the evidence is part of it"


def test_progress_bars_and_byte_counts_are_not_deck_lines():
    """`42900K` contains `429`. The old watcher cried rate-limit on it."""
    states, seen = _fold(NOISY_BUT_FINE)
    assert states == {} and seen == 0


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #


def test_packaged_beats_every_other_reading():
    assert sv.classify("task_8cf41208274f  9 component(s)  consistency=ok") \
        == "packaged"


def test_a_park_is_not_a_rejection():
    assert sv.classify("PARKED after 3 repair attempts — needs a human") \
        == "parked"


def test_a_rejection_that_also_reports_a_score_is_a_rejection():
    line = ("11 component(s)  gt=0.000  input=0.000   REJECTED — "
            "degradation(s) with no scoreable component: ['d5']")
    assert sv.classify(line) == "rejected"


def test_the_session_limit_is_infra_not_a_deck_problem():
    line = "INFRA after 6 attempt(s) — 429 You've hit your session limit"
    assert sv.classify(line) == "infra"


def test_an_escalation_outranks_the_park_it_causes():
    line = ("ESCALATED — attack/wrong_params/no-branch/text_runs "
            "(no perturbation branch); 2 unspent attempt(s) kept")
    assert sv.classify(line) == "escalated"


# --------------------------------------------------------------------------- #
# the three ways the old watcher lied
# --------------------------------------------------------------------------- #


def test_folding_the_same_log_twice_reacts_once():
    """Re-reading history as new is why its predecessor exited instantly on
    every launch and supervised nothing."""
    states, seen = sv.update({}, HEALTHY, now=1000.0)
    before = states["deck0001"].lines
    states, seen2 = sv.update(states, HEALTHY, now=1100.0, seen=seen)
    assert seen2 == seen
    assert states["deck0001"].lines == before
    assert states["deck0001"].last_seen_at == 1000.0, (
        "a deck that produced no new line has not been heard from again")


def test_a_growing_log_only_folds_the_new_part():
    states, seen = sv.update({}, HEALTHY, now=1000.0)
    more = HEALTHY + "  deck0003  PARKED after 3 repair attempts — needs a human\n"
    states, seen = sv.update(states, more, now=2000.0, seen=seen)
    assert states["deck0003"].kind == "parked"
    assert states["deck0001"].last_seen_at == 1000.0


def test_a_deck_that_has_gone_quiet_is_reported():
    states, _ = _fold(HEALTHY, now=0.0)
    alerts = sv.diagnose(states, now=60 * 60)      # an hour later
    quiet = [a for a in alerts if a.what.startswith("quiet")]
    # ...but only the ones still in flight: a packaged deck is quiet on purpose
    assert {a.deck for a in quiet} == {"deck0003"}


def test_a_settled_deck_is_never_reported_as_stalled():
    states, _ = _fold(HEALTHY, now=0.0)
    assert states["deck0001"].settled()
    alerts = sv.diagnose(states, now=99 * 60 * 60)
    assert not [a for a in alerts if a.deck == "deck0001"]


# --------------------------------------------------------------------------- #
# the loop detector — the thing that would have caught deck0003 on day one
# --------------------------------------------------------------------------- #


LOOPING = """
  deck0003  8/14 attack(s)   REJECTED — wrong_params: unproven gate — d4/move on slide 1 (the branch for this operator changed nothing)
  deck0003  repaired (attempt 1) after attacks.json:rejected, re-running from ['recipe']
  deck0003  8/14 attack(s)   REJECTED — wrong_params: unproven gate — d4/move on slide 1 (the branch for this operator changed nothing)
"""


def test_the_same_complaint_twice_is_a_loop():
    """deck0003 received an identical work order three times and spent every
    attempt on a defect the repairer had no power to fix. Two is enough to
    say so; waiting for the third is watching it burn."""
    states, _ = _fold(LOOPING)
    alerts = sv.diagnose(states, now=1000.0)
    loops = [a for a in alerts if "same complaint" in a.what]
    assert len(loops) == 1 and loops[0].deck == "deck0003"
    assert loops[0].level == "stop"


def test_two_different_complaints_are_not_a_loop():
    """deck0006 was rejected three times for three different reasons, which is
    the repair loop working. Calling that a loop would cry at every retry."""
    text = """
  deck0006  REJECTED — degradation(s) with no scoreable component: ['d5']
  deck0006  REJECTED — coherence: no_full_page_overlay fires on `half_restore`
  deck0006  REJECTED — half_restore: 0.305 outside 0.35..0.65
"""
    states, _ = _fold(text)
    assert not [a for a in sv.diagnose(states, now=1000.0)
                if "same complaint" in a.what]


def test_the_same_complaint_with_different_numbers_still_counts():
    """`0.305` and `0.481` are the same defect twice, not two defects."""
    text = """
  deck0006  REJECTED — half_restore: 0.305 outside 0.35..0.65
  deck0006  REJECTED — half_restore: 0.481 outside 0.35..0.65
"""
    states, _ = _fold(text)
    assert [a for a in sv.diagnose(states, now=1000.0)
            if "same complaint" in a.what]


def test_an_escalated_deck_is_reported_once_and_not_also_as_a_loop():
    text = ("  deck0003  ESCALATED — attack/wrong_params/no-branch/move "
            "(no perturbation branch)\n")
    states, _ = _fold(text)
    alerts = sv.diagnose(states, now=1000.0)
    assert len(alerts) == 1 and alerts[0].what == "escalated"


# --------------------------------------------------------------------------- #
# state that survives a restart
# --------------------------------------------------------------------------- #


def test_state_round_trips(tmp_path):
    states, seen = _fold(HEALTHY)
    path = tmp_path / "s.json"
    sv.save(path, states, seen)
    back, seen_back = sv.load(path)
    assert seen_back == seen
    assert back["deck0001"].kind == "packaged"
    assert back["deck0001"].last_seen_at == states["deck0001"].last_seen_at


def test_a_corrupt_state_file_starts_over_rather_than_raising(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{half written")
    assert sv.load(path) == ({}, 0)


def test_no_state_file_starts_over(tmp_path):
    assert sv.load(tmp_path / "nope.json") == ({}, 0)


# --------------------------------------------------------------------------- #
# two false alarms the fixtures could not have caught
#
# Both found by running this module against a real 6400-line log instead of
# against the excerpts above. Fixtures agree with whoever wrote them.
# --------------------------------------------------------------------------- #


def test_the_closing_summary_is_not_a_stream_of_new_events():
    """`### where it got to` re-lists every deck in the same two-space format.

    Read on, and one summary becomes ten fresh "events", every deck's final
    state becomes whatever the summary last said about it, and four settled
    decks came back reading `progress`.
    """
    text = HEALTHY + """
### where it got to  [12:02:20]
  deck0003  attacks.json:rejected  → recipe: The attack battery beat this task
  deck0004  consistency.json:fail  → recipe: The instruction and the files contradict
"""
    states, _ = _fold(text)
    assert "deck0004" not in states, "deck0004 never appeared in the run itself"
    # deck0003 keeps the last thing the *run* said about it, not the last thing
    # the summary did
    assert states["deck0003"].last.startswith("{'tasks': 1")


def test_a_deck_the_renderer_cannot_open_is_not_a_repair_loop():
    """deck0002 and deck0010 each reported the same failure twice because the
    *run* retries `inspect` — not because a repair is going round in circles.

    A deck LibreOffice will not convert is a fact that repeats every time it
    is asked. Both were flagged `[stop] same complaint 2x` on the first real
    log, and a detector that alarms on the two decks nobody can fix teaches
    its reader to skip it.
    """
    text = """
  deck0002  FAILED — deck0002: source.pptx: soffice exited 1: Unspecified Application Error (after 3 attempts)
  deck0002  FAILED — deck0002: source.pptx: soffice exited 1: Unspecified Application Error (after 3 attempts)
  deck0010  FAILED — deck0010: source.pptx: rendered 13 of 14 slides (after 3 attempts)
  deck0010  FAILED — deck0010: source.pptx: rendered 13 of 14 slides (after 3 attempts)
"""
    states, _ = _fold(text)
    assert not [a for a in sv.diagnose(states, now=1000.0)
                if "same complaint" in a.what]


def test_a_repeated_rejection_is_still_a_loop_after_that_change():
    """The negative control. Narrowing the set must not have switched the
    detector off — deck0003's identical work order three times is the case it
    exists for."""
    states, _ = _fold(LOOPING)
    assert [a for a in sv.diagnose(states, now=1000.0)
            if "same complaint" in a.what]


def test_the_runs_own_summary_is_not_narration_either():
    """There are two summaries, and cutting at only the second left a residue.

    `run` prints its stage table and a "N deck(s) waiting on a repair" block
    *before* the job script prints `### where it got to`. Those work-order
    lines are in the same two-space format, so four decks that had finished
    came back reading `progress` with a work order as their last event —
    deck0003 showed `attacks.json:rejected → recipe:` instead of `PARKED`.
    """
    text = """
  deck0003  PARKED after 3 repair attempts — needs a human
stages: 1·ingested  2·inspected  3·proposed
deck0003 ✓ ✓ ✓ ✓ ✓ ✓ H ≈ ≈ ↺ ·  batch200_003
4 deck(s) waiting on a repair
  deck0003  attacks.json:rejected  → recipe: The attack battery beat this task
  deck0004  consistency.json:fail  → recipe: The instruction and the files
### where it got to  [12:02:20]
  deck0003  attacks.json:rejected  → recipe: The attack battery beat this task
"""
    states, _ = _fold(text)
    assert states["deck0003"].kind == "parked"
    assert "deck0004" not in states


def test_a_deck_waiting_on_a_lock_is_not_progress():
    """It read as `progress` — a line arrived and nothing else matched — so
    two decks held by a dead run's pid looked like they were working for
    twenty minutes. "Waiting" and "working" must not print the same."""
    text = ("  deck0006  BUSY — deck0006 is locked by pid 10663 running "
            "'hardened' since 2026-08-06T11:54:12\n")
    states, _ = _fold(text)
    assert states["deck0006"].kind == "blocked"
    alerts = sv.diagnose(states, now=1000.0)
    assert [a for a in alerts if a.what == "waiting on a lock"]


def test_a_stored_verdict_is_re_read_with_todays_rules(tmp_path):
    """A cached judgement from superseded code, in the tool whose whole job is
    not to mislead its reader.

    `update` only classifies lines it has not seen, so a deck that has gone
    quiet keeps the kind it was given when its last line arrived. Minutes after
    `BUSY` became its own state, the two decks stuck on a dead run's lock were
    still printing `progress` — their kind predated the `blocked` kind, and a
    stuck deck is by definition never going to produce the new line that would
    re-read it.
    """
    path = tmp_path / "s.json"
    st = sv.DeckState("deck0006", kind="progress", lines=1,
                      last="BUSY — deck0006 is locked by pid 10663 running "
                           "'hardened' since 2026-08-06T11:54:12")
    sv.save(path, {"deck0006": st}, 1)

    back, _ = sv.load(path)
    assert back["deck0006"].kind == "blocked"
    assert [a for a in sv.diagnose(back, now=1000.0)
            if a.what == "waiting on a lock"]


# --------------------------------------------------------------------------- #
# noise, which is how a monitor stops being read
# --------------------------------------------------------------------------- #


def test_a_deck_the_renderer_cannot_open_is_not_reported_as_quiet():
    """deck0002 and deck0010 were reported "quiet 32m" on every poll. True,
    and also exactly what a finished deck looks like. Nothing more is going to
    happen to them in this run and the table already says `failed`."""
    text = ("  deck0002  FAILED — deck0002: source.pptx: soffice exited 1: "
            "Unspecified Application Error (after 3 attempts)\n")
    states, _ = _fold(text, now=0.0)
    assert sv.diagnose(states, now=60 * 60) == []


def test_a_deck_is_not_reported_twice_for_one_situation():
    """A deck waiting on a lock is quiet *because* it is waiting. Printing
    both `waiting on a lock` and `quiet 32m` teaches the reader to skim."""
    text = ("  deck0006  BUSY — deck0006 is locked by pid 10663 running "
            "'hardened' since 2026-08-06T11:54:12\n")
    states, _ = _fold(text, now=0.0)
    alerts = sv.diagnose(states, now=60 * 60)
    assert [a.what for a in alerts] == ["waiting on a lock"]


def test_a_genuinely_silent_deck_is_still_reported():
    """The negative control: suppressing the duplicate must not switch the
    stall detector off. A deck mid-stage that has said nothing for an hour is
    the case it exists for."""
    text = "  deck0004  6 change(s) on 5 slide(s)  gate=ok\n"
    states, _ = _fold(text, now=0.0)
    assert [a.what.split()[0] for a in sv.diagnose(states, now=60 * 60)] \
        == ["quiet"]
