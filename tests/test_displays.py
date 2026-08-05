"""Who owns which X display, what dies with a run, and what it waits for.

Driving a real WPS needs Xvfb, WPS and half a minute, and none of that belongs
in a test suite.  Two halves of it can be tested anyway, and they are the two
that fail silently.

The allocator: two WPS instances on one display click each other's dialogs and
save into each other's documents, and the report still says `stable`.

The waiting: every fixed sleep in the round trip has been replaced by a wait
for the condition it stood in for, and a condition that is read wrong is worse
than a sleep that was too short — it can put keystrokes into a document that
was never meant to change.  So the conditions are tested against fakes that
reproduce what a real run does, including the ways a real run has gone wrong.

    python3 -m pytest tests/ -q
"""

import contextlib
import multiprocessing as mp
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import wps_roundtrip as w                            # noqa: E402


@pytest.fixture
def pool(tmp_path):
    """A pool over three displays, with its own lock and socket directories."""
    (tmp_path / "sock").mkdir()
    return w.DisplayPool(base=90, span=3,
                         lock_dir=tmp_path / "locks",
                         sock_dir=tmp_path / "sock")


# --------------------------------------------------------------------------- #
# exclusivity
# --------------------------------------------------------------------------- #


def test_two_claims_never_name_the_same_display(pool):
    """The whole point.  Two WPS instances on one display steal each other's
    focus and the wrong document gets saved, with no error anywhere."""
    with pool.claim() as a, pool.claim() as b, pool.claim() as c:
        assert len({a, b, c}) == 3


def test_a_display_taken_by_another_process_is_not_handed_out(tmp_path):
    """A second OS process holds display 90; this one must not take it too.
    `pptxgym` is a CLI and two invocations overlap all the time, so a lock
    that only counts threads counts the wrong thing."""
    (tmp_path / "sock").mkdir()
    kw = dict(base=90, span=3, lock_dir=str(tmp_path / "locks"),
              sock_dir=str(tmp_path / "sock"))
    got, release = mp.Queue(), mp.Event()
    proc = mp.Process(target=_hold, args=(kw, got, release))
    proc.start()
    try:
        assert got.get(timeout=20) == ":90"
        with w.DisplayPool(**kw).claim() as mine:
            assert mine != ":90"
    finally:
        release.set()
        proc.join(timeout=20)


def _hold(kw, got, release):
    with w.DisplayPool(**kw).claim() as num:
        got.put(num)
        release.wait(timeout=30)


def test_a_display_with_a_live_x_socket_is_skipped(pool):
    """`:0` is somebody's real desktop, and an Xvfb started outside the pool
    owns its number just as firmly.  Starting a second server on either one
    fails, or worse, lands the run on a display it does not control."""
    (Path(pool.sock_dir) / "X90").touch()
    with pool.claim() as num:
        assert num == ":91"


def test_an_x_lock_file_alone_is_enough_to_skip_a_display(pool):
    """Xvfb refuses to start on a number whose `/tmp/.X<n>-lock` exists even
    when the socket is gone, so the pool must read that file too."""
    (Path(pool.sock_dir).parent / ".X90-lock").touch()
    with pool.claim() as num:
        assert num == ":91"


def test_skipping_an_occupied_display_does_not_leak_its_lock(pool):
    """The socket is checked with the lock already held, so that two runs
    racing for one number cannot both read 'free'.  The cost of that ordering
    is a lock taken on a display we then refuse — and if it were not given
    back, one stray socket would retire a display number for good."""
    sock = Path(pool.sock_dir) / "X90"
    sock.touch()
    assert pool._try(90) is None
    sock.unlink()
    with pool.claim() as num:
        assert num == ":90"


# --------------------------------------------------------------------------- #
# release
# --------------------------------------------------------------------------- #


