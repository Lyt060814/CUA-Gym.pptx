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

from pptxgym import agent as agentmod                             # noqa: E402
from pptxgym import foreman                                       # noqa: E402


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


def test_codex_effort_rounds_down_to_what_codex_has():
    assert agentmod._CODEX_EFFORT["xhigh"] == "high"
    assert agentmod._CODEX_EFFORT["max"] == "high"


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
    assert cmd[:3] == ["claude", "--agent", "proposer"]
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
