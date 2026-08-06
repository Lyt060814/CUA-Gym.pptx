"""pptxgym — turn a folder of real .pptx decks into computer-use RL tasks.

    pptxgym ingest  corpus/
    pptxgym run     --agent-workers 6
    pptxgym status

Every subcommand is one stage and can be run on its own; `run` chains them and
skips whatever is already done, so a failed batch is resumed rather than
restarted.

Concurrency is measured in two currencies, not one.  Four of the stages spend
API capacity on a `claude -p` subprocess; the other six spend CPU on soffice,
rendering, and — after the last agent — deriving the reward, attacking it and
writing the task out.  Timed over ten decks, the agent stages take ~85% of the
wall clock
(reconcile median 6.0 min, solvable 7.5; degrade 2.3, materialise 0.1), so a
single limit either starves the renderers or oversubscribes the API — we have
seen both.  Each stage now takes a slot from its own pool and gives it back
when it finishes, which also means a deck stuck in a repair loop no longer
holds a slot it is not using.

Both pools are honoured by `run` and by a single-stage command alike, and the
thread pool underneath them is sized from the same two numbers: a stage is a
blocking call that owns its thread for minutes, so the default executor's
`min(32, cpu_count + 4)` was a third, unannounced limit.

The third limit is the API itself, and it is per *account*, so no number of
machines buys around it: ten of the ten-deck pilot's ~100 agent runs died on
infrastructure rather than on the deck.  `--api-retries` (default 3, backoff
30s/60s/90s) covers the transient ones and nothing else — never a timeout,
never a `max_turns` stop, both of which are the agent hitting a real ceiling.
An exhausted budget still parks the deck, every attempt keeps its own log
under `retries/`, and `status` says which decks limped through.

All of that is per-deck evidence.  The run itself is recorded separately, as
one append-only event stream under `work/runs/<run-id>/events.jsonl` —
`pipeline.RunLog` says why, `pptxgym history` reads it back, and the header it
opens with carries the limits as this module *resolved* them rather than as
they were typed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import agent as agentmod
from . import pipeline as pl

DEFAULT_WORK = Path("work")

# repair is an agent stage under another name
_AGENT_WORK = pl.AGENT_STAGES | {"repair"}

# a few threads above what the pools can hand out, so the executor is never
# the thing that runs out first
_THREAD_HEADROOM = 4

# `run` owns the scheduling: it holds the pools, the thread pool, and the only
# event loop in the process.  Every sub-command it invokes therefore has to
# take `_each`'s single-threaded path — anything else opens a second pool with
# a second set of limits on a worker thread, and neither pool then means what
# it says.  The invariant used to live as two hardcoded `1`s in a Namespace
# literal deep inside `_run_stages`: correct, unexplained, and silently fatal
# the day somebody made them configurable.  It is now one named constant, one
# constructor (`_stage_args`), and a refusal in `_each`.
SUB_WORKERS = 1

# set only while `cmd_run` is scheduling; read by `_each` to enforce the above
_UNDER_RUN = False


def _default_cpu_workers() -> int:
    return max(2, (os.cpu_count() or 4) // 4)


class Pools:
    """One limit per kind of resource being spent.

    Holding a single slot for a deck's whole journey conflated two questions:
    how many decks may be in flight, and how much of each resource may be
    consumed at once.  They have different answers — the API tolerates a
    handful of concurrent agents, the machine tolerates more renderers — and
    the deck count itself needs no limit at all, since a deck waiting for a
    slot costs nothing.
    """

    def __init__(self, agent: int, cpu: int):
        self.agent = asyncio.Semaphore(max(1, agent))
        self.cpu = asyncio.Semaphore(max(1, cpu))

    def for_stage(self, stage: str | None) -> asyncio.Semaphore:
        # an unnamed piece of work is assumed to be the expensive kind
        return self.cpu if stage is not None and stage not in _AGENT_WORK \
            else self.agent


def _executor_size(agent: int, cpu: int) -> int:
    """Threads enough that the pools, not the executor, decide concurrency."""
    return max(1, agent) + max(1, cpu) + _THREAD_HEADROOM


@contextlib.contextmanager
def _threads_for(loop, agent: int, cpu: int):
    """Give the loop a thread pool big enough for the limits we just set.

    Every stage runs as a blocking call on a worker thread and holds it for
    minutes, so the executor's size is a concurrency limit of its own.  The
    default one is `min(32, cpu_count + 4)` — 24 on this machine — which was
    invisible while nothing ran more than six at a time.  Past that, raising
    `--workers` did nothing at all and said nothing about it: the semaphore
    let the work through and the thread pool quietly queued it.
    """
    ex = ThreadPoolExecutor(max_workers=_executor_size(agent, cpu),
                            thread_name_prefix="pptxgym")
    loop.set_default_executor(ex)
    try:
        yield ex
    finally:
        ex.shutdown(wait=True)


def _pool_sizes(args) -> tuple[int, int]:
    """The two limits, from the flags — read once so they cannot disagree."""
    return _workers_for(args, "proposed"), _workers_for(args, "degraded")


def _pools_for(args) -> Pools:
    """The one place a command turns its flags into limits."""
    return Pools(*_pool_sizes(args))


# --------------------------------------------------------------------------- #
# the tail
#
# The wall clock of an N-wide run is the slowest deck, not the mean.  Ten decks:
# eight finished in 23 minutes, two ran for 67 more and produced nothing —
# 1h30m against a mean of 35m02s.  One deck sets the cost of the whole batch.
#
# The obvious lever is the repair budget, and the measurement says no: yield
# across budgets runs 0→20%, 1→30%, 2→60%, 3→80%, so every repair round
# converts.  The tail is the problem and not the budget, so what is bounded here
# is the deck rather than the loop.
#
# Three things this is careful about, each of which the code insisted on:
#
#   * **Working time, never elapsed.**  See `pipeline._WORKED`.
#   * **A floor, always.**  Three cache-hit decks finish in 0.4s each; three
#     times a median of 0.4s is a deadline that parks every real deck in a
#     resumed run before it has done anything.  The floor is what stops a rule
#     about the tail from becoming a rule about resumption.
#   * **Enforced between stages, never inside one.**  A deck is parked at the
#     next boundary rather than interrupted, so it never leaves a half-written
#     artefact for a later run to judge.  It also means the deadline is a bound
#     on *spend* and only indirectly on the wall clock: a deck already inside a
#     40-minute agent stage will finish it.
# --------------------------------------------------------------------------- #

DEADLINE_FACTOR = 3.0        # of the running median, as chosen
DEADLINE_MIN_SAMPLES = 3     # a median of one deck is not a median
#: Minutes of working time below which no deck is ever parked, whatever the
#: median says.  A single agent stage may legitimately run to `--timeout`
#: (40 minutes by default), so a deck that has done one long stage and nothing
#: else is slow, not runaway.  It doubles as the bootstrap: until there are
#: `DEADLINE_MIN_SAMPLES` finished decks to take a median of, this is the whole
#: budget.
DEADLINE_FLOOR_MIN = 45.0


class Deadline:
    """How much working time one deck may spend before the run gives up on it.

    `record` is called as each deck finishes, so the median is over decks that
    are done rather than over a snapshot of decks in flight — the second would
    be a median of "how far has everyone got", which falls as the batch grows
    and would tighten the deadline exactly when the machine is busiest.
    """

    def __init__(self, factor: float | None = DEADLINE_FACTOR,
                 minutes: float | None = None,
                 floor_min: float = DEADLINE_FLOOR_MIN,
                 min_samples: int = DEADLINE_MIN_SAMPLES):
        self.factor = factor
        self.minutes = minutes
        self.floor_min = floor_min
        self.min_samples = min_samples
        self.samples: list[float] = []

    @classmethod
    def parse(cls, text) -> "Deadline":
        """`3x` — of the median — or `90` / `90m` — flat — or `off`."""
        s = str(text or "").strip().lower()
        if s in ("off", "none", "0"):
            return cls(factor=None)
        if s.endswith("x"):
            return cls(factor=float(s[:-1]))
        return cls(factor=None, minutes=float(s.rstrip("m")))

    @classmethod
    def from_args(cls, args) -> "Deadline":
        return cls.parse(getattr(args, "deck_deadline", None)
                         or f"{DEADLINE_FACTOR:g}x")

    def describe(self) -> str:
        if self.minutes:
            return f"{self.minutes:g}m of working time per deck"
        if not self.factor:
            return "no deadline"
        return (f"{self.factor:g}× the median finished deck, "
                f"never under {self.floor_min:g}m")

    def record(self, deck) -> None:
        self.samples.append(deck.worked())

    def budget_s(self) -> float | None:
        """Seconds a deck may work for, as things stand, or None for no limit."""
        if self.minutes:
            return self.minutes * 60
        if not self.factor:
            return None
        floor = self.floor_min * 60
        if len(self.samples) < self.min_samples:
            # A median of one is not a median, and saying so is the difference
            # between a bootstrap and a coincidence.
            return floor
        import statistics
        return max(floor, self.factor * statistics.median(self.samples))

    def over(self, deck) -> str | None:
        """Why this deck should stop, or None."""
        budget = self.budget_s()
        worked = deck.worked()
        if budget is None or worked <= budget:
            return None
        return (f"this deck has spent {_hms(worked)} of working time, past the "
                f"{_hms(budget)} deadline ({self.describe()}). It is parked "
                f"with everything it produced intact — `pptxgym run --deck "
                f"{deck.id}` resumes it")


def resolved_limits(args) -> dict:
    """Every limit this run is actually bound by, after the flags are read.

    This is what goes in the run log's header, and the distinction is the
    whole point of it.  `--workers` is an alias for `--agent-workers`, so a
    reader that went back to the argv looking for the long form found nothing,
    fell back to the default of 1, and reported the run at 328% utilisation.
    `--cpu-workers` is worse: its default is not in the argv at all, because it
    is derived from the core count of whichever machine ran it.

    The thread pool is in here too.  It is the third limit and the one nobody
    declares — see `_threads_for` — and a run where it, rather than a
    semaphore, was the binding constraint is otherwise indistinguishable.
    """
    agent, cpu = _pool_sizes(args)
    out = {"agent_workers": agent, "cpu_workers": cpu,
           "threads": _executor_size(agent, cpu),
           # a default nobody typed is exactly the kind of limit this header
           # exists to make visible
           "deck_deadline": Deadline.from_args(args).describe()}
    for flag, key in (("api_retries", "api_retries"), ("timeout", "timeout_min"),
                      ("until", "until"), ("attack_workers", "attack_workers"),
                      ("wps_workers", "wps_workers"), ("no_wps", "no_wps"),
                      ("force", "force")):
        v = getattr(args, flag, None)
        if v is not None:
            out[key] = v
    return out


# --------------------------------------------------------------------------- #
# stages
# --------------------------------------------------------------------------- #


def _decks(args) -> list[pl.Deck]:
    work = Path(args.work)
    if getattr(args, "deck", None):
        return [pl.Deck(work / d) for d in args.deck]
    return pl.decks_in(work)


def _workers_for(args, stage: str | None) -> int:
    """How many of this stage may run at once."""
    if stage is not None and stage not in _AGENT_WORK:
        return max(1, getattr(args, "cpu_workers", None) or _default_cpu_workers())
    return max(1, getattr(args, "workers", 1) or 1)


def _each(args, fn, stage: str | None = None):
    """Run a per-deck stage across decks, honouring the right worker limit.

    Re-running one stage over a batch is the most common thing anyone does,
    and it was the one path that ignored --workers: a plain for-loop, so six
    four-minute agents took twenty-five minutes instead of five.

    A CPU stage invoked on its own is limited by the CPU pool, not the agent
    one: `pptxgym degrade --deck …` has no reason to run four at a time just
    because that is all the API will take.
    """
    decks = _decks(args)
    workers = _workers_for(args, stage)
    if _UNDER_RUN and workers > 1 and len(decks) > 1:
        raise RuntimeError(
            f"{stage!r} asked for {workers} workers underneath `run`, which "
            f"already owns the event loop, the pools and the thread pool. A "
            f"sub-command that opened a pool of its own would start a second "
            f"loop on a worker thread with a second set of limits, and both "
            f"would be wrong — see SUB_WORKERS.")
    if workers == 1 or len(decks) == 1:
        for deck in decks:
            # `run` starts the clock itself, inside the pool slot; starting it
            # again here would time the same stage twice and put two
            # `stage_started` records in the run log for one execution.
            if stage is not None and not _UNDER_RUN:
                deck.begin(stage)
            print("  " + _guarded(fn, deck, args))
        return

    async def main():
        loop = asyncio.get_running_loop()
        # the same pools `run` uses, rather than a semaphore of its own.  Two
        # mechanisms meant two sets of rules: anything added to `Pools` — a
        # third resource, a global ceiling — would have applied to a batch run
        # and silently not to `pptxgym propose --workers 8`, which is the more
        # common way to work.
        pools = _pools_for(args)
        sem = pools.for_stage(stage)

        async def one(deck):
            async with sem:
                if stage is not None:
                    deck.begin(stage)  # inside the slot: work, not the wait
                return await loop.run_in_executor(None, _guarded, fn, deck, args)

        with _threads_for(loop, *_pool_sizes(args)):
            for line in await asyncio.gather(*[one(d) for d in decks]):
                print("  " + line)

    asyncio.run(main())


# Why a stage did nothing.  Three different facts, and a run log that recorded
# only the first would say a resumed run and a blocked run looked the same.
SKIP_DONE = "already_done"          # the cache hit: it was done, and it holds
SKIP_UPSTREAM = "upstream_not_ready"  # the stage above has not passed
SKIP_NOTHING = "nothing_to_do"      # there is no work of this kind on this deck


def _skip(deck, stage: str, why: str, note: str) -> str:
    """Say — and record — that a stage did nothing, and why.

    "Nothing happened because it was already done" is the commonest thing a
    resumed run does and it used to leave no trace anywhere: the console line
    scrolled past and `state.json` was, by definition, unchanged.  A run log
    without it cannot tell a resumed run from a run that never started, which
    is exactly the question anyone asks first.
    """
    st = deck.state().get(stage, {})
    pl.log_event("stage_skipped", deck=deck.id, stage=stage, why=why,
                 note=note, was=st.get("status"), since=st.get("at"))
    return f"{deck.id}  {note}"


def _guarded(fn, deck, args) -> str:
    """Run one deck's stage; a crash takes down that deck and nobody else.

    `StageError` is caught where it is raised, but anything else — a corrupt
    package upsetting python-pptx, a full disk — propagated out of the
    gather and cancelled every other deck in the batch.  Tolerable across ten
    decks; across a hundred it throws away hours of finished work because one
    file was bad.
    """
    try:
        return fn(deck, args)
    except Exception:                                            # noqa: BLE001
        return _record_crash(deck, "stage")


def _record_crash(deck, stage: str) -> str:
    """Keep the traceback beside the deck and say so in one line."""
    tb = traceback.format_exc()
    log = deck.root / f"crash-{stage}.log"
    try:
        deck.root.mkdir(parents=True, exist_ok=True)
        log.write_text(tb)
        deck.mark(stage, "crashed", error=tb.strip().splitlines()[-1][:200],
                  log=str(log))
    except OSError:
        pass
    return f"{deck.id}  CRASHED in {stage} — {tb.strip().splitlines()[-1][:120]}"


def _ingest_line(ev: dict):
    """One line per file, as it happens: a 10k-file ingest is hours long."""
    if ev["event"] == "rejected":
        print(f"  ✗ {ev['name'][:46]:<48}{ev['reason']} — {ev['why']}")
    else:
        mark = "·" if ev["event"] == "duplicate" else " "
        print(f"  {mark} {ev['deck']}  {ev['name'][:44]:<46}"
              f"{ev['slides']} slides"
              + ("   (already registered)" if ev["event"] == "duplicate" else ""))


def cmd_ingest(args):
    """Register a corpus, whatever is in it.

    The loop this replaces called `pl.ingest` per file, so the first truncated
    upload — and the Zenodo corpus has hundreds — ended the batch with a
    traceback that did not even name the file.  `ingest_many` walks
    directories, classifies what it cannot open, deduplicates by content and
    always returns a summary.
    """
    work = Path(args.work)
    r = pl.ingest_many(args.paths, work, progress=_ingest_line)
    print(f"scanned {r['scanned']} file(s) into {work}: "
          f"{len(r['registered'])} registered, {len(r['duplicate'])} duplicate, "
          f"{len(r['rejected'])} rejected")
    if r["rejected"]:
        from collections import Counter
        by = Counter(x["reason"] for x in r["rejected"])
        print("  rejected by reason: "
              + ", ".join(f"{k} {v}" for k, v in by.most_common()))
        print(f"  every one of them is written down in {r['rejects_file']}")


def _inspect_one(deck, args):
    if deck.done("inspected") and not args.force:
        return _skip(deck, "inspected", SKIP_DONE, "(already inspected)")
    try:
        d = pl.inspect(deck, dpi=args.dpi, force=args.force,
                       roundtrip=getattr(args, "roundtrip", False))
        return (f"{deck.id}  digest {d['digest_kb']}KB "
                f"(min {d['digest_min_kb']}KB)  {d['renders']} renders")
    except pl.StageError as e:
        return f"{deck.id}  FAILED — {e}"


def cmd_inspect(args):
    _each(args, _inspect_one, "inspected")


# --------------------------------------------------------------------------- #
# the measurement that cannot live in a stage
# --------------------------------------------------------------------------- #


def _wps_measured(deck) -> bool:
    """Does this deck already carry a WPS number worth keeping?

    A failed attempt is recorded too — it says the GUI would not cooperate on
    that file, which is worth knowing — but it does not count as measured, or
    a single bad run would exclude the deck from every later pass.
    """
    try:
        rep = json.loads((deck.root / "roundtrip-wps.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return rep.get("verdict") not in (None, "unmeasured")


def _wps_sample(decks, n):
    """`n` decks spread evenly across the batch, deterministically.

    Not the first `n`: decks arrive in ingest order, which is the corpus's own
    ordering — by conference, by year, by depositor — so the first `n` are all
    the same kind of deck and would answer a different question than the one
    being asked.
    """
    if not n or n >= len(decks):
        return decks
    step = len(decks) / n
    return [decks[int(i * step)] for i in range(n)]


def cmd_wps(args):
    """Round-trip decks through WPS and write the number where the digest reads it.

    This is a batch pass and not a pipeline stage, and it is the only honest
    place to put it.  WPS has no headless converter on Linux: `wps_roundtrip`
    drives the real GUI on an Xvfb display at 60–90 s a deck.  In `inspect`
    that would dominate ingestion, so `inspect` never runs it — and, until
    now, nothing else did either, which left every deck reading `governs:
    null` and the proposer on its conservative branch forever.

    `--workers` is how many displays are used at once.  It was one, and not
    because the measurement needs to be serial: `wps_roundtrip.DisplayPool`
    hands out :99 upwards and `batch` has been claiming them correctly for the
    attack battery all along.  One deck at a time over a thousand decks is
    about twenty-two hours; four at a time is about five.  The ceiling is
    memory — roughly 660MB per worker at the peak — not correctness.

    `--sample N` is the corpus-scale answer: measure N decks spread across the
    batch, at a cost you choose, and read the result as a property of WPS
    rather than of any one deck.  What it deliberately does NOT do is write a
    synthesised `roundtrip-wps.json` into the decks it did not measure.  The
    digest reads that file as a statement about *that* deck and honours no
    field that could mark it as inferred, so a copied number would put a
    sentence the proposer trusts — "WPS changed nothing on this deck" — into
    the context of a deck nobody opened.  The unmeasured decks keep the
    conservative reading, which is the true one for them.
    """
    from . import wps_roundtrip as wps

    missing = wps.preflight()
    if missing:
        print("cannot measure the WPS round trip: " + "; ".join(missing))
        print("  (the decks keep `governs: null` and the proposer stays "
              "conservative about position work, which is correct)")
        return

    decks = [d for d in _decks(args) if d.source.exists()]
    todo = [d for d in decks if args.force or not _wps_measured(d)]
    todo = _wps_sample(todo, args.sample)
    if not todo:
        print(f"nothing to measure: all {len(decks)} deck(s) already carry a "
              f"WPS number (`--force` to redo one)")
        return
    workers = max(1, getattr(args, "workers", 1) or 1)
    print(f"measuring {len(todo)} of {len(decks)} deck(s), {workers} at a time "
          f"(~60–90 s each, one virtual display per worker)")

    from collections import Counter
    verdicts, worst, failed = Counter(), 0.0, 0
    # `batch` claims a display per worker from the same pool `attacks` uses and
    # yields in completion order, so the results arrive out of argument order
    # and have to be matched back by path.  Serial was never a property of the
    # measurement — it was a property of there being one display — and over a
    # thousand decks the difference is about nineteen hours.
    by_path = {str(deck.source): deck for deck in todo}
    for rec in wps.batch(sorted(by_path), workers=workers):
        deck = by_path[rec["pptx"]]
        rep = rec.get("report")
        if rep is None:
            rep = {"renderer": "wps", "verdict": "unmeasured",
                   "error": (rec.get("error") or "no report")[:200]}
            failed += 1
        (deck.root / "roundtrip-wps.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=1))
        # fold it into the digest, which is the only place the proposer looks
        folded = pl.rebuild_digest(deck) if rep.get("verdict") != "unmeasured" \
            else False
        verdicts[rep.get("verdict")] += 1
        worst = max(worst, rep.get("changed_frac") or 0.0)
        print(f"  {deck.id}  {str(rep.get('verdict')):<11}"
              f"{(rep.get('changed_frac') or 0.0):.1%} of "
              f"{rep.get('shapes', '?')} shapes  {rec.get('seconds', 0):.0f}s"
              + ("  digest updated" if folded else "")
              + (f"  — {rep.get('error', '')[:60]}" if rep.get("error") else ""),
              flush=True)

    print("verdicts: " + ", ".join(f"{k} {v}" for k, v in verdicts.most_common()))
    print(f"worst drift seen: {worst:.1%} of shapes")
    left = [d for d in decks if not _wps_measured(d)]
    if left:
        print(f"{len(left)} deck(s) still unmeasured — their digests keep "
              f"`governs: null` on purpose; a number measured elsewhere is not "
              f"a measurement of them")
    if failed:
        print(f"{failed} deck(s) the GUI would not round-trip; the reason is "
              f"in each deck's roundtrip-wps.json")


def _api_retries(args) -> int:
    """The retry budget for one agent stage, from the flags."""
    n = getattr(args, "api_retries", None)
    return agentmod.API_RETRIES if n is None else max(0, int(n))


def _assignment(args) -> agentmod.Assignment:
    """Which model and effort each stage asks for, from the flags."""
    return agentmod.Assignment.from_args(args)


def _ran(res: dict, asked: dict) -> dict:
    """What to record about the model, per stage, in `state.json`.

    Both halves matter and they are not the same fact.  `model_asked` is what
    we requested — `null` meaning "whatever `claude` defaults to" — and it is
    what decides whether a later run may reuse this artefact.  `model_ran` is
    what the log says actually produced the tokens, which is the only way a
    `--fallback-model` that quietly finished the deck on a weaker model is
    ever distinguishable from the rest of the batch.
    """
    rec = {"model_asked": asked.get("model"), "effort": asked.get("effort")}
    for key in ("model_ran", "fallback"):
        if res.get(key) is not None:
            rec[key] = res[key]
    return rec


def _model_changed(deck, stage: str, args) -> str | None:
    """Whether this stage's artefact was made under a different assignment.

    Re-running `propose` under another model is a different artefact, not a
    cache hit — which is the whole point of being able to set them per stage.

    It cannot fall out of `STAGE_INPUTS` for free: those are paths under the
    deck root and `Deck.fingerprint` digests *files*, and a model name is not
    one.  Making it a real fingerprint input is a line in `pipeline.py`; this
    is the same rule enforced where the decision is actually taken, which is
    the `done()` / `promoted()` guard right here.

    A stage with no `model_asked` predates the flag and gets the benefit of
    the doubt — otherwise every deck in every existing work directory would
    re-run the moment this shipped.
    """
    st = deck.state().get(stage, {})
    if "model_asked" not in st:
        return None
    was = (st.get("model_asked"), st.get("effort"))
    want = _assignment(args).for_stage(stage)
    now = (want["model"], want["effort"])
    if was == now:
        return None
    return f"{_show_model(was)} → {_show_model(now)}"


def _show_model(pair) -> str:
    model, effort = pair
    return (model or "default") + (f"/{effort}" if effort else "")


def _skip_done(deck, stage: str, args, word: str) -> str | None:
    """The "nothing to do" line, or None when the stage has to run."""
    if not deck.done(stage) or getattr(args, "force", False):
        return None
    if _model_changed(deck, stage, args):
        return None
    return _skip(deck, stage, SKIP_DONE, f"({word})")


def _redo_note(deck, stage: str, args) -> str:
    changed = _model_changed(deck, stage, args) if deck.done(stage) else None
    return f"   (re-run: {changed})" if changed else ""


def _limped(res: dict) -> dict:
    """What to record about a run that needed more than one attempt.

    Retrying is not a licence to call the deck clean.  A deck that took four
    attempts is a measurement of the account's headroom, and a batch where
    every deck limped is a batch that wanted fewer `--workers`, not more.
    """
    n = res.get("attempts", 1)
    return {"api_attempts": n} if n > 1 else {}


def _stamps(outputs) -> dict:
    """What each of a stage's outputs looks like before the stage runs.

    `agent._stamp` is `(mtime_ns, size)` and it is deliberately not a clock
    reading: filesystem timestamps are quantised — ~10 ms here — so a file
    written *after* an attempt began can carry an mtime a few milliseconds
    before it, and a wall-clock comparison silently calls this attempt's work
    somebody else's.  Reusing it rather than writing a second one keeps the
    retry path and this one answering the same question the same way.
    """
    return {p: agentmod._stamp(p) for p in outputs}


def _left_over(outputs, before: dict) -> list:
    """The outputs this attempt did not write, and did not have to create.

    A stage killed mid-run — Ctrl-C, an OOM, a machine going away — leaves
    whatever the agent had written by then on disk.  Resume is otherwise
    correct: `promoted()` requires `ok`, so the stage simply runs again and
    normally overwrites it.  What is not covered is the re-run that *fails*
    without writing: the checker then reads the corpse of the previous attempt,
    finds a well-formed file, and passes it — and the deck carries a verdict
    reached by a process that was killed.

    An output that was on disk when this attempt began and is byte-for-byte
    where it was when the attempt ended is that corpse.  A file this attempt
    created (nothing before) or moved out of the way (nothing after) is not.
    """
    return [p for p in outputs
            if before.get(p) is not None and agentmod._stamp(p) == before[p]]


def _record_retries(deck, stage: str, res: dict):
    """Put each infra retry on the run's record, not only the deck's.

    `state.json` keeps the count; what it cannot keep is *when*.  Thirteen
    session errors across a ninety-minute run are either an account under
    steady pressure or one five-minute outage, and those want opposite
    responses — the only thing that distinguishes them is the clock.
    """
    for h in res.get("retried") or []:
        pl.log_event("stage_retried", deck=deck.id, stage=stage,
                     attempt=h.get("attempt"), kind=h.get("kind"),
                     why=h.get("why"), backoff_s=h.get("backoff_s"),
                     kept=h.get("kept"))


def _agent_stage(deck, stage, spec_builder, checker, args):
    try:
        with pl.lock(deck, stage):
            # keep the previous attempt: the log is opened "w" and the output
            # file is overwritten, so without this a re-run erases the evidence
            # of what the last one decided
            kept = pl.archive_attempt(deck, stage)
            spec = spec_builder(deck)
            asked = _assignment(args).apply(spec, stage)
            spec.timeout_min = args.timeout
            spec.log = deck.root / f"{stage}.jsonl"
            # The retries happen inside the pool slot this stage is holding,
            # deliberately.  Handing the slot back so another deck can take it
            # is exactly wrong when the thing that failed is a per-account rate
            # limit: it would replace one waiting deck with one more caller.
            spec.api_retries = _api_retries(args)
            # taken here, where the attempt begins, and read back below: an
            # output older than this line is not this attempt's answer
            before = _stamps(spec.outputs)
            res = asyncio.run(agentmod.run_agent(spec))
            _record_retries(deck, stage, res)
            # every outcome carries the same two facts: how many attempts it
            # took, and which model made it
            record = {**_limped(res), **_ran(res, asked)}
            if res["status"] == "timeout":
                # not retried, on purpose: the agent was working and ran out of
                # clock, and a second run costs the same clock for the same
                # answer
                deck.mark(stage, "failed", error="timeout", log=res["log"],
                          **record)
                return f"TIMEOUT after {args.timeout}min"
            if res["status"] == "infra":
                # the budget is spent.  Still not a verdict about the deck:
                # leave the stage unjudged so a re-run picks it up, and never
                # let the repair loop count it
                deck.mark(stage, "infra", error=res["why"], log=res["log"],
                          **record)
                tries = res.get("attempts", 1)
                return (f"INFRA after {tries} attempt(s) — {res['why']}"
                        if tries > 1 else f"INFRA — {res['why']}")
            if res["status"] == "truncated":
                # the file it left behind will very likely pass the checker.
                # Also not retried: `max_turns` is a ceiling, not a flake
                deck.mark(stage, "failed", error=res["why"], log=res["log"],
                          **record)
                return f"TRUNCATED — {res['why']}"
            if res["status"] == "barrier":
                # the launcher refused to start the agent because it could not
                # seal it off from what it must not read.  Nothing ran, so
                # there is no answer to judge — and a stage that quietly ran it
                # anyway is the defect this exists to prevent
                deck.mark(stage, "failed", error=res["why"], log=res["log"],
                          **record)
                return f"BARRIER — {res['why']}"
            stale = _left_over(spec.outputs, before)
            if stale:
                # The checker is about to be handed a file this run never
                # wrote.  It would very likely pass it — that is the point of
                # `_left_over` — so it is not asked.  The bytes are already
                # copied into `attempts/`, and what is left on disk would
                # otherwise be read as a verdict by `_rework_of` next round.
                names = ", ".join(p.name for p in stale)
                for p in stale:
                    if kept:
                        p.unlink(missing_ok=True)
                deck.mark(stage, "failed", log=res["log"], **record,
                          error=f"the agent wrote nothing and {names} was "
                                f"left over from an earlier attempt"
                                + (f" (kept in {kept})" if kept else ""))
                return (f"REJECTED — {names} predates this attempt, so it is "
                        f"not this attempt's answer"
                        + (f"; the earlier one is in {kept}" if kept else ""))
            try:
                detail = checker(deck)
            except pl.StageError as e:
                deck.mark(stage, "failed", error=str(e), log=res["log"],
                          **record)
                return f"REJECTED — {e}"
            # a well-formed report is not the same as a passing one: the gates
            # return their verdict and it decides the stage's status
            v = detail.get("verdict")
            sent_back = v is not None and v not in pl.PASSING_VERDICTS
            deck.mark(stage, "rejected" if sent_back else "ok",
                      **{**detail, **record})
            # the console keeps the gate's own summary plus the one fact a
            # reader acts on; which model ran is a question for `state.json`
            # and the `status` table, not for every line of a batch log
            return {**detail, **_limped(res)}
    except pl.DeckBusy as e:
        return f"BUSY — {e}"


def _propose_one(deck, args):
    if (line := _skip_done(deck, "proposed", args, "already proposed")):
        return line
    if not deck.done("inspected"):
        return _skip(deck, "proposed", SKIP_UPSTREAM,
                     "skipped — not inspected")
    redo = _redo_note(deck, "proposed", args)
    out = _agent_stage(
        deck, "proposed",
        lambda d: agentmod.AgentRun("proposer", agentmod.propose_prompt(d),
                                    outputs=[d.proposal]),
        pl.check_proposal, args)
    return f"{deck.id}  {out}{redo}"


def cmd_propose(args):
    _each(args, _propose_one, "proposed")


def _recipe_one(deck, args):
    if (line := _skip_done(deck, "recipe", args, "already has a recipe")):
        return line
    if not deck.done("proposed"):
        return _skip(deck, "recipe", SKIP_UPSTREAM, "skipped — not proposed")
    if not (json.loads(deck.proposal.read_text()).get("tasks")):
        deck.mark("recipe", "skipped", reason="proposal is empty by design")
        return f"{deck.id}  skipped — deck yields no task"
    redo = _redo_note(deck, "recipe", args)
    out = _agent_stage(
        deck, "recipe",
        lambda d: agentmod.AgentRun("recipe-writer", agentmod.recipe_prompt(d),
                                    max_turns=80, outputs=[d.recipe]),
        pl.check_recipe, args)
    return f"{deck.id}  {out}{redo}"


def cmd_recipe(args):
    _each(args, _recipe_one, "recipe")


def _degrade_one(deck, args):
    if deck.done("degraded") and not args.force:
        return _skip(deck, "degraded", SKIP_DONE, "(already degraded)")
    if not deck.done("recipe"):
        return _skip(deck, "degraded", SKIP_UPSTREAM, "skipped — no recipe")
    try:
        # the lock is what stops the recipe agent from committing its own work:
        # its parent holds this deck while it runs, so a shelled-out
        # `pptxgym degrade` is refused and it has to use `tools trial`
        with pl.lock(deck, "degraded"):
            d = pl.degrade(deck)
        return (f"{deck.id}  {d['changes']} change(s) on {d['slides']} "
                f"slide(s)  gate={d['gate']}")
    except pl.DeckBusy as e:
        return f"{deck.id}  BUSY — {e}"
    except pl.StageError as e:
        return f"{deck.id}  FAILED — {e}"


def cmd_degrade(args):
    _each(args, _degrade_one, "degraded")


def _materialise_one(deck, args):
    st = deck.status_of("materialised")
    if st in ("ok", "partial") and not args.force:
        return _skip(deck, "materialised", SKIP_DONE, "(already materialised)")
    if not deck.done("degraded"):
        return _skip(deck, "materialised", SKIP_UPSTREAM,
                     "skipped — not degraded")
    try:
        # Under the deck lock, like every other stage that writes.  This was
        # the one that was not, and it cost a real run: a `materialise`
        # re-running here landed on a deck whose `reconcile` agent was
        # mid-judgement in another process, and that agent then failed with
        # "the agent wrote nothing" — a deck lost to a race, reported as a
        # model failure.  Two concurrent runs is not a hypothetical
        # configuration; it is what scaling this out looks like.
        with pl.lock(deck, "materialised"):
            d = pl.materialise(deck)
        tail = f"  ({d['unmet']} unmet)" if d.get("unmet") else ""
        return (f"{deck.id}  {d['produced']} asset(s): "
                f"{', '.join(d['kinds']) or '—'}{tail}")
    except pl.DeckBusy as e:
        return f"{deck.id}  BUSY — {e}"
    except pl.StageError as e:
        return f"{deck.id}  FAILED — {e}"


def cmd_materialise(args):
    _each(args, _materialise_one, "materialised")


def _reconcile_one(deck, args):
    if (line := _skip_done(deck, "reconciled", args, "already reconciled")):
        return line
    mat = deck.status_of("materialised")
    if mat not in ("ok", "partial"):
        return _skip(deck, "reconciled", SKIP_UPSTREAM,
                     "skipped — assets not materialised")
    redo = _redo_note(deck, "reconciled", args)
    out = _agent_stage(
        deck, "reconciled",
        lambda d: agentmod.AgentRun("reconciler", agentmod.reconcile_prompt(d),
                                    max_turns=60,
                                    outputs=[d.root / "task.json"]),
        pl.check_reconcile, args)
    return f"{deck.id}  {out}{redo}"


def cmd_reconcile(args):
    _each(args, _reconcile_one, "reconciled")


# --------------------------------------------------------------------------- #
# the three deterministic stages after the last agent
#
# They cost CPU and not API capacity — `_workers_for` puts anything outside
# `_AGENT_WORK` on the cpu pool, so they need no special case there — and every
# one of them can come back `rejected`, which `run` routes to the repair loop
# exactly as it routes a rejected reconcile.
# --------------------------------------------------------------------------- #


def _cpu_gate(deck, stage: str, fn, ok_line) -> str:
    """Run one deterministic gate under the deck lock, and say what it decided.

    The shape all three share: archive the previous attempt so a re-run cannot
    quietly replace the last verdict, run, and report the first problem when
    there is one.  `fn` marks the stage itself — a gate whose verdict is
    written by its caller can be overruled by its caller.
    """
    try:
        with pl.lock(deck, stage):
            pl.archive_attempt(deck, stage)
            detail = fn(deck)
    except pl.DeckBusy as e:
        return f"{deck.id}  BUSY — {e}"
    except pl.StageError as e:
        return f"{deck.id}  FAILED — {e}"
    # Formatting is not the work, and must not be able to undo it.  `fn` has
    # already marked the stage by the time we get here, so an exception raised
    # while *describing* what happened used to propagate out of the gate and
    # park a deck whose stage was recorded `ok` moments earlier.  That is how
    # deck0007 died: `harden` began returning `Report.coverage()` and this
    # line still asked for the removed `attacks` key.
    #
    # The formatter is the most volatile thing here — it names keys from a
    # dict three modules away — and the least important. It says so and
    # carries on.
    try:
        line = f"{deck.id}  {ok_line(detail)}"
    except Exception as e:                     # noqa: BLE001 — reported, not raised
        line = (f"{deck.id}  {stage} ok, but this line could not be built "
                f"({type(e).__name__}: {e}) — see state.json")
    if detail.get("problems"):
        line += f"   REJECTED — {detail['problems'][0][:150]}"
    return line


def _score_one(deck, args):
    if deck.done("scored") and not args.force:
        return _skip(deck, "scored", SKIP_DONE, "(already scored)")
    if not deck.done("solvable"):
        return _skip(deck, "scored", SKIP_UPSTREAM,
                     "skipped — not through the solvability gate")
    return _cpu_gate(
        deck, "scored", pl.score_task,
        lambda d: (f"{d['components']} component(s)  gt={d['gt']:.3f}  "
                   f"input={d['input']:.3f}  weights={d['weights']}"
                   + (f"  ({d['unscoreable']} unscoreable: "
                      f"{'; '.join(d.get('unscoreable_why') or [])})"
                      if d.get("unscoreable") else "")))


def cmd_score(args):
    _each(args, _score_one, "scored")


def _harden_one(deck, args):
    if deck.done("hardened") and not args.force:
        return _skip(deck, "hardened", SKIP_DONE, "(already hardened)")
    if not deck.done("scored"):
        return _skip(deck, "hardened", SKIP_UPSTREAM,
                     "skipped — no accepted scoring plan to attack")
    return _cpu_gate(
        deck, "hardened",
        lambda d: pl.harden(d, workers=args.attack_workers,
                            wps_workers=args.wps_workers,
                            wps=not args.no_wps, keep=args.keep_candidates),
        # `attacks_scored`/`attacks_total`, not `attacks`: `harden` returns
        # `Report.coverage()` now, which counts what *ran*.  This line still
        # asked for the removed key and raised KeyError — after `hardened` had
        # already been recorded `ok`, so a deck was parked on a console message
        # about work that had succeeded.
        lambda d: (f"{d['attacks_scored']}/{d['attacks_total']} attack(s), "
                   f"{d['variants_scored']}/{d['variants_total']} variant(s)"
                   + (f"  beaten by {', '.join(d['beaten'])}"
                      if d.get("beaten") else "")
                   + (f"  credit lost on {', '.join(d['variants_lost'])}"
                      if d.get("variants_lost") else "")))


def cmd_harden(args):
    _each(args, _harden_one, "hardened")


def _package_one(deck, args):
    if deck.done("packaged") and not args.force:
        return _skip(deck, "packaged", SKIP_DONE, "(already packaged)")
    if not deck.done("hardened"):
        return _skip(deck, "packaged", SKIP_UPSTREAM,
                     "skipped — not through the attack battery")
    out_root = Path(args.out or (Path(args.work) / "emitted"))
    return _cpu_gate(
        deck, "packaged",
        lambda d: pl.package(d, out_root, getattr(args, "task_id", None)),
        lambda d: (f"task_{d.get('task_id')}  {d.get('components', '?')} "
                   f"component(s)  consistency={d.get('consistency')}"
                   + (f"  ({d['warn']} warn)" if d.get("warn") else "")))


def cmd_package(args):
    _each(args, _package_one, "packaged")


def _solvable_one(deck, args):
    if deck.done("solvable") and not args.force \
            and not _model_changed(deck, "solvable", args):
        # A deck that passed before the bundle was an artefact — or on another
        # machine — holds a verdict with nothing to deliver.  Bundling is
        # deterministic and the stage fingerprints say these are the same bytes
        # the verdict was reached on, so rebuild it rather than re-probe it or,
        # worse, publish one fewer task than was approved.
        gaps = pl.bundle_problems(deck)
        if gaps:
            pl.bundle(deck)
            return _skip(deck, "solvable", SKIP_DONE,
                         f"(already probed; bundle rebuilt — {gaps[0]})")
        return _skip(deck, "solvable", SKIP_DONE, "(already probed)")
    if not deck.done("reconciled"):
        return _skip(deck, "solvable", SKIP_UPSTREAM,
                     "skipped — not reconciled")
    t = json.loads((deck.root / "task.json").read_text())
    if t.get("verdict") == "needs_rework":
        return _skip(deck, "solvable", SKIP_UPSTREAM,
                     "skipped — reconcile rejected it first")
    # rebuilt every time: the probe judges the files as they stand now, and a
    # bundle left over from before a repair would have it judging the old task
    pl.bundle(deck)
    redo = _redo_note(deck, "solvable", args)
    # The probe does not run here.  It runs in a copy of the bundle under the
    # system temp directory, inside a namespace where `work/` and the corpus
    # are empty — so the answer key is not something it is asked to leave
    # alone, it is something that is not there.  See `pl.probe_workspace`.
    try:
        with pl.probe_workspace(deck) as ws:
            out = _agent_stage(
                deck, "solvable",
                lambda d: agentmod.AgentRun(
                    "solver-probe", agentmod.solvability_prompt(d, ws),
                    cwd=ws.dir, max_turns=50,
                    launcher=ws.launcher, env=ws.env, settings=ws.settings,
                    add_dirs=[agentmod.SKILLS], collect=ws.collect,
                    outputs=[d.root / "solvability.json"]),
                pl.check_solvability, args)
    except pl.StageError as e:
        # the workspace could not be built, or the barrier could not be
        # established.  Not a verdict about the deck, and emphatically not a
        # reason to probe it without one
        deck.mark("solvable", "failed", error=str(e))
        return f"{deck.id}  FAILED — {e}"
    return f"{deck.id}  {out}{redo}"


def cmd_solvable(args):
    _each(args, _solvable_one, "solvable")


def _rework_from_verdict(d: dict) -> list | None:
    """An agent gate writes its own work order; we only read it."""
    if d.get("verdict") in pl.PASSING_VERDICTS:
        return None
    return d.get("rework") or None


def _rework_from_plan(d: dict) -> list | None:
    """`comparators` rejects a plan in prose, and every reason is the same
    stage: a floor that will not sit at zero, a degradation nothing scores, a
    gate that fires on correct work.  None of them is a tolerance to widen —
    the module says so at its own top — so they go back to `recipe` as one
    work order rather than as a list of separate complaints, because they are
    usually one mistake seen from several angles.
    """
    reasons = d.get("rejected") or []
    if not reasons:
        return None
    return [{"stage": "recipe",
             "what": "The scoring plan derived from this recipe cannot be "
                     "used. Change what is broken, not the comparator: "
                     + "; ".join(reasons[:4]),
             "why": "a component whose floor is above the limit pays for work "
                    "nobody did, and a degradation with no scoreable component "
                    "asks for work nobody scores"}]


def _rework_from_attacks(d: dict) -> list | None:
    reasons = d.get("rejected") or []
    if not reasons:
        return None
    return [{"stage": "recipe",
             "what": "The attack battery beat this task, or a legitimate "
                     "solution lost credit on it: " + "; ".join(reasons[:4]),
             "why": "better to lose a task than to ship one that can be "
                    "cheated — and a gate that fires on correct work rejects "
                    "the task exactly as a successful cheat does"}]


def _rework_from_consistency(d: dict) -> list | None:
    fail, _warn = pl.consistency_problems(d)
    if not fail:
        return None
    return [{"stage": "recipe",
             "what": "The instruction and the files contradict each other: "
                     + "; ".join(fail[:4]),
             "why": "the instruction described damage that was never applied, "
                    "or demanded something the ground truth is not — either "
                    "way the solver is being sent after an answer that is not "
                    "there"}]


#: Where an open work order can come from, in the order they are believed.
#: The two agent gates come first because they are upstream: a deck reconcile
#: has already sent back is not also a scoring problem, it is the same problem
#: seen earlier.  `task.json` is not retired after a repair — reconcile rewrites
#: it — but every other artefact here is regenerated by a deterministic stage,
#: so leaving it on disk would have `_rework_of` order the same repair again
#: next round with the fix already applied.
GATE_ARTEFACTS = (
    ("solvability.json", "solvable", _rework_from_verdict),
    ("task.json", None, _rework_from_verdict),
    ("plan.json", "scored", _rework_from_plan),
    ("attacks.json", "hardened", _rework_from_attacks),
    ("consistency.json", "packaged", _rework_from_consistency),
)


def _rework_of(deck):
    """The open work order, from whichever gate rejected the deck."""
    for name, _stage, reader in GATE_ARTEFACTS:
        f = deck.root / name
        if not f.exists():
            continue
        try:
            d = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        rework = reader(d)
        if rework:
            return rework, f"{name}:{d.get('verdict') or 'rejected'}"
    return None, None


#: What a repair to each stage is expected to rewrite.  `materialise` names the
#: proposal because that is where an asset is *declared*; the files themselves
#: are produced by the stage, not by the repairer.
REPAIR_ARTEFACT = {"proposed": "proposal.json", "recipe": "recipe.json",
                   "materialise": "proposal.json"}


def _repair_outputs(deck, rework) -> list:
    """The artefacts this work order tells the repairer to rewrite.

    Declared on the spec so the retry machinery covers the repairer too.  It
    was the one agent stage naming no `outputs` at all — and it is the stage
    whose entire job is to rewrite upstream artefacts, so a repair killed by a
    403 half-way through `recipe.json` left the half-written file exactly where
    the next stage would read it, with no copy of what it replaced.
    """
    names = {REPAIR_ARTEFACT[r["stage"]] for r in rework
             if r.get("stage") in REPAIR_ARTEFACT}
    return [deck.root / n for n in sorted(names)]


def _repair_watch(deck, rework) -> list:
    """Everything a repair of this work order could legitimately have moved.

    Wider than `_repair_outputs`, and only used to answer "did anything happen
    at all".  A repair ordered at `materialise` may fix the declaration in the
    proposal *or* the asset beside it, and calling the second one a no-op would
    park a deck that had just been repaired correctly.
    """
    out = list(_repair_outputs(deck, rework))
    if any(r.get("stage") == "materialise" for r in rework):
        adir = deck.root / "assets"
        out += sorted(p for p in adir.rglob("*") if p.is_file()) \
            if adir.is_dir() else []
    return out


def _repair_one(deck, args):
    if deck.status_of("reconciled") not in ("ok", "stale", "rejected"):
        return _skip(deck, "repair", SKIP_UPSTREAM,
                     "skipped — not reconciled yet")
    rework, source = _rework_of(deck)
    if not rework:
        return _skip(deck, "repair", SKIP_NOTHING, "nothing to repair")
    done = pl.repairs_done(deck)
    if done >= pl.MAX_REPAIRS:
        deck.mark("reconciled", "needs_human", attempts=done,
                  rejected_by=source, reason=rework[0].get("what", "")[:200])
        return (f"{deck.id}  PARKED after {done} repair attempts — "
                f"needs a human")
    try:
        with pl.lock(deck, "repair"):
            tools_before = pl.tool_tree_state()
            spec = agentmod.AgentRun(
                "orchestrator", agentmod.repair_prompt(deck, rework, source),
                max_turns=60)
            asked = _assignment(args).apply(spec, "repair")
            spec.timeout_min = args.timeout
            spec.api_retries = _api_retries(args)
            spec.outputs = _repair_outputs(deck, rework)
            # Named from the logs on disk, not from `done`: an attempt that
            # died on infrastructure keeps its log and no longer counts, so the
            # two numbers part company — and reusing the number would have the
            # next repair overwrite the evidence of the outage.
            spec.log = pl.next_repair_log(deck)
            watched = _repair_watch(deck, rework)
            before = _stamps(watched)
            res = asyncio.run(agentmod.run_agent(spec))
            _record_retries(deck, "repair", res)
            # a repair fixes one deck; the tools are shared by all of them
            edited = pl.revert_tool_changes(deck, tools_before, spec.log.stem)
    except pl.DeckBusy as e:
        return f"{deck.id}  BUSY — {e}"
    if edited:
        deck.mark("reconciled", "needs_human", attempts=done + 1,
                  rejected_by=source,
                  reason=f"the repair edited the shared tools ({edited}); "
                         f"reverted, diff kept beside the log for review")
        return (f"{deck.id}  STOPPED — the repair edited {edited}, which is "
                f"off limits; change reverted, diff kept for review")
    # The repairer is an agent stage under another name, so it is recorded like
    # one: which model, how many attempts, and how it ended.  `truncated` is
    # `failed` and not `ok`: repair runs at max_turns=60 and this batch's
    # repairs used 57, 60, 62 and 57 turns, so a repairer cut off mid-thought
    # is the common case rather than the exotic one — and calling that a
    # successful repair retires the complaint nobody addressed, which is the
    # same laundering `_infra_failure` exists to stop.
    status = {"timeout": "failed", "truncated": "failed",
              "infra": "infra"}.get(res["status"], "ok")
    # It ran, it finished, and not one byte of what it was told to change
    # moved.  Treating that as a repair retires the verdict that ordered it and
    # re-runs every stage below on exactly the files that were just rejected —
    # a whole round of the loop, at agent prices, to reach the same answer.
    untouched = status == "ok" and _stamps(_repair_watch(deck, rework)) == before
    named = ", ".join(p.name for p in spec.outputs) or "anything upstream"
    deck.mark("repair", "failed" if untouched else status,
              attempt=done + 1, rejected_by=source, log=str(spec.log),
              **_limped(res), **_ran(res, asked),
              **({"error": f"the repair changed none of {named}"}
                 if untouched else {}))
    if res["status"] == "timeout":
        return f"{deck.id}  repair TIMEOUT"
    if res["status"] == "truncated":
        return f"{deck.id}  repair TRUNCATED — {res['why']}"
    if res["status"] == "infra":
        # The retries are spent and the repairer never got to work.  Falling
        # through would invalidate the stages below it and retire the verdict
        # that ordered the repair — a whole round of the loop consumed by an
        # outage, and the complaint marked as addressed by nobody.
        return (f"{deck.id}  repair INFRA after {res.get('attempts', 1)} "
                f"attempt(s) — {res['why']}")
    if untouched:
        return f"{deck.id}  repair CHANGED NOTHING — {named} are as they were"
    # whatever it touched, the stages below that point are now stale
    stages = {r.get("stage") for r in rework}
    for stg in ("proposed", "recipe", "materialise"):
        if stg in stages:
            pl.invalidate_from(deck, stg)
    # the one event a run-level reader cannot reconstruct from anywhere else:
    # this deck, at this moment, went back to that stage, and here is the
    # verdict that sent it
    pl.log_event("sent_back", deck=deck.id, stage="repair", source=source,
                 to=sorted(s for s in stages if s), attempt=done + 1,
                 what=(rework[0].get("what") or ""))
    # Retire the verdict that ordered this repair: left in place, `_rework_of`
    # reads it again next round and the loop repairs the same complaint until
    # it hits MAX_REPAIRS, with the fix already applied.  Every artefact in
    # `GATE_ARTEFACTS` that a deterministic stage rebuilds is retired the same
    # way, for the same reason — the one exception is `task.json`, which
    # reconcile overwrites itself.
    name = (source or "").split(":", 1)[0]
    stage = next((s for n, s, _r in GATE_ARTEFACTS if n == name and s), None)
    if stage:
        pl.archive_attempt(deck, stage)
        (deck.root / name).unlink(missing_ok=True)
        deck.mark(stage, "stale", reason=f"repaired after {source}")
    return (f"{deck.id}  repaired (attempt {done + 1}) after {source}, "
            f"re-running from {sorted(stages) or ['?']}")


def cmd_repair(args):
    _each(args, _repair_one, "repair")


STAGE_FN = {"inspected": "cmd_inspect", "proposed": "cmd_propose",
            "recipe": "cmd_recipe", "degraded": "cmd_degrade",
            "materialised": "cmd_materialise", "reconciled": "cmd_reconcile",
            "solvable": "cmd_solvable", "scored": "cmd_score",
            "hardened": "cmd_harden", "packaged": "cmd_package"}


async def _run_one(deck, args, pools, deadline=None):
    """Drive one deck through the stages, honouring what is already done.

    The deck holds no slot of its own.  It takes one from the pool that fits
    the stage about to run and gives it straight back, so a deck queueing for
    the API is not also occupying a renderer, and a deck three repairs deep is
    not occupying anything at all while it waits.
    """
    try:
        await _run_stages(deck, args, pools, deadline)
    except Exception:                                            # noqa: BLE001
        print("  " + _record_crash(deck, "run"))
    finally:
        # Every deck that stops contributes to the median, including a parked
        # one: it spent that time, and a sample set drawn only from the decks
        # that went well would raise the deadline every time the batch went
        # badly.
        if deadline is not None:
            deadline.record(deck)


def _stage_args(args, deck_id: str, **over) -> argparse.Namespace:
    """The Namespace `run` hands a sub-command for one deck.

    The worker limits are `SUB_WORKERS` and not a literal, so the invariant
    they encode has a name and one place to change.  `_each` refuses anything
    else while `run` is scheduling.
    """
    ns = argparse.Namespace(
        work=args.work, deck=[deck_id], force=False, dpi=args.dpi,
        roundtrip=getattr(args, "roundtrip", False),
        workers=SUB_WORKERS, cpu_workers=SUB_WORKERS,
        # the assignment travels down as the raw strings rather than as one
        # resolved model: the sub-command parses them for the stage *it* is
        # running, so `--model propose=opus` means the same thing whether it
        # was typed at `run` or at `propose`
        timeout=args.timeout,
        model=args.model, effort=getattr(args, "effort", None),
        fallback_model=getattr(args, "fallback_model", None),
        # the API budget is per agent stage, so it travels down unchanged: a
        # `run` that tolerates three 429s tolerates them at every stage
        api_retries=_api_retries(args),
        # the deterministic tail.  `attack_workers` and `wps_workers` are NOT
        # forced to one: they are threads and displays *inside* one deck's
        # stage, so they are bounded by the cpu pool that already holds the
        # deck, not by the rule that stops a sub-command opening a second pool.
        out=getattr(args, "out", None), task_id=None,
        attack_workers=getattr(args, "attack_workers", 4),
        wps_workers=getattr(args, "wps_workers", 2),
        no_wps=getattr(args, "no_wps", False),
        keep_candidates=getattr(args, "keep_candidates", False))
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _parked(deck) -> bool:
    """Has somebody, or something, given up on this deck?

    Any stage will do.  A park is a decision — it says the loop has nothing
    left to try — and the next thing the run does must not quietly work on the
    deck anyway.
    """
    return any(isinstance(v, dict) and v.get("status") == "needs_human"
               for v in deck.state().values())


def _repaired(deck, was_attempt) -> bool:
    """Did the repair that was just scheduled actually repair anything?

    Three ways it did not, and all three used to read the same as success from
    outside: the deck was parked instead, the repairer never ran (no work
    order, a held lock), or it ran and ended in something other than `ok`.
    `attempt` is what separates a repair that happened from a record left by
    the previous round.
    """
    if _parked(deck):
        return False
    rec = deck.state().get("repair", {})
    return rec.get("status") == "ok" and rec.get("attempt") != was_attempt


async def _run_stages(deck, args, pools, deadline=None):
    loop = asyncio.get_running_loop()

    # Before anything is skipped as done: a deck parked against a producer we
    # have since fixed is stale, not bad, and it must not sit behind a spent
    # repair budget for a bug it did not make.  Fires only on a code digest
    # moving, which no repairer is permitted to do.
    refunded = pl.retire_park_after_code_fix(deck)
    if refunded:
        print(f"  {deck.id}  UNPARKED — {refunded}")

    async def step(stage, fn, ns):
        async with pools.for_stage(stage):
            deck.begin(stage)          # inside the slot: work, not the wait
            return await loop.run_in_executor(None, fn, ns)

    def out_of_time(stage) -> bool:
        """Park the deck if it has spent its budget, before it spends more.

        Checked at a stage boundary and never inside one.  Interrupting a
        running agent would leave exactly the half-written artefact
        `_left_over` exists to refuse, and would do it deliberately.
        """
        why = deadline.over(deck) if deadline is not None else None
        if not why:
            return False
        deck.mark(stage, "needs_human", reason=why,
                  worked_s=round(deck.worked(), 1))
        print(f"  {deck.id}  PARKED before {stage} — {why}")
        return True

    for stage in pl.STAGES:
        # a stage whose artefact was made under a different model is not a
        # cache hit — see `_model_changed`.  It re-runs, and the sub-command
        # agrees, because both ask the same question.
        if pl.STAGES.index(stage) > pl.STAGES.index(args.until):
            # asked before anything is recorded: a run told to stop at
            # `reconciled` did not skip `packaged`, it was never going to
            # reach it, and a log saying otherwise reads as work avoided
            break
        if deck.promoted(stage) and not _model_changed(deck, stage, args):
            if stage != "ingested":
                # The commonest event in a resumed run, and until now the only
                # one that left nothing behind: the sub-command is not even
                # called, so its own "(already …)" line never happens either.
                _skip(deck, stage, SKIP_DONE, "(already done)")
            continue
        if stage == "ingested":
            continue
        if out_of_time(stage):
            return
        ns = _stage_args(args, deck.id)
        fn = globals()[STAGE_FN[stage]]
        await step(stage, fn, ns)
        rejected = deck.state().get(stage, {}).get("status") == "rejected"
        if not deck.promoted(stage) and not rejected:
            # A rejected *verdict* goes round the repair loop below — the gate
            # did its job and the answer was "no", so re-running the gate would
            # only ask the same question twice.  A stage whose output failed
            # the checker outright — malformed JSON, a missing key, a probe
            # that read the answer key — used to end the deck's run here
            # without a word.  One clean retry, then park it where `status`
            # shows it.
            ns2 = _stage_args(args, deck.id, force=True)
            await step(stage, fn, ns2)
            if deck.state().get(stage, {}).get("status") == "infra":
                return              # the API was down; nothing was judged
            if not deck.promoted(stage):
                # carry the failure detail across: parking a deck with a clean
                # record loses the only account of why it stopped, and `error`
                # is not where every stage puts it
                prev = {k: v for k, v in deck.state().get(stage, {}).items()
                        if k not in ("status", "at", "_in")}
                deck.mark(stage, "needs_human", attempts=2, **prev)
                return
        if stage in pl.GATE_STAGES:
            # A rejected deck goes round the repair loop rather than stopping
            # the run or, worse, being carried forward as if it had passed.
            # Every gate uses this one loop — the three deterministic ones
            # added last route back to `recipe` through the same
            # `_rework_of` / `invalidate_from` / repair-prompt path the agent
            # gates already close over.
            for _ in range(pl.MAX_REPAIRS):
                if not _rework_of(deck)[0]:
                    break
                if out_of_time("repair"):
                    return
                was = deck.state().get("repair", {}).get("attempt")
                await step("repair", cmd_repair, ns)
                if not _repaired(deck, was):
                    # Checked *before* the stages are re-run, which is the whole
                    # bug: `_repair_one` parks a deck by marking `reconciled`
                    # needs_human, the loop below then re-ran `reconciled`,
                    # which overwrote that mark with a fresh `rejected` — so the
                    # guard at the bottom never saw a parked deck and three
                    # reconcile sessions ran on decks already given up on.
                    #
                    # It covers the other half too.  When the repair timed out,
                    # hit its turn ceiling, died on the API or changed nothing,
                    # re-running every stage below asks the same question of the
                    # same bytes, at agent prices, for the same answer.
                    break
                for s2 in pl.STAGES[pl.STAGES.index("recipe"):]:
                    if not deck.promoted(s2) and pl.STAGES.index(s2) <= \
                            pl.STAGES.index(args.until):
                        if out_of_time(s2):
                            return
                        await step(s2, globals()[STAGE_FN[s2]], ns)
                if _parked(deck):
                    break
            if not deck.promoted(stage):
                # out of repairs, or the repair changed nothing.  Carrying on
                # to the next stage would build on a task the gate rejected,
                # which is the one outcome this loop exists to prevent.
                return


def cmd_run(args):
    global _UNDER_RUN
    decks = _decks(args)
    deadline = Deadline.from_args(args)

    async def main():
        loop = asyncio.get_running_loop()
        pools = _pools_for(args)
        with _threads_for(loop, *_pool_sizes(args)):
            # `return_exceptions` is the second net.  `_run_one` already
            # catches per deck, but a failure in the machinery around it — not
            # in a stage — would otherwise cancel every other deck mid-flight.
            for r in await asyncio.gather(
                    *[_run_one(d, args, pools, deadline) for d in decks],
                    return_exceptions=True):
                if isinstance(r, BaseException):
                    print(f"  batch error: {type(r).__name__}: {r}")

    _UNDER_RUN = True
    try:
        asyncio.run(main())
    finally:
        _UNDER_RUN = False
    cmd_status(args)


BIG_BATCH = 24          # beyond this a row per deck stops being readable


def _inflight(decks) -> list[tuple]:
    """Who is working on what right now, from the per-deck locks."""
    out = []
    for deck in decks:
        f = deck.root / ".lock"
        if not f.exists():
            continue
        try:
            held = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        pid = held.get("pid")
        alive = True
        if pid:
            try:
                os.kill(pid, 0)
            except OSError:
                alive = False
        out.append((deck.id, held.get("stage"), held.get("at"), pid, alive))
    return out


def _disk(work: Path) -> tuple[int, int]:
    """Bytes under the work directory, and how many decks that is spread over.

    A deck carries a source, a degraded copy, a render per slide and its
    assets.  Ten is nothing; a hundred is worth knowing before the disk says
    so on your behalf, halfway through a batch.
    """
    total = n = 0
    for deck_dir in sorted(work.glob("deck*")):
        if not deck_dir.is_dir():
            continue
        n += 1
        for p in deck_dir.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    return total, n


def _human(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024 or unit == "TB":
            return f"{nbytes:.0f}{unit}" if unit == "B" else f"{nbytes:.1f}{unit}"
        nbytes /= 1024.0


def _summarise(decks):
    """Counts rather than rows: the shape of a batch too big to read."""
    from collections import Counter
    furthest, verdicts = Counter(), Counter()
    for deck in decks:
        reached = "—"
        for s in pl.STAGES:
            if deck.promoted(s):
                reached = s
            else:
                break
        furthest[reached] += 1
        f = deck.root / "solvability.json"
        if f.exists():
            try:
                verdicts[json.loads(f.read_text()).get("verdict")] += 1
            except (OSError, json.JSONDecodeError):
                pass
    print("furthest stage reached")
    for s in pl.STAGES + ["—"]:
        if furthest.get(s):
            print(f"  {s:<14}{furthest[s]:>4}")
    if verdicts:
        print("solvability verdicts")
        for v, c in verdicts.most_common():
            print(f"  {str(v):<14}{c:>4}")


def cmd_status(args):
    decks = _decks(args)
    if len(decks) > BIG_BATCH and not getattr(args, "all", False):
        _summarise(decks)
        print(f"\n({len(decks)} decks — `--all` for the full table)")
        _status_tail(args, decks)
        return
    rows = []
    for deck in _decks(args):
        st = deck.state()
        cells = []
        for s in pl.STAGES:
            v = deck.status_of(s)
            cells.append({"ok": "✓", "failed": "✗", "skipped": "–",
                          "partial": "~", "stale": "≈", "rejected": "↺",
                          "infra": "!", "needs_human": "H",
                          None: "·"}.get(v, "?"))
        note = ""
        for s in reversed(pl.STAGES):
            if st.get(s, {}).get("status") == "failed":
                note = str(st[s].get("error", ""))[:70]
                break
        else:
            r = st.get("reconciled", {})
            d = st.get("degraded", {})
            if r.get("status") == "ok":
                note = (f"{r.get('difficulty')} {r.get('est_steps')} steps · "
                        f"{r.get('assets')} assets"
                        + ("  (instruction changed)"
                           if r.get("instruction_changed") else ""))
            elif d.get("status") == "ok":
                note = f"{d.get('changes')} changes / {d.get('slides')} slides"
        rows.append((deck.id, " ".join(cells), deck.meta().get("name", "")[:22],
                     note))
    # The columns are named once, above the table, rather than by a truncated
    # header that never lined up with them.  At eight stages the mismatch was
    # survivable; at eleven the table stops being readable, which is the whole
    # point of having one.
    width = 2 * len(pl.STAGES)
    print("stages: " + "  ".join(f"{i + 1}·{s}"
                                 for i, s in enumerate(pl.STAGES)))
    print(f"{'deck':<9}{' '.join(str((i + 1) % 10) for i in range(len(pl.STAGES))):<{width}} "
          f"{'file':<24} what it says")
    for r in rows:
        print(f"{r[0]:<9}{r[1]:<{width}} {r[2]:<24} {r[3]}")
    done = sum(1 for deck in _decks(args) if deck.done(pl.STAGES[-1]))
    print(f"\n{done}/{len(rows)} through `{pl.STAGES[-1]}`")
    _status_tail(args, _decks(args))


def _models_used(decks) -> dict:
    """Which model actually made each stage's artefacts, over the batch.

    The point of setting them per stage is to find out which stages are worth
    the strong model, and that question is unanswerable if the record says
    only what was asked for: a deck proposed by one model has to be
    distinguishable from a deck proposed by another, after the fact, from the
    deck itself.
    """
    from collections import Counter
    out: dict[str, "Counter"] = {}
    for deck in decks:
        for stage, rec in deck.state().items():
            if not isinstance(rec, dict) or "model_asked" not in rec:
                continue
            ran = rec.get("model_ran") or "unrecorded"
            if rec.get("effort"):
                ran += f"/{rec['effort']}"
            if rec.get("fallback"):
                ran += "  ← FALLBACK, not the model we asked for"
            out.setdefault(stage, Counter())[ran] += 1
    return out


def _api_attempts(state: dict) -> int:
    """The most attempts any one stage of this deck needed, or 0 for a clean run."""
    worst = max((v.get("api_attempts", 1) for v in state.values()
                 if isinstance(v, dict)), default=1)
    return worst if worst > 1 else 0


def _status_tail(args, decks):
    """What is running, what is stuck, and what it all costs on disk."""
    live = _inflight(decks)
    if live:
        print(f"\nrunning now ({len(live)})")
        for did, stage, at, pid, alive in live:
            note = "" if alive else "   ← pid is gone, stale lock"
            print(f"  {did}  {stage:<12} since {at}  pid {pid}{note}")

    # A deck a gate sent back is not "still going": it is stopped, waiting for
    # a repair that only `run` performs.  Judged one stage at a time — which is
    # how anyone actually works — four of these sat rejected with nobody
    # scheduled to pick them up, and the table said nothing about it.
    open_work = []
    for deck in decks:
        rw, src = _rework_of(deck)
        if rw and not deck.done(pl.STAGES[-1]):
            open_work.append((deck, rw, src))
    if open_work:
        ids = " ".join(d.id for d, _, _ in open_work)
        print(f"\n{len(open_work)} deck(s) waiting on a repair "
              f"— `pptxgym run --deck {ids}`")
        for deck, rw, src in open_work[:12]:
            stages = ", ".join(sorted({r.get("stage", "?") for r in rw}))
            print(f"  {deck.id}  {src}  → {stages}: "
                  f"{(rw[0].get('what') or '')[:64]}")
        if len(open_work) > 12:
            print(f"  … and {len(open_work) - 12} more")

    # A passing verdict with nothing to hand over used to be invisible: three
    # decks carried `solvable: ok` and no bundle, and only `publish` noticed —
    # by quietly rebuilding them.  Existence only, so this stays cheap over a
    # whole corpus; `check_solvability` is where the bytes are verified.
    naked = [d.id for d in decks
             if d.done("solvable") and pl.bundle_problems(d, verify_bytes=False)]
    if naked:
        print(f"\n{len(naked)} deck(s) passed with no deliverable — "
              f"`pptxgym solvable --deck {' '.join(naked[:12])}` rebuilds the "
              f"bundle without re-probing")

    # A deck that got through on its fourth attempt is not the same deck as one
    # that got through on its first, and a batch where half of them limped is a
    # batch that wanted fewer `--workers`.  The rate limit is per account, so
    # this is the number that says whether the account had room for the run.
    limped = sorted(((d.id, n) for d in decks
                     if (n := _api_attempts(d.state()))), key=lambda r: -r[1])
    if limped:
        worst = limped[0]
        print(f"\n{len(limped)} deck(s) needed an API retry to get through "
              f"(worst: {worst[0]}, {worst[1]} attempts on one stage) — "
              f"the account was at its limit for part of this run")
        print("  " + "  ".join(f"{did}×{n}" for did, n in limped[:12])
              + (" …" if len(limped) > 12 else ""))

    used = _models_used(decks)
    if used:
        print("\nmodel per stage (what the logs say actually ran)")
        for stage in pl.STAGES + ["repair"]:
            if stage in used:
                print(f"  {stage:<14}" + ", ".join(
                    f"{m} ×{c}" for m, c in used[stage].most_common()))

    stuck = [d.id for d in decks
             if any(v.get("status") in ("needs_human", "crashed")
                    for v in d.state().values())]
    if stuck:
        print(f"\n{len(stuck)} deck(s) parked for a human: "
              f"{' '.join(stuck[:12])}{' …' if len(stuck) > 12 else ''}")

    total, n = _disk(Path(args.work))
    if n:
        print(f"\ndisk: {_human(total)} across {n} decks "
              f"(~{_human(total // n)} each)")

    last = pl.latest_run(Path(args.work))
    if last:
        print(f"\nlast run: {last.name} — `pptxgym history` for where its wall "
              f"clock went and which decks went round the loop")


# --------------------------------------------------------------------------- #
# the run, read back
#
# `status` answers "where is each deck now".  This answers the other question,
# the one that took a person and ten open directories: where did ninety minutes
# go, which decks went back to which stage, when, and why.  Everything here is
# read out of one `events.jsonl` — no state.json, no mtimes, no correlating a
# console log that carried neither timestamps nor stage names.
# --------------------------------------------------------------------------- #


def _hms(seconds) -> str:
    s = int(seconds or 0)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _stage_runs(events) -> list[dict]:
    """Every stage execution in the run, paired up and timed.

    `ms` is the pipeline's own measurement (the clock starts inside the pool
    slot, so it is work rather than waiting) and is used when it is there.  A
    stage marked without a `begin` — ingest, a repair recorded by hand — falls
    back to the gap between its start and finish records, and says which.
    """
    open_at: dict[tuple, float] = {}
    out = []
    for e in events:
        key = (e.get("deck"), e.get("stage"))
        if e.get("event") == "stage_started":
            open_at[key] = e.get("ts")
        elif e.get("event") == "stage_finished":
            began = open_at.pop(key, None)
            ms = e.get("ms")
            if ms is None and began is not None and e.get("ts") is not None:
                ms = int((e["ts"] - began) * 1000)
            out.append({"deck": e.get("deck"), "stage": e.get("stage"),
                        "status": e.get("status"), "ms": ms or 0,
                        "at": e.get("t"), "ts": e.get("ts"), "began": began,
                        "rec": e})
    for (deck, stage), began in open_at.items():
        # still running when the log ends — a killed run, or one being watched
        out.append({"deck": deck, "stage": stage, "status": "running",
                    "ms": 0, "at": None, "ts": None, "began": began, "rec": {}})
    return out


def _run_span(events) -> tuple[float, float]:
    ts = [e["ts"] for e in events if isinstance(e.get("ts"), (int, float))]
    return (min(ts), max(ts)) if ts else (0.0, 0.0)


def _history_header(head: dict, events: list, path: Path):
    lim = head.get("limits") or {}
    first, last = _run_span(events)
    end = next((e for e in reversed(events)
                if e.get("event") == "run_finished"), None)
    print(f"run {head.get('run') or path.parent.name}   {path}")
    print("  argv     " + " ".join(head.get("argv") or []))
    print("  limits   " + "  ".join(f"{k} {v}" for k, v in lim.items())
          + "        (as resolved, not as typed)")
    print(f"  code     {head.get('commit') or 'not a git tree'}"
          + ("   ← the tree was dirty, so this run is not reproducible"
             if head.get("dirty") else ""))
    print(f"  span     {head.get('t')} → "
          f"{(end or events[-1]).get('t')}   {_hms(last - first)}"
          f"   {head.get('decks', '?')} deck(s)"
          + ("" if end else "   ← no run_finished record: it was killed, or "
                            "it is still going"))
    if end and end.get("outcome") not in (None, "ok"):
        print(f"  ended    {end['outcome']}")


def _history_clock(events: list, head: dict):
    """Where the wall clock went, per stage and per pool."""
    from collections import defaultdict

    runs = [r for r in _stage_runs(events) if r["ms"]]
    if not runs:
        return
    by_stage = defaultdict(list)
    for r in runs:
        by_stage[r["stage"]].append(r)
    first, last = _run_span(events)
    wall = max(last - first, 1e-9)

    print("\nwhere the wall clock went")
    print(f"  {'stage':<14}{'runs':>5}{'busy':>10}{'median':>9}   longest")
    order = pl.STAGES + ["repair"]
    import statistics
    for stage in sorted(by_stage, key=lambda s: order.index(s)
                        if s in order else 99):
        rs = sorted(by_stage[stage], key=lambda r: -r["ms"])
        total = sum(r["ms"] for r in rs) / 1000
        mid = statistics.median(r["ms"] for r in rs) / 1000
        print(f"  {stage:<14}{len(rs):>5}{_hms(total):>10}{_hms(mid):>9}   "
              f"{rs[0]['deck']} {_hms(rs[0]['ms'] / 1000)}")

    # The number the argv could not answer: busy time over the capacity that
    # was actually available.  Both halves come from this file — the durations
    # from the events, the divisor from the header's *resolved* limits.
    lim = head.get("limits") or {}
    if wall < MIN_WALL_FOR_UTILISATION:
        print(f"  (too short a span — {_hms(wall)} — to divide work by)")
        return
    for name, key, want in (("agent", "agent_workers", True),
                            ("cpu", "cpu_workers", False)):
        slots = lim.get(key)
        if not slots:
            continue
        busy = sum(r["ms"] for r in runs
                   if (r["stage"] in _AGENT_WORK) is want) / 1000
        used = busy / (wall * slots)
        print(f"  {name} pool: {_hms(busy)} of work over {_hms(wall)} × "
              f"{slots} slot(s) = {used:.0%} used"
              # A figure over 100% is not a busy pool, it is a broken sum: more
              # work than the slots could have held.  Saying so beats printing
              # it — the last reader of a number like this reported 328% and
              # believed it.
              + ("   ← impossible: more work than the slots could hold, so "
                 "this log is missing time" if used > 1.05 else ""))


def _history_decks(events: list):
    """One line per deck: how far it got, how long it took, how it ended."""
    from collections import defaultdict

    runs = _stage_runs(events)
    per = defaultdict(list)
    for r in runs:
        if r["deck"]:
            per[r["deck"]].append(r)
    sent = defaultdict(int)
    for e in events:
        if e.get("event") == "sent_back":
            sent[e.get("deck")] += 1
    skips = defaultdict(int)
    for e in events:
        if e.get("event") == "stage_skipped":
            skips[e.get("deck")] += 1
    if not per and not skips:
        return
    print("\nwhat happened to each deck")
    for deck in sorted(set(per) | set(skips)):
        rs = per.get(deck, [])
        busy = sum(r["ms"] for r in rs) / 1000
        last = rs[-1] if rs else None
        ending = (f"{last['stage']} {last['status']}" if last
                  else "nothing to do")
        loop = f"  ↺{sent[deck]} back" if sent.get(deck) else ""
        print(f"  {deck:<10}{len(rs):>3} stage(s){skips[deck]:>4} skipped"
              f"{_hms(busy):>9}   {ending}{loop}")


#: Statuses whose appearance in the stream is the answer to "what went wrong".
NOTABLE = ("rejected", "failed", "infra", "needs_human", "crashed")
LOOP_MAX = 40
#: Below this many seconds of span, a utilisation figure is a division by
#: rounding error rather than a measurement.
MIN_WALL_FOR_UTILISATION = 5.0


def _history_loop(events: list):
    """The loop, in the order it happened."""
    lines = []
    for e in events:
        kind, deck = e.get("event"), e.get("deck")
        if kind == "sent_back":
            lines.append(f"  {e.get('t', '')[11:]}  {deck:<10}"
                         f"{e.get('source')} → repair {e.get('attempt')} → "
                         f"{', '.join(e.get('to') or ['?'])}")
        elif kind == "stage_retried":
            lines.append(f"  {e.get('t', '')[11:]}  {deck:<10}"
                         f"{e.get('stage')} retried after {e.get('kind')} "
                         f"(waited {e.get('backoff_s')}s) — "
                         f"{str(e.get('why') or '')[:60]}")
        elif kind == "stage_finished" and e.get("status") in NOTABLE:
            why = (e.get("error") or e.get("reason") or e.get("verdict")
                   or (e.get("problems") or [""])[0])
            lines.append(f"  {e.get('t', '')[11:]}  {deck:<10}"
                         f"{e.get('stage')} {e.get('status')}"
                         + (f" — {str(why)[:80]}" if why else ""))
    if not lines:
        return
    print(f"\nthe loop ({len(lines)} event(s))")
    for line in lines[:LOOP_MAX]:
        print(line)
    if len(lines) > LOOP_MAX:
        print(f"  … and {len(lines) - LOOP_MAX} more — read {pl.RUN_EVENTS} "
              f"itself for all of them")


def _history_skips(events: list):
    """What the run did not do, which on a resumed run is most of it."""
    from collections import Counter

    by = Counter((e.get("stage"), e.get("why")) for e in events
                 if e.get("event") == "stage_skipped")
    if not by:
        return
    print(f"\nnothing to do ({sum(by.values())} skip(s))")
    for (stage, why), n in sorted(by.items(),
                                  key=lambda kv: (-kv[1], str(kv[0]))):
        print(f"  {str(stage):<14}{str(why):<20}{n:>4}")


def _at_moment(events: list, when: str):
    """Who was in which stage at a given time of day.

    The question every timing in the post-mortem needed and no artefact could
    answer: the console log had neither timestamps nor stage names, so it had
    to come from the observer's samples or from file mtimes.
    """
    want = when.strip()
    open_now: dict[str, tuple] = {}
    seen = None
    for e in events:
        t = (e.get("t") or "")[11:]
        if t and t > want:
            break
        seen = e.get("t")
        if e.get("event") == "stage_started":
            open_now[e.get("deck")] = (e.get("stage"), t)
        elif e.get("event") == "stage_finished":
            open_now.pop(e.get("deck"), None)
    print(f"\nat {want} (last record {seen})")
    if not open_now:
        print("  nothing was running")
    for deck, (stage, since) in sorted(open_now.items()):
        print(f"  {deck:<10}{stage:<14}since {since}")


def _resolve_run(args) -> Path | None:
    work = Path(args.work)
    if getattr(args, "run", None):
        for cand in (Path(args.run), work / pl.RUNS / args.run):
            if cand.exists():
                return cand if cand.is_file() else cand / pl.RUN_EVENTS
        return None
    latest = pl.latest_run(work)
    return latest / pl.RUN_EVENTS if latest else None


def cmd_history(args):
    """Render one run's event stream."""
    work = Path(args.work)
    runs = pl.run_dirs(work)
    if getattr(args, "list", False):
        if not runs:
            print(f"no runs recorded under {work / pl.RUNS}")
        for p in runs:
            head = next(iter(pl.read_events(p)[:1]), {})
            print(f"  {p.name:<24}{head.get('t', '?'):<21}"
                  + " ".join((head.get("argv") or [])[1:6]))
        return

    path = _resolve_run(args)
    if path is None:
        print(f"no run log under {work / pl.RUNS} — every `pptxgym run` or "
              f"single-stage command writes one from now on")
        return
    events = pl.read_events(path)
    if not events:
        print(f"{path} is empty")
        return
    head = events[0] if events[0].get("event") == "run_started" else {}
    _history_header(head, events, path)
    if getattr(args, "at", None):
        _at_moment(events, args.at)
        return
    _history_clock(events, head)
    _history_decks(events)
    _history_loop(events)
    _history_skips(events)