def test_a_claim_is_released_when_the_block_raises(pool):
    """A round trip that throws — WPS never showed a window, a dialog would
    not close — must not retire its display for the rest of the batch."""
    with pytest.raises(ZeroDivisionError):
        with pool.claim() as first:
            assert first == ":90"
            1 / 0
    with pool.claim() as again:
        assert again == ":90"


def test_a_claim_is_released_when_the_block_is_interrupted(pool):
    """Ctrl-C during a batch: the same rule, by the path people actually
    take."""
    with pytest.raises(KeyboardInterrupt):
        with pool.claim():
            raise KeyboardInterrupt
    with pool.claim() as again:
        assert again == ":90"


def test_a_claim_dies_with_the_process_that_held_it(tmp_path):
    """A killed run leaves its lock file behind.  If ownership were the file
    existing rather than a kernel lock on it, that display would be lost to
    every later run on the machine."""
    (tmp_path / "sock").mkdir()
    kw = dict(base=90, span=1, lock_dir=str(tmp_path / "locks"),
              sock_dir=str(tmp_path / "sock"))
    got = mp.Queue()
    proc = mp.Process(target=_hold_forever, args=(kw, got))
    proc.start()
    assert got.get(timeout=20) == ":90"
    proc.kill()
    proc.join(timeout=20)
    assert (tmp_path / "locks" / "display90.lock").exists()
    with w.DisplayPool(**kw).claim() as num:
        assert num == ":90"


def _hold_forever(kw, got):
    with w.DisplayPool(**kw).claim() as num:
        got.put(num)
        time.sleep(300)


# --------------------------------------------------------------------------- #
# exhaustion
# --------------------------------------------------------------------------- #


def test_an_exhausted_pool_says_so_instead_of_doubling_up(pool):
    """Handing out a busy display to keep a batch moving is exactly the
    failure this class exists to prevent, so running out is an error."""
    with pool.claim(), pool.claim(), pool.claim():
        with pytest.raises(w.NoFreeDisplay):
            with pool.claim():
                pass


def test_a_claim_can_wait_for_one_to_come_free(pool):
    """A batch with more decks than displays must queue, not fail: the
    bounded pool is the point of the workers flag."""
    import threading

    held = threading.Event()

    def hold_three():
        with pool.claim(), pool.claim(), pool.claim():
            held.set()
            time.sleep(2)

    t = threading.Thread(target=hold_three)
    t.start()
    held.wait(timeout=10)
    with pool.claim(wait=30, poll=0.2) as num:
        assert num in (":90", ":91", ":92")
    t.join(timeout=10)


# --------------------------------------------------------------------------- #
# what is still running on a display
# --------------------------------------------------------------------------- #


def test_a_leader_that_exits_leaving_a_child_still_counts_as_running():
    """WPS's launcher exits and the application carries on as its child.  If
    that read as "the process died", every round trip would abandon its deck
    seconds after opening it."""
    proc = subprocess.Popen(["sh", "-c", "sleep 30 & exit 0"],
                            start_new_session=True)
    proc.wait(timeout=10)
    try:
        assert w._alive(proc)
    finally:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)


def test_a_group_with_nothing_left_in_it_is_reported_dead():
    """A WPS killed by the OOM killer used to keep its display for the rest
    of the 240 s timeout, which is how one dead deck turned a 70 s batch into
    a four-minute one."""
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    assert w._alive(proc)
    w._killpg(proc)
    assert not w._alive(proc)


def test_killing_a_run_takes_its_children_with_it():
    """`proc.terminate()` reaches the launcher only.  A surviving WPS child
    goes on holding the display after the claim was handed back, and the next
    run's clicks land in its window."""
    proc = subprocess.Popen(["sh", "-c", "sleep 30 & echo $!; wait"],
                            stdout=subprocess.PIPE, text=True,
                            start_new_session=True)
    child = int(proc.stdout.readline())
    os.kill(child, 0)                      # alive before
    w._killpg(proc)
    time.sleep(0.5)
    with pytest.raises(OSError):
        os.kill(child, 0)


