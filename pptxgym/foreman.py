"""Run one orchestrator per deck, and judge nothing.

This is the run loop the orchestrator architecture leaves behind. The old
`run` sequenced eleven stages per deck and owned every verdict in between;
that judgment now belongs to one agent per deck (`.claude/agents/
orchestrator.md`), and what remains for Python is exactly what a scaling
pipeline still owes:

- **prep** — ingest and inspect are deterministic; a deck arrives at its
  orchestrator with the digest and renders already on disk, the same way the
  old flow prepped them.
- **spawn** — one `claude --agent orchestrator` per deck, through
  `agent.run_agent`, which is where the infra-retry, log-archiving and
  which-model-actually-ran machinery already lives.
- **guard** — the shared tools are fingerprinted before each orchestrator and
  any edit is reverted (or named, when it cannot be) exactly as the repair
  loop did. One repair agent once patched `degrade_exec` mid-run, correctly,
  and silently changed what every other deck would be degraded into. An
  owner owns one deck; the tools are everybody's.
- **collect** — a deck ships when the *mechanical record* says so: `packaged`
  ok in state.json, a REVIEW.md on disk, `scored` reading gt=1.000 /
  input=0.000, and a bundle whose bytes agree with what the instruction
  promises. Anything else parks, with the orchestrator's own last words as
  the reason. Nothing here reads a proposal, weighs a finding or overrides
  an agent; a deck this loop cannot mechanically verify is a deck a human
  looks at.

What is deliberately absent: stage sequencing, rework routing, repair
budgets, and every per-stage gate. The orchestrator's manual carries the
doctrine; this file carries the plumbing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from . import agent as agentmod
from . import pipeline as pl

#: What the orchestrator and its specialists run on unless told otherwise.
#: A directive, not a default-by-accident: spawned agents run opus at high
#: effort. The session default of whoever launched the foreman is exactly the
#: thing this exists to override.
MODEL = "opus"
EFFORT = "high"

#: Turn and wall-clock budget for one deck's orchestrator. The first trial
#: (workx/deck0001) used 128 of 150 turns and 161 minutes wall, with two
#: specialist re-runs and a second sealed probe inside that; 150/300 gives the
#: same head-room to a harder deck without letting a wedged one hold a slot
#: for a day.
MAX_TURNS = 150
TIMEOUT_MIN = 300

#: The orchestrator's toolset. `Task` is the subagent tool's name in the CLI
#: (verified against the session init record of the first trial), and it is
#: the one entry the specialist stages' default list does not carry.
TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "Task"]


def mission(deck: pl.Deck, work: Path, turns: int,
            smodel: str, seffort: str) -> str:
    """The per-deck brief. Doctrine lives in orchestrator.md; this names
    the deck, the boundaries and the budget, and nothing else."""
    meta = deck.meta()
    return f"""You own one deck end to end.

Work root: {work.resolve()}
Deck: {deck.id} ({meta.get('slides', '?')} slides). ingest and inspect have \
already run; the digest and renders are ready under {deck.root.resolve()}/.

Deliver either a packaged task in {work.resolve()}/emitted/ whose three
measurements hold, or a reasoned no in {deck.root.resolve()}/REVIEW.md. Write
REVIEW.md as you go.

Standing controls:
- When you run a specialist verb (propose / recipe / reconcile / solvable),
  always add: --model {smodel} --effort {seffort}
- Only {deck.root.resolve()}/ is yours. Other decks' directories, and any
  other work root, are other experiments' evidence — do not read them.
- The pipeline's code and prompts are not yours to change; a defect in a verb
  is a finding for REVIEW.md, not an edit.
- Never git commit, push or checkout.

