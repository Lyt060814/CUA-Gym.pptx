"""What the observer says, against runs whose answer is known in advance.

There is no real hundred-deck run to check this against, and there will not be
one until the thing being checked is trusted enough to be used on it.  So
every case here is fabricated: a `work/` tree and a process table built to
represent a state somebody wrote down first — five decks executing and three
queued, a lock held by a pid that is gone, a display pool at capacity — and
the assertion is that the observer reports exactly that state and no other.

A sampler that reports plausible numbers nobody checked is worse than no
sampler, because the numbers get quoted.

    python3 -m pytest tests/test_observe.py -q
"""

import fcntl
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import observe as ob                                # noqa: E402


# --------------------------------------------------------------------------- #
# fixtures: a work tree, and a process table
# --------------------------------------------------------------------------- #


class FakeProbe(ob.Probe):
    """The machine, as a literal.  Every number the sampler reads comes here."""

    def __init__(self, procs, flocks=None, mem=None, now=None, holds=()):
        self.procs = list(procs)
        self.flocks = dict(flocks or {})
        self.mem = dict(mem or {"MemTotal": 8 << 30, "MemAvailable": 7 << 30,
                                "SwapTotal": 0, "SwapFree": 0})
        self._now = now
        self.holds = set(holds)

    def now(self):
        return self._now if self._now is not None else time.time()

    def processes(self):
        return list(self.procs)

    def flock_holders(self):
        return dict(self.flocks)

    def meminfo(self):
        return dict(self.mem)

    def holds_path(self, pid, root):
        return pid in self.holds


def P(pid, ppid=1, name="claude", cmd=None, rss=300 << 20, start=1000):
    return ob.Proc(pid=pid, ppid=ppid, name=name, cmdline=cmd or name,
                   rss=rss, start_ticks=start)


RUN_ARGV = ("/usr/bin/python3 -m pptxgym run --work work "
            "--agent-workers 8 --cpu-workers 6")


def aged(p: Path, seconds: float) -> Path:
    """Backdate a file, so an age this test chose is the age the observer reads."""
    old = time.time() - seconds
    os.utime(p, (old, old))
    return p


def make_deck(work: Path, deck: str, done: list[str], lock=None,
              statuses=None, log_age_s=None, lock_age_s=None, logs=None):
    """One deck directory in a state we choose.

    `done` is the stages that finished; `lock` is `(stage, pid)` if the deck
    is claiming to be working.  `log_age_s` backdates the agent log so the
    stall detector can be aimed at it, and `lock_age_s` backdates the lock.

    The lock defaults to being *at least* as old as the log, because that is
    the only way round it can be: a stage takes its lock and then opens its
    log.  A fixture that backdated the log alone described a deck that cannot
    exist — and it was the fixture the stall detector was checked against,
    which is part of why the detector shipped believing a 24-minute-old log
    belonged to a 13-minute-old stage.
    """
    d = work / deck
    d.mkdir(parents=True, exist_ok=True)
    st = {}
    for s in done:
        st[s] = {"status": (statuses or {}).get(s, "ok"),
                 "at": "2026-08-05T00:00:00"}
    for s, status in (statuses or {}).items():
        st.setdefault(s, {"status": status, "at": "2026-08-05T00:00:00"})
    (d / "state.json").write_text(json.dumps(st))
    for name, age in (logs or ()):
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"type":"system"}\n')
        aged(p, age)
    if lock:
        stage, pid = lock
        f = d / ".lock"
        f.write_text(json.dumps(
            {"pid": pid, "stage": stage, "at": "2026-08-05T00:00:00"}))
        if stage in ob.AGENT_STAGES and logs is None:
            log = d / f"{stage}.jsonl"
            log.write_text('{"type":"system"}\n')
            if log_age_s:
                aged(log, log_age_s)
        if lock_age_s is None and log_age_s:
            lock_age_s = log_age_s + 1
        if lock_age_s:
            aged(f, lock_age_s)
    return d


def sampler(work, probe, **kw):
    return ob.Sampler(work=Path(work), probe=probe, **kw)


# --------------------------------------------------------------------------- #
# the case the whole thing exists for: five running, three queued
# --------------------------------------------------------------------------- #


def test_five_executing_and_three_queued_is_reported_as_exactly_that(tmp_path):
    """Eight decks, five holding a slot and three waiting for one.

    This is the state a log cannot distinguish from "eight decks in flight",
    and it is the reason the file exists.
    """
    work = tmp_path / "work"
    # four agent stages running, one cpu stage running
    for i, stage in enumerate(["proposed", "recipe", "reconciled", "solvable"]):
        make_deck(work, f"deck000{i + 1}", ob.STAGES[:ob.STAGES.index(stage)],
                  lock=(stage, 500))
    make_deck(work, "deck0005", ob.STAGES[:ob.STAGES.index("degraded")],
              lock=("degraded", 500))
    # three with work left and no lock: queued
    for i in (6, 7, 8):
        make_deck(work, f"deck000{i}", ["ingested", "inspected"])

    procs = [P(500, ppid=1, name="python3", cmd=RUN_ARGV, rss=200 << 20)]
    procs += [P(600 + i, ppid=500, name="claude", cmd="claude --agent x -p ...",
                rss=(300 + i * 10) << 20) for i in range(4)]
    procs += [P(700, ppid=500, name="soffice.bin", cmd="soffice.bin --headless",
                rss=250 << 20)]
    s = sampler(work, FakeProbe(procs)).sample()

    assert s["pools"]["agent"]["running"] == 4
    assert s["pools"]["agent"]["procs"] == 4       # the independent view agrees
    assert s["pools"]["cpu"]["running"] == 1
    assert s["counts"]["running"] == 5
    assert s["counts"]["queued"] == 3
    # the three are queued for the pool their *next* stage will draw on
    assert sorted(s["pools"]["agent"]["queue"]) == ["deck0006", "deck0007",
                                                    "deck0008"]
    assert s["pools"]["cpu"]["queue"] == []
    assert s["pools"]["agent"]["limit"] == 8       # read off the run's own argv
    assert s["pools"]["cpu"]["limit"] == 6
    assert s["limits"]["source"] == "run-argv"
    assert s["warn"] == []


def test_queue_depth_is_split_by_the_pool_the_deck_is_waiting_for(tmp_path):
    """Two decks waiting for an agent slot and two for a cpu slot is not the
    same run as four waiting for one pool, and raising the wrong limit is what
    happens when they are added together."""
    work = tmp_path / "work"
    # next stage is `proposed` (agent) for two of them...
    for i in (1, 2):
        make_deck(work, f"deck000{i}", ["ingested", "inspected"])
    # ...and `degraded` (cpu) for the other two
    for i in (3, 4):
        make_deck(work, f"deck000{i}",
                  ob.STAGES[:ob.STAGES.index("degraded")])
    s = sampler(work, FakeProbe([P(500, name="python3", cmd=RUN_ARGV)])).sample()
    assert s["pools"]["agent"]["queued"] == 2
    assert s["pools"]["cpu"]["queued"] == 2


def test_a_deck_at_the_last_stage_is_finished_and_not_queued(tmp_path):
    work = tmp_path / "work"
    make_deck(work, "deck0001", ob.STAGES)
    make_deck(work, "deck0002", ob.STAGES[:-1])
    s = sampler(work, FakeProbe([P(500, name="python3", cmd=RUN_ARGV)])).sample()
    rows = {d["id"]: d for d in s["decks"]}
    assert rows["deck0001"]["finished"] and not rows["deck0001"]["queued"]
    assert rows["deck0002"]["next"] == "packaged"
    assert s["counts"]["finished"] == 1


def test_until_shortens_the_horizon(tmp_path):
    """A run told `--until solvable` has finished a deck that reached it; the
    observer would otherwise report every deck as three stages short."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ob.STAGES[:ob.STAGES.index("solvable") + 1])
    probe = FakeProbe([P(500, name="python3", cmd=RUN_ARGV)])
    assert not sampler(work, probe).sample()["decks"][0]["finished"]
    assert sampler(work, probe, until="solvable").sample()["decks"][0]["finished"]


def test_materialised_partial_counts_as_promoted(tmp_path):
    """`partial` is where that stage is designed to end.  Reading it as unfinished
    puts a deck in the queue that the scheduler has already moved past."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ob.STAGES[:ob.STAGES.index("materialised")],
              statuses={"materialised": "partial"})
    s = sampler(work, FakeProbe([P(500, name="python3", cmd=RUN_ARGV)])).sample()
    assert s["decks"][0]["done_through"] == "materialised"
    assert s["decks"][0]["next"] == "reconciled"


# --------------------------------------------------------------------------- #
# a deck that has been given up on is not waiting for a slot
# --------------------------------------------------------------------------- #


def _spent_deck(work: Path, deck: str, repairs: int, status="rejected", **kw):
    """deck0001 as the ten-deck run left it: `materialised` ok, `reconciled`
    rejected, and three `repair-*.jsonl` beside it.  `run`'s repair loop is
    `for _ in range(MAX_REPAIRS)`, so at three the loop has fallen out of the
    bottom and nothing will pick this deck up again."""
    return make_deck(work, deck, ob.STAGES[:ob.STAGES.index("reconciled")],
                     statuses={"reconciled": status},
                     logs=[(f"repair-{i + 1:02d}.jsonl", 600.0)
                           for i in range(repairs)], **kw)


