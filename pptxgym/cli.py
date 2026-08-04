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
                return await loop.run_in_executor(None, _guarded, fn, deck, args)

        with _threads_for(loop, *_pool_sizes(args)):
            for line in await asyncio.gather(*[one(d) for d in decks]):
                print("  " + line)

    asyncio.run(main())


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
        return f"{deck.id}  (already inspected)"
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
    return f"{deck.id}  ({word})"


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


def _agent_stage(deck, stage, spec_builder, checker, args):
    try:
        with pl.lock(deck, stage):
            # keep the previous attempt: the log is opened "w" and the output
            # file is overwritten, so without this a re-run erases the evidence
            # of what the last one decided
            pl.archive_attempt(deck, stage)
            spec = spec_builder(deck)
            asked = _assignment(args).apply(spec, stage)
            spec.timeout_min = args.timeout
            spec.log = deck.root / f"{stage}.jsonl"
            # The retries happen inside the pool slot this stage is holding,
            # deliberately.  Handing the slot back so another deck can take it
            # is exactly wrong when the thing that failed is a per-account rate
            # limit: it would replace one waiting deck with one more caller.
            spec.api_retries = _api_retries(args)
            res = asyncio.run(agentmod.run_agent(spec))
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
        return f"{deck.id}  skipped — not inspected"
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
        return f"{deck.id}  skipped — not proposed"
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
        return f"{deck.id}  (already degraded)"
    if not deck.done("recipe"):
        return f"{deck.id}  skipped — no recipe"
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
        return f"{deck.id}  (already materialised)"
    if not deck.done("degraded"):
        return f"{deck.id}  skipped — not degraded"
    try:
        d = pl.materialise(deck)
        tail = f"  ({d['unmet']} unmet)" if d.get("unmet") else ""
        return (f"{deck.id}  {d['produced']} asset(s): "
                f"{', '.join(d['kinds']) or '—'}{tail}")
    except pl.StageError as e:
        return f"{deck.id}  FAILED — {e}"


def cmd_materialise(args):
    _each(args, _materialise_one, "materialised")


def _reconcile_one(deck, args):
    if (line := _skip_done(deck, "reconciled", args, "already reconciled")):
        return line
    mat = deck.status_of("materialised")
    if mat not in ("ok", "partial"):
        return f"{deck.id}  skipped — assets not materialised"
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
    line = f"{deck.id}  {ok_line(detail)}"
    if detail.get("problems"):
        line += f"   REJECTED — {detail['problems'][0][:150]}"
    return line


def _score_one(deck, args):
    if deck.done("scored") and not args.force:
        return f"{deck.id}  (already scored)"
    if not deck.done("solvable"):
        return f"{deck.id}  skipped — not through the solvability gate"
    return _cpu_gate(
        deck, "scored", pl.score_task,
        lambda d: (f"{d['components']} component(s)  gt={d['gt']:.3f}  "
                   f"input={d['input']:.3f}  weights={d['weights']}"
                   + (f"  ({d['unscoreable']} unscoreable)"
                      if d.get("unscoreable") else "")))


def cmd_score(args):
    _each(args, _score_one, "scored")


def _harden_one(deck, args):
    if deck.done("hardened") and not args.force:
        return f"{deck.id}  (already hardened)"
    if not deck.done("scored"):
        return f"{deck.id}  skipped — no accepted scoring plan to attack"
    return _cpu_gate(
        deck, "hardened",
        lambda d: pl.harden(d, workers=args.attack_workers,
                            wps_workers=args.wps_workers,
                            wps=not args.no_wps, keep=args.keep_candidates),
        lambda d: (f"{d['attacks']} attack(s), {d['variants']} variant(s)"
                   + (f"  beaten by {', '.join(d['beaten'])}"
                      if d.get("beaten") else "")
                   + (f"  credit lost on {', '.join(d['variants_lost'])}"
                      if d.get("variants_lost") else "")))


def cmd_harden(args):
    _each(args, _harden_one, "hardened")


def _package_one(deck, args):
    if deck.done("packaged") and not args.force:
        return f"{deck.id}  (already packaged)"
    if not deck.done("hardened"):
        return f"{deck.id}  skipped — not through the attack battery"
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
            return f"{deck.id}  (already probed; bundle rebuilt — {gaps[0]})"
        return f"{deck.id}  (already probed)"
    if not deck.done("reconciled"):
        return f"{deck.id}  skipped — not reconciled"
    t = json.loads((deck.root / "task.json").read_text())
    if t.get("verdict") == "needs_rework":
        return f"{deck.id}  skipped — reconcile rejected it first"
    # rebuilt every time: the probe judges the files as they stand now, and a
    # bundle left over from before a repair would have it judging the old task
    pl.bundle(deck)
    redo = _redo_note(deck, "solvable", args)
    out = _agent_stage(
        deck, "solvable",
        lambda d: agentmod.AgentRun("solver-probe",
                                    agentmod.solvability_prompt(d),
                                    max_turns=50,
                                    outputs=[d.root / "solvability.json"]),
        pl.check_solvability, args)
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


