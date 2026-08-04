"""How a batch is scheduled, and what happens when one deck goes wrong.

Every rule here exists because of something a ten-deck run did, and every one
of them gets sharper at a hundred.

    python3 -m pytest tests/ -q
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import cli                                          # noqa: E402
from pptxgym import pipeline as pl                               # noqa: E402


def _args(**kw):
    base = dict(work="work", deck=None, workers=1, cpu_workers=None,
                force=False, dpi=110, model=None, timeout=30)
    base.update(kw)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------- #
# two currencies, two pools
# --------------------------------------------------------------------------- #


def test_agent_and_cpu_stages_draw_on_different_pools():
    """A single limit tuned for the API starves the renderers, and one tuned
    for the renderers oversubscribes the API.  We hit both."""
    pools = cli.Pools(agent=2, cpu=7)
    for stage in ("proposed", "recipe", "reconciled", "solvable", "repair"):
        assert pools.for_stage(stage) is pools.agent
    for stage in ("inspected", "degraded", "materialised"):
        assert pools.for_stage(stage) is pools.cpu


def test_a_cpu_stage_run_alone_is_not_capped_by_the_agent_limit():
    """`pptxgym degrade --workers 2` has no reason to run two at a time just
    because two is all the API will take."""
    a = _args(workers=2, cpu_workers=6)
    assert cli._workers_for(a, "degraded") == 6
    assert cli._workers_for(a, "proposed") == 2


def test_cpu_default_is_derived_not_guessed():
    a = _args(workers=3, cpu_workers=None)
    assert cli._workers_for(a, "degraded") == cli._default_cpu_workers()
    assert cli._default_cpu_workers() >= 2


def test_a_deck_holds_no_slot_between_stages(tmp_path):
    """The old scheduler took one slot for a deck's whole journey, so a deck
    three repairs deep occupied capacity it was not using.  A deck with
    nothing left to do must acquire nothing at all."""
    deck = pl.Deck(tmp_path / "deck0001")
    deck.root.mkdir(parents=True)
    (deck.root / "meta.json").write_text(json.dumps({"slides": 1}))
    for s in pl.STAGES:
        deck.mark(s, "ok")

    pools_seen = {}

    async def main():
        pools = cli.Pools(agent=1, cpu=1)
        await cli._run_one(deck, _args(until="solvable"), pools)
        pools_seen["agent"] = pools.agent._value
        pools_seen["cpu"] = pools.cpu._value

    asyncio.run(main())
    assert pools_seen == {"agent": 1, "cpu": 1}      # everything given back


# --------------------------------------------------------------------------- #
# one bad deck is one bad deck
# --------------------------------------------------------------------------- #


def test_an_unexpected_exception_does_not_take_the_batch_with_it(tmp_path):
    """`StageError` was caught; anything else propagated out of the gather and
    cancelled every other deck — throwing away hours of finished work because
    one file was corrupt."""
    deck = pl.Deck(tmp_path / "deck0001")
    deck.root.mkdir(parents=True)

    def explode(_deck, _args):
        raise ValueError("this package is not a zip file")

    line = cli._guarded(explode, deck, _args())
    assert "CRASHED" in line and "not a zip file" in line
    assert (deck.root / "crash-stage.log").exists()
    assert deck.state()["stage"]["status"] == "crashed"


def test_a_crash_leaves_the_traceback_where_someone_can_read_it(tmp_path):
    """A parked deck with a clean record loses the only account of why it
    stopped — that mistake has already been made once here."""
    deck = pl.Deck(tmp_path / "deck0001")
    deck.root.mkdir(parents=True)

    def explode(_deck, _args):
        raise KeyError("sldNum")

    cli._guarded(explode, deck, _args())
    tb = (deck.root / "crash-stage.log").read_text()
    assert "Traceback" in tb and "KeyError" in tb


# --------------------------------------------------------------------------- #
# a batch you cannot read is a batch you cannot steer
# --------------------------------------------------------------------------- #


def test_a_live_lock_is_reported_as_running(tmp_path):
    deck = pl.Deck(tmp_path / "deck0001")
    deck.root.mkdir(parents=True)
    with pl.lock(deck, "solvable"):
        live = cli._inflight([deck])
    assert live and live[0][0] == "deck0001" and live[0][1] == "solvable"
    assert live[0][4] is True                       # this process is alive


def test_a_lock_held_by_a_dead_process_is_flagged_not_believed(tmp_path):
    deck = pl.Deck(tmp_path / "deck0001")
    deck.root.mkdir(parents=True)
    (deck.root / ".lock").write_text(json.dumps(
        {"pid": 2 ** 22, "stage": "recipe", "at": "2026-08-04T00:00:00"}))
    live = cli._inflight([deck])
    assert live[0][4] is False


def test_disk_is_counted_rather_than_estimated(tmp_path):
    d = tmp_path / "deck0001"
    d.mkdir()
    (d / "source.pptx").write_bytes(b"x" * 2048)
    total, n = cli._disk(tmp_path)
    assert (total, n) == (2048, 1)
    assert cli._human(2048) == "2.0KB"
