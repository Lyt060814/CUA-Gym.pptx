"""Run the task on a real machine before its `.py` is allowed into git.

`verify_fetchable` asks a URL whether it resolves.  That is the cheapest
question worth asking and it is not the question that matters: a task whose
materials are all reachable can still hand the agent nothing, because `setup`
is code — it renames, it digests, it launches WPS, it raises `AssetFetchError`
when what arrived is not what the emitter recorded.  The only check that
answers "will an episode start" is to start one.

So this module is a **stronger `verify_fetchable`**, in the same slot and with
the same consequence.  It provisions a VM through the OSWorld smoke runner,
calls the task's own `setup()` against the *real baked URLs*, and reports what
happened.  Nothing else about `publish` moves: materials still go up first, the
`.py` is still written only after its materials have been proved to work, and a
task that does not pass is dropped from the commit and named in the summary.

    upload deck's materials  ->  fetch-check  ->  setup() on a VM   (per deck)
                                                                    ...
                                          one git commit, at the end  (barrier)

**The barrier is the commit and nothing else.**  A deck that finishes early is
*ready*; it waits only for the batch's single commit, never for a slow sibling
to reach the same stage.  That is why the three phases above are a per-deck
pipeline running in a thread rather than three batch passes: the batch passes
would make every deck's readiness the readiness of the slowest one.

**A failure is two different claims and they must not be blurred.**

| what happened | verdict | ships | what the summary may say |
|---|---|---|---|
| `setup()` ran, deck is on the machine | `ok` | yes | verified |
| `setup()` raised, asset 404, evaluator import error | `task_broken` | no | this task is broken, and here is why |
| the dataset does not serve what we just uploaded | `materials_missing` | no | the upload did not land |
| quota, capacity, a 502 from the VM's own server, an instance that never came up | `infrastructure` | no | **unverified** — not failed |
| anything this module will not attribute | `unattributed` | no | unverified, and the reason is quoted rather than guessed |

The distinction is not cosmetic and it is not hypothetical: a stock OSWorld
task failed here with a 404 on a missing Hugging Face asset, and one of ours
failed with a 502 while uploading.  Both came back as a non-zero exit and a
`setup_failed`-shaped report.  One of them is a task to fix and the other is a
task to re-run, and a summary that calls them both "failed" has destroyed the
only information in the run.

**Where the deciding fact lives was measured, not assumed.**  On the first real
batch (six instances, 2026-08-05) the 404 did *not* appear in `result.json` at
all: OSWorld's downloader retries ten times and then raises `Failed to download
<url>. No retries left.`, with the status stripped.  The only surviving copy of
"404" is a line of `setup.log`, which is why `classify` reads that log's cause
lines as a third tier.  Without it, an honest classifier can only say
`unattributed` — which is what it did say, before the log was read.

**The vCPU ceiling is genuinely unknown, so it is discovered rather than
declared.**  `servicequotas:GetServiceQuota` is denied to this IAM user, 36
vCPUs belong to other people's long-running instances, and every smoke test is
one `t3.medium`.  The pool therefore starts conservative (`--aws-workers`,
default 4 = 8 vCPU) and *narrows on evidence*: `VcpuLimitExceeded`,
`InstanceLimitExceeded` and `InsufficientInstanceCapacity` mean "you asked for
too much", so the ceiling drops to just under the concurrency that was refused,
a cooldown starts, and the deck is put back in the queue **without spending one
of its attempts**.  A task that failed because we ran out of quota is not a bad
task, and recording it as one is the worst outcome available here.

The ceiling never grows back on its own.  Widening after a refusal would be a
guess in the direction that costs money and gets other people's instances
refused; a run that ended narrow says so in its report, and a human can raise
the flag next time.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# where the runner is
# --------------------------------------------------------------------------- #
#
# The smoke runner belongs to OSWorld-V2, not to this repository: it is the
# thing that knows how to turn a task file into a booted EC2 instance, and
# re-implementing it here would mean maintaining a second opinion about how the
# benchmark starts an episode.  It is invoked as a subprocess for the same
# reason — it runs in OSWorld's environment, under OSWorld's `uv` lock, and
# importing it would drag that dependency set into this package.

#: The checkout that owns the runner. Overridable because a second checkout is
#: a normal thing to have; defaulted because typing it every time is not.
OSWORLD_REPO_ENV = "PPTXGYM_OSWORLD_REPO"
DEFAULT_OSWORLD_REPO = Path(__file__).resolve().parents[3] / "OSWorld-V2"

RUNNER_REL = ".claude/skills/aws-setup-smoke-test/scripts/test_setup_aws.py"

#: `uv` is installed under `~/.local/bin`, which a non-login shell does not
#: have on `PATH` — a background run that resolved it from `PATH` alone would
#: fail with exit 127 and look like a broken task.  See `resolve_uv`.
UV_ENV = "PPTXGYM_UV"

#: One `t3.medium` per test, and the ceiling on how many may exist at once
#: cannot be read from AWS by this IAM user.  Four is a starting point, not a
#: measurement, and `Ceiling` is what turns it into one.
DEFAULT_AWS_WORKERS = 4

#: Uploads are network-bound and cheap; the limit is politeness to the dataset
#: (one commit per task, in this mode) rather than a resource.
DEFAULT_HF_WORKERS = 4

#: A smoke test is 3-4 minutes.  This is the point at which we stop believing
#: the instance is coming up, and it is generous on purpose: killing a healthy
#: run costs the whole run *and* leaks the instance it was about to release.
SMOKE_TIMEOUT = 45 * 60

#: How long the runner is given to release its EC2 instance after SIGTERM. It
#: converts SIGTERM into KeyboardInterrupt precisely so its `finally` can call
#: `env.close()`; killing it outright leaves an instance running and billing.
SIGTERM_GRACE = 300

OK = "ok"
TASK_BROKEN = "task_broken"
MATERIALS_MISSING = "materials_missing"
INFRASTRUCTURE = "infrastructure"
UNATTRIBUTED = "unattributed"

#: The verdicts whose task may be committed.  One entry, deliberately: the
#: point of this stage is that "ships" has exactly one reason.
SHIPPABLE = (OK,)


class SmokeUnavailable(RuntimeError):
    """The runner cannot be started at all — refuse the batch, don't blame it.

    Marking forty decks `unverified` one at a time because `uv` is not on the
    path is forty misleading records and forty minutes; the whole point of this
    class is that the batch stops before the first one.
    """


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #


_SECRETS = (
    (re.compile(r"\b(?:AKIA|ASIA|AIDA|AGPA|AROA|ANPA|ANVA)[0-9A-Z]{12,}\b"),
     "<redacted>"),
    (re.compile(r"\bhf_[A-Za-z0-9]{16,}\b"), "<redacted>"),
    (re.compile(r"\b(?:ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{16,}\b"),
     "<redacted>"),
    (re.compile(r"(?i)\b(aws_secret_access_key|aws_session_token|secret_key|"
                r"password|token|authorization)\b(\s*[:=]\s*)\S+"),
     r"\1\2<redacted>"),
    (re.compile(r"(?i)(X-Amz-(?:Signature|Credential|Security-Token))="
                r"[^&\s\"']+"), r"\1=<redacted>"),
)


def redact(text: str) -> str:
    """Take the credentials out of anything this module records.

    The runner's own `setup.log` is not ours to rewrite, but every string that
    reaches *our* artefacts — the report, the events, the outcome's `why` —
    goes through here.  A presigned URL in a traceback is a working credential
    for as long as it is valid, and a report is exactly the kind of file that
    gets pasted into a message.
    """
    out = str(text or "")
    for pattern, replacement in _SECRETS:
        out = pattern.sub(replacement, out)
    return out


# --------------------------------------------------------------------------- #
# what the failure was
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Verdict:
    kind: str
    why: str
    #: True only for the errors that mean "you asked AWS for too much at once".
    #: They are the only ones that narrow the pool, because they are the only
    #: ones narrowing the pool can fix.
    capacity: bool = False


#: Ordered.  First match wins, and the order is "specific and unambiguous"
#: first — a 404 and a `VcpuLimitExceeded` each mean one thing, whereas
#: `EnvironmentSetupError` means only that setup did not finish.
_SIGNATURES: tuple[tuple[re.Pattern, str, str, bool], ...] = (
    # ---- we asked for too much; nothing is wrong with the task -------------
    (re.compile(r"VcpuLimitExceeded"), INFRASTRUCTURE,
     "AWS refused the instance: the account's vCPU limit was reached", True),
    (re.compile(r"InstanceLimitExceeded"), INFRASTRUCTURE,
     "AWS refused the instance: the instance limit was reached", True),
    (re.compile(r"InsufficientInstanceCapacity"), INFRASTRUCTURE,
     "AWS had no capacity for the instance type in this AZ", True),
    (re.compile(r"MaxSpotInstanceCountExceeded"), INFRASTRUCTURE,
     "AWS refused the spot instance: the spot count limit was reached", True),
    (re.compile(r"RequestLimitExceeded|\bThrottl"), INFRASTRUCTURE,
     "AWS throttled the API calls that provision the instance", True),

    # ---- the machine or its server, not the task ---------------------------
    (re.compile(r"\b50[234]\b|Bad Gateway|Service Unavailable|Gateway Time"),
     INFRASTRUCTURE,
     "the VM's own HTTP server answered 5xx — the machine was up but not "
     "serving", False),
    (re.compile(r"Connection refused|Connection reset|Connection aborted|"
                r"Remote end closed|Max retries exceeded|NewConnectionError|"
                r"ConnectionError|ProtocolError|IncompleteRead"),
     INFRASTRUCTURE, "the connection to the VM failed", False),
    (re.compile(r"(?i)read timed out|readtimeout|connecttimeout|timed out "
                r"waiting|did not become ready|never came up"),
     INFRASTRUCTURE, "the VM did not come up in time", False),
    (re.compile(r"EndpointConnectionError|BotoCoreError|NoCredentialsError|"
                r"Unable to locate credentials|ExpiredToken|AuthFailure"),
     INFRASTRUCTURE, "the AWS API call itself failed", False),
    (re.compile(r"InvalidSubnetID|InvalidGroup|InvalidAMIID|"
                r"UnauthorizedOperation"),
     INFRASTRUCTURE, "the AWS environment is misconfigured for this account",
     False),

    # ---- the task ----------------------------------------------------------
    (re.compile(r"\b404\b|Not Found|EntryNotFound|RepositoryNotFound"),
     TASK_BROKEN, "a file the task fetches is not there (404)", False),
    (re.compile(r"\b40[13]\b|Unauthorized|Forbidden|GatedRepo"),
     TASK_BROKEN,
     "the dataset refused the fetch (401/403) — the materials are not public "
     "to the machine that has to read them", False),
    (re.compile(r"AssetFetchError"), TASK_BROKEN,
     "the task refused to start: its own materials check failed", False),
    (re.compile(r"did not arrive intact"), TASK_BROKEN,
     "a fetched file did not match the digest the emitter recorded", False),
    (re.compile(r"ModuleNotFoundError|ImportError|cannot import name"),
     TASK_BROKEN, "the task file does not import", False),
    (re.compile(r"SyntaxError|IndentationError"), TASK_BROKEN,
     "the task file does not parse", False),
    (re.compile(r"No such file or directory|FileNotFoundError"), TASK_BROKEN,
     "the task read a file that is not there", False),
    (re.compile(r"AttributeError|NameError|TypeError|KeyError|IndexError|"
                r"JSONDecodeError|ValueError"),
     TASK_BROKEN, "the task raised while setting the machine up", False),
)

#: Statuses the runner writes that decide the verdict on their own, when no
#: signature fired.  `setup_failed` is deliberately absent: it is the status
#: that means "setup did not finish", which is the question, not the answer.
_STATUS_VERDICTS = {
    "task_load_failed": Verdict(
        TASK_BROKEN, "the task file could not be loaded at all"),
    "import_failed": Verdict(
        INFRASTRUCTURE, "the runner could not import DesktopEnv — that is the "
                        "harness, not the task"),
    "ami_resolve_failed": Verdict(
        INFRASTRUCTURE, "no AMI for this region and screen size"),
    "interrupted": Verdict(
        INFRASTRUCTURE, "the smoke test was interrupted before it finished"),
    "running": Verdict(
        INFRASTRUCTURE, "the runner died before it wrote a verdict — the "
                        "result file still says `running`"),
}


def _scan(text: str) -> Verdict | None:
    for pattern, kind, why, capacity in _SIGNATURES:
        if pattern.search(text):
            return Verdict(kind, why, capacity)
    return None


def classify(raw: dict) -> Verdict:
    """What the smoke test proved, from what the runner wrote down.

    `raw` is `run_smoke`'s return: the parsed `result.json` (or `None`), the
    exit status, the tail of whatever the process said, and the *cause lines*
    of `setup.log`.

    Three tiers, in this order, and the order is the whole of the logic:

    1. **the error message**, because it is the proximate cause.  A 502 raised
       while uploading arrives with a `FileNotFoundError` further down the same
       traceback — the cached file the failed upload never wrote — and a scan
       over the whole blob reads the consequence instead of the cause.
    2. **the traceback**, for when the message says nothing worth reading.
    3. **`setup.log`'s cause lines**, because *the status the answer depends on
       is not in `result.json` at all*.  Measured, on a real run: OSWorld's
       `_download_setup` retries ten times and then raises
       `Failed to download <url>. No retries left.`, dropping the HTTP status
       that says whether this is a 404 (the task names a file that is not
       there) or a 503 (the dataset was down).  Those are the two verdicts this
       module exists to separate, and the only surviving copy of the deciding
       fact is the log line `... caused by 404 Client Error`.

    Only the *cause* lines of the log are read, never the whole tail.  A setup
    log contains the boot loop, which logs connection refusals as a matter of
    routine while it waits for the VM's server to come up; scanning that would
    turn every task failure into "the infrastructure did it".
    """
    result = raw.get("result")
    if raw.get("timed_out"):
        return Verdict(INFRASTRUCTURE,
                       f"the smoke test passed its {raw.get('timeout', '?')}s "
                       f"deadline and was stopped")
    if not isinstance(result, dict):
        note = raw.get("stderr_tail") or ""
        found = _scan(note) if note else None
        return found or Verdict(
            UNATTRIBUTED,
            "the runner wrote no result.json, so there is nothing to read: "
            + (redact(note.strip().splitlines()[-1]) if note.strip()
               else f"exit {raw.get('returncode')}"))

    status = str(result.get("status") or "")
    if status == OK and result.get("success"):
        return Verdict(OK, "setup() ran on a real VM")

    message = " ".join(str(result.get(k) or "")
                       for k in ("error", "error_type"))
    found = (_scan(message)
             or _scan(str(result.get("traceback") or ""))
             or _scan("\n".join(raw.get("log_causes") or [])))
    if found:
        return found
    by_status = _STATUS_VERDICTS.get(status)
    if by_status:
        return by_status

    # Deliberately not "broken".  Refusing to attribute is a claim we can
    # defend; either of the other two would be a claim we cannot.
    first_line = str(result.get("error") or "").strip().splitlines()
    detail = redact(first_line[0]) if first_line else f"status {status!r}"
    return Verdict(UNATTRIBUTED,
                   f"failed for a reason this check will not attribute to "
                   f"either the task or the infrastructure: {detail}")


# --------------------------------------------------------------------------- #
# the ceiling nobody will tell us
# --------------------------------------------------------------------------- #


class Ceiling:
    """A concurrency limit that narrows when AWS says it is too wide.

    A plain semaphore encodes a number somebody knew.  Nobody knows this one:
    the quota API is denied to this IAM user, and the account's spare capacity
    depends on how many instances other people are running this afternoon.  So
    the limit is a hypothesis, and `too_many` is the only thing that updates
    it — from evidence, in the safe direction, once per refusal.

    The refusal reports the concurrency that was *in flight* when it happened,
    not the current limit: those differ while the pool is draining, and the
    number that was proved not to work is the one that was actually running.
    """

    def __init__(self, limit: int, *, floor: int = 1,
                 cooldown: float = 60.0, cooldown_max: float = 600.0):
        self.limit = max(1, int(limit))
        self.started = self.limit
        self.floor = max(1, int(floor))
        self.in_flight = 0
        self.refusals = 0
        self.history: list[str] = []
        self._cooldown = float(cooldown)
        self._cooldown_max = float(cooldown_max)
        self._not_before = 0.0
        self._cond = threading.Condition()

    # -- the pool ---------------------------------------------------------- #

    @contextlib.contextmanager
    def slot(self):
        """Hold one instance's worth of the ceiling; yield the concurrency."""
        with self._cond:
            while True:
                wait = self._not_before - time.monotonic()
                if wait > 0:
                    self._cond.wait(timeout=wait)
                    continue
                if self.in_flight < self.limit:
                    break
                self._cond.wait(timeout=1.0)
            self.in_flight += 1
            mine = self.in_flight
        try:
            yield mine
        finally:
            with self._cond:
                self.in_flight -= 1
                self._cond.notify_all()

    # -- the evidence ------------------------------------------------------ #

    def too_many(self, observed: int) -> str:
        """AWS refused at `observed` concurrent instances. Narrow, and rest."""
        with self._cond:
            self.refusals += 1
            before = self.limit
            self.limit = max(self.floor, min(self.limit, observed) - 1)
            pause = min(self._cooldown_max,
                        self._cooldown * (2 ** (self.refusals - 1)))
            pause *= 0.75 + random.random() * 0.5          # jitter, not a herd
            self._not_before = max(self._not_before, time.monotonic() + pause)
            note = (f"AWS refused at {observed} concurrent instance(s); "
                    f"ceiling {before} -> {self.limit}, "
                    f"pausing {pause:.0f}s before the next launch")
            if self.limit == before == self.floor:
                note += (" — already at the floor, so the account simply has "
                         "no room right now")
            self.history.append(note)
            self._cond.notify_all()
            return note


