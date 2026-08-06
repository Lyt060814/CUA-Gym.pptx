"""What a run leaves behind, and what a killed one must not.

Two things, both learned from one ninety-minute batch of ten decks.

The run had no run-level record.  Eleven stages, four repair rounds, thirteen
session errors and two parked decks left excellent *per-deck* evidence and
nothing at all about the run: reconstructing which deck went back to which
stage, when and why meant opening ten directories by hand and correlating them
against a console log that carried neither timestamps nor stage names.  Every
test in the first half is a question that post-mortem had to answer the hard
way.

And a process killed mid-stage leaves whatever the agent had written by then on
disk.  Resume is otherwise correct — `promoted()` requires `ok`, so the stage
simply runs again — but a re-run that *fails without writing* hands the checker
the corpse of the previous attempt, which is well formed by construction and
passes.  The retry path already refuses to do this; the killed-process path did
not.

    python3 -m pytest tests/test_runlog.py -q
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import agent                                        # noqa: E402
from pptxgym import cli                                          # noqa: E402
from pptxgym import pipeline as pl                               # noqa: E402


# --------------------------------------------------------------------------- #
# scaffolding
# --------------------------------------------------------------------------- #


def _args(work, **kw):
    base = dict(work=str(work), deck=None, workers=1, cpu_workers=None,
                force=False, dpi=110, model=None, effort=None,
                fallback_model=None, timeout=1, api_retries=0,
                until=pl.STAGES[-1], list=False, run=None, at=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _deck(work, name="deck0001"):
    d = pl.Deck(Path(work) / name)
    d.root.mkdir(parents=True, exist_ok=True)
    (d.root / "meta.json").write_text(
        json.dumps({"slides": 3, "name": "a deck", "checksum": "ab" * 8}))
    d.mark("inspected", "ok")
    return d


@pytest.fixture
def closed_run():
    """No run is open when a test starts, and none is left open when it ends."""
    pl.close_run()
    yield
    pl.close_run()


#: A `claude` that writes what it is told to and nothing more.  `WROTE=` empty
#: is the whole point of half of this file: a process that exits cleanly having
#: produced nothing, which is what a re-run after a kill does when it fails.
FAKE = '''
import json, os, sys
out, body = os.environ.get("FAKE_OUT", ""), os.environ.get("FAKE_BODY", "")
if out and body:
    open(out, "w").write(body)
print(json.dumps({"type": "system", "subtype": "init",
                  "model": "claude-opus-5[1m]"}, separators=(",", ":")))
print(json.dumps({"type": "result", "subtype": "success",
                  "terminal_reason": "end_turn", "result": "done",
                  "modelUsage": {"claude-opus-5[1m]": {"outputTokens": 10}}},
                 separators=(",", ":")))
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    d.mkdir()
    (d / "claude").write_text(f"#!{sys.executable}\n" + FAKE)
    (d / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")


def _events(work) -> list:
    return pl.read_events(pl.latest_run(work))


def _kinds(events, kind) -> list:
    return [e for e in events if e.get("event") == kind]


# --------------------------------------------------------------------------- #
# the header: the limits as resolved, not as typed
# --------------------------------------------------------------------------- #


def test_the_header_carries_the_limit_that_actually_bound_the_run(tmp_path,
                                                                  closed_run):
    """`--workers` is an alias for `--agent-workers`.  A reader that went back
    to the argv looking for the long form found nothing, fell back to the
    default of 1, and reported a utilisation of 328%.  The number a reader
    divides by has to be in the file."""
    args = cli.build_parser().parse_args(["run", "--workers", "6"])
    lim = cli.resolved_limits(args)
    assert lim["agent_workers"] == 6
    assert lim["cpu_workers"] == cli._default_cpu_workers()   # never in argv
    assert lim["threads"] == cli._executor_size(6, lim["cpu_workers"])

    log = pl.open_run(tmp_path, argv=["pptxgym", "run", "--workers", "6"],
                      limits=lim, decks=10)
    head = pl.read_events(log.path)[0]
    assert head["event"] == "run_started"
    assert head["limits"]["agent_workers"] == 6
    assert "--workers" in head["argv"]          # and the typed form is kept too
    assert head["decks"] == 10


def test_the_header_says_which_code_made_the_run(tmp_path, closed_run):
    """A run made from a dirty tree is not reproducible, and half the surprises
    in the pilot were a stage behaving differently because somebody had edited
    it mid-batch."""
    log = pl.open_run(tmp_path, argv=["pptxgym", "run"], limits={})
    head = pl.read_events(log.path)[0]
    assert "commit" in head and "dirty" in head
    assert head["pid"] == os.getpid()


# --------------------------------------------------------------------------- #
# a log you cannot tail during the run is not a debugging tool
# --------------------------------------------------------------------------- #


def test_every_record_is_on_disk_before_the_next_one_is_written(tmp_path,
                                                                closed_run):
    """The console log was redirected with `nohup` and sat at zero bytes for
    twenty minutes, because Python block-buffers a redirected stdout.  This
    file is read by a second reader while the first is still writing it."""
    log = pl.open_run(tmp_path, argv=["pptxgym", "run"], limits={})
    for i in range(3):
        log.emit("stage_started", deck=f"deck{i:04d}", stage="proposed")
        # a *separate* handle, as `tail -f` is: nothing here closed the file
        assert len(pl.read_events(log.path)) == i + 2

    with open(log.path) as fh:                  # and it really is one per line
        assert len([ln for ln in fh if ln.strip()]) == 4


def test_a_run_killed_mid_record_is_still_readable(tmp_path, closed_run):
    """The run somebody most wants to read is the one that was killed."""
    log = pl.open_run(tmp_path, argv=["pptxgym", "run"], limits={})
    log.emit("stage_started", deck="deck0001", stage="recipe")
    with open(log.path, "a") as fh:
        fh.write('{"event": "stage_fini')          # the machine went away here
    events = pl.read_events(log.path)
    assert [e["event"] for e in events] == ["run_started", "stage_started"]


def test_with_no_run_open_nothing_is_recorded_and_nothing_breaks(tmp_path):
    """Every emit is a no-op outside a run: a single-stage command run by hand,
    a test, an import.  The pipeline behaves exactly as it did before."""
    pl.close_run()
    deck = _deck(tmp_path)
    deck.begin("proposed")
    deck.mark("proposed", "ok", tasks=1)
    assert pl.run_log() is None
    assert not (tmp_path / pl.RUNS).exists()


# --------------------------------------------------------------------------- #
# what a run's own record has to contain
# --------------------------------------------------------------------------- #


def test_a_stage_records_when_it_started_what_it_decided_and_how_long(
        tmp_path, closed_run):
    """`/tmp/arun.log` had no timestamps and no stage names, so every timing in
    the post-mortem came from the observer's samples or from file mtimes."""
    pl.open_run(tmp_path, argv=["pptxgym", "run"], limits={})
    deck = _deck(tmp_path)
    deck.begin("recipe")
    time.sleep(0.01)
    deck.mark("recipe", "ok", steps=4)

    started, finished = _kinds(_events(tmp_path), "stage_started"), \
        _kinds(_events(tmp_path), "stage_finished")
    assert [(e["deck"], e["stage"]) for e in started] == [("deck0001", "recipe")]
    last = finished[-1]
    assert (last["deck"], last["stage"], last["status"]) == \
        ("deck0001", "recipe", "ok")
    assert last["ms"] >= 10 and last["steps"] == 4
    assert last["t"] and last["ts"]                     # both clocks, per record


