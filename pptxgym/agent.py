"""Run a stage that needs judgement, as a headless `claude -p` subprocess.

Two rules learned the hard way and worth keeping:

**Point at the skill by absolute path and say "read it in full".**  Skill
auto-discovery is not something to rely on in headless mode; an explicit path
worked on every one of the seventeen agents run so far.

**Never let file existence mean success.**  The agent's job is done when the
file it wrote passes this pipeline's own checker, which is why every runner
takes a `check` callable and re-runs it after the process exits.

**Retry the infrastructure, never the answer.**  A 429, a 403 or an aborted
stream says nothing about the deck and ends it anyway; the rate limit is per
*account*, so it is the one constraint more machines cannot buy around, and on
a multi-hour batch it will be hit — ten of the ten-deck pilot's ~100 agent runs
died this way.  A timeout and a `max_turns` stop are the opposite: the agent
was working and reached a real ceiling, so a retry buys the same answer for a
second helping of the budget.  Only what `_infra_failure` names `infra` is
retried, and every attempt keeps its own log.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / ".claude" / "agents"
SKILLS = ROOT / ".claude" / "skills"

# --------------------------------------------------------------------------- #
# how hard to try again
#
# Three retries — four attempts — with `min(30 * attempt, 120)` seconds
# between them: the curve CUA-Gym's batch orchestrator settled on, and at most
# 30 + 60 + 90 = three minutes of waiting per deck.  The wait is the number to
# argue about, not the count.
#
# Three minutes is chosen against what the pilot's logs actually contain: an
# aborted stream, a 403 during a token refresh, a burst that clears in a
# minute.  It deliberately does *not* try to outlast the other thing in there
# — `You've hit your session limit · resets 8:40pm` — because waiting hours
# inside a stage would hold a pool slot for hours, and choosing to do that is
# a human's decision, not a retry's.  Those decks park with the reset time in
# their `why`, which is where `status` shows it.
#
# The retry itself is nearly free — an infra failure aborts in seconds and
# burns no turns — so the only real cost is the wait.
# --------------------------------------------------------------------------- #

API_RETRIES = 3

# An expired login does not un-expire on a timer.  It is retried *once*, and
# once only, because the one thing waiting can fix here is a token-refresh
# race — thirty attempts against a revoked key is thirty times the same 403,
# and it buries the one message a human has to see (two decks were once parked
# as `needs_human` for exactly this, and the log is how we know).  This cap
# covers any 403, not only the `auth_error` label: see `_infra_failure`.
AUTH_RETRIES = 1

BACKOFF_STEP = 30
BACKOFF_CAP = 120


def backoff_seconds(attempt: int) -> int:
    """How long to wait after attempt `attempt` failed."""
    return min(BACKOFF_STEP * attempt, BACKOFF_CAP)


# --------------------------------------------------------------------------- #
# which model runs which stage
#
# A knob, not an assignment.  All five agent stages are judgement — the
# mechanical work (degrade, materialise, score, harden, package) is Python
# with no model in it at all — so there is no stage that is obviously cheap,
# and nothing here picks one.  The default is what it has always been: no
# `--model` and no `--effort`, so `claude` decides and this file moves
# nothing until somebody changes one variable and measures it.
# --------------------------------------------------------------------------- #

#: what the command line calls a stage -> the key the pipeline records under.
#: Both spellings are accepted because both are what people type.
STAGE_OF = {"propose": "proposed", "proposed": "proposed",
            "recipe": "recipe",
            "reconcile": "reconciled", "reconciled": "reconciled",
            "solvable": "solvable"}

ROLES = ("propose", "recipe", "reconcile", "solvable")

#: `claude --effort <level>`.  Validated because a typo is silently ignored by
#: the CLI, and a stage that was supposed to be running at `high` and was not
#: is a measurement that quietly means nothing.
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def parse_per_stage(text: str | None, what: str = "model") -> dict:
    """`opus` — every stage — or `propose=opus,recipe=sonnet` — named ones.

    Returns stage-key -> value, with `"*"` holding the bare form.  A bare
    value is what `--model opus` has always meant and keeps meaning.
    """
    out: dict[str, str] = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        role, sep, val = part.partition("=")
        role, val = role.strip(), val.strip()
        if not sep:
            out["*"] = role
            continue
        if role not in STAGE_OF:
            raise ValueError(
                f"--{what}: {role!r} is not an agent stage. "
                f"Pick from {', '.join(ROLES)}, or give a bare value to set "
                f"every stage at once.")
        if not val:
            raise ValueError(f"--{what}: {role}= with nothing after it")
        if what == "effort" and val not in EFFORTS:
            raise ValueError(
                f"--effort: {val!r} is not a level. Pick from "
                f"{', '.join(EFFORTS)}.")
        out[STAGE_OF[role]] = val
    if what == "effort" and out.get("*") not in (None, *EFFORTS):
        raise ValueError(f"--effort: {out['*']!r} is not a level. Pick from "
                         f"{', '.join(EFFORTS)}.")
    return out


@dataclass
class Assignment:
    """Which model, effort and fallback each agent stage asks for."""

    model: dict = field(default_factory=dict)
    effort: dict = field(default_factory=dict)
    fallback: dict = field(default_factory=dict)

    @classmethod
    def from_args(cls, args) -> "Assignment":
        return cls(parse_per_stage(getattr(args, "model", None), "model"),
                   parse_per_stage(getattr(args, "effort", None), "effort"),
                   parse_per_stage(getattr(args, "fallback_model", None),
                                   "fallback-model"))

    def for_stage(self, stage: str) -> dict:
        key = STAGE_OF.get(stage, stage)
        return {"model": self.model.get(key, self.model.get("*")),
                "effort": self.effort.get(key, self.effort.get("*")),
                "fallback_model": self.fallback.get(key,
                                                    self.fallback.get("*"))}

    def apply(self, spec: "AgentRun", stage: str) -> dict:
        """Put this stage's assignment on the spec, and say what it was."""
        got = self.for_stage(stage)
        spec.model = got["model"]
        spec.effort = got["effort"]
        spec.fallback_model = got["fallback_model"]
        return got


