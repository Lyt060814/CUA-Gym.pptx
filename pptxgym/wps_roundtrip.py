"""What WPS changes just by opening and saving a deck.

`roundtrip.py` measures the same thing through LibreOffice, because that is
the renderer we can drive headlessly.  It is a proxy, and the numbers it
produces set every position tolerance the reward will use — so it is worth
knowing how far off the proxy is.  WPS is the application the tasks are
actually solved in.

WPS has no working headless converter on Linux: `wpp --headless` exits
silently and `--convert-to` is not implemented in the Linux build.  What does
work is the same thing a solver does — open the file in the GUI, press
Ctrl+S — driven on a virtual display so it never touches anyone's desktop.

Needs `Xvfb` and `xdotool`.  On this machine only xdotool was installed; a
first attempt without a virtual display sat on the real one for four minutes
and died on a first-run dialog.

A round trip used to cost 37 s of which 3.9 s was CPU: 20 s of `LOAD_WAIT`
and 15 s of scripted sleeps, each one a guess at how long some step takes on
the worst deck.  Every one of them has been replaced by a wait for the thing
it stood in for — the display existing, the document's name appearing in the
window title, the process going quiet, the title's modified marker, the saved
package being complete on disk — so a deck now takes as long as it takes.
Measured serially on the same ten `work/` decks, twice, with every verdict
and every media part identical to the sleeping version's:

    per deck   min    median   max
    before     36.4   36.5     42.8
    after      10.3   11.5     12.5

The spread is the point as much as the middle: what a deck costs is now what
it needs.  A deck whose fonts take fifteen seconds to resolve gets fifteen
seconds, where the old constant would have clicked into it at twenty come
what may.

The sleeps that remain are the ones with nothing to observe, and they say so
where they are defined.  `PPTXGYM_WPS_TRACE=1` prints where a deck's seconds
went; on a median deck it is 3.9 s waiting for the document to appear and go
quiet, 2.9 s for the first-run dialog and the quiet after it, 4.0 s for the
keystrokes and the same quiet either side of them, and 0.5 s for the save.

That is 3x on one deck, and it holds up in a batch: four workers put the same
ten decks through in 38 s, against the 10.5 s each — 105 s — that the table
this docstring used to carry.  The pipeline takes it one deck at a time on the
critical path, which is where the 25 s comes off.

A display costs about 500 MB while it runs — 86 MB of Xvfb and 400 MB of WPS,
with a peak near 660 MB while the document loads.  So the ceiling is (free
memory / 0.66 GB): six on this machine with 3.5 GB free, around twenty on an
idle one, a few hundred on a 256 GB server long before 32 vCPUs would notice.
Past it, the kernel kills the WPS holding the biggest deck; that deck reports
a failure rather than a wrong answer, but it is a deck you have to run again,
so leave headroom.

WPS also segfaults during startup on its own, which is worth knowing before
reading anything into a single failed deck: ten repeats of a deck that had
just crashed all passed.  It is also the one number this rewrite may have
made worse, so it was measured rather than assumed.  A first version of this
sequence, which clicked into the deck as soon as the window had gone quiet
once, crashed 4 times in 66 launches against 0 in 42 of the old sleeping
sequence; waiting for that quiet again after the click and after the
keystrokes — the two sleeps that looked most redundant — brought it to 2 in
80, with none in the last 40.  `roundtrip_wps` retries a failed attempt once,
which is what makes the remainder somebody else's problem rather than the
caller's.

A display outlives the run that took it, and that is the other thing worth
knowing here.  The claim is a kernel `flock`, which dies with its holder — but
the *Xvfb* does not, because `atexit` does not run for a signalled process, so
a killed run leaves a server sitting on `/tmp/.X99-lock` and the pool refuses
that number to everybody afterwards.  It refuses for a good reason: a display
whose X files exist may be somebody's real desktop.  Two of the sixty-four
went that way in one day, which is why eight round trips serialised onto :101
and the pool read `1 of 64` all run — silent queueing, no error anywhere.

So every claim now writes a receipt beside its lock: which process claimed it,
which Xvfb it started, with what argv, which WPS processes, which scratch
directories.  `reclaim` walks the pool holding each lock in turn and takes
back only what a receipt proves — the same pid, the same start jiffy, the same
boot, the same argv, our uid.  A server that answers and cannot be identified
that precisely is left alone and reported, `:0` included.  A leaked display
costs one run; a killed desktop costs somebody their afternoon.

    python3 -m pptxgym.wps_roundtrip work/deck0001/source.pptx
    python3 -m pptxgym.wps_roundtrip --workers 6 work/deck00*/source.pptx
    python3 -m pptxgym.wps_roundtrip --reclaim            # what is stranded
    python3 -m pptxgym.wps_roundtrip --reclaim --apply    # take it back
"""

from __future__ import annotations

import atexit
import contextlib
import errno
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

WPP = "/opt/kingsoft/wps-office/office6/wpp"
DISPLAY = ":99"
SCREEN = "1920x1200"
# fixed screen, so these are stable: the notes pane along the bottom and the
# save button in the quick-access row left of the ribbon tabs
NOTES_XY = (500, 1143)
SAVE_XY = (139, 50)
DIRTY_MARK = "ZZ"

POLL = 0.2              # how often anything below asks whether it can stop yet

# "the document has finished loading" is two conditions, neither of which is a
# clock.  The window title turns from `Presentation` into `<file> -
# Presentation` when the document is open, and the process stops burning CPU
# when it has finished laying it out.  The title alone is not enough: it
# appears about a second before the work is done, and clicking into a pane
# that is not there yet types into whatever *is*.
IDLE_WINDOW = 1.5       # wall clock over which the process must stay quiet
IDLE_CPU = 0.05         # CPU seconds it may spend in that window and still
                        # count as quiet — WPS drips ~0.003 s/s while idle
IDLE_RSS = 4 << 20      # ...and how much its memory may move in it

# The first-run font check ("Some formula symbols might not be displayed
# correctly due to missing fonts Symbol") is the one thing here with no
# observable "it is not coming".  It arrives about half a second after the
# document title on this box, so by the time the load has gone quiet it is
# already up and this grace is never spent; it is insurance against a slower
# box, not a schedule.  A dialog that turns up even later than this is caught
# instead by the dirty-marker check below, which notices that the keystrokes
# went somewhere else.
DIALOG_GRACE = 3.0      # watch this long for a dialog that may never come
DIALOG_QUIET = 1.0      # ...and this long with none, after closing one
DIALOG_GONE = 5.0       # how long a closed dialog gets to actually disappear

#: The windows that must be gone before anything is typed.
#:
#: `WPS Office` was not on this list and had to be added: an HF Jobs container
#: showed one, 635x243, still up after `_settle_dialogs` had run, because the
#: pattern only ever named `System Check|Tip|Prompt`.  It is a promotional
#: window rather than a dialog, which is why nobody put it here, and it holds
#: focus like one — `_xdo(env, "type", ...)` sends to the focused window, so
#: every keystroke went to it and the document never went modified.  That
#: looked from the outside like keys not arriving, and three explanations were
#: tried against that misreading.
#:
#: It cannot match the document window, whose title always ends
#: `- Presentation`.
DIALOG_RE = "System Check|WPS Office|Tip|Prompt"

DIRTY_WAIT = 15.0       # for the title's modified marker after typing.  It
                        # usually lands in under a second and has taken six on
                        # a loaded box; waiting longer costs nothing when it
                        # arrives, because the wait ends when it does
DIRTY_WAIT_LOAD_SHARE = 0.5
                        # ...but 15 s is calibrated to *this* machine, and it
                        # is the last wall clock left in this module.  On an
                        # HF Jobs container the same deck loads an order of
                        # magnitude slower, the marker missed the window, and
                        # it was reported as "the notes edit did not reach the
                        # document" -- which reads like a focus or keyboard
                        # fault and is really a stopwatch.  Three container
                        # runs were spent on that misreading.
                        #
                        # So the floor stays, and a machine that has just
                        # proved itself slow gets proportionally longer.  The
                        # load time is the honest measure of how slow this box
                        # is being *right now*: same application, comparable
                        # work, moments earlier.  It costs nothing on a fast
                        # box, where the floor dominates, and nothing on a
                        # slow one either, since the wait still ends the
                        # moment the marker lands.
WRITE_SETTLE = 30.0     # for the saved package to be complete on disk
UNDIRTIED_GRACE = 60.0  # save deadline when the document never looked modified

#: `source.pptx - Presentation`, and `source.pptx * - Presentation` once it has
#: unsaved changes.  Lazy on the name so the ` * ` is not swallowed by it.
TITLE_RE = re.compile(r"^(?P<name>.*?)\s*(?P<dirty>\*)?\s*-\s+Presentation$")

DISPLAY_BASE = 99       # :99 upwards; :0 is somebody's real desktop
DISPLAY_SPAN = 64
LOCK_DIR = Path(os.environ.get(
    "PPTXGYM_DISPLAY_LOCKS", Path(tempfile.gettempdir()) / "pptxgym-displays"))
X_SOCKET_DIR = Path("/tmp/.X11-unix")

WORK_PREFIX = "wpsrt-"  # every scratch directory this module makes
WORK_ROOT = Path(tempfile.gettempdir())

#: A leaked work directory is only swept once it is older than any run could
#: be.  `timeout` is 240 s at its longest and a deck takes twelve, so a
#: quarter of an hour is a wide margin bought for nothing: the directories
#: this exists to remove are days old.
WORK_MIN_AGE = 900.0


class WpsUnavailable(RuntimeError):
    pass


class NoFreeDisplay(RuntimeError):
    """Every display in the pool is in use by somebody who is still alive."""


def _have(*names) -> list[str]:
    return [n for n in names if not shutil.which(n)]


