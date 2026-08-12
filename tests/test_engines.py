"""The codex lane: same specs, different CLI, honestly mapped.

What these tests pin down is the *contract*, not codex's behaviour: which
command a spec turns into, how lane purity travels, and that the failure
classifiers read codex's event stream the way `_infra_failure` reads
claude's — with "unrecognisable" always meaning "no opinion", never "clean".
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym.orchestration import agent as agentmod                             # noqa: E402
from pptxgym.orchestration import foreman                                       # noqa: E402


# --------------------------------------------------------------------------- #
# command construction
# --------------------------------------------------------------------------- #


def test_codex_cmd_maps_what_maps_and_omits_what_does_not(monkeypatch):
    monkeypatch.delenv("PPTXGYM_SKIP_PERMISSIONS", raising=False)
    spec = agentmod.AgentRun("proposer", "do the thing", engine="codex",
                             model="gpt-5-codex", effort="high",
                             max_turns=80, fallback_model="sonnet")
    cmd = agentmod._codex_cmd(spec)
    assert cmd[:2] == ["codex", "exec"]
    assert "--json" in cmd
    assert "-m" in cmd and cmd[cmd.index("-m") + 1] == "gpt-5-codex"
    assert any("model_reasoning_effort" in c for c in cmd)
    # what has no codex equivalent must not leak through as fake flags
    joined = " ".join(cmd)
    assert "--max-turns" not in joined
    assert "--fallback-model" not in joined
    assert "--allowedTools" not in joined
    # outside a container the write boundary stays up
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"


def test_codex_cmd_opens_the_sandbox_only_inside_a_container(monkeypatch):
    monkeypatch.setenv("PPTXGYM_SKIP_PERMISSIONS", "1")
    spec = agentmod.AgentRun("proposer", "p", engine="codex")
    cmd = agentmod._codex_cmd(spec)
    assert cmd[cmd.index("--sandbox") + 1] == "danger-full-access"


def test_the_flag_on_the_spec_reaches_the_command(monkeypatch):
    """The child runs under `{**os.environ, **spec.env}`, so the command
    builder must read the same merge. It read bare os.environ on the first
    calibration run: the foreman's own environment had no
    PPTXGYM_SKIP_PERMISSIONS, every codex orchestrator got
    `--sandbox workspace-write`, the container's kernel refused bwrap a
    namespace, and all ten decks parked with every tool call dead."""
    monkeypatch.delenv("PPTXGYM_SKIP_PERMISSIONS", raising=False)
    spec = agentmod.AgentRun("proposer", "p", engine="codex",
                             env={"PPTXGYM_SKIP_PERMISSIONS": "1"})
    cmd = agentmod._codex_cmd(spec)
    assert cmd[cmd.index("--sandbox") + 1] == "danger-full-access"
    claude_cmd = agentmod._claude_cmd(spec)
    assert "--permission-mode" in claude_cmd


def test_codex_effort_passes_xhigh_through():
    """codex-cli 0.147 speaks xhigh; only claude's `max` still rounds."""
    assert agentmod._CODEX_EFFORT["xhigh"] == "xhigh"
    assert agentmod._CODEX_EFFORT["max"] == "xhigh"


def test_an_absent_flag_does_not_erase_a_builder_default():
    """The probe pins haiku on its own spec; Assignment.apply must only
    overwrite what a flag actually asked for."""
    spec = agentmod.AgentRun("solver-probe", "p", model="haiku")
    asked = agentmod.Assignment().apply(spec, "solvable")
    assert spec.model == "haiku"
    assert asked["model"] is None          # the record still says "not asked"
    agentmod.Assignment(model={"*": "sonnet"}).apply(spec, "solvable")
    assert spec.model == "sonnet"          # a real flag still wins