#: Which CLI runs the agent.  Read from the environment by default so lane
#: purity is free: the foreman sets `PPTXGYM_ENGINE` on the orchestrator it
#: spawns, the orchestrator's Bash inherits it, and every specialist verb it
#: runs lands on the same engine without any flag being passed around.
ENGINE_ENV = "PPTXGYM_ENGINE"
ENGINES = ("claude", "codex")


def default_engine() -> str:
    got = os.environ.get(ENGINE_ENV, "claude").strip() or "claude"
    if got not in ENGINES:
        raise ValueError(f"{ENGINE_ENV}={got!r}: pick from {ENGINES}")
    return got


@dataclass
class AgentRun:
    name: str                 # .claude/agents/<name>.md
    prompt: str
    cwd: Path = ROOT
    max_turns: int = 60
    timeout_min: int = 30
    model: str | None = None
    #: which CLI runs this agent — see `default_engine`.
    engine: str = field(default_factory=default_engine)
    allowed_tools: list[str] = field(
        default_factory=lambda: ["Bash", "Read", "Write", "Edit", "Glob", "Grep"])
    log: Path | None = None
    api_retries: int = API_RETRIES
    effort: str | None = None
    #: `claude --fallback-model`.  Only safe because `ran_as` can see, after
    #: the fact, which model actually produced the work: a deck silently
    #: finished on a weaker model would otherwise be an artefact we could
    #: never tell apart from the rest of the batch.
    fallback_model: str | None = None
    #: what this run is expected to write.  A run cut off by a 403 can leave a
    #: well-formed *partial* file behind — that is the failure `_infra_failure`
    #: exists for — and if the retry then dies without writing, the checker
    #: would judge the half-written one and pass it.  Named here, it is moved
    #: aside with the log it belongs to.
    outputs: list[Path] = field(default_factory=list)
    #: argv the agent is launched *through*.  The solvability probe runs inside
    #: a mount namespace where the answer key does not exist; that is a wrapper
    #: around the same command rather than a different command, and keeping it
    #: here means nothing else in this file has to know it happened.
    launcher: list[str] = field(default_factory=list)
    #: added to the environment, not replacing it.
    env: dict = field(default_factory=dict)
    #: `--add-dir`.  A stage whose cwd is not the repository still has to be
    #: able to read the skill that is its manual.
    add_dirs: list[Path] = field(default_factory=list)
    #: `--settings`, as JSON.  A permission deny rule is enforced by the
    #: harness before the tool runs, which is a barrier rather than a request.
    settings: str | None = None
    #: run after the process exits, however it exited.  An agent working in a
    #: directory of its own writes its answer there, and this is what brings it
    #: back; it runs per attempt, so a retry's archive holds that attempt's own
    #: file rather than its predecessor's.
    collect: object = None


async def run_agent(spec: AgentRun) -> dict:
    """Run the agent, retrying only when it was the API that failed.

    Returns the last attempt's result, plus `attempts`, plus — when there was
    more than one — `retried` (the evidence of each) and `recovered`.  A run
    that limped through on its fourth attempt is a fact about the account's
    headroom, and one that reads identically to a clean run is a fact nobody
    ever learns.
    """
    log = spec.log or (spec.cwd / f"{spec.name}.jsonl")
    history: list[dict] = []
    attempt = 1
    while True:
        before = {p: _stamp(p) for p in spec.outputs}
        res = await _run_once(spec, log)
        if res["status"] != "infra":
            break                     # a verdict, a timeout, or a ceiling hit
        if attempt > _retries_allowed(spec, res.get("kind")):
            break
        history.append({"attempt": attempt, "kind": res.get("kind"),
                        "why": res.get("why"),
                        "kept": _keep_attempt(log, spec, attempt, before),
                        "backoff_s": backoff_seconds(attempt)})
        await _hold_off(attempt)
        attempt += 1
    res["attempts"] = attempt
    if history:
        res["retried"] = history
        if res["status"] == "infra":
            res["why"] = (f"{res.get('why', '')} — gave up after "
                          f"{attempt} attempts").strip()
        else:
            res["recovered"] = True
    return res


def _retries_allowed(spec: AgentRun, kind: str | None) -> int:
    budget = max(0, int(spec.api_retries or 0))
    return min(budget, AUTH_RETRIES) if kind == "auth_error" else budget


async def _hold_off(attempt: int) -> int:
    """The wait between attempts, as its own function so a test can skip it."""
    delay = backoff_seconds(attempt)
    await asyncio.sleep(delay)
    return delay


def _stamp(p: Path) -> tuple | None:
    """Enough of a file to tell whether an attempt rewrote it.

    A wall-clock comparison was the obvious way and the wrong one: file
    timestamps are quantised — 10ms here — so a file written *after* the
    attempt started can carry an mtime a few milliseconds before it, and the
    evidence is silently left behind.  Before-and-after needs no clock.
    """
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _keep_attempt(log: Path, spec: AgentRun, attempt: int,
                  before: dict) -> str:
    """Move a dead attempt's evidence out of the way of the next one.

    The log is opened `"w"`, so without this the retry erases the reason the
    first attempt died — and that reason is the only thing separating an
    expired login from a lazy agent.  It goes to `retries/<stem>-try-NN/`
    rather than beside the log on purpose: a log left beside the deck reads as
    an attempt that happened, and an outage is not an attempt.
    """
    n = attempt
    while (dest := log.parent / "retries" / f"{log.stem}-try-{n:02d}").exists():
        n += 1
    dest.mkdir(parents=True, exist_ok=True)
    # the log and its stderr are this attempt's by construction; an output file
    # is only ours if this attempt actually wrote it — one that predates the
    # attempt belongs to a stage that already succeeded, and moving it would
    # destroy the artefact the pipeline is standing on
    mine = [log, log.with_suffix(".stderr.log")]
    mine += [p for p in spec.outputs if _stamp(p) not in (None, before.get(p))]
    for src in mine:
        try:
            src.rename(dest / src.name)
        except OSError:
            pass
    return str(dest)


#: What a launcher exits with when it could not establish the barrier it was
#: put there to establish.  It is a distinct status all the way up rather than
#: a failed-looking run, because "the probe could not be sealed off" and "the
#: probe had nothing to say" want opposite responses from a human.
BARRIER_FAILED = 97