# --------------------------------------------------------------------------- #


def build_parser():
    ap = argparse.ArgumentParser(prog="pptxgym", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default=str(DEFAULT_WORK),
                    help="working directory (default: ./work)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="register source decks")
    p.add_argument("paths", nargs="+", help=".pptx files or directories")
    p.set_defaults(func=cmd_ingest)

    def deck_arg(q):
        q.add_argument("--deck", nargs="*", help="deck ids (default: all)")
        # `--workers` keeps its name because that is what everyone types, but
        # it now means "agent stages at once" — the limit that actually binds.
        q.add_argument("--workers", "--agent-workers", type=int, default=1,
                       dest="workers",
                       help="agent stages in parallel (default: 1). These "
                            "spend API capacity and are ~85%% of the wall "
                            "clock")
        q.add_argument("--cpu-workers", type=int, default=None,
                       help=f"soffice/render stages in parallel "
                            f"(default: cores/4 = {_default_cpu_workers()})")

    def model_args(q):
        """Which model runs this stage, and how hard it thinks.

        Defaults are `None` on both, which is exactly today's behaviour: no
        `--model`, no `--effort`, `claude` decides.  Nothing here invents an
        assignment — all five agent stages are judgement (the mechanical work
        has no model in it at all), so which of them is worth the strong model
        is a question for the pilot, one variable at a time.
        """
        q.add_argument("--model", default=None,
                       help="`opus` for every agent stage, or per stage: "
                            "`propose=opus,recipe=sonnet` (stages: "
                            + ", ".join(agentmod.ROLES) + ")")
        q.add_argument("--effort", default=None,
                       help="`claude --effort`: " + ", ".join(agentmod.EFFORTS)
                            + ". Bare or per stage, like --model")
        q.add_argument("--fallback-model", default=None, dest="fallback_model",
                       help="model to fall back to when the primary is "
                            "overloaded. Safe only because the log says which "
                            "model actually produced the work — that is "
                            "recorded per stage as `model_ran`, and a deck "
                            "quietly finished on the weaker one is visible "
                            "afterwards rather than indistinguishable")

    def retry_arg(q):
        """The API budget, on every command that launches an agent.

        A rate limit is per account, so it is the one limit more machines
        cannot buy around; on a multi-hour batch it is not an if.  Only an
        `api_error` / `auth_error` is retried — never a timeout, never a
        `max_turns` stop, both of which are the agent reaching a real ceiling
        and would cost the budget twice for the same answer.
        """
        q.add_argument("--api-retries", type=int,
                       default=agentmod.API_RETRIES, dest="api_retries",
                       help=f"retries when the API itself fails — a 429 or a "
                            f"403, never a timeout or a truncated answer "
                            f"(default: {agentmod.API_RETRIES}; backoff 30s, "
                            f"60s, 90s …, capped at {agentmod.BACKOFF_CAP}s. "
                            f"An expired login is retried "
                            f"{agentmod.AUTH_RETRIES}× whatever this says)")

    common = dict(deck_arg=deck_arg)

    def render_args(q):
        q.add_argument("--dpi", type=int, default=None,
                       help=f"render DPI (default: per deck — "
                            f"{pl.RENDER_DPI_COARSE}, or {pl.RENDER_DPI} where "
                            f"the deck's own text is under "
                            f"{pl.SMALL_TEXT_PT:g}pt)")
        q.add_argument("--roundtrip", action="store_true",
                       help="also round-trip through LibreOffice (a second "
                            "soffice conversion per deck; a corpus-fragility "
                            "signal only — WPS is what bounds position work)")

    p = sub.add_parser("inspect", help="digest + renders (deterministic)")
    common["deck_arg"](p)
    render_args(p)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("wps", help="measure the WPS round trip (GUI)")
    p.add_argument("--deck", nargs="*", help="deck ids (default: all)")
    p.add_argument("--workers", type=int, default=1,
                   help="decks at once, one virtual display and ~660MB each "
                        "(default: 1)")
    p.add_argument("--sample", type=int, default=None,
                   help="measure this many decks, spread across the batch, "
                        "instead of all of them")
    p.add_argument("--force", action="store_true",
                   help="re-measure decks that already have a number")
    p.set_defaults(func=cmd_wps)

    p = sub.add_parser("propose", help="agent: which tasks this deck should yield")
    common["deck_arg"](p)
    p.add_argument("--force", action="store_true")
    model_args(p)
    p.add_argument("--timeout", type=int, default=30, help="minutes")
    retry_arg(p)
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("recipe", help="agent: proposal -> executable recipe")
    common["deck_arg"](p)
    p.add_argument("--force", action="store_true")
    model_args(p)
    p.add_argument("--timeout", type=int, default=40, help="minutes")
    retry_arg(p)
    p.set_defaults(func=cmd_recipe)

    p = sub.add_parser("degrade", help="apply the recipe + package gate")
    common["deck_arg"](p)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_degrade)

    p = sub.add_parser("materialise", help="produce the assets the task promises")
    common["deck_arg"](p)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_materialise)

    p = sub.add_parser("reconcile", help="agent: does the file still match the instruction")
    common["deck_arg"](p)
    p.add_argument("--force", action="store_true")
    model_args(p)
    p.add_argument("--timeout", type=int, default=30, help="minutes")
    retry_arg(p)
    p.set_defaults(func=cmd_reconcile)

    def harden_args(q):
        q.add_argument("--attack-workers", type=int, default=4,
                       help="candidate decks built at once, within one deck's "
                            "battery (default: 4)")
        q.add_argument("--wps-workers", type=int, default=2,
                       help="WPS round trips at once, one virtual display and "
                            "~660MB each (default: 2)")
        q.add_argument("--no-wps", action="store_true",
                       help="skip the round-trip attack. It then counts as an "
                            "unproven gate and rejects the task, which is the "
                            "honest reading — a gate nobody fired is not a gate")
        q.add_argument("--keep-candidates", action="store_true",
                       help="keep the attack decks under work/<deck>/attacks/ "
                            "(one full copy of the input per attack)")

    def package_args(q):
        q.add_argument("--out", default=None,
                       help="where the runnable tasks go (default: "
                            "<work>/emitted)")

    p = sub.add_parser("score", help="derive the reward from delta.json and "
                                     "calibrate it")
    common["deck_arg"](p)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("harden", help="try to cheat the task; reject it if that "
                                      "works")
    common["deck_arg"](p)
    harden_args(p)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_harden)

    p = sub.add_parser("package", help="consistency gate, then write the "
                                       "runnable task")
    common["deck_arg"](p)
    package_args(p)
    p.add_argument("--task-id", default=None,
                   help="override the content-derived id (one deck only)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("run", help="all stages, resuming what is already done")
    common["deck_arg"](p)
    harden_args(p)
    package_args(p)
    # The default used to be `degraded`, four stages short of a finished task,
    # so a batch left every deck parked halfway with nothing saying why. `run`
    # means run.
    p.add_argument("--until", default=pl.STAGES[-1], choices=pl.STAGES,
                   help=f"stop after this stage (default: {pl.STAGES[-1]})")
    p.add_argument("--deck-deadline", default=f"{DEADLINE_FACTOR:g}x",
                   dest="deck_deadline", metavar="3x|90m|off",
                   help=f"how much *working* time one deck may spend before it "
                        f"is parked: a multiple of the median finished deck "
                        f"(default {DEADLINE_FACTOR:g}x, never under "
                        f"{DEADLINE_FLOOR_MIN:g}m and never a median of fewer "
                        f"than {DEADLINE_MIN_SAMPLES} decks), a flat number of "
                        f"minutes, or `off`. The wall clock of a wide run is "
                        f"its slowest deck: eight of ten finished in 23 min "
                        f"and two ran 67 min longer for nothing")
    render_args(p)
    model_args(p)
    p.add_argument("--timeout", type=int, default=40, help="minutes per agent")
    retry_arg(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("solvable", help="agent: can this task actually be done")
    common["deck_arg"](p)
    p.add_argument("--force", action="store_true")
    model_args(p)
    p.add_argument("--timeout", type=int, default=30, help="minutes")
    retry_arg(p)
    p.set_defaults(func=cmd_solvable)

    p = sub.add_parser("repair", help="agent: fix a deck a gate rejected")
    common["deck_arg"](p)
    p.add_argument("--force", action="store_true")
    model_args(p)
    p.add_argument("--timeout", type=int, default=30, help="minutes")
    retry_arg(p)
    p.set_defaults(func=cmd_repair)

    p = sub.add_parser("status", help="stage table")
    common["deck_arg"](p)
    p.add_argument("--all", action="store_true",
                   help=f"full table even beyond {BIG_BATCH} decks")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("history", help="what one run actually did, from its "
                                       "event stream")
    p.add_argument("run", nargs="?", default=None,
                   help=f"run id, or a path to an {pl.RUN_EVENTS} "
                        f"(default: the most recent run)")
    p.add_argument("--list", action="store_true", help="every recorded run")
    p.add_argument("--at", default=None, metavar="HH:MM:SS",
                   help="who was in which stage at that moment")
    p.set_defaults(func=cmd_history)
    return ap


#: Commands that move decks, and therefore have a run worth recording.
#: `status`, `history` and `ingest` are not among them: the first two only
#: read, and ingestion writes `rejects.jsonl`, which is the same idea for the
#: one stage that meets the corpus rather than the pipeline.
LOGGED_COMMANDS = {"run", "inspect", "propose", "recipe", "degrade",
                   "materialise", "reconcile", "solvable", "score", "harden",
                   "package", "repair"}


def _start_run_log(args, argv):
    """Open the run's event stream, and say where it is.

    Printed rather than merely opened, because the first thing anyone does
    with a batch that will take ninety minutes is put it under `nohup` and
    walk away — and then the question is which file to tail.
    """
    if getattr(args, "cmd", None) not in LOGGED_COMMANDS:
        return None
    try:
        log = pl.open_run(Path(args.work), argv=["pptxgym", *argv],
                          cmd=args.cmd, limits=resolved_limits(args),
                          decks=len(_decks(args)))
    except OSError as e:                                         # noqa: BLE001
        print(f"(no run log: {e})")
        return None
    print(f"run {log.run_id} — {log.path}")
    return log


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    try:
        # a mistyped stage or effort level is refused here rather than three
        # decks into a batch, and long before `claude` silently ignores it
        agentmod.Assignment.from_args(args)
    except ValueError as e:
        sys.exit(f"pptxgym: {e}")
    # A redirected stdout is block-buffered, so `nohup pptxgym run … > log &`
    # left that log at zero bytes for twenty minutes while the run was very
    # much happening.  The run's own event stream is flushed per record for the
    # same reason; this is the console saying the same thing.
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)
    _start_run_log(args, argv)
    outcome = "ok"
    try:
        args.func(args)
    except KeyboardInterrupt:
        outcome = "interrupted"
        sys.exit(130)
    except Exception as e:                                       # noqa: BLE001
        outcome = f"{type(e).__name__}: {e}"[:200]
        raise
    finally:
        pl.close_run(outcome=outcome)


if __name__ == "__main__":
    main()
