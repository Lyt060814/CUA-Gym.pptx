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

One round trip costs 40 s here and up to 90 s on a slow deck, and the reward
stage wants one per task: a thousand tasks is most of a day if they queue
behind a single display, which they did while the display number was a
constant.  Each run now claims its own out of `DisplayPool`, and a round trip
is 4 s of CPU inside 40 s of waiting — so the limit is memory, not cores.

Measured on this box, ten `work/` decks, every concurrent verdict identical
to the serial one:

    workers   1      2      4      6      8
    s/deck    38.9   19.0   10.5   6.6    5.1

A display costs about 500 MB while it runs — 86 MB of Xvfb and 400 MB of WPS,
with a peak near 660 MB while the document loads — and 3.9 s of CPU.  So the
ceiling is (free memory / 0.66 GB): six on this machine with 3.5 GB free,
around twenty on an idle one, a few hundred on a 256 GB server long before 32
vCPUs would notice.  Past it, the kernel kills the WPS holding the biggest
deck; that deck reports a failure rather than a wrong answer, but it is a
deck you have to run again, so leave headroom.

    python3 -m pptxgym.wps_roundtrip work/deck0001/source.pptx
    python3 -m pptxgym.wps_roundtrip --workers 6 work/deck00*/source.pptx
"""

from __future__ import annotations

import atexit
import contextlib
import errno
import fcntl
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
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
LOAD_WAIT = 20          # the font-check dialog arrives seconds after the window

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
        for _ in range(40):
            time.sleep(0.25)
            if subprocess.run(["xdotool", "search", "--name", "."],
                              env={**os.environ, "DISPLAY": self.num},
                              capture_output=True).returncode in (0, 1):
                break
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


def _xdo(env, *args) -> str:
    return subprocess.run(["xdotool", *args], env=env,
                          capture_output=True, text=True).stdout.strip()


def _windows(env, pattern: str) -> list[str]:
    return [w for w in _xdo(env, "search", "--onlyvisible",
                            "--name", pattern).splitlines() if w]


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
        ids = [w for w in _xdo(env, "search", "--onlyvisible", "--name",
                               "System Check|Tip|Prompt").splitlines() if w]
        if not ids:
            break
        for wid in ids:
            seen.append(_xdo(env, "getwindowname", wid) or wid)
            _xdo(env, "windowclose", wid)
        time.sleep(2)
    return seen


def roundtrip_wps(pptx: str, timeout: int = 240,
                  display: str | None = None, wait: float = 0.0) -> Path:
    """Open the deck in WPS, save it, return the saved copy.

    `display` names the X display to use.  Left off, one is claimed from
    `POOL` for the length of the call and released again — which is what
    makes several of these safe to run at once.
    """
    if display is not None:
        return _roundtrip_on(pptx, display, timeout)
    with POOL.claim(wait=wait) as num:
        return _roundtrip_on(pptx, num, timeout)


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
    with _Screen(display) as screen:
        env = screen.env
        proc = screen.spawn([binary, str(target)])
        try:
            deadline = time.time() + timeout
            # The main window exists almost immediately, but the font-check
            # dialog arrives several seconds later and is modal — polling for
            # the window and pressing on swallowed every click that followed.
            # Wait for the document to be up, then clear dialogs, then insist
            # nothing modal is left before touching anything.
            time.sleep(LOAD_WAIT)
            # Under a loaded box the *window* itself can be later than that.
            # Waiting for it is fine; what is not fine is pressing on as soon
            # as it appears, so the dialog still gets its head start.
            if not _windows(env, "Presentation"):
                while time.time() < deadline and not _windows(env, "Presentation"):
                    time.sleep(2)
                time.sleep(LOAD_WAIT)
            _dismiss_dialogs(env, rounds=4)
            found = _windows(env, "Presentation")
            if not found:
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
            # deck's content where it was and the dirty flag set.
            _xdo(env, "mousemove", str(NOTES_XY[0]), str(NOTES_XY[1]))
            _xdo(env, "click", "1")
            time.sleep(2)
            _xdo(env, "type", "--delay", "120", DIRTY_MARK)
            time.sleep(2)
            # Exactly as many backspaces as characters typed.  The `+ 2` that
            # used to be here was insurance against a dropped keystroke, and
            # it ate two characters of every deck's real speaker notes —
            # `fundamental` came back as `fundameal`.  A stray `Z` left behind
            # by a keystroke that never landed is visible and reportable; text
            # silently deleted from the file being measured is neither, and
            # this function's whole purpose is to leave the document alone.
            for _ in range(len(DIRTY_MARK)):
                _xdo(env, "key", "--clearmodifiers", "BackSpace")
            time.sleep(2)

            _xdo(env, "mousemove", str(SAVE_XY[0]), str(SAVE_XY[1]))
            _xdo(env, "click", "1")
            time.sleep(3)
            _dismiss_dialogs(env, rounds=1)     # "keep this format?", if asked

            while time.time() < deadline:
                if target.stat().st_mtime_ns != before:
                    time.sleep(4)          # let the write settle
                    return target
                if not _alive(proc):
                    # Run enough of these at once and the box runs out of
                    # memory, and what dies is the WPS holding the biggest
                    # deck.  Waiting out the full timeout for a process that
                    # is already gone kept one display busy for four minutes
                    # and turned a 70 s batch into a 240 s one.
                    raise WpsUnavailable(
                        f"WPS died before writing {target.name} — out of "
                        f"memory, most likely: fewer workers")
                time.sleep(2)
            raise WpsUnavailable(f"WPS never wrote {target.name} within {timeout}s")
        finally:
            # not `proc.terminate()`: WPS forks, and a surviving child keeps
            # the display busy after the claim has been given back
            _killpg(proc, grace=15)


def check(pptx: str, display: str | None = None, wait: float = 0.0) -> dict:
    """Round-trip through WPS and report what it did, in `roundtrip`'s terms."""
    from . import roundtrip as lo

    saved = roundtrip_wps(pptx, display=display, wait=wait)
    rep = lo.compare(pptx, str(saved))
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