def _spec_env(spec: AgentRun, key: str) -> str | None:
    """A flag as the *child* will see it: spec.env over this process's env.

    The command builders decide flags for a subprocess that runs under
    `{**os.environ, **spec.env}` — reading bare `os.environ` here chose
    `--sandbox workspace-write` for every codex orchestrator on the first
    calibration run, whose container cannot create bwrap namespaces, and all
    ten decks parked with every tool call dead."""
    return (spec.env or {}).get(key, os.environ.get(key))


def _claude_cmd(spec: AgentRun) -> list[str]:
    cmd = ["claude", "--agent", spec.name, "-p", spec.prompt,
           "--max-turns", str(spec.max_turns),
           "--output-format", "stream-json", "--verbose",
           "--allowedTools", ",".join(spec.allowed_tools)]
    for d in spec.add_dirs:
        cmd += ["--add-dir", str(d)]
    if spec.settings:
        cmd += ["--settings", spec.settings]
    if spec.model:
        cmd += ["--model", spec.model]
    if spec.effort:
        cmd += ["--effort", spec.effort]
    if spec.fallback_model:
        # `--fallback-model` only works with --print, which is what -p is
        cmd += ["--fallback-model", spec.fallback_model]
    if _spec_env(spec, "PPTXGYM_SKIP_PERMISSIONS") == "1":
        cmd += ["--permission-mode", "dontAsk"]
    return cmd


def agent_manual(name: str) -> str:
    """The agent definition's body, frontmatter stripped.

    Codex has no `--agent`: the persona that `.claude/agents/<name>.md`
    installs for `claude` has to travel inside the prompt instead.  Same
    words, different envelope — the manuals themselves stay single-source.
    """
    p = AGENTS / f"{name}.md"
    try:
        text = p.read_text()
    except OSError:
        return ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


#: `codex exec -c model_reasoning_effort=...` accepts these; anything else
#: (claude's `xhigh`/`max`) rounds down to the nearest thing codex has.
_CODEX_EFFORT = {"low": "low", "medium": "medium", "high": "high",
                 "xhigh": "high", "max": "high", "minimal": "minimal"}


def _codex_cmd(spec: AgentRun) -> list[str]:
    """The codex equivalent of `_claude_cmd`, mapped honestly.

    What does not map is left out rather than faked: codex has no turn cap
    (the wall clock in `timeout_min` is the budget), no `--allowedTools`
    (its sandbox is the boundary), and no fallback model.  The agent manual
    is prepended to the prompt because there is no `--agent`.
    """
    manual = agent_manual(spec.name)
    prompt = (f"{manual}\n\n---\n\n{spec.prompt}" if manual else spec.prompt)
    cmd = ["codex", "exec", "--json", "--skip-git-repo-check", prompt]
    if spec.model:
        cmd += ["-m", spec.model]
    if spec.effort:
        cmd += ["-c", f'model_reasoning_effort="{_CODEX_EFFORT[spec.effort]}"']
    # Same trust decision as `--permission-mode dontAsk` on the claude side:
    # inside a disposable container the sandbox would only break the nested
    # verbs (they need network for their own model calls); on a workstation
    # the write boundary stays up.
    if _spec_env(spec, "PPTXGYM_SKIP_PERMISSIONS") == "1":
        cmd += ["--sandbox", "danger-full-access"]
    else:
        cmd += ["--sandbox", "workspace-write"]
    return cmd


