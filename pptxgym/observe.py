"""Watch a run from outside it, and say afterwards what actually happened.

    python -m pptxgym.observe watch --interval 10
    python -m pptxgym.observe report work/observe-1785856000.jsonl

Nothing in this pipeline records *achieved* concurrency.  `--agent-workers 8`
is a limit we asked for; it is not evidence that eight agent stages were ever
running at once, and a log that shows decks moving cannot tell the difference
between eight busy slots and one busy slot with seven decks queued behind it.
That difference is the whole question when the ten-deck rehearsal is used to
size a hundred-deck run: if the pools were never full, raising them buys
nothing and the constraint is somewhere else.

**This samples from outside the run and instruments nothing.**  It opens no
sockets into the pipeline, imports none of its scheduler, and writes nothing
the pipeline reads.  Everything comes from four read-only sources:

  * `/proc` — who is alive, whose child they are, and how much they hold
  * `work/*/state.json` — what each deck has finished
  * `work/*/.lock` — what each deck claims to be doing, and under which pid
  * `/proc/locks` — which display-pool `flock`s are held, by whom

The last one deserves a note: the obvious way to ask whether a display lock is
held is to try to take it, and that is exactly what an observer must not do.
A non-blocking `flock` that succeeds for a millisecond is a display the
pipeline could not have had in that millisecond, and at a hundred decks
against a span of 64 that is a `NoFreeDisplay` the observer caused.
`/proc/locks` answers the same question by reading, and throws in the holder's
pid for free.

The timeline is JSONL: one `header` record, then one `sample` record per tick.
`report` reads it back.  Sampling is the point — an average over samples is an
unbiased estimate of occupancy, and a peak over samples is a lower bound on
the true peak, which is the honest direction for that error to run.

What sampling cannot see is written down in `BLIND_SPOTS`, and the report
prints it.  A sampler whose limitations are not stated next to its numbers
gets believed past the point where it is right.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = 1

# --------------------------------------------------------------------------- #
# what the pipeline looks like from outside
# --------------------------------------------------------------------------- #

#: Taken from the pipeline rather than restated, so a stage added there is not
#: silently invisible here.  Imported defensively: the observer has to keep
#: working when it is pointed at a work directory produced by another checkout.
try:                                                             # pragma: no cover
    from . import pipeline as _pl
    STAGES: list[str] = list(_pl.STAGES)
    AGENT_STAGES: set[str] = set(_pl.AGENT_STAGES)
    PROMOTES: dict = dict(_pl.PROMOTES)
except Exception:                                                # noqa: BLE001
    STAGES = ["ingested", "inspected", "proposed", "recipe", "degraded",
              "materialised", "reconciled", "solvable",
              "scored", "hardened", "packaged"]
    AGENT_STAGES = {"proposed", "recipe", "reconciled", "solvable"}
    PROMOTES = {"materialised": ("ok", "partial")}

#: `repair` is an agent stage under another name — it spends the same currency
#: and takes a slot from the same pool.  `cli._AGENT_WORK` says so; this is the
#: same statement, made without importing a module somebody else is rewriting.
AGENT_WORK = AGENT_STAGES | {"repair"}

#: Statuses that mean the deck stopped and will not move again without a human.
PARKED = ("needs_human", "crashed", "failed", "infra")

#: Process names that mean an agent stage is executing.  `claude -p` is the
#: only thing in this pipeline that spends API capacity.
AGENT_PROCS = ("claude",)

#: Process names that mean CPU work is executing.  Note that these are *not*
#: pool slots: `harden` runs `attack_workers` soffice conversions inside one
#: cpu slot, so four soffices can belong to one deck.  Counted anyway, because
#: "how many soffices are on this box" is the number that predicts the OOM.
CPU_PROCS = ("soffice", "soffice.bin", "oosplash", "pdftoppm", "pdfimages",
             "wpp", "wps", "et", "Xvfb", "xdotool", "libreoffice")

_HELPERS = set(AGENT_PROCS) | set(CPU_PROCS)

#: How long an agent stage may hold its lock without writing a byte to its
#: stream-json log before we call it stalled.  `claude -p` emits a record per
#: assistant turn and per tool result; the longest quiet stretch measured in
#: the ten-deck run's logs was well under a minute.
STALL_AFTER = 300.0

BLIND_SPOTS = [
    "A stage shorter than the interval can fall between two samples entirely; "
    "`materialise` runs in ~6s, so peak CPU concurrency is a lower bound.",
    "A helper that reparents to init before the first sample (a leftover Xvfb "
    "or wpp from an earlier run) is attributed to nobody unless it holds a "
    "file under the work directory.",
    "`claude` processes are attributed by descent from the run's pid, so an "
    "agent stage started by a different process — a hand-run sub-command — is "
    "counted only if its own pptxgym process is passed with --pid.",
    "Deck-to-process attribution does not exist: the run is the parent of "
    "every `claude`, so a stalled deck is found from its log's mtime, not "
    "from a process.",
    "`inspected` and `materialised` take no deck lock, so a deck inside one "
    "of those stages reads as queued for one or two samples.",
]


# --------------------------------------------------------------------------- #
# a process table, from /proc, behind a seam a test can replace
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Proc:
    """One process, as much of it as this needs.

    `start_ticks` is field 22 of `/proc/<pid>/stat`.  It is here so that
    attribution can be sticky without being wrong: a pid that is reused inside
    a long run would otherwise inherit the ownership of the process that used
    to hold it, and inherit it invisibly.
    """

    pid: int
    ppid: int
    name: str
    cmdline: str
    rss: int = 0                 # bytes
    start_ticks: int = 0

    @property
    def key(self) -> tuple:
        return (self.pid, self.start_ticks)


class Probe:
    """Everything the sampler reads from the machine, in one replaceable object.

    The fixtures in `tests/test_observe.py` hand `sample()` a `FakeProbe` with
    a process table somebody wrote by hand and knows the answer to.  That is
    the only way to test a sampler: there is no run to point it at, and a
    sampler checked against a real run is checked against numbers nobody
    independently knows.
    """

    def now(self) -> float:
        return time.time()

    def processes(self) -> list[Proc]:                           # pragma: no cover
        return read_procs()

    def flock_holders(self) -> dict[tuple, int]:                 # pragma: no cover
        """(st_dev, st_ino) -> pid, for every `flock` held on the machine."""
        return read_flocks()

    def meminfo(self) -> dict[str, int]:                         # pragma: no cover
        return read_meminfo()

    def holds_path(self, pid: int, root: Path) -> bool:          # pragma: no cover
        """Does this process have its cwd, or any open file, under `root`?"""
        return _holds_path(pid, root)


_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def read_procs(proc_root: str = "/proc") -> list[Proc]:
    """The process table, from /proc, tolerating processes that exit mid-read.

    `comm` may contain spaces and parentheses (`(sd-pam)`), so the fields
    after it are found from the *last* `)` rather than by splitting.
    """
    out: list[Proc] = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return out
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"{proc_root}/{pid}/stat", "rb") as fh:
                stat = fh.read().decode("utf-8", "replace")
            rp = stat.rfind(")")
            comm = stat[stat.find("(") + 1:rp]
            rest = stat[rp + 2:].split()
            ppid = int(rest[1])
            start_ticks = int(rest[19])
            rss = int(rest[21]) * _PAGE
        except (OSError, ValueError, IndexError):
            continue                     # exited between listdir and open
        try:
            with open(f"{proc_root}/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().decode("utf-8", "replace").replace("\0", " ").strip()
        except OSError:
            cmd = ""
        out.append(Proc(pid=pid, ppid=ppid, name=comm, cmdline=cmd or comm,
                        rss=rss, start_ticks=start_ticks))
    return out


_LOCK_RE = re.compile(
    r"^\s*\d+:\s+(\S+)\s+(\S+)\s+(\S+)\s+(\d+)\s+([0-9a-fA-F]+):([0-9a-fA-F]+):(\d+)")


def read_flocks(path: str = "/proc/locks") -> dict[tuple, int]:
    """Every `flock` held on the machine, keyed by the file it is held on.

    `/proc/locks` prints the device as `MAJ:MIN` in hex and the inode in
    decimal, which is why the two halves of the key are parsed differently.
    POSIX record locks are skipped: the display pool uses `flock`, and a
    POSIX lock on the same inode would be a different claim by somebody else.
    """
    out: dict[tuple, int] = {}
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        m = _LOCK_RE.match(line)
        if not m or m.group(1) != "FLOCK":
            continue
        pid = int(m.group(4))
        dev = os.makedev(int(m.group(5), 16), int(m.group(6), 16))
        out[(dev, int(m.group(7)))] = pid
    return out


def read_meminfo(path: str = "/proc/meminfo") -> dict[str, int]:
    """`MemTotal`, `MemAvailable`, `SwapTotal`, `SwapFree`, in bytes."""
    out: dict[str, int] = {}
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        k, _, v = line.partition(":")
        v = v.strip().split()
        if not v:
            continue
        try:
            out[k] = int(v[0]) * (1024 if len(v) > 1 and v[1] == "kB" else 1)
        except ValueError:
            continue
    return out


def _holds_path(pid: int, root: Path) -> bool:
    """cwd or any open fd under `root`.  Used only to adopt orphaned helpers."""
    root = str(root)
    try:
        if os.path.realpath(f"/proc/{pid}/cwd").startswith(root):
            return True
    except OSError:
        return False
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                if os.readlink(f"/proc/{pid}/fd/{fd}").startswith(root):
                    return True
            except OSError:
                continue
    except OSError:
        pass
    return False


# --------------------------------------------------------------------------- #
# whose processes are these
# --------------------------------------------------------------------------- #

_SUBCOMMANDS = {"run", "ingest", "inspect", "propose", "recipe", "degrade",
                "materialise", "reconcile", "solvable", "score", "harden",
                "package", "repair", "wps", "status"}


def looks_like_pipeline(cmdline: str) -> bool:
    """Is this argv a pptxgym pipeline process rather than something else?

    Deliberately narrow.  `--pid` is the supported way to say which run to
    watch; auto-detection exists so that the common case needs no ceremony,
    and a false positive here would poison every number in the timeline.
    """
    toks = cmdline.split()
    if not any("pptxgym" in t for t in toks):
        return False
    if any("observe" in t for t in toks):
        return False                     # this program, or another observer
    return any(t in _SUBCOMMANDS for t in toks)


def _ancestors(pid: int, by_pid: dict[int, Proc]) -> set[int]:
    seen, cur = set(), pid
    while cur in by_pid and cur not in seen:
        seen.add(cur)
        cur = by_pid[cur].ppid
    return seen


@dataclass
class Attribution:
    """Which of the machine's processes belong to the run being watched.

    This machine runs several `claude` processes that are somebody's
    subagents, not pipeline stages.  Counting those would not make the numbers
    noisy, it would make every one of them wrong — five subagents and one
    agent stage reads as a full agent pool.

    **The rule is descent.**  A process belongs to the run if it is the run's
    process or a transitive child of it.  `claude -p` is spawned by
    `asyncio.create_subprocess_exec` directly from the run, and `soffice` by
    `subprocess.run`, so both are children at depth one or two, and no
    subagent of a different `claude` session is anywhere underneath.

    Two additions, because descent alone loses things:

    *Sticky.*  A process that was ours at an earlier sample stays ours while
    it is alive, keyed on (pid, start time) so a reused pid is not inherited.
    `wps_roundtrip` starts Xvfb and wpp in their own session, so if the run
    dies they reparent to init — and those are exactly the processes still
    holding memory that a post-mortem wants to see.

    *Work directory.*  A helper we do not otherwise own, but which has its cwd
    or an open file under the work tree, is adopted.  This catches a helper
    already orphaned before the observer started.

    What it misses is in `BLIND_SPOTS`, and the sample carries a count of the
    `claude` and `soffice` processes it decided were *not* ours, so an
    attribution rule that has gone wrong shows up as a number rather than as a
    silence.
    """

    work: Path
    roots: list[int] = field(default_factory=list)
    sticky: bool = True
    _seen: set = field(default_factory=set, repr=False)

    def find_roots(self, procs: list[Proc]) -> list[int]:
        """Auto-detect the run, excluding this process and its own ancestors.

        `python -m pptxgym.observe` has "pptxgym" in its argv, as does the
        shell that launched it; taking either as the run would make the
        observer's own tree the thing it measures.
        """
        by_pid = {p.pid: p for p in procs}
        mine = _ancestors(os.getpid(), by_pid) | {os.getpid()}
        return sorted(p.pid for p in procs
                      if p.pid not in mine and looks_like_pipeline(p.cmdline))

    def resolve(self, procs: list[Proc]) -> dict[int, str]:
        """pid -> why it is ours.  Empty when no run can be found."""
        by_pid = {p.pid: p for p in procs}
        roots = [r for r in (self.roots or self.find_roots(procs)) if r in by_pid]
        # a root that is itself a child of another root is not a second run
        roots = [r for r in roots
                 if not (_ancestors(r, by_pid) - {r}) & set(roots)]

        kids: dict[int, list[int]] = {}
        for p in procs:
            kids.setdefault(p.ppid, []).append(p.pid)

        owned: dict[int, str] = {}
        stack = list(roots)
        while stack:
            pid = stack.pop()
            if pid in owned:
                continue
            owned[pid] = "root" if pid in roots else "descendant"
            stack += kids.get(pid, [])

        if self.sticky:
            for p in procs:
                if p.pid not in owned and p.key in self._seen:
                    owned[p.pid] = "sticky"

        for p in procs:
            if p.pid in owned or p.name not in _HELPERS:
                continue
            if self.probe_holds(p.pid):
                owned[p.pid] = "workdir"

        self._seen = {by_pid[pid].key for pid in owned if pid in by_pid}
        self.found_roots = roots
        return owned

    #: overridden by the sampler so a fixture can answer without a real /proc
    def probe_holds(self, pid: int) -> bool:                     # pragma: no cover
        return _holds_path(pid, self.work)


# --------------------------------------------------------------------------- #
# what each deck is doing, from its own files
# --------------------------------------------------------------------------- #

#: The log an agent stage streams into while it runs.  A deck locked on one of
#: these whose log has not grown is the deck the run should be stopped for.
def _stage_log(deck_dir: Path, stage: str) -> Path | None:
    if stage == "repair":
        got = sorted(deck_dir.glob("repair-*.jsonl"))
        return got[-1] if got else None
    if stage in AGENT_STAGES:
        return deck_dir / f"{stage}.jsonl"
    return None


def _promoted(status: str | None, stage: str) -> bool:
    return status in PROMOTES.get(stage, ("ok",))


def read_state(deck_dir: Path) -> dict:
    try:
        return json.loads((deck_dir / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def read_lock(deck_dir: Path, alive: set[int],
              now: float | None = None) -> dict | None:
    """The deck's `.lock`, plus whether the pid in it is still running.

    A lock whose holder is gone is the single most useful thing this observer
    can find: the deck is not running, nothing will ever release it, and every
    other view of the run says the deck is busy.
    """
    f = deck_dir / ".lock"
    try:
        held = json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    pid = held.get("pid")
    return {"stage": held.get("stage"), "pid": pid, "at": held.get("at"),
            "alive": bool(pid) and pid in alive,
            "age_s": _age(f, now)}


def _age(p: Path, now: float | None = None) -> float | None:
    try:
        return round((now or time.time()) - p.stat().st_mtime, 1)
    except OSError:
        return None


def deck_view(deck_dir: Path, alive: set[int], now: float,
              until: str | None = None) -> dict:
    """One deck's row in a sample: what it finished, what it is on, since when.

    Stage progress is read straight out of `state.json` rather than through
    `pipeline.Deck.promoted`, which re-hashes every input of every stage to
    decide whether a tick is stale.  That is right for a scheduler and wrong
    for something that runs every ten seconds against a hundred decks — the
    observer would spend its interval hashing `input.pptx`.  The cost is that
    a stage invalidated by an edit mid-run reads here as done; the raw
    statuses are in the sample, so the report can be recomputed if that ever
    matters.
    """
    st = read_state(deck_dir)
    last = until or STAGES[-1]
    horizon = STAGES[:STAGES.index(last) + 1] if last in STAGES else STAGES

    done_through, nxt = "", None
    for s in horizon:
        if _promoted(st.get(s, {}).get("status"), s):
            done_through = s
        else:
            nxt = s
            break

    parked = None
    for s in horizon:
        status = st.get(s, {}).get("status")
        if status in PARKED:
            parked = {"stage": s, "status": status}
            break

    lock = read_lock(deck_dir, alive, now)
    running_stage = lock["stage"] if lock and lock["alive"] else None
    pool = _pool_of(running_stage or nxt)

    row = {
        "id": deck_dir.name,
        "done_through": done_through,
        "next": nxt,
        "finished": nxt is None,
        "parked": parked,
        "pool": pool,
        "stage": running_stage or nxt,
        "running": running_stage is not None,
        "since_s": None,
        "lock": lock,
    }

    # how long it has been where it is: the lock's mtime while it holds one,
    # otherwise the age of the last thing written into the deck
    if lock:
        row["since_s"] = lock["age_s"]
    else:
        row["since_s"] = _age(deck_dir / "state.json", now)

    log = _stage_log(deck_dir, running_stage) if running_stage else None
    if log is not None and log.exists():
        row["log"] = log.name
        row["log_age_s"] = _age(log, now)
        try:
            row["log_bytes"] = log.stat().st_size
        except OSError:
            pass

    # eligible to run and not running: this is what queue depth counts
    row["queued"] = bool(nxt) and not row["running"] and parked is None
    return row


def _pool_of(stage: str | None) -> str | None:
    if stage is None:
        return None
    return "agent" if stage in AGENT_WORK else "cpu"


# --------------------------------------------------------------------------- #
# the display pool
# --------------------------------------------------------------------------- #


def display_pool_conf() -> dict:
    """Base, span and lock directory — from `wps_roundtrip` where possible."""
    try:                                                         # pragma: no cover
        from . import wps_roundtrip as w
        return {"base": w.DISPLAY_BASE, "span": w.DISPLAY_SPAN,
                "dir": str(w.LOCK_DIR)}
    except Exception:                                            # noqa: BLE001
        return {"base": 99, "span": 64,
                "dir": os.environ.get(
                    "PPTXGYM_DISPLAY_LOCKS",
                    str(Path(tempfile.gettempdir()) / "pptxgym-displays"))}


def display_occupancy(conf: dict, flocks: dict[tuple, int],
                      owned: dict[int, str]) -> dict:
    """How many of the pool's `flock`s are held, and by whom.

    Read-only by construction — see the module docstring on why the observer
    must not try to take a lock to find out whether it is free.

    A lock *file* that exists proves nothing: `DisplayPool` never unlinks it
    and the pid written inside is whoever held it last.  Only `/proc/locks`
    says it is held now.
    """
    d = Path(conf["dir"])
    holders, held = [], 0
    for n in range(conf["base"], conf["base"] + conf["span"]):
        f = d / f"display{n}.lock"
        try:
            st = f.stat()
        except OSError:
            continue
        pid = flocks.get((st.st_dev, st.st_ino))
        if pid is None:
            continue
        held += 1
        holders.append({"display": n, "pid": pid, "owned": pid in owned})
    return {"base": conf["base"], "span": conf["span"], "dir": conf["dir"],
            "held": held, "by_run": sum(1 for h in holders if h["owned"]),
            "free": conf["span"] - held,
            "saturated": held >= conf["span"],
            "holders": holders, "method": "proc-locks"}


# --------------------------------------------------------------------------- #
# one sample
# --------------------------------------------------------------------------- #


def _limits_from_argv(cmdline: str) -> dict:
    """The limits the run was actually started with, read off its own argv.

    Better than being told: the flag the operator typed and the flag they
    meant are not always the same, and the whole point of this file is to
    compare what was asked for against what happened.
    """
    toks = cmdline.split()
    out: dict = {}

    def _val(*names):
        for i, t in enumerate(toks):
            for n in names:
                if t == n and i + 1 < len(toks):
                    return toks[i + 1]
                if t.startswith(n + "="):
                    return t.split("=", 1)[1]
        return None

    for key, names in (("agent", ("--workers", "--agent-workers")),
                       ("cpu", ("--cpu-workers",))):
        v = _val(*names)
        if v and v.lstrip("-").isdigit():
            out[key] = int(v)
    v = _val("--until")
    if v:
        out["until"] = v
    return out


def _default_cpu_workers() -> int:
    return max(2, (os.cpu_count() or 4) // 4)


@dataclass
class Sampler:
    """Takes one sample of a running pipeline.  Holds only what must persist.

    The only state carried between samples is the attribution's memory of pids
    it has already claimed.  Everything else is read fresh, so a sample is
    interpretable on its own and a missed tick costs one row and nothing else.
    """

    work: Path
    probe: Probe = field(default_factory=Probe)
    roots: list[int] = field(default_factory=list)
    limits: dict = field(default_factory=dict)
    until: str | None = None
    stall_after: float = STALL_AFTER
    seq: int = 0
    started: float = 0.0
    _attr: Attribution | None = None

    def __post_init__(self):
        self.work = Path(self.work)
        self._attr = Attribution(work=self.work.resolve(), roots=list(self.roots))
        # the fixture's probe answers this too, so adoption is testable
        self._attr.probe_holds = lambda pid: self.probe.holds_path(
            pid, self.work.resolve())
        if not self.started:
            self.started = self.probe.now()

    # -- the pieces --------------------------------------------------------- #

    def deck_dirs(self) -> list[Path]:
        try:
            return sorted(p for p in self.work.glob("deck*") if p.is_dir())
        except OSError:
            return []

    def effective_limits(self, procs: list[Proc], roots: list[int]) -> dict:
        """Explicit flags win; otherwise read the run's argv; otherwise guess.

        Recorded with its provenance, because "the limit was 8" and "we
        assumed the limit was 8 because nobody said" are different claims and
        only one of them belongs in a report.
        """
        out = dict(self.limits)
        src = "flag" if out else None
        if len(out) < 2 and roots:
            by_pid = {p.pid: p for p in procs}
            for r in roots:
                got = _limits_from_argv(by_pid[r].cmdline) if r in by_pid else {}
                for k in ("agent", "cpu"):
                    if k not in out and k in got:
                        out[k] = got[k]
                        src = src or "run-argv"
                if "until" in got and not self.until:
                    self.until = got["until"]
        if "agent" not in out:
            out["agent"] = 1
            src = src or "default"
        if "cpu" not in out:
            out["cpu"] = _default_cpu_workers()
            src = src or "default"
        out["source"] = src or "default"
        return out

    # -- the sample --------------------------------------------------------- #

    def sample(self) -> dict:
        now = self.probe.now()
        procs = self.probe.processes()
        by_pid = {p.pid: p for p in procs}
        owned = self._attr.resolve(procs)
        roots = getattr(self._attr, "found_roots", [])
        alive = set(by_pid)
        limits = self.effective_limits(procs, roots)

        decks = [deck_view(d, alive, now, self.until) for d in self.deck_dirs()]

        # -- pool occupancy, from the locks ---------------------------------- #
        pools = {}
        for pool in ("agent", "cpu"):
            run_ids = [d["id"] for d in decks
                       if d["running"] and d["pool"] == pool]
            q_ids = [d["id"] for d in decks if d["queued"] and d["pool"] == pool]
            pools[pool] = {"limit": limits[pool], "running": len(run_ids),
                           "queued": len(q_ids), "decks": run_ids,
                           "queue": q_ids}

        # -- and from the processes, which is the independent view ----------- #
        by_name: dict[str, int] = {}
        rss_by_name: dict[str, int] = {}
        rss_total = 0
        largest = None
        for pid, why in owned.items():
            p = by_pid.get(pid)
            if p is None:
                continue
            rss_total += p.rss
            by_name[p.name] = by_name.get(p.name, 0) + 1
            rss_by_name[p.name] = rss_by_name.get(p.name, 0) + p.rss
            if largest is None or p.rss > largest["rss"]:
                largest = {"pid": p.pid, "name": p.name, "rss": p.rss,
                           "cmd": p.cmdline[:120]}
        pools["agent"]["procs"] = sum(by_name.get(n, 0) for n in AGENT_PROCS)
        pools["cpu"]["procs"] = sum(by_name.get(n, 0) for n in CPU_PROCS)

        unattributed: dict[str, int] = {}
        for p in procs:
            if p.pid in owned or p.name not in _HELPERS:
                continue
            unattributed[p.name] = unattributed.get(p.name, 0) + 1

        # -- decks that are not where they say they are ---------------------- #
        hung = []
        for d in decks:
            lk = d["lock"]
            if lk and not lk["alive"]:
                hung.append({"id": d["id"], "stage": lk["stage"],
                             "why": f"lock held by dead pid {lk['pid']}",
                             "since_s": d["since_s"]})
            elif (d["running"] and d["stage"] in AGENT_WORK
                  and (d.get("log_age_s") or 0) > self.stall_after):
                hung.append({"id": d["id"], "stage": d["stage"],
                             "why": f"no log output for {d['log_age_s']}s",
                             "since_s": d["since_s"]})

        warn = []
        if pools["agent"]["running"] != pools["agent"]["procs"]:
            warn.append(
                f"agent locks say {pools['agent']['running']} running, "
                f"{pools['agent']['procs']} claude process(es) attributed")
        if not roots and (pools["agent"]["queued"] or pools["cpu"]["queued"]
                          or pools["agent"]["running"]
                          or pools["cpu"]["running"]):
            # queue depth means "eligible and not running", which only means
            # "held back by a limit" while there is a scheduler to hold it
            warn.append("no pptxgym process visible in /proc: the queue "
                        "below is decks with work left, not decks a limit is "
                        "holding back (pass --pid if the run is elsewhere)")
        for name, n in unattributed.items():
            if name in AGENT_PROCS:
                warn.append(f"{n} {name} process(es) on this box are not part "
                            f"of the run and were not counted")

        mem = self.probe.meminfo()
        displays = display_occupancy(display_pool_conf(),
                                     self.probe.flock_holders(), owned)
        if displays["saturated"]:
            warn.append(f"display pool saturated: all {displays['span']} held")

        self.seq += 1
        return {
            "v": SCHEMA, "kind": "sample", "seq": self.seq,
            "t": round(now, 3),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "elapsed_s": round(now - self.started, 1),
            "work": str(self.work),
            "roots": [{"pid": r, "cmd": by_pid[r].cmdline[:200]}
                      for r in roots if r in by_pid],
            "limits": limits,
            "pools": pools,
            "decks": decks,
            "counts": {
                "decks": len(decks),
                "running": sum(1 for d in decks if d["running"]),
                "queued": sum(1 for d in decks if d["queued"]),
                "finished": sum(1 for d in decks if d["finished"]),
                "parked": sum(1 for d in decks if d["parked"]),
            },
            "procs": {"owned": len(owned), "by_name": by_name,
                      "unattributed": unattributed,
                      "reasons": _tally(owned.values())},
            "mem": {
                "rss_total": rss_total,
                "rss_by_name": rss_by_name,
                "largest": largest,
                "avail": mem.get("MemAvailable"),
                "total": mem.get("MemTotal"),
                "swap_used": (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0))
                             if "SwapTotal" in mem else None,
            },
            "displays": displays,
            "hung": hung,
            "warn": warn,
        }


def _tally(xs) -> dict:
    out: dict = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out


# --------------------------------------------------------------------------- #
# the live line
# --------------------------------------------------------------------------- #


def _gb(n) -> str:
    return "?" if n is None else f"{n / 1e9:.1f}G"


def live_line(s: dict) -> str:
    """One line per sample, dense enough to watch and short enough to read."""
    a, c = s["pools"]["agent"], s["pools"]["cpu"]
    m = s["mem"]
    big = m.get("largest") or {}
    bits = [
        f"{s['iso'][11:]}",
        f"+{int(s['elapsed_s']) // 60:d}m{int(s['elapsed_s']) % 60:02d}s",
        f"agent {a['running']}/{a['limit']}"
        + (f" q{a['queued']}" if a["queued"] else "")
        + (f" [{a['procs']}p]" if a["procs"] != a["running"] else ""),
        f"cpu {c['running']}/{c['limit']}"
        + (f" q{c['queued']}" if c["queued"] else "")
        + (f" [{c['procs']}p]" if c["procs"] else ""),
        f"rss {_gb(m['rss_total'])}/{_gb(m.get('avail'))} avail",
        f"max {int((big.get('rss') or 0) / 1e6)}M {big.get('name', '—')}",
        f"disp {s['displays']['by_run']}/{s['displays']['span']}",
        f"done {s['counts']['finished']}/{s['counts']['decks']}",
    ]
    line = "  ".join(bits)
    if s["hung"]:
        line += "  !! " + ", ".join(f"{h['id']} {h['why']}" for h in s["hung"])
    return line


def transitions(prev: dict | None, cur: dict) -> list[str]:
    """What changed between two samples, as lines worth printing.

    A tail of the log tells you a deck moved; this tells you *when* and *from
    what*, which is the thing you cannot reconstruct afterwards from a log
    that only records completions.
    """
    if prev is None:
        return []
    was = {d["id"]: d for d in prev["decks"]}
    out = []
    for d in cur["decks"]:
        b = was.get(d["id"])
        if b is None:
            out.append(f"    {d['id']} appeared")
            continue
        if b["done_through"] != d["done_through"] and d["done_through"]:
            out.append(f"    {d['id']} finished {d['done_through']}")
        if b["stage"] != d["stage"] or b["running"] != d["running"]:
            verb = "started" if d["running"] else "left"
            out.append(f"    {d['id']} {verb} {d['stage'] or '—'}"
                       + (f" (waited {b['since_s']}s)"
                          if d["running"] and not b["running"] and b["since_s"]
                          else ""))
        if d["parked"] and not b["parked"]:
            out.append(f"    {d['id']} PARKED at {d['parked']['stage']} "
                       f"({d['parked']['status']})")
    return out


# --------------------------------------------------------------------------- #
# watch
# --------------------------------------------------------------------------- #


def watch(work: Path, out: Path, interval: float = 10.0,
          probe: Probe | None = None, roots: list[int] | None = None,
          limits: dict | None = None, until: str | None = None,
          max_samples: int | None = None, quiet: bool = False,
          stall_after: float = STALL_AFTER, sleep=time.sleep) -> Path:
    """Sample until interrupted, appending to `out` and printing as it goes.

    Appends rather than truncates, and flushes every sample: the reason to
    have this at all is the run that dies, and a timeline buffered in memory
    is a timeline that goes down with it.
    """
    s = Sampler(work=Path(work), probe=probe or Probe(), roots=list(roots or []),
                limits=dict(limits or {}), until=until, stall_after=stall_after)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = {"v": SCHEMA, "kind": "header", "started": s.started,
              "iso": time.strftime("%Y-%m-%dT%H:%M:%S",
                                   time.localtime(s.started)),
              "work": str(Path(work).resolve()), "interval_s": interval,
              "stages": STAGES, "agent_stages": sorted(AGENT_WORK),
              "argv": " ".join(sys.argv), "blind_spots": BLIND_SPOTS}
    prev = None
    n = 0
    with open(out, "a", buffering=1) as fh:
        fh.write(json.dumps(header, ensure_ascii=False) + "\n")
        if not quiet:
            print(f"watching {work} every {interval}s -> {out}")
        while max_samples is None or n < max_samples:
            try:
                cur = s.sample()
            except Exception as e:                               # noqa: BLE001
                cur = {"v": SCHEMA, "kind": "error", "t": time.time(),
                       "error": f"{type(e).__name__}: {e}"}
                fh.write(json.dumps(cur, ensure_ascii=False) + "\n")
                if not quiet:
                    print(f"  sample failed: {cur['error']}")
                n += 1
                if max_samples is None or n < max_samples:
                    sleep(interval)
                continue
            fh.write(json.dumps(cur, ensure_ascii=False) + "\n")
            if not quiet:
                for line in transitions(prev, cur):
                    print(line)
                print(live_line(cur))
            prev = cur
            n += 1
            if max_samples is None or n < max_samples:
                sleep(interval)
    return out


# --------------------------------------------------------------------------- #
# tokens, from the agent logs
# --------------------------------------------------------------------------- #

RESULT_TAIL = 32000


def last_result(log: Path) -> dict:
    """The `type: result` record `claude` ends a run with, or `{}`.

    A local copy of `agent.last_result` on purpose: the analyser reads logs
    from runs made by other checkouts of this pipeline, and an observer that
    stops working when a module it imported is refactored is an observer that
    is not there on the day it is wanted.
    """
    try:
        tail = log.read_text(errors="replace")[-RESULT_TAIL:]
    except OSError:
        return {}
    for line in reversed(tail.splitlines()):
        # `claude` writes compact JSON; a log that has been through anything
        # that pretty-prints it is still a log, and refusing to read it would
        # mean the run was classified as having ended cleanly however it ended
        if '"type":"result"' not in line and '"type": "result"' not in line:
            continue
        try:
            return json.loads(line[line.index("{"):])
        except (ValueError, json.JSONDecodeError):
            return {}
    return {}


_STAGE_OF_LOG = {"proposed": "proposed", "recipe": "recipe",
                 "reconciled": "reconciled", "solvable": "solvable"}


def _stage_of_log(p: Path) -> str:
    stem = p.stem
    if stem.startswith("repair-"):
        return "repair"
    return _STAGE_OF_LOG.get(stem, stem)


def deck_tokens(deck_dir: Path) -> dict:
    """Every agent session this deck ever spent, by stage.

    Three places hold logs and all three count: the live one, `retries/`
    (attempts killed by a 429, which cost tokens and produced nothing) and
    `attempts/` (superseded runs, kept by `archive_attempt`).  `attempts/`
    holds *copies*, so the same session appears twice — dedup is by
    `session_id`, which is the only identifier that survives a copy.

    Counting only the live log is how a deck that took ten solvability
    attempts gets reported as having cost one.
    """
    seen: set = set()
    by_stage: dict[str, dict] = {}
    for log in sorted(deck_dir.rglob("*.jsonl")):
        if log.parent.name == "trial":
            continue
        d = last_result(log)
        if not d:
            continue
        sid = d.get("session_id") or f"{log}"
        if sid in seen:
            continue
        seen.add(sid)
        u = d.get("usage") or {}
        stage = _stage_of_log(log)
        acc = by_stage.setdefault(stage, {
            "sessions": 0, "output": 0, "input": 0, "cache_read": 0,
            "cache_creation": 0, "cost_usd": 0.0, "turns": 0,
            "api_ms": 0, "wall_ms": 0, "errors": 0})
        acc["sessions"] += 1
        acc["output"] += int(u.get("output_tokens") or 0)
        acc["input"] += int(u.get("input_tokens") or 0)
        acc["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
        acc["cache_creation"] += int(u.get("cache_creation_input_tokens") or 0)
        acc["cost_usd"] += float(d.get("total_cost_usd") or 0.0)
        acc["turns"] += int(d.get("num_turns") or 0)
        acc["api_ms"] += int(d.get("duration_api_ms") or 0)
        acc["wall_ms"] += int(d.get("duration_ms") or 0)
        if d.get("is_error") or d.get("terminal_reason") not in (
                None, "completed"):
            acc["errors"] += 1
    return by_stage


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def load_timeline(path: Path) -> tuple[dict, list[dict]]:
    header, samples = {}, []
    with open(path, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue                # a half-written last line, mid-crash
            if d.get("kind") == "header":
                header = d
            elif d.get("kind") == "sample":
                samples.append(d)
    return header, samples


def _spans(samples: list[dict]) -> list[float]:
    """Seconds each sample stands for — the gap to the next one.

    The last sample stands for the median gap: it is the only honest guess,
    and pretending it stands for zero would under-count whatever was running
    when the run — or the observer — stopped.
    """
    if not samples:
        return []
    gaps = [samples[i + 1]["t"] - samples[i]["t"] for i in range(len(samples) - 1)]
    tail = statistics.median(gaps) if gaps else 0.0
    return gaps + [tail]


def analyse(header: dict, samples: list[dict], work: Path | None = None) -> dict:
    """Everything the timeline can answer, as data.  `render` prints it."""
    if not samples:
        return {"error": "timeline has no samples"}
    spans = _spans(samples)
    t0, t1 = samples[0]["t"], samples[-1]["t"] + spans[-1]
    work = Path(work or header.get("work") or samples[0].get("work") or "work")

    limits = samples[-1].get("limits", {})
    pools: dict[str, dict] = {}
    for pool in ("agent", "cpu"):
        run = [s["pools"][pool]["running"] for s in samples if pool in s["pools"]]
        q = [s["pools"][pool]["queued"] for s in samples if pool in s["pools"]]
        pr = [s["pools"][pool].get("procs", 0) for s in samples
              if pool in s["pools"]]
        lim = limits.get(pool)
        at_lim = sum(sp for s, sp in zip(samples, spans)
                     if lim and s["pools"][pool]["running"] >= lim)
        busy = sum(s["pools"][pool]["running"] * sp
                   for s, sp in zip(samples, spans))
        pools[pool] = {
            "limit": lim,
            "peak": max(run) if run else 0,
            "peak_procs": max(pr) if pr else 0,
            "mean": round(busy / (t1 - t0), 2) if t1 > t0 else 0,
            "utilisation": round(busy / ((t1 - t0) * lim), 3)
                           if lim and t1 > t0 else None,
            "seconds_at_limit": round(at_lim, 1),
            "frac_at_limit": round(at_lim / (t1 - t0), 3) if t1 > t0 else 0,
            "peak_queue": max(q) if q else 0,
            "queue_deck_seconds": round(
                sum(s["pools"][pool]["queued"] * sp
                    for s, sp in zip(samples, spans)), 1),
        }

    # -- per deck, second by second -------------------------------------- #
    decks: dict[str, dict] = {}
    for s, span in zip(samples, spans):
        for d in s["decks"]:
            rec = decks.setdefault(d["id"], {
                "id": d["id"], "working_s": 0.0, "waiting_s": 0.0,
                "idle_s": 0.0, "by_stage": {}, "wait_by_stage": {},
                "first_t": s["t"], "last_t": s["t"], "finished_t": None,
                "final": None, "parked": None, "stalled_s": 0.0})
            rec["last_t"] = s["t"] + span
            if d["running"]:
                rec["working_s"] += span
                st = d["stage"] or "?"
                rec["by_stage"][st] = round(rec["by_stage"].get(st, 0) + span, 1)
            elif d["queued"]:
                rec["waiting_s"] += span
                st = d["next"] or "?"
                rec["wait_by_stage"][st] = round(
                    rec["wait_by_stage"].get(st, 0) + span, 1)
            else:
                rec["idle_s"] += span
            if d["finished"] and rec["finished_t"] is None:
                rec["finished_t"] = s["t"]
            rec["final"] = d["done_through"]
            rec["parked"] = d["parked"]
        for h in s.get("hung", []):
            if h["id"] in decks:
                decks[h["id"]]["stalled_s"] += span

    for rec in decks.values():
        end = rec["finished_t"] if rec["finished_t"] is not None else rec["last_t"]
        rec["elapsed_s"] = round(end - t0, 1)
        for k in ("working_s", "waiting_s", "idle_s", "stalled_s"):
            rec[k] = round(rec[k], 1)

    ordered = sorted(decks.values(), key=lambda r: -r["elapsed_s"])
    critical = ordered[0] if ordered else None

    # -- per stage, across decks ------------------------------------------ #
    by_stage: dict[str, dict] = {}
    for rec in decks.values():
        for st, secs in rec["by_stage"].items():
            e = by_stage.setdefault(st, {"stage": st, "decks": 0, "total_s": 0.0,
                                         "each": [], "wait_s": 0.0})
            e["decks"] += 1
            e["total_s"] += secs
            e["each"].append(secs)
        for st, secs in rec["wait_by_stage"].items():
            e = by_stage.setdefault(st, {"stage": st, "decks": 0, "total_s": 0.0,
                                         "each": [], "wait_s": 0.0})
            e["wait_s"] += secs
    for e in by_stage.values():
        e["total_s"] = round(e["total_s"], 1)
        e["wait_s"] = round(e["wait_s"], 1)
        e["median_s"] = round(statistics.median(e["each"]), 1) if e["each"] else 0
        e["max_s"] = round(max(e["each"]), 1) if e["each"] else 0

    # -- memory ------------------------------------------------------------ #
    rss = [s["mem"]["rss_total"] for s in samples if s.get("mem")]
    biggest = None
    for s in samples:
        big = (s.get("mem") or {}).get("largest")
        if big and (biggest is None or big["rss"] > biggest["rss"]):
            biggest = dict(big, at=s["iso"])
    avail = [s["mem"]["avail"] for s in samples
             if (s.get("mem") or {}).get("avail")]
    mem = {"peak_rss": max(rss) if rss else 0,
           "mean_rss": int(statistics.mean(rss)) if rss else 0,
           "largest_proc": biggest,
           "min_avail": min(avail) if avail else None,
           "at_min_avail": next((s["iso"] for s in samples
                                 if (s.get("mem") or {}).get("avail")
                                 == (min(avail) if avail else None)), None)}

    displays = {"peak_held": max((s["displays"]["held"] for s in samples
                                  if s.get("displays")), default=0),
                "span": samples[-1].get("displays", {}).get("span"),
                "ever_saturated": any(s.get("displays", {}).get("saturated")
                                      for s in samples)}

    hung: dict[str, dict] = {}
    for s in samples:
        for h in s.get("hung", []):
            hung.setdefault(h["id"], dict(h, first_seen=s["iso"], samples=0))
            hung[h["id"]]["samples"] += 1
    warns: dict[str, int] = {}
    for s in samples:
        for w in s.get("warn", []):
            warns[w] = warns.get(w, 0) + 1
    unattributed: dict[str, int] = {}
    for s in samples:
        for k, v in (s.get("procs", {}).get("unattributed") or {}).items():
            unattributed[k] = max(unattributed.get(k, 0), v)

    out = {
        "run": {"started": samples[0]["iso"], "ended": samples[-1]["iso"],
                "wall_s": round(t1 - t0, 1), "samples": len(samples),
                "interval_s": header.get("interval_s"),
                "decks": len(decks), "work": str(work),
                "limit_source": limits.get("source")},
        "pools": pools,
        "decks": ordered,
        "stages": sorted(by_stage.values(), key=lambda e: -e["total_s"]),
        "critical_path": critical,
        "mem": mem,
        "displays": displays,
        "hung": list(hung.values()),
        "warnings": warns,
        "unattributed": unattributed,
    }
    out.update(from_work_dir(work))
    return out


def from_work_dir(work: Path) -> dict:
    """The half of the report that comes from the run's own files.

    Kept separate from the timeline analysis so that each can be wrong on its
    own.  Stage durations are the interesting case: `state.json` records
    `duration_ms` if the run that wrote it recorded one, and derives it from
    consecutive `at` stamps if not — and the derived number includes the time
    a deck spent queued between the two stages, so it is an upper bound.  The
    timeline's `working_s` has no such problem and disagreeing with this is
    informative rather than embarrassing.
    """
    work = Path(work)
    decks = sorted(p for p in work.glob("deck*") if p.is_dir())
    if not decks:
        return {"work_missing": str(work)}

    tokens_by_stage: dict[str, dict] = {}
    tokens_by_deck: dict[str, dict] = {}
    state_stage: dict[str, dict] = {}
    yields = {"in": len(decks), "packaged": 0, "stopped": {}}
    for d in decks:
        per = deck_tokens(d)
        for stage, acc in per.items():
            tot = tokens_by_stage.setdefault(stage, {
                "sessions": 0, "output": 0, "input": 0, "cache_read": 0,
                "cache_creation": 0, "cost_usd": 0.0, "turns": 0,
                "api_ms": 0, "wall_ms": 0, "errors": 0, "decks": 0})
            tot["decks"] += 1
            for k, v in acc.items():
                tot[k] = tot.get(k, 0) + v
        roll = {"output": 0, "cache_read": 0, "cost_usd": 0.0, "sessions": 0,
                "wall_ms": 0}
        for acc in per.values():
            for k in roll:
                roll[k] += acc.get(k, 0)
        roll["cost_usd"] = round(roll["cost_usd"], 4)
        tokens_by_deck[d.name] = roll

        st = read_state(d)
        prev_t = None
        for stage in STAGES:
            rec = st.get(stage)
            if not rec:
                continue
            ms = rec.get("duration_ms")
            derived = False
            at = _parse_at(rec.get("at"))
            if ms is None and at is not None and prev_t is not None:
                ms = int((at - prev_t) * 1000)
                derived = True
            if at is not None:
                prev_t = at
            if ms is None or ms < 0:
                continue
            e = state_stage.setdefault(stage, {"stage": stage, "n": 0,
                                               "total_ms": 0, "derived": 0,
                                               "each": []})
            e["n"] += 1
            e["total_ms"] += ms
            e["each"].append(ms)
            e["derived"] += 1 if derived else 0

        last, status = "", None
        for stage in STAGES:
            s = st.get(stage, {}).get("status")
            if _promoted(s, stage):
                last = stage
            else:
                status = s
                break
        if last == STAGES[-1]:
            yields["packaged"] += 1
        else:
            key = f"{last or '—'} -> {status or 'not started'}"
            yields["stopped"][key] = yields["stopped"].get(key, 0) + 1

    for e in state_stage.values():
        e["median_ms"] = int(statistics.median(e["each"])) if e["each"] else 0
        e["max_ms"] = max(e["each"]) if e["each"] else 0

    return {"tokens_by_stage": tokens_by_stage, "tokens_by_deck": tokens_by_deck,
            "state_stages": sorted(state_stage.values(),
                                   key=lambda e: -e["total_ms"]),
            "yield": yields}


def _parse_at(s) -> float | None:
    if not s:
        return None
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


def _hms(sec) -> str:
    if sec is None:
        return "—"
    sec = int(sec)
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m" if sec >= 3600 \
        else f"{sec // 60}m{sec % 60:02d}s"


def _k(n) -> str:
    n = n or 0
    for unit, div in (("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return str(int(n))


def render(a: dict) -> str:
    """The report, as the paragraphs somebody will be asked to explain."""
    if "error" in a:
        return a["error"]
    L: list[str] = []
    r = a["run"]
    L.append(f"run          {r['started']} -> {r['ended']}   "
             f"wall {_hms(r['wall_s'])}   {r['samples']} samples "
             f"@ {r['interval_s']}s   {r['decks']} decks")

    L.append("")
    L.append("CONCURRENCY  (limit is what was asked for; peak is what happened)")
    L.append(f"  {'pool':<7}{'limit':>6}{'peak':>6}{'mean':>7}{'util':>7}"
             f"{'at limit':>10}{'peak q':>8}{'queued deck-s':>15}")
    for pool, p in a["pools"].items():
        util = "—" if p["utilisation"] is None else f"{p['utilisation']:.0%}"
        atlim = "{:.0%}".format(p["frac_at_limit"])
        L.append(f"  {pool:<7}{str(p['limit']):>6}{p['peak']:>6}"
                 f"{p['mean']:>7.2f}{util:>7}"
                 f"{atlim:>10}{p['peak_queue']:>8}"
                 f"{p['queue_deck_seconds']:>15.0f}")
    for pool, p in a["pools"].items():
        if p["limit"] and p["peak"] < p["limit"]:
            L.append(f"  → the {pool} pool never filled ({p['peak']} of "
                     f"{p['limit']}); raising it would have changed nothing.")
        elif p["limit"] and p["frac_at_limit"] > 0.5:
            L.append(f"  → the {pool} pool was full {p['frac_at_limit']:.0%} of "
                     f"the run with up to {p['peak_queue']} deck(s) waiting: "
                     f"this limit was the constraint.")

    L.append("")
    L.append("DECKS        (working = holding a slot; waiting = eligible, no slot)")
    L.append(f"  {'deck':<11}{'elapsed':>9}{'working':>9}{'waiting':>9}"
             f"{'idle':>8}  reached")
    for d in a["decks"]:
        note = ""
        if d["parked"]:
            note = f"  PARKED {d['parked']['stage']}/{d['parked']['status']}"
        elif d["stalled_s"]:
            note = f"  stalled {_hms(d['stalled_s'])}"
        L.append(f"  {d['id']:<11}{_hms(d['elapsed_s']):>9}"
                 f"{_hms(d['working_s']):>9}{_hms(d['waiting_s']):>9}"
                 f"{_hms(d['idle_s']):>8}  {d['final'] or '—'}{note}")
    cp = a["critical_path"]
    if cp:
        worst = max(cp["by_stage"].items(), key=lambda kv: kv[1],
                    default=(None, 0))
        L.append(f"  → critical path is {cp['id']} at {_hms(cp['elapsed_s'])}. "
                 f"Wall clock at N-wide is this deck, not the mean "
                 f"({_hms(statistics.mean([d['elapsed_s'] for d in a['decks']]))}).")
        if worst[0]:
            L.append(f"    Its longest stage was {worst[0]} "
                     f"({_hms(worst[1])}); it waited {_hms(cp['waiting_s'])} "
                     f"for a slot in total.")

    L.append("")
    L.append("STAGES       (observed, from the timeline)")
    L.append(f"  {'stage':<14}{'decks':>6}{'total':>9}{'median':>9}{'max':>9}"
             f"{'queued for':>12}")
    for e in a["stages"]:
        L.append(f"  {e['stage']:<14}{e['decks']:>6}{_hms(e['total_s']):>9}"
                 f"{_hms(e['median_s']):>9}{_hms(e['max_s']):>9}"
                 f"{_hms(e['wait_s']):>12}")

    if a.get("state_stages"):
        L.append("")
        L.append("             (from state.json)")
        for e in a["state_stages"]:
            tag = f"  [{e['derived']}/{e['n']} derived]" if e["derived"] else ""
            L.append(f"  {e['stage']:<14}{e['n']:>6}"
                     f"{_hms(e['total_ms'] / 1000):>9}"
                     f"{_hms(e['median_ms'] / 1000):>9}"
                     f"{_hms(e['max_ms'] / 1000):>9}{tag}")
        if any(e["derived"] for e in a["state_stages"]):
            L.append("  ! `derived` means the stage recorded no duration_ms "
                     "and the number is the gap between two `at` stamps.")
            L.append("    That gap is everything between the two marks — the "
                     "queue, a repair loop, and the operator going to bed —")
            L.append("    so read it as an upper bound only. The observed "
                     "table above is the one to quote.")

    if a.get("tokens_by_stage"):
        L.append("")
        L.append("TOKENS       (every session, including retries and superseded "
                 "attempts)")
        L.append(f"  {'stage':<12}{'sess':>6}{'output':>9}{'cache rd':>10}"
                 f"{'cache wr':>10}{'turns':>7}{'$':>9}{'err':>5}")
        tot = {}
        for stage, t in sorted(a["tokens_by_stage"].items(),
                               key=lambda kv: -kv[1]["output"]):
            L.append(f"  {stage:<12}{t['sessions']:>6}{_k(t['output']):>9}"
                     f"{_k(t['cache_read']):>10}{_k(t['cache_creation']):>10}"
                     f"{t['turns']:>7}{t['cost_usd']:>9.2f}{t['errors']:>5}")
            for k, v in t.items():
                if isinstance(v, (int, float)):
                    tot[k] = tot.get(k, 0) + v
        L.append(f"  {'TOTAL':<12}{tot.get('sessions', 0):>6}"
                 f"{_k(tot.get('output')):>9}{_k(tot.get('cache_read')):>10}"
                 f"{_k(tot.get('cache_creation')):>10}"
                 f"{tot.get('turns', 0):>7}{tot.get('cost_usd', 0):>9.2f}"
                 f"{tot.get('errors', 0):>5}")
        n = max(1, len(a.get("tokens_by_deck") or {}))
        L.append(f"  → {_k(tot.get('output', 0) / n)} output and "
                 f"{_k(tot.get('cache_read', 0) / n)} cache-read per deck, "
                 f"${tot.get('cost_usd', 0) / n:.2f}.")

    m = a["mem"]
    L.append("")
    L.append(f"MEMORY       peak tree RSS {_gb(m['peak_rss'])}, mean "
             f"{_gb(m['mean_rss'])}, least free {_gb(m['min_avail'])}"
             + (f" at {m['at_min_avail']}" if m["at_min_avail"] else ""))
    if m["largest_proc"]:
        b = m["largest_proc"]
        L.append(f"             largest single process "
                 f"{_gb(b['rss'])} ({b['name']}, pid {b['pid']}, at {b['at']})")

    d = a["displays"]
    L.append(f"DISPLAYS     peak {d['peak_held']} of {d['span']} held"
             + ("  — SATURATED at least once" if d["ever_saturated"] else ""))

    y = a.get("yield")
    if y:
        L.append("")
        L.append(f"YIELD        {y['in']} deck(s) in, {y['packaged']} packaged")
        for where, n in sorted(y["stopped"].items(), key=lambda kv: -kv[1]):
            L.append(f"             {n} stopped at {where}")

    if a["hung"]:
        L.append("")
        L.append("HUNG / STALLED")
        for h in a["hung"]:
            L.append(f"  {h['id']:<11}{h['stage'] or '—':<14}{h['why']} "
                     f"(first seen {h['first_seen']}, {h['samples']} sample(s))")

    if a["warnings"]:
        L.append("")
        L.append("WARNINGS     (count = samples in which it was true)")
        for w, n in sorted(a["warnings"].items(), key=lambda kv: -kv[1]):
            L.append(f"  {n:>4}x  {w}")

    if a.get("unattributed"):
        L.append("")
        L.append("NOT COUNTED  processes on this box that were not part of the "
                 "run (peak):")
        for k, v in sorted(a["unattributed"].items()):
            L.append(f"             {v} x {k}")

    L.append("")
    L.append("WHAT SAMPLING CANNOT SEE")
    for b in BLIND_SPOTS:
        L.append(f"  - {b}")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m pptxgym.observe",
        description="sample a running pipeline from outside it, and report on "
                    "the run afterwards")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", help="sample a run into a JSONL timeline")
    w.add_argument("--work", default="work")
    w.add_argument("--interval", type=float, default=10.0)
    w.add_argument("--out", default=None,
                   help="timeline path (default work/observe-<epoch>.jsonl)")
    w.add_argument("--pid", type=int, action="append", default=[],
                   help="the run's pid; repeatable. Without it the run is "
                        "found by argv, which is the thing to override when "
                        "the box has more than one on it.")
    w.add_argument("--agent-limit", type=int, default=None)
    w.add_argument("--cpu-limit", type=int, default=None)
    w.add_argument("--until", default=None,
                   help="the stage the run was told to stop at, so a deck "
                        "that reached it counts as finished")
    w.add_argument("--stall-after", type=float, default=STALL_AFTER)
    w.add_argument("--samples", type=int, default=None,
                   help="stop after this many samples (default: until ^C)")
    w.add_argument("--once", action="store_true", help="take one sample")
    w.add_argument("--quiet", action="store_true")

    r = sub.add_parser("report", help="turn a timeline into the answers")
    r.add_argument("timeline")
    r.add_argument("--work", default=None,
                   help="override the work dir recorded in the timeline")
    r.add_argument("--json", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "watch":
        out = Path(args.out or Path(args.work) / f"observe-{int(time.time())}.jsonl")
        limits = {}
        if args.agent_limit:
            limits["agent"] = args.agent_limit
        if args.cpu_limit:
            limits["cpu"] = args.cpu_limit
        n = 1 if args.once else args.samples
        try:
            watch(Path(args.work), out, interval=args.interval,
                  roots=args.pid, limits=limits, until=args.until,
                  max_samples=n, quiet=args.quiet,
                  stall_after=args.stall_after)
        except KeyboardInterrupt:
            print(f"\nstopped. timeline: {out}")
            print(f"  python -m pptxgym.observe report {out}")
            return 0
        print(f"timeline: {out}")
        return 0

    header, samples = load_timeline(Path(args.timeline))
    a = analyse(header, samples, work=args.work)
    print(json.dumps(a, ensure_ascii=False, indent=1, default=str)
          if args.json else render(a))
    return 0


if __name__ == "__main__":                                       # pragma: no cover
    raise SystemExit(main())
