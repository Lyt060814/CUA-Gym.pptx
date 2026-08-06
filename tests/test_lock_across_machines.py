"""A lock has to mean something after `work/` has moved to another machine.

Found by the supervisor on its first live poll, one minute after it was
written: two decks in run 11 sitting at

    BUSY — deck0006 is locked by pid 10663 running 'hardened' since 11:54:12

Pid 10663 was **run 10's** process, on a container destroyed an hour earlier.
The lock came back inside the resume tarball, and run 11's own process was also
pid 10663 — same image, same startup sequence, same pid every time — so
`os.kill(10663, 0)` succeeded and the deck was locked by itself. Deterministic,
not a coincidence, and it would have hit every resumed run from now on.

    python3 -m pytest tests/test_lock_across_machines.py -q
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import pipeline as pl                              # noqa: E402


def _deck(tmp_path) -> pl.Deck:
    root = tmp_path / "deck0006"
    root.mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"name": "d.pptx", "slides": 3}))
    return pl.Deck(root)


def _write_lock(deck, **fields):
    (deck.root / ".lock").write_text(json.dumps(
        {"pid": os.getpid(), "stage": "hardened",
         "at": "2026-08-06T11:54:12", **fields}))


def test_a_lock_from_another_boot_is_stale_even_if_the_pid_is_alive(tmp_path):
    """The bug, exactly. Our own live pid, a different machine."""
    deck = _deck(tmp_path)
    _write_lock(deck, boot="00000000-0000-0000-0000-000000000000",
                started="1")
    with pl.lock(deck, "hardened"):
        pass                     # must not raise


def test_a_lock_from_a_reused_pid_on_this_machine_is_stale(tmp_path):
    """Same boot, same pid, different process. `started` is what tells them
    apart — a pid is a number the kernel hands out again."""
    deck = _deck(tmp_path)
    ident = pl.lock._identity()
    _write_lock(deck, boot=ident.get("boot"), started="1")
    with pl.lock(deck, "hardened"):
        pass


def test_a_lock_this_very_process_holds_is_still_honoured(tmp_path):
    """The negative control. If the fix made every lock stale, the whole
    mechanism would be off and every test above would pass for the wrong
    reason — two stages could then interleave on one deck, which is the
    corruption the lock exists to prevent."""
    deck = _deck(tmp_path)
    _write_lock(deck, **{k: v for k, v in pl.lock._identity().items()
                         if k != "pid"})
    with pytest.raises(pl.DeckBusy):
        with pl.lock(deck, "recipe"):
            pass


def test_a_lock_written_before_this_existed_is_judged_the_old_way(tmp_path):
    """A lock with neither field is not declared stale on sight — the same
    reading `stale()` takes of a record made before `<code>` fingerprints."""
    deck = _deck(tmp_path)
    _write_lock(deck)                      # pid only, as the old code wrote it
    with pytest.raises(pl.DeckBusy):
        with pl.lock(deck, "recipe"):
            pass


def test_a_dead_pid_is_still_stale(tmp_path):
    deck = _deck(tmp_path)
    _write_lock(deck, pid=999_999)
    with pl.lock(deck, "hardened"):
        pass


def test_the_lock_records_enough_to_be_judged_next_time(tmp_path):
    """Whatever this platform can tell us has to actually be written down;
    a lock that records only a pid recreates the bug for the next reader."""
    deck = _deck(tmp_path)
    with pl.lock(deck, "recipe"):
        held = json.loads((deck.root / ".lock").read_text())
    assert held["pid"] == os.getpid()
    if Path("/proc/self/stat").exists():           # Linux, which is where it ran
        assert held.get("started"), "the pid's start time is the discriminator"


def test_releasing_removes_it(tmp_path):
    deck = _deck(tmp_path)
    with pl.lock(deck, "recipe"):
        assert (deck.root / ".lock").exists()
    assert not (deck.root / ".lock").exists()