async def _run_once(spec: AgentRun, log: Path) -> dict:
    cmd = _codex_cmd(spec) if spec.engine == "codex" else _claude_cmd(spec)
    cmd = [*spec.launcher, *cmd]

    log.parent.mkdir(parents=True, exist_ok=True)
    err = log.with_suffix(".stderr.log")
    with open(log, "w") as lf, open(err, "w") as ef:
        ef.write(f"$ {spec.engine} agent={spec.name} <prompt>\n\n"
                 f"{spec.prompt}\n")
        ef.write("=" * 60 + "\n")
        ef.flush()
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=lf, stderr=ef, cwd=str(spec.cwd),
            env={**os.environ, **spec.env} if spec.env else None)
        try:
            await asyncio.wait_for(proc.wait(), timeout=spec.timeout_min * 60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            _collect(spec)
            return {"status": "timeout", "log": str(log)}
    _collect(spec)
    if spec.launcher and proc.returncode == BARRIER_FAILED:
        return {"status": "barrier", "log": str(log),
                "why": _tail(err) or "the launcher could not establish the "
                                     "barrier the probe runs behind"}
    if spec.engine == "codex":
        return {"status": "exited", "returncode": proc.returncode,
                "log": str(log), **codex_ran_as(log),
                **_codex_infra(log, proc.returncode)}
    return {"status": "exited", "returncode": proc.returncode, "log": str(log),
            **ran_as(log), **_infra_failure(log)}


def _collect(spec: AgentRun) -> None:
    """Bring back what the agent wrote where it was working.

    Never fatal: a collector that raises here would turn "the agent produced
    nothing" into a crash, and the checker is the thing entitled to say what a
    missing answer means.
    """
    if not callable(spec.collect):
        return
    try:
        spec.collect()
    except OSError:
        pass


def _tail(err: Path, n: int = 400) -> str:
    try:
        return err.read_text(errors="replace").strip()[-n:]
    except OSError:
        return ""


def _infra_failure(log: Path) -> dict:
    """How the run ended, when that is not "it finished".

    The stage checkers judge the *shape* of the report, which a half-written
    one satisfies perfectly.  One probe was cut off by a 403 after writing a
    well-formed verdict; the checker passed it, and it read `solvable, no
    leaks` where the finished re-run read `undetermined, 2 leaks`.  Nothing
    downstream could have caught that — the only evidence is here.

    A 403 or a rate limit is also indistinguishable from a lazy agent (no
    output file at all), and two decks were parked as `needs_human` for an
    expired login.  Infrastructure failure has to be named as such, or the
    pipeline fills up with content judgements it never actually made.
    """
    d = last_result(log)
    if d:
        why = d.get("terminal_reason")
        if why == "aborted_streaming":
            # The stream died mid-answer: `is_error`, no result text, and
            # `subtype: error_during_execution`.  Three of the ten-deck
            # pilot's runs ended this way and every one of them was read as a
            # clean exit, which handed the checker whatever half-file was on
            # disk — the exact laundering this function exists to stop.
            return {"status": "infra", "kind": "aborted_streaming",
                    "why": "the response stream was aborted mid-run; nothing "
                           "it wrote is a finished answer"}
        if why in ("api_error", "auth_error"):
            # `kind` is what decides how hard it is worth trying again: a rate
            # limit clears on its own, a revoked key does not.
            #
            # A 403 counts as the second kind whatever the CLI calls it. Every
            # 403 in the pilot came through as `api_error` reading "Failed to
            # authenticate. API Error: 403 Request not allowed" — the expired
            # login that parked two decks was one of these — and no amount of
            # waiting has ever fixed one.
            kind = "auth_error" if (why == "auth_error"
                                    or d.get("api_error_status") == 403) \
                else "api_error"
            return {"status": "infra", "kind": kind,
                    "why": f"{d.get('api_error_status') or ''} "
                           f"{str(d.get('result'))[:120]}".strip()}
        if why in ("max_turns", "max_tokens", "refusal"):
            return {"status": "truncated", "kind": why,
                    "why": f"the run stopped on {why} — whatever it wrote is "
                           f"an interrupted answer, not a finished one"}
    return {}


#: How much of the log's tail to read looking for the result record.  It is
#: one line and carries `modelUsage`, so it grows with every model that ran —
#: measured at 1.6–2.4 KB today, and a `--fallback-model` adds another entry.
#: Clip its head and `json.loads` fails, which reads as "no opinion": the run
#: would be classified as a clean exit however it really ended.
RESULT_TAIL = 32000


def last_result(log: Path) -> dict:
    """The `type: result` record `claude` ends a run with, or `{}`."""
    try:
        tail = log.read_text(errors="replace")[-RESULT_TAIL:]
    except OSError:
        return {}
    for line in reversed(tail.splitlines()):
        if '"type":"result"' not in line:
            continue
        try:
            return json.loads(line[line.index("{"):])
        except (ValueError, json.JSONDecodeError):
            return {}
    return {}


def ran_as(log: Path) -> dict:
    """Which model actually did the work, from the log rather than the flag.

    `--model` is a request and `--fallback-model` is permission to ignore it.
    Recording the request alone would mean a deck quietly finished on a weaker
    model is an artefact nobody can tell apart from the rest of the batch —
    which is the whole reason to record anything here.  Two independent
    witnesses in the log: the session's model on the `system/init` line, and
    every model that actually produced tokens in the result's `modelUsage`.

    The little background model is in `modelUsage` on every run (a few dozen
    tokens, for titles and the like), so "the model that ran" is the one that
    wrote the most output, not merely one that appears.
    """
    out: dict = {}
    try:
        with open(log, errors="replace") as fh:
            head = fh.readline()
    except OSError:
        return {}
    try:
        d = json.loads(head)
        if d.get("type") == "system" and d.get("model"):
            out["model_session"] = d["model"]
    except (ValueError, json.JSONDecodeError):
        pass
    usage = last_result(log).get("modelUsage") or {}
    if isinstance(usage, dict) and usage:
        worked = max(usage.items(),
                     key=lambda kv: (kv[1] or {}).get("outputTokens", 0)
                     if isinstance(kv[1], dict) else 0)
        out["model_ran"] = worked[0]
        out["model_tokens"] = {k: (v or {}).get("outputTokens", 0)
                               for k, v in usage.items() if isinstance(v, dict)}
    ran, session = out.get("model_ran"), out.get("model_session")
    if ran and session and ran != session:
        # the fallback fired, or the session was re-routed. Either way this
        # deck was not made by the model we asked for, and that is the fact
        # the record exists to keep.
        out["fallback"] = True
    return out


# --------------------------------------------------------------------------- #
# the codex witnesses
#
# Same two questions `ran_as`/`_infra_failure` answer for claude — which model
# actually worked, and did the run end for a reason that is about the API
# rather than the deck — read from codex's `--json` event stream instead.
# The parsers are deliberately shape-tolerant: the event schema has changed
# between codex releases, and a parser that recognises nothing must read as
# "no opinion", never as "clean exit".
# --------------------------------------------------------------------------- #


def _codex_events(log: Path) -> list[dict]:
    try:
        text = log.read_text(errors="replace")[-RESULT_TAIL:]
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except (ValueError, json.JSONDecodeError):
            continue
    return out


def _walk_strings(obj, depth: int = 0):
    if depth > 6:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v, depth + 1)


def _find_key(obj, key: str, depth: int = 0):
    if depth > 6 or not isinstance(obj, (dict, list)):
        return None
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], (str, int, dict)):
            return obj[key]
        vals = obj.values()
    else:
        vals = obj
    for v in vals:
        got = _find_key(v, key, depth + 1)
        if got is not None:
            return got
    return None


def codex_ran_as(log: Path) -> dict:
    """Which model codex says it used, plus token totals when present."""
    out: dict = {}
    for ev in _codex_events(log):
        model = _find_key(ev, "model")
        if isinstance(model, str) and model and "model" not in out:
            out["model_session"] = out["model_ran"] = model
        usage = _find_key(ev, "usage") or _find_key(ev, "token_count")
        if isinstance(usage, dict):
            tok = (usage.get("output_tokens") or usage.get("outputTokens")
                   or (usage.get("total_token_usage") or {}).get(
                       "output_tokens"))
            if isinstance(tok, int):
                out["model_tokens"] = {out.get("model_ran", "codex"): tok}
    return out


#: substring -> (status, kind).  Matched lowercase, most specific first.
_CODEX_FAILURES = (
    ("401", ("infra", "auth_error")),
    ("unauthorized", ("infra", "auth_error")),
    ("not logged in", ("infra", "auth_error")),
    ("login", ("infra", "auth_error")),
    ("403", ("infra", "auth_error")),
    ("usage limit", ("infra", "api_error")),
    ("rate limit", ("infra", "api_error")),
    ("429", ("infra", "api_error")),
    ("quota", ("infra", "api_error")),
    ("stream disconnected", ("infra", "aborted_streaming")),
    ("stream error", ("infra", "aborted_streaming")),
    ("connection", ("infra", "api_error")),
    ("internal server error", ("infra", "api_error")),
    ("500", ("infra", "api_error")),
    ("context window", ("truncated", "max_tokens")),
    ("token limit", ("truncated", "max_tokens")),
)


