"""Editing one prompt must not re-roll the other three agent stages.

All four seeded on the whole `agent` module, so a change to `recipe_prompt`
marked `proposed`, `reconciled` and `solvable` stale on every deck too. That is
not a rounding error. Two prompt fixes on one afternoon re-ran all four stages
for eight decks — a run's tokens and ninety minutes of wall clock — and the
re-roll *lost* deck0001 and deck0007, both of which had reached `packaged` on
the previous run. These stages have real run-to-run variance, so re-running one
is a gamble, not a refresh, and invalidating it for a reason that cannot have
changed its answer is paying for the gamble with nothing at stake on it.

    python3 -m pytest tests/test_prompt_fingerprint.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import pipeline as pl                              # noqa: E402

AGENTS = ("proposed", "recipe", "reconciled", "solvable")


def _edit(path: Path, needle: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert needle in text, f"{needle!r} is not in {path.name}"
    path.write_text(text.replace(needle, replacement, 1), encoding="utf-8")


@pytest.fixture()
def agent_copy(tmp_path):
    src = Path(pl.__file__).parent / "agent.py"
    copy = tmp_path / "agent.py"
    copy.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return copy


def _all(copy):
    return {s: pl._agent_parts(copy, p) for s, p in pl.STAGE_PROMPT.items()}


def test_editing_one_prompt_moves_only_that_stage(agent_copy):
    before = _all(agent_copy)
    pl._AGENT_PARTS.clear()
    _edit(agent_copy, "Turn a task proposal into an executable degradation",
          "Turn a task proposal into an executable degradation recipe now")
    after = _all(agent_copy)

    moved = [s for s in before if before[s] != after[s]]
    assert moved == ["recipe"], (
        f"editing `recipe_prompt` should move `recipe` and nothing else, "
        f"moved: {moved}")


def test_editing_the_reconcile_prompt_moves_only_reconcile(agent_copy):
    before = _all(agent_copy)
    pl._AGENT_PARTS.clear()
    _edit(agent_copy, "Reconcile one degraded PPT task",
          "Reconcile one single degraded PPT task")
    after = _all(agent_copy)
    assert [s for s in before if before[s] != after[s]] == ["reconciled"]


def test_editing_shared_machinery_moves_all_four(agent_copy):
    """The other direction, and it has to hold or the split is a way to keep
    a tick that was not earned. Retries, backoff and `run_agent` are what every
    agent stage runs through."""
    before = _all(agent_copy)
    pl._AGENT_PARTS.clear()
    _edit(agent_copy, "API_RETRIES = 3", "API_RETRIES = 5")
    after = _all(agent_copy)
    assert sorted(s for s in before if before[s] != after[s]) == \
        sorted(pl.STAGE_PROMPT)


def test_an_unparsable_agent_module_falls_back_to_the_whole_file(tmp_path):
    """Mid-edit, or renamed, or gone. Over-invalidating costs a re-run;
    under-invalidating lets a stage keep a tick it has not earned, and those
    are not the same mistake."""
    broken = tmp_path / "agent.py"
    broken.write_text("def recipe_prompt(:\n  pass\n")
    pl._AGENT_PARTS.clear()
    shared, prompt = pl._agent_parts(broken, "recipe_prompt")
    assert prompt == "recipe_prompt", "fell back rather than guessing"


def test_a_renamed_prompt_falls_back_rather_than_silently_matching_nothing(
        agent_copy):
    pl._AGENT_PARTS.clear()
    shared, prompt = pl._agent_parts(agent_copy, "no_such_prompt")
    assert prompt == "no_such_prompt"


def test_every_agent_stage_names_a_prompt_that_exists():
    """A typo here would silently fall back to whole-file hashing and put the
    cost back without anybody noticing."""
    import ast

    src = Path(pl.__file__).parent / "agent.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    defined = {n.name for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = sorted(set(pl.STAGE_PROMPT.values()) - defined)
    assert not missing, f"STAGE_PROMPT names functions that do not exist: {missing}"


def test_the_stages_named_are_the_stages_that_seed_on_agent():
    """If a fifth agent stage is added and not listed, it keeps the old
    whole-module behaviour silently."""
    seeded = {s for s, mods in pl.STAGE_CODE_SEEDS.items() if "agent" in mods}
    assert seeded == set(pl.STAGE_PROMPT)