def test_a_deck_whose_repair_budget_is_spent_is_not_queued(tmp_path):
    """`agent 1/8 q1` with seven slots free is the exact shape of the evidence
    that says a limit is set too low.  A deck nobody will run again must not
    be able to produce it — this one produced it for 167 samples."""
    work = tmp_path / "work"
    _spent_deck(work, "deck0001", repairs=ob.MAX_REPAIRS)
    s = sampler(work, FakeProbe([P(500, name="python3", cmd=RUN_ARGV)])).sample()
    row = s["decks"][0]
    assert row["queued"] is False
    assert row["parked"]["stage"] == "reconciled"
    assert row["parked"]["status"] == "rejected"
    assert "repair budget spent (3 of 3)" in row["parked"]["why"]
    assert s["pools"]["agent"]["queued"] == 0
    assert s["counts"]["parked"] == 1


def test_a_rejected_deck_with_repairs_left_is_still_queued(tmp_path):
    """The other side of the same rule: `rejected` is a verdict, not a stop.
    Two repairs in, the loop will run a third, so the deck really is waiting
    for an agent slot and really does belong in the queue."""
    work = tmp_path / "work"
    _spent_deck(work, "deck0001", repairs=ob.MAX_REPAIRS - 1)
    s = sampler(work, FakeProbe([P(500, name="python3", cmd=RUN_ARGV)])).sample()
    assert s["decks"][0]["parked"] is None
    assert s["decks"][0]["queued"] is True
    assert s["pools"]["agent"]["queued"] == 1


def test_a_repair_that_is_running_has_not_spent_the_budget(tmp_path):
    """The third `repair-*.jsonl` exists from the moment the third repair
    starts writing it.  A stage that is executing is not a stage that gave
    up."""
    work = tmp_path / "work"
    _spent_deck(work, "deck0001", repairs=ob.MAX_REPAIRS,
                lock=("repair", 601), lock_age_s=60)
    procs = [P(500, name="python3", cmd=RUN_ARGV),
             P(601, ppid=500, name="claude")]
    s = sampler(work, FakeProbe(procs)).sample()
    assert s["decks"][0]["parked"] is None
    assert s["decks"][0]["running"] is True


def test_a_needs_human_deck_is_parked_whatever_the_repair_count(tmp_path):
    work = tmp_path / "work"
    _spent_deck(work, "deck0001", repairs=0, status="needs_human")
    s = sampler(work, FakeProbe([P(500, name="python3", cmd=RUN_ARGV)])).sample()
    assert s["decks"][0]["parked"] == {"stage": "reconciled",
                                       "status": "needs_human",
                                       "why": "status"}
    assert s["decks"][0]["queued"] is False


def test_a_retry_log_does_not_spend_one_of_the_three_chances(tmp_path):
    """`repairs_done` counts the repairer's own logs in the deck root.  An
    attempt the API killed is under `retries/` and cost the deck nothing."""
    work = tmp_path / "work"
    d = _spent_deck(work, "deck0001", repairs=ob.MAX_REPAIRS - 1)
    p = d / "retries" / "repair-03-try-01" / "repair-03.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("{}\n")
    assert ob.repairs_done(d) == ob.MAX_REPAIRS - 1
    s = sampler(work, FakeProbe([P(500, name="python3", cmd=RUN_ARGV)])).sample()
    assert s["decks"][0]["queued"] is True


# --------------------------------------------------------------------------- #
# a deck holding a lock with nothing behind it
# --------------------------------------------------------------------------- #


def test_a_lock_held_by_a_dead_pid_is_a_hung_deck(tmp_path):
    """The state this run is meant to catch: `state.json` and `.lock` both say
    the deck is working, and there is no process."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ob.STAGES[:ob.STAGES.index("proposed")],
              lock=("proposed", 9999))            # 9999 is not in the table
    make_deck(work, "deck0002", ob.STAGES[:ob.STAGES.index("proposed")],
              lock=("proposed", 601))
    procs = [P(500, name="python3", cmd=RUN_ARGV),
             P(601, ppid=500, name="claude")]
    s = sampler(work, FakeProbe(procs)).sample()

    assert [h["id"] for h in s["hung"]] == ["deck0001"]
    assert "dead pid 9999" in s["hung"][0]["why"]
    # and the deck is not counted as running, because it is not
    assert s["pools"]["agent"]["running"] == 1
    assert s["pools"]["agent"]["procs"] == 1
    assert s["decks"][0]["running"] is False


def test_a_live_lock_with_a_silent_log_is_reported_as_stalled(tmp_path):
    """The other shape of hang: the process is alive and has stopped doing
    anything.  `claude -p` streams a record per turn, so a log that has not
    grown in ten minutes is a stage that is not working.

    The stage has held its lock for longer than its log has been silent, which
    is the only arrangement a real stall can have."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ob.STAGES[:ob.STAGES.index("solvable")],
              lock=("solvable", 601), log_age_s=900, lock_age_s=1000)
    make_deck(work, "deck0002", ob.STAGES[:ob.STAGES.index("solvable")],
              lock=("solvable", 601), log_age_s=5, lock_age_s=1000)
    procs = [P(500, name="python3", cmd=RUN_ARGV),
             P(601, ppid=500, name="claude")]
    s = sampler(work, FakeProbe(procs), stall_after=300).sample()
    assert [h["id"] for h in s["hung"]] == ["deck0001"]
    assert "no log output" in s["hung"][0]["why"]
    assert s["blind"] == []


# --------------------------------------------------------------------------- #
# the false alarm of 2026-08-05: a liveness signal older than the stage
# --------------------------------------------------------------------------- #


def _deck0008_at_0712(work: Path, with_retry: bool):
    """The file state behind samples 315-317 of the ten-deck run, rebuilt.

    From `/tmp/arun-timeline.jsonl` and `work/deck0008/`: `repair` had held
    its lock since 06:59:07 (808.9s at 07:12:36); `repair-03.jsonl` was not in
    the deck root, because `_keep_attempt` had moved it to
    `retries/repair-03-try-01/` for a 30-second API backoff; the newest name
    left behind was `repair-02.jsonl`, last written at 06:47:41 — 1495.0s
    earlier.  The observer reported `no log output for 1495.0s` on a stage
    that was 808.9s old and about to finish normally.
    """
    logs = [("repair-01.jsonl", 37_000.0), ("repair-02.jsonl", 1495.0)]
    if with_retry:
        logs.append(("retries/repair-03-try-01/repair-03.jsonl", 17.0))
    return make_deck(work, "deck0008",
                     ob.STAGES[:ob.STAGES.index("reconciled") + 1],
                     lock=("repair", 601), lock_age_s=808.9, logs=logs)


def test_a_log_older_than_the_stage_that_wrote_it_is_not_a_stall(tmp_path):
    """A liveness signal older than the thing it is measuring is not evidence
    of a stall; it is evidence the observer is reading the wrong file.

    Both numbers were in the sample that raised the false alarm — 1495.0s of
    silence inside a stage 808.9s old — and it printed them side by side.
    """
    work = tmp_path / "work"
    _deck0008_at_0712(work, with_retry=False)
    procs = [P(500, name="python3", cmd=RUN_ARGV),
             P(601, ppid=500, name="claude")]
    s = sampler(work, FakeProbe(procs), stall_after=300).sample()

    assert s["hung"] == []                       # this is the whole point
    assert [b["id"] for b in s["blind"]] == ["deck0008"]
    assert s["blind"][0]["log"] == "repair-02.jsonl"
    assert s["decks"][0]["log_covers_stage"] is False
    assert any("older than the stage's own lock" in w for w in s["warn"])
    assert any("not judging whether it stalled" in w for w in s["warn"])


def test_the_log_a_retry_moved_aside_is_still_the_stages_own(tmp_path):
    """And the fix that makes the guard unnecessary in this case: the live log
    was under `retries/`, seventeen seconds old, all along."""
    work = tmp_path / "work"
    _deck0008_at_0712(work, with_retry=True)
    procs = [P(500, name="python3", cmd=RUN_ARGV),
             P(601, ppid=500, name="claude")]
    s = sampler(work, FakeProbe(procs), stall_after=300).sample()

    assert s["hung"] == [] and s["blind"] == []
    row = s["decks"][0]
    assert row["log"] == "retries/repair-03-try-01/repair-03.jsonl"
    assert row["log_age_s"] < 30
    assert row["log_covers_stage"] is True


