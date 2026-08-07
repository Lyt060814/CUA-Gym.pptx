"""What happens when the API, and not the deck, is what failed.

A batch takes hours and the rate limit is per *account*, so it is the one
constraint that adding machines cannot buy around: it will be hit, and today a
single 429 ends the deck and waits for a human to notice.  Everything here is
about the line between "the infrastructure flaked" — worth trying again — and
"the agent hit a real ceiling", where a retry buys the same answer twice.

Nothing here calls the real API: a fake `claude` on PATH decides how each
attempt ends, which is the same boundary `run_agent` shells out across.

    python3 -m pytest tests/test_retry.py -q
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import agent                                        # noqa: E402
from pptxgym import cli                                          # noqa: E402
from pptxgym import pipeline as pl                               # noqa: E402


FAKE_CLAUDE = '''
import json, os, sys, time

state = os.environ["FAKE_CLAUDE_STATE"]
with open(state, "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")     # every call, as it was invoked
n = sum(1 for _ in open(state))

fail_n = int(os.environ.get("FAKE_CLAUDE_FAIL", "0"))
kind = os.environ.get("FAKE_CLAUDE_KIND", "api_error")
out = os.environ.get("FAKE_CLAUDE_OUT", "")
body = os.environ.get("FAKE_CLAUDE_BODY", "{}")
partial = os.environ.get("FAKE_CLAUDE_PARTIAL", "")
sleep_s = float(os.environ.get("FAKE_CLAUDE_SLEEP", "0"))
if sleep_s:
    time.sleep(sleep_s)

failing = n <= fail_n
if out and not failing:
    open(out, "w").write(body)
if out and failing and partial:
    open(out, "w").write(partial)          # cut off mid-answer, as a 403 does

rec = {"type": "result", "num_turns": 3}
if failing:
    status = int(os.environ.get("FAKE_CLAUDE_STATUS",
                                "429" if kind == "api_error" else "401"))
    rec.update({"subtype": "error", "terminal_reason": kind,
                "api_error_status": status,
                "result": "simulated %s on call %d" % (kind, n)})
else:
    rec.update({"subtype": "success", "terminal_reason": "end_turn",
                "result": "wrote it"})
session = os.environ.get("FAKE_CLAUDE_SESSION_MODEL", "claude-opus-5[1m]")
worked = os.environ.get("FAKE_CLAUDE_WORKED_MODEL", session)
rec["modelUsage"] = {session: {"outputTokens": 40},
                     worked: {"outputTokens": 9000}}
print(json.dumps({"type": "system", "subtype": "init", "model": session},
                 separators=(",", ":")))
print(json.dumps(rec, separators=(",", ":")))
sys.exit(1 if failing else 0)
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A `claude` on PATH that fails the first N calls the way the API does."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE)
    script.chmod(0o755)
    state = tmp_path / "calls.txt"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(state))

    def calls() -> int:
        return len(_lines())

    def _lines() -> list:
        return state.read_text().splitlines() if state.exists() else []

    #: how the Nth call was invoked, which is the only honest way to check
    #: that a flag we did not set was really not passed
    calls.argv = lambda i=-1: json.loads(_lines()[i])
    return calls


@pytest.fixture
def no_waiting(monkeypatch):
    """Skip the backoff itself but keep a record of what it would have been."""
    waits: list[int] = []

    async def _hold(attempt, spec=None):
        waits.append(agent.backoff_seconds(
            attempt,
            getattr(spec, "backoff_step", None) or agent.BACKOFF_STEP,
            getattr(spec, "backoff_cap", None) or agent.BACKOFF_CAP))
        return 0

    monkeypatch.setattr(agent, "_hold_off", _hold)
    return waits


def _spec(tmp_path, **kw):
    kw.setdefault("timeout_min", 1)
    kw.setdefault("log", tmp_path / "proposed.jsonl")
    return agent.AgentRun("proposer", "do the thing", cwd=tmp_path, **kw)


def _run(spec):
    return asyncio.run(agent.run_agent(spec))


# --------------------------------------------------------------------------- #
# the retry itself
# --------------------------------------------------------------------------- #


def test_a_rate_limit_no_longer_ends_the_deck(tmp_path, fake_claude,
                                              no_waiting, monkeypatch):
    """The negative control in prose: two 429s in a row used to be the end of
    that deck's run, and a human had to notice and re-issue the command."""
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "2")
    res = _run(_spec(tmp_path, api_retries=3))
    assert res["status"] == "exited"
    assert fake_claude() == 3
    assert res["attempts"] == 3
    assert res["recovered"] is True