@pytest.mark.parametrize("status", ["ok", "rejected", "failed", "infra",
                                    "needs_human", "crashed", "skipped"])
def test_every_outcome_reaches_the_run_log_and_not_only_the_good_ones(
        status, tmp_path, closed_run):
    """A gate saying no, an outage nobody judged, a parked deck and a crash are
    the four things a post-mortem is about."""
    pl.open_run(tmp_path, argv=["pptxgym", "run"], limits={})
    _deck(tmp_path).mark("solvable", status, error="whatever it was")
    assert [e["status"] for e in _kinds(_events(tmp_path), "stage_finished")
            if e["stage"] == "solvable"] == [status]


def test_a_skip_is_an_event(tmp_path, closed_run):
    """The single most common thing a resumed run does, and it used to leave no
    trace at all — so a resumed run's log and a run that never started read
    exactly the same."""
    pl.open_run(tmp_path, argv=["pptxgym", "propose"], limits={})
    deck = _deck(tmp_path)
    deck.mark("proposed", "ok", tasks=1)
    line = cli._propose_one(deck, _args(tmp_path))

    assert "already proposed" in line
    skips = _kinds(_events(tmp_path), "stage_skipped")
    assert [(e["deck"], e["stage"], e["why"]) for e in skips] == \
        [("deck0001", "proposed", cli.SKIP_DONE)]
    assert skips[0]["was"] == "ok" and skips[0]["since"]


def test_a_stage_blocked_by_the_one_above_it_is_a_different_skip(tmp_path,
                                                                 closed_run):
    """"It was already done" and "it could not start" are opposite facts about
    a run, and a log that spelled both "nothing happened" would not distinguish
    a resumed batch from a stalled one."""
    pl.open_run(tmp_path, argv=["pptxgym", "recipe"], limits={})
    deck = _deck(tmp_path)                       # inspected, never proposed
    cli._recipe_one(deck, _args(tmp_path))
    assert [e["why"] for e in _kinds(_events(tmp_path), "stage_skipped")] == \
        [cli.SKIP_UPSTREAM]