def test_a_stage_with_no_log_at_all_is_unjudged_and_not_stalled(tmp_path):
    """The other way to have no liveness signal.  It could be a stage that
    never started; it could equally be a stage whose log this file does not
    know the name of, and that is a defect here rather than there.  Either
    way it is not evidence, so nobody is woken for it."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ob.STAGES[:ob.STAGES.index("solvable")],
              lock=("solvable", 601), lock_age_s=900, logs=())
    probe = FakeProbe([P(500, name="python3", cmd=RUN_ARGV),
                       P(601, ppid=500, name="claude")])
    s = ob.Sampler(work=work, probe=probe, stall_after=300)
    first, second = s.sample(), s.sample()
    assert first["hung"] == [] and second["alerts"] == []
    assert [b["id"] for b in first["blind"]] == ["deck0001"]
    assert "no log of its own exists" in first["blind"][0]["why"]


def test_a_stage_the_observer_cannot_see_raises_nothing(tmp_path):
    """The blind case is a fact about the observer, so it goes in `warn` and
    never into `hung` — and nothing is woken up for it."""
    work = tmp_path / "work"
    _deck0008_at_0712(work, with_retry=False)
    probe = FakeProbe([P(500, name="python3", cmd=RUN_ARGV),
                       P(601, ppid=500, name="claude")])
    alerts = ob.Alerts()
    s = ob.Sampler(work=work, probe=probe, alerts=alerts)
    assert s.sample()["alerts"] == []
    assert s.sample()["alerts"] == []            # not on the second look either


def test_lock_view_and_process_view_disagreeing_is_a_warning(tmp_path):
    """Two decks claim an agent slot and one `claude` is alive.  Neither view
    is authoritative, so the sample says so rather than picking one."""
    work = tmp_path / "work"
    for i in (1, 2):
        make_deck(work, f"deck000{i}", ob.STAGES[:ob.STAGES.index("recipe")],
                  lock=("recipe", 500))
    procs = [P(500, name="python3", cmd=RUN_ARGV),
             P(601, ppid=500, name="claude")]
    s = sampler(work, FakeProbe(procs)).sample()
    assert s["pools"]["agent"]["running"] == 2
    assert s["pools"]["agent"]["procs"] == 1
    assert any("locks say 2 running, 1 claude" in w for w in s["warn"])


# --------------------------------------------------------------------------- #
# process attribution
# --------------------------------------------------------------------------- #


def test_other_peoples_claude_processes_are_not_counted(tmp_path):
    """This box runs several `claude` subagents that are not pipeline stages.
    Counting them would not add noise, it would make every agent-pool number
    in the timeline wrong."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ob.STAGES[:ob.STAGES.index("recipe")],
              lock=("recipe", 500))
    procs = [
        P(500, ppid=1, name="python3", cmd=RUN_ARGV),
        P(601, ppid=500, name="claude"),                     # ours
        P(10805, ppid=1, name="claude"),                     # somebody's session
        P(13886, ppid=10805, name="claude"),                 # their subagent
        P(733821, ppid=10805, name="claude"),                # and another
    ]
    s = sampler(work, FakeProbe(procs)).sample()
    assert s["pools"]["agent"]["procs"] == 1
    assert s["procs"]["by_name"]["claude"] == 1
    # and the three we declined to count are named, so a rule that has gone
    # wrong shows up as a number instead of as a silence
    assert s["procs"]["unattributed"]["claude"] == 3
    assert any("not part of the run" in w for w in s["warn"])


def test_descendants_at_any_depth_are_ours(tmp_path):
    work = tmp_path / "work"
    make_deck(work, "deck0001", ["ingested"])
    procs = [P(500, ppid=1, name="python3", cmd=RUN_ARGV),
             P(600, ppid=500, name="soffice", cmd="soffice --headless"),
             P(601, ppid=600, name="oosplash"),
             P(602, ppid=601, name="soffice.bin")]
    s = sampler(work, FakeProbe(procs)).sample()
    assert s["procs"]["owned"] == 4
    assert s["pools"]["cpu"]["procs"] == 3


def test_a_helper_that_reparents_to_init_stays_ours(tmp_path):
    """`wps_roundtrip` starts Xvfb and wpp in their own session, so if the run
    dies they are adopted by init.  Those are exactly the processes a
    post-mortem wants the memory of, so ownership is sticky once seen."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ["ingested"])
    xvfb = P(900, ppid=500, name="Xvfb", cmd="Xvfb :99", start=4242)
    procs = [P(500, ppid=1, name="python3", cmd=RUN_ARGV), xvfb]
    s = sampler(work, FakeProbe(procs))
    first = s.sample()
    assert first["procs"]["reasons"]["descendant"] == 1

    orphan = ob.Proc(pid=900, ppid=1, name="Xvfb", cmdline="Xvfb :99",
                     rss=xvfb.rss, start_ticks=4242)
    s.probe.procs = [P(500, ppid=1, name="python3", cmd=RUN_ARGV), orphan]
    second = s.sample()
    assert second["procs"]["reasons"].get("sticky") == 1
    assert second["pools"]["cpu"]["procs"] == 1


def test_sticky_ownership_does_not_survive_a_reused_pid(tmp_path):
    """A pid that comes back as a different process is a different process.
    Inheriting the old one's ownership would be an invisible error."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ["ingested"])
    s = sampler(work, FakeProbe([P(500, ppid=1, name="python3", cmd=RUN_ARGV),
                                 P(900, ppid=500, name="Xvfb", start=4242)]))
    s.sample()
    s.probe.procs = [P(500, ppid=1, name="python3", cmd=RUN_ARGV),
                     P(900, ppid=1, name="Xvfb", start=99999)]   # new process
    second = s.sample()
    assert "sticky" not in second["procs"]["reasons"]
    assert second["procs"]["unattributed"].get("Xvfb") == 1


def test_an_orphan_holding_a_file_under_work_is_adopted(tmp_path):
    """A helper already orphaned before the first sample has no ancestry left
    to follow.  An open file under the work tree is the remaining evidence."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ["ingested"])
    procs = [P(500, ppid=1, name="python3", cmd=RUN_ARGV),
             P(901, ppid=1, name="soffice.bin"),
             P(902, ppid=1, name="soffice.bin")]
    s = sampler(work, FakeProbe(procs, holds={901})).sample()
    assert s["procs"]["reasons"].get("workdir") == 1
    assert s["procs"]["unattributed"]["soffice.bin"] == 1


def test_the_observer_never_attributes_itself(tmp_path):
    """`python -m pptxgym.observe` has "pptxgym" in its argv and so does the
    shell that launched it.  Either one taken as the run would make the
    observer the thing it measures."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ["ingested"])
    me = os.getpid()
    procs = [P(me, ppid=1, name="python3",
               cmd="python3 -m pptxgym.observe watch --work work"),
             P(500, ppid=1, name="python3", cmd=RUN_ARGV),
             P(601, ppid=500, name="claude")]
    s = sampler(work, FakeProbe(procs)).sample()
    assert [r["pid"] for r in s["roots"]] == [500]
    assert me not in [r["pid"] for r in s["roots"]]


def test_an_explicit_pid_overrides_the_search(tmp_path):
    work = tmp_path / "work"
    make_deck(work, "deck0001", ["ingested"])
    procs = [P(500, ppid=1, name="python3", cmd=RUN_ARGV),
             P(501, ppid=1, name="python3", cmd=RUN_ARGV),  # a second run
             P(601, ppid=500, name="claude"),
             P(602, ppid=501, name="claude")]
    s = sampler(work, FakeProbe(procs), roots=[501]).sample()
    assert [r["pid"] for r in s["roots"]] == [501]
    assert s["pools"]["agent"]["procs"] == 1


def test_a_root_that_is_a_child_of_another_root_is_not_a_second_run(tmp_path):
    """`pptxgym run` shelling out to `pptxgym status` would otherwise be
    counted as two runs and its limits read twice."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ["ingested"])
    procs = [P(500, ppid=1, name="python3", cmd=RUN_ARGV),
             P(510, ppid=500, name="python3",
               cmd="python3 -m pptxgym status --work work")]
    s = sampler(work, FakeProbe(procs)).sample()
    assert [r["pid"] for r in s["roots"]] == [500]


@pytest.mark.parametrize("cmd,want", [
    (RUN_ARGV, True),
    ("python3 -m pptxgym.observe watch --work work", False),
    ("claude --agent ppt-proposer -p ...", False),
    ("/bin/bash -c cd /x && python -m pptxgym propose --workers 4", True),
    ("vim pptxgym/cli.py", False),
    ("grep -r pptxgym .", False),
])
def test_what_counts_as_a_pipeline_process(cmd, want):
    assert ob.looks_like_pipeline(cmd) is want


# --------------------------------------------------------------------------- #
# limits
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("argv,want", [
    (RUN_ARGV, {"agent": 8, "cpu": 6}),
    ("python -m pptxgym run --workers 3", {"agent": 3}),
    ("python -m pptxgym run --agent-workers=5 --cpu-workers=2",
     {"agent": 5, "cpu": 2}),
    ("python -m pptxgym run", {}),
    ("python -m pptxgym run --until solvable", {"until": "solvable"}),
    # the spelling the ten-deck run actually used, with everything round it
    ("/home/x/python -m pptxgym.cli run --workers 8 --cpu-workers 6 "
     "--wps-workers 2 --api-retries 3 --until packaged",
     {"agent": 8, "cpu": 6, "until": "packaged"}),
    # argparse takes any unambiguous prefix, so these started real runs too
    ("python -m pptxgym run --worker 8 --cpu-worker 6", {"agent": 8, "cpu": 6}),
    ("python -m pptxgym run --agent-work=4", {"agent": 4}),
    # --work is a flag in its own right and a prefix of --workers: the exact
    # match wins, and a work *directory* is not a worker count
    ("python -m pptxgym run --work work --cpu-workers 2", {"cpu": 2}),
    # --wps-workers and --attack-workers are neither pool
    ("python -m pptxgym harden --wps-workers 2 --attack-workers 4", {}),
])
def test_limits_are_read_off_the_runs_own_argv(argv, want):
    assert ob._limits_from_argv(argv) == want


def test_an_explicit_limit_beats_the_argv_and_says_so(tmp_path):
    work = tmp_path / "work"
    make_deck(work, "deck0001", ["ingested"])
    probe = FakeProbe([P(500, name="python3", cmd=RUN_ARGV)])
    s = sampler(work, probe, limits={"agent": 2, "cpu": 2}).sample()
    assert s["limits"]["agent"] == 2 and s["limits"]["source"] == "flag"


def test_with_no_run_visible_the_limit_is_refused_rather_than_guessed(tmp_path):
    """The four samples taken after the ten-deck run exited found no argv,
    filled the agent limit in from a default of 1, and the report divided
    ninety minutes of occupancy by it: `util 328%`, presented as the
    conclusion that the agent pool was the constraint.  There is no default
    any more."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ["ingested"])
    s = sampler(work, FakeProbe([])).sample()
    assert s["limits"]["agent"] is None and s["limits"]["cpu"] is None
    assert s["limits"]["source"] == "unknown"
    assert s["pools"]["agent"]["limit"] is None
    assert any("no pptxgym process visible" in w for w in s["warn"])
    assert any("the agent pool's limit is unknown" in w and "--agent-limit" in w
               for w in s["warn"])