def _codex_infra(log: Path, returncode: int | None = None) -> dict:
    """Name the runs that ended on the API rather than on an answer.

    Codex marks trouble with `error`-typed events (and mirrors some to
    stderr); the last one wins, same as claude's `type: result`.  A nonzero
    exit with no recognisable error stays unclassified on purpose — the
    stage checker judges what is on disk, which is the same treatment an
    unrecognised claude exit gets.
    """
    last_err = ""
    for ev in _codex_events(log):
        t = ev.get("type") or _find_key(ev, "type")
        if isinstance(t, str) and "error" in t.lower():
            # everything textual in the event except the type tag itself
            msgs = [s for s in _walk_strings(ev)
                    if s and "error" not in s.lower().split(".")]
            last_err = " ".join(msgs)[:300] or "error event with no message"
    if not last_err:
        return {}
    low = last_err.lower()
    for needle, (status, kind) in _CODEX_FAILURES:
        if needle in low:
            return {"status": status, "kind": kind, "why": last_err[:200]}
    # an error event we cannot classify is still not a finished answer
    return {"status": "infra", "kind": "api_error", "why": last_err[:200]}


def skill_path(name: str) -> str:
    p = SKILLS / name / "SKILL.md"
    if not p.exists():
        raise FileNotFoundError(f"no skill {name!r} at {p}")
    return str(p)


def propose_prompt(deck) -> str:
    return f"""Propose computer-use RL tasks from one real PPT deck.

FIRST: read {skill_path('ppt-task-proposal')} in full and follow it exactly.
It defines the judgement criteria, the difficulty calibration, the output JSON
schema and the hard rules.

Deck materials:
- structural digest (JSON): {deck.digest_min}
  (large decks: read it with offset/limit rather than all at once)
- page renders, one per slide, p-01.png …: {deck.renders}/

Read the digest AND look at every render before deciding. The digest carries
text formatting, table cell grids, SmartArt nodes, connector topology, z-order
and overlaps, grouping, layout/master identity, speaker notes, transitions,
animation build steps, motion paths, and `hard_target` flags marking shapes
nobody can recreate through a GUI. Use all of it; hard_target shapes make fine
context and terrible targets.

Write the JSON — exactly the schema in the skill, nothing else — to:
  {deck.proposal}

Then reply with one line: task count, difficulty, total est_steps. Do not
paste the JSON into your reply."""


def reconcile_prompt(deck) -> str:
    import json as _json
    task = (_json.loads(deck.proposal.read_text()).get("tasks") or [{}])[0]
    pages = sorted({p for g in task.get("degradations") or []
                    for p in g.get("slides", [])})

    # What `materialise` could not produce, stated rather than left in a file.
    #
    # `check_reconcile` rejects the deck when an unmet asset is neither
    # answered with `needs_rework` nor *named in prose* somewhere in the task
    # record — and it matches by substring, so the record has to contain the
    # literal word `reference_image`. Neither this prompt nor the reconcile
    # skill said so: the manifest was handed over as a path among six, the
    # requirement lived only inside the gate, and four decks across three runs
    # were rejected for failing to acknowledge something they were never told
    # about. An agent cannot meet a contract nobody states.
    unmet = []
    man = deck.root / "assets" / "manifest.json"
    if man.exists():
        try:
            unmet = _json.loads(man.read_text()).get("unmet") or []
        except (OSError, ValueError):
            unmet = []
    could_not = ""
    if unmet:
        lines = "\n".join(
            f"  - {u.get('kind')} for slide(s) {u.get('slides')}: "
            f"{str(u.get('why'))[:160]}" for u in unmet[:8])
        kinds = ", ".join(sorted({str(u.get("kind")) for u in unmet}))
        could_not = f"""
MATERIALISE COULD NOT PRODUCE {len(unmet)} ASSET(S) THE TASK PROMISES:

{lines}

The instruction offers these to the solver and they do not exist. Do exactly
one of the following — doing neither is a rejection:

  (a) set `verdict` to "needs_rework", with a `rework` entry naming the stage
      that has to change (proposed / recipe / materialise) and what, or
  (b) deal with it in the record and say so — the words {kinds} must appear
      literally in `notes`, or in the note on the degradation that wanted
      them, because a substring match is how the gate reads this.

What you may not do is quietly drop the promise from the instruction and say
nothing about it.
"""

    return f"""Reconcile one degraded PPT task against its own instruction and
assets, then write the final task record.
{could_not}
FIRST: read {skill_path('ppt-task-reconcile')} in full and follow it exactly.

Your deck: {deck.id}  ({deck.meta().get('name')}, {deck.meta().get('slides')} slides)
  proposal : {deck.proposal}
  recipe   : {deck.recipe}          (every step names its `deg` and explains
                                     itself in `_why` — read both)
  delta    : {deck.delta}
  assets   : {deck.root / 'assets' / 'manifest.json'}
  write to : {deck.root / 'task.json'}

Affected pages: {pages}
Render them before judging anything:
  python -m pptxgym.tools pair {deck.root} {' '.join(str(p) for p in pages[:8])}

Reply with one line: verdict, whether the instruction changed, and the biggest
thing you fixed or flagged. Do not paste the JSON."""