def test_the_budget_is_a_budget(tmp_path, fake_claude, no_waiting, monkeypatch):
    """`--api-retries 2` is three attempts, not four and not forever."""
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "99")
    res = _run(_spec(tmp_path, api_retries=2))
    assert fake_claude() == 3
    assert res["status"] == "infra"
    assert res["attempts"] == 3
    assert "gave up after 3 attempts" in res["why"]
    assert res.get("recovered") is None


def test_zero_retries_is_exactly_todays_behaviour(tmp_path, fake_claude,
                                                  no_waiting, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "99")
    res = _run(_spec(tmp_path, api_retries=0))
    assert fake_claude() == 1
    assert res["status"] == "infra"
    assert "retried" not in res


def test_the_backoff_is_the_one_the_batch_orchestrator_settled_on():
    """`min(30 * attempt, 120)`: three minutes of waiting over three retries,
    which outlasts a per-minute limit bucket several times over without
    spinning on a revoked key."""
    assert [agent.backoff_seconds(i) for i in range(1, 6)] == \
        [30, 60, 90, 120, 120]


def test_the_backoff_really_is_waited_between_attempts(tmp_path, fake_claude,
                                                       no_waiting, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "2")
    _run(_spec(tmp_path, api_retries=3))
    assert no_waiting == [30, 60]


# --------------------------------------------------------------------------- #
# what is *not* retried
# --------------------------------------------------------------------------- #


def test_a_timeout_is_never_retried(tmp_path, fake_claude, no_waiting,
                                    monkeypatch):
    """The agent was working and ran out of clock.  Re-running it costs the
    same clock again for the same answer — CUA-Gym's orchestrator declined to
    retry timeouts for exactly this reason and it is the right call."""
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "0")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "5")
    res = _run(_spec(tmp_path, api_retries=3, timeout_min=0.004))
    assert res["status"] == "timeout"
    assert fake_claude() == 1
    assert no_waiting == []


@pytest.mark.parametrize("why", ["max_turns", "max_tokens", "refusal"])
def test_a_ceiling_is_never_retried(why, tmp_path, fake_claude, no_waiting,
                                    monkeypatch):
    """A `max_turns` stop is not the infrastructure flaking; it is the agent
    hitting a limit it will hit again, at full price."""
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "99")
    monkeypatch.setenv("FAKE_CLAUDE_KIND", why)
    res = _run(_spec(tmp_path, api_retries=3))
    assert res["status"] == "truncated"
    assert res["kind"] == why
    assert fake_claude() == 1


def test_an_expired_login_is_confirmed_once_and_then_left_alone(
        tmp_path, fake_claude, no_waiting, monkeypatch):
    """Waiting cannot un-revoke a key.  One extra attempt covers the only
    thing waiting *can* fix — a token-refresh race — and thirty would only
    bury the one message a human has to see."""
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "99")
    monkeypatch.setenv("FAKE_CLAUDE_KIND", "auth_error")
    res = _run(_spec(tmp_path, api_retries=8))
    assert fake_claude() == 1 + agent.AUTH_RETRIES == 2
    assert res["status"] == "infra"
    assert res["kind"] == "auth_error"