def preflight() -> list[str]:
    """What is missing, in the words someone would need to fix it."""
    out = []
    missing = _have("Xvfb", "xdotool")
    if missing:
        out.append(f"not installed: {', '.join(missing)} "
                   f"(sudo apt-get install -y xvfb xdotool)")
    if not Path(WPP).exists() and not shutil.which("wpp"):
        out.append("WPS Presentation not found")
    return out


# --------------------------------------------------------------------------- #
# proving that a process is ours
# --------------------------------------------------------------------------- #
#
# Everything below answers one question about one process: is this the
# process we wrote down, or something that merely looks like it.
#
# A pid cannot answer it — pids are reused.  A pattern cannot answer it
# either: "an Xvfb on :99 with our geometry" describes somebody else's Xvfb on
# :99 with our geometry exactly as well, and killing on that description is a
# pattern-kill by another name.  A pid *and the moment it started* does answer
# it.  The kernel stamps every process with the jiffy it began at, publishes
# it in /proc, and never issues the same (pid, start) pair twice in one boot.
# Carry the boot id alongside and the pair is unique for as long as the
# machine has been up, which outlasts any leak this module can produce.

BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def _boot_id() -> str:
    """This boot, as the kernel names it.

    A pid from before a reboot means nothing, and /tmp survives a reboot on
    plenty of machines, so every claim written down carries the boot it was
    written in.  A receipt from an earlier boot is a note about processes that
    no longer exist, and is never evidence for killing anything.
    """
    with contextlib.suppress(OSError):
        return BOOT_ID_PATH.read_text().strip()
    return ""                                    # pragma: no cover