def test_claims_are_handed_out_in_order_so_a_small_batch_reuses_numbers(pool):
    """Displays are cheap but not free — each is an Xvfb worth of memory, and
    on this box memory is what runs out first."""
    with pool.claim() as a:
        assert a == ":90"
    with pool.claim() as b:
        assert b == ":90"


# --------------------------------------------------------------------------- #
# reading the window title
# --------------------------------------------------------------------------- #


def test_the_title_says_which_file_is_open_and_whether_it_is_modified():
    """Both facts replace a sleep.  The name is how "the document has loaded"
    is observed instead of counted out, and the star is the only check there
    has ever been that the notes-pane keystrokes reached the document."""
    assert w.parse_title("source.pptx - Presentation") == ("source.pptx", False)
    assert w.parse_title("source.pptx * - Presentation") == ("source.pptx", True)


def test_a_window_with_nothing_open_in_it_is_not_a_document():
    """WPS shows a bare `Presentation` window within a second of launch and
    only names the file when it has actually opened it.  Reading the first as
    the second is what made a 20 s sleep necessary in the first place."""
    assert w.parse_title("Presentation") is None
    assert w.parse_title("System Check") is None
    assert w.parse_title("") is None


def test_a_deck_whose_name_looks_like_a_title_is_still_read_correctly():
    """The corpus is other people's files and some of them are called
    strange things."""
    assert w.parse_title("a - Presentation.pptx - Presentation") == (
        "a - Presentation.pptx", False)
    assert w.parse_title("my deck (v2) * - Presentation") == (
        "my deck (v2)", True)


def test_the_document_window_is_picked_by_name(monkeypatch):
    """A dialog and a splash share the display with the deck; the round trip
    must read the dirty flag off the right one."""
    monkeypatch.setattr(w, "_titles", lambda env: [
        ("11", "System Check"), ("12", "Presentation"),
        ("13", "other.pptx - Presentation"),
        ("14", "source.pptx * - Presentation")])
    assert w._document({}, "source.pptx") == ("14", True)
    assert w._document({}, "other.pptx") == ("13", False)