Your turn budget is {turns}; specialists bill their own. End with three
lines: the task, the three measurement readings, and where REVIEW.md stands."""


# --------------------------------------------------------------------------- #
# the mechanical verdict
# --------------------------------------------------------------------------- #


def shipped(deck: pl.Deck) -> tuple[bool, str]:
    """Does the mechanical record say this deck produced a task?

    Reads, never runs: the measurements were executed by the verbs the
    orchestrator called, they recompute from artefacts every time, and
    `package` will not mark itself ok over a broken bundle. What this checks
    is that every required record exists and says what shipping requires —
    which is a statement about the *record*, so a deck that passes here can
    still be audited by re-running the verbs, and the first trial was.
    """
    if not deck.done("packaged"):
        return False, "state.json does not record `packaged: ok`"
    if not (deck.root / "REVIEW.md").exists():
        return False, "no REVIEW.md — the argument is half the deliverable"
    sc = (deck.state().get("scored") or {})
    gt, inp = sc.get("gt"), sc.get("input")
    if gt != 1.0 or inp != 0.0:
        return False, f"scored records gt={gt} input={inp}, not 1.0/0.0"
    gaps = pl.bundle_problems(deck)
    if gaps:
        return False, f"bundle: {gaps[0]}"
    return True, ""


def _last_words(res: dict, log: Path) -> str:
    """The orchestrator's final line, for the park record."""
    txt = (agentmod.last_result(log).get("result") or "").strip()
    if txt:
        return txt[-400:]
    return str(res.get("why") or res.get("status") or "")[:400]


# --------------------------------------------------------------------------- #
# a broken verb: the request loop
#
# An orchestrator that hits a defect in a shared verb may not fix it — the
# tools are everybody's — but a deck dying of a small script bug is exactly
# the waste the old pipeline was condemned for. So the manual has it write
# `toolfix.json` (contract violation + repro + optionally the fix it would
# make) and wait a bounded while for `toolfix-answer.json`. The foreman's
# side of that contract is here: surface the request the moment it appears,
# classify the park when nobody answered, and — because the fix arrives as a
# *commit* made outside the run — recognise an authorised HEAD move as the
# fix landing rather than as an agent moving the branch.
# --------------------------------------------------------------------------- #


def _toolfix(deck: pl.Deck, name: str = "toolfix.json") -> dict | None:
    try:
        return json.loads((deck.root / name).read_text())
    except (OSError, ValueError):
        return None


def _toolfix_unresolved(deck: pl.Deck) -> dict | None:
    """The deck's fix request, when nobody has (successfully) answered it."""
    fix = _toolfix(deck)
    if not fix:
        return None
    ans = _toolfix(deck, "toolfix-answer.json")
    return None if (ans and ans.get("fixed")) else fix


def _authorized_heads(work: Path) -> set[str]:
    """Commits the supervising side has declared to be mid-run tool fixes.

    One JSON object per line in `<work>/authorized-heads.jsonl`, written by a
    human (or their session) alongside the fix commit: {"head": ..., "why":
    ...}. Anything else that moves HEAD under a running orchestrator is still
    a violation.
    """
    out = set()
    try:
        for line in (work / "authorized-heads.jsonl").read_text().splitlines():
            try:
                h = json.loads(line).get("head")
            except ValueError:
                continue
            if h:
                out.add(h)
    except OSError:
        pass
    return out


async def _watch_toolfix(deck: pl.Deck) -> None:
    """Say it the moment a request lands, not when the run ends.

    The whole point of the request loop is turnaround: an orchestrator waits
    ~30 minutes and then parks, so a request only found in the post-mortem is
    a request that was always going to time out.
    """
    while True:
        await asyncio.sleep(30)
        fix = _toolfix(deck)
        if fix:
            print(f"  {deck.id}  TOOLFIX REQUESTED — {fix.get('verb')}: "
                  f"{str(fix.get('what'))[:140]}", flush=True)
            pl.log_event("toolfix_requested", deck=deck.id,
                         verb=fix.get("verb"), what=fix.get("what"),
                         answer=str((deck.root / "toolfix-answer.json")
                                    .resolve()))
            return


# --------------------------------------------------------------------------- #
# one deck, prep to verdict
# --------------------------------------------------------------------------- #