def _proc_start(pid: int) -> int | None:
    """The jiffy this pid started at, or None if it is gone.

    Field 22 of `/proc/<pid>/stat`, read after the comm field so a process
    called `) (` cannot shift the columns.
    """
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return int(fh.read().rsplit(") ", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _proc_argv(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return [a.decode("utf-8", "replace")
                    for a in fh.read().split(b"\0") if a]
    except OSError:
        return []


def _proc_uid(pid: int) -> int | None:
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None


def _proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:                 # somebody else's process, or already gone
        return None


def _proc_env(pid: int) -> dict:
    """The environment a process was started with, when we may read it.

    Only for processes of our own uid — which is the only kind this module is
    ever willing to act on, so the restriction costs nothing.
    """
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}
    out = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.decode("utf-8", "replace").partition("=")
        if sep:
            out.setdefault(key, value)
    return out


def _ident(pid: int) -> dict | None:
    """All a later run can check about a live process.  None if it is gone."""
    start = _proc_start(pid)
    if start is None:
        return None
    return {"pid": pid, "start": start, "argv": _proc_argv(pid),
            "uid": _proc_uid(pid)}


def _still(rec: dict | None) -> bool:
    """Is the process this record describes still the one under that pid?

    A record with no start time answers no, and that is the important half.
    `_proc_start` returns None for a pid that is gone, so comparing the two
    directly would make a receipt whose start time was never captured — an
    Xvfb that died before we could read it — match every dead pid on the
    machine, and pids are reused.  It fails closed: unidentifiable means not
    ours, which costs a leaked display and never a stranger's process.
    """
    if not rec or rec.get("start") is None:
        return False
    return _proc_start(rec.get("pid", -1)) == rec["start"]


def _kill_ident(rec: dict, grace: float = 5.0) -> bool:
    """Signal one process, re-checking its identity before each shot.

    The gap between deciding to kill something and killing it is where a
    pattern-kill goes wrong: the process exits, the pid is reissued, and the
    signal lands on a stranger.  So the (pid, start) pair is re-read between
    the decision and SIGTERM, and again between SIGTERM and SIGKILL, and a
    pair that has stopped matching ends the attempt rather than escalating it.
    """
    pid = rec["pid"]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not _still(rec):
            return True                          # it went away by itself
        with contextlib.suppress(OSError, ProcessLookupError):
            os.kill(pid, sig)
        deadline = time.time() + grace
        while time.time() < deadline:
            if not _still(rec):
                return True
            time.sleep(0.05)
        grace = 2.0
    return not _still(rec)


# --------------------------------------------------------------------------- #
# receipts
# --------------------------------------------------------------------------- #
#
# `flock` already says whether a display's *claim* is live: the kernel drops
# it when the holder dies, so a lock nobody holds is a run that is over.  What
# `flock` cannot say is what that run left behind.  A killed run's Xvfb goes
# on holding `/tmp/.X99-lock`, the pool refuses a display whose X files exist
# — rightly, because such a display may be somebody's actual desktop — and the
# number is retired for every later run on the box.  Yesterday that happened
# to :99 and :100, and eight round trips serialised onto :101 without one
# error between them.  At sixty-four numbers and a hundred decks the same
# silence empties the pool.
#
# So the claim writes down, beside the lock, what it started: the owning
# process's identity, the Xvfb's identity and exact argv, the WPS processes,
# and the scratch directories.  It is written by the holder of the lock and by
# nobody else, so it needs no locking of its own; it is read by a later run
# that holds that same lock, so no live claimant can be racing it.
#
# The receipt is what turns "an X server is sitting on :99" into "the X server
# on :99 is pid 12346, which is the process this run started at 21:40:12 and
# never stopped".  One of those is a pattern; the other is a fact about a
# named process, and only the second is worth acting on.

RECEIPT_VERSION = 1


def _receipt_path(lock_dir, n: int) -> Path:
    return Path(lock_dir) / f"display{n}.json"


def _read_receipt(lock_dir, n: int) -> dict | None:
    try:
        return json.loads(_receipt_path(lock_dir, n).read_text())
    except (OSError, ValueError):
        return None


def _write_receipt(lock_dir, n: int, rec: dict) -> None:
    """Replace display `n`'s receipt atomically.

    Atomically because a reader is a *later run deciding whether to kill
    something*, and half a receipt read as a whole one is the worst input that
    decision can have.  `os.replace` means a reader sees the old file or the
    new one.
    """
    d = Path(lock_dir)
    with contextlib.suppress(OSError):
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=f"display{n}.", suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(rec, fh)
        os.replace(tmp, _receipt_path(d, n))


def _new_receipt(n: int) -> dict:
    return {"version": RECEIPT_VERSION, "boot": _boot_id(),
            "host": socket.gethostname(), "display": n,
            "owner": _ident(os.getpid()), "claimed": time.time(),
            "server": None, "clients": [], "workdirs": []}


def _amend_receipt(lock_dir, n: int, **fields) -> dict:
    """Add to display `n`'s receipt, creating one if this run never claimed it.

    A run given an explicit `--display` never went through `claim`, and its
    Xvfb leaks in exactly the same way, so it gets a receipt too.
    """
    rec = _read_receipt(lock_dir, n)
    if not rec or rec.get("boot") != _boot_id():
        rec = _new_receipt(n)
    for key, value in fields.items():
        # `clients=<one client>` adds to the list, `clients=[]` replaces it:
        # a screen adds one WPS at a time and forgets them all at once
        if key in ("clients", "workdirs") and not isinstance(value, list):
            rec.setdefault(key, [])
            if value not in rec[key]:
                rec[key].append(value)
        else:
            rec[key] = value
    _write_receipt(lock_dir, n, rec)
    return rec


def _drop_receipt(lock_dir, n: int) -> None:
    """Forget display `n`.

    Only on a clean release, and only after the Xvfb it named is dead: a
    receipt is the sole evidence that would let a later run reclaim that
    display, so dropping one while its server is alive converts a reclaimable
    leak into an unattributable one.
    """
    with contextlib.suppress(OSError):
        _receipt_path(lock_dir, n).unlink()


def _x_lock_pid(path: Path) -> int | None:
    """The pid in `/tmp/.X<n>-lock`, which the X server writes there itself."""
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _is_our_workdir(path: Path, recorded=()) -> bool:
    """Is `path` a scratch directory this module made?

    A recorded one is ours by name.  Otherwise the test is the prefix, the
    place and the ownership together: `tempfile.mkdtemp(prefix="wpsrt-")` in
    this file is the only thing on the machine that makes `/tmp/wpsrt-*`, and
    it makes them mode 700 under our uid, so another user's process cannot be
    inside one in the first place.
    """
    path = Path(path)
    for root in recorded:
        with contextlib.suppress(ValueError):
            path.relative_to(Path(root))
            return True
    for parent in (path, *path.parents):
        if parent.parent == WORK_ROOT and parent.name.startswith(WORK_PREFIX):
            with contextlib.suppress(OSError):
                return parent.stat().st_uid == os.getuid()
    return False


class DisplayPool:
    """Hands out X display numbers, one owner at a time.

    Two WPS instances on one display steal each other's focus and click each
    other's dialogs, and they do it silently — the save lands in the wrong
    document or nowhere, and the report still reads `stable`.  So the display
    number is the thing that has to be exclusive, and it has to be exclusive
    against *processes*, not only threads: this pipeline is a CLI and two
    invocations overlap all the time.

    The claim is an `flock` on a file per display number.  `flock` conflicts
    between separate open file descriptions, which two threads of one process
    also have, so one mechanism covers both cases; and the kernel drops the
    lock when the holder dies, so a killed run leaves nothing to garbage
    collect.  A display whose X socket already exists is never handed out
    even if nobody holds its lock — that is somebody's real desktop, or an
    Xvfb started outside the pool.

    That last rule is right and it is also how the pool empties.  The lock
    dies with the run; the *Xvfb* does not, because `atexit` does not run for
    a signalled process, and the file it holds keeps saying "occupied" to
    every run that follows.  `reclaim` is the way back, and it takes only what
    a receipt proves this pool started — see `_reclaim_one`.
    """

    def __init__(self, base: int = DISPLAY_BASE, span: int = DISPLAY_SPAN,
                 lock_dir: Path | str = LOCK_DIR,
                 sock_dir: Path | str = X_SOCKET_DIR):
        self.base, self.span = base, span
        self.lock_dir = Path(lock_dir)
        self.sock_dir = Path(sock_dir)

    def numbers(self) -> list[int]:
        return list(range(self.base, self.base + self.span))

    def files(self, n: int) -> tuple[Path, Path]:
        """The two files an X server on `n` may leave behind.

        Which one appears depends on the box.  `/tmp/.X11-unix` here is not
        mode 1777, so Xvfb cannot bind the filesystem socket and listens on
        the abstract one instead: only `/tmp/.X99-lock` shows up.  Elsewhere
        both do.
        """
        return self.sock_dir / f"X{n}", self.sock_dir.parent / f".X{n}-lock"

    def occupied(self, n: int) -> bool:
        """Is an X server already sitting on this number?"""
        return any(p.exists() for p in self.files(n))

    def answers(self, n: int) -> str | None:
        """The address an X server on `n` is actually listening on, or None.

        The files above outlive the server that made them; a connection does
        not.  Connecting is what tells a live desktop from a dead one's
        leftovers, and it is the cheapest question in this module that nobody
        can answer wrongly: an address that accepts a connection has a process
        behind it, and one that refuses has none.  Both namespaces are tried
        because Xvfb uses whichever it can get.

        Nothing is sent.  This opens a connection and closes it, which is what
        every X client on the machine does several times a second.
        """
        for addr in (f"\0{self.sock_dir}/X{n}", f"{self.sock_dir}/X{n}"):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            try:
                sock.connect(addr)
                return addr.replace("\0", "@")
            except OSError:
                pass
            finally:
                sock.close()
        return None

    def _lock(self, n: int):
        """The lock file descriptor for `n`, or None if somebody holds it.

        No socket check: this is the liveness question on its own — *is any
        process still claiming this number* — which `reclaim` needs to ask
        about numbers it is precisely not willing to hand out yet.
        """
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_dir / f"display{n}.lock",
                     os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EACCES, errno.EAGAIN):
                return None
            raise
        return fd

    @staticmethod
    def _unlock(fd) -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def _try(self, n: int):
        """The lock file descriptor for `n`, or None if it is not free."""
        fd = self._lock(n)
        if fd is None:
            return None
        # the socket check goes *after* the lock, so that two claimants
        # racing for the same number cannot both read "free" and both start
        # an Xvfb on it
        if self.occupied(n):
            os.close(fd)
            return None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        _write_receipt(self.lock_dir, n, _new_receipt(n))
        return fd

    # ----------------------------------------------------------------- #
    # reclaiming what an interrupted run left behind
    # ----------------------------------------------------------------- #

    def survey(self, numbers=None) -> list[dict]:
        """What every number in the pool is doing, and how that is known.

        Reads nothing destructively and kills nothing.  `reclaim(apply=True)`
        is the same walk with the actions carried out, so what a dry run
        prints is what an applied run does.
        """
        return self.reclaim(apply=False, numbers=numbers)

    def reclaim(self, apply: bool = False, numbers=None) -> list[dict]:
        """Take back the displays an interrupted run left holding.

        One record per number, each carrying its own `evidence`: the sentences
        that justify the verdict, in the words somebody checking this by hand
        would use.  Nothing is ever reclaimed without a receipt naming the
        exact process being ended, so every record can say why that process is
        ours.  Where it cannot, the display is left alone and reported —
        a leaked display costs a run; a killed desktop costs a person's day.
        """
        out = []
        for n in (self.numbers() if numbers is None else numbers):
            fd = self._lock(n)
            if fd is None:
                out.append(self._describe_held(n))
                continue
            try:
                out.append(self._reclaim_one(n, apply))
            finally:
                self._unlock(fd)
        return out

    def _describe_held(self, n: int) -> dict:
        """A display whose claim a live process still holds: never touched.

        The kernel refused us the lock, which is the only proof needed that
        somebody is still using this number.  The receipt is read only to say
        *who*, and a receipt that disagrees with the kernel loses.
        """
        rec = _read_receipt(self.lock_dir, n) or {}
        owner = rec.get("owner") or {}
        who = ""
        if _still(owner):
            argv = " ".join(owner.get("argv") or [])[:80]
            age = time.time() - (rec.get("claimed") or time.time())
            who = (f" by pid {owner['pid']} ({argv or 'unknown'}), "
                   f"{age / 60:.0f} min ago")
        return {"display": n, "state": "held", "reclaimed": False,
                "evidence": [f"display{n}.lock is held{who} — a live run owns "
                             f"this number"], "actions": []}

    def _reclaim_one(self, n: int, apply: bool) -> dict:
        """Decide, with the claim in hand, what has been left on one display.

        Holding the lock is what makes this safe to do at all: no live
        claimant can be starting an Xvfb here while we look, so anything found
        belongs either to a run that is over or to somebody outside the pool.

        Five outcomes, and the difference between the middle two is the whole
        point of the file:

          free            nothing here.
          ours            the receipt names the process holding this number
                          exactly: same boot, same pid, same start jiffy, same
                          argv, our uid.  That is the process this pool
                          started and nobody ever stopped — reclaimable,
                          whether or not it got as far as taking the display.
          foreign         a server answers and nothing proves it is ours.
                          Left alone.  This is `:0` and anything like it.
          stale           no server answers, but its files are still here and
                          a receipt shows we wrote them.  Files only.
          unattributable  files with no listener and no receipt.  Left alone,
                          and reported, because a human can settle in ten
                          seconds what this cannot settle at all.
        """
        rec = _read_receipt(self.lock_dir, n)
        addr = self.answers(n)
        files = [p for p in self.files(n) if p.exists()]
        ev = [f"no process holds display{n}.lock — whatever is here outlived "
              f"the run that claimed it"]
        actions: list[str] = []

        # A run killed during `_Screen.__enter__` leaves an Xvfb that is alive
        # and has not taken the display yet.  Nothing is listening, no files
        # exist, and the number reads free — and the next run to take it finds
        # its own Xvfb refused seconds later by the one still starting up.  So
        # the identity question is asked of a recorded server whether or not
        # it ever answered; the proof is the same proof either way.
        asking = bool(addr) or bool((rec or {}).get("server"))
        ours, why = (self._server_is_ours(n, rec, addr) if asking
                     else (False, []))
        ev += why
        if ours:
            victims = [rec["server"]] + self._our_leftovers(n, rec)
            if apply:
                for v in victims:
                    ok = _kill_ident(v)
                    actions.append(f"{'killed' if ok else 'could not kill'} "
                                   f"pid {v['pid']} ({v.get('what', 'Xvfb')})")
                actions += self._sweep_files(n)
                _drop_receipt(self.lock_dir, n)
            else:
                actions += [f"would kill pid {v['pid']} "
                            f"({v.get('what', 'Xvfb')})" for v in victims]
                actions.append(
                    f"would remove {', '.join(str(p) for p in files)}"
                    if files else "no files to remove")
            return {"display": n, "state": "ours", "reclaimed": apply,
                    "evidence": ev, "actions": actions}
        if addr:
            return {"display": n, "state": "foreign", "reclaimed": False,
                    "evidence": ev,
                    "actions": [
                        f"leave it: an X server is listening on :{n} "
                        f"and nothing here proves it is ours"]}

        ev.append(f"nothing is listening on :{n}: both the abstract and the "
                  f"filesystem socket refuse a connection")
        # `_our_leftovers` walks /proc, and this walk happens over sixty-four
        # numbers at the moment the pool is under contention — so it is only
        # asked about a display that has something to explain.  A number with
        # no files and no receipt has never been used by anybody.
        strays = self._our_leftovers(n, rec) if (files or rec) else []
        if not files and not strays:
            return {"display": n, "state": "free", "reclaimed": False,
                    "evidence": ev, "actions": []}

        ours, why = self._files_are_ours(n, rec, files)
        ev += why
        if not ours:
            return {"display": n, "state": "unattributable",
                    "reclaimed": False, "evidence": ev,
                    "actions": [f"leave {', '.join(str(p) for p in files)}: "
                                f"no receipt of ours accounts for it"]}
        if apply:
            for v in strays:
                ok = _kill_ident(v)
                actions.append(f"{'killed' if ok else 'could not kill'} "
                               f"pid {v['pid']} ({v.get('what', '?')})")
            actions += self._sweep_files(n)
            _drop_receipt(self.lock_dir, n)
        else:
            actions += [f"would kill pid {v['pid']} ({v.get('what', '?')})"
                        for v in strays]
            actions.append(f"would remove {', '.join(str(p) for p in files)}")
        return {"display": n, "state": "stale", "reclaimed": apply,
                "evidence": ev, "actions": actions}

    def _server_is_ours(self, n: int, rec: dict | None,
                        addr: str | None) -> tuple[bool, list[str]]:
        """Is the process holding display `n` the one our receipt named?

        Every clause has to pass, and each one is a sentence in the report.
        The pid alone would not do — pids are reused — and the argv alone
        would not either, since "Xvfb :99 -screen 0 1920x1200x24" is a
        description anyone's script could match.  The pair (pid, start jiffy)
        within one boot is an identity, and that is what is being checked;
        the argv and the uid are there so that a receipt corrupted into
        naming the wrong process still cannot authorise a kill.

        `addr` is where a server answered, or None when the receipt names one
        that never got that far.
        """
        ev = [f"an X server is listening on {addr}"] if addr else []
        if not rec:
            return False, ev + [f"no receipt: this pool has no record of "
                                f"starting a server on :{n}"]
        if rec.get("boot") != _boot_id():
            return False, ev + [f"the receipt is from boot {rec.get('boot')}, "
                                f"not this one — its pids mean nothing now"]
        srv = rec.get("server")
        if not srv:
            return False, ev + ["the receipt records a claim but no server of "
                                "ours on this number"]
        start = _proc_start(srv["pid"])
        if start is None:
            return False, ev + [f"the receipt names pid {srv['pid']} as our "
                                f"server and that pid is gone" + (
                                    ", so whatever is listening is somebody "
                                    "else" if addr else "")]
        if start != srv["start"]:
            return False, ev + [f"pid {srv['pid']} is alive but started at "
                                f"{start}, not {srv['start']} — the pid was "
                                f"reused and this is a different process"]
        argv = _proc_argv(srv["pid"])
        if argv != srv.get("argv"):
            return False, ev + [f"pid {srv['pid']} is running {argv}, "
                                f"which is not the command we recorded"]
        uid = _proc_uid(srv["pid"])
        if uid != os.getuid():
            return False, ev + [f"pid {srv['pid']} belongs to uid {uid}, "
                                f"not to us"]
        when = time.strftime("%H:%M:%S", time.localtime(rec.get("claimed", 0)))
        return True, ev + [
            f"it is pid {srv['pid']}, started at jiffy {start} — the exact "
            f"process this pool spawned at {when} and never stopped",
            f"its command line is unchanged: {' '.join(argv)}",
            f"its owner (pid {(rec.get('owner') or {}).get('pid')}) is gone, "
            f"which is why nothing released the claim"] + ([] if addr else [
                f"it is not listening on :{n} yet — an Xvfb interrupted "
                f"during startup, which would refuse the next run's server "
                f"the moment it did come up"])

    def _files_are_ours(self, n: int, rec: dict | None,
                        files) -> tuple[bool, list[str]]:
        """Did our server write the files left on `n`?

        The X server writes its own pid into `/tmp/.X<n>-lock`, so when that
        number is the pid our receipt recorded, the file is ours by its own
        contents and not by inference.  Where there is no lock file the socket
        file stands on the receipt alone: we ran a server on this number this
        boot, that process is gone, and nothing is listening — so the file
        cannot belong to a session anybody is using.
        """
        ev = []
        if not rec or rec.get("boot") != _boot_id() or not rec.get("server"):
            return False, ev + [f"no receipt of this boot records a server of "
                                f"ours on :{n}"]
        srv = rec["server"]
        if _still(srv):
            return False, ev + [f"the server we recorded "
                                f"(pid {srv['pid']}) is still running but "
                                f"is not listening — leaving it alone"]
        ev.append(f"our receipt records Xvfb pid {srv['pid']} on :{n} this "
                  f"boot, and that process is gone")
        lock = self.sock_dir.parent / f".X{n}-lock"
        if lock.exists():
            pid = _x_lock_pid(lock)
            if pid != srv["pid"]:
                return False, ev + [f"{lock} names pid {pid}, not the "
                                    f"server we started — somebody else "
                                    f"wrote it"]
            ev.append(f"{lock} contains pid {pid}: the X server writes its "
                      f"own pid there, so our process wrote this file")
        return True, ev

    def _our_leftovers(self, n: int, rec: dict | None) -> list[dict]:
        """The WPS processes still on display `n` that are provably ours.

        Two ways to be sure, and a process needs one of them.

        *Recorded.*  The receipt holds the identity of every client this pool
        spawned; a pid whose start jiffy still matches is that same process.

        *Adopted.*  A WPS that segfaults restarts itself as `wpp recover1` in
        a session of its own, which `killpg` cannot follow and which the
        receipt therefore never saw.  It is claimed only on all four of the
        conditions yesterday's cleanup used: our uid, `DISPLAY=:n` in its own
        environment, a cwd inside a scratch directory this module made, and
        the display's claim provably unheld (which the caller has already
        established by holding it).  A process satisfying all four cannot
        belong to anyone else — nothing but this module puts a WPS on a pool
        display with a `wpsrt-` cwd.
        """
        out, seen = [], set()
        for cli in (rec or {}).get("clients") or []:
            if _still(cli):
                out.append({**cli, "what": "WPS we spawned, recorded in the "
                                           "receipt"})
                seen.add(cli["pid"])
        workdirs = [Path(w) for w in (rec or {}).get("workdirs") or []]
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or int(entry) in seen:
                continue
            pid = int(entry)
            if _proc_uid(pid) != os.getuid():
                continue
            argv = _proc_argv(pid)
            if not argv or Path(argv[0]).name not in ("wpp", Path(WPP).name):
                continue
            if _proc_env(pid).get("DISPLAY") != f":{n}":
                continue
            cwd = _proc_cwd(pid)
            if not cwd or not _is_our_workdir(Path(cwd), workdirs):
                continue
            start = _proc_start(pid)
            if start is None:
                continue
            out.append({"pid": pid, "start": start, "argv": argv,
                        "what": f"{' '.join(argv)[:40]} — DISPLAY=:{n}, cwd "
                                f"{cwd}, our uid, claim unheld"})
        return out

    def _sweep_files(self, n: int) -> list[str]:
        """Remove the X files for `n`, once the server that made them is dead.

        Xvfb removes these itself on a clean exit and not on a kill, and one
        left behind retires the number for every later run — which is the
        whole defect.
        """
        done = []
        for p in self.files(n):
            if not p.exists():
                continue
            try:
                p.unlink()
                done.append(f"removed {p}")
            except OSError as e:
                done.append(f"could not remove {p}: {e}")
        return done

    # ----------------------------------------------------------------- #

    @contextlib.contextmanager
    def claim(self, wait: float = 0.0, poll: float = 1.0,
              reclaim: bool | None = None):
        """Own a display number for the duration of the block.

        Released on the way out however the block ends — return, exception or
        interrupt.  A leaked claim is worse than a crash: the next run blocks
        on it forever with nothing to show for it.

        When nothing is free, and before waiting or failing, the corpses of
        earlier runs are swept — but only the ones a receipt proves this pool
        started.  That sweep is what makes an interrupted run cost one display
        for one claim instead of one display forever, and it prints what it
        took: a reclaim nobody can see is as hard to trust as the silent
        queueing it fixes.  `PPTXGYM_DISPLAY_RECLAIM=0` turns it off.
        """
        if reclaim is None:
            reclaim = os.environ.get("PPTXGYM_DISPLAY_RECLAIM", "1") != "0"
        deadline = time.time() + wait
        swept = False
        while True:
            for n in self.numbers():
                fd = self._try(n)
                if fd is not None:
                    try:
                        yield f":{n}"
                    finally:
                        _drop_receipt(self.lock_dir, n)
                        self._unlock(fd)
                    return
            if reclaim and not swept:
                swept = True
                if self._sweep("nothing free"):
                    continue
            if time.time() >= deadline:
                raise NoFreeDisplay(self._exhausted())
            time.sleep(poll)

    def _sweep(self, why: str) -> int:
        """Reclaim what is provably ours, saying so on stderr.  Count taken."""
        taken = [r for r in self.reclaim(apply=True) if r["reclaimed"]]
        for r in taken:
            print(f"pptxgym: reclaimed :{r['display']} ({why}) — "
                  + "; ".join(r["evidence"][1:3] or r["evidence"]),
                  file=sys.stderr, flush=True)
        return len(taken)

    def _exhausted(self) -> str:
        """Why the pool has nothing, in enough detail to act on.

        `all 64 displays are in use` is true and useless.  A pool that is
        full of live runs needs patience; a pool that is full of corpses
        needs `reclaim`; a pool full of other people's X servers needs a
        different base.  These are three different days and the message says
        which one it is.
        """
        rows = self.survey()
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        parts = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        tail = ""
        if counts.get("foreign") or counts.get("unattributable"):
            tail = ("; some are held by X servers this pool cannot account "
                    "for — `python3 -m pptxgym.wps_roundtrip --reclaim` lists "
                    "them and what it would take")
        return (f"all {self.span} displays from :{self.base} are in use "
                f"({parts}){tail}")


POOL = DisplayPool()

_LIVE: set = set()          # screens started by this process, for the cleanup
_LIVE_LOCK = threading.Lock()


def cleanup(*_):
    """Kill every Xvfb and every WPS this process started.

    An abandoned Xvfb holds its display against the socket check forever, and
    a stranded WPS keeps writing into a file nobody is watching.

    Public, and the entry point a library caller should use.  `atexit` runs it
    on a normal exit and not on a signal, and a module that installs its own
    signal handlers on import steals them from the application that imported
    it — so the honest arrangement is that this is callable, `session()` wraps
    it, and a *program* (not a library) may additionally call
    `install_signal_handlers`.
    """
    with _LIVE_LOCK:
        screens = list(_LIVE)
    for s in screens:
        try:
            s.close()
        finally:
            # a `close` that throws half way must not leave the screen on a
            # list that something later iterates and closes again
            with _LIVE_LOCK:
                _LIVE.discard(s)


_cleanup_all = cleanup          # the name the older callers use

atexit.register(cleanup)


@contextlib.contextmanager
def session(reclaim: bool = True):
    """Everything this module starts inside the block is stopped after it.

    For callers that drive the round trip from inside a longer-lived program —
    the pipeline does — where `atexit` is both too late and, if the program is
    signalled, never.  Wrapping the WPS phase in this makes an interrupted
    phase cost nothing beyond itself, because `finally` runs for an exception
    and for a `KeyboardInterrupt` even though `atexit` does not run for a
    `SIGKILL`.

    `reclaim` sweeps the pool on the way in, which is where a *previous*
    process's leak gets collected: nothing inside this process can clean up
    after a process that is already gone, and the receipts can.
    """
    if reclaim:
        with contextlib.suppress(Exception):                    # noqa: BLE001
            POOL._sweep("starting a WPS session")
    try:
        yield POOL
    finally:
        cleanup()


def install_signal_handlers(signals=(signal.SIGTERM, signal.SIGINT)):
    """Stop what we started when the process is asked to stop.

    A program may call this.  A library may not, which is the whole reason
    receipts exist: handlers installed on import take SIGTERM away from
    whatever imported us, and the application's own shutdown then never runs.
    Returns the handlers it replaced, so a caller can put them back.
    """
    previous = {}
    for sig in signals:
        def handler(signum, frame):
            cleanup()
            old = previous.get(signum)
            if callable(old):
                return old(signum, frame)
            raise SystemExit(128 + signum)
        previous[sig] = signal.signal(sig, handler)          # noqa: B023
    return previous


class _Screen:
    """A private X display, so nothing appears on the user's desktop."""

    def __init__(self, num: str = DISPLAY, lock_dir=None):
        self.num = num
        self.proc = None
        self.clients: list = []
        #: where this display's receipt lives.  `POOL` is what every real
        #: claim comes from; a test pool passes its own.
        self.lock_dir = Path(lock_dir) if lock_dir else POOL.lock_dir

    @property
    def n(self) -> int:
        return int(self.num.lstrip(":"))

    def __enter__(self):
        argv = ["Xvfb", self.num, "-screen", "0", SCREEN + "x24",
                "-nolisten", "tcp"]
        self.proc = subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        with _LIVE_LOCK:
            _LIVE.add(self)
        # Written down before the server is even up, because the window in
        # which this process can be killed starts now.  A receipt naming a
        # server that never started costs a later run one refusal; a server
        # started with no receipt costs it the display.
        _amend_receipt(self.lock_dir, self.n, server={
            "pid": self.proc.pid, "start": _proc_start(self.proc.pid),
            "argv": argv, "started": time.time()})
        # Two conditions, and the order is not optional.  The server drops a
        # file on disk when it takes the display, so that file appearing is
        # what "Xvfb is up" means — and it has to be checked with `stat` and
        # not with xdotool, because xdotool against a display that does not
        # exist yet does not fail: Xlib sits on the connect and this blocked
        # for two minutes when it was asked first.  The quarter-second sleep
        # that used to come before it was load-bearing for that reason and
        # nothing else.
        #
        # Which file depends on the box.  `/tmp/.X11-unix` here is not mode
        # 1777, so Xvfb cannot bind the filesystem socket and listens on the
        # abstract one instead: `/tmp/.X11-unix/X99` never appears and only
        # `/tmp/.X99-lock` does.  Either one means the same thing, which is
        # also why `DisplayPool.occupied` reads both.
        n = self.num.lstrip(":")
        ready = (X_SOCKET_DIR / f"X{n}", X_SOCKET_DIR.parent / f".X{n}-lock")
        deadline = time.time() + 30
        while time.time() < deadline and not any(p.exists() for p in ready):
            if self.proc.poll() is not None:
                self.close()            # `__exit__` never runs for a throwing
                raise WpsUnavailable(   # `__enter__`, so give the claim back
                    f"Xvfb exited without starting {self.num}")
            time.sleep(0.02)
        while time.time() < deadline:
            try:
                if subprocess.run(["xdotool", "search", "--name", "."],
                                  env={**os.environ, "DISPLAY": self.num},
                                  capture_output=True,
                                  timeout=10).returncode in (0, 1):
                    break
            except subprocess.TimeoutExpired:
                pass
            time.sleep(0.05)
        return self

    def spawn(self, argv: list[str]) -> subprocess.Popen:
        """Run a program on this display, in its own process group.

        WPS forks; terminating the process we launched leaves the children
        holding the display.  Its own session means one `killpg` takes the
        whole family down.
        """
        proc = subprocess.Popen(argv, env=self.env,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)
        self.clients.append(proc)
        # so that a later run can end this WPS by name rather than by
        # description, if this one is killed before it can
        _amend_receipt(self.lock_dir, self.n, clients={
            "pid": proc.pid, "start": _proc_start(proc.pid), "argv": argv})
        return proc

    def close(self):
        for proc in self.clients:
            _killpg(proc)
        self.clients = []
        if self.proc:
            _killpg(self.proc)
            self.proc = None
        # Two things this cannot reach, both of which used to end with a
        # display number retired for every later run on the box, and both of
        # which the receipt written in `__enter__` now answers *after the
        # fact* — which is the only time they can be answered, because in
        # both cases this code did not run:
        #
        #   * a WPS that segfaults starts `wpp recover1` in a session of its
        #     own, which `killpg` by construction cannot follow.  `_Screen`
        #     never sees it; `DisplayPool._our_leftovers` finds it later by
        #     its display, its cwd and its uid.
        #   * `atexit` does not run when the owning process is signalled, so
        #     a `kill` of the caller leaves the Xvfb up and the lock file on
        #     disk with nobody holding the claim.  That is what `reclaim`
        #     collects, and the receipt is what lets it prove the Xvfb it
        #     finds is this one and not a stranger's.
        #
        # Xvfb removes these on a clean exit and not on a kill; leaving one
        # behind would retire the display number permanently.  This one is
        # ours — we started the server on it and still hold its claim.
        n = self.num.lstrip(":")
        for p in (X_SOCKET_DIR / f"X{n}", X_SOCKET_DIR.parent / f".X{n}-lock"):
            with contextlib.suppress(OSError):
                p.unlink()
        # the server named in the receipt is dead, so the receipt is no longer
        # evidence about anything; a stale one whose pid gets reused is the
        # one way this scheme could point a later reclaim at a stranger
        _amend_receipt(self.lock_dir, self.n, server=None, clients=[])
        with _LIVE_LOCK:
            _LIVE.discard(self)

    def __exit__(self, *exc):
        self.close()
        return False

    @property
    def env(self):
        return {**os.environ, "DISPLAY": self.num}


def _alive(proc: subprocess.Popen) -> bool:
    """Is anything left of the process group we launched?

    `proc.poll()` is not enough: WPS's launcher exits and leaves the real
    application behind as a child.  `start_new_session` makes the group id
    equal to the pid we spawned, so the group outlives the leader and can be
    asked about after the leader has been reaped.
    """
    if proc.poll() is None:
        return True
    try:
        os.killpg(proc.pid, 0)
    except OSError:
        return False
    return True


def _killpg(proc: subprocess.Popen, grace: float = 10.0):
    """SIGTERM the process group, then insist."""
    if proc.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), sig)
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            grace = 5.0