# --------------------------------------------------------------------------- #
# one smoke test
# --------------------------------------------------------------------------- #


def resolve_uv(explicit: str | None = None) -> str:
    """`uv`, found the way a background shell cannot find it.

    A background bash does not read the profile, so `~/.local/bin` is not on
    `PATH` and `uv` exits 127 — which arrives here as a task that failed to
    start, i.e. as a lie about the task.  Hence the explicit fallback.
    """
    for candidate in (explicit, os.environ.get(UV_ENV), shutil.which("uv"),
                      str(Path.home() / ".local" / "bin" / "uv")):
        if candidate and Path(candidate).exists():
            return str(candidate)
        if candidate and shutil.which(candidate):
            return shutil.which(candidate)
    raise SmokeUnavailable(
        "`uv` was not found. It runs the OSWorld smoke runner in OSWorld's own "
        f"environment; set {UV_ENV} to its path")


def resolve_osworld(explicit: str | Path | None = None) -> Path:
    root = Path(explicit or os.environ.get(OSWORLD_REPO_ENV)
                or DEFAULT_OSWORLD_REPO)
    return root


def runner_path(osworld: Path) -> Path:
    return Path(osworld) / RUNNER_REL


def preflight(osworld: Path, uv: str) -> None:
    """Refuse the batch, once, for anything that would fail every deck."""
    osworld = Path(osworld)
    if not (osworld / "task_loader.py").exists():
        raise SmokeUnavailable(
            f"{osworld} is not an OSWorld-V2 checkout (no task_loader.py); "
            f"set {OSWORLD_REPO_ENV}")
    runner = runner_path(osworld)
    if not runner.exists():
        raise SmokeUnavailable(f"the smoke runner is not at {runner}")
    if not os.environ.get("AWS_ACCESS_KEY_ID") or \
            not os.environ.get("AWS_SECRET_ACCESS_KEY"):
        raise SmokeUnavailable(
            "AWS credentials are not in the environment, so every smoke test "
            "would fail for the same reason and none of it would be about the "
            "tasks. `source .env_vars` first")