async def run_deck(deck: pl.Deck, work: Path, args) -> dict:
    started = time.monotonic()

    def _finish(outcome: str, why: str = "", **extra) -> dict:
        rec = {"deck": deck.id, "outcome": outcome, "why": why,
               "minutes": round((time.monotonic() - started) / 60, 1),
               "at": time.strftime("%Y-%m-%dT%H:%M:%S"), **extra}
        (deck.root / "foreman.json").write_text(json.dumps(rec, indent=1))
        pl.log_event("deck_done", deck=deck.id, outcome=outcome, why=why)
        return rec

    # ---- prep: deterministic, no model ---------------------------------- #
    if not deck.done("inspected"):
        try:
            await asyncio.to_thread(pl.inspect, deck, roundtrip=args.roundtrip)
        except pl.StageError as e:
            return _finish("parked", f"inspect failed — {e}")

    # ---- spawn: one owner ------------------------------------------------ #
    before = pl.tool_tree_state()
    log = deck.root / "orchestrator.jsonl"
    spec = agentmod.AgentRun(
        "orchestrator",
        mission(deck, work, args.max_turns, args.specialist_model,
                args.specialist_effort),
        max_turns=args.max_turns, timeout_min=args.timeout,
        model=args.model, effort=args.effort,
        allowed_tools=list(TOOLS), log=log,
        outputs=[deck.root / "REVIEW.md"],
        env={"PPTXGYM_SKIP_PERMISSIONS": "1"})
    pl.log_event("deck_started", deck=deck.id, model=args.model,
                 effort=args.effort, max_turns=args.max_turns)
    watch = asyncio.create_task(_watch_toolfix(deck))
    try:
        res = await agentmod.run_agent(spec)
    finally:
        watch.cancel()

    # ---- guard: the tools are everybody's -------------------------------- #
    touched = pl.revert_tool_changes(deck, before, "foreman")
    if touched == "HEAD moved" and \
            (pl.code_version() or {}).get("commit") in _authorized_heads(work):
        # the working tree is clean and HEAD sits on a commit the supervising
        # side declared to be a mid-run tool fix: that is the answer to a
        # toolfix request landing, not an agent moving the branch
        pl.log_event("toolfix_head_authorized", deck=deck.id,
                     head=(pl.code_version() or {}).get("commit"))
        touched = None
    if touched:
        return _finish("parked", f"the orchestrator edited the shared tools "
                                 f"({touched}); reverted where possible, "
                                 f"diff kept beside the log",
                       agent=res.get("status"), **agentmod.ran_as(log))

    # ---- collect: the record decides ------------------------------------- #
    if res.get("status") in ("infra", "timeout", "truncated", "barrier"):
        return _finish("parked", f"orchestrator {res.get('status')}: "
                                 f"{str(res.get('why') or '')[:200]}",
                       agent=res.get("status"), **agentmod.ran_as(log))
    ok, why = shipped(deck)
    if ok:
        return _finish("shipped", task=(deck.state().get("packaged") or {})
                       .get("task_id"), agent=res.get("status"),
                       **agentmod.ran_as(log))
    # A reasoned no is a normal outcome, not a failure: the deck parks with
    # the orchestrator's own words and its REVIEW.md is the record to read.
    # A deck that asked for a tool fix and never got one is named as such —
    # it is the one kind of park that re-running after a commit clears, and
    # `pick_decks` will sweep it up on the next invocation.
    fix = _toolfix_unresolved(deck)
    if fix:
        why = (f"tool_defect: {fix.get('verb')} — "
               f"{str(fix.get('what'))[:150]}; {why}")
        return _finish("parked", why, kind="tool_defect",
                       last=_last_words(res, log), agent=res.get("status"),
                       **agentmod.ran_as(log))
    return _finish("parked", why, last=_last_words(res, log),
                   agent=res.get("status"), **agentmod.ran_as(log))


# --------------------------------------------------------------------------- #
# the batch
# --------------------------------------------------------------------------- #