TRACE = os.environ.get("PPTXGYM_WPS_TRACE")


def _trace(what: str, since: float):
    """Where the seconds went, when somebody is asking.

    Every wait below is a wait for a condition, so the interesting question
    stops being "how long is the sleep" and becomes "which condition was slow
    on this deck".  `PPTXGYM_WPS_TRACE=1` answers it without a rebuild.
    """
    if TRACE:
        print(f"  wps {what:<22}{time.time() - since:6.2f}s",
              file=sys.stderr, flush=True)


def _xdo(env, *args) -> str:
    # A display that has gone away takes xdotool with it — Xlib blocks on the
    # connect rather than failing — and every wait below is written to give up
    # eventually, so none of them may sit behind a call that never returns.
    try:
        return subprocess.run(["xdotool", *args], env=env, capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except subprocess.TimeoutExpired:
        return ""


def _windows(env, pattern: str) -> list[str]:
    return [w for w in _xdo(env, "search", "--onlyvisible",
                            "--name", pattern).splitlines() if w]


def _titles(env) -> list[tuple[str, str]]:
    """Every visible window, as (id, title)."""
    ids = [w for w in _xdo(env, "search", "--onlyvisible",
                           "--name", ".").splitlines() if w]
    return [(w, _xdo(env, "getwindowname", w)) for w in ids]


def parse_title(title: str) -> tuple[str, bool] | None:
    """The document name and whether it has unsaved changes, from the title.

    WPS writes `source.pptx - Presentation`, and inserts a ` * ` once the
    document is modified.  Two facts fall out of that, and both replace a
    sleep: the title only names a file once the file is open, and the star is
    a direct read of the dirty flag that the whole notes-pane trick exists to
    set.  Returns None for the bare `Presentation` of a window with nothing
    loaded in it yet, and for anything that is not a document window.
    """
    m = TITLE_RE.match(title or "")
    if not m or not m.group("name"):
        return None
    return m.group("name"), bool(m.group("dirty"))


def _document(env, name: str | None = None):
    """The (id, dirty) of the open document window, or None if there is none.

    `name` picks between several open documents; without it any loaded one
    will do, which is what a fresh display always has.
    """
    fallback = None
    for wid, title in _titles(env):
        parsed = parse_title(title)
        if parsed is None:
            continue
        got, dirty = parsed
        if name is not None and got == name:
            return wid, dirty
        fallback = fallback or (wid, dirty)
    return fallback


def _group_load(pgid: int) -> tuple[float, int]:
    """CPU seconds burned and memory held, over every process in the group.

    Reading these twice a second and watching them stop moving is what "the
    document has finished loading" looks like from outside the application —
    and unlike a fixed wait it is right on a deck that takes a minute as well
    as on one that takes two seconds.  Both numbers are needed: pulling a
    hundred megabytes of media off disk is nearly free of CPU and shows up
    only as memory, and clicking into a document that is still doing it is
    how one deck's WPS took a segfault.
    """
    hz = os.sysconf("SC_CLK_TCK")
    cpu, rss = 0.0, 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as fh:
                fields = fh.read().rsplit(") ", 1)[1].split()
            if int(fields[2]) != pgid:          # 5th field of stat: pgrp
                continue
            cpu += (int(fields[11]) + int(fields[12])) / hz
            rss += int(fields[21]) * 4096
        except (OSError, IndexError, ValueError):
            continue                            # it exited mid-read
    return cpu, rss


def _wait_idle(proc, deadline: float, window: float = IDLE_WINDOW,
               budget: float = IDLE_CPU, growth: int = IDLE_RSS) -> bool:
    """Block until the process group stops working, or the deadline passes."""
    quiet_since = None
    cpu, rss = _group_load(proc.pid)
    while time.time() < deadline:
        time.sleep(POLL)
        now_cpu, now_rss = _group_load(proc.pid)
        busy = (now_cpu - cpu > budget) or abs(now_rss - rss) > growth
        if busy or quiet_since is None:
            quiet_since, cpu, rss = (None if busy else time.time()), now_cpu, now_rss
        elif time.time() - quiet_since >= window:
            return True
        if not _alive(proc):
            return False
    return False


def _wait_loaded(env, proc, name: str, deadline: float):
    """Block until the deck is open and the application has gone quiet.

    Two separate things, in this order.  The title says the document is open;
    the CPU and the memory say it has finished being read and drawn.  Acting
    on the title alone clicks into a notes pane that has not been laid out
    yet, and a click that misses the notes pane types into whatever it hits
    instead.
    """
    while time.time() < deadline:
        found = _document(env, name)
        if found:
            _wait_idle(proc, deadline)
            return found
        if not _alive(proc):
            return None
        time.sleep(POLL)
    return None


def _dismiss_dialogs(env, rounds: int = 4) -> list[str]:
    """Close whatever WPS puts up before it will do anything else.

    The first launch shows a font check ("Some formula symbols might not be
    displayed correctly due to missing fonts Symbol") and sits there.  Its
    Close button ignores clicks and Escape, and `xdotool windowkill` takes the
    whole application down with it — it kills the X client, not the window.
    `windowclose` sends WM_DELETE_WINDOW and the dialog goes quietly.
    """
    seen = []
    for _ in range(rounds):
        ids = _windows(env, DIALOG_RE)
        if not ids:
            break
        for wid in ids:
            seen.append(_xdo(env, "getwindowname", wid) or wid)
            _xdo(env, "windowclose", wid)
        # the window going away is the thing that was being waited for, and it
        # takes well under a second; the two seconds this used to sleep were a
        # guess at the worst case
        gone = time.time() + DIALOG_GONE
        while time.time() < gone and set(_windows(
                env, DIALOG_RE)) & set(ids):
            time.sleep(0.1)
    return seen


def _settle_dialogs(env, deadline: float, grace: float = DIALOG_GRACE,
                    quiet: float = DIALOG_QUIET) -> list[str]:
    """Close the first-run dialogs, and wait out the ones still coming.

    Whether a dialog appears at all, and when, is the one thing in this
    module with no observable answer — there is no signal for "no dialog is
    on its way".  So this is still a wait on a clock, but a short one: watch
    for `grace` seconds, and once something has been closed, insist on
    `quiet` seconds with nothing new before calling it clear.  The dialogs
    are modal, so closing one early and walking away leaves the next one to
    swallow every click that follows.
    """
    seen, watch_until = [], time.time() + grace
    while time.time() < min(watch_until, deadline):
        closed = _dismiss_dialogs(env, rounds=4)
        if closed:
            seen += closed
            watch_until = time.time() + quiet
        else:
            time.sleep(POLL)
    return seen


def _notes_candidates(env, name: str) -> list[tuple[int, int]]:
    """Points to try clicking for the notes pane, best first.

    `NOTES_XY` is a point on a `SCREEN`-sized window, and it is right whenever
    the window fills the screen — which it does on a developer machine and did
    not in an HF Jobs container, where there is no window manager to maximise
    anything.  The symptom there was precise and misleading: keys demonstrably
    arrived (F5 opened the slideshow) and the document never went modified,
    because the click was landing outside any editable field.

    So the first candidate is `NOTES_XY` rescaled to whatever the window
    actually is, and the rest walk up the bottom of the window, because how
    much height the notes pane takes depends on a view state we do not
    control.  Falling back to the constant when the geometry cannot be read
    keeps the old behaviour rather than inventing a worse one.
    """
    win = _document(env, name)
    geom = None
    if win:
        out = _xdo(env, "getwindowgeometry", "--shell", win[0]) or ""
        got = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
        try:
            geom = (int(got["X"]), int(got["Y"]),
                    int(got["WIDTH"]), int(got["HEIGHT"]))
        except (KeyError, ValueError):
            geom = None
    if not geom:
        return [NOTES_XY]

    x0, y0, w, h = geom
    sw, sh = (int(v) for v in SCREEN.split("x"))
    if (w, h) == (sw, sh) and (x0, y0) == (0, 0):
        # The window is exactly the screen, which is the case this constant
        # was measured on. Scaling it by a ratio of 1 is a no-op, but going
        # through the arithmetic at all invites the failure below, so say so.
        _trace(f"notes {NOTES_XY} (window is the screen)", time.time())
        return [NOTES_XY, (NOTES_XY[0], 1120), (NOTES_XY[0], 1160)]
    fx, fy = NOTES_XY[0] / sw, NOTES_XY[1] / sh
    # The first is the constant expressed as a fraction of the real window;
    # the others sit progressively further up its bottom edge.
    pts = [(x0 + int(w * fx), y0 + int(h * f))
           for f in (fy, 0.90, 0.84, 0.78)]
    # Logged because getting this wrong is silent: every click lands somewhere
    # harmless, nothing is typed anywhere, and the failure reads as "the notes
    # edit did not reach the document" — the same message as five unrelated
    # causes. A container probe proved (500, 1143) on a 1920x1200 screen does
    # dirty the document, so if production fails while the probe succeeds, the
    # points are the thing to look at first.
    _trace(f"notes {pts} from window {geom}", time.time())
    return pts


def _wait_dirty(env, name: str, deadline: float) -> bool:
    """Has the title picked up its modified marker yet?"""
    while time.time() < deadline:
        found = _document(env, name)
        if found and found[1]:
            return True
        time.sleep(POLL)
    return False


def _wait_written(path: Path, before: int, deadline: float) -> bool:
    """Block until `path` is a complete package that WPS has stopped writing.

    The four seconds this replaces were "let the write settle".  What settled
    is observable: the size stops changing and the package can be opened and
    its directory read.  Saying no here is what stops a half-written file
    being compared against the original, where it would read as a deck that
    lost every shape in it — `fragile`, for a renderer that did nothing.
    """
    last, still = None, 0
    while time.time() < deadline:
        try:
            st = path.stat()
        except OSError:
            time.sleep(0.1)
            continue
        if st.st_mtime_ns == before:
            time.sleep(0.1)
            continue
        still = still + 1 if st.st_size == last else 0
        last = st.st_size
        if still >= 2:
            with contextlib.suppress(OSError, zipfile.BadZipFile, RuntimeError):
                with zipfile.ZipFile(path) as z:
                    if "[Content_Types].xml" in z.namelist():
                        return True
        time.sleep(0.1)
    return False


# --------------------------------------------------------------------------- #
# the scratch directories
# --------------------------------------------------------------------------- #


def _paths_in_use() -> set[str]:
    """Every path some live process is standing in, has open, or has mapped.

    One walk of /proc for all of them, because the question is asked about a
    few hundred directories at once.  Processes belonging to other users are
    unreadable and skipped, which loses nothing: a `wpsrt-` directory is mode
    700 under our uid, so nobody else can be inside one.
    """
    out: set[str] = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        base = f"/proc/{entry}"
        with contextlib.suppress(OSError):
            out.add(os.readlink(f"{base}/cwd"))
        with contextlib.suppress(OSError):
            for fd in os.listdir(f"{base}/fd"):
                with contextlib.suppress(OSError):
                    out.add(os.readlink(f"{base}/fd/{fd}"))
        with contextlib.suppress(OSError):
            with open(f"{base}/maps") as fh:
                for line in fh:             # a file can be mapped and closed
                    path = line.split(" ", 5)[-1].strip()
                    if path.startswith("/"):
                        out.add(path)
    return out


def _live_workdirs(lock_dir=None) -> set[str]:
    """Scratch directories named by a receipt whose owner is still running."""
    d = Path(lock_dir or POOL.lock_dir)
    live: set[str] = set()
    with contextlib.suppress(OSError):
        for f in d.glob("display*.json"):
            rec = {}
            with contextlib.suppress(OSError, ValueError):
                rec = json.loads(f.read_text())
            if rec.get("boot") == _boot_id() and _still(rec.get("owner")):
                live.update(rec.get("workdirs") or [])
    return live


def reclaim_workdirs(apply: bool = False, root: Path | str = None,
                     min_age: float = WORK_MIN_AGE,
                     lock_dir=None) -> list[dict]:
    """Sweep the `wpsrt-` directories no live run can be using.

    Same rule as the displays: take only what can be shown to be dead.  A
    directory is swept when all four hold, and the record says which of them
    a survivor failed:

      * it is `/tmp/wpsrt-*` — a name only `_roundtrip_on` produces;
      * it belongs to our uid;
      * no live process has it as a cwd, has a file in it open, or has one
        mapped, and no live claim's receipt names it;
      * it has not been touched for `min_age` — fifteen minutes, against a
        round trip that takes twelve seconds and times out at four minutes.

    The last two overlap on purpose.  The in-use scan is exact but has a
    blind moment — between `mkdtemp` and WPS opening the copy, nothing holds
    the directory — and the age check covers precisely that moment.

    They are not free: 257 of them, 1.2 GB, were on this box, each a copy of a
    deck that is still in `work/` anyway.  They accumulated because a failed
    attempt left its copy behind; that is fixed at the source in
    `_roundtrip_on`, and this is for the ones already on disk.
    """
    root = Path(root or WORK_ROOT)
    in_use, live, now = _paths_in_use(), _live_workdirs(lock_dir), time.time()
    out = []
    for d in sorted(root.glob(WORK_PREFIX + "*")):
        if not d.is_dir():
            continue
        why = []
        try:
            st = d.stat()
        except OSError:
            continue
        if st.st_uid != os.getuid():
            why.append(f"owned by uid {st.st_uid}, not by us")
        age = now - st.st_mtime
        if age < min_age:
            why.append(f"touched {age:.0f}s ago, inside the {min_age:.0f}s "
                       f"margin a running deck could still be in")
        if str(d) in live:
            why.append("a live run's receipt names it")
        holders = [p for p in in_use
                   if p == str(d) or p.startswith(str(d) + "/")]
        if holders:
            why.append(f"a live process is using {holders[0]}")
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) \
            if not why else 0
        rec = {"path": str(d), "bytes": size, "dead": not why,
               "kept_because": why, "removed": False}
        if not why:
            rec["evidence"] = [
                f"{d.name} is a `{WORK_PREFIX}` directory under {root}, which "
                f"only this module creates",
                f"uid {st.st_uid} is ours and it is mode "
                f"{oct(st.st_mode & 0o777)}, so no other user can be in it",
                "nothing on this machine has it open, mapped or as a cwd",
                f"it has not been written to for {age / 3600:.1f} h"]
            if apply:
                shutil.rmtree(d, ignore_errors=True)
                rec["removed"] = not d.exists()
        out.append(rec)
    return out


