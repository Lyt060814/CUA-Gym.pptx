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
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import profiles
from .prompts import (
    SKILLS,
    _degradation_checklist,
    propose_prompt,
    recipe_prompt,
    reconcile_prompt,
    skill_path,
    solvability_prompt,
)
from .solvability import (
    DEG_KEYS,
    E_ITEMS,
    OUT_OF_BUNDLE,
    REWORK_STAGES,
    STEP_BAND,
    VERDICTS,
    _degradation_problems as _solvability_degradation_problems,
    _leak_location_problem as _solvability_leak_location_problem,
    _num as _solvability_num,
    _text as _solvability_text,
    solvability_rubric_problems,
    verdict_from_findings,
)


def _num(v):
    return _solvability_num(v)


def _text(v) -> str:
    return _solvability_text(v)


def _leak_location_problem(where: str) -> str:
    return _solvability_leak_location_problem(where)


def _degradation_problems(d, i: int) -> list[str]:
    return _solvability_degradation_problems(d, i)

ROOT = Path(__file__).resolve().parents[2]
RESOURCE_ROOT = Path(__file__).resolve().parents[1] / "resources"
AGENTS = RESOURCE_ROOT / "agents"

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


#: A lane whose quota is shared with somebody else's live workload — the
#: relay serving muse-spark also serves rollouts — needs the opposite policy
#: from a private account.  The first calibration lost 9 of 10 decks to
#: `429 Too Many Requests` inside six minutes: four attempts three minutes
#: apart cannot outlast a rollout burst, and giving up early wastes the
#: prep the deck already paid for.  Batch work has no deadline; waiting is
#: nearly free, and a deck that waits twenty minutes still ships.
SHARED_RETRIES = 8
SHARED_BACKOFF_STEP = 90
SHARED_BACKOFF_CAP = 600


def backoff_seconds(attempt: int, step: int = BACKOFF_STEP,
                    cap: int = BACKOFF_CAP) -> int:
    """How long to wait after attempt `attempt` failed."""
    return min(step * attempt, cap)


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
        """Put this stage's assignment on the spec, and say what it was.

        A flag beats a builder default, but an *absent* flag does not erase
        one: the probe spec pins haiku for itself, and clobbering that with
        None handed a codex-lane probe to whatever the config file named.
        """
        got = self.for_stage(stage)
        spec.model = got["model"] or spec.model
        spec.effort = got["effort"] or spec.effort
        spec.fallback_model = got["fallback_model"] or spec.fallback_model
        return got


#: Which CLI runs the agent.  Read from the environment by default so lane
#: purity is free: the foreman sets `PPTXGYM_ENGINE` on the orchestrator it
#: spawns, the orchestrator's Bash inherits it, and every specialist verb it
#: runs lands on the same engine without any flag being passed around.
ENGINE_ENV = "PPTXGYM_ENGINE"
ENGINES = ("claude", "codex")

#: The sealed witness is its own lane.  It deliberately does not inherit the
#: orchestrator engine: the owner and the witness are independent decisions,
#: and losing one provider must not strand every otherwise-complete deck.
PROBE_ENGINE_ENV = "PPTXGYM_PROBE_ENGINE"
PROBE_MODEL_ENV = "PPTXGYM_PROBE_MODEL"
PROBE_EFFORT_ENV = "PPTXGYM_PROBE_EFFORT"
ROUTES_ENV = "PPTXGYM_ROUTES_JSON"

#: How many times a codex *specialist* is handed its own stage back.
#: `codex exec` is one-shot — it ends when the model stops calling tools —
#: and the calibration's specialists finished 0 of 36 `recipe` starts that
#: way: they quit early, wrote nothing, and nobody handed the work back.
#: The orchestrator already gets this through foreman.CONTINUATIONS; the
#: specialists got zero. Claude specialists keep zero: `--max-turns` means
#: a clean early exit is a decision there, not an accident.
CODEX_SPECIALIST_CONTINUATIONS = 2