def test_the_probe_defaults_to_claude_haiku_whatever_the_lane(tmp_path,
                                                              monkeypatch):
    """The owner lane does not silently choose the independent witness."""
    import contextlib, types
    from pptxgym.commands import cli
    from pptxgym.core import pipeline as pl
    deck = types.SimpleNamespace(root=tmp_path, id="deck0001",
                                 done=lambda s: s == "reconciled")
    (tmp_path / "task.json").write_text("{}")
    ws = types.SimpleNamespace(dir=tmp_path, bundle=tmp_path / "bundle",
                               launcher=None, env={}, settings=None,
                               collect=None)
    captured = {}

    def grab(deck_, stage, spec_builder, checker, args,
             fixed_assignment=False):
        captured["spec"] = spec_builder(deck_)
        return "ok"

    monkeypatch.setenv(agentmod.ENGINE_ENV, "codex")
    monkeypatch.setattr(agentmod, "solvability_prompt", lambda d, w: "probe")
    monkeypatch.setattr(pl, "bundle", lambda d: None)
    monkeypatch.setattr(pl, "probe_workspace",
                        lambda d, engine="claude": contextlib.nullcontext(ws))
    monkeypatch.setattr(cli, "_agent_stage", grab)
    monkeypatch.setattr(cli, "_redo_note", lambda d, s, a: "")
    monkeypatch.setattr(cli, "_model_changed", lambda d, s, a: None)
    cli._solvable_one(deck, types.SimpleNamespace(force=False,
                                                   work=str(tmp_path)))
    assert captured["spec"].engine == "claude"
    assert captured["spec"].model == "haiku"


def test_the_probe_can_be_pinned_to_codex_independently(tmp_path, monkeypatch):
    import contextlib, types
    from pptxgym.commands import cli
    from pptxgym.core import pipeline as pl
    deck = types.SimpleNamespace(root=tmp_path, id="deck0001",
                                 done=lambda s: s == "reconciled")
    (tmp_path / "task.json").write_text("{}")
    ws = types.SimpleNamespace(dir=tmp_path, bundle=tmp_path / "bundle",
                               launcher=["setpriv"], env={}, settings=None,
                               collect=None)
    captured = {}

    def grab(deck_, stage, spec_builder, checker, args,
             fixed_assignment=False):
        captured["spec"] = spec_builder(deck_)
        captured["fixed"] = fixed_assignment
        return "ok"

    monkeypatch.setenv(agentmod.ENGINE_ENV, "claude")
    monkeypatch.setenv(agentmod.PROBE_ENGINE_ENV, "codex")
    monkeypatch.setenv(agentmod.PROBE_MODEL_ENV, "gpt-5.6-terra")
    monkeypatch.setenv(agentmod.PROBE_EFFORT_ENV, "medium")
    monkeypatch.setattr(agentmod, "solvability_prompt", lambda d, w: "probe")
    monkeypatch.setattr(pl, "bundle", lambda d: None)
    monkeypatch.setattr(pl, "probe_workspace",
                        lambda d, engine="claude": contextlib.nullcontext(ws))
    monkeypatch.setattr(cli, "_agent_stage", grab)
    monkeypatch.setattr(cli, "_redo_note", lambda d, s, a: "")
    monkeypatch.setattr(cli, "_model_changed", lambda d, s, a: None)

    cli._solvable_one(deck, types.SimpleNamespace(force=False,
                                                   work=str(tmp_path)))
    spec = captured["spec"]
    assert (spec.engine, spec.model, spec.effort) == \
        ("codex", "gpt-5.6-terra", "medium")
    assert captured["fixed"] is True
    assert "HF_TOKEN" in spec.unset_env and "GH_TOKEN" in spec.unset_env


def test_codex_probes_share_a_cross_process_slot_pool(tmp_path, monkeypatch):
    import types
    from pptxgym.commands import cli

    monkeypatch.setenv("PPTXGYM_PROBE_WORKERS", "2")
    deck = types.SimpleNamespace(id="deck0001")
    with cli._codex_probe_slot(tmp_path, deck) as first:
        with cli._codex_probe_slot(tmp_path, deck) as second:
            assert {first, second} == {1, 2}
    # Kernel locks are released by the context, while their harmless files
    # remain reusable for every independently spawned CLI process.
    with cli._codex_probe_slot(tmp_path, deck) as again:
        assert again in (1, 2)