def roundtrip_wps(pptx: str, timeout: int = 240, display: str | None = None,
                  wait: float = 0.0, attempts: int = 2) -> Path:
    """Open the deck in WPS, save it, return the saved copy.

    `display` names the X display to use.  Left off, one is claimed from
    `POOL` for the length of the call and released again — which is what
    makes several of these safe to run at once.

    A failed attempt is retried on the same claim.  WPS segfaults during
    startup a couple of times in a hundred launches on this box, taking its
    deck with it, and nothing about the deck predicts it.  Each attempt opens
    its own copy under its own Xvfb, so the second owes nothing to the first,
    and twelve seconds spent retrying is cheaper than a deck coming back
    unmeasured — which is what the old sequence did with it, after four
    minutes of holding a display.
    """
    def run(num):
        last = None
        for n in range(max(1, attempts)):
            started = time.time()
            try:
                return _roundtrip_on(pptx, num, timeout)
            except WpsUnavailable as error:
                if preflight():             # not flakiness; nothing to retry
                    raise
                last = error
                _trace(f"attempt {n + 1} failed ({error})", started)
        raise last

    if display is not None:
        return run(display)
    with POOL.claim(wait=wait) as num:
        return run(num)


def _roundtrip_on(pptx: str, display: str, timeout: int = 240) -> Path:
    missing = preflight()
    if missing:
        raise WpsUnavailable("; ".join(missing))

    src = Path(pptx).resolve()
    work = Path(tempfile.mkdtemp(prefix=WORK_PREFIX, dir=WORK_ROOT))
    target = work / src.name
    shutil.copy2(src, target)
    before = target.stat().st_mtime_ns
    # so that a WPS which restarts itself out of `killpg`'s reach can still be
    # recognised later by where it is standing.  Only onto a receipt that
    # already exists: one exists exactly when this display was claimed, and
    # inventing one here would put a note about our scratch directory into the
    # record of a display somebody else may be holding.
    with contextlib.suppress(Exception):                        # noqa: BLE001
        n = int(display.lstrip(":"))
        if _read_receipt(POOL.lock_dir, n):
            _amend_receipt(POOL.lock_dir, n, workdirs=str(work))

    binary = WPP if Path(WPP).exists() else "wpp"
    try:
        return _open_and_save(src, target, binary, display, timeout, before)
    except BaseException:
        # The copy is worth nothing once the attempt has failed, and the
        # caller only deletes directories it was handed.  257 of these, 1.2 GB
        # of /tmp, were the retries and the failures of a few days' running —
        # each one a deck-sized copy of a deck that is still on disk anyway.
        shutil.rmtree(work, ignore_errors=True)
        raise


