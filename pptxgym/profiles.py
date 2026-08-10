"""Two profiles, one pipeline.

`full` is the pipeline as it was measured: four specialists, each of them a
pair of eyes the orchestrator does not own.  It shipped 20 of 23 decks and it
is the thing to hand to somebody who asks for a rigorous generator.

`fast` is for the day the batch is due.  One agent writes the three judgement
artefacts itself — `proposal.json`, `recipe.json`, `task.json` — and records
each through `adopt`, which runs the same checker the specialist's output had
to pass.  What is given up is *fresh eyes*, not checks:

  - every machine checker still runs (`check_proposal`, `check_recipe`,
    `check_reconcile`), so a malformed artefact still cannot be recorded;
  - the mechanical instruction-vs-files pass still runs at packaging and
    still blocks on a contradiction — that was always the decidable half of
    reconcile's job and it was never the agent doing it;
  - all three hard measurements are untouched, because they cost nothing to
    keep: on deck0007 `score` took 3.4s and the fourteen-attack battery 24.3s,
    both of them pure Python with no model in them.  Cutting attacks would
    have saved twenty seconds and removed the only defence against reward
    hacking there is.

What is genuinely lost is the second reader of the proposal, and nothing in
the pipeline can measure that: a thin task scores 1.000/0.000, survives every
attack and packages cleanly.  The sealed solvability probe is kept for the
same reason — a task whose answer leaks into its own bundle is worth less
than nothing to train on, and no amount of self-review finds that.
"""

import os

FULL = "full"
FAST = "fast"
PROFILES = (FULL, FAST)

#: Read from the environment so it travels the way the engine does: the
#: foreman sets it on the orchestrator, the orchestrator's shell inherits it,
#: and every nested verb sees the same profile with no flag threaded through.
PROFILE_ENV = "PPTXGYM_PROFILE"
FOCUS_ENV = "PPTXGYM_FOCUS"

#: Stages the orchestrator may write itself under `fast`, and the checker
#: that has to pass first.  `solvable` is deliberately not here: it is the
#: sealed probe, the one witness that cannot be replaced by its own subject.
ADOPTABLE = ("proposed", "recipe", "reconciled")


def profile(args=None) -> str:
    """The profile in force: the flag if given, else the environment."""
    got = getattr(args, "profile", None) if args is not None else None
    got = (got or os.environ.get(PROFILE_ENV) or FULL).strip() or FULL
    if got not in PROFILES:
        raise ValueError(f"unknown profile {got!r} — pick from "
                         f"{', '.join(PROFILES)}")
    return got


def adopted(rec: dict) -> bool:
    """Was this record written by the owner, under a profile that allows it?

    Both halves matter.  `adopted` alone would let a `full`-profile deck
    launder a hand-written stage by adding a key; the profile stamp is what
    the record carries about the rules it was made under.
    """
    return bool(rec.get("adopted")) and rec.get("profile") == FAST