def test_a_resumed_run_says_what_it_found_already_done(tmp_path, closed_run):
    """A finished deck costs 0.4s and, until now, produced not one line
    anywhere saying which stages that 0.4s covered."""
    pl.open_run(tmp_path, argv=["pptxgym", "run"], limits={})
    deck = _deck(tmp_path)
    for s in pl.STAGES:
        deck.mark(s, "ok")

    async def main():
        await cli._run_one(deck, _args(tmp_path), cli.Pools(agent=1, cpu=1))

    asyncio.run(main())
    skipped = {e["stage"] for e in _kinds(_events(tmp_path), "stage_skipped")}
    assert skipped == set(pl.STAGES) - {"ingested"}
    assert not _kinds(_events(tmp_path), "stage_started")     # nothing ran


def test_the_repair_that_sent_a_deck_back_is_one_event(tmp_path, closed_run,
                                                       monkeypatch):
    """"Which deck went back to which stage, when, and why" is the question the
    whole file exists for, and it is the one thing no per-deck artefact
    records: `invalidate_from` deletes the state it invalidates."""
    pl.open_run(tmp_path, argv=["pptxgym", "run"], limits={})
    deck = _deck(tmp_path)
    deck.mark("reconciled", "ok")
    (deck.root / "task.json").write_text(json.dumps({"verdict": "ready"}))
    (deck.root / "plan.json").write_text(json.dumps({"rejected": ["floor 0.55"]}))

    async def _wrote(spec):
        for p in spec.outputs:
            p.write_text('{"slides": {"1": []}}')
        return {"status": "exited", "returncode": 0, "log": str(spec.log)}

    monkeypatch.setattr(cli.agentmod, "run_agent", _wrote)
    monkeypatch.setattr(cli.pl, "tool_tree_state", lambda: None)
    cli._repair_one(deck, _args(tmp_path))

    sent = _kinds(_events(tmp_path), "sent_back")
    assert len(sent) == 1
    assert sent[0]["deck"] == "deck0001" and sent[0]["to"] == ["recipe"]
    assert sent[0]["source"].startswith("plan.json")
    assert sent[0]["attempt"] == 1


def test_a_field_a_stage_happens_to_call_stage_cannot_shadow_the_real_one(
        tmp_path, closed_run):
    """The stream is navigated by `deck` and `stage`; a detail key colliding
    with one of them would send a record to the wrong deck."""
    pl.open_run(tmp_path, argv=["pptxgym", "run"], limits={})
    _deck(tmp_path).mark("scored", "ok", deck="deck9999", event="nonsense",
                         components=3)
    e = _kinds(_events(tmp_path), "stage_finished")[-1]
    assert (e["deck"], e["stage"]) == ("deck0001", "scored")
    assert e["components"] == 3


def test_one_enormous_detail_does_not_become_the_log(tmp_path, closed_run):
    """A `problems` list pasted in full turns one record into a screenful and
    the file into something nobody tails."""
    pl.open_run(tmp_path, argv=["pptxgym", "run"], limits={})
    _deck(tmp_path).mark("hardened", "rejected",
                         problems=[f"attack {i}: " + "x" * 500 for i in range(50)])
    e = _kinds(_events(tmp_path), "stage_finished")[-1]
    assert len(e["problems"]) == pl.EVENT_LIST_MAX
    assert all(len(p) <= pl.EVENT_STR_MAX for p in e["problems"])


# --------------------------------------------------------------------------- #
# reading it back
# --------------------------------------------------------------------------- #


def _timeline(work, records):
    """A run log written by hand, for the renderer to read."""
    log = pl.open_run(work, argv=["pptxgym", "run", "--workers", "6"],
                      limits={"agent_workers": 6, "cpu_workers": 4},
                      decks=2)
    for rec in records:
        log.emit(rec.pop("event"), **rec)
    pl.close_run(outcome="ok")
    return log


def _ticking(monkeypatch, step=60.0, start=1785880000.0):
    """A clock that advances a minute per record, so a timeline written in a
    millisecond still has a span to divide by."""
    now = [start]

    def _t():
        now[0] += step
        return now[0]

    monkeypatch.setattr(pl.time, "time", _t)


