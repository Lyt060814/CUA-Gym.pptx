"""Who owns which X display, and what happens when a run dies holding one.

The GUI half of `wps_roundtrip` cannot be unit-tested — it needs Xvfb, WPS and
ninety seconds.  The allocator can be, and it is the half that fails silently:
two WPS instances on one display click each other's dialogs and save into each
other's documents, and the report still says `stable`.  Every test here is a
way that has been observed to happen, or a way a claim can leak so that the
next batch queues forever behind nothing.

    python3 -m pytest tests/ -q
"""

import contextlib
import multiprocessing as mp
import os
import signal
import subprocess
import sys
import time
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