def test_codex_probe_slot_count_fails_closed(monkeypatch):
    from pptxgym.commands import cli
    from pptxgym.core import pipeline as pl
    monkeypatch.setenv("PPTXGYM_PROBE_WORKERS", "many")
    with pytest.raises(pl.StageError, match="must be an integer"):
        cli._codex_probe_workers()
    monkeypatch.setenv("PPTXGYM_PROBE_WORKERS", "0")
    with pytest.raises(pl.StageError, match="at least 1"):
        cli._codex_probe_workers()


def test_the_agent_manual_travels_inside_the_prompt():
    """Codex has no `--agent`: the persona claude gets from
    .claude/agents/<name>.md must arrive as prompt text, frontmatter
    stripped — same words, different envelope."""
    manual = agentmod.agent_manual("orchestrator")
    assert manual, "orchestrator.md must exist and have a body"
    assert not manual.startswith("---"), "frontmatter must be stripped"
    spec = agentmod.AgentRun("orchestrator", "MISSION-TEXT", engine="codex")
    cmd = agentmod._codex_cmd(spec)
    prompt = cmd[cmd.index("--skip-git-repo-check") + 1]
    assert manual[:80] in prompt and "MISSION-TEXT" in prompt


def test_claude_cmd_is_unchanged_by_the_engine_field():
    spec = agentmod.AgentRun("proposer", "p", engine="claude",
                             model="opus", effort="high")
    cmd = agentmod._claude_cmd(spec)
    assert cmd[0] == "claude"
    assert cmd[cmd.index("--agent") + 1] == "proposer"
    definitions = json.loads(cmd[cmd.index("--agents") + 1])
    assert "proposer" in definitions
    assert "prompt" in definitions["proposer"]
    assert "--max-turns" in cmd


# --------------------------------------------------------------------------- #
# lane purity
# --------------------------------------------------------------------------- #


def test_engine_defaults_from_the_environment(monkeypatch):
    """The foreman sets PPTXGYM_ENGINE on the orchestrator; the CLI verbs it
    runs build their AgentRuns with no engine argument at all. This default
    is the whole mechanism of lane purity."""
    monkeypatch.setenv(agentmod.ENGINE_ENV, "codex")
    assert agentmod.AgentRun("proposer", "p").engine == "codex"
    monkeypatch.delenv(agentmod.ENGINE_ENV)
    assert agentmod.AgentRun("proposer", "p").engine == "claude"


def test_an_unknown_engine_is_an_error_not_a_silent_claude(monkeypatch):
    monkeypatch.setenv(agentmod.ENGINE_ENV, "gemini")
    with pytest.raises(ValueError):
        agentmod.AgentRun("proposer", "p")


# --------------------------------------------------------------------------- #
# the codex witnesses
# --------------------------------------------------------------------------- #


def _log(tmp_path, events):
    p = tmp_path / "run.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in events))
    return p


def test_codex_rate_limit_reads_as_infra(tmp_path):
    log = _log(tmp_path, [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "error", "message": "Rate limit reached, retry later"},
    ])
    got = agentmod._codex_infra(log)
    assert got["status"] == "infra" and got["kind"] == "api_error"


def test_codex_expired_login_reads_as_auth_error(tmp_path):
    log = _log(tmp_path, [
        {"type": "error", "message": "401 Unauthorized — please login"},
    ])
    got = agentmod._codex_infra(log)
    assert got["kind"] == "auth_error"