def test_history_says_where_the_wall_clock_went(tmp_path, closed_run, capsys,
                                                monkeypatch):
    _ticking(monkeypatch, step=300.0)
    _timeline(tmp_path, [
        dict(event="stage_started", deck="deck0001", stage="solvable"),
        dict(event="stage_finished", deck="deck0001", stage="solvable",
             status="ok", ms=450_000),
        dict(event="stage_started", deck="deck0002", stage="solvable"),
        dict(event="stage_finished", deck="deck0002", stage="solvable",
             status="ok", ms=150_000),
        dict(event="stage_finished", deck="deck0002", stage="degraded",
             status="ok", ms=60_000),
    ])
    cli.cmd_history(_args(tmp_path))
    out = capsys.readouterr().out
    assert "where the wall clock went" in out
    assert "solvable" in out and "10m00s" in out          # 450s + 150s of work
    assert "agent pool" in out and "6 slot(s)" in out     # against the real limit
    assert "as resolved, not as typed" in out


def test_a_utilisation_over_a_hundred_percent_is_reported_as_impossible(
        tmp_path, closed_run, capsys, monkeypatch):
    """The last reader of a number like this reported 328% and believed it.
    More work than the slots could have held is a broken sum, and saying so
    beats printing it."""
    _ticking(monkeypatch, step=1.0)              # a one-second span …
    _timeline(tmp_path, [
        dict(event="stage_started", deck="deck0001", stage="solvable"),
        dict(event="stage_finished", deck="deck0001", stage="solvable",
             status="ok", ms=3_600_000),          # … holding an hour of work
    ])
    cli.cmd_history(_args(tmp_path))
    out = capsys.readouterr().out
    assert "too short a span" in out or "impossible" in out


def test_a_span_too_short_to_divide_by_is_not_divided_by(tmp_path, closed_run,
                                                         capsys):
    _timeline(tmp_path, [
        dict(event="stage_started", deck="deck0001", stage="solvable"),
        dict(event="stage_finished", deck="deck0001", stage="solvable",
             status="ok", ms=1_000),
    ])
    cli.cmd_history(_args(tmp_path))
    out = capsys.readouterr().out
    assert "too short a span" in out and "% used" not in out


def test_history_shows_which_decks_went_round_the_loop(tmp_path, closed_run,
                                                       capsys):
    _timeline(tmp_path, [
        dict(event="stage_finished", deck="deck0004", stage="solvable",
             status="rejected", verdict="undetermined", ms=1000),
        dict(event="sent_back", deck="deck0004", stage="repair",
             source="solvability.json:undetermined", to=["recipe"], attempt=1),
        dict(event="stage_retried", deck="deck0007", stage="reconciled",
             attempt=1, kind="api_error", backoff_s=30, why="429 rate limited"),
        dict(event="stage_finished", deck="deck0008", stage="reconciled",
             status="needs_human", reason="out of repairs"),
    ])
    cli.cmd_history(_args(tmp_path))
    out = capsys.readouterr().out
    assert "the loop (4 event(s))" in out
    assert "deck0004" in out and "→ repair 1 → recipe" in out
    assert "retried after api_error" in out and "waited 30s" in out
    assert "needs_human" in out and "out of repairs" in out


def test_history_answers_who_was_in_which_stage_at_a_given_moment(
        tmp_path, closed_run, capsys):
    """The question every timing in the post-mortem needed: at 07:12, which
    deck was where.  Nothing but this file can answer it."""
    log = pl.open_run(tmp_path, argv=["pptxgym", "run"], limits={})
    log.emit("stage_started", deck="deck0001", stage="solvable")
    log.emit("stage_started", deck="deck0002", stage="degraded")
    log.emit("stage_finished", deck="deck0002", stage="degraded", status="ok")
    pl.close_run()

    when = pl.read_events(log.path)[-1]["t"][11:]
    cli.cmd_history(_args(tmp_path, at=when))
    out = capsys.readouterr().out
    assert "deck0001" in out and "solvable" in out
    assert "deck0002" not in out.split("at " + when, 1)[1]   # it had finished


def test_history_counts_the_skips_because_a_resumed_run_is_mostly_skips(
        tmp_path, closed_run, capsys):
    _timeline(tmp_path, [
        dict(event="stage_skipped", deck="deck0001", stage="proposed",
             why=cli.SKIP_DONE, note="(already done)"),
        dict(event="stage_skipped", deck="deck0002", stage="proposed",
             why=cli.SKIP_DONE, note="(already done)"),
        dict(event="stage_skipped", deck="deck0002", stage="solvable",
             why=cli.SKIP_UPSTREAM, note="skipped — not reconciled"),
    ])
    cli.cmd_history(_args(tmp_path))
    out = capsys.readouterr().out
    assert "nothing to do (3 skip(s))" in out
    assert cli.SKIP_DONE in out and cli.SKIP_UPSTREAM in out


def test_history_says_plainly_when_a_run_was_killed(tmp_path, closed_run,
                                                    capsys):
    """No footer means the process did not get to write one, and a reader must
    not take the last event's clock for the run's end."""
    log = pl.open_run(tmp_path, argv=["pptxgym", "run"], limits={})
    log.emit("stage_started", deck="deck0001", stage="solvable")
    cli.cmd_history(_args(tmp_path))
    assert "killed, or it is still going" in capsys.readouterr().out


