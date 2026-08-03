"""pptxgym — turn a folder of real .pptx decks into computer-use RL tasks.

    pptxgym ingest  corpus/*.pptx
    pptxgym run     --all --workers 6
    pptxgym status

Every subcommand is one stage and can be run on its own; `run` chains them and
skips whatever is already done, so a failed batch is resumed rather than
restarted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import agent as agentmod
from . import pipeline as pl

DEFAULT_WORK = Path("work")


# --------------------------------------------------------------------------- #
# stages
# --------------------------------------------------------------------------- #


def _decks(args) -> list[pl.Deck]:
    work = Path(args.work)
    if getattr(args, "deck", None):
        return [pl.Deck(work / d) for d in args.deck]
    return pl.decks_in(work)


def _each(args, fn):
    """Run a per-deck stage across decks, honouring --workers.

    Re-running one stage over a batch is the most common thing anyone does,
    and it was the one path that ignored --workers: a plain for-loop, so six
    four-minute agents took twenty-five minutes instead of five.
    """
    decks = _decks(args)
    workers = max(1, getattr(args, "workers", 1) or 1)
    if workers == 1 or len(decks) == 1:
        for deck in decks:
            print("  " + fn(deck, args))
        return

    async def main():
        sem = asyncio.Semaphore(workers)
        loop = asyncio.get_running_loop()

        async def one(deck):
            async with sem:
                return await loop.run_in_executor(None, fn, deck, args)

        for line in await asyncio.gather(*[one(d) for d in decks]):
            print("  " + line)

    asyncio.run(main())


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
    _each(args, _inspect_one)


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
            try:
                detail = checker(deck)
            except pl.StageError as e:
                deck.mark(stage, "failed", error=str(e), log=res["log"])
                return f"REJECTED — {e}"
            deck.mark(stage, "ok", **detail)
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
    _each(args, _propose_one)


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
    _each(args, _recipe_one)


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
    _each(args, _degrade_one)


def _materialise_one(deck, args):
    st = deck.state().get("materialised", {}).get("status")
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
    _each(args, _materialise_one)


def _reconcile_one(deck, args):
    if deck.done("reconciled") and not args.force:
        return f"{deck.id}  (already reconciled)"
    mat = deck.state().get("materialised", {}).get("status")
    if mat not in ("ok", "partial"):
        return f"{deck.id}  skipped — assets not materialised"
    out = _agent_stage(
        deck, "reconciled",
        lambda d: agentmod.AgentRun("reconciler", agentmod.reconcile_prompt(d),
                                    max_turns=60),
        pl.check_reconcile, args)
    return f"{deck.id}  {out}"


def cmd_reconcile(args):
    _each(args, _reconcile_one)


def _solvable_one(deck, args):
    if deck.done("solvable") and not args.force:
        return f"{deck.id}  (already probed)"
    if not deck.done("reconciled"):
        return f"{deck.id}  skipped — not reconciled"
    t = json.loads((deck.root / "task.json").read_text())
    if t.get("verdict") == "needs_rework":
        return f"{deck.id}  skipped — reconcile rejected it first"
    out = _agent_stage(
        deck, "solvable",
        lambda d: agentmod.AgentRun("solver-probe",
                                    agentmod.solvability_prompt(d),
                                    max_turns=50),
        pl.check_solvability, args)
    return f"{deck.id}  {out}"


def cmd_solvable(args):
    _each(args, _solvable_one)


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
    st = deck.state().get("reconciled", {})
    if st.get("status") != "ok":
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
            spec = agentmod.AgentRun("orchestrator", agentmod.repair_prompt(deck),
                                     max_turns=60)
            spec.model, spec.timeout_min = args.model, args.timeout
            spec.log = deck.root / f"repair-{done + 1:02d}.jsonl"
            res = asyncio.run(agentmod.run_agent(spec))
    except pl.DeckBusy as e:
        return f"{deck.id}  BUSY — {e}"
    if res["status"] == "timeout":
        return f"{deck.id}  repair TIMEOUT"
    # whatever it touched, the stages below that point are now stale
    stages = {r.get("stage") for r in rework}
    for stg in ("proposed", "recipe", "materialise"):
        if stg in stages:
            pl.invalidate_from(deck, stg)
    return (f"{deck.id}  repaired (attempt {done + 1}) after {source}, "
            f"re-running from {sorted(stages) or ['?']}")


def cmd_repair(args):
    _each(args, _repair_one)


async def _run_one(deck, args, sem):
    """Drive one deck through the stages, honouring what is already done."""
    async with sem:
        loop = asyncio.get_running_loop()
        for stage in pl.STAGES:
            if stage == "ingested" or deck.done(stage):
                continue
            if pl.STAGES.index(stage) > pl.STAGES.index(args.until):
                break
            ns = argparse.Namespace(work=args.work, deck=[deck.id],
                                    force=False, dpi=args.dpi, workers=1,
                                    model=args.model, timeout=args.timeout)
            fn = {"inspected": cmd_inspect, "proposed": cmd_propose,
                  "recipe": cmd_recipe, "degraded": cmd_degrade,
                  "materialised": cmd_materialise,
                  "reconciled": cmd_reconcile}[stage]
            await loop.run_in_executor(None, fn, ns)
            if not deck.done(stage):
                return
            if stage in ("reconciled", "solvable"):
                # a rejected deck goes round the repair loop rather than
                # stopping the run or, worse, being carried forward as if it
                # had passed
                for _ in range(pl.MAX_REPAIRS):
                    if not _rework_of(deck)[0]:
                        break
                    await loop.run_in_executor(None, cmd_repair, ns)
                    for s2 in ("recipe", "degraded", "materialised",
                               "reconciled", "solvable"):
                        if not deck.done(s2) and pl.STAGES.index(s2) <= \
                                pl.STAGES.index(args.until):
                            await loop.run_in_executor(
                                None, {"recipe": cmd_recipe,
                                       "degraded": cmd_degrade,
                                       "materialised": cmd_materialise,
                                       "reconciled": cmd_reconcile,
                                       "solvable": cmd_solvable}[s2], ns)
                    if deck.state().get("reconciled", {}).get(
                            "status") == "needs_human":
                        break


def cmd_run(args):
    decks = _decks(args)
    sem = asyncio.Semaphore(args.workers)

    async def main():
        await asyncio.gather(*[_run_one(d, args, sem) for d in decks])

    asyncio.run(main())
    cmd_status(args)


def cmd_status(args):
    rows = []
    for deck in _decks(args):
        st = deck.state()
        cells = []
        for s in pl.STAGES:
            v = st.get(s, {}).get("status")
            cells.append({"ok": "✓", "failed": "✗", "skipped": "–",
                          "partial": "~", None: "·"}.get(v, "?"))
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
        q.add_argument("--workers", type=int, default=1,
                       help="decks in parallel (default: 1)")

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