def test_codex_stream_death_is_not_a_clean_exit(tmp_path):
    """The claude-side lesson, re-learned in advance: a stream that died
    mid-answer left a well-formed partial file, and three pilot runs were
    read as clean exits. The codex parser must never repeat that."""
    log = _log(tmp_path, [
        {"type": "error", "message": "stream disconnected before completion"},
    ])
    got = agentmod._codex_infra(log)
    assert got["status"] == "infra" and got["kind"] == "aborted_streaming"


def test_an_unclassifiable_error_event_still_blocks(tmp_path):
    log = _log(tmp_path, [{"type": "error", "message": "computer says no"}])
    assert agentmod._codex_infra(log)["status"] == "infra"


def test_no_error_events_means_no_opinion(tmp_path):
    log = _log(tmp_path, [
        {"type": "item.completed", "item": {"type": "agent_message",
                                            "text": "done"}},
    ])
    assert agentmod._codex_infra(log) == {}


def test_codex_ran_as_finds_the_model_and_tokens(tmp_path):
    log = _log(tmp_path, [
        {"type": "session_configured", "model": "gpt-5-codex"},
        {"type": "turn.completed", "usage": {"input_tokens": 1200,
                                             "output_tokens": 345}},
    ])
    got = agentmod.codex_ran_as(log)
    assert got["model_ran"] == "gpt-5-codex"
    assert got["model_tokens"] == {"gpt-5-codex": 345}


# --------------------------------------------------------------------------- #
# the foreman's split
# --------------------------------------------------------------------------- #


def test_engine_split_assigns_in_deck_order():
    got = foreman.parse_engine_split("claude=2,codex=3", 5)
    assert got == ["claude", "claude", "codex", "codex", "codex"]


def test_engine_split_default_is_all_claude():
    assert foreman.parse_engine_split(None, 3) == ["claude"] * 3


def test_engine_split_shortfall_lands_on_the_first_named_lane():
    """The calibrated lane absorbs the rounding, not the one under trial."""
    got = foreman.parse_engine_split("claude=1,codex=1", 4)
    assert got == ["claude", "codex", "claude", "claude"]


def test_engine_split_refuses_unknown_engines():
    with pytest.raises(ValueError):
        foreman.parse_engine_split("claude=1,gemini=2", 3)


def test_mission_speaks_no_claude_model_names_to_a_codex_deck(tmp_path):
    """The ASSIGN table is claude vocabulary; a codex orchestrator told to
    pass `--model sonnet` would either crash its verb or silently run a
    model nobody chose."""
    class FakeDeck:
        id = "deck0001"
        root = tmp_path

        def meta(self):
            return {"slides": 10}
    text = foreman.mission(FakeDeck(), tmp_path, 220, foreman.ASSIGN,
                           wps=False, engine="codex")
    assert "--model sonnet" not in text and "--model opus" not in text
    text_claude = foreman.mission(FakeDeck(), tmp_path, 220, foreman.ASSIGN,
                                  wps=False, engine="claude")
    assert "--model sonnet" in text_claude


# --------------------------------------------------------------------------- #
# a lane whose quota belongs to somebody else too
# --------------------------------------------------------------------------- #


def test_the_codex_lane_waits_far_longer_than_the_private_one():
    """The relay's quota is shared with live rollouts, so a 429 there means
    "somebody else is busy", not "this deck is doomed". Four attempts three
    minutes apart lost 9 of 10 decks in the first calibration; batch work
    has no deadline and can afford to outlast a burst."""
    codex = agentmod.AgentRun("orchestrator", "p", engine="codex")
    claude = agentmod.AgentRun("orchestrator", "p", engine="claude")
    assert codex.api_retries > claude.api_retries
    patience = sum(agentmod.backoff_seconds(i, codex.backoff_step,
                                            codex.backoff_cap)
                   for i in range(1, codex.api_retries + 1))
    assert patience >= 30 * 60, "a codex deck should outlast a rollout burst"


def test_an_explicit_retry_budget_still_wins():
    spec = agentmod.AgentRun("orchestrator", "p", engine="codex",
                             api_retries=1, backoff_step=5, backoff_cap=5)
    assert spec.api_retries == 1
    assert agentmod.backoff_seconds(9, spec.backoff_step, spec.backoff_cap) == 5


