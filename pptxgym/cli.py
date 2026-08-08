"""pptxgym — turn a folder of real .pptx decks into computer-use RL tasks.

    pptxgym ingest  corpus/
    pptxgym propose --deck deck0001 --workers 6
    pptxgym status

Every subcommand is one stage and is run on its own.  What sequences them is
`pptxgym.foreman`, which spawns one orchestrator agent per deck; the agent
decides what to run next, what a verdict means and when to stop.  This module
owns the verbs and nothing above them — there is no stage driver here, no
repair loop and no rework routing, because judging a deck is the
orchestrator's job and re-running a stage is one more verb it can call.

Concurrency is measured in two currencies, not one.  Four of the stages spend
API capacity on a `claude -p` subprocess; the other six spend CPU on soffice,
rendering, and — after the last agent — deriving the reward, attacking it and
writing the task out.  Timed over ten decks, the agent stages take ~85% of the
wall clock
(reconcile median 6.0 min, solvable 7.5; degrade 2.3, materialise 0.1), so a
single limit either starves the renderers or oversubscribes the API — we have
seen both.  Each stage takes a slot from its own pool and gives it back when
it finishes.

Both pools are honoured by every command that walks more than one deck, and
the thread pool underneath them is sized from the same two numbers: a stage is
a blocking call that owns its thread for minutes, so the default executor's
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
from . import escalate
from . import mailbox
from . import pipeline as pl
from . import profiles

DEFAULT_WORK = Path("work")

# the stages that spend API capacity rather than CPU
_AGENT_WORK = set(pl.AGENT_STAGES)

# a few threads above what the pools can hand out, so the executor is never
# the thing that runs out first
_THREAD_HEADROOM = 4


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
           "threads": _executor_size(agent, cpu)}
    for flag, key in (("api_retries", "api_retries"), ("timeout", "timeout_min"),
                      ("attack_workers", "attack_workers"),
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
    if workers == 1 or len(decks) == 1:
        for deck in decks:
            if stage is not None:
                deck.begin(stage)
            print("  " + _guarded(fn, deck, args))
        return

    async def main():
        loop = asyncio.get_running_loop()
        # one mechanism, so anything added to `Pools` — a third resource, a
        # global ceiling — applies to every command that walks more than one
        # deck rather than to some of them.
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


def _upstream(deck, up: str, plain: str) -> str:
    """Why the stage above is not ready — `plain` when it simply never ran.

    "skipped — not proposed" is true and useless when `proposed` is sitting
    there recorded `ok`: the reader believes the pipeline is confused and
    reaches for `--force`, which re-runs *this* stage and cannot touch the
    upstream that is blocking it.  deck0004 spent its last minutes in exactly
    that loop, and the fact it needed — its proposal was recorded by hand, so
    the record carries no input fingerprints and reads as stale for ever —
    was in `state.json` all along and in no message anywhere.
    """
    rec = deck.state().get(up) or {}
    status = rec.get("status")
    if status is None:
        return plain
    moved = deck.stale(up)
    if moved:
        hand = "" if (rec.get("_in") or {}) else (
            " — the record has no input fingerprints, so it was written by "
            "hand rather than by a run, and no rerun of this stage can clear "
            "it; the stage above has to actually run")
        return f"skipped — {up} is stale ({', '.join(moved)}){hand}"
    return f"skipped — {up} is {status}"


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


def _api_retries(args) -> int | None:
    """The retry budget for one agent stage, or None to leave it to the lane.

    None is the default and it matters.  `AgentRun` picks the budget from its
    engine — a private account gets three tries, a lane sharing its quota with
    somebody else's live rollouts gets eight and waits up to ten minutes
    between them — and this used to overwrite that with the flag's default
    unconditionally.  Every specialist on the shared lane was therefore back
    on three tries with the patience fix applied only to the orchestrator
    above it, which is what the run's own `limits` records show.  A flag that
    was never passed should not out-vote what the lane knows about itself.
    """
    n = getattr(args, "api_retries", None)
    return None if n is None else max(0, int(n))


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
            # Take the previous answer off the desk before asking again.
            #
            # `archive_attempt` says "Move a stage's artefacts into attempts/"
            # and copies them, so the old file was still sitting there when the
            # agent started. An agent asked to write a recipe, finding a
            # complete and valid `recipe.json` already present, reasonably
            # leaves it alone — and `_left_over` then reads the unchanged mtime
            # as "the agent wrote nothing" and fails the deck. Four times in
            # run 11, costing deck0003 and deck0007, and only ever when a stage
            # is asked again, because only then is there a good answer already
            # on disk to leave alone.
            #
            # Removing it also removes an anchor: a second opinion written on
            # top of the first is not a second opinion. The bytes are safe in
            # `attempts/`, which is where the next reader is pointed.
            if kept:
                for out in spec.outputs:
                    Path(out).unlink(missing_ok=True)
            # The retries happen inside the pool slot this stage is holding,
            # deliberately.  Handing the slot back so another deck can take it
            # is exactly wrong when the thing that failed is a per-account rate
            # limit: it would replace one waiting deck with one more caller.
            if (budget := _api_retries(args)) is not None:
                spec.api_retries = budget
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
                # leave the stage unjudged so a re-run picks it up
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
                # otherwise be read as this attempt's verdict by whoever looks
                # next.
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
            # the log goes on the passing mark too, not only the failing
            # ones: which specialist produced an accepted artefact is the
            # first question asked of a deck somebody doubts
            deck.mark(stage, "rejected" if sent_back else "ok",
                      log=res.get("log"), **{**detail, **record})
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
                     _upstream(deck, "inspected", "skipped — not inspected"))
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
        return _skip(deck, "recipe", SKIP_UPSTREAM,
                     _upstream(deck, "proposed", "skipped — not proposed"))
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
        return _skip(deck, "degraded", SKIP_UPSTREAM,
                     _upstream(deck, "recipe", "skipped — no recipe"))
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
                     _upstream(deck, "degraded", "skipped — not degraded"))
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
                     _upstream(deck, "materialised",
                               "skipped — assets not materialised"))
    redo = _redo_note(deck, "reconciled", args)
    out = _agent_stage(
        deck, "reconciled",
        # 40, down from 60: the three trial decks' reconcilers ran 16-20
        # minutes each, and the machine consistency check already covers the
        # decidable half of their manual. The turn cap is the honest lever —
        # it bounds breadth without deleting the judgement the stage is for.
        lambda d: agentmod.AgentRun("reconciler", agentmod.reconcile_prompt(d),
                                    max_turns=40,
                                    outputs=[d.root / "task.json"]),
        pl.check_reconcile, args)
    return f"{deck.id}  {out}{redo}"


def cmd_reconcile(args):
    _each(args, _reconcile_one, "reconciled")


# --------------------------------------------------------------------------- #
# adopting your own work — the fast profile's one concession
# --------------------------------------------------------------------------- #


ADOPT_CHECKERS = {"proposed": ("proposal.json", "check_proposal"),
                  "recipe": ("recipe.json", "check_recipe"),
                  "reconciled": ("task.json", "check_reconcile")}


def _adopt_one(deck, args):
    """Record an artefact the orchestrator wrote itself — checked, and said.

    There was no such verb, and the manual told the orchestrator it could
    write a proposal by hand anyway.  Both halves of that were wrong.  A
    record written straight into `state.json` carries no input fingerprints,
    so `status_of` reads it as stale for ever and every stage below it
    refuses to run — the permission was not usable by the agent it was given
    to.  And a record that does not say who wrote it reads exactly like a
    specialist's.

    So the concession is made properly or not at all: the same checker runs,
    `mark` stamps the fingerprints, and the record says `adopted` and under
    which profile.  A `full`-profile deck cannot adopt at all.
    """
    prof = profiles.profile(args)
    if prof != profiles.FAST:
        raise pl.StageError(
            f"{deck.id}: adopt is a {profiles.FAST}-profile verb and this run "
            f"is {prof}. Under {profiles.FULL} every judgement stage is a "
            f"specialist's to write; if one cannot run, wait for it, fix its "
            f"input, or park the deck.")
    stage = args.stage
    if stage not in profiles.ADOPTABLE:
        raise pl.StageError(
            f"{deck.id}: {stage!r} cannot be adopted. Adoptable: "
            f"{', '.join(profiles.ADOPTABLE)}. The solvability probe is "
            f"sealed on purpose — a witness cannot be replaced by the deck "
            f"it is a witness about.")
    # Provenance, not freshness, decides this. deck0004's specialist had
    # *succeeded* four minutes earlier — four degradations, 397 seconds — and
    # the owner wrote three over the top of it. Keying the refusal on
    # `done()` would have let that through, because the record it replaced
    # was by then stale on a technicality; what makes it wrong is that
    # somebody else looked and this record is theirs.
    rec = deck.state().get(stage) or {}
    ran = bool(rec.get("log")) or "model_asked" in rec
    if ran and not profiles.adopted(rec) and not args.force:
        return (f"{deck.id}  refused — {stage} already stands on a "
                f"specialist's run, which is not yours to replace "
                f"(--force if you mean it)")
    artefact, checker = ADOPT_CHECKERS[stage]
    try:
        detail = getattr(pl, checker)(deck)
    except pl.StageError as e:
        # Caught here, exactly as every other stage catches its own checker.
        # Letting it out lands in `_guarded`, which records the crash under
        # the literal key "stage" — deck0003's real state.json grew a
        # `stage: crashed` entry saying "says easy but 120 steps is medium",
        # which is a checker doing its job dressed up as the pipeline falling
        # over. The deck sees a rejection it can act on; the run log sees a
        # rejection it can count.
        deck.mark(stage, "rejected", adopted=True, profile=profiles.FAST,
                  by="orchestrator", artefact=artefact, error=str(e))
        return f"{deck.id}  REJECTED — {e}"
    deck.mark(stage, "ok", adopted=True, profile=profiles.FAST,
              by="orchestrator", artefact=artefact, **detail)
    return f"{deck.id}  adopted {stage} ({artefact}) — {checker} passed"


def cmd_adopt(args):
    _each(args, _adopt_one, "adopted")


# --------------------------------------------------------------------------- #
# the three deterministic stages after the last agent
#
# They cost CPU and not API capacity — `_workers_for` puts anything outside
# `_AGENT_WORK` on the cpu pool, so they need no special case there — and every
# one of them can come back `rejected`, which is a verdict for the deck's owner
# to act on exactly as a rejected reconcile is.
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
                     _upstream(deck, "solvable",
                               "skipped — not through the solvability gate"))
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
                     _upstream(deck, "scored",
                               "skipped — no accepted scoring plan to attack"))
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
                     _upstream(deck, "hardened",
                               "skipped — not through the attack battery"))
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
                     _upstream(deck, "reconciled", "skipped — not reconciled"))
    t = json.loads((deck.root / "task.json").read_text())
    if t.get("verdict") == "needs_rework":
        return _skip(deck, "solvable", SKIP_UPSTREAM,
                     "skipped — reconcile rejected it first")
    # rebuilt every time: the probe judges the files as they stand now, and a
    # bundle left over from an earlier attempt would have it judging the old
    # task
    pl.bundle(deck)
    redo = _redo_note(deck, "solvable", args)
    # The probe does not run here.  It runs in a copy of the bundle under the
    # system temp directory, inside a namespace where `work/` and the corpus
    # are empty — so the answer key is not something it is asked to leave
    # alone, it is something that is not there.  See `pl.probe_workspace`.
    #
    # And it runs on claude whatever lane the deck is on, for two reasons
    # that are really one: the deny-rules half of the barrier is claude
    # `settings.json` vocabulary that codex does not read (a codex probe on
    # a machine without the kernel mask would face only the log scan), and
    # the probe is meant to approximate the policy the tasks will train —
    # haiku — not the strongest model the lane happens to carry.  A --model
    # flag still wins; the lane's engine never does.
    try:
        with pl.probe_workspace(deck) as ws:
            out = _agent_stage(
                deck, "solvable",
                lambda d: agentmod.AgentRun(
                    "solver-probe", agentmod.solvability_prompt(d, ws),
                    cwd=ws.dir, max_turns=50, engine="claude", model="haiku",
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


def cmd_mail(args):
    """Act on the supervising side's reply, if there is one to act on.

    Exit code is the interesting output, because the caller is a shell loop
    around the run:

      0  nothing to do — no reply, or none this run has not already applied
      3  a fix arrived: the commit is on stdout, check it out and run again

    Everything the reply says other than "fixed" is applied here and now,
    because it needs no new code: a `wontfix` or a `not-ours` parks the decks
    with the reason so their remaining attempts are not spent on something the
    frontend has already looked at and decided about.
    """
    work = Path(args.work)
    replies = mailbox.read(args.file or (work / mailbox.FILENAME))
    if not replies:
        print("no reply to act on")
        return 0
    fresh = mailbox.unapplied(replies, work / mailbox.APPLIED)
    if not fresh:
        print(f"{len(replies)} reply/replies, all already applied")
        return 0

    escalations = escalate.collect(work)
    fix = None
    for reply in fresh:
        decks = mailbox.targets(reply, escalations)
        print(f"  {reply['verdict']:9s} {reply['signature'] or ','.join(decks)}"
              f"  -> {len(decks)} deck(s)"
              + (f"  {reply['note'][:80]}" if reply["note"] else ""))
        if reply["verdict"] == "fixed":
            # Not applied here: a running process has already imported its
            # modules, so the only way to pick up a fix is to be restarted
            # against it. The caller does that, and the deck's owner picks
            # the work back up against the fixed code — every stage
            # fingerprints the code that produced it, so `status` shows which
            # verdicts the fix invalidated.
            fix = fix or reply["commit"]
            continue
        for deck_id in decks:
            deck = pl.Deck(work / deck_id)
            if not deck.root.exists():
                continue
            if reply["verdict"] == "not-ours":
                # Stop protecting it: drop the escalation so the normal gates
                # finish the job.
                (deck.root / escalate.FILENAME).unlink(missing_ok=True)
                print(f"    {deck_id}  escalation withdrawn")
            else:                       # wontfix | stop
                deck.mark("reconciled", "needs_human",
                          reason=f"{reply['verdict']}: {reply['note'][:180]}",
                          answered=reply["signature"] or reply["id"])
                print(f"    {deck_id}  parked ({reply['verdict']})")
    mailbox.mark_applied(fresh, work / mailbox.APPLIED)
    if fix:
        print(fix)
        return 3
    return 0


def cmd_blocked(args):
    """What the run could not fix by itself, grouped by defect.

    Grouped, because the count of decks behind one signature *is* the priority
    order and no per-deck view can show it. Ten decks reporting one missing
    perturbation branch is one thing to check and ten decks to resume; read
    deck by deck it is ten separate investigations of the same bug, which is
    how today's runs were read and why it took three of them.

    The two sources are printed apart and labelled, because they are not the
    same kind of statement. A gate reports a mechanical fact — nothing about a
    deck decides whether `PERTURB` has an entry for an operator. An agent
    reports a belief, held by something for which "the pipeline is broken" is
    the answer that ends work it cannot finish.
    """
    work = Path(args.work)
    groups = escalate.group(escalate.collect(work))
    if args.json:
        print(json.dumps(groups, ensure_ascii=False, indent=1))
        return 0
    if not groups:
        print("nothing escalated — every deck either passed or was refused on "
              "its own merits")
        return 0

    facts = [g for g in groups if g["source"] == "gate"]
    claims = [g for g in groups if g["source"] != "gate"]

    def show(items, heading, note):
        if not items:
            return
        print(f"\n{heading}  ({note})")
        for g in items:
            decks = ", ".join(g["decks"][:8])
            more = f" +{len(g['decks']) - 8}" if len(g["decks"]) > 8 else ""
            print(f"\n  {g['signature']}")
            print(f"    blocking {len(g['decks'])} deck(s): {decks}{more}")
            print(f"    {str(g.get('detail') or '')[:220]}")

    show(facts, "FOUND BY THE PIPELINE IN ITSELF",
         "mechanical; a deck cannot cause these")
    show(claims, "CLAIMED BY A REPAIR AGENT",
         "unverified; check before believing")
    blocked = sum(len(g["decks"]) for g in groups)
    print(f"\n{len(groups)} defect(s) blocking {blocked} deck(s). "
          f"Fixing one resumes every deck behind it.")
    return 0


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

    # A deck a gate sent back is not "still going": it is stopped, and what to
    # do about it belongs to whoever owns the deck.  Named here rather than
    # left as a `↺` in one cell of a wide table — four of these once sat
    # rejected with nobody looking at them, and the table said nothing.
    sent_back = [d.id for d in decks
                 if any(isinstance(v, dict) and v.get("status") == "rejected"
                        for v in d.state().values())
                 and not d.done(pl.STAGES[-1])]
    if sent_back:
        print(f"\n{len(sent_back)} deck(s) carry a gate's `no`: "
              f"{' '.join(sent_back[:12])}"
              f"{' …' if len(sent_back) > 12 else ''}")

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
        for stage in pl.STAGES:
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
              f"clock went and what went wrong")


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
    stage marked without a `begin` — ingest, or a stage recorded by hand —
    falls back to the gap between its start and finish records.
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
    order = pl.STAGES
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
        print(f"  {deck:<10}{len(rs):>3} stage(s){skips[deck]:>4} skipped"
              f"{_hms(busy):>9}   {ending}")


#: Statuses whose appearance in the stream is the answer to "what went wrong".
NOTABLE = ("rejected", "failed", "infra", "needs_human", "crashed")
LOOP_MAX = 40
#: Below this many seconds of span, a utilisation figure is a division by
#: rounding error rather than a measurement.
MIN_WALL_FOR_UTILISATION = 5.0


def _history_loop(events: list):
    """What went wrong, in the order it happened."""
    lines = []
    for e in events:
        kind, deck = e.get("event"), e.get("deck")
        if kind == "stage_retried":
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
    print(f"\nwhat went wrong ({len(lines)} event(s))")
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
                       default=None, dest="api_retries",
                       help=f"retries when the API itself fails — a 429 or a "
                            f"403, never a timeout or a truncated answer "
                            f"(default: whatever the lane calls for — "
                            f"{agentmod.API_RETRIES} on a private account, "
                            f"{agentmod.SHARED_RETRIES} on one sharing its "
                            f"quota. An expired login is retried "
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

    p = sub.add_parser(
        "adopt",
        help="fast profile: record an artefact you wrote yourself, checked")
    common["deck_arg"](p)
    p.add_argument("--stage", required=True, choices=list(profiles.ADOPTABLE),
                   help="which stage's artefact you are adopting")
    p.add_argument("--force", action="store_true",
                   help="adopt over a record a specialist produced. Rarely "
                        "right: the specialist looked, and you did not")
    p.add_argument("--profile", choices=list(profiles.PROFILES), default=None,
                   help=f"default: ${profiles.PROFILE_ENV} or "
                        f"{profiles.FULL}")
    p.set_defaults(func=cmd_adopt)

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

    p = sub.add_parser("solvable", help="agent: can this task actually be done")
    common["deck_arg"](p)
    p.add_argument("--force", action="store_true")
    model_args(p)
    p.add_argument("--timeout", type=int, default=30, help="minutes")
    retry_arg(p)
    p.set_defaults(func=cmd_solvable)

    p = sub.add_parser("status", help="stage table")
    common["deck_arg"](p)
    p.add_argument("--all", action="store_true",
                   help=f"full table even beyond {BIG_BATCH} decks")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("mail", help="act on the supervising side's reply")
    p.add_argument("--file", default=None,
                   help=f"where the reply is (default: <work>/{mailbox.FILENAME})")
    p.set_defaults(func=cmd_mail)

    p = sub.add_parser("blocked", help="defects the run could not fix itself, "
                                       "grouped by defect rather than by deck")
    p.add_argument("--json", action="store_true",
                   help="machine-readable, for the supervising side")
    p.set_defaults(func=cmd_blocked)

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
LOGGED_COMMANDS = {"inspect", "propose", "recipe", "degrade",
                   "materialise", "reconcile", "adopt", "solvable",
                   "score", "harden", "package"}


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
    code = 0
    try:
        # A command's return value is its exit code. `mail` uses it to tell a
        # shell loop that a fix arrived and the run has to be restarted against
        # it, which is not something a caller should have to learn by parsing
        # prose. `None` from every other command still means 0.
        code = args.func(args) or 0
    except KeyboardInterrupt:
        outcome = "interrupted"
        sys.exit(130)
    except Exception as e:                                       # noqa: BLE001
        outcome = f"{type(e).__name__}: {e}"[:200]
        raise
    finally:
        pl.close_run(outcome=outcome)
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