def dirty_tool_paths() -> list[str]:
    """Uncommitted paths in the tool tree, or [] when clean / not a git tree."""
    return sorted(pl._tool_entries(pl.tool_tree_state()))


def pick_decks(work: Path, args) -> list[pl.Deck]:
    if args.deck:
        return [pl.Deck(work / d) for d in args.deck]
    out = []
    for deck in pl.decks_in(work):
        if not args.force and shipped(deck)[0]:
            continue
        out.append(deck)
    return out


async def run_batch(work: Path, decks: list[pl.Deck], args) -> list[dict]:
    sem = asyncio.Semaphore(max(1, args.workers))

    async def one(deck: pl.Deck) -> dict:
        async with sem:
            return await run_deck(deck, work, args)

    return list(await asyncio.gather(*(one(d) for d in decks)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="pptxgym.foreman",
        description="one orchestrator per deck; prep, spawn, guard, collect")
    ap.add_argument("paths", nargs="*",
                    help=".pptx files or directories to ingest first")
    ap.add_argument("--work", default="work")
    ap.add_argument("--deck", nargs="*", help="deck ids (default: every deck "
                    "in the work root not already shipped)")
    ap.add_argument("--workers", type=int, default=2,
                    help="orchestrators running at once")
    ap.add_argument("--max-turns", type=int, default=MAX_TURNS)
    ap.add_argument("--timeout", type=int, default=TIMEOUT_MIN,
                    help="minutes of wall clock per deck")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--effort", default=EFFORT)
    ap.add_argument("--specialist-model", default=MODEL)
    ap.add_argument("--specialist-effort", default=EFFORT)
    ap.add_argument("--roundtrip", action="store_true", default=True)
    ap.add_argument("--no-roundtrip", dest="roundtrip", action="store_false")
    ap.add_argument("--force", action="store_true",
                    help="re-run decks that already shipped")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="launch even though the tool tree has uncommitted "
                         "changes (the guard will blame the agents for them)")
    args = ap.parse_args(argv)

    # The guard attributes an agent's edits by comparing the tree around its
    # run, and that comparison is only sound from a clean start. Run 2's two
    # decks finished their work and were parked anyway, because the foreman
    # was launched around the foreman's own uncommitted source: the spawn
    # fingerprint recorded the dirty files, the supervisor committed them
    # mid-run, and at collect time the guard could only say "the tree changed
    # and I cannot attribute it" — which is a park. Commit first.
    dirty = dirty_tool_paths()
    if dirty and not args.allow_dirty:
        print("refusing to start: the tool tree has uncommitted changes ("
              + ", ".join(dirty[:5]) + ("…" if len(dirty) > 5 else "") + ").\n"
              "Every mid-run commit would then read as an agent's edit and "
              "park its deck. Commit (or stash) first, or pass --allow-dirty "
              "to take that risk knowingly.")
        return 2

    work = Path(args.work)
    if args.paths:
        r = pl.ingest_many(args.paths, work)
        print(f"ingested: {len(r['registered'])} registered, "
              f"{len(r['duplicate'])} duplicate, {len(r['rejected'])} rejected")
    decks = pick_decks(work, args)
    if not decks:
        print("nothing to do — every deck in the work root has shipped")
        return 0

    pl.open_run(work, argv=argv or sys.argv[1:], cmd="foreman",
                decks=[d.id for d in decks],
                limits={"workers": args.workers, "max_turns": args.max_turns,
                        "timeout_min": args.timeout, "model": args.model,
                        "effort": args.effort})
    try:
        results = asyncio.run(run_batch(work, decks, args))
    finally:
        pl.close_run()

    for r in results:
        line = f"  {r['deck']}  {r['outcome']:8s} {r.get('minutes', '?')}min"
        if r["outcome"] == "shipped":
            line += f"  task_{r.get('task')}"
        if r.get("why"):
            line += f"  — {r['why'][:120]}"
        print(line)
    done = sum(1 for r in results if r["outcome"] == "shipped")
    print(f"{done}/{len(results)} shipped")
    return 0 if done == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