def test_an_auth_blip_that_clears_still_recovers(tmp_path, fake_claude,
                                                 no_waiting, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    monkeypatch.setenv("FAKE_CLAUDE_KIND", "auth_error")
    res = _run(_spec(tmp_path, api_retries=3))
    assert res["status"] == "exited"
    assert res["attempts"] == 2


# --------------------------------------------------------------------------- #
# the evidence
# --------------------------------------------------------------------------- #


def test_every_attempt_keeps_its_own_log(tmp_path, fake_claude, no_waiting,
                                         monkeypatch):
    """The log is opened "w", so a retry would erase why the first attempt
    died — and that is the only thing separating an expired login from a lazy
    agent.  Two decks were once parked as `needs_human` for an expired login
    and the log is the whole reason we know that."""
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "2")
    res = _run(_spec(tmp_path, api_retries=3))

    kept = sorted(p.name for p in (tmp_path / "retries").iterdir())
    assert kept == ["proposed-try-01", "proposed-try-02"]
    for n in (1, 2):
        d = tmp_path / "retries" / f"proposed-try-{n:02d}"
        assert "simulated api_error on call %d" % n in \
            (d / "proposed.jsonl").read_text()
        assert (d / "proposed.stderr.log").exists()
    # and the canonical name holds the attempt that actually produced the work
    assert "wrote it" in (tmp_path / "proposed.jsonl").read_text()
    assert [h["kind"] for h in res["retried"]] == ["api_error"] * 2
    assert [h["backoff_s"] for h in res["retried"]] == [30, 60]


def test_a_second_run_of_the_stage_does_not_overwrite_the_first_evidence(
        tmp_path, fake_claude, no_waiting, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    _run(_spec(tmp_path, api_retries=3))
    (tmp_path / "calls.txt").unlink()
    _run(_spec(tmp_path, api_retries=3))
    assert sorted(p.name for p in (tmp_path / "retries").iterdir()) == \
        ["proposed-try-01", "proposed-try-02"]


def test_a_retry_log_never_lands_beside_the_deck(
        tmp_path, fake_claude, no_waiting, monkeypatch):
    """Anything reading the deck root counts what it finds there as an attempt
    that happened.  An outage is not an attempt, so its evidence goes under
    `retries/` and nowhere else."""
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "2")
    _run(_spec(tmp_path, api_retries=3, log=tmp_path / "reconciled.jsonl"))
    assert sorted(p.name for p in (tmp_path / "retries").iterdir()) == \
        ["reconciled-try-01", "reconciled-try-02"]
    assert sorted(p.name for p in tmp_path.glob("reconciled-*.jsonl")) == []


def test_a_half_written_artefact_does_not_survive_into_the_retry(
        tmp_path, fake_claude, no_waiting, monkeypatch):
    """A 403 can cut a run off *after* it has written a well-formed partial
    file — that is the failure `_infra_failure` exists for.  If the retry then
    dies without writing, the checker would judge the half-written one."""
    out = tmp_path / "proposal.json"
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    monkeypatch.setenv("FAKE_CLAUDE_OUT", str(out))
    monkeypatch.setenv("FAKE_CLAUDE_PARTIAL", '{"tasks": [{"name": "half')
    monkeypatch.setenv("FAKE_CLAUDE_BODY",
                       json.dumps({"tasks": [], "no_task_reason": "nothing"}))
    _run(_spec(tmp_path, api_retries=2, outputs=[out]))

    assert json.loads(out.read_text())["no_task_reason"] == "nothing"
    assert (tmp_path / "retries" / "proposed-try-01" / "proposal.json"
            ).read_text().startswith('{"tasks": [{"name": "half')