def solvability_prompt(deck, ws=None) -> str:
    """The probe's job, in the workspace it is actually standing in.

    It used to name `work/<deck>/` four times — the bundle, the deck directory
    it must not read, and the report to write into it — while running with its
    cwd one level above that directory.  Two probes were voided for reads that
    the prompt itself had put within arm's reach, one of them a bare `ls` of
    the directory it had been told to write to.  Nothing here names the deck
    directory now, because nothing here can reach it.
    """
    import json as _json
    t = _json.loads((deck.root / "task.json").read_text())
    # Tolerant of shape, because this is a *summary* and a summary must not
    # kill a deck. Three of ten decks in the first cold run died here with
    # `AttributeError: 'str' object has no attribute 'get'`: an agent had
    # written `assets` as a list of names rather than a list of records, which
    # nothing between it and here rejects. The same lesson as `_cpu_gate`
    # formatting its console line outside the try — the most volatile code in
    # a stage should not be the code that can end it.
    def _line(x, *keys, prefix=""):
        if isinstance(x, str):
            return f"{prefix}{x}"
        if isinstance(x, dict):
            vals = [x.get(k) for k in keys]
            head = vals[0] if vals else None
            tail = next((v for v in vals[1:] if v), None)
            return f"{prefix}{head}" + (f"  — {tail}" if tail else "")
        return f"{prefix}{x!r}"

    assets = "\n".join(_line(a, "file", "why", "kind", prefix="      ")
                        for a in t.get("assets") or []) or "      (none)"
    degs = "\n".join(_line(d, "id", "slides", prefix="    ")
                      for d in t.get("degradations") or [])
    bundle = ws.bundle if ws is not None else deck.root / "bundle"
    report = ws.report if ws is not None else deck.root / "solvability.json"
    sealed = ("The pipeline's own directories are not merely off limits, they "
              "are not there: this process runs in a namespace where the deck "
              "directory and the corpus do not exist. There is nothing to "
              "resist and nothing to find."
              if ws is not None and ws.kind == "namespace+deny" else
              "The deck directory is denied to you by the permission system "
              "and every read you make is checked against it afterwards.")
    return f"""Judge whether one degraded PPT task can actually be solved.

FIRST: read {skill_path('ppt-task-solvability')} in full and follow it exactly.

Everything a solver would get is in one directory, and it is your whole world:

  {bundle}/
    input.pptx        the broken file
    instruction.md    verbatim, all the solver is told
    assets/
{assets}

  the task claims {len(t.get('degradations') or [])} separate breaks:
{degs}
  and declares difficulty {t.get('difficulty')} / {t.get('est_steps')} steps.

{sealed} So work from the bundle and from {ws.dir if ws is not None else bundle.parent}/,
which is yours; naming a pipeline file in prose is fine, and there is no file
outside the bundle worth going to look for.

`python3 -m pptxgym.…` works here as it always has.

Do not repair anything. Write your findings — the schema is in the skill — to:
  {report}

Reply with one line: verdict, and the single most important finding."""


# --------------------------------------------------------------------------- #
# the solvability rubric, as arithmetic
#
# `ppt-task-solvability` was rewritten into five ordered passes and a
# first-match-wins verdict table because the probe returned four different
# verdicts on one unchanged bundle.  The rewrite changed the prompt and nothing
# else: the gate still validated the enum and the presence of `rework`, never
# the verdict against the findings — and an archived run shipped `leaks: 2`
# with a verdict of `undetermined`, against the skill's own rule, and passed.
#
# A rubric that is only a prompt is a suggestion.  Everything below is the part
# of that skill a machine can decide, decided:
#
#   * the Pass 5 table.  Given the findings the report already contains, the
#     verdict is not an opinion — it is the first line of a five-line table
#     that matches.  The precedence (`leaked` before `undetermined` before
#     `ambiguous` before `overdetermined`) is the walk order, so enforcing the
#     table enforces the precedence.
#   * the Pass 2 classification: `undetermined` empty when `determinate` is
#     true, `rivals` empty when `determinate` is true, and a `determinate:
#     false` degradation with no rivals having to say which part and why.
#   * the Pass 4 arithmetic: the top-level step count is the sum of the
#     per-degradation ones, and the 25% band against the declared total.
#   * the shape the fields have to be in for any of the above to mean
#     anything: a missing `rivals` key and an empty one read identically to a
#     table that cannot see prose.
#
# What is deliberately *not* here is every rule whose subject is evidence the
# checker does not have.  "A leak must be load-bearing" is the probe's
# judgement, made with the package open; re-deciding it from a JSON field would
# be answering the same question with strictly less to go on.  The line is:
# whether a finding exists is the probe's call, and what a set of findings
# implies is arithmetic.  This file only does the arithmetic.
#
# It lives beside `solvability_prompt` on purpose.  The prompt is one half of a
# contract and this is the other, and the failure being fixed is exactly the
# two halves drifting apart — the rubric moved, the checker did not.
# --------------------------------------------------------------------------- #

#: The evidence items of Pass 1.  Every degradation carries all six, each one a
#: citation or an explicit empty string, so that "no item hit" is a statement
#: rather than an omission.
E_ITEMS = ("E1", "E2", "E3", "E4", "E5", "E6")

VERDICTS = ("solvable", "undetermined", "leaked", "ambiguous", "overdetermined")

REWORK_STAGES = ("proposed", "recipe", "materialise")

#: Keys every degradation must carry.  Absence is not the same as an empty
#: value here: a missing `rivals` is indistinguishable from `rivals: []` to the
#: verdict table, so a degradation that found rivals and forgot the field would
#: be read as one that found none.  `tolerance` is in the list for the same
#: reason `checks` is — an empty list is the probe saying it looked.
DEG_KEYS = ("id", "end_state", "checks", "determinate", "rivals",
            "undetermined", "tolerance", "est_steps_measured",
            "overdetermined")

#: Names that say a `leaks` entry is pointing outside `bundle/`.  The skill's
#: rule is absolute — "anything outside `bundle/` cannot leak", because the
#: solver never sees it — and it is the one part of the leak definition that
#: needs no judgement: these files are not in the bundle by construction.
#: Matched against the first token of `where` only, so prose that merely
#: mentions a render does not trip it.
OUT_OF_BUNDLE = frozenset({
    "source.pptx", "delta.json", "recipe.json", "proposal.json", "digest.json",
    "digest_min.json", "task.json", "plan.json", "solvability.json",
    "renders", "compare", "attempts",
})

#: How far the measured step total may sit from the declared one before the
#: probe owes a `rework` note against `proposed`.  The skill's number.
STEP_BAND = 0.25