def test_history_with_nothing_recorded_says_so(tmp_path, closed_run, capsys):
    cli.cmd_history(_args(tmp_path))
    assert "no run log under" in capsys.readouterr().out


def test_status_points_at_the_run_that_produced_it(tmp_path, closed_run,
                                                   capsys):
    pl.open_run(tmp_path, argv=["pptxgym", "run"], limits={})
    pl.close_run()
    cli._status_tail(_args(tmp_path), [])
    assert "`pptxgym history`" in capsys.readouterr().out


def test_the_command_line_offers_it(tmp_path):
    args = cli.build_parser().parse_args(["history", "--at", "07:12:00"])
    assert args.func is cli.cmd_history and args.at == "07:12:00"
    assert cli.build_parser().parse_args(["history", "--list"]).list


# --------------------------------------------------------------------------- #
# an output that predates this attempt is not this attempt's answer
# --------------------------------------------------------------------------- #


CORPSE = json.dumps({"tasks": [], "no_task_reason": "written by a process "
                                                    "that was then killed"})


def test_an_artefact_from_a_killed_process_is_not_judged_as_this_run_s_answer(
        tmp_path, fake_claude, monkeypatch, closed_run):
    """The gap the retry path already closes and the kill path did not.  The
    file on disk is well formed — it would pass `check_proposal` on its own
    merits — and it was written by a process that no longer exists.  The re-run
    fails to write anything, and the checker must not be handed the corpse.

    The mechanism changed and got stronger. `_left_over` used to *detect* the
    corpse after the fact; the agent stage now archives it and takes it off
    disk before the agent starts, so there is nothing left to detect and the
    checker refuses for the plainer reason that no answer exists. Same
    outcome, same bytes kept, one fewer thing that has to be noticed.

    The change was made for a different bug: an agent finding a *good*
    previous answer already in place would reasonably leave it alone, and be
    reported as having written nothing. That cost two decks in one run.
    `_left_over` still guards the stages `archive_attempt` does not cover.
    """
    work = tmp_path / "work"
    deck = _deck(work)
    deck.proposal.write_text(CORPSE)             # killed after writing this
    monkeypatch.delenv("FAKE_OUT", raising=False)   # this attempt writes nothing

    line = cli._propose_one(deck, _args(work))

    assert deck.status_of("proposed") != "ok"
    assert "REJECTED" in line
    assert "proposal.json" in deck.state()["proposed"]["error"]
    # and the bytes are not destroyed, they are moved where a re-run cannot
    # mistake them for an answer
    assert (deck.root / "attempts" / "proposed-01" / "proposal.json"
            ).read_text() == CORPSE
    assert not deck.proposal.exists()


def test_the_same_artefact_rewritten_by_this_attempt_is_this_attempt_s_answer(
        tmp_path, fake_claude, monkeypatch, closed_run):
    """The other half, and the reason this is a stamp and not a clock: file
    timestamps are quantised to ~10ms, so a file written milliseconds after the
    attempt began can carry an mtime just before it."""
    work = tmp_path / "work"
    deck = _deck(work)
    deck.proposal.write_text(CORPSE)
    monkeypatch.setenv("FAKE_OUT", str(deck.proposal))
    monkeypatch.setenv("FAKE_BODY", json.dumps(
        {"tasks": [], "no_task_reason": "this run's own answer"}))

    line = cli._propose_one(deck, _args(work))

    assert deck.status_of("proposed") == "ok"
    assert "this run's own answer" in line or "tasks" in line
    assert "this run's own" in deck.proposal.read_text()


def test_a_rewrite_of_identical_bytes_still_counts_as_written(tmp_path,
                                                              fake_claude,
                                                              monkeypatch,
                                                              closed_run):
    """An agent that reaches the same conclusion twice writes the same bytes,
    and `size` alone cannot tell that from a file nobody touched — which is
    why the stamp carries `mtime_ns` as well."""
    work = tmp_path / "work"
    deck = _deck(work)
    deck.proposal.write_text(CORPSE)
    time.sleep(0.02)                             # past the ~10ms quantisation
    monkeypatch.setenv("FAKE_OUT", str(deck.proposal))
    monkeypatch.setenv("FAKE_BODY", CORPSE)

    assert deck.status_of("proposed") is None
    cli._propose_one(deck, _args(work))
    assert deck.status_of("proposed") == "ok"


