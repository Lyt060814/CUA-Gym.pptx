"""Per-deck state machine.

Every stage reads a directory and writes a directory.  Nothing is carried in a
conversation, which is what makes the run resumable, inspectable, and equally
runnable by a person or by a headless agent.

    ingested -> inspected -> proposed -> recipe -> degraded

Stages after `degraded` (reward authoring, verification, packaging) are
deliberately absent: they exist as code elsewhere but have not been through a
batch run yet, and a stage that has never been exercised does not belong in a
pipeline that other people are meant to trust.

A deck directory:

    work/deck0001/
      meta.json        where the source came from, how many slides
      source.pptx      the untouched deck — also the ground truth
      digest.json      structural digest for the proposer
      digest.min.json  same, compact, for an agent's context
      renders/p-NN.png one per slide
      proposal.json    what tasks this deck should yield  (agent)
      recipe.json      how to actually break it           (agent)
      input.pptx       the broken file
      delta.json       every change, with its prior value
      state.json       stage -> {status, at, detail}
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

STAGES = ["ingested", "inspected", "proposed", "recipe", "degraded"]
AGENT_STAGES = {"proposed", "recipe"}


class StageError(RuntimeError):
    pass


@dataclass
class Deck:
    root: Path

    @property
    def id(self) -> str:
        return self.root.name

    # -- paths -------------------------------------------------------------- #
    @property
    def source(self):
        return self.root / "source.pptx"

    @property
    def digest(self):
        return self.root / "digest.json"

    @property
    def digest_min(self):
        return self.root / "digest.min.json"

    @property
    def renders(self):
        return self.root / "renders"

    @property
    def proposal(self):
        return self.root / "proposal.json"

    @property
    def recipe(self):
        return self.root / "recipe.json"

    @property
    def input_pptx(self):
        return self.root / "input.pptx"

    @property
    def delta(self):
        return self.root / "delta.json"

    # -- state -------------------------------------------------------------- #
    def state(self) -> dict:
        f = self.root / "state.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def mark(self, stage: str, status: str, **detail):
        st = self.state()
        st[stage] = {"status": status, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     **detail}
        (self.root / "state.json").write_text(
            json.dumps(st, ensure_ascii=False, indent=1))

    def done(self, stage: str) -> bool:
        return self.state().get(stage, {}).get("status") == "ok"

    def stage_now(self) -> str:
        """The furthest stage completed, or "" if nothing has run."""
        last = ""
        for s in STAGES:
            if self.done(s):
                last = s
            else:
                break
        return last

    def meta(self) -> dict:
        f = self.root / "meta.json"
        return json.loads(f.read_text()) if f.exists() else {}


def ingest(pptx: Path, work: Path, deck_id: str | None = None) -> Deck:
    """Register a source deck. The source is also the ground truth — nothing
    downstream ever writes to it."""
    work.mkdir(parents=True, exist_ok=True)
    if deck_id is None:
        n = 1 + max([int(p.name[4:]) for p in work.glob("deck[0-9]*")
                     if p.name[4:].isdigit()] or [0])
        deck_id = f"deck{n:04d}"
    deck = Deck(work / deck_id)
    deck.root.mkdir(parents=True, exist_ok=True)
    shutil.copy(pptx, deck.source)

    from pptx import Presentation
    prs = Presentation(str(deck.source))
    (deck.root / "meta.json").write_text(json.dumps({
        "id": deck_id, "origin": str(Path(pptx).resolve()),
        "name": Path(pptx).name, "slides": len(prs.slides),
        "size_in": [round(prs.slide_width / 914400, 1),
                    round(prs.slide_height / 914400, 1)],
    }, ensure_ascii=False, indent=1))
    deck.mark("ingested", "ok", slides=len(prs.slides))
    return deck


def inspect(deck: Deck, dpi: int = 110, force: bool = False) -> dict:
    """Digest + one render per slide. Deterministic; no agent involved."""
    from . import deck_digest, render

    if deck.digest.exists() and not force:
        pass
    else:
        d = deck_digest.digest(str(deck.source))
        deck.digest.write_text(json.dumps(d, ensure_ascii=False, indent=1))
        # the compact copy is what an agent reads: same content, ~half the
        # tokens, and the indented one stays for humans
        deck.digest_min.write_text(
            json.dumps(d, ensure_ascii=False, separators=(",", ":")))

    deck.renders.mkdir(exist_ok=True)
    have = sorted(deck.renders.glob("p-*.png"))
    n_slides = deck.meta().get("slides", 0)
    if force or len(have) < n_slides:
        for f in have:
            f.unlink()
        render.render_pptx(str(deck.source), str(deck.renders), "p", dpi=dpi)
        have = sorted(deck.renders.glob("p-*.png"))

    detail = {"digest_kb": deck.digest.stat().st_size // 1024,
              "digest_min_kb": deck.digest_min.stat().st_size // 1024,
              "renders": len(have)}
    if len(have) < n_slides:
        deck.mark("inspected", "failed", **detail,
                  error=f"rendered {len(have)} of {n_slides} slides")
        raise StageError(f"{deck.id}: rendered {len(have)} of {n_slides} slides")
    deck.mark("inspected", "ok", **detail)
    return detail


def check_proposal(deck: Deck) -> dict:
    """Promote `proposed` only if the file is a usable proposal, not merely
    present.  File-existence as a success test is how a batch run ends up full
    of plausible rubbish."""
    if not deck.proposal.exists():
        raise StageError(f"{deck.id}: no proposal.json")
    try:
        p = json.loads(deck.proposal.read_text())
    except json.JSONDecodeError as e:
        raise StageError(f"{deck.id}: proposal.json is not valid JSON ({e})")

    tasks = p.get("tasks")
    if tasks is None:
        raise StageError(f"{deck.id}: proposal has no `tasks` key")
    if not tasks:
        # an empty proposal is a legitimate answer, but it has to say why
        if not (p.get("no_task_reason") or "").strip():
            raise StageError(f"{deck.id}: empty `tasks` with no no_task_reason")
        return {"tasks": 0, "reason": p["no_task_reason"][:120]}

    n_slides = deck.meta().get("slides", 10 ** 6)
    out = []
    for t in tasks:
        for key in ("name", "difficulty", "est_steps", "instruction",
                    "degradations"):
            if not t.get(key):
                raise StageError(f"{deck.id}: task missing `{key}`")
        if not t["degradations"]:
            raise StageError(f"{deck.id}: task {t['name']} has no degradations")
        for g in t["degradations"]:
            for page in g.get("slides", []):
                if not 1 <= page <= n_slides:
                    raise StageError(
                        f"{deck.id}: {g.get('id')} targets slide {page}, "
                        f"deck has {n_slides}")
        total = sum(g.get("est_steps", 0) for g in t["degradations"])
        band = ("easy" if t["est_steps"] <= 100
                else "medium" if t["est_steps"] <= 300 else "hard")
        if band != t["difficulty"]:
            raise StageError(
                f"{deck.id}: task {t['name']} says {t['difficulty']} but "
                f"{t['est_steps']} steps is {band}")
        out.append({"name": t["name"], "difficulty": t["difficulty"],
                    "est_steps": t["est_steps"], "sum_of_parts": total,
                    "degradations": len(t["degradations"])})
    return {"tasks": len(tasks), "detail": out}


def check_recipe(deck: Deck) -> dict:
    """The recipe has to name ops that exist and slides that exist."""
    from . import degrade_exec

    if not deck.recipe.exists():
        raise StageError(f"{deck.id}: no recipe.json")
    r = json.loads(deck.recipe.read_text())
    n_slides = deck.meta().get("slides", 10 ** 6)
    n_steps = 0
    for page, steps in (r.get("slides") or {}).items():
        if not str(page).isdigit() or not 1 <= int(page) <= n_slides:
            raise StageError(f"{deck.id}: recipe targets slide {page!r}")
        for st in steps:
            if st.get("op") not in degrade_exec.REGISTRY:
                raise StageError(
                    f"{deck.id}: unknown op {st.get('op')!r} "
                    f"(known: {', '.join(sorted(degrade_exec.REGISTRY))})")
            n_steps += 1
    for key in ("smartart", "chart"):
        for spec in (r.get(key) or []):
            if not 1 <= spec.get("slide", 0) <= n_slides:
                raise StageError(f"{deck.id}: {key} targets slide "
                                 f"{spec.get('slide')!r}")
            n_steps += 1
    if not n_steps:
        raise StageError(f"{deck.id}: recipe does nothing")
    return {"steps": n_steps, "slides": len(r.get("slides") or {})}


def degrade(deck: Deck) -> dict:
    """Apply the recipe, then gate the package. A broken or leaky file never
    reaches the next stage."""
    from . import degrade_exec, pkg_check

    recipe = json.loads(deck.recipe.read_text())
    delta = degrade_exec.run(str(deck.source), recipe, str(deck.input_pptx))
    deck.delta.write_text(json.dumps(delta, ensure_ascii=False, indent=1))

    integ = pkg_check.check(str(deck.input_pptx))
    leak = pkg_check.leak_check(str(deck.input_pptx), delta, str(deck.source))
    problems = (integ["problems"] + integ["duplicate_ids"]
                + leak["leaks"] + leak["dead_rels"])
    n = sum(len(v) for v in delta["slides"].values())
    detail = {"changes": n, "slides": len(delta["slides"]),
              "gate": "ok" if not problems else "FAILED",
              "problems": problems[:8]}
    if problems:
        deck.mark("degraded", "failed", **detail)
        raise StageError(f"{deck.id}: package gate failed — {problems[0]}")
    deck.mark("degraded", "ok", **detail)
    return detail


def decks_in(work: Path) -> list[Deck]:
    return [Deck(p) for p in sorted(work.glob("deck*")) if p.is_dir()]


class DeckBusy(RuntimeError):
    pass


class lock:
    """Refuse to run two stages on one deck at once.

    Stages hand off through files, so two processes working the same deck will
    happily interleave: one writes a fresh proposal while the other is midway
    through a recipe for the previous one, and the pair that lands on disk has
    never been consistent.  It is invisible afterwards — the files all parse —
    which is exactly why it needs a lock rather than care.
    """

    def __init__(self, deck: Deck, stage: str):
        self.path = deck.root / ".lock"
        self.stage = stage
        self.deck = deck

    def __enter__(self):
        import os
        if self.path.exists():
            try:
                held = json.loads(self.path.read_text())
            except Exception:                                # noqa: BLE001
                held = {}
            pid = held.get("pid")
            alive = False
            if pid:
                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False
            if alive:
                raise DeckBusy(
                    f"{self.deck.id} is locked by pid {pid} running "
                    f"{held.get('stage')!r} since {held.get('at')}")
        self.path.write_text(json.dumps(
            {"pid": os.getpid(), "stage": self.stage,
             "at": time.strftime("%Y-%m-%dT%H:%M:%S")}))
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
        return False