def test_nested_verbs_inherit_the_patient_policy(monkeypatch):
    """A specialist verb builds its AgentRun with no retry argument, so the
    policy has to follow the engine the environment pins, not the call."""
    monkeypatch.setenv(agentmod.ENGINE_ENV, "codex")
    assert agentmod.AgentRun("proposer", "p").api_retries \
        == agentmod.SHARED_RETRIES


# --------------------------------------------------------------------------- #
# "the process exited" is not "the work is done"
# --------------------------------------------------------------------------- #


def _fake_once(results):
    """Replace _run_once with a scripted sequence of outcomes."""
    calls = []

    async def _once(spec, log):
        calls.append(spec.prompt)
        return results[min(len(calls) - 1, len(results) - 1)]
    return _once, calls


def test_a_clean_exit_with_work_outstanding_is_handed_back(monkeypatch):
    """codex exec ends when the model stops calling tools. Three of the first
    five muse-spark decks stopped at 15-16 minutes, none out of budget, none
    having written the REVIEW.md the brief asks for, one a single `package`
    away from shipping — and nothing pushed them."""
    import asyncio
    once, calls = _fake_once([{"status": "exited", "returncode": 0}])
    monkeypatch.setattr(agentmod, "_run_once", once)
    gaps = iter(["no bundle yet", "still no bundle", None])
    spec = agentmod.AgentRun("orchestrator", "BRIEF", engine="codex",
                             continuations=5, timeout_min=60,
                             unfinished=lambda: next(gaps, None))
    res = asyncio.run(agentmod.run_agent(spec))
    assert res["continued"] == 2
    assert len(calls) == 3
    assert "CONTINUATION 1" in calls[1] and "no bundle yet" in calls[1]
    # the brief travels with the continuation: an agent that has forgotten
    # its boundaries is more dangerous than one that stopped
    assert "BRIEF" in calls[1]
    assert spec.prompt == "BRIEF", "the spec must be left as it was found"


def test_a_finished_deck_is_never_handed_back(monkeypatch):
    import asyncio
    once, calls = _fake_once([{"status": "exited", "returncode": 0}])
    monkeypatch.setattr(agentmod, "_run_once", once)
    spec = agentmod.AgentRun("orchestrator", "BRIEF", engine="codex",
                             continuations=5, unfinished=lambda: None)
    res = asyncio.run(agentmod.run_agent(spec))
    assert "continued" not in res and len(calls) == 1


def test_continuations_stop_at_their_budget(monkeypatch):
    import asyncio
    once, calls = _fake_once([{"status": "exited", "returncode": 0}])
    monkeypatch.setattr(agentmod, "_run_once", once)
    spec = agentmod.AgentRun("orchestrator", "BRIEF", engine="codex",
                             continuations=2, timeout_min=60,
                             unfinished=lambda: "never done")
    res = asyncio.run(agentmod.run_agent(spec))
    assert res["continued"] == 2 and len(calls) == 3


def test_an_unanswerable_check_stops_nothing(monkeypatch):
    """The budget that governs is time, and a check that raises is not a
    reason to spend more of it."""
    import asyncio
    once, calls = _fake_once([{"status": "exited", "returncode": 0}])
    monkeypatch.setattr(agentmod, "_run_once", once)

    def _boom():
        raise OSError("state.json is unreadable")
    spec = agentmod.AgentRun("orchestrator", "BRIEF", engine="codex",
                             continuations=5, unfinished=_boom)
    asyncio.run(agentmod.run_agent(spec))
    assert len(calls) == 1