def _repair_one(deck, args):
    if deck.status_of("reconciled") not in ("ok", "stale", "rejected"):
        return f"{deck.id}  skipped — not reconciled yet"
    rework, source = _rework_of(deck)
    if not rework:
        return f"{deck.id}  nothing to repair"
    done = pl.repairs_done(deck)
    if done >= pl.MAX_REPAIRS:
        deck.mark("reconciled", "needs_human", attempts=done,
                  rejected_by=source, reason=rework[0].get("what", "")[:200])
        return (f"{deck.id}  PARKED after {done} repair attempts — "
                f"needs a human")
    try:
        with pl.lock(deck, "repair"):
            before = pl.tool_tree_state()
            spec = agentmod.AgentRun(
                "orchestrator", agentmod.repair_prompt(deck, rework, source),
                max_turns=60)
            asked = _assignment(args).apply(spec, "repair")
            spec.timeout_min = args.timeout
            spec.api_retries = _api_retries(args)
            spec.log = deck.root / f"repair-{done + 1:02d}.jsonl"
            res = asyncio.run(agentmod.run_agent(spec))
            # a repair fixes one deck; the tools are shared by all of them
            edited = pl.revert_tool_changes(deck, before,
                                            f"repair-{done + 1:02d}")
    except pl.DeckBusy as e:
        return f"{deck.id}  BUSY — {e}"
    if edited:
        deck.mark("reconciled", "needs_human", attempts=done + 1,
                  rejected_by=source,
                  reason=f"the repair edited the shared tools ({edited}); "
                         f"reverted, diff kept beside the log for review")
        return (f"{deck.id}  STOPPED — the repair edited {edited}, which is "
                f"off limits; change reverted, diff kept for review")
    # the repairer is an agent stage under another name, so it is recorded like
    # one: which model, how many attempts, and how it ended.  It was the one
    # agent whose model left no trace at all.
    deck.mark("repair", {"timeout": "failed",
                         "infra": "infra"}.get(res["status"], "ok"),
              attempt=done + 1, rejected_by=source, log=str(spec.log),
              **_limped(res), **_ran(res, asked))
    if res["status"] == "timeout":
        return f"{deck.id}  repair TIMEOUT"
    if res["status"] == "infra":
        # The retries are spent and the repairer never got to work.  Falling
        # through would invalidate the stages below it and retire the verdict
        # that ordered the repair — a whole round of the loop consumed by an
        # outage, and the complaint marked as addressed by nobody.
        return (f"{deck.id}  repair INFRA after {res.get('attempts', 1)} "
                f"attempt(s) — {res['why']}")
    # whatever it touched, the stages below that point are now stale
    stages = {r.get("stage") for r in rework}
    for stg in ("proposed", "recipe", "materialise"):
        if stg in stages:
            pl.invalidate_from(deck, stg)
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


async def _run_one(deck, args, pools):
    """Drive one deck through the stages, honouring what is already done.

    The deck holds no slot of its own.  It takes one from the pool that fits
    the stage about to run and gives it straight back, so a deck queueing for
    the API is not also occupying a renderer, and a deck three repairs deep is
    not occupying anything at all while it waits.
    """
    try:
        await _run_stages(deck, args, pools)
    except Exception:                                            # noqa: BLE001
        print("  " + _record_crash(deck, "run"))


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


async def _run_stages(deck, args, pools):
    loop = asyncio.get_running_loop()

    async def step(stage, fn, ns):
        async with pools.for_stage(stage):
            return await loop.run_in_executor(None, fn, ns)

    for stage in pl.STAGES:
        # a stage whose artefact was made under a different model is not a
        # cache hit — see `_model_changed`.  It re-runs, and the sub-command
        # agrees, because both ask the same question.
        if stage == "ingested" or (deck.promoted(stage)
                                   and not _model_changed(deck, stage, args)):
            continue
        if pl.STAGES.index(stage) > pl.STAGES.index(args.until):
            break
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
                await step("repair", cmd_repair, ns)
                for s2 in pl.STAGES[pl.STAGES.index("recipe"):]:
                    if not deck.promoted(s2) and pl.STAGES.index(s2) <= \
                            pl.STAGES.index(args.until):
                        await step(s2, globals()[STAGE_FN[s2]], ns)
                if deck.state().get("reconciled", {}).get(
                        "status") == "needs_human":
                    break
            if not deck.promoted(stage):
                # out of repairs, or the repair changed nothing.  Carrying on
                # to the next stage would build on a task the gate rejected,
                # which is the one outcome this loop exists to prevent.
                return


def cmd_run(args):
    global _UNDER_RUN
    decks = _decks(args)

    async def main():
        loop = asyncio.get_running_loop()
        pools = _pools_for(args)
        with _threads_for(loop, *_pool_sizes(args)):
            # `return_exceptions` is the second net.  `_run_one` already
            # catches per deck, but a failure in the machinery around it — not
            # in a stage — would otherwise cancel every other deck mid-flight.
            for r in await asyncio.gather(
                    *[_run_one(d, args, pools) for d in decks],
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
                note = (f"{r.get('difficulty')} {r.get('est_steps')}步 · "
                        f"{r.get('assets')} assets"
                        + ("  (指令已改)" if r.get("instruction_changed") else ""))
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
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        # a mistyped stage or effort level is refused here rather than three
        # decks into a batch, and long before `claude` silently ignores it
        agentmod.Assignment.from_args(args)
    except ValueError as e:
        sys.exit(f"pptxgym: {e}")
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