def test_a_run_whose_argv_names_no_limit_is_unknown_not_one(tmp_path):
    """The CLI's default is a number in a module somebody else is rewriting.
    Restating it here is how a limit that has drifted becomes indistinguishable
    from a limit that was measured."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ["ingested"])
    probe = FakeProbe([P(500, name="python3", cmd="python -m pptxgym run")])
    s = sampler(work, probe).sample()
    assert s["limits"]["agent"] is None
    assert s["limits"]["sources"] == {"agent": "unknown", "cpu": "unknown"}
    assert any("names no agent worker flag" in w for w in s["warn"])


def test_one_known_limit_and_one_unknown_are_reported_separately(tmp_path):
    work = tmp_path / "work"
    make_deck(work, "deck0001", ["ingested"])
    probe = FakeProbe([P(500, name="python3",
                         cmd="python -m pptxgym run --workers 8")])
    s = sampler(work, probe).sample()
    assert s["limits"]["agent"] == 8 and s["limits"]["cpu"] is None
    assert s["limits"]["sources"] == {"agent": "run-argv", "cpu": "unknown"}
    assert s["limits"]["source"] == "unknown"      # the worse of the two


def test_until_is_taken_from_the_runs_argv_too(tmp_path):
    work = tmp_path / "work"
    make_deck(work, "deck0001", ob.STAGES[:ob.STAGES.index("solvable") + 1])
    probe = FakeProbe([P(500, name="python3",
                         cmd="python -m pptxgym run --until solvable")])
    assert sampler(work, probe).sample()["decks"][0]["finished"]


# --------------------------------------------------------------------------- #
# memory
# --------------------------------------------------------------------------- #


def test_memory_is_the_runs_tree_and_its_largest_single_process(tmp_path):
    """7 GB available and a `claude -p` measured at 250-620 MB: the sample
    that shows the approach is the one worth having, not the corpse."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ["ingested"])
    procs = [P(500, ppid=1, name="python3", cmd=RUN_ARGV, rss=200 << 20)]
    procs += [P(600 + i, ppid=500, name="claude", rss=(250 + i * 60) << 20)
              for i in range(6)]
    procs += [P(9000, ppid=1, name="claude", rss=4 << 30)]      # not ours
    s = sampler(work, FakeProbe(procs)).sample()
    ours = (200 + sum(250 + i * 60 for i in range(6))) << 20
    assert s["mem"]["rss_total"] == ours
    assert s["mem"]["largest"]["pid"] == 605                    # 550 MB
    assert s["mem"]["largest"]["rss"] == 550 << 20
    assert s["mem"]["avail"] == 7 << 30
    assert s["mem"]["rss_by_name"]["claude"] == ours - (200 << 20)


def test_meminfo_is_parsed_into_bytes(tmp_path):
    p = tmp_path / "meminfo"
    p.write_text("MemTotal:       16305892 kB\nMemAvailable:    7000000 kB\n"
                 "HugePages_Total:       0\n")
    got = ob.read_meminfo(str(p))
    assert got["MemTotal"] == 16305892 * 1024
    assert got["HugePages_Total"] == 0


# --------------------------------------------------------------------------- #
# the display pool
# --------------------------------------------------------------------------- #