def proxy_note(env: dict | None = None) -> str | None:
    """Say so when an HTTP proxy stands between this machine and the VM.

    Measured, on the first real run of this stage: every task that tried to put
    a file on its instance failed with `502`, and the 502 came from
    `http://127.0.0.1:7897` — a proxy configured in this shell's environment —
    rather than from the VM.  The verdict (`infrastructure`, retry) was right;
    the *explanation* would have sent somebody to look at an AMI that was
    working perfectly.

    `no_proxy` here exempts the private ranges only, and the runner talks to
    the instance's **public** IP, so every request to it is proxied.  This is
    not something to fix silently — bypassing the proxy is a decision about
    somebody's network — so it is named and left to a person.
    """
    env = env if env is not None else os.environ
    proxy = env.get("HTTP_PROXY") or env.get("http_proxy") \
        or env.get("HTTPS_PROXY") or env.get("https_proxy")
    if not proxy:
        return None
    bypass = (env.get("NO_PROXY") or env.get("no_proxy") or "")
    if "*" in bypass.split(","):
        return None
    return (f"an HTTP proxy is set in this environment ({proxy}) and no_proxy "
            f"exempts private addresses only, so every request to an "
            f"instance's public IP goes through it — a 5xx here may be the "
            f"proxy's and not the VM's")