def test_an_artefact_from_an_earlier_run_is_not_ours_to_move(
        tmp_path, fake_claude, no_waiting, monkeypatch):
    """`task.json` predates a re-run of reconcile.  Sweeping a *previous*
    stage's good output into retries/ because this attempt died would destroy
    the very artefact the pipeline is standing on."""
    out = tmp_path / "task.json"
    out.write_text('{"verdict": "ready"}')
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "99")
    _run(_spec(tmp_path, api_retries=1, outputs=[out]))
    assert json.loads(out.read_text())["verdict"] == "ready"


# --------------------------------------------------------------------------- #
# what the pipeline does with it
# --------------------------------------------------------------------------- #


def _args(work, **kw):
    base = dict(work=str(work), deck=None, workers=1, cpu_workers=None,
                force=False, dpi=110, model=None, timeout=1, api_retries=3)
    base.update(kw)
    return argparse.Namespace(**base)


def _deck(work, name="deck0001"):
    d = pl.Deck(work / name)
    d.root.mkdir(parents=True)
    (d.root / "meta.json").write_text(
        json.dumps({"slides": 3, "name": "a deck", "checksum": "ab" * 8}))
    d.mark("inspected", "ok")
    return d


@pytest.fixture
def proposing(tmp_path, monkeypatch):
    """A work dir with one inspected deck, and a fake proposer for it."""
    work = tmp_path / "work"
    work.mkdir()
    deck = _deck(work)
    monkeypatch.setenv("FAKE_CLAUDE_OUT", str(deck.proposal))
    monkeypatch.setenv("FAKE_CLAUDE_BODY", json.dumps(
        {"tasks": [], "no_task_reason": "this deck yields nothing worth doing"}))
    return work, deck


def test_a_deck_that_limped_completes_and_says_so(proposing, fake_claude,
                                                  no_waiting, monkeypatch):
    """Requirement in one line: the deck finishes, and the record does not
    read like a clean run."""
    work, deck = proposing
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "2")
    line = cli._propose_one(deck, _args(work, api_retries=3))

    assert deck.status_of("proposed") == "ok"
    assert deck.state()["proposed"]["api_attempts"] == 3
    assert fake_claude() == 3
    assert "tasks" in line
    assert cli._api_attempts(deck.state()) == 3


def test_a_clean_deck_carries_no_retry_mark(proposing, fake_claude,
                                            no_waiting, monkeypatch):
    work, deck = proposing
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "0")
    cli._propose_one(deck, _args(work))
    assert "api_attempts" not in deck.state()["proposed"]
    assert cli._api_attempts(deck.state()) == 0


def test_an_exhausted_budget_still_parks_the_deck(proposing, fake_claude,
                                                  no_waiting, monkeypatch):
    """Retrying is not a licence to pretend the deck succeeded.  The stage is
    left unjudged — `infra`, not `failed` and certainly not `ok` — so a re-run
    picks it up and the repair loop never counts it."""
    work, deck = proposing
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "99")
    line = cli._propose_one(deck, _args(work, api_retries=2))

    assert deck.status_of("proposed") == "infra"
    assert not deck.promoted("proposed")
    assert deck.state()["proposed"]["api_attempts"] == 3
    assert "INFRA after 3 attempt(s)" in line


def test_the_status_table_shows_which_decks_limped(proposing, fake_claude,
                                                   no_waiting, monkeypatch,
                                                   capsys):
    """A deck that needed four attempts is a signal about the account's
    headroom, and one that reads identically to a clean run is a signal nobody
    ever gets."""
    work, deck = proposing
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "2")
    cli._propose_one(deck, _args(work, api_retries=3))
    cli._status_tail(_args(work), [deck])
    out = capsys.readouterr().out
    assert "1 deck(s) needed an API retry" in out
    assert "deck0001×3" in out


# --------------------------------------------------------------------------- #
# the flag
# --------------------------------------------------------------------------- #


def test_every_command_that_launches_an_agent_takes_the_flag():
    ap = cli.build_parser()
    for cmd in ("propose", "recipe", "reconcile", "solvable"):
        args = ap.parse_args([cmd, "--api-retries", "5"])
        assert args.api_retries == 5
        assert ap.parse_args([cmd]).api_retries == agent.API_RETRIES