def _flock(path: Path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_display_occupancy_counts_only_locks_that_are_really_held(tmp_path):
    """A lock *file* proves nothing: `DisplayPool` never unlinks one and the
    pid written inside is whoever held it last.  Only /proc/locks says held."""
    d = tmp_path / "displays"
    d.mkdir()
    conf = {"base": 99, "span": 4, "dir": str(d)}
    for n in range(99, 103):
        (d / f"display{n}.lock").write_text("500\n")     # every file exists
    held = {}
    for n, pid in ((99, 500), (100, 601)):
        st = (d / f"display{n}.lock").stat()
        held[(st.st_dev, st.st_ino)] = pid

    got = ob.display_occupancy(conf, held, {500: "root", 601: "descendant"})
    assert got["held"] == 2 and got["free"] == 2 and not got["saturated"]
    assert got["by_run"] == 2
    assert [h["display"] for h in got["holders"]] == [99, 100]


def test_a_display_held_by_somebody_elses_process_is_held_but_not_ours(tmp_path):
    d = tmp_path / "displays"
    d.mkdir()
    conf = {"base": 99, "span": 2, "dir": str(d)}
    (d / "display99.lock").write_text("")
    st = (d / "display99.lock").stat()
    got = ob.display_occupancy(conf, {(st.st_dev, st.st_ino): 4242}, {})
    assert got["held"] == 1 and got["by_run"] == 0
    assert got["holders"][0]["owned"] is False


def test_a_saturated_display_pool_is_visible_and_warned_about(tmp_path):
    """DISPLAY_SPAN is 64 and saturation is currently silent.  Ten decks will
    not reach it; a hundred might, and it must not be found by inference."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ["ingested"])
    d = tmp_path / "displays"
    d.mkdir()
    conf = {"base": 99, "span": 3, "dir": str(d)}
    held = {}
    for n in range(99, 102):
        f = d / f"display{n}.lock"
        f.write_text("")
        st = f.stat()
        held[(st.st_dev, st.st_ino)] = 500

    got = ob.display_occupancy(conf, held, {500: "root"})
    assert got["saturated"] and got["free"] == 0

    probe = FakeProbe([P(500, name="python3", cmd=RUN_ARGV)], flocks=held)
    s = ob.Sampler(work=work, probe=probe)
    monkey = ob.display_pool_conf
    try:
        ob.display_pool_conf = lambda: conf
        sample = s.sample()
    finally:
        ob.display_pool_conf = monkey
    assert sample["displays"]["saturated"]
    assert any("display pool saturated" in w for w in sample["warn"])


def test_proc_locks_parsing_finds_a_lock_this_test_is_holding(tmp_path):
    """The one thing a fixture cannot fake: that the format `/proc/locks`
    really uses is the format being parsed.  Hex device, decimal inode."""
    if not Path("/proc/locks").exists():
        pytest.skip("no /proc/locks")
    f = tmp_path / "display99.lock"
    fd = _flock(f)
    try:
        st = f.stat()
        got = ob.read_flocks()
        assert got.get((st.st_dev, st.st_ino)) == os.getpid()
    finally:
        os.close(fd)
    assert (st.st_dev, st.st_ino) not in ob.read_flocks()


def test_the_observer_takes_no_lock_of_its_own(tmp_path):
    """An observer that probes by trying to `flock` would, for the instant it
    held one, be a display the pipeline could not have — and at a hundred
    decks against a span of 64 that is a NoFreeDisplay it caused."""
    d = tmp_path / "displays"
    d.mkdir()
    f = d / "display99.lock"
    f.write_text("")
    ob.display_occupancy({"base": 99, "span": 1, "dir": str(d)}, {}, {})
    st = f.stat()
    assert (st.st_dev, st.st_ino) not in ob.read_flocks()   # nothing was taken


# --------------------------------------------------------------------------- #
# reading the real /proc
# --------------------------------------------------------------------------- #


def test_read_procs_finds_this_process_with_its_real_parent():
    procs = {p.pid: p for p in ob.read_procs()}
    me = procs.get(os.getpid())
    assert me is not None
    assert me.ppid == os.getppid()
    assert me.rss > 1 << 20              # a python interpreter is not tiny
    assert me.start_ticks > 0


def test_a_comm_with_spaces_and_parens_does_not_shift_the_fields(tmp_path):
    """`(sd-pam)` and `Web Content` are real process names, and splitting the
    stat line on whitespace puts the ppid in the wrong column for both."""
    proc = tmp_path / "proc"
    (proc / "4242").mkdir(parents=True)
    fields = ["S", "77", "1", "1", "0", "-1", "4194304", "0", "0", "0", "0",
              "10", "20", "0", "0", "20", "0", "1", "0", "555666", "12345678",
              "2048"]
    (proc / "4242" / "stat").write_text("4242 (Web Content (x)) " + " ".join(fields))
    (proc / "4242" / "cmdline").write_bytes(b"/usr/bin/x\0--flag\0")
    got = ob.read_procs(str(proc))
    assert len(got) == 1
    assert got[0].name == "Web Content (x)"
    assert got[0].ppid == 77
    assert got[0].start_ticks == 555666
    assert got[0].rss == 2048 * ob._PAGE
    assert got[0].cmdline == "/usr/bin/x --flag"


# --------------------------------------------------------------------------- #
# watch: the timeline itself
# --------------------------------------------------------------------------- #


def test_watch_writes_a_header_and_one_record_per_sample(tmp_path, capsys):
    work = tmp_path / "work"
    make_deck(work, "deck0001", ob.STAGES[:ob.STAGES.index("recipe")],
              lock=("recipe", 500))
    probe = FakeProbe([P(500, name="python3", cmd=RUN_ARGV),
                       P(601, ppid=500, name="claude")])
    out = tmp_path / "t.jsonl"
    ob.watch(work, out, interval=0.0, probe=probe, max_samples=3,
             sleep=lambda s: None)
    header, samples = ob.load_timeline(out)
    assert header["kind"] == "header" and header["interval_s"] == 0.0
    assert header["blind_spots"]
    assert len(samples) == 3
    assert [s["seq"] for s in samples] == [1, 2, 3]
    assert "agent 1/8" in capsys.readouterr().out


def test_watch_appends_rather_than_truncating(tmp_path):
    """The reason to have this at all is the run that dies; a second watch
    must not erase the first one's evidence."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ["ingested"])
    out = tmp_path / "t.jsonl"
    probe = FakeProbe([P(500, name="python3", cmd=RUN_ARGV)])
    for _ in range(2):
        ob.watch(work, out, interval=0.0, probe=probe, max_samples=1,
                 quiet=True, sleep=lambda s: None)
    _, samples = ob.load_timeline(out)
    assert len(samples) == 2


def test_a_half_written_last_line_does_not_lose_the_timeline(tmp_path):
    """A run killed mid-flush leaves a truncated final line, and that is
    exactly the timeline somebody needs to read."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ["ingested"])
    out = tmp_path / "t.jsonl"
    ob.watch(work, out, interval=0.0,
             probe=FakeProbe([P(500, name="python3", cmd=RUN_ARGV)]),
             max_samples=2, quiet=True, sleep=lambda s: None)
    with open(out, "a") as fh:
        fh.write('{"kind": "sample", "seq": 3, "dec')
    _, samples = ob.load_timeline(out)
    assert len(samples) == 2


def test_a_sample_that_raises_is_recorded_and_the_watch_continues(tmp_path):
    work = tmp_path / "work"
    make_deck(work, "deck0001", ["ingested"])
    out = tmp_path / "t.jsonl"

    class Broken(FakeProbe):
        n = 0

        def processes(self):
            Broken.n += 1
            if Broken.n == 1:
                raise OSError("proc went away")
            return []

    ob.watch(work, out, interval=0.0, probe=Broken([]), max_samples=2,
             quiet=True, sleep=lambda s: None)
    kinds = [json.loads(l)["kind"] for l in open(out) if l.strip()]
    assert kinds == ["header", "error", "sample"]


def test_transitions_name_what_moved_and_how_long_it_waited():
    prev = {"decks": [{"id": "deck0001", "stage": "recipe", "running": False,
                       "done_through": "proposed", "since_s": 42.0,
                       "parked": None}]}
    cur = {"decks": [{"id": "deck0001", "stage": "recipe", "running": True,
                      "done_through": "proposed", "since_s": 1.0,
                      "parked": None}]}
    got = ob.transitions(prev, cur)
    assert got == ["    deck0001 started recipe (waited 42.0s)"]
    assert ob.transitions(None, cur) == []


def test_a_parking_is_announced_once():
    prev = {"decks": [{"id": "d1", "stage": "solvable", "running": True,
                       "done_through": "reconciled", "since_s": 1,
                       "parked": None}]}
    cur = {"decks": [{"id": "d1", "stage": "solvable", "running": False,
                      "done_through": "reconciled", "since_s": 1,
                      "parked": {"stage": "solvable", "status": "needs_human"}}]}
    lines = ob.transitions(prev, cur)
    assert any("PARKED at solvable (needs_human)" in x for x in lines)
    assert ob.transitions(cur, cur) == []


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def _sample(t, seq, decks, agent_limit=4, cpu_limit=6, mem=None, displays=None,
            limit_source="run-argv", roots=()):
    """A timeline row, hand-built, so the analyser is checked against arithmetic
    somebody did on paper."""
    def row(d):
        stage, running, queued = d["stage"], d.get("running"), d.get("queued")
        return {"id": d["id"], "stage": stage, "running": bool(running),
                "queued": bool(queued), "next": d.get("next", stage),
                "done_through": d.get("done", ""), "finished": d.get("finished",
                                                                     False),
                "parked": d.get("parked"),
                "pool": ob._pool_of(stage), "since_s": d.get("since_s", 0),
                "lock": None}
    rows = [row(d) for d in decks]
    pools = {}
    for pool, lim in (("agent", agent_limit), ("cpu", cpu_limit)):
        r = [x["id"] for x in rows if x["running"] and x["pool"] == pool]
        q = [x["id"] for x in rows if x["queued"] and x["pool"] == pool]
        pools[pool] = {"limit": lim, "running": len(r), "queued": len(q),
                       "decks": r, "queue": q, "procs": len(r)}
    return {"v": 1, "kind": "sample", "seq": seq, "t": t,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)),
            "elapsed_s": t, "work": "work", "roots": list(roots),
            "limits": {"agent": agent_limit, "cpu": cpu_limit,
                       "source": limit_source,
                       "sources": {"agent": limit_source,
                                   "cpu": limit_source}},
            "pools": pools, "decks": rows,
            "counts": {"decks": len(rows),
                       "running": sum(1 for x in rows if x["running"]),
                       "queued": sum(1 for x in rows if x["queued"]),
                       "finished": sum(1 for x in rows if x["finished"]),
                       "parked": 0},
            "procs": {"owned": 0, "by_name": {}, "unattributed": {},
                      "reasons": {}},
            "mem": mem or {"rss_total": 1 << 30, "rss_by_name": {},
                           "largest": {"pid": 1, "name": "claude",
                                       "rss": 600 << 20},
                           "avail": 7 << 30, "total": 8 << 30,
                           "swap_used": 0},
            "displays": displays or {"base": 99, "span": 64, "held": 0,
                                     "by_run": 0, "free": 64,
                                     "saturated": False, "holders": [],
                                     "method": "proc-locks"},
            "hung": [], "warn": []}


def test_peak_concurrency_is_measured_against_the_limit_that_was_set():
    """The single number that says whether the flags were the constraint."""
    t0 = 1_800_000_000
    samples = [
        _sample(t0 + 0, 1, [{"id": "d1", "stage": "recipe", "running": True},
                            {"id": "d2", "stage": "recipe", "queued": True},
                            {"id": "d3", "stage": "recipe", "queued": True}]),
        _sample(t0 + 10, 2, [{"id": "d1", "stage": "recipe", "running": True},
                             {"id": "d2", "stage": "recipe", "running": True},
                             {"id": "d3", "stage": "recipe", "queued": True}]),
        _sample(t0 + 20, 3, [{"id": "d1", "stage": "recipe", "running": True},
                             {"id": "d2", "stage": "recipe", "running": True},
                             {"id": "d3", "stage": "recipe", "running": True}]),
    ]
    a = ob.analyse({"interval_s": 10}, samples, work="/nonexistent")
    ag = a["pools"]["agent"]
    assert ag["limit"] == 4
    assert ag["peak"] == 3            # never filled: the limit was not the wall
    assert ag["peak_queue"] == 2
    assert ag["queue_deck_seconds"] == 30.0     # 2*10 + 1*10 + 0*10
    assert ag["frac_at_limit"] == 0.0
    assert "never filled" in ob.render(a)


def test_a_pool_that_was_full_the_whole_time_is_named_as_the_constraint():
    t0 = 1_800_000_000
    decks = [{"id": "d1", "stage": "recipe", "running": True},
             {"id": "d2", "stage": "recipe", "running": True},
             {"id": "d3", "stage": "recipe", "queued": True}]
    samples = [_sample(t0 + 10 * i, i + 1, decks, agent_limit=2)
               for i in range(4)]
    a = ob.analyse({"interval_s": 10}, samples, work="/nonexistent")
    assert a["pools"]["agent"]["peak"] == 2
    assert a["pools"]["agent"]["frac_at_limit"] == 1.0
    assert a["pools"]["agent"]["utilisation"] == 1.0
    assert "this limit was the constraint" in ob.render(a)


def test_waiting_for_a_slot_is_separated_from_working():
    """Elapsed time at N-wide is not work; the question is how much of it was
    queueing, and raising a limit only buys back the second kind."""
    t0 = 1_800_000_000
    samples = [
        _sample(t0 + 0, 1, [{"id": "d1", "stage": "recipe", "running": True},
                            {"id": "d2", "stage": "recipe", "queued": True}]),
        _sample(t0 + 10, 2, [{"id": "d1", "stage": "recipe", "running": True},
                             {"id": "d2", "stage": "recipe", "queued": True}]),
        _sample(t0 + 20, 3, [{"id": "d1", "stage": "recipe", "running": True,
                              "done": "recipe"},
                             {"id": "d2", "stage": "recipe", "running": True}]),
    ]
    a = ob.analyse({"interval_s": 10}, samples, work="/nonexistent")
    by = {d["id"]: d for d in a["decks"]}
    assert by["d1"]["working_s"] == 30.0 and by["d1"]["waiting_s"] == 0.0
    assert by["d2"]["waiting_s"] == 20.0 and by["d2"]["working_s"] == 10.0
    assert by["d2"]["wait_by_stage"] == {"recipe": 20.0}
    stages = {e["stage"]: e for e in a["stages"]}
    assert stages["recipe"]["total_s"] == 40.0
    assert stages["recipe"]["wait_s"] == 20.0


def test_the_critical_path_deck_is_named_and_so_is_what_made_it_slow():
    """Wall clock at N-wide is the slowest deck, not the average."""
    t0 = 1_800_000_000
    samples = []
    for i in range(6):
        decks = [{"id": "fast", "stage": "recipe",
                  "running": i < 1, "finished": i >= 1},
                 {"id": "slow", "stage": "solvable", "running": True}]
        samples.append(_sample(t0 + 10 * i, i + 1, decks))
    a = ob.analyse({"interval_s": 10}, samples, work="/nonexistent")
    assert a["critical_path"]["id"] == "slow"
    assert a["critical_path"]["elapsed_s"] == 60.0
    assert a["critical_path"]["by_stage"] == {"solvable": 60.0}
    out = ob.render(a)
    assert "critical path is slow" in out
    assert "longest stage was solvable" in out


def test_a_deck_that_finished_stops_accruing_elapsed_time():
    t0 = 1_800_000_000
    samples = [
        _sample(t0 + 0, 1, [{"id": "d1", "stage": "recipe", "running": True}]),
        _sample(t0 + 10, 2, [{"id": "d1", "stage": None, "finished": True,
                              "done": "packaged"}]),
        _sample(t0 + 20, 3, [{"id": "d1", "stage": None, "finished": True,
                              "done": "packaged"}]),
    ]
    a = ob.analyse({"interval_s": 10}, samples, work="/nonexistent")
    assert a["decks"][0]["elapsed_s"] == 10.0
    assert a["decks"][0]["working_s"] == 10.0


def test_the_last_sample_stands_for_the_median_gap_not_for_zero():
    t0 = 1_800_000_000
    samples = [_sample(t0 + 10 * i, i + 1,
                       [{"id": "d1", "stage": "recipe", "running": True}])
               for i in range(3)]
    a = ob.analyse({"interval_s": 10}, samples, work="/nonexistent")
    assert a["run"]["wall_s"] == 30.0
    assert a["decks"][0]["working_s"] == 30.0


def test_hangs_seen_during_the_run_are_carried_into_the_report():
    t0 = 1_800_000_000
    samples = [_sample(t0 + 10 * i, i + 1,
                       [{"id": "d1", "stage": "proposed"}]) for i in range(3)]
    for s in samples[1:]:
        s["hung"] = [{"id": "d1", "stage": "proposed",
                      "why": "lock held by dead pid 9999", "since_s": 900}]
        s["warn"] = ["agent locks say 1 running, 0 claude process(es) attributed"]
    a = ob.analyse({"interval_s": 10}, samples, work="/nonexistent")
    assert a["hung"][0]["id"] == "d1" and a["hung"][0]["samples"] == 2
    assert list(a["warnings"].values()) == [2]
    assert "dead pid 9999" in ob.render(a)


def test_an_empty_timeline_says_so_rather_than_dividing_by_zero():
    a = ob.analyse({}, [])
    assert "no samples" in ob.render(a)


# --------------------------------------------------------------------------- #
# report: the limit is a claim, and a claim can be missing or wrong
# --------------------------------------------------------------------------- #


def test_the_limit_comes_from_the_run_and_not_from_its_last_breath():
    """The last sample is the one most likely to have been taken after the run
    it was measuring had gone.  Reading the limit from it is how ninety
    minutes of eight-wide occupancy got divided by 1."""
    t0 = 1_800_000_000
    live = [_sample(t0 + 10 * i, i + 1,
                    [{"id": "d1", "stage": "recipe", "running": True}],
                    agent_limit=8, roots=[{"pid": 500, "cmd": "run"}])
            for i in range(5)]
    dead = [_sample(t0 + 10 * (5 + i), 6 + i,
                    [{"id": "d1", "stage": "recipe", "queued": True}],
                    agent_limit=1, cpu_limit=5, limit_source="default")
            for i in range(2)]
    a = ob.analyse({"interval_s": 10}, live + dead, work="/nonexistent")
    ag = a["pools"]["agent"]
    assert ag["limit"] == 8 and ag["limit_source"] == "run-argv"
    assert ag["utilisation"] <= 1.0


def test_a_limit_nobody_recorded_gets_no_utilisation_and_no_verdict():
    t0 = 1_800_000_000
    samples = [_sample(t0 + 10 * i, i + 1,
                       [{"id": "d1", "stage": "recipe", "running": True},
                        {"id": "d2", "stage": "recipe", "running": True}],
                       agent_limit=None, cpu_limit=None,
                       limit_source="unknown") for i in range(3)]
    a = ob.analyse({"interval_s": 10}, samples, work="/nonexistent")
    ag = a["pools"]["agent"]
    assert ag["limit"] is None and ag["limit_source"] == "unknown"
    assert ag["utilisation"] is None
    assert ag["frac_at_limit"] is None
    assert ag["peak"] == 2 and ag["mean"] == 2.0        # measured, and kept
    page = ob.render(a)
    assert "the agent pool's limit is unknown" in page
    assert "utilisation is not computed" in page
    assert "this limit was the constraint" not in page
    assert "never filled" not in page
    agent_row = next(l for l in page.splitlines() if l.startswith("  agent "))
    assert "%" not in agent_row and "?" in agent_row


def test_a_limit_the_occupancy_contradicts_is_dropped_not_divided_by():
    """Eight stages ran at once; a limit of 1 forbids that.  Occupancy is
    measured and the limit is hearsay, so it is the limit that goes."""
    t0 = 1_800_000_000
    decks = [{"id": f"d{i}", "stage": "recipe", "running": True}
             for i in range(8)]
    samples = [_sample(t0 + 10 * i, i + 1, decks, agent_limit=1)
               for i in range(3)]
    a = ob.analyse({"interval_s": 10}, samples, work="/nonexistent")
    assert a["pools"]["agent"]["limit"] is None
    assert a["pools"]["agent"]["limit_source"] == "contradicted"
    assert a["pools"]["agent"]["utilisation"] is None
    assert "not the limit that was in force" in ob.render(a)


def test_samples_that_disagree_about_the_limit_are_not_averaged():
    t0 = 1_800_000_000
    a = ob.analyse({"interval_s": 10}, [
        _sample(t0, 1, [{"id": "d1", "stage": "recipe"}], agent_limit=8),
        _sample(t0 + 10, 2, [{"id": "d1", "stage": "recipe"}], agent_limit=4),
    ], work="/nonexistent")
    assert a["pools"]["agent"]["limit"] is None
    assert a["pools"]["agent"]["limit_source"] == "conflicting"


# --------------------------------------------------------------------------- #
# the real run of 2026-08-05, replayed
# --------------------------------------------------------------------------- #

REAL = Path(__file__).parent / "fixtures" / "arun-timeline-slice.jsonl"


def _real():
    if not REAL.exists():                                # pragma: no cover
        pytest.skip("real timeline slice not present")
    return ob.load_timeline(REAL)


def test_the_real_runs_limit_is_eight_and_its_utilisation_is_possible():
    """`--workers 8`, and the report said `limit 1 … util 328%` because the
    last four samples were taken after the run had exited.  These are those
    samples, verbatim, alongside the ones that knew."""
    header, samples = _real()
    assert samples[-1]["limits"] == {"agent": 1, "cpu": 5, "source": "default"}
    a = ob.analyse(header, samples, work="/nonexistent")
    ag = a["pools"]["agent"]
    assert ag["limit"] == 8                       # not 1, and not the last word
    assert ag["limit_source"] == "run-argv"
    assert 0.0 < ag["utilisation"] <= 1.0
    page = ob.render(a)
    row = next(l for l in page.splitlines() if l.startswith("  agent "))
    assert "328%" not in row and " 8 " in row
    assert "the agent pool was full" not in page   # the conclusion it drew


def test_the_real_false_alarm_is_visible_in_its_own_sample():
    """The three `hung` records the ten-deck run produced are all deck0008,
    and every one of them carries the number that disproves it."""
    header, samples = _real()
    hung = [(s["seq"], h) for s in samples for h in s.get("hung", [])]
    assert {h["id"] for _, h in hung} == {"deck0008"}
    for _, h in hung:
        age = float(h["why"].split("for ")[1].rstrip("s"))
        assert age > h["since_s"]                 # older than the stage itself

    # and the report does not repeat it: a timeline written before the sampler
    # learned the rule still gets read by a report that knows it
    a = ob.analyse(header, samples, work="/nonexistent")
    assert a["hung"] == []
    assert [b["id"] for b in a["blind"]] == ["deck0008"]
    assert a["blind"][0]["samples"] == 3
    page = ob.render(a)
    assert "HUNG / STALLED" not in page
    assert "reading the wrong log" in page
    assert next(d for d in a["decks"] if d["id"] == "deck0008")["stalled_s"] == 0


def test_the_real_run_going_away_is_something_to_say_out_loud():
    """The last four samples: no pptxgym process, two decks short of the end.
    Ninety minutes of watching told nobody, and a human had to keep asking."""
    _, samples = _real()
    al = ob.Alerts()
    fired = [a for s in samples for a in al.check(s)]
    kinds = [a["kind"] for a in fired]
    assert "run-gone" in kinds
    gone = next(a for a in fired if a["kind"] == "run-gone")
    assert gone["iso"].startswith("2026-08-05T07:48")
    assert kinds.count("run-gone") == 1           # once, not four times


# --------------------------------------------------------------------------- #
# alerts: the part that leaves the file
# --------------------------------------------------------------------------- #


def _mem(avail, total=16 << 30):
    return {"rss_total": 1 << 30, "rss_by_name": {},
            "largest": {"pid": 1, "name": "claude", "rss": 600 << 20},
            "avail": avail, "total": total, "swap_used": 0}


def test_a_healthy_run_raises_nothing_at_all(tmp_path):
    """The default has to be silence.  An observer that cries wolf is ignored,
    and an ignored observer costs attention as well as telling nobody."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ob.STAGES[:ob.STAGES.index("recipe")],
              lock=("recipe", 601), log_age_s=3, lock_age_s=60)
    make_deck(work, "deck0002", ["ingested", "inspected"])
    probe = FakeProbe([P(500, name="python3", cmd=RUN_ARGV),
                       P(601, ppid=500, name="claude")])
    s = ob.Sampler(work=work, probe=probe)
    assert [a for _ in range(5) for a in s.sample()["alerts"]] == []