def test_the_guard_reuses_the_stamp_the_retry_path_already_defined():
    """Two implementations of "did this attempt write it" would drift, and the
    interesting half of the answer — that a clock cannot be used — is already
    written down in `agent._stamp`."""
    assert cli._stamps.__module__ == "pptxgym.cli"
    p = Path(__file__)
    assert cli._stamps([p])[p] == agent._stamp(p)
    assert cli._left_over([p], {p: agent._stamp(p)}) == [p]
    assert cli._left_over([p], {p: (0, 0)}) == []
    assert cli._left_over([p], {}) == []          # nothing was there before


# --------------------------------------------------------------------------- #
# the repairer, which is the stage that rewrites artefacts for a living
# --------------------------------------------------------------------------- #


def test_the_repairer_declares_what_it_is_expected_to_rewrite(tmp_path):
    """It was the one agent stage naming no `outputs` at all, so the half-
    written-output guarantee did not cover the stage whose whole job is to
    rewrite upstream artefacts."""
    deck = _deck(tmp_path)
    assert cli._repair_outputs(deck, [{"stage": "recipe"}]) == \
        [deck.root / "recipe.json"]
    assert cli._repair_outputs(deck, [{"stage": "proposed"},
                                      {"stage": "recipe"}]) == \
        [deck.root / "proposal.json", deck.root / "recipe.json"]


def test_a_repair_ordered_at_materialise_may_fix_the_asset_itself(tmp_path):
    """The declaration lives in the proposal and the file lives beside it.
    Watching only the first would call a correct repair a no-op and park a deck
    that had just been fixed."""
    deck = _deck(tmp_path)
    (deck.root / "assets").mkdir()
    (deck.root / "assets" / "figures.csv").write_text("a,b\n")
    watched = cli._repair_watch(deck, [{"stage": "materialise"}])
    assert deck.root / "proposal.json" in watched
    assert deck.root / "assets" / "figures.csv" in watched


def test_a_repair_that_changed_nothing_does_not_retire_the_complaint(
        tmp_path, monkeypatch, closed_run):
    """It ran, it finished, and not one byte of what it was told to change
    moved.  Calling that a repair re-runs every stage below on exactly the
    files that were just rejected, at agent prices, for the same answer."""
    deck = _deck(tmp_path)
    deck.mark("reconciled", "ok")
    (deck.root / "task.json").write_text(json.dumps({"verdict": "ready"}))
    (deck.root / "plan.json").write_text(json.dumps({"rejected": ["floor"]}))

    async def _idle(spec):
        return {"status": "exited", "returncode": 0, "log": str(spec.log)}

    monkeypatch.setattr(cli.agentmod, "run_agent", _idle)
    monkeypatch.setattr(cli.pl, "tool_tree_state", lambda: None)
    line = cli._repair_one(deck, _args(tmp_path))

    assert "CHANGED NOTHING" in line
    assert deck.state()["repair"]["status"] == "failed"
    assert (deck.root / "plan.json").exists()          # the order still stands
    assert cli._rework_of(deck)[0]
    # and it is still recorded like the agent stage it is
    assert deck.state()["repair"]["attempt"] == 1


def test_a_repair_cut_off_at_its_turn_ceiling_is_not_a_repair(tmp_path,
                                                              monkeypatch,
                                                              closed_run):
    """Repair runs at max_turns=60 and this batch's repairs used 57, 60, 62 and
    57 turns, so a repairer cut off mid-thought is the common case.  It used to
    be recorded as `ok`."""
    deck = _deck(tmp_path)
    deck.mark("reconciled", "ok")
    (deck.root / "task.json").write_text(json.dumps({"verdict": "ready"}))
    (deck.root / "plan.json").write_text(json.dumps({"rejected": ["floor"]}))

    async def _truncated(spec):
        for p in spec.outputs:                   # it even wrote something first
            p.write_text('{"slides": {"1": []}}')
        return {"status": "truncated", "kind": "max_turns",
                "why": "the run stopped on max_turns", "log": str(spec.log)}

    monkeypatch.setattr(cli.agentmod, "run_agent", _truncated)
    monkeypatch.setattr(cli.pl, "tool_tree_state", lambda: None)
    line = cli._repair_one(deck, _args(tmp_path))

    assert "TRUNCATED" in line
    assert deck.state()["repair"]["status"] == "failed"
    assert (deck.root / "plan.json").exists()          # nothing was retired
    assert deck.state()["repair"]["model_asked"] is None   # still recorded


def test_an_outage_does_not_permanently_spend_a_repair(tmp_path):
    """deck0008's `repair-01.jsonl` is a twenty-seven-second aborted stream: a
    repair that never happened, holding a third of that deck's budget for good.
    The deck was parked after what were really two attempts."""
    deck = _deck(tmp_path)
    (deck.root / "repair-01.jsonl").write_text(json.dumps(
        {"type": "result", "terminal_reason": "aborted_streaming",
         "subtype": "error_during_execution", "is_error": True,
         "result": None}, separators=(",", ":")) + "\n")
    assert pl.repairs_done(deck) == 0

    (deck.root / "repair-02.jsonl").write_text(json.dumps(
        {"type": "result", "terminal_reason": "end_turn",
         "subtype": "success", "result": "repaired the recipe"},
        separators=(",", ":")) + "\n")
    assert pl.repairs_done(deck) == 1