def test_a_timed_out_agent_is_not_handed_more_work(monkeypatch):
    """A timeout means the clock ran out, not that the agent stopped early."""
    import asyncio
    once, calls = _fake_once([{"status": "timeout"}])
    monkeypatch.setattr(agentmod, "_run_once", once)
    spec = agentmod.AgentRun("orchestrator", "BRIEF", engine="codex",
                             continuations=5, unfinished=lambda: "not done")
    asyncio.run(agentmod.run_agent(spec))
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# testimony needs a witness
# --------------------------------------------------------------------------- #


def _deck_with(tmp_path, state):
    from pptxgym.core import pipeline as pl
    root = tmp_path / "deck0001"
    root.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(json.dumps(state))
    return pl.Deck(root)


def test_hand_written_testimony_is_refused(tmp_path):
    """A muse-spark orchestrator hand-wrote task.json, ran the checker
    itself and marked reconcile ok — verdict, input fingerprints, everything
    a reader looks for, and none of the fields a run leaves behind (the real
    deck0003, whose REVIEW.md says so in words)."""
    deck = _deck_with(tmp_path, {
        "reconciled": {"status": "ok", "verdict": "ready", "assets": 5},
        "solvable": {"status": "ok", "verdict": "solvable",
                     "model_asked": "sonnet"},
    })
    why = foreman.unwitnessed(deck)
    assert "reconciled" in why


def test_a_witnessed_deck_passes(tmp_path):
    deck = _deck_with(tmp_path, {
        "reconciled": {"status": "ok", "model_asked": "opus",
                       "model_ran": "claude-opus-5"},
        "solvable": {"status": "ok", "model_asked": "sonnet"},
    })
    assert foreman.unwitnessed(deck) == ""


def test_a_codex_deck_with_no_model_name_is_not_accused(tmp_path):
    """The codex lane asks for no model, so `model_asked` is null and
    `model_ran` depends on parsing an engine's stream — which was wrong for
    exactly one release. Three honestly-run decks, each with a four-minute
    reconcile behind it, looked forged. A provenance rule must fail towards
    accusing nobody."""
    deck = _deck_with(tmp_path, {
        "reconciled": {"status": "ok", "model_asked": None, "effort": None,
                       "duration_ms": 238191, "verdict": "ready"},
        "solvable": {"status": "ok", "model_asked": None, "effort": None,
                     "duration_ms": 250021, "verdict": "solvable"},
    })
    assert foreman.unwitnessed(deck) == ""


def test_a_parked_stage_is_not_accused(tmp_path):
    """A deck that never claimed to pass reconcile is parked, not forged."""
    deck = _deck_with(tmp_path, {
        "reconciled": {"status": "failed", "error": "checker said no"},
    })
    assert foreman.unwitnessed(deck) == ""


def test_a_hand_written_proposal_is_refused_too(tmp_path):
    """Proposals were carved out of this rule and deck0004 is why they are
    back in. Its propose specialist succeeded with four degradations; four
    minutes later the orchestrator overwrote the record by hand with three,
    "after specialist failure". Nothing downstream can see the difference —
    a thin proposal scores 1.000/0.000 and survives every attack — so the
    only place it can be caught is here."""
    deck = _deck_with(tmp_path, {
        "proposed": {"status": "ok", "note": "hand-written proposal after "
                                             "specialist failure"},
        "recipe": {"status": "ok", "log": "/w/deck0004/recipe.jsonl"},
        "reconciled": {"status": "ok", "model_asked": "opus"},
        "solvable": {"status": "ok", "model_asked": "sonnet"},
    })
    assert "proposed" in foreman.unwitnessed(deck)


def test_a_proposal_a_specialist_wrote_passes(tmp_path):
    deck = _deck_with(tmp_path, {
        "proposed": {"status": "ok", "model_asked": None, "tasks": 1,
                     "log": "/w/deck0004/proposed.jsonl"},
        "recipe": {"status": "ok", "log": "/w/deck0004/recipe.jsonl"},
        "reconciled": {"status": "ok", "model_asked": "opus"},
        "solvable": {"status": "ok", "model_asked": "sonnet"},
    })
    assert foreman.unwitnessed(deck) == ""


