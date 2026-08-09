"""Which model runs which stage, and how we know afterwards.

The knob exists so that one variable can be changed at a time against the
pilot; it deliberately assigns nothing.  What it must never do is leave the
batch with artefacts nobody can attribute — a deck proposed by one model has
to be distinguishable from a deck proposed by another, from the deck itself,
after the fact.  That is why `--fallback-model` is wired only as far as the
log can confirm what actually ran.

Same boundary as `test_retry.py`: a fake `claude` on PATH, no API.

    python3 -m pytest tests/test_models.py -q
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pptxgym import agent                                        # noqa: E402
from pptxgym import cli                                          # noqa: E402
from test_retry import (fake_claude, no_waiting, proposing,      # noqa: E402,F401
                        _args, _deck)


# --------------------------------------------------------------------------- #
# the mapping
# --------------------------------------------------------------------------- #


def test_a_bare_value_still_means_every_stage():
    """`--model opus` is what everyone types and it keeps meaning what it did."""
    a = agent.Assignment.from_args(_args("work", model="opus"))
    for role in agent.ROLES:
        assert a.for_stage(role)["model"] == "opus"


def test_a_stage_can_be_named_on_its_own():
    a = agent.Assignment.from_args(
        _args("work", model="propose=opus,recipe=sonnet"))
    assert a.for_stage("propose")["model"] == "opus"
    assert a.for_stage("recipe")["model"] == "sonnet"
    # a stage nobody named keeps the default, which is `claude`'s own
    assert a.for_stage("reconcile")["model"] is None


def test_a_bare_value_and_a_named_one_compose():
    a = agent.Assignment.from_args(_args("work", model="sonnet,recipe=opus"))
    assert a.for_stage("recipe")["model"] == "opus"
    assert a.for_stage("solvable")["model"] == "sonnet"


def test_the_pipeline_name_and_the_command_line_name_are_both_accepted():
    """`propose` is what you type; `proposed` is what `state.json` calls it."""
    a = agent.Assignment.from_args(_args("work", model="proposed=opus"))
    assert a.for_stage("proposed")["model"] == "opus"
    assert a.for_stage("propose")["model"] == "opus"


def test_effort_is_carried_per_stage_the_same_way():
    a = agent.Assignment.from_args(
        _args("work", model=None, effort="solvable=high"))
    assert a.for_stage("solvable")["effort"] == "high"
    assert a.for_stage("propose")["effort"] is None


@pytest.mark.parametrize("flags,bad", [
    (dict(model="propos=opus"), "propos"),
    (dict(effort="turbo"), "turbo"),
    (dict(effort="propose=fast"), "fast"),
    (dict(model="propose="), "nothing after it"),
])
def test_a_typo_is_refused_before_anything_runs(flags, bad):
    """`claude` ignores an effort level it does not recognise, so a stage that
    was supposed to be running at `high` and was not would be a measurement
    that quietly means nothing."""
    with pytest.raises(ValueError) as e:
        agent.Assignment.from_args(_args("work", **{**dict(model=None), **flags}))
    assert bad in str(e.value)


def test_the_command_line_refuses_it_too(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["propose", "--effort", "turbo"])
    assert "not a level" in str(e.value)


# --------------------------------------------------------------------------- #
# what reaches `claude`
# --------------------------------------------------------------------------- #


def test_the_default_passes_neither_flag(proposing, fake_claude, no_waiting):
    """This change alone must move nothing: no `--model`, no `--effort`, and
    `claude` decides exactly as it did yesterday."""
    work, deck = proposing
    cli._propose_one(deck, _args(work))
    argv = fake_claude.argv()
    assert "--model" not in argv
    assert "--effort" not in argv
    assert "--fallback-model" not in argv


def test_the_assignment_reaches_the_subprocess(proposing, fake_claude,
                                               no_waiting):
    work, deck = proposing
    cli._propose_one(deck, _args(work, model="propose=opus,recipe=sonnet",
                                 effort="propose=high"))
    argv = fake_claude.argv()
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "high"


def test_a_stage_nobody_named_is_left_at_the_default(proposing, fake_claude,
                                                     no_waiting):
    work, deck = proposing
    cli._propose_one(deck, _args(work, model="recipe=sonnet"))
    assert "--model" not in fake_claude.argv()


# --------------------------------------------------------------------------- #
# the record
# --------------------------------------------------------------------------- #


def test_state_records_what_was_asked_and_what_actually_ran(
        proposing, fake_claude, no_waiting, monkeypatch):
    work, deck = proposing
    monkeypatch.setenv("FAKE_CLAUDE_SESSION_MODEL", "claude-sonnet-9")
    cli._propose_one(deck, _args(work, model="opus", effort="high"))

    st = deck.state()["proposed"]
    assert st["model_asked"] == "opus"          # the alias we typed
    assert st["effort"] == "high"
    assert st["model_ran"] == "claude-sonnet-9"  # the model the log says ran
    assert "fallback" not in st


def test_a_run_with_no_flags_still_records_which_model_ran(
        proposing, fake_claude, no_waiting):
    """Most useful case of all: today every deck is made by whatever `claude`
    defaults to, and nothing anywhere says which model that was."""
    work, deck = proposing
    cli._propose_one(deck, _args(work))
    st = deck.state()["proposed"]
    assert st["model_asked"] is None
    assert st["model_ran"] == "claude-opus-5[1m]"


def test_a_fallback_that_fired_is_visible_afterwards(
        proposing, fake_claude, no_waiting, monkeypatch):
    """The trap this avoids: a deck silently completed on a weaker model,
    indistinguishable from the rest of the batch.  The session's model is on
    the init line and the model that produced the tokens is in `modelUsage`,
    so the two can be compared."""
    work, deck = proposing
    monkeypatch.setenv("FAKE_CLAUDE_SESSION_MODEL", "claude-opus-5[1m]")
    monkeypatch.setenv("FAKE_CLAUDE_WORKED_MODEL", "claude-haiku-4-5")
    cli._propose_one(deck, _args(work, model="opus",
                                 fallback_model="claude-haiku-4-5"))

    st = deck.state()["proposed"]
    assert st["model_asked"] == "opus"
    assert st["model_ran"] == "claude-haiku-4-5"
    assert st["fallback"] is True
    assert "--fallback-model" in fake_claude.argv()


def test_the_background_model_is_not_mistaken_for_the_one_that_worked(
        tmp_path):
    """Something small is billed on every run — a few dozen tokens for titles
    — so "which model ran" is the one that wrote the most, not one that merely
    appears."""
    log = tmp_path / "proposed.jsonl"
    log.write_text("\n".join([
        json.dumps({"type": "system", "subtype": "init",
                    "model": "claude-opus-5[1m]"}, separators=(",", ":")),
        json.dumps({"type": "result", "terminal_reason": "completed",
                    "modelUsage": {"claude-haiku-4-5": {"outputTokens": 20},
                                   "claude-opus-5[1m]": {"outputTokens": 22222}}},
                   separators=(",", ":"))]))
    got = agent.ran_as(log)
    assert got["model_ran"] == "claude-opus-5[1m]"
    assert "fallback" not in got


def test_a_result_record_is_still_found_when_the_log_is_long(tmp_path):
    """`modelUsage` grows with every model that ran, and the result is one
    line: clip its head and `json.loads` fails, which reads as "no opinion" —
    the run would be classified as a clean exit however it really ended."""
    log = tmp_path / "proposed.jsonl"
    filler = json.dumps({"type": "assistant", "text": "x" * 20000})
    log.write_text(filler + "\n" + json.dumps(
        {"type": "result", "terminal_reason": "api_error",
         "api_error_status": 429, "result": "rate limited",
         "modelUsage": {m: {"outputTokens": 10, "note": "y" * 200}
                        for m in ("a", "b", "c", "d", "e", "f")}},
        separators=(",", ":")))
    assert agent._infra_failure(log)["status"] == "infra"


# --------------------------------------------------------------------------- #
# staleness
# --------------------------------------------------------------------------- #


def test_the_same_assignment_is_still_a_cache_hit(proposing, fake_claude,
                                                  no_waiting):
    work, deck = proposing
    cli._propose_one(deck, _args(work, model="opus"))
    line = cli._propose_one(deck, _args(work, model="opus"))
    assert "already proposed" in line
    assert fake_claude() == 1


def test_an_artefact_made_by_another_model_stays_a_cache_hit_on_resume(
        proposing, fake_claude, no_waiting):
    """Changing a recovery lane must not erase a successful answer.

    A model comparison uses a fresh work tree or ``--force``.  Resume changes
    providers operationally, and the old provenance remains authoritative.
    """
    work, deck = proposing
    cli._propose_one(deck, _args(work))
    line = cli._propose_one(deck, _args(work, model="opus"))
    assert fake_claude() == 1
    assert "already proposed" in line
    assert deck.state()["proposed"]["model_asked"] is None


def test_force_can_refresh_a_stage_under_a_changed_assignment(
        proposing, fake_claude, no_waiting):
    work, deck = proposing
    cli._propose_one(deck, _args(work, model="opus"))
    line = cli._propose_one(
        deck, _args(work, model="opus", effort="high", force=True))
    assert fake_claude() == 2
    assert "opus → opus/high" in line


def test_a_deck_from_before_the_flag_is_left_alone(proposing, fake_claude,
                                                   no_waiting):
    """No `model_asked` means the artefact predates the record, not that it
    was made with the default — otherwise every deck in every existing work
    directory would re-run the moment this shipped."""
    work, deck = proposing
    deck.mark("proposed", "ok", tasks=0)
    assert cli._model_changed(deck, "proposed", _args(work, model="opus")) is None
    assert "already proposed" in cli._propose_one(deck, _args(work, model="opus"))
    assert fake_claude() == 0


def test_assignment_drift_is_visible_but_does_not_invalidate_state(
        proposing, fake_claude, no_waiting):
    work, deck = proposing
    cli._propose_one(deck, _args(work, model="opus"))
    assert deck.promoted("proposed")
    assert cli._model_changed(deck, "proposed", _args(work, model="sonnet"))
    assert cli._model_changed(deck, "proposed", _args(work, model="opus")) is None


def test_a_stage_with_no_model_at_all_never_looks_changed(proposing,
                                                          fake_claude,
                                                          no_waiting):
    """`--model opus` applies to agent stages; `degrade` has no model in it,
    so a bare value must not make every deterministic stage re-run."""
    _work, deck = proposing
    deck.mark("degraded", "ok", changes=3)
    assert cli._model_changed(deck, "degraded", _args("work", model="opus")) is None


# --------------------------------------------------------------------------- #
# the flags and the table
# --------------------------------------------------------------------------- #


def test_every_agent_command_takes_the_three_flags():
    ap = cli.build_parser()
    for cmd in ("propose", "recipe", "reconcile", "solvable"):
        args = ap.parse_args([cmd, "--model", "opus", "--effort", "high",
                              "--fallback-model", "sonnet"])
        assert (args.model, args.effort, args.fallback_model) == \
            ("opus", "high", "sonnet")
        blank = ap.parse_args([cmd])
        assert (blank.model, blank.effort, blank.fallback_model) == \
            (None, None, None)


def test_the_status_table_says_which_model_made_what(proposing, fake_claude,
                                                     no_waiting, monkeypatch,
                                                     capsys):
    work, deck = proposing
    monkeypatch.setenv("FAKE_CLAUDE_WORKED_MODEL", "claude-haiku-4-5")
    cli._propose_one(deck, _args(work, model="opus", effort="high"))
    cli._status_tail(_args(work), [deck])
    out = capsys.readouterr().out
    assert "model per stage" in out
    assert "claude-haiku-4-5/high" in out
    assert "FALLBACK" in out