def default_engine() -> str:
    got = os.environ.get(ENGINE_ENV, "claude").strip() or "claude"
    if got not in ENGINES:
        raise ValueError(f"{ENGINE_ENV}={got!r}: pick from {ENGINES}")
    return got


def configured_routes() -> dict:
    """Resolved provider-neutral routes inherited from ``pptxgym run``."""
    raw = os.environ.get(ROUTES_ENV, "")
    if not raw:
        return {}
    try:
        got = json.loads(raw)
    except ValueError as error:
        raise ValueError(f"{ROUTES_ENV} does not contain JSON: {error}") from error
    return got if isinstance(got, dict) else {}


def configured_route(stage: str, *, owner: bool = False) -> dict:
    key = "owner" if owner else {
        "proposed": "proposal", "propose": "proposal",
        "recipe": "recipe", "reconciled": "reconcile",
        "reconcile": "reconcile", "solvable": "probe",
    }.get(stage, stage)
    return dict(configured_routes().get(key) or {})


def apply_route(spec: "AgentRun", stage: str, *, owner: bool = False) -> dict:
    """Apply a route without erasing explicit stage flags."""
    route = configured_route(stage, owner=owner)
    if not route:
        return {}
    spec.engine = route.get("harness") or spec.engine
    spec.model = route.get("model") or spec.model
    spec.effort = route.get("effort") or spec.effort
    spec.connection = route
    return route


