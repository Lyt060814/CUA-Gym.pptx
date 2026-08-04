"""pptxgym — turn a folder of real .pptx decks into computer-use RL tasks.

    pptxgym ingest  corpus/*.pptx
    pptxgym run     --until solvable --agent-workers 6
    pptxgym status

Every subcommand is one stage and can be run on its own; `run` chains them and
skips whatever is already done, so a failed batch is resumed rather than
restarted.

Concurrency is measured in two currencies, not one.  Four of the stages spend
API capacity on a `claude -p` subprocess; three spend CPU on soffice and
rendering.  Timed over ten decks, the agent stages take ~85% of the wall clock
(reconcile median 6.0 min, solvable 7.5; degrade 2.3, materialise 0.1), so a
single limit either starves the renderers or oversubscribes the API — we have
seen both.  Each stage now takes a slot from its own pool and gives it back
when it finishes, which also means a deck stuck in a repair loop no longer
holds a slot it is not using.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

from . import agent as agentmod
from . import pipeline as pl

DEFAULT_WORK = Path("work")

# repair is an agent stage under another name
_AGENT_WORK = pl.AGENT_STAGES | {"repair"}


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

    def for_stage(self, stage: str) -> asyncio.Semaphore:
        return self.agent if stage in _AGENT_WORK else self.cpu


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
            print("  " + _guarded(fn, deck, args))
        return

    async def main():
        sem = asyncio.Semaphore(workers)
        loop = asyncio.get_running_loop()

        async def one(deck):
            async with sem:
                return await loop.run_in_executor(None, _guarded, fn, deck, args)

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


def cmd_ingest(args):
    work = Path(args.work)
    n = 0
    for pattern in args.paths:
        p = Path(pattern)
        files = sorted(p.glob("*.pptx")) if p.is_dir() else [p]
        for f in files:
            deck = pl.ingest(f, work)
            print(f"  {deck.id}  {f.name[:52]}  "
                  f"{deck.meta()['slides']} slides")
            n += 1
    print(f"ingested {n} deck(s) into {work}")


def _inspect_one(deck, args):
    if deck.done("inspected") and not args.force:
        return f"{deck.id}  (already inspected)"
    try:
        d = pl.inspect(deck, dpi=args.dpi, force=args.force)
        return (f"{deck.id}  digest {d['digest_kb']}KB "
                f"(min {d['digest_min_kb']}KB)  {d['renders']} renders")
    except pl.StageError as e:
        return f"{deck.id}  FAILED — {e}"


def cmd_inspect(args):
    _each(args, _inspect_one, "inspected")


def _agent_stage(deck, stage, spec_builder, checker, args):
    try:
        with pl.lock(deck, stage):
            # keep the previous attempt: the log is opened "w" and the output
            # file is overwritten, so without this a re-run erases the evidence
            # of what the last one decided
            pl.archive_attempt(deck, stage)
            spec = spec_builder(deck)
            spec.model = args.model
            spec.timeout_min = args.timeout
            spec.log = deck.root / f"{stage}.jsonl"
            res = asyncio.run(agentmod.run_agent(spec))
            if res["status"] == "timeout":
                deck.mark(stage, "failed", error="timeout", log=res["log"])
                return f"TIMEOUT after {args.timeout}min"
            if res["status"] == "infra":
                # not a verdict about the deck: leave the stage unjudged so a
                # re-run picks it up, and never let the repair loop count it
                deck.mark(stage, "infra", error=res["why"], log=res["log"])
                return f"INFRA — {res['why']}"
            if res["status"] == "truncated":
                # the file it left behind will very likely pass the checker
                deck.mark(stage, "failed", error=res["why"], log=res["log"])
                return f"TRUNCATED — {res['why']}"
            try:
                detail = checker(deck)
            except pl.StageError as e:
                deck.mark(stage, "failed", error=str(e), log=res["log"])
                return f"REJECTED — {e}"
            # a well-formed report is not the same as a passing one: the gates
            # return their verdict and it decides the stage's status
            v = detail.get("verdict")
            sent_back = v is not None and v not in pl.PASSING_VERDICTS
            deck.mark(stage, "rejected" if sent_back else "ok", **detail)
            return detail
    except pl.DeckBusy as e:
        return f"BUSY — {e}"


def _propose_one(deck, args):
    if deck.done("proposed") and not args.force:
        return f"{deck.id}  (already proposed)"
    if not deck.done("inspected"):
        return f"{deck.id}  skipped — not inspected"
    out = _agent_stage(
        deck, "proposed",
        lambda d: agentmod.AgentRun("proposer", agentmod.propose_prompt(d)),
        pl.check_proposal, args)
    return f"{deck.id}  {out}"


def cmd_propose(args):
    _each(args, _propose_one, "proposed")


def _recipe_one(deck, args):
    if deck.done("recipe") and not args.force:
        return f"{deck.id}  (already has a recipe)"
    if not deck.done("proposed"):
        return f"{deck.id}  skipped — not proposed"
    if not (json.loads(deck.proposal.read_text()).get("tasks")):
        deck.mark("recipe", "skipped", reason="proposal is empty by design")
        return f"{deck.id}  skipped — deck yields no task"
    out = _agent_stage(
        deck, "recipe",
        lambda d: agentmod.AgentRun("recipe-writer", agentmod.recipe_prompt(d),
                                    max_turns=80),
        pl.check_recipe, args)
    return f"{deck.id}  {out}"


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
    if deck.done("reconciled") and not args.force:
        return f"{deck.id}  (already reconciled)"
    mat = deck.status_of("materialised")
    if mat not in ("ok", "partial"):
        return f"{deck.id}  skipped — assets not materialised"
    out = _agent_stage(
        deck, "reconciled",
        lambda d: agentmod.AgentRun("reconciler", agentmod.reconcile_prompt(d),
                                    max_turns=60),
        pl.check_reconcile, args)
    return f"{deck.id}  {out}"


def cmd_reconcile(args):
    _each(args, _reconcile_one, "reconciled")


def _solvable_one(deck, args):
    if deck.done("solvable") and not args.force:
        return f"{deck.id}  (already probed)"
    if not deck.done("reconciled"):
        return f"{deck.id}  skipped — not reconciled"
    t = json.loads((deck.root / "task.json").read_text())
    if t.get("verdict") == "needs_rework":
        return f"{deck.id}  skipped — reconcile rejected it first"
    # rebuilt every time: the probe judges the files as they stand now, and a
    # bundle left over from before a repair would have it judging the old task
    pl.bundle(deck)
    out = _agent_stage(
        deck, "solvable",
        lambda d: agentmod.AgentRun("solver-probe",
                                    agentmod.solvability_prompt(d),
                                    max_turns=50),
        pl.check_solvability, args)
    return f"{deck.id}  {out}"


def cmd_solvable(args):
    _each(args, _solvable_one, "solvable")


def _rework_of(deck):
    """The open work order, from whichever gate rejected the deck."""
    for name, key in (("solvability.json", "verdict"),
                      ("task.json", "verdict")):
        f = deck.root / name
        if not f.exists():
            continue
        d = json.loads(f.read_text())
        bad = (d.get(key) not in ("ready", "solvable"))
        if bad and d.get("rework"):
            return d["rework"], f"{name}:{d.get(key)}"
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
            spec.model, spec.timeout_min = args.model, args.timeout
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
    if res["status"] == "timeout":
        return f"{deck.id}  repair TIMEOUT"
    # whatever it touched, the stages below that point are now stale
    stages = {r.get("stage") for r in rework}
    for stg in ("proposed", "recipe", "materialise"):
        if stg in stages:
            pl.invalidate_from(deck, stg)
    # retire the verdict that ordered this repair: left in place, `_rework_of`
    # reads it again next round and the loop repairs the same complaint until
    # it hits MAX_REPAIRS, with the fix already applied
    if source and source.startswith("solvability"):
        pl.archive_attempt(deck, "solvable")
        (deck.root / "solvability.json").unlink(missing_ok=True)
        deck.mark("solvable", "stale", reason=f"repaired after {source}")
    return (f"{deck.id}  repaired (attempt {done + 1}) after {source}, "
            f"re-running from {sorted(stages) or ['?']}")


def cmd_repair(args):
    _each(args, _repair_one, "repair")


STAGE_FN = {"inspected": "cmd_inspect", "proposed": "cmd_propose",
            "recipe": "cmd_recipe", "degraded": "cmd_degrade",
            "materialised": "cmd_materialise", "reconciled": "cmd_reconcile",
            "solvable": "cmd_solvable"}


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


async def _run_stages(deck, args, pools):
    loop = asyncio.get_running_loop()

    async def step(stage, fn, ns):
        async with pools.for_stage(stage):
            return await loop.run_in_executor(None, fn, ns)

    for stage in pl.STAGES:
        if stage == "ingested" or deck.promoted(stage):
            continue
        if pl.STAGES.index(stage) > pl.STAGES.index(args.until):
            break
        ns = argparse.Namespace(work=args.work, deck=[deck.id],
                                force=False, dpi=args.dpi, workers=1,
                                cpu_workers=1,
                                model=args.model, timeout=args.timeout)
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
            ns2 = argparse.Namespace(**{**vars(ns), "force": True})
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
        if stage in ("reconciled", "solvable"):
            # a rejected deck goes round the repair loop rather than stopping
            # the run or, worse, being carried forward as if it had passed
            for _ in range(pl.MAX_REPAIRS):
                if not _rework_of(deck)[0]:
                    break
                await step("repair", cmd_repair, ns)
                for s2 in ("recipe", "degraded", "materialised",
                           "reconciled", "solvable"):
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
    decks = _decks(args)
    pools = None

    async def main():
        nonlocal pools
        pools = Pools(_workers_for(args, "proposed"),
                      _workers_for(args, "degraded"))
        # `return_exceptions` is the second net.  `_run_one` already catches
        # per deck, but a failure in the machinery around it — not in a stage —
        # would otherwise cancel every other deck mid-flight.
        for r in await asyncio.gather(
                *[_run_one(d, args, pools) for d in decks],
                return_exceptions=True):
            if isinstance(r, BaseException):
                print(f"  batch error: {type(r).__name__}: {r}")

    asyncio.run(main())
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
        rows.append((deck.id, " ".join(cells), deck.meta().get("name", "")[:26],
                     note))
    print(f"{'deck':<9}{'  '.join(s[:4] for s in pl.STAGES)}   file")
    for r in rows:
        print(f"{r[0]:<9}{r[1]:<16} {r[2]:<28} {r[3]}")
    done = sum(1 for deck in _decks(args) if deck.done(pl.STAGES[-1]))
    print(f"\n{done}/{len(rows)} through `{pl.STAGES[-1]}`")
    _status_tail(args, _decks(args))


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
              f"— `pptxgym run --until solvable --deck {ids}`")
        for deck, rw, src in open_work[:12]:
            stages = ", ".join(sorted({r.get("stage", "?") for r in rw}))
            print(f"  {deck.id}  {src}  → {stages}: "
                  f"{(rw[0].get('what') or '')[:64]}")
        if len(open_work) > 12:
            print(f"  … and {len(open_work) - 12} more")

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

    common = dict(deck_arg=deck_arg)

    p = sub.add_parser("inspect", help="digest + renders (deterministic)")
    common["deck_arg"](p)
    p.add_argument("--dpi", type=int, default=110)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("propose", help="agent: which tasks this deck should yield")
    common["deck_arg"](p)
    p.add_argument("--force", action="store_true")
    p.add_argument("--model", default=None)
    p.add_argument("--timeout", type=int, default=30, help="minutes")
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("recipe", help="agent: proposal -> executable recipe")
    common["deck_arg"](p)
    p.add_argument("--force", action="store_true")
    p.add_argument("--model", default=None)
    p.add_argument("--timeout", type=int, default=40, help="minutes")
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
    p.add_argument("--model", default=None)
    p.add_argument("--timeout", type=int, default=30, help="minutes")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("run", help="all stages, resuming what is already done")
    common["deck_arg"](p)
    p.add_argument("--until", default="degraded", choices=pl.STAGES)
    p.add_argument("--dpi", type=int, default=110)
    p.add_argument("--model", default=None)
    p.add_argument("--timeout", type=int, default=40, help="minutes per agent")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("solvable", help="agent: can this task actually be done")
    common["deck_arg"](p)
    p.add_argument("--force", action="store_true")
    p.add_argument("--model", default=None)
    p.add_argument("--timeout", type=int, default=30, help="minutes")
    p.set_defaults(func=cmd_solvable)

    p = sub.add_parser("repair", help="agent: fix a deck a gate rejected")
    common["deck_arg"](p)
    p.add_argument("--force", action="store_true")
    p.add_argument("--model", default=None)
    p.add_argument("--timeout", type=int, default=30, help="minutes")
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
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