def smoke_command(py: Path, out_dir: Path, *, runner: Path, uv: str,
                  instance_type: str | None = None,
                  region: str | None = None) -> list[str]:
    """The argv, built in one place so it can be asserted about.

    Nothing secret is ever on it.  The runner takes its credentials from the
    environment it inherits, and an argv is world-readable in `ps` for as long
    as the process lives.
    """
    cmd = [uv, "run", "python", str(runner),
           "--task-path", str(py), "--output-dir", str(out_dir)]
    if instance_type:
        cmd += ["--instance-type", str(instance_type)]
    if region:
        cmd += ["--region", str(region)]
    return cmd


def run_smoke(py: Path, out_dir: Path, *, runner: Path, osworld: Path,
              uv: str, timeout: float = SMOKE_TIMEOUT,
              instance_type: str | None = None,
              region: str | None = None, env: dict | None = None) -> dict:
    """Provision a VM, run the task's `setup()`, and read what came back.

    Started in its own session so that a deadline can signal the whole process
    group.  `uv run python …` is two processes, and SIGTERM to the wrapper
    alone would leave the runner — and therefore the EC2 instance — alive.  The
    runner turns SIGTERM into a `KeyboardInterrupt` for exactly this reason:
    its `finally` calls `env.close()`, which is what stops the billing.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = smoke_command(py, out_dir, runner=runner, uv=uv,
                        instance_type=instance_type, region=region)
    started = time.time()
    proc = subprocess.Popen(cmd, cwd=str(osworld), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            start_new_session=True,
                            env=dict(env or os.environ))
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop(proc)
        try:
            output, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:                  # pragma: no cover
            output = ""
    return {
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "timeout": timeout,
        "seconds": round(time.time() - started, 1),
        "result": _read_result(out_dir / "result.json"),
        "result_path": str(out_dir / "result.json"),
        "log_causes": cause_lines(out_dir / "setup.log"),
        "stderr_tail": redact("\n".join((output or "").splitlines()[-40:])),
    }


def _stop(proc: subprocess.Popen) -> None:
    """SIGTERM the group, wait for the instance to be released, then kill."""
    for sig, wait in ((signal.SIGTERM, SIGTERM_GRACE), (signal.SIGKILL, 10)):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):      # pragma: no cover
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


#: Lines of `setup.log` that name *why* something failed. The harness writes
#: `Failed to download <url> caused by 404 Client Error: ...` and then throws
#: the status away before it reaches `result.json`; this is where it survives.
_CAUSE = re.compile(r"(?i)caused by|SETUP FAILED|Error details:")

#: How much of the log to look at. The failure is at the end, and a debug-level
#: setup log is megabytes.
_LOG_TAIL_BYTES = 512 * 1024
_MAX_CAUSES = 20


def cause_lines(log: Path) -> list[str]:
    """The lines of `setup.log` that carry a reason, most recent last.

    Deliberately not the whole tail: the boot loop logs connection refusals
    while it waits for the VM's own server, so a scan over everything would
    attribute every task failure to the infrastructure.
    """
    try:
        with open(log, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - _LOG_TAIL_BYTES))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    found = [line.strip() for line in text.splitlines() if _CAUSE.search(line)]
    return [redact(line) for line in found[-_MAX_CAUSES:]]


def _read_result(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------- #
# the batch
# --------------------------------------------------------------------------- #


@dataclass
class Outcome:
    id: str
    verdict: str
    why: str
    attempts: list[dict] = field(default_factory=list)
    seconds: float = 0.0
    #: whether this deck's materials reached the dataset. Recorded rather than
    #: inferred from the verdict: "uploaded but not fetchable" and "did not
    #: upload" are the same verdict and different facts.
    uploaded: bool = False

    @property
    def ships(self) -> bool:
        return self.verdict in SHIPPABLE

    @property
    def unverified(self) -> bool:
        """Not the same claim as "failed", and the summary must keep them apart."""
        return self.verdict in (INFRASTRUCTURE, UNATTRIBUTED)


@dataclass
class Report:
    outcomes: dict[str, Outcome] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    #: things about *this machine* that could be mistaken for a verdict
    notes: list[str] = field(default_factory=list)
    ceiling_started: int = 0
    ceiling_ended: int = 0
    capacity_refusals: int = 0
    ceiling_history: list[str] = field(default_factory=list)

    def of(self, kind: str) -> list[Outcome]:
        return [o for o in self.outcomes.values() if o.verdict == kind]

    @property
    def shipping(self) -> list[str]:
        return sorted(o.id for o in self.outcomes.values() if o.ships)

    @property
    def unverified(self) -> list[Outcome]:
        return [o for o in self.outcomes.values() if o.unverified]


def verify_batch(rows, *, upload, fetch_check, smoke, artefacts: Path,
                 aws_workers: int = DEFAULT_AWS_WORKERS,
                 hf_workers: int = DEFAULT_HF_WORKERS,
                 attempts: int = 3, capacity_retries: int = 6,
                 cooldown: float = 60.0, log=None) -> Report:
    """Upload, fetch-check and smoke-test every task; barrier only at the end.

    One thread per deck, two independent limits.  The thread runs the deck's
    whole pipeline, so a deck that finished its upload is smoke-testing while
    its neighbour is still uploading — the alternative, three batch passes,
    makes every deck as slow as the slowest deck in the phase before it.

    The limits are semaphore-shaped rather than thread-shaped for the reason
    `cli.Pools` gives: how many decks are in flight and how much of each
    resource is in use are different questions, and a deck waiting for a slot
    costs nothing.  A thread releases the upload slot before it asks for an AWS
    slot, so a four-minute VM boot never holds an upload's turn.

    `upload`, `fetch_check` and `smoke` are injected: this module knows the
    order and the arithmetic, `publish` knows Hugging Face, and a test knows
    neither.
    """
    from concurrent.futures import ThreadPoolExecutor

    rows = list(rows)
    report = Report(ceiling_started=aws_workers,
                    notes=[n for n in (proxy_note(),) if n])
    ceiling = Ceiling(aws_workers, cooldown=cooldown)
    hf = threading.Semaphore(max(1, hf_workers))
    lock = threading.Lock()

    def say(line: str) -> None:
        line = redact(line)
        with lock:
            report.events.append(line)
        if log:
            log(line)

    def one(row) -> Outcome:
        tid = str(row["id"])
        out = Outcome(tid, UNATTRIBUTED, "never ran")
        began = time.time()
        try:
            with hf:
                try:
                    upload(row)
                except Exception as error:                 # noqa: BLE001
                    out.verdict, out.why = MATERIALS_MISSING, (
                        f"the materials did not upload: "
                        f"{type(error).__name__}: {redact(error)}")
                    return out
            out.uploaded = True
            say(f"task_{tid}: materials uploaded")

            missing = list(fetch_check(row) or [])
            if missing:
                out.verdict, out.why = MATERIALS_MISSING, (
                    f"uploaded, but the dataset does not serve it: "
                    f"{redact(missing[0])}")
                return out

            tries, capacity_left = attempts, capacity_retries
            while True:
                n = len(out.attempts) + 1
                where = Path(artefacts) / f"task_{tid}" / f"attempt-{n}"
                with ceiling.slot() as concurrent:
                    say(f"task_{tid}: smoke test attempt {n} "
                        f"({concurrent} instance(s) in flight)")
                    raw = smoke(row, where)
                verdict = classify(raw)
                out.attempts.append({
                    "n": n, "verdict": verdict.kind, "why": verdict.why,
                    "status": (raw.get("result") or {}).get("status"),
                    "seconds": raw.get("seconds"),
                    "dir": str(where),
                })
                out.verdict, out.why = verdict.kind, verdict.why

                if verdict.kind in (OK, TASK_BROKEN):
                    return out
                if verdict.capacity:
                    note = ceiling.too_many(concurrent)
                    say(f"task_{tid}: {note}")
                    capacity_left -= 1
                    if capacity_left <= 0:
                        out.why = (f"{verdict.why}; the account had no room "
                                   f"for it after {capacity_retries} tries")
                        return out
                    continue            # a quota refusal is not an attempt
                tries -= 1
                say(f"task_{tid}: {verdict.kind} — {verdict.why} "
                    f"({tries} retr{'y' if tries == 1 else 'ies'} left)")
                if tries <= 0:
                    return out
        finally:
            out.seconds = round(time.time() - began, 1)
            with lock:
                report.outcomes[tid] = out

    if rows:
        width = min(len(rows), max(1, aws_workers) + max(1, hf_workers) + 4)
        with ThreadPoolExecutor(max_workers=width,
                                thread_name_prefix="pptxgym-vm") as pool:
            for future in [pool.submit(one, row) for row in rows]:
                future.result()

    report.ceiling_ended = ceiling.limit
    report.capacity_refusals = ceiling.refusals
    report.ceiling_history = list(ceiling.history)
    return report


# --------------------------------------------------------------------------- #
# saying what happened
# --------------------------------------------------------------------------- #


def render(report: Report) -> str:
    """The summary, with the two kinds of failure kept apart on purpose."""
    lines = []
    verified = [o for o in report.outcomes.values() if o.ships]
    lines.append(f"vm check   {len(verified)}/{len(report.outcomes)} task(s) "
                 f"ran setup() on a real VM")
    for o in sorted(verified, key=lambda o: o.id):
        lines.append(f"    ok task_{o.id}  {o.seconds:.0f}s")

    broken = report.of(TASK_BROKEN) + report.of(MATERIALS_MISSING)
    if broken:
        lines.append(f"broken, not shipped ({len(broken)}) — these are task "
                     f"defects:")
        for o in sorted(broken, key=lambda o: o.id):
            lines.append(f"    x task_{o.id}  {o.why}")

    if report.unverified:
        lines.append(f"unverified, not shipped ({len(report.unverified)}) — "
                     f"the infrastructure failed, not the task; nothing here "
                     f"is a claim that these tasks are broken:")
        for o in sorted(report.unverified, key=lambda o: o.id):
            lines.append(f"    ? task_{o.id}  {o.why} "
                         f"({len(o.attempts)} attempt(s))")

    if report.capacity_refusals:
        lines.append(f"aws ceiling  started {report.ceiling_started}, ended "
                     f"{report.ceiling_ended}, {report.capacity_refusals} "
                     f"capacity refusal(s)")
        for note in report.ceiling_history[:4]:
            lines.append(f"    · {note}")
    else:
        lines.append(f"aws ceiling  {report.ceiling_started}, never refused")
    for note in report.notes:
        lines.append(f"note          {note}")
    return "\n".join(lines)