def test_a_park_is_announced_at_once_because_it_cannot_flap(tmp_path):
    work = tmp_path / "work"
    _spent_deck(work, "deck0001", repairs=ob.MAX_REPAIRS)
    probe = FakeProbe([P(500, name="python3", cmd=RUN_ARGV)])
    s = ob.Sampler(work=work, probe=probe)
    first = s.sample()["alerts"]
    assert [a["kind"] for a in first] == ["park"]
    assert first[0]["id"] == "deck0001" and first[0]["level"] == "error"
    assert "needs a human" in first[0]["msg"]
    # and once only: a deck stuck for an hour is one line, not three hundred
    assert s.sample()["alerts"] == []
    assert s.sample()["alerts"] == []


def test_a_crash_and_an_infra_failure_are_named_as_themselves(tmp_path):
    work = tmp_path / "work"
    _spent_deck(work, "deck0001", repairs=0, status="crashed")
    _spent_deck(work, "deck0002", repairs=0, status="infra")
    probe = FakeProbe([P(500, name="python3", cmd=RUN_ARGV)])
    fired = ob.Sampler(work=work, probe=probe).sample()["alerts"]
    assert sorted(a["kind"] for a in fired) == ["crash", "infra"]


def test_a_stall_must_be_seen_twice_before_it_is_announced(tmp_path):
    """One unlucky read — a `stat` that raced a rename — is not a stall.  Two
    consecutive samples costs twenty seconds of notice at the default
    interval and buys immunity to exactly that."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ob.STAGES[:ob.STAGES.index("solvable")],
              lock=("solvable", 601), log_age_s=900, lock_age_s=1000)
    probe = FakeProbe([P(500, name="python3", cmd=RUN_ARGV),
                       P(601, ppid=500, name="claude")])
    s = ob.Sampler(work=work, probe=probe, stall_after=300)
    assert s.sample()["alerts"] == []
    assert [a["kind"] for a in s.sample()["alerts"]] == ["stall"]
    assert s.sample()["alerts"] == []


def test_a_condition_that_clears_rearms_and_one_that_does_not_stays_quiet():
    al = ob.Alerts(confirm=2)
    hot = {"seq": 1, "iso": "t", "t": 1.0, "decks": [], "roots": [{"pid": 1}],
           "hung": [{"id": "d1", "stage": "solvable", "why": "no log output "
                     "for 900.0s", "since_s": 1000}],
           "mem": _mem(9 << 30), "displays": {"saturated": False}}
    cool = dict(hot, hung=[])
    assert al.check(hot) == []                   # once: not confirmed yet
    assert len(al.check(hot)) == 1               # twice: said
    assert al.check(hot) == []                   # still firing: still quiet
    assert al.check(cool) == []                  # cleared: nothing to say
    assert al.check(hot) == []                   # and it must build up again
    assert len(al.check(hot)) == 1               # ...before it speaks


def test_memory_alerts_below_the_floor_and_not_above_it():
    """15% of total.  The ten-deck run's floor was 4.8G of 16.2G — 30% — with
    eight agent stages and four soffices alive, so this fires well below
    anything a healthy run of that shape has produced, and still leaves room
    to act before the OOM killer picks a victim for us."""
    base = {"seq": 1, "iso": "t", "t": 1.0, "decks": [], "roots": [{"pid": 1}],
            "hung": [], "displays": {"saturated": False}}
    ok = ob.Alerts(confirm=1)
    assert ok.check(dict(base, mem=_mem(int(0.16 * (16 << 30))))) == []
    low = ob.Alerts(confirm=1)
    fired = low.check(dict(base, mem=_mem(int(0.14 * (16 << 30)))))
    assert [a["kind"] for a in fired] == ["memory"]
    assert "below 15%" in fired[0]["msg"]


def test_a_saturated_display_pool_is_worth_saying_out_loud():
    base = {"seq": 1, "iso": "t", "t": 1.0, "decks": [], "roots": [{"pid": 1}],
            "hung": [], "mem": _mem(9 << 30)}
    al = ob.Alerts(confirm=1)
    fired = al.check(dict(base, displays={"saturated": True, "span": 64,
                                          "by_run": 64}))
    assert [a["kind"] for a in fired] == ["displays"]
    assert fired[0]["level"] == "warning"
    assert "NoFreeDisplay" in fired[0]["msg"]


def test_a_run_that_has_gone_is_not_a_run_that_is_idle():
    al = ob.Alerts(confirm=1)
    decks = [{"id": "d1", "finished": False, "parked": None}]
    assert al.check({"seq": 1, "iso": "t", "t": 1.0, "decks": decks,
                     "roots": [{"pid": 500}], "hung": [], "mem": _mem(9 << 30),
                     "displays": {"saturated": False}}) == []
    fired = al.check({"seq": 2, "iso": "t", "t": 2.0, "decks": decks,
                      "roots": [], "hung": [], "mem": _mem(9 << 30),
                      "displays": {"saturated": False}})
    assert [a["kind"] for a in fired] == ["run-gone"]


def test_a_finished_run_with_nothing_left_to_do_is_not_an_alarm():
    al = ob.Alerts(confirm=1)
    decks = [{"id": "d1", "finished": True, "parked": None}]
    assert al.check({"seq": 1, "iso": "t", "t": 1.0, "decks": decks,
                     "roots": [], "hung": [], "mem": _mem(9 << 30),
                     "displays": {"saturated": False}}) == []


def test_an_alert_leaves_by_the_console_the_file_and_the_exit_code(tmp_path,
                                                                   capsys):
    """Three doors, because three different things read this: a human at a
    console, a wrapper polling a path, and a cron line reading `$?`."""
    work = tmp_path / "work"
    _spent_deck(work, "deck0001", repairs=ob.MAX_REPAIRS)
    flag = tmp_path / "alerts.jsonl"
    out = tmp_path / "t.jsonl"
    res = ob.watch(work, out, interval=0.0,
                   probe=FakeProbe([P(500, name="python3", cmd=RUN_ARGV)]),
                   max_samples=3, quiet=True, sleep=lambda s: None,
                   alert_file=flag)
    assert len(res["alerts"]) == 1
    err = capsys.readouterr().err
    assert err.count("!! ALERT") == 1                 # stderr, even under quiet
    assert "[error/park]" in err
    got = [json.loads(l) for l in flag.read_text().splitlines()]
    assert [a["kind"] for a in got] == ["park"]
    assert got[0]["seq"] == 1 and got[0]["id"] == "deck0001"
    assert ob.main(["watch", "--work", str(work), "--once", "--quiet",
                    "--out", str(out)]) == ob.EXIT_ALERTED


def test_quiet_silences_the_samples_and_never_the_alarm(tmp_path, capsys):
    work = tmp_path / "work"
    _spent_deck(work, "deck0001", repairs=ob.MAX_REPAIRS)
    ob.watch(work, tmp_path / "t.jsonl", interval=0.0,
             probe=FakeProbe([P(500, name="python3", cmd=RUN_ARGV)]),
             max_samples=1, quiet=True, sleep=lambda s: None)
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "!! ALERT" in cap.err


def test_no_alerts_records_everything_and_announces_nothing(tmp_path, capsys):
    work = tmp_path / "work"
    _spent_deck(work, "deck0001", repairs=ob.MAX_REPAIRS)
    out = tmp_path / "t.jsonl"
    res = ob.watch(work, out, interval=0.0,
                   probe=FakeProbe([P(500, name="python3", cmd=RUN_ARGV)]),
                   max_samples=1, quiet=True, sleep=lambda s: None,
                   announce=False)
    assert res["alerts"] == []
    assert capsys.readouterr().err == ""
    _, samples = ob.load_timeline(out)
    assert [a["kind"] for a in samples[0]["alerts"]] == ["park"]
    assert ob.main(["watch", "--work", str(work), "--once", "--quiet",
                    "--no-alerts", "--out", str(out)]) == 0


def test_stop_on_alert_stops(tmp_path):
    work = tmp_path / "work"
    _spent_deck(work, "deck0001", repairs=ob.MAX_REPAIRS)
    out = tmp_path / "t.jsonl"
    ob.watch(work, out, interval=0.0,
             probe=FakeProbe([P(500, name="python3", cmd=RUN_ARGV)]),
             max_samples=9, quiet=True, sleep=lambda s: None,
             stop_on_alert=True)
    _, samples = ob.load_timeline(out)
    assert len(samples) == 1


def test_the_report_lists_the_alerts_the_run_raised(tmp_path):
    work = tmp_path / "work"
    _spent_deck(work, "deck0001", repairs=ob.MAX_REPAIRS)
    out = tmp_path / "t.jsonl"
    ob.watch(work, out, interval=0.0,
             probe=FakeProbe([P(500, name="python3", cmd=RUN_ARGV)]),
             max_samples=2, quiet=True, sleep=lambda s: None)
    header, samples = ob.load_timeline(out)
    page = ob.render(ob.analyse(header, samples, work=work))
    assert "ALERTS" in page and "needs a human" in page


# --------------------------------------------------------------------------- #
# report: the half that comes from the work directory
# --------------------------------------------------------------------------- #


def _agent_log(path: Path, session: str, output: int, cache_read: int = 0,
               cost: float = 1.0, turns: int = 10, ms: int = 60000,
               reason: str = "completed"):
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"type": "result", "session_id": session, "num_turns": turns,
           "duration_ms": ms, "duration_api_ms": ms // 2,
           "total_cost_usd": cost, "terminal_reason": reason,
           "is_error": False,
           "usage": {"input_tokens": 60, "output_tokens": output,
                     "cache_read_input_tokens": cache_read,
                     "cache_creation_input_tokens": 1000}}
    # compact, the way `claude --output-format stream-json` writes it
    path.write_text('{"type":"system","model":"m"}\n'
                    + '{"type":"assistant"}\n'
                    + json.dumps(rec, separators=(",", ":")) + "\n")


def test_a_result_record_is_found_whether_or_not_it_is_compact(tmp_path):
    """`claude` writes compact JSON.  A log that has been through anything
    that reformatted it is still the only evidence of how the run ended."""
    for i, sep in enumerate(((",", ":"), (", ", ": "))):
        p = tmp_path / f"log{i}.jsonl"
        p.write_text(json.dumps({"type": "result", "session_id": "x",
                                 "usage": {"output_tokens": 7}},
                                separators=sep) + "\n")
        assert ob.last_result(p)["usage"]["output_tokens"] == 7
    assert ob.last_result(tmp_path / "missing.jsonl") == {}


def test_tokens_are_counted_per_stage_including_superseded_attempts(tmp_path):
    """`archive_attempt` *copies* the live log into attempts/, so a naive walk
    counts the same session twice; and counting only the live log reports a
    deck that took ten solvability runs as having cost one."""
    work = tmp_path / "work"
    d = make_deck(work, "deck0001", ob.STAGES[:ob.STAGES.index("solvable")])
    _agent_log(d / "solvable.jsonl", "s-live", output=27_000, cache_read=1_100_000)
    _agent_log(d / "attempts" / "solvable-01" / "solvable.jsonl",
               "s-first", output=9_000, cache_read=400_000)
    # the copy archive_attempt made of the live log, byte for byte
    _agent_log(d / "attempts" / "solvable-02" / "solvable.jsonl",
               "s-live", output=27_000, cache_read=1_100_000)
    # an attempt killed by a 429, kept under retries/
    _agent_log(d / "retries" / "solvable-try-01" / "solvable.jsonl",
               "s-429", output=120, cache_read=5_000, reason="api_error")
    _agent_log(d / "recipe.jsonl", "r-1", output=12_000)
    _agent_log(d / "repair-01.jsonl", "p-1", output=3_000)

    got = ob.deck_tokens(d)
    assert got["solvable"]["sessions"] == 3            # not 4: the copy is one
    assert got["solvable"]["output"] == 27_000 + 9_000 + 120
    assert got["solvable"]["cache_read"] == 1_100_000 + 400_000 + 5_000
    assert got["solvable"]["errors"] == 1
    assert got["recipe"]["output"] == 12_000
    assert got["repair"]["output"] == 3_000


def test_the_report_totals_tokens_over_every_stage_and_deck(tmp_path):
    """Earlier measurement covered `solvable` only; the last three stages have
    never been measured at all, and the tail is where the surprises are."""
    work = tmp_path / "work"
    for i in (1, 2):
        d = make_deck(work, f"deck000{i}", ob.STAGES)
        for stage, out in (("proposed", 20_000), ("recipe", 12_000),
                           ("reconciled", 30_000), ("solvable", 27_000)):
            _agent_log(d / f"{stage}.jsonl", f"{stage}-{i}", output=out,
                       cache_read=1_000_000, cost=1.5)
    a = ob.from_work_dir(work)
    assert a["tokens_by_stage"]["solvable"]["output"] == 54_000
    assert a["tokens_by_stage"]["solvable"]["decks"] == 2
    assert a["tokens_by_deck"]["deck0001"]["output"] == 89_000
    assert a["tokens_by_deck"]["deck0001"]["cache_read"] == 4_000_000
    assert a["tokens_by_deck"]["deck0001"]["cost_usd"] == 6.0
    assert a["yield"] == {"in": 2, "packaged": 2, "stopped": {}}


def test_yield_says_where_the_decks_that_did_not_make_it_stopped(tmp_path):
    work = tmp_path / "work"
    make_deck(work, "deck0001", ob.STAGES)
    make_deck(work, "deck0002", ob.STAGES[:ob.STAGES.index("solvable")],
              statuses={"solvable": "needs_human"})
    make_deck(work, "deck0003", ob.STAGES[:ob.STAGES.index("solvable")],
              statuses={"solvable": "rejected"})
    make_deck(work, "deck0004", ["ingested"])
    y = ob.from_work_dir(work)["yield"]
    assert y["in"] == 4 and y["packaged"] == 1
    assert y["stopped"]["reconciled -> needs_human"] == 1
    assert y["stopped"]["reconciled -> rejected"] == 1
    assert y["stopped"]["ingested -> not started"] == 1


def test_duration_ms_is_used_when_recorded_and_derived_when_not(tmp_path):
    """`duration_ms` is what a stage should record.  Where it is missing the
    gap between two `at` stamps is all there is, and that gap includes the
    queue — so the report marks which of the two it is looking at."""
    work = tmp_path / "work"
    d = work / "deck0001"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({
        "ingested": {"status": "ok", "at": "2026-08-05T00:00:00"},
        "inspected": {"status": "ok", "at": "2026-08-05T00:01:00",
                      "duration_ms": 42_000},
        "proposed": {"status": "ok", "at": "2026-08-05T00:11:00"},
    }))
    got = {e["stage"]: e for e in ob.from_work_dir(work)["state_stages"]}
    assert got["inspected"]["total_ms"] == 42_000
    assert got["inspected"]["derived"] == 0
    assert got["proposed"]["total_ms"] == 600_000      # 00:01:00 -> 00:11:00
    assert got["proposed"]["derived"] == 1
    assert "upper bound" in ob.render(ob.analyse(
        {}, [_sample(1_800_000_000, 1, [{"id": "deck0001", "stage": None,
                                         "finished": True}])], work=work))


def test_the_report_runs_end_to_end_over_a_fabricated_run(tmp_path, capsys):
    """One pass over the whole thing: watch a fixture, report on the timeline,
    and check the numbers in the printed page are the ones we built in."""
    work = tmp_path / "work"
    make_deck(work, "deck0001", ob.STAGES[:ob.STAGES.index("solvable")],
              lock=("solvable", 601))
    make_deck(work, "deck0002", ob.STAGES[:ob.STAGES.index("recipe")])
    d = work / "deck0001"
    _agent_log(d / "solvable.jsonl", "s1", output=27_000, cache_read=1_100_000)

    procs = [P(500, ppid=1, name="python3", cmd=RUN_ARGV, rss=200 << 20),
             P(601, ppid=500, name="claude", rss=620 << 20),
             P(9000, ppid=1, name="claude", rss=1 << 30)]      # not ours
    out = tmp_path / "t.jsonl"
    ob.watch(work, out, interval=0.0, probe=FakeProbe(procs), max_samples=2,
             quiet=True, sleep=lambda s: None)
    header, samples = ob.load_timeline(out)
    page = ob.render(ob.analyse(header, samples, work=work))

    assert "1 deck(s) in, 0 packaged" not in page          # two decks in
    assert "2 deck(s) in, 0 packaged" in page
    assert "27.0k" in page                                  # solvable output
    assert "deck0001" in page and "deck0002" in page
    assert "1 x claude" in page                             # not counted
    assert "WHAT SAMPLING CANNOT SEE" in page


def test_the_cli_parses_both_subcommands(tmp_path):
    work = tmp_path / "work"
    make_deck(work, "deck0001", ["ingested"])
    out = tmp_path / "t.jsonl"
    assert ob.main(["watch", "--work", str(work), "--once", "--quiet",
                    "--out", str(out)]) == 0
    _, samples = ob.load_timeline(out)
    assert len(samples) == 1
    assert ob.main(["report", str(out), "--work", str(work)]) == 0
    assert ob.main(["report", str(out), "--work", str(work), "--json"]) == 0