# --------------------------------------------------------------------------- #
# waiting for the application to finish loading
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _group(cmd):
    proc = subprocess.Popen(cmd, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        yield proc
    finally:
        w._killpg(proc, grace=2)


def test_a_busy_process_group_is_not_idle():
    """`LOAD_WAIT` was 20 s because nobody knew when WPS had stopped working.
    It is observable, and a deck that takes longer than 20 s to load — which
    the corpus has — is exactly the case the constant got wrong."""
    with _group(["sh", "-c", "while :; do :; done"]) as proc:
        assert w._wait_idle(proc, time.time() + 2, window=0.5) is False


def test_a_process_group_that_stops_working_reports_idle():
    with _group(["sh", "-c", "sleep 30"]) as proc:
        assert w._wait_idle(proc, time.time() + 5, window=0.5) is True


def test_idle_is_measured_over_the_whole_group_not_the_leader():
    """WPS's launcher exits immediately and the application carries on as a
    child of it, so a reading taken from the pid we spawned would call a
    loading deck idle."""
    with _group(["sh", "-c", "while :; do :; done & wait"]) as proc:
        cpu, _ = w._group_load(proc.pid)
        time.sleep(0.5)
        again, _ = w._group_load(proc.pid)
        assert again > cpu


def test_waiting_for_idle_gives_up_rather_than_hanging():
    """Every wait in the module is bounded; a deck that never settles has to
    fail rather than hold its display for the rest of the batch."""
    with _group(["sh", "-c", "while :; do :; done"]) as proc:
        t0 = time.time()
        assert w._wait_idle(proc, time.time() + 1, window=0.5) is False
        assert time.time() - t0 < 3


def test_a_dead_process_is_not_waited_on():
    with _group(["sh", "-c", "sleep 30"]) as proc:
        w._killpg(proc, grace=2)
        assert w._wait_idle(proc, time.time() + 30, window=0.5) is False


# --------------------------------------------------------------------------- #
# waiting for the save to land
# --------------------------------------------------------------------------- #


def _pptx(path: Path, extra: bytes = b""):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        if extra:
            z.writestr("ppt/junk.bin", extra)


def test_an_untouched_file_is_never_reported_as_written(tmp_path):
    """The four seconds this replaces came after the mtime had already
    changed; the wait itself still has to know the difference."""
    f = tmp_path / "d.pptx"
    _pptx(f)
    assert w._wait_written(f, f.stat().st_mtime_ns, time.time() + 1) is False


def test_a_complete_package_is_reported_as_written(tmp_path):
    # a generous deadline on purpose: this asks what the wait decides, not how
    # fast it decides it, and the box it runs on has several WPS instances and
    # somebody else's test suite on it
    f = tmp_path / "d.pptx"
    _pptx(f)
    before = f.stat().st_mtime_ns
    time.sleep(0.01)
    _pptx(f, b"x" * 1000)
    assert w._wait_written(f, before, time.time() + 30) is True


def test_a_half_written_package_is_not_mistaken_for_a_saved_one(tmp_path):
    """A truncated zip compared against the original reads as a deck with
    every shape missing — a `fragile` verdict for a renderer that did
    nothing wrong.  Better to wait, and then to fail."""
    f = tmp_path / "d.pptx"
    _pptx(f)
    before = f.stat().st_mtime_ns
    data = f.read_bytes()
    f.write_bytes(data[:len(data) // 2])
    assert w._wait_written(f, before, time.time() + 1) is False


def test_a_package_still_growing_is_waited_out(tmp_path):
    """WPS writes a big deck over several seconds; returning at the first
    changed mtime hands the comparison a file that is still being written."""
    f = tmp_path / "d.pptx"
    _pptx(f)
    before = f.stat().st_mtime_ns

    def write_slowly():
        for n in range(4):
            time.sleep(0.1)
            f.write_bytes(b"P" * (100 * (n + 1)))       # not a zip yet
        _pptx(f, b"y" * 5000)

    t = threading.Thread(target=write_slowly)
    t.start()
    try:
        t0 = time.time()
        assert w._wait_written(f, before, time.time() + 30) is True
        assert time.time() - t0 > 0.3
    finally:
        t.join(timeout=30)


# --------------------------------------------------------------------------- #
# the dialogs
# --------------------------------------------------------------------------- #


class FakeX:
    """An xdotool that answers from a script instead of from an X server."""

    def __init__(self, dialogs=(), title="source.pptx - Presentation"):
        self.dialogs = list(dialogs)         # ids currently on screen
        self.title = title
        self.calls = []

    def __call__(self, env, *args):
        self.calls.append(args)
        if args[0] == "search":
            pattern = args[-1]
            if "System Check" in pattern:
                return "\n".join(self.dialogs)
            return "\n".join(self.dialogs + ["900"])
        if args[0] == "getwindowname":
            return "System Check" if args[1] in self.dialogs else self.title
        if args[0] == "windowclose":
            if args[1] in self.dialogs:
                self.dialogs.remove(args[1])
            return ""
        return ""

    def verbs(self):
        return [a[0] for a in self.calls]


def test_a_dialog_is_closed_and_never_killed(monkeypatch):
    """`xdotool windowkill` kills the X client, which is the whole
    application: the deck closes unsaved and the round trip reports nothing.
    `windowclose` sends WM_DELETE_WINDOW and the dialog goes quietly."""
    fake = FakeX(dialogs=["101"])
    monkeypatch.setattr(w, "_xdo", fake)
    assert w._dismiss_dialogs({}) == ["System Check"]
    assert "windowclose" in fake.verbs()
    assert "windowkill" not in fake.verbs()


def test_closing_a_dialog_returns_as_soon_as_it_is_gone(monkeypatch):
    """It used to sleep two seconds per round on the chance that it had
    not."""
    monkeypatch.setattr(w, "_xdo", FakeX(dialogs=["101"]))
    t0 = time.time()
    w._dismiss_dialogs({})
    assert time.time() - t0 < 1


def test_a_dialog_that_will_not_close_is_given_up_on(monkeypatch):
    """The caller checks afterwards whether anything modal is left and
    refuses to type into a deck it cannot see, so this has to end."""
    class Stuck(FakeX):
        def __call__(self, env, *args):
            if args[0] == "windowclose":
                return ""                     # ignores it
            return FakeX.__call__(self, env, *args)

    monkeypatch.setattr(w, "_xdo", Stuck(dialogs=["101"]))
    monkeypatch.setattr(w, "DIALOG_GONE", 0.3)
    t0 = time.time()
    w._dismiss_dialogs({}, rounds=2)
    assert time.time() - t0 < 3


def test_a_dialog_arriving_late_is_still_caught(monkeypatch):
    """The font check turns up seconds after the window, and it is modal:
    closing the first one and pressing on leaves the next to swallow every
    click that follows.  This is the one wait with nothing to observe — there
    is no signal for "no dialog is coming" — so it watches for a while."""
    fake = FakeX()
    monkeypatch.setattr(w, "_xdo", fake)

    def late():
        time.sleep(0.4)
        fake.dialogs.append("102")

    t = threading.Thread(target=late)
    t.start()
    try:
        seen = w._settle_dialogs({}, time.time() + 10, grace=1.5, quiet=0.3)
        assert seen == ["System Check"]
        assert fake.dialogs == []
    finally:
        t.join(timeout=5)


def test_watching_for_a_dialog_that_never_comes_is_bounded(monkeypatch):
    monkeypatch.setattr(w, "_xdo", FakeX())
    t0 = time.time()
    assert w._settle_dialogs({}, time.time() + 30, grace=0.4, quiet=0.2) == []
    assert time.time() - t0 < 2


# --------------------------------------------------------------------------- #
# starting the display
# --------------------------------------------------------------------------- #


def test_the_display_is_not_asked_about_before_it_exists(tmp_path, monkeypatch):
    """xdotool against a display that has not started yet does not fail:
    Xlib sits on the connect, and asking it first turned a round trip into a
    two-and-a-half-minute one.  The file the server drops is what has to be
    waited for, and it has to be waited for with `stat`.
    """
    sock_dir = tmp_path / ".X11-unix"
    sock_dir.mkdir()
    monkeypatch.setattr(w, "X_SOCKET_DIR", sock_dir)
    lock = tmp_path / ".X91-lock"
    asked = []

    class Subprocess:
        """Real, except that Xvfb is a `sleep` that takes its time."""

        def __getattr__(self, name):
            return getattr(subprocess, name)

        def Popen(self, argv, **kw):        # noqa: N802
            proc = subprocess.Popen(["sleep", "10"], **kw)
            threading.Timer(0.4, lock.touch).start()
            return proc

        def run(self, argv, **kw):
            asked.append(lock.exists())
            return subprocess.run(["true"], **kw)

    monkeypatch.setattr(w, "subprocess", Subprocess())
    screen = w._Screen(":91")
    try:
        screen.__enter__()
        assert asked and all(asked), "xdotool was asked before the server existed"
    finally:
        screen.close()


def test_an_xvfb_that_dies_on_startup_is_reported_not_waited_on(tmp_path,
                                                               monkeypatch):
    """A display number that Xvfb refuses is a claim to give back, not a
    thirty-second stare at a file that will never appear."""
    sock_dir = tmp_path / ".X11-unix"
    sock_dir.mkdir()
    monkeypatch.setattr(w, "X_SOCKET_DIR", sock_dir)

    class Subprocess:
        def __getattr__(self, name):
            return getattr(subprocess, name)

        def Popen(self, argv, **kw):        # noqa: N802
            return subprocess.Popen(["false"], **kw)

    monkeypatch.setattr(w, "subprocess", Subprocess())
    with pytest.raises(w.WpsUnavailable):
        w._Screen(":92").__enter__()
    assert not w._LIVE, "a screen that never started is still being tracked"


# --------------------------------------------------------------------------- #
# the round trip's one destructive step
# --------------------------------------------------------------------------- #


class FakeRun:
    """A WPS that opens a deck, dirties on a keystroke and saves on a click.

    Enough of one to ask the question that matters: how many times does the
    round trip type into the document, and how many characters does it delete
    afterwards.  Everything else about the sequence is timing; this is the
    part that can quietly change a file we are measuring.
    """

    def __init__(self, target_holder, dirty_on_type=True, late_dialog=False):
        self.holder = target_holder
        self.dirty_on_type = dirty_on_type
        self.late_dialog = late_dialog
        self.dialogs = []
        self.typed = self.backspaces = self.saves = 0
        self.dirty = False
        self.at = None

    def xdo(self, env, *args):
        if args[0] == "search":
            return "\n".join(self.dialogs) if "System Check" in args[-1] else "900"
        if args[0] == "getwindowname":
            return "System Check"
        if args[0] == "windowclose":
            if args[1] in self.dialogs:
                self.dialogs.remove(args[1])
            return ""
        if args[0] == "mousemove":
            self.at = (int(args[1]), int(args[2]))
        if args[0] == "type":
            self.typed += 1
            if self.late_dialog and self.typed == 1:
                self.dialogs.append("103")      # it ate the keystrokes
            elif self.dirty_on_type:
                self.dirty = True
        if args[0] == "key":
            self.backspaces += 1
        if args[0] == "click" and self.at == w.SAVE_XY:
            self.saves += 1
            if self.dirty:
                _pptx(self.holder["target"], b"saved" * 100)
                self.dirty = False
        return ""

    def titles(self, env):
        name = self.holder["target"].name
        star = " * " if self.dirty else " "
        return [("900", f"{name}{star}- Presentation")]


@pytest.fixture
def fake_wps(monkeypatch, tmp_path):
    """`_roundtrip_on` with the X server, the clock and WPS itself faked."""
    holder = {}

    class FakeScreen:
        def __init__(self, num):
            self.num, self.env = num, {"DISPLAY": num}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def spawn(self, argv):
            holder["target"] = Path(argv[-1])
            return subprocess.Popen(["sleep", "30"], start_new_session=True)

    monkeypatch.setattr(w, "preflight", lambda: [])
    monkeypatch.setattr(w, "_Screen", FakeScreen)
    monkeypatch.setattr(w, "_wait_idle", lambda *a, **k: True)
    monkeypatch.setattr(w, "_killpg", lambda *a, **k: None)
    monkeypatch.setattr(w, "DIRTY_WAIT", 0.4)
    monkeypatch.setattr(w, "DIALOG_GRACE", 0.2)
    monkeypatch.setattr(w, "DIALOG_QUIET", 0.1)
    monkeypatch.setattr(w, "UNDIRTIED_GRACE", 1.0)

    def run(**kw):
        wps = FakeRun(holder, **kw)
        monkeypatch.setattr(w, "_xdo", wps.xdo)
        monkeypatch.setattr(w, "_titles", wps.titles)
        src = tmp_path / "source.pptx"
        _pptx(src, b"original")
        return wps, src

    return run


def test_the_deck_is_typed_into_once_and_untyped_exactly(fake_wps):
    """Two characters in and two characters out.  The `+ 2` that used to be
    here as insurance ate two characters of every deck's speaker notes."""
    wps, src = fake_wps()
    out = w._roundtrip_on(str(src), ":90", timeout=30)
    assert wps.typed == 1
    assert wps.backspaces == len(w.DIRTY_MARK)
    assert wps.saves == 1
    assert zipfile.ZipFile(out).namelist()          # a complete package


def test_a_missing_dirty_marker_alone_never_earns_a_second_helping(fake_wps):
    """If the title does not show the star and nothing explains why, the
    document may well be holding the first `ZZ` — the marker could simply be
    absent in this build.  Typing a second one and deleting two characters
    would take two characters of somebody's notes with it."""
    wps, src = fake_wps(dirty_on_type=False)
    with pytest.raises(w.WpsUnavailable):
        w._roundtrip_on(str(src), ":90", timeout=30)
    assert wps.typed == 1
    assert wps.backspaces == len(w.DIRTY_MARK)


def test_a_dialog_that_ate_the_keystrokes_earns_one(fake_wps):
    """A modal dialog found afterwards is proof the document never saw them,
    which is what makes a second attempt safe — and the deck still has to
    come back saved."""
    wps, src = fake_wps(dirty_on_type=True, late_dialog=True)
    out = w._roundtrip_on(str(src), ":90", timeout=30)
    assert wps.typed == 2
    assert wps.backspaces == len(w.DIRTY_MARK)
    assert zipfile.ZipFile(out).namelist()


def test_a_deck_whose_wps_died_is_opened_again_before_it_is_given_up_on(
        monkeypatch, tmp_path):
    """WPS segfaults on startup once in twenty launches here and takes its
    deck with it.  Each attempt is a fresh copy under a fresh Xvfb, so the
    second one owes nothing to the first."""
    tries = []

    def flaky(pptx, display, timeout=240):
        tries.append(display)
        if len(tries) == 1:
            raise w.WpsUnavailable("WPS died before writing d.pptx")
        return Path(pptx)

    monkeypatch.setattr(w, "preflight", lambda: [])
    monkeypatch.setattr(w, "_roundtrip_on", flaky)
    assert w.roundtrip_wps("d.pptx", display=":90") == Path("d.pptx")
    assert tries == [":90", ":90"], "the retry should reuse the claim it holds"


def test_a_deck_that_fails_twice_is_reported_not_retried_forever(monkeypatch):
    """A bounded number of attempts, so a deck WPS cannot open fails the
    batch instead of holding a display until somebody notices."""
    tries = []

    def always(pptx, display, timeout=240):
        tries.append(display)
        raise w.WpsUnavailable("WPS died before writing d.pptx")

    monkeypatch.setattr(w, "preflight", lambda: [])
    monkeypatch.setattr(w, "_roundtrip_on", always)
    with pytest.raises(w.WpsUnavailable):
        w.roundtrip_wps("d.pptx", display=":90")
    assert len(tries) == 2


def test_a_missing_wps_is_not_retried(monkeypatch):
    """Nothing about the second attempt will be different."""
    tries = []

    def always(pptx, display, timeout=240):
        tries.append(display)
        raise w.WpsUnavailable("WPS Presentation not found")

    monkeypatch.setattr(w, "preflight", lambda: ["WPS Presentation not found"])
    monkeypatch.setattr(w, "_roundtrip_on", always)
    with pytest.raises(w.WpsUnavailable):
        w.roundtrip_wps("d.pptx", display=":90")
    assert len(tries) == 1


def test_a_deck_that_is_never_written_fails_instead_of_returning_the_original(
        fake_wps):
    """The copy handed back is compared against the deck it came from.  One
    that WPS never wrote is byte-identical to it, which reads as a perfect
    round trip: the strongest possible claim, from the weakest possible
    evidence."""
    wps, src = fake_wps(dirty_on_type=False)
    with pytest.raises(w.WpsUnavailable) as caught:
        w._roundtrip_on(str(src), ":90", timeout=30)
    assert "modified" in str(caught.value)
    assert wps.saves == 1