def test_the_next_repair_does_not_overwrite_the_evidence_of_the_outage(
        tmp_path):
    """The count and the file number part company the moment an outage stops
    counting, and naming the next attempt after the count would destroy the log
    that proves it was an outage."""
    deck = _deck(tmp_path)
    (deck.root / "repair-01.jsonl").write_text(json.dumps(
        {"type": "result", "terminal_reason": "aborted_streaming",
         "is_error": True, "result": None}, separators=(",", ":")) + "\n")
    assert pl.repairs_done(deck) == 0
    assert pl.next_repair_log(deck).name == "repair-02.jsonl"


# --------------------------------------------------------------------------- #
# a parked deck is a decision
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# the tail
#
# The wall clock of an N-wide run is its slowest deck.  Eight of ten decks
# finished in 23 minutes; two ran 67 minutes longer and produced nothing.
# --------------------------------------------------------------------------- #


def _worked(deck, seconds):
    """Pretend this deck has done `seconds` of work, through the same counter
    the stages feed."""
    pl._WORKED[str(deck.root)] = seconds


@pytest.mark.parametrize("text,factor,minutes", [
    ("3x", 3.0, None), ("1.5x", 1.5, None),
    ("90", None, 90.0), ("90m", None, 90.0),
    ("off", None, None), ("none", None, None),
])
def test_the_deadline_can_be_a_multiple_a_number_or_nothing(text, factor,
                                                            minutes):
    d = cli.Deadline.parse(text)
    assert (d.factor, d.minutes) == (factor, minutes)


def test_the_default_is_visible_in_the_header_because_nobody_typed_it(
        tmp_path, closed_run):
    """A limit that binds the run and appears in no argv is exactly what the
    header is for."""
    args = cli.build_parser().parse_args(["run"])
    assert args.deck_deadline == "3x"
    lim = cli.resolved_limits(args)
    assert "3× the median" in lim["deck_deadline"]
    assert f"{cli.DEADLINE_FLOOR_MIN:g}m" in lim["deck_deadline"]

    log = pl.open_run(tmp_path, argv=["pptxgym", "run"], limits=lim)
    assert "deck_deadline" in pl.read_events(log.path)[0]["limits"]


def test_a_median_of_one_deck_is_not_a_median(tmp_path):
    """Bootstrap: until there are enough finished decks to take a median of,
    the budget is the floor and not a coincidence."""
    d = cli.Deadline()
    assert d.budget_s() == cli.DEADLINE_FLOOR_MIN * 60
    for _ in range(cli.DEADLINE_MIN_SAMPLES - 1):
        d.samples.append(60 * 60.0)
        assert d.budget_s() == cli.DEADLINE_FLOOR_MIN * 60
    d.samples.append(60 * 60.0)
    assert d.budget_s() == 3 * 60 * 60.0          # now it is three medians


def test_a_resumed_run_of_cache_hits_cannot_park_the_one_deck_with_work_to_do(
        tmp_path):
    """The floor's whole reason for existing.  Three decks that were already
    finished take 0.4s each; three times a median of 0.4s is a deadline of
    about a second, and the next deck — the only one with anything to do —
    would be parked before it started."""
    d = cli.Deadline()
    d.samples = [0.4, 0.4, 0.4]
    assert d.budget_s() == cli.DEADLINE_FLOOR_MIN * 60

    deck = _deck(tmp_path)
    _worked(deck, 20 * 60)                         # twenty minutes of real work
    assert d.over(deck) is None


def test_the_deadline_counts_working_time_and_not_the_wait_for_a_slot(
        tmp_path, closed_run):
    """deck0001 waited twenty-nine minutes for a pool slot.  A rule built on
    elapsed time would park decks for being scheduled late, and would get
    worse as concurrency rises."""
    deck = _deck(tmp_path)
    d = cli.Deadline(minutes=1)

    deck.begin("solvable")                         # the slot is taken here
    time.sleep(0.02)
    deck.mark("solvable", "ok")
    worked = deck.worked()
    assert 0.01 < worked < 5                       # the work, not the wall
    assert d.over(deck) is None

    _worked(deck, 61)
    assert "past the" in d.over(deck)


def test_every_execution_counts_not_every_stage(tmp_path, closed_run):
    """A deck that ran `reconciled` four times spent four times, and
    `state.json` only ever keeps the last one."""
    deck = _deck(tmp_path)
    for _ in range(3):
        deck.begin("reconciled")
        time.sleep(0.01)
        deck.mark("reconciled", "rejected")
    assert deck.worked() >= 0.03
    assert len(deck.state()) == 2                  # inspected + one reconciled