def _open_and_save(src: Path, target: Path, binary: str, display: str,
                   timeout: int, before: int) -> Path:
    """The GUI sequence itself, for one deck on one display."""
    t0 = time.time()
    with _Screen(display) as screen:
        env = screen.env
        _trace("xvfb", t0)
        proc = screen.spawn([binary, str(target)])
        try:
            deadline = time.time() + timeout
            mark = time.time()
            # The main window exists almost immediately, but the font-check
            # dialog arrives several seconds later and is modal — polling for
            # the window and pressing on swallowed every click that followed.
            # Wait for the document to be up, then clear dialogs, then insist
            # nothing modal is left before touching anything.  That order is
            # the load-bearing part and it has not changed; what has changed
            # is that "up" is now read off the window title and the process's
            # CPU rather than counted out on a clock.
            if not _wait_loaded(env, proc, target.name, deadline):
                raise WpsUnavailable(
                    f"WPS never opened {src.name}: "
                    f"{[t for _, t in _titles(env)]}")
            # How slow this machine is being, measured rather than assumed.
            # See DIRTY_WAIT_LOAD_SHARE.
            dirty_wait = max(DIRTY_WAIT,
                             (time.time() - mark) * DIRTY_WAIT_LOAD_SHARE)
            _trace("loaded", mark)
            mark = time.time()
            _settle_dialogs(env, deadline)
            # a dialog can hold the load up, so the quiet has to be re-earned
            # after they are gone rather than before
            _wait_idle(proc, deadline)
            _trace("dialogs", mark)
            mark = time.time()
            if not _document(env, target.name):
                raise WpsUnavailable(
                    f"WPS never showed a window for {src.name}")
            left = _windows(env, DIALOG_RE)
            if left:
                raise WpsUnavailable(
                    f"a modal dialog would not close: "
                    f"{[_xdo(env, 'getwindowname', w) for w in left]}")

            # Dirty the document, then undo the edit by hand.
            #
            # Neither Ctrl+S nor the toolbar's save button writes anything
            # while the file is unmodified — WPS treats saving an untouched
            # document as a no-op, exactly as PowerPoint does.  Three rounds
            # of debugging went into blaming focus, the window manager and
            # synthetic key events before that turned out to be the whole
            # story: clicks and keys had been working the entire time.
            #
            # Typing into the notes pane and deleting it again leaves the
            # deck's content where it was and the dirty flag set.  The title
            # gains a ` * ` when it works, which is both the signal to stop
            # waiting and the only check there has ever been that the
            # keystrokes reached the document at all.
            # Where the notes pane is, asked rather than assumed.
            #
            # NOTES_XY is a constant because SCREEN is, and on this machine the
            # window fills the screen.  In an HF Jobs container it does not:
            # keystrokes were measured *arriving* there — F5 opened the
            # slideshow, two new windows — while the document never went
            # modified, which is exactly what a click landing outside any
            # editable field looks like.
            #
            # Trying another candidate is safe under this function's own
            # invariant: "a document that never went modified cannot be holding
            # the first ZZ".  Not-dirty means nothing landed, so a later
            # attempt cannot be a second mark in the file, and only the attempt
            # that dirties is ever backspaced.
            for point in _notes_candidates(env, target.name):
                _xdo(env, "mousemove", str(point[0]), str(point[1]))
                _xdo(env, "click", "1")
                # Give the window the X input focus, explicitly.
                #
                # `xdotool type` sends to whatever holds the input focus, and
                # nothing here has ever set it. On this machine that is
                # harmless: with no window manager the server is in PointerRoot
                # mode, keyboard input follows the pointer, and the pointer is
                # over the window because we just clicked there. A container's
                # server does not do that, and the screenshot showed the
                # consequence exactly — the notes pane still reading "Click to
                # add notes" after 71 seconds of clicking and typing at the
                # right coordinates. Nothing was landing anywhere.
                #
                # Not an error if it fails: a server that will not take the
                # focus request is no worse off than before the call.
                doc = _document(env, target.name)
                if doc:
                    _xdo(env, "windowfocus", doc[0])
                # The two seconds that used to sit between the click and the
                # typing were doing something after all: clicking into the
                # notes pane makes WPS re-lay-out, and typing into it while it
                # is doing that segfaulted the application on 4 decks in 66
                # runs — a failed deck rather than a wrong one, but a deck to
                # run again.  Waiting for the same quiet the load waits for
                # costs a second and a half and has held at 0 crashes since.
                _wait_idle(proc, deadline)
                _xdo(env, "type", "--delay", "120", DIRTY_MARK)
                dirty = _wait_dirty(env, target.name,
                                    min(deadline, time.time() + dirty_wait))
                if dirty or time.time() >= deadline:
                    break
            if not dirty:
                # Something swallowed them.  A dialog that arrived after the
                # settle above is the one explanation that can be confirmed —
                # and confirming it is what makes a second attempt safe,
                # because a document that never went modified cannot be
                # holding the first `ZZ`.  Without that evidence, type
                # nothing more: two marks in the file and one set of
                # backspaces is the silent corruption this whole function
                # exists to avoid.
                if _settle_dialogs(env, deadline, grace=0.5):
                    _xdo(env, "mousemove", str(NOTES_XY[0]), str(NOTES_XY[1]))
                    _xdo(env, "click", "1")
                    _xdo(env, "type", "--delay", "120", DIRTY_MARK)
                    dirty = _wait_dirty(env, target.name,
                                        min(deadline, time.time() + dirty_wait))
            # Exactly as many backspaces as characters typed.  The `+ 2` that
            # used to be here was insurance against a dropped keystroke, and
            # it ate two characters of every deck's real speaker notes —
            # `fundamental` came back as `fundameal`.  A stray `Z` left behind
            # by a keystroke that never landed is visible and reportable; text
            # silently deleted from the file being measured is neither, and
            # this function's whole purpose is to leave the document alone.
            for _ in range(len(DIRTY_MARK)):
                _xdo(env, "key", "--clearmodifiers", "BackSpace")
            _wait_idle(proc, deadline)      # same again before pressing save
            _trace("dirtied" if dirty else "NOT DIRTIED", mark)
            mark = time.time()

            _xdo(env, "mousemove", str(SAVE_XY[0]), str(SAVE_XY[1]))
            _xdo(env, "click", "1")
            # A document that never looked modified is one WPS will decline to
            # write, and waiting the full timeout to find that out costs a
            # display four minutes.  Give it a generous minute and then say
            # what actually went wrong.  (The marker is also how a build that
            # does not draw one would present itself — hence a grace long
            # enough to save any deck in this corpus, rather than an error.)
            end = deadline if dirty else min(deadline,
                                             time.time() + UNDIRTIED_GRACE)
            while time.time() < end:
                if target.stat().st_mtime_ns != before:
                    if not _wait_written(target, before,
                                         min(deadline,
                                             time.time() + WRITE_SETTLE)):
                        raise WpsUnavailable(
                            f"WPS left {target.name} half-written")
                    _trace("saved", mark)
                    _trace("total", t0)
                    return target
                if not _alive(proc):
                    # Run enough of these at once and the box runs out of
                    # memory, and what dies is the WPS holding the biggest
                    # deck.  Waiting out the full timeout for a process that
                    # is already gone kept one display busy for four minutes
                    # and turned a 70 s batch into a 240 s one.  It also
                    # segfaults on its own every so often — six times in a day
                    # on this box, under the old timings as well as these — so
                    # the deck is worth retrying before the worker count is
                    # blamed.
                    raise WpsUnavailable(
                        f"WPS died before writing {target.name} — a segfault "
                        f"or, with several workers, the OOM killer")
                _dismiss_dialogs(env, rounds=1)  # "keep this format?", if asked
                time.sleep(POLL)
            raise WpsUnavailable(
                f"WPS never wrote {target.name}" + ("" if dirty else
                    " and the title never showed it as modified — the notes "
                    "edit did not reach the document"))
        finally:
            # not `proc.terminate()`: WPS forks, and a surviving child keeps
            # the display busy after the claim has been given back.
            #
            # Fifteen seconds of grace, which two decks in ten actually use.
            # Cutting it to five looked free — the document is clean by now,
            # so a tidy exit has nothing left to write — but a round trip that
            # takes 11 s instead of 37 s is also one that kills WPS 26 s
            # earlier in its life, while its own background threads are still
            # starting up, and what it leaves behind is read by the next
            # launch.  See the crash-rate note in the module docstring: this
            # is the one place where being patient is cheaper than being
            # right, so it waits.
            _killpg(proc, grace=15)