# --------------------------------------------------------------------------- #
# what the pilot's own logs turned out to contain
#
# Ten of the ~100 agent runs in `work/` died on infrastructure.  Their exact
# shapes are worth pinning down, because two of the three were misclassified.
# --------------------------------------------------------------------------- #


def _log(tmp_path, record) -> Path:
    p = tmp_path / "proposed.jsonl"
    p.write_text(json.dumps({"type": "system", "subtype": "init",
                             "model": "claude-opus-5[1m]"},
                            separators=(",", ":")) + "\n"
                 + json.dumps({"type": "result", **record},
                              separators=(",", ":")) + "\n")
    return p


def test_a_403_is_an_auth_failure_whatever_the_cli_labels_it(tmp_path):
    """Every 403 in the pilot arrived as `api_error` reading "Failed to
    authenticate. API Error: 403 Request not allowed" — including the expired
    login that parked two decks.  Waiting has never fixed one, so it must not
    get the full budget just because the label says `api_error`."""
    log = _log(tmp_path, {"terminal_reason": "api_error", "is_error": True,
                          "api_error_status": 403,
                          "result": "Failed to authenticate. API Error: 403 "
                                    "Request not allowed"})
    got = agent._infra_failure(log)
    assert got["status"] == "infra"
    assert got["kind"] == "auth_error"
    assert "403" in got["why"]


def test_a_session_limit_keeps_its_reset_time_where_a_human_can_read_it(
        tmp_path):
    """Three minutes of backoff cannot outlast `resets 8:40pm`, and should not
    try.  The deck parks — but the reason has to survive into `status`."""
    log = _log(tmp_path, {"terminal_reason": "api_error", "is_error": True,
                          "api_error_status": 429,
                          "result": "You've hit your session limit · resets "
                                    "8:40pm (Asia/Shanghai)"})
    got = agent._infra_failure(log)
    assert got["kind"] == "api_error"
    assert "resets 8:40pm" in got["why"]


def test_an_aborted_stream_is_infrastructure_and_not_a_clean_exit(tmp_path):
    """Three pilot runs ended `aborted_streaming` / `error_during_execution`
    with no result text at all, and every one of them was read as a normal
    exit — so the checker judged whatever half-written file was on disk.  That
    is the exact laundering this classification exists to stop."""
    log = _log(tmp_path, {"terminal_reason": "aborted_streaming",
                          "subtype": "error_during_execution",
                          "is_error": True, "result": None})
    got = agent._infra_failure(log)
    assert got["status"] == "infra"
    assert got["kind"] == "aborted_streaming"


def test_an_aborted_stream_is_retried_like_any_other_flake(
        tmp_path, fake_claude, no_waiting, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "2")
    monkeypatch.setenv("FAKE_CLAUDE_KIND", "aborted_streaming")
    res = _run(_spec(tmp_path, api_retries=3))
    assert res["status"] == "exited"
    assert fake_claude() == 3


def test_a_403_is_capped_even_when_it_arrives_as_an_api_error(
        tmp_path, fake_claude, no_waiting, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "99")
    monkeypatch.setenv("FAKE_CLAUDE_KIND", "api_error")
    monkeypatch.setenv("FAKE_CLAUDE_STATUS", "403")
    res = _run(_spec(tmp_path, api_retries=8))
    assert fake_claude() == 1 + agent.AUTH_RETRIES == 2
    assert res["kind"] == "auth_error"


def test_a_429_still_gets_the_whole_budget(tmp_path, fake_claude, no_waiting,
                                           monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "99")
    monkeypatch.setenv("FAKE_CLAUDE_STATUS", "429")
    _run(_spec(tmp_path, api_retries=3))
    assert fake_claude() == 4