def test_a_deck_past_its_deadline_parks_between_stages_with_its_work_intact(
        tmp_path, closed_run, monkeypatch):
    """Parked the way an exhausted repair budget parks: state intact, a stated
    reason, visible in `status`, resumable.  And parked *between* stages —
    interrupting one would leave exactly the half-written artefact the rest of
    this file is about."""
    pl.open_run(tmp_path, argv=["pptxgym", "run"], limits={})
    deck = _deck(tmp_path)
    deck.mark("proposed", "ok")
    deck.proposal.write_text(json.dumps({"tasks": [{"name": "keep me"}]}))
    _worked(deck, 90 * 60)

    ran = []
    for stage, fname in cli.STAGE_FN.items():
        monkeypatch.setattr(cli, fname, lambda ns, s=stage: ran.append(s))

    async def main():
        await cli._run_stages(deck, _args(tmp_path), cli.Pools(agent=1, cpu=1),
                              cli.Deadline(minutes=60))

    asyncio.run(main())

    assert ran == []                               # not one more stage was run
    assert deck.status_of("recipe") == "needs_human"
    assert "past the" in deck.state()["recipe"]["reason"]
    assert deck.state()["recipe"]["worked_s"] == 90 * 60
    assert json.loads(deck.proposal.read_text())["tasks"]   # nothing deleted
    assert cli._parked(deck)
    # and the run's own record says which deck stopped and why
    parked = [e for e in _kinds(_events(tmp_path), "stage_finished")
              if e["status"] == "needs_human"]
    assert parked and "deadline" in parked[0]["reason"]


def test_a_deck_inside_its_budget_is_left_alone(tmp_path, closed_run,
                                                monkeypatch):
    deck = _deck(tmp_path)
    _worked(deck, 10 * 60)
    ran = []
    for stage, fname in cli.STAGE_FN.items():
        monkeypatch.setattr(cli, fname, lambda ns, s=stage: ran.append(s))

    async def main():
        await cli._run_stages(deck, _args(tmp_path, until="proposed"),
                              cli.Pools(agent=1, cpu=1),
                              cli.Deadline(minutes=60))

    asyncio.run(main())
    assert "proposed" in ran


def test_off_means_off(tmp_path):
    deck = _deck(tmp_path)
    _worked(deck, 10 * 60 * 60)
    assert cli.Deadline.parse("off").budget_s() is None
    assert cli.Deadline.parse("off").over(deck) is None


def test_a_parked_deck_still_counts_towards_the_median(tmp_path, closed_run):
    """A sample set drawn only from the decks that went well would raise the
    deadline every time the batch went badly."""
    deck = _deck(tmp_path)
    _worked(deck, 42.0)
    d = cli.Deadline()

    async def main():
        await cli._run_one(deck, _args(tmp_path, until="ingested"),
                           cli.Pools(agent=1, cpu=1), d)

    asyncio.run(main())
    assert d.samples == [42.0]


def test_a_parked_deck_is_not_worked_on_for_another_eight_minutes(
        tmp_path, closed_run, monkeypatch):
    """`_repair_one` parks a deck by marking `reconciled` needs_human; the loop
    then re-ran `reconciled`, which overwrote that mark with a fresh verdict —
    so the guard at the bottom never saw a parked deck.  Three reconcile
    sessions ran on decks already given up on, at $6.21 and nineteen minutes,
    and the run's last eight minutes were nothing else."""
    pl.open_run(tmp_path, argv=["pptxgym", "run"], limits={})
    deck = _deck(tmp_path)
    for s in pl.STAGES[:pl.STAGES.index("solvable")]:
        deck.mark(s, "ok")
    (deck.root / "task.json").write_text(json.dumps({"verdict": "ready"}))
    (deck.root / "solvability.json").write_text(json.dumps(
        {"verdict": "undetermined",
         "rework": [{"stage": "recipe", "what": "make it decidable"}]}))
    deck.mark("solvable", "rejected", verdict="undetermined")

    ran = []
    for stage, fname in cli.STAGE_FN.items():
        monkeypatch.setattr(cli, fname, lambda ns, s=stage: ran.append(s))

    def _park(ns):
        ran.append("repair")
        deck.mark("reconciled", "needs_human", attempts=3)

    monkeypatch.setattr(cli, "cmd_repair", _park)

    async def main():
        await cli._run_stages(deck, _args(tmp_path), cli.Pools(agent=1, cpu=1))

    asyncio.run(main())
    assert ran.count("repair") == 1
    assert "reconciled" not in ran          # nothing was asked of it afterwards
    assert cli._parked(deck)