def check(pptx: str, display: str | None = None, wait: float = 0.0) -> dict:
    """Round-trip through WPS and report what it did, in `roundtrip`'s terms."""
    from . import roundtrip as lo

    saved = roundtrip_wps(pptx, display=display, wait=wait)
    try:
        rep = lo.compare(pptx, str(saved))
    finally:
        # `roundtrip_wps` hands back a copy in a directory of its own, and its
        # other caller keeps that file; this one only wants the comparison.
        # A hundred decks left behind a hundred copies of a hundred decks —
        # 617 MB of /tmp on this box before anyone noticed.
        shutil.rmtree(saved.parent, ignore_errors=True)
    rep["renderer"] = "wps"
    structural = (rep["counts"].get("missing", 0)
                  + rep["counts"].get("kind_changed", 0)
                  + rep["counts"].get("text_changed", 0)
                  + abs(rep["slides_before"] - rep["slides_after"]))
    rep["structural"] = structural
    rep["verdict"] = ("fragile" if structural or rep["changed_frac"] > 0.25
                      else "noisy" if rep["changed_frac"] > 0.05
                      else "stable")
    return rep


def batch(paths, workers: int = 4, timeout: int = 240, wait: float = 1800):
    """Round-trip many decks at once, yielding each result as it lands.

    Yields `{"pptx", "ok", "seconds", "display", "report" | "error"}` in
    completion order, not argument order — a hundred decks is over an hour
    even eight at a time, and a batch that only speaks at the end is a batch
    nobody can watch.

    Each worker claims its own display, so `workers` is how many displays get
    used at once and not which ones: two batches running side by side share
    one pool and simply take different numbers.  How high to set it is a
    memory question — about 660 MB per worker at the peak — and the module
    docstring has the measurements.
    """
    def one(path):
        t0 = time.time()
        rec = {"pptx": str(path), "seconds": 0.0, "display": None}
        try:
            with POOL.claim(wait=wait) as num:
                rec["display"] = num
                rec["report"] = check(str(path), display=num)
                rec["ok"] = True
        except Exception as e:                          # noqa: BLE001
            rec["ok"] = False
            rec["error"] = f"{type(e).__name__}: {e}"
        rec["seconds"] = round(time.time() - t0, 1)
        return rec

    paths = list(paths)
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = [pool.submit(one, p) for p in paths]
        for fut in as_completed(futures):
            yield fut.result()
    finally:
        # Ctrl-C, or a caller that stopped reading: drop what has not begun
        # and kill what has, rather than leaving Xvfbs behind holding
        # displays that nothing will ever release.
        pool.shutdown(wait=False, cancel_futures=True)
        _cleanup_all()


