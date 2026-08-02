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


def cmd_inspect(args):
    for deck in _decks(args):
        if deck.done("inspected") and not args.force:
            print(f"  {deck.id}  (already inspected)")
            continue
        try:
            d = pl.inspect(deck, dpi=args.dpi, force=args.force)
            print(f"  {deck.id}  digest {d['digest_kb']}KB "
                  f"(min {d['digest_min_kb']}KB)  {d['renders']} renders")
        except pl.StageError as e:
            print(f"  {deck.id}  FAILED — {e}")


def _agent_stage(deck, stage, spec_builder, checker, args):
    try:
        with pl.lock(deck, stage):
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


def cmd_propose(args):
    for deck in _decks(args):
        if deck.done("proposed") and not args.force:
            print(f"  {deck.id}  (already proposed)")
            continue
        if not deck.done("inspected"):
            print(f"  {deck.id}  skipped — not inspected")
            continue
        out = _agent_stage(
            deck, "proposed",
            lambda d: agentmod.AgentRun("proposer", agentmod.propose_prompt(d)),
            pl.check_proposal, args)
        print(f"  {deck.id}  {out}")


def cmd_recipe(args):
    for deck in _decks(args):
        if deck.done("recipe") and not args.force:
            print(f"  {deck.id}  (already has a recipe)")
            continue
        if not deck.done("proposed"):
            print(f"  {deck.id}  skipped — not proposed")
            continue
        if not (json.loads(deck.proposal.read_text()).get("tasks")):
            deck.mark("recipe", "skipped", reason="proposal is empty by design")
            print(f"  {deck.id}  skipped — deck yields no task")
            continue
        out = _agent_stage(
            deck, "recipe",
            lambda d: agentmod.AgentRun("recipe-writer", agentmod.recipe_prompt(d),
                                        max_turns=80),
            pl.check_recipe, args)
        print(f"  {deck.id}  {out}")


def cmd_degrade(args):
    for deck in _decks(args):
        if deck.done("degraded") and not args.force:
            print(f"  {deck.id}  (already degraded)")
            continue
        if not deck.done("recipe"):
            print(f"  {deck.id}  skipped — no recipe")
            continue
        try:
            # the lock is what stops the recipe agent from committing its own
            # work: its parent holds this deck while it runs, so a shelled-out
            # `pptxgym degrade` is refused and it has to use `tools trial`
            with pl.lock(deck, "degraded"):
                d = pl.degrade(deck)
            print(f"  {deck.id}  {d['changes']} change(s) on {d['slides']} "
                  f"slide(s)  gate={d['gate']}")
        except pl.DeckBusy as e:
            print(f"  {deck.id}  BUSY — {e}")
        except pl.StageError as e:
            print(f"  {deck.id}  FAILED — {e}")


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
                                    force=False, dpi=args.dpi,
                                    model=args.model, timeout=args.timeout)
            fn = {"inspected": cmd_inspect, "proposed": cmd_propose,
                  "recipe": cmd_recipe, "degraded": cmd_degrade}[stage]
            await loop.run_in_executor(None, fn, ns)
            if not deck.done(stage):
                if deck.state().get(stage, {}).get("status") == "skipped":
                    return
                return


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
                          None: "·"}.get(v, "?"))
        note = ""
        for s in reversed(pl.STAGES):
            if st.get(s, {}).get("status") == "failed":
                note = str(st[s].get("error", ""))[:70]
                break
        else:
            d = st.get("degraded", {})
            if d.get("status") == "ok":
                note = f"{d.get('changes')} changes / {d.get('slides')} slides"
        rows.append((deck.id, " ".join(cells), deck.meta().get("name", "")[:26],
                     note))
    print(f"{'deck':<9}{'  '.join(s[:4] for s in pl.STAGES)}   file")
    for r in rows:
        print(f"{r[0]:<9}{r[1]:<16} {r[2]:<28} {r[3]}")
    done = sum(1 for deck in _decks(args) if deck.done("degraded"))
    print(f"\n{done}/{len(rows)} through `degraded`")


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

    common = dict(deck_arg=lambda q: q.add_argument(
        "--deck", nargs="*", help="deck ids (default: all)"))

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

    p = sub.add_parser("run", help="all stages, resuming what is already done")
    common["deck_arg"](p)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--until", default="degraded", choices=pl.STAGES)
    p.add_argument("--dpi", type=int, default=110)
    p.add_argument("--model", default=None)
    p.add_argument("--timeout", type=int, default=40, help="minutes per agent")
    p.set_defaults(func=cmd_run)

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