def test_a_recipe_skipped_by_design_is_not_accused(tmp_path):
    """A deck whose proposal is empty by design records `recipe: skipped`,
    which claims nothing and so cannot be forged."""
    deck = _deck_with(tmp_path, {
        "proposed": {"status": "ok", "log": "/w/d/proposed.jsonl"},
        "recipe": {"status": "skipped", "reason": "proposal is empty by "
                                                 "design"},
    })
    assert foreman.unwitnessed(deck) == ""


# --------------------------------------------------------------------------- #
# the lane's patience reaches the specialists, and a block says why
# --------------------------------------------------------------------------- #


def test_an_unset_retry_flag_leaves_the_budget_to_the_lane():
    """The flag's default used to be written over every specialist's spec, so
    the shared lane's eight tries survived only on the orchestrator above
    them — the run's own `limits` records show `api_retries: 3` on every
    nested verb of a codex deck."""
    from argparse import Namespace

    from pptxgym.commands import cli

    assert cli._api_retries(Namespace()) is None
    assert cli._api_retries(Namespace(api_retries=None)) is None
    assert cli._api_retries(Namespace(api_retries=5)) == 5

    spec = agentmod.AgentRun("proposer", "x", engine="codex")
    if (budget := cli._api_retries(Namespace())) is not None:
        spec.api_retries = budget
    assert spec.api_retries == agentmod.SHARED_RETRIES


def test_a_blocked_stage_says_the_upstream_is_stale(tmp_path):
    """"skipped — not proposed" is true and useless when `proposed` sits
    there recorded ok. deck0004 read it, reached for `--force`, got the same
    line, and spent its last minutes in that loop."""
    from pptxgym.commands import cli
    from pptxgym.core import pipeline as pl

    root = tmp_path / "deck0004"
    root.mkdir(parents=True)
    (root / "digest.json").write_text('{"slides": []}')     # a real input
    (root / "state.json").write_text(json.dumps({
        "proposed": {"status": "ok", "note": "hand-written", "_in": {}},
    }))
    deck = pl.Deck(root)

    note = cli._upstream(deck, "proposed", "skipped — not proposed")
    assert "stale" in note and "digest.json" in note
    assert "by hand" in note and "no rerun of this stage can clear it" in note


def test_a_stage_that_never_ran_keeps_the_plain_message(tmp_path):
    from pptxgym.commands import cli
    from pptxgym.core import pipeline as pl

    root = tmp_path / "deck0004"
    root.mkdir(parents=True)
    deck = pl.Deck(root)
    assert cli._upstream(deck, "proposed", "skipped — not proposed") \
        == "skipped — not proposed"


def test_the_codex_model_is_read_from_the_head_of_a_long_log(tmp_path):
    """codex names its model in the first line and reports its failure in
    the last, so a tail-only reader saw neither on any real run."""
    head = {"type": "session_configured", "model": "muse-spark-1.1"}
    filler = [{"type": "item.completed",
               "item": {"type": "command_execution", "command": "x" * 400}}
              for _ in range(300)]
    log = _log(tmp_path, [head, *filler])
    assert log.stat().st_size > 2 * agentmod.RESULT_TAIL
    assert agentmod.codex_ran_as(log).get("model_ran") == "muse-spark-1.1"


def test_missing_outputs_are_a_gap_without_a_callback(tmp_path):
    """A codex specialist that exits cleanly leaving its promised file
    unwritten must read as unfinished, or the hand-back never fires."""
    out = tmp_path / "recipe.json"
    spec = agentmod.AgentRun("recipe-writer", "p", outputs=[out])
    gap = agentmod._still_missing(spec)
    assert gap and "recipe.json" in gap
    out.write_text("{}")
    assert agentmod._still_missing(spec) is None
    # a caller-supplied verdict still wins over the outputs check
    spec.unfinished = lambda: None
    (tmp_path / "other.json").write_text("{}")
    assert agentmod._still_missing(spec) is None