def probe_assignment() -> dict:
    """The engine/model/effort pinned for the sealed solvability witness."""
    route = configured_route("probe")
    engine = (os.environ.get(PROBE_ENGINE_ENV) or route.get("harness")
              or "claude").strip()
    if engine not in ENGINES:
        raise ValueError(f"{PROBE_ENGINE_ENV}={engine!r}: pick from {ENGINES}")
    model = os.environ.get(PROBE_MODEL_ENV) or route.get("model")
    if model is None:
        model = "haiku" if engine == "claude" else \
            (os.environ.get("PPTXGYM_CODEX_MODEL") or None)
    effort = os.environ.get(PROBE_EFFORT_ENV) or route.get("effort")
    if effort is None and engine == "codex":
        effort = "medium"
    if effort and effort not in EFFORTS:
        raise ValueError(f"{PROBE_EFFORT_ENV}={effort!r}: pick from {EFFORTS}")
    return {"engine": engine, "model": model, "effort": effort,
            "fallback_model": None, "connection": route}


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
    #: left None to mean "whatever this engine's quota deserves" — see
    #: `__post_init__`.  Nested specialist verbs build their AgentRuns with
    #: no retry argument at all, so the policy has to follow the engine
    #: rather than the call site.
    api_retries: int | None = None
    backoff_step: int | None = None
    backoff_cap: int | None = None
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
    #: Keys removed from the inherited environment before `env` is applied.
    #: The sealed probe needs the relay credential used by its own CLI, but it
    #: has no reason to inherit GitHub/HF credentials or the run coordinates
    #: that point back to answer-key archives.
    unset_env: list[str] = field(default_factory=list)
    #: Optional connection details supplied by the high-level route table.
    connection: dict = field(default_factory=dict)
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
    #: `() -> str | None`: the reason this agent is not finished yet, or None
    #: when it is done.  `codex exec` is one-shot — it ends when the model
    #: stops calling tools, and muse-spark stopped mid-deck on three of five
    #: calibration decks at 15-16 minutes, none of them out of budget and
    #: none of them having written the REVIEW.md the brief asks for.  claude
    #: has `--max-turns` to keep pushing; this is that budget's equivalent,
    #: and it is checked for every engine because "the process exited" has
    #: never been the same claim as "the work is done".
    unfinished: object = None
    #: how many times to hand the work back when `unfinished` says so.
    continuations: int = 0

    def __post_init__(self) -> None:
        shared = self.engine == "codex"
        if self.api_retries is None:
            self.api_retries = SHARED_RETRIES if shared else API_RETRIES
        if self.backoff_step is None:
            self.backoff_step = (SHARED_BACKOFF_STEP if shared
                                 else BACKOFF_STEP)
        if self.backoff_cap is None:
            self.backoff_cap = SHARED_BACKOFF_CAP if shared else BACKOFF_CAP


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
    resumed = 0
    started = time.monotonic()
    prompt0 = spec.prompt
    while True:
        before = {p: _stamp(p) for p in spec.outputs}
        res = await _run_once(spec, log)
        if res["status"] != "infra":
            # A clean exit is not a finished deck. Hand the work back with
            # what is still missing, as long as there is wall clock left —
            # the budget that governs is time, so a continuation that would
            # start past the deadline is not started.
            left = spec.timeout_min * 60 - (time.monotonic() - started)
            gap = _still_missing(spec) if resumed < spec.continuations \
                and res["status"] == "exited" and left > 120 else None
            if gap:
                resumed += 1
                history.append({"attempt": attempt, "kind": "unfinished",
                                "why": gap[:200],
                                "kept": _keep_attempt(log, spec, attempt,
                                                      before),
                                "backoff_s": 0})
                spec.prompt = _continue_prompt(prompt0, gap, resumed)
                spec.timeout_min = max(2, int(left // 60))
                attempt += 1
                continue
            break                     # a verdict, a timeout, or a ceiling hit
        if attempt > _retries_allowed(spec, res.get("kind")):
            break
        history.append({"attempt": attempt, "kind": res.get("kind"),
                        "why": res.get("why"),
                        "kept": _keep_attempt(log, spec, attempt, before),
                        "backoff_s": backoff_seconds(attempt,
                                                     spec.backoff_step,
                                                     spec.backoff_cap)})
        await _hold_off(attempt, spec)
        attempt += 1
    res["attempts"] = attempt
    spec.prompt = prompt0
    if resumed:
        res["continued"] = resumed
    if history:
        res["retried"] = history
        if res["status"] == "infra":
            res["why"] = (f"{res.get('why', '')} — gave up after "
                          f"{attempt} attempts").strip()
        else:
            res["recovered"] = True
    return res


def _still_missing(spec: AgentRun) -> str | None:
    """What the agent was asked for and has not produced, or None.

    Without a caller-supplied `unfinished`, the promised outputs answer the
    question: a specialist that exited cleanly leaving `recipe.json`
    unwritten has not finished, whatever its exit code says. That is how
    every codex specialist in the calibration died — `codex exec` ends when
    the model stops calling tools — and with no gap detector the hand-back
    machinery never saw them.
    """
    if callable(spec.unfinished):
        try:
            return spec.unfinished()
        except Exception:                                        # noqa: BLE001
            return None               # an unanswerable check stops nothing
    missing = [str(p) for p in (spec.outputs or []) if not Path(p).exists()]
    if missing:
        return f"promised output not written: {', '.join(missing)}"
    return None


def _continue_prompt(original: str, gap: str, n: int) -> str:
    """Hand the same work back, naming what is outstanding.

    The original brief comes with it: a continuation that only says "keep
    going" loses the boundaries — which deck is yours, which flags each verb
    takes, what may not be edited — and an agent that has forgotten those is
    more dangerous than one that stopped.
    """
    return (f"{original}\n\n---\n\nCONTINUATION {n}. Your previous session "
            f"ended without finishing, and the work is still yours. What is "
            f"outstanding:\n\n  {gap}\n\nEverything already on disk stands — "
            f"read {'state.json'} first and pick up from the furthest stage "
            f"that recorded `ok`, rather than redoing it. Ending a session is "
            f"not delivering: finish the deck, or write the reasoned no in "
            f"REVIEW.md that the brief asks for.")


def _retries_allowed(spec: AgentRun, kind: str | None) -> int:
    budget = max(0, int(spec.api_retries or 0))
    return min(budget, AUTH_RETRIES) if kind == "auth_error" else budget


async def _hold_off(attempt: int, spec: "AgentRun | None" = None) -> int:
    """The wait between attempts, as its own function so a test can skip it."""
    delay = backoff_seconds(attempt,
                            getattr(spec, "backoff_step", None) or BACKOFF_STEP,
                            getattr(spec, "backoff_cap", None) or BACKOFF_CAP)
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


#: Models with no `effort` parameter.  The supported list is every Claude 4.6
#: and later *reasoning* model — fable-5, opus-5/4.8/4.7/4.6, sonnet-5/4.6,
#: opus-4.5 — and Haiku 4.5 is not on it.  Passing the flag anyway is not a
#: no-op you find out about later: it is an argument the CLI hands to an API
#: that rejects it, so a whole lane dies at the first call with an error about
#: a flag nobody chose to send.  The lane asks for an effort; this decides
#: whether the model can be asked.
EFFORTLESS = ("haiku",)


def _effortless(model: str | None) -> bool:
    return any(m in (model or "").lower() for m in EFFORTLESS)


def _claude_cmd(spec: AgentRun) -> list[str]:
    manual = agent_manual(spec.name)
    cmd = ["claude"]
    if manual:
        agents = {spec.name: {
            "description": f"pptxgym {spec.name} agent",
            "prompt": manual,
        }}
        cmd += ["--agents", json.dumps(agents, ensure_ascii=False)]
    cmd += ["--agent", spec.name, "-p", spec.prompt,
           "--max-turns", str(spec.max_turns),
           "--output-format", "stream-json", "--verbose",
           "--allowedTools", ",".join(spec.allowed_tools)]
    for d in spec.add_dirs:
        cmd += ["--add-dir", str(d)]
    if spec.settings:
        cmd += ["--settings", spec.settings]
    if spec.model:
        cmd += ["--model", spec.model]
    if spec.effort and not _effortless(spec.model):
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
#: (claude's `max`) rounds down to the nearest thing codex has.  `xhigh`
#: passes through since codex-cli 0.147 — the old rounding silently ran a
#: lane a notch below what the launch asked for.
_CODEX_EFFORT = {"low": "low", "medium": "medium", "high": "high",
                 "xhigh": "xhigh", "max": "xhigh", "minimal": "minimal"}


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
    connection = spec.connection or {}
    if connection.get("base_url"):
        auth = connection.get("auth", "")
        env_key = connection.get("auth_env") or (
            auth[4:] if auth.startswith("env:") else "OPENAI_API_KEY")
        cmd += ["-c", 'model_provider="pptxgym"',
                "-c", 'model_providers.pptxgym.name="pptxgym"',
                "-c", f'model_providers.pptxgym.base_url="{connection["base_url"]}"',
                "-c", f'model_providers.pptxgym.env_key="{env_key}"',
                "-c", f'model_providers.pptxgym.wire_api="{connection.get("wire_api", "responses")}"']
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
        child_env = {**os.environ}
        for key in spec.unset_env:
            child_env.pop(key, None)
        child_env.update(spec.env or {})
        connection = spec.connection or {}
        auth = connection.get("auth", "")
        if spec.engine == "claude" and connection.get("base_url"):
            child_env["ANTHROPIC_BASE_URL"] = connection["base_url"]
            auth_env = connection.get("auth_env") or (
                auth[4:] if auth.startswith("env:") else "ANTHROPIC_API_KEY")
            if child_env.get(auth_env):
                child_env["ANTHROPIC_AUTH_TOKEN"] = child_env[auth_env]
            elif child_env.get("ANTHROPIC_API_KEY"):
                child_env["ANTHROPIC_AUTH_TOKEN"] = child_env["ANTHROPIC_API_KEY"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=lf, stderr=ef, cwd=str(spec.cwd),
            env=child_env)
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
    # Head *and* tail: codex names the model in `session_configured`, which
    # is the first line of the stream, and the failure it died of is in the
    # last. Reading only the tail meant every log over 32 KB came back with
    # no model at all — and a provenance check written against that would
    # have refused every honestly-run codex deck in the batch.
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return []
    if len(text) > 2 * RESULT_TAIL:
        text = text[:RESULT_TAIL] + "\n" + text[-RESULT_TAIL:]
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