def report_reclaim(apply: bool = False, pool: DisplayPool | None = None,
                   workdirs: bool = True, out=sys.stdout) -> dict:
    """Say what is reclaimable, why, and what would be done about it.

    The dry run and the applied run take the same walk and print the same
    lines, so what somebody reads before saying yes is what happens when they
    do.  Every line that ends in an action is preceded by the sentences that
    justify it — which is the point: this file kills processes, and a kill
    nobody can attribute is worse than the leak it fixes.
    """
    pool = pool or POOL
    rows = pool.reclaim(apply=apply)
    verb = "reclaiming" if apply else "would reclaim"
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    print(f"displays :{pool.base}-:{pool.base + pool.span - 1}  "
          + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())), file=out)
    for r in rows:
        if r["state"] in ("free", "held") and not r["actions"]:
            continue
        head = {"ours": verb, "stale": verb}.get(r["state"], "leaving")
        print(f"\n  :{r['display']}  {r['state']} — {head}", file=out)
        for line in r["evidence"]:
            print(f"      · {line}", file=out)
        for line in r["actions"]:
            print(f"      → {line}", file=out)

    dirs = reclaim_workdirs(apply=apply) if workdirs else []
    if dirs:
        dead = [d for d in dirs if d["dead"]]
        held = len(dirs) - len(dead)
        size = sum(d["bytes"] for d in dead)
        print(f"\nwork directories in {WORK_ROOT}: {len(dirs)} found, "
              f"{len(dead)} dead ({size / 2**20:.0f} MB), {held} still in use "
              f"or too recent to judge", file=out)
        if dead:
            print(f"  {'removed' if apply else 'would remove'}: "
                  f"{', '.join(Path(d['path']).name for d in dead[:6])}"
                  + (f" and {len(dead) - 6} more" if len(dead) > 6 else ""),
                  file=out)
            print(f"  why {Path(dead[0]['path']).name} counts as dead — the "
                  f"same four hold for every one of them:", file=out)
            for line in dead[0]["evidence"]:
                print(f"      · {line}", file=out)
        for d in dirs:
            if not d["dead"]:
                print(f"      kept {Path(d['path']).name}: "
                      f"{'; '.join(d['kept_because'])}", file=out)
    if not apply:
        print("\nnothing has been changed.  `--reclaim --apply` to do it.",
              file=out)
    return {"displays": rows, "workdirs": dirs}


def main():
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pptx", nargs="*")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--workers", type=int, default=1,
                    help="decks to round-trip at once, one display each")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--reclaim", action="store_true",
                    help="report the displays and scratch directories an "
                         "interrupted run left behind, and what taking them "
                         "back would involve")
    ap.add_argument("--apply", action="store_true",
                    help="with --reclaim: actually take back what the report "
                         "says is provably ours")
    args = ap.parse_args()

    if args.reclaim:
        got = report_reclaim(apply=args.apply)
        if args.json:
            print(json.dumps(got, ensure_ascii=False, default=str))
        return
    if not args.pptx:
        ap.error("give at least one .pptx, or --reclaim")

    missing = preflight()
    if missing:
        raise SystemExit("cannot run: " + "; ".join(missing))

    install_signal_handlers()

    done, total, failed = 0, len(args.pptx), 0
    for rec in batch(args.pptx, workers=args.workers, timeout=args.timeout):
        done += 1
        name = Path(rec["pptx"]).parent.name + "/" + Path(rec["pptx"]).name
        if args.json:
            print(json.dumps(rec, ensure_ascii=False), flush=True)
        elif rec["ok"]:
            r = rec["report"]
            print(f"[{done}/{total}] {name[:40]:<42}{r['verdict']:<9}"
                  f"{r['changed']}/{r['shapes']} ({r['changed_frac']:.1%})  "
                  f"{rec['seconds']}s {rec['display']}  {r['counts']}",
                  flush=True)
        else:
            print(f"[{done}/{total}] {name[:40]:<42}FAILED — {rec['error']}",
                  flush=True)
        failed += not rec["ok"]
    if failed:
        raise SystemExit(f"{failed}/{total} failed")


if __name__ == "__main__":
    main()