def _num(v):
    """`v` as a number, or None.  `True` is not a step count."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _text(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def verdict_from_findings(report: dict) -> tuple[str, int, str]:
    """Walk the Pass 5 table over the findings.  Returns (verdict, line, why).

    First line that matches wins, which is what makes the precedence
    executable: `leaked` is checked before `undetermined` not because a leak
    feels worse but because closing one can turn a determinate degradation
    indeterminate, so the other questions cannot be asked until it is gone.
    """
    degs = report.get("degradations") or []
    if report.get("leaks"):
        return "leaked", 1, "`leaks` is non-empty"
    for d in degs:
        if not d.get("determinate") and not (d.get("rivals") or []):
            return ("undetermined", 2,
                    f"{d.get('id')} is `determinate: false` with no `rivals`")
    for d in degs:
        if d.get("rivals"):
            return "ambiguous", 3, f"{d.get('id')} names rivals"
    for d in degs:
        if d.get("overdetermined"):
            return ("overdetermined", 4,
                    f"{d.get('id')} is `overdetermined: true`")
    return ("solvable", 5,
            "no leak, no unenumerable gap, no rivals, nothing overdetermined")


def _leak_location_problem(where: str) -> str:
    """Whether a `leaks` entry's location is one the skill says cannot leak.

    Only the first token is read.  `where` is a location — `ppt/diagrams/
    data5.xml` — and scanning the whole string would fire on a sentence that
    named a render while pointing at a package part, which is a true finding
    refused for its prose.
    """
    tok = _text(where).replace("\\", "/").split()
    if not tok:
        return ""
    parts = [p for p in tok[0].strip("`\"'(),.").split("/") if p]
    if parts and parts[0] == "bundle":
        parts = parts[1:]
    if parts and parts[0] == "assets":
        return ("points into `bundle/assets/`, which the skill says is never a "
                "leak — a shipped asset is either Pass 5's `overdetermined` "
                "question or a `rework` note against `materialise`")
    hit = next((p for p in parts if p.lower() in OUT_OF_BUNDLE), "")
    if hit:
        return (f"points at `{hit}`, which is outside `bundle/` — a solver "
                f"never sees it, so it cannot leak; that is a `rework` note")
    return ""


def _degradation_problems(d, i: int) -> list[str]:
    who = _text(d.get("id")) or f"degradation #{i + 1}"
    out: list[str] = []
    if not isinstance(d, dict):
        return [f"degradation #{i + 1} is not an object"]

    missing = [k for k in DEG_KEYS if k not in d]
    if missing:
        out.append(f"{who} has no {', '.join('`%s`' % k for k in missing)} — "
                   f"every degradation carries all of {', '.join(DEG_KEYS)}")

    if not _text(d.get("end_state")):
        out.append(f"{who} has no end_state")

    checks = d.get("checks")
    if "checks" in d:
        if not isinstance(checks, dict):
            out.append(f"{who}: `checks` is not an object")
        else:
            gone = [e for e in E_ITEMS if e not in checks]
            if gone:
                out.append(f"{who}: `checks` is missing {', '.join(gone)} — "
                           f"write all six, empty string for the ones that "
                           f"did not hit")
            bad = [k for k in checks if k not in E_ITEMS]
            if bad:
                out.append(f"{who}: `checks` has {', '.join(sorted(bad))}, "
                           f"which is not one of {', '.join(E_ITEMS)}")
            if any(not isinstance(v, str) for v in checks.values()):
                out.append(f"{who}: every `checks` value is a citation or an "
                           f"empty string")

    determinate = d.get("determinate")
    if "determinate" in d and not isinstance(determinate, bool):
        out.append(f"{who}: `determinate` is {determinate!r}, not true or false")
    rivals = d.get("rivals") or []
    if "rivals" in d and not isinstance(d.get("rivals"), list):
        out.append(f"{who}: `rivals` is not a list")
        rivals = []
    caveat = _text(d.get("undetermined"))

    if determinate is True:
        # Pass 2's first row, and the defect the rewrite was for: 25 of 30
        # archived degradations were called determinate and then carried a
        # contradictory caveat, because the field was the only place a benign
        # observation could go.  `tolerance` and `rivals` are that place now.
        if caveat:
            out.append(
                f"{who} is `determinate: true` with a non-empty "
                f"`undetermined` — that caveat is either a tolerance or a "
                f"gap, and Pass 2 has a field for each")
        if rivals:
            out.append(f"{who} is `determinate: true` and names rivals — "
                       f"rivals are competing end states, so it is not "
                       f"determinate")
        if not _text(d.get("evidence")):
            out.append(f"{who} is called determinate with no `evidence` — "
                       f"that is a guess wearing a verdict")
        if isinstance(checks, dict) and not any(_text(v)
                                                for v in checks.values()):
            out.append(f"{who} is called determinate with every E1-E6 empty — "
                       f"nothing pins the end state")
    elif determinate is False and not rivals and not caveat:
        # Pass 2's third row: a gap you cannot even enumerate still has to say
        # which part of the end state it is and why.
        out.append(f"{who} is `determinate: false` with no `rivals` and an "
                   f"empty `undetermined` — say which part is not pinned and "
                   f"why")

    for j, t in enumerate(d.get("tolerance") or []):
        if not isinstance(t, dict):
            out.append(f"{who}: tolerance #{j + 1} is not an object")
            continue
        if _text(t.get("rule")).upper() not in ("T1", "T2"):
            out.append(f"{who}: tolerance #{j + 1} names rule "
                       f"{t.get('rule')!r} — a gap is a tolerance under T1 or "
                       f"T2 and under nothing else")
        if not _text(t.get("what")) or not _text(t.get("why")):
            out.append(f"{who}: tolerance #{j + 1} needs `what` and a `why` "
                       f"that cites what makes it one")

    steps = _num(d.get("est_steps_measured"))
    if "est_steps_measured" in d and (steps is None or steps <= 0):
        out.append(f"{who}: `est_steps_measured` is "
                   f"{d.get('est_steps_measured')!r} — the scoring stage reads "
                   f"that field and weights the reward by it")

    if "overdetermined" in d and not isinstance(d.get("overdetermined"), bool):
        out.append(f"{who}: `overdetermined` is "
                   f"{d.get('overdetermined')!r}, not true or false")
    return out


def solvability_rubric_problems(report: dict) -> list[str]:
    """Every rule of `ppt-task-solvability` a machine can decide, decided.

    Returns one string per breach, worst first: shape before arithmetic, so a
    report whose fields are missing is told that rather than being told the
    verdict table disagrees with findings it could not read.  Empty means the
    report is internally consistent — which is not the same as right, and is
    the only claim a checker is entitled to make about a judgement stage.
    """
    if not isinstance(report, dict):
        return ["solvability.json is not an object"]

    shape: list[str] = []
    rules: list[str] = []

    verdict = report.get("verdict")
    if verdict not in VERDICTS:
        return [f"unknown verdict {verdict!r}"]
    if not _text(report.get("verdict_reason")):
        shape.append("no `verdict_reason` — one sentence, naming the table "
                     "line that decided it")

    degs = report.get("degradations")
    if not degs or not isinstance(degs, list):
        return ["no per-degradation findings"]
    for i, d in enumerate(degs):
        shape += _degradation_problems(d, i)

    for i, lk in enumerate(report.get("leaks") or []):
        who = f"leak #{i + 1}"
        if not isinstance(lk, dict):
            shape.append(f"{who} is not an object")
            continue
        if not _text(lk.get("what")) or not _text(lk.get("where")):
            shape.append(f"{who} needs `what` and `where`")
        if not _text(lk.get("load_bearing")):
            # The truth of the reason is the probe's to judge; that one was
            # written down is the difference between a leak and a note.
            shape.append(f"{who} states no `load_bearing` reason — a finding "
                         f"that closes no gap and eliminates no rival is "
                         f"`residue`, not a leak")
        if (bad := _leak_location_problem(lk.get("where"))):
            rules.append(f"{who} {bad}")

    for i, x in enumerate(report.get("residue") or []):
        if not isinstance(x, dict) or not _text(x.get("what")) \
                or not _text(x.get("why_not_a_leak")):
            shape.append(f"residue #{i + 1} needs `what` and `why_not_a_leak` "
                         f"— the second is what makes it residue and not a "
                         f"leak nobody wrote up")

    rework = report.get("rework") or []
    for i, x in enumerate(rework):
        if not isinstance(x, dict) or x.get("stage") not in REWORK_STAGES:
            shape.append(f"rework #{i + 1} targets "
                         f"{(x or {}).get('stage')!r} — the pipeline re-runs "
                         f"one of {', '.join(REWORK_STAGES)}")
        elif not _text(x.get("what")):
            shape.append(f"rework #{i + 1} says nothing in `what`")

    # ---- Pass 4: the two sums ------------------------------------------- #
    measured = _num(report.get("est_steps_measured"))
    declared = _num(report.get("est_steps_declared"))
    if measured is None:
        shape.append("no top-level `est_steps_measured`")
    if declared is None:
        shape.append("no `est_steps_declared`")
    per = [_num(d.get("est_steps_measured")) for d in degs
           if isinstance(d, dict)]
    if measured is not None and all(p is not None for p in per):
        total = sum(per)
        if abs(total - measured) > 0.5:
            rules.append(
                f"top-level `est_steps_measured` is {measured} but the "
                f"degradations add up to {total} — the top-level value is "
                f"the sum of them")
    if measured is not None and declared not in (None, 0):
        off = abs(measured - declared) / abs(declared)
        owed = off > STEP_BAND
        told = any(isinstance(x, dict) and x.get("stage") == "proposed"
                   for x in rework)
        if owed and not told:
            rules.append(
                f"measured {measured} steps against a declared {declared} — "
                f"{off:.0%} out, past the {STEP_BAND:.0%} band, and no "
                f"`rework` entry against `proposed` says so")

    # ---- Pass 5: the table ---------------------------------------------- #
    if verdict != "solvable" and not rework:
        rules.append(f"verdict {verdict!r} with no `rework` — the pipeline "
                     f"uses it to decide what to re-run")
    want, line, why = verdict_from_findings(report)
    if want != verdict:
        rules.append(
            f"verdict is {verdict!r}, but line {line} of the Pass 5 table "
            f"matches these findings and says {want!r}: {why}. First line "
            f"that matches wins; if the verdict is the right one then a "
            f"finding is misfiled")
    return shape + rules


def _degradation_checklist(task: dict) -> str:
    """Every degradation the recipe has to implement, by id, one per line.

    The prompt said "Implement every one of them" and then named none of them:
    the count was there, the ids were in `proposal.json`, and enumerating them
    was left to the agent. `check_recipe` meanwhile requires every declared id
    to be the `deg` of some step. That gap is the most frequent rejection in
    the pipeline.

    Tolerant of shape for the reason `solvability_prompt` gives: this is a
    summary, and a summary must not be what ends a deck.
    """
    out = []
    for item in task.get("degradations") or []:
        if not isinstance(item, dict):
            out.append(f"  - {item}")
            continue
        what = (item.get("what") or item.get("anchor")
                or item.get("disclosure_detail") or "")
        out.append(f"  - {item.get('id')}  slides {item.get('slides')}"
                   + (f"  — {str(what)[:90]}" if what else ""))
    return "\n".join(out) or "  (the proposal lists none — that is a defect)"


def recipe_prompt(deck) -> str:
    proposal = json.loads(deck.proposal.read_text())
    tasks = proposal.get("tasks") or []
    task = tasks[0] if tasks else {}
    return f"""Turn a task proposal into an executable degradation recipe, for
one deck.

FIRST: read {skill_path('ppt-degrade-recipe')} in full and follow it exactly.

Your deck: {deck.id}  ({deck.meta().get('name')}, {deck.meta().get('slides')} slides)
  proposal : {deck.proposal}
  digest   : {deck.digest}
  renders  : {deck.renders}/p-NN.png
  source   : {deck.source}
  write to : {deck.recipe}

The proposal's task is "{task.get('name', '?')}" with
{len(task.get('degradations') or [])} degradations on slides
{task.get('slides')}. Implement every one of them:

{_degradation_checklist(task)}

Every id above must appear as the `deg` of at least one recipe step. A
degradation with no step is the single commonest way a deck is rejected here —
five times across three runs, and twice it was the same id on the same deck
on a second attempt. The instruction still asks the solver for that work,
nothing broke, so nothing it produces can be scored. If one of them cannot be
done on this deck, say so in your reply rather than leaving it out silently: it
is the proposal that is then wrong, and fixing it is not your job here.

Look at the renders before choosing any path — a digest entry called `path 3`
is only identifiable against the picture.

Then reply with one line: how many steps on how many slides, plus anything you
had to approximate or skip. Do not paste the JSON."""
