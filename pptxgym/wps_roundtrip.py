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

    python3 -m pptxgym.wps_roundtrip work/deck0001/source.pptx
    python3 -m pptxgym.wps_roundtrip --workers 6 work/deck00*/source.pptx
"""

from __future__ import annotations

import atexit
import contextlib
import errno
import fcntl
import os
import re
import shutil
import signal
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

DIRTY_WAIT = 15.0       # for the title's modified marker after typing.  It
                        # usually lands in under a second and has taken six on
                        # a loaded box; waiting longer costs nothing when it
                        # arrives, because the wait ends when it does
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
    """

    def __init__(self, base: int = DISPLAY_BASE, span: int = DISPLAY_SPAN,
                 lock_dir: Path | str = LOCK_DIR,
                 sock_dir: Path | str = X_SOCKET_DIR):
        self.base, self.span = base, span
        self.lock_dir = Path(lock_dir)
        self.sock_dir = Path(sock_dir)

    def numbers(self) -> list[int]:
        return list(range(self.base, self.base + self.span))

    def occupied(self, n: int) -> bool:
        """Is an X server already sitting on this number?"""
        return (self.sock_dir / f"X{n}").exists() or \
               (self.sock_dir.parent / f".X{n}-lock").exists()

    def _try(self, n: int):
        """The lock file descriptor for display `n`, or None if taken."""
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
        # the socket check goes *after* the lock, so that two claimants
        # racing for the same number cannot both read "free" and both start
        # an Xvfb on it
        if self.occupied(n):
            os.close(fd)
            return None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        return fd

    @contextlib.contextmanager
    def claim(self, wait: float = 0.0, poll: float = 1.0):
        """Own a display number for the duration of the block.

        Released on the way out however the block ends — return, exception or
        interrupt.  A leaked claim is worse than a crash: the next run blocks
        on it forever with nothing to show for it.
        """
        deadline = time.time() + wait
        while True:
            for n in self.numbers():
                fd = self._try(n)
                if fd is not None:
                    try:
                        yield f":{n}"
                    finally:
                        with contextlib.suppress(OSError):
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        os.close(fd)
                    return
            if time.time() >= deadline:
                raise NoFreeDisplay(
                    f"all {self.span} displays from :{self.base} are in use")
            time.sleep(poll)


POOL = DisplayPool()

_LIVE: set = set()          # screens started by this process, for the cleanup
_LIVE_LOCK = threading.Lock()


def _cleanup_all(*_):
    """Kill every Xvfb and every WPS this process started.

    An abandoned Xvfb holds its display against the socket check forever, and
    a stranded WPS keeps writing into a file nobody is watching.
    """
    with _LIVE_LOCK:
        screens = list(_LIVE)
    for s in screens:
        s.close()


atexit.register(_cleanup_all)


class _Screen:
    """A private X display, so nothing appears on the user's desktop."""

    def __init__(self, num: str = DISPLAY):
        self.num = num
        self.proc = None
        self.clients: list = []

    def __enter__(self):
        self.proc = subprocess.Popen(
            ["Xvfb", self.num, "-screen", "0", SCREEN + "x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        with _LIVE_LOCK:
            _LIVE.add(self)
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
        return proc

    def close(self):
        for proc in self.clients:
            _killpg(proc)
        self.clients = []
        if self.proc:
            _killpg(self.proc)
            self.proc = None
        # Two things this cannot reach, both worth knowing about because both
        # end with a display number retired for every later run on the box:
        #
        #   * a WPS that segfaults starts `wpp recover1` in a session of its
        #     own, which `killpg` by construction cannot follow.  The Xvfb
        #     below still dies, so the leftover is a process pointed at a
        #     display that no longer exists rather than a busy display — but
        #     it is a leftover, and it is somebody's memory.
        #   * `atexit` does not run when the owning process is signalled, so
        #     a `kill` of the caller leaves the Xvfb up and the lock file on
        #     disk with nobody holding the claim.  `main()` installs a SIGTERM
        #     handler for this; a library caller that wants the same
        #     protection has to install its own.
        #
        # Xvfb removes these on a clean exit and not on a kill; leaving one
        # behind would retire the display number permanently.  This one is
        # ours — we started the server on it and still hold its claim.
        n = self.num.lstrip(":")
        for p in (X_SOCKET_DIR / f"X{n}", X_SOCKET_DIR.parent / f".X{n}-lock"):
            with contextlib.suppress(OSError):
                p.unlink()
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
        ids = _windows(env, "System Check|Tip|Prompt")
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
                env, "System Check|Tip|Prompt")) & set(ids):
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
    work = Path(tempfile.mkdtemp(prefix="wpsrt-"))
    target = work / src.name
    shutil.copy2(src, target)
    before = target.stat().st_mtime_ns

    binary = WPP if Path(WPP).exists() else "wpp"
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
            left = _windows(env, "System Check|Tip|Prompt")
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
            _xdo(env, "mousemove", str(NOTES_XY[0]), str(NOTES_XY[1]))
            _xdo(env, "click", "1")
            # The two seconds that used to sit between the click and the
            # typing were doing something after all: clicking into the notes
            # pane makes WPS re-lay-out, and typing into it while it is doing
            # that segfaulted the application on 4 decks in 66 runs — a failed
            # deck rather than a wrong one, but a deck to run again.  Waiting
            # for the same quiet the load waits for costs a second and a half
            # and has held at 0 crashes since.
            _wait_idle(proc, deadline)
            _xdo(env, "type", "--delay", "120", DIRTY_MARK)
            dirty = _wait_dirty(env, target.name,
                                min(deadline, time.time() + DIRTY_WAIT))
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
                                        min(deadline, time.time() + DIRTY_WAIT))
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


def main():
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pptx", nargs="+")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--workers", type=int, default=1,
                    help="decks to round-trip at once, one display each")
    ap.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()

    missing = preflight()
    if missing:
        raise SystemExit("cannot run: " + "; ".join(missing))

    signal.signal(signal.SIGTERM, lambda *a: (_cleanup_all(), os._exit(143)))

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
