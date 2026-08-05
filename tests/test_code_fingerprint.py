"""A stage is stale when the code that produced it moved, not only its inputs.

The defect this is about is measurable rather than theoretical.
`degrade_exec.strip_thumbnail` — which deletes `docProps/thumbnail.jpeg`, a
render of the *undamaged* slide 1 that Office leaves in the package — landed at
08:52 on 2026-08-05.  `work/deck0001/input.pptx` was built at 06:57, five
stages earlier and an hour before, and nothing in the pipeline could tell that
the file was made by code that no longer exists: `STAGE_INPUTS` fingerprints
`recipe.json` and `source.pptx`, neither of which moved.  The deck kept
`degraded: ok`, shipped the thumbnail, and the solvability probe correctly
called it a leak — by which time the repair budget had been spent on three
unrelated complaints and the deck was parked as though it were a bad deck.

Every producer fix has this shape.  The fix reaches decks that have not run
yet, and silently misses every deck already past the stage.

    python3 -m pytest tests/test_code_fingerprint.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import pipeline as pl                                # noqa: E402


def _deck(tmp_path, name="deck0001", **files) -> pl.Deck:
    d = pl.Deck(tmp_path / name)
    d.root.mkdir(parents=True, exist_ok=True)
    for key, body in files.items():
        f = d.root / key.replace("__", "/")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    return d


@pytest.fixture(autouse=True)
def _clean_caches():
    """The digest is memoised per process; a test that fakes one must not leak
    it into the next."""
    pl._CODE_DIGESTS.clear()
    pl._CODE_CLOSURE.clear()
    yield
    pl._CODE_DIGESTS.clear()
    pl._CODE_CLOSURE.clear()


# --------------------------------------------------------------------------- #
# which modules count as a stage's code
# --------------------------------------------------------------------------- #


def test_a_stage_owns_the_modules_that_actually_do_its_work():
    assert "degrade_exec" in pl.stage_modules("degraded")
    assert "pkg_check" in pl.stage_modules("degraded")
    assert "assets" in pl.stage_modules("materialised")
    assert "comparators" in pl.stage_modules("scored")


def test_the_closure_is_transitive_so_a_helper_fix_reaches_its_stage():
    """`degrade_exec` does the damage through `smartart` and `charts`; a fix in
    either changes what `degraded` produces, and a hand-written list is how
    that gets forgotten."""
    mods = pl.stage_modules("degraded")
    assert {"smartart", "charts", "census"} <= set(mods)


def test_no_stage_is_fingerprinted_against_the_whole_repo():
    """The failure mode on the other side: a docs commit, or a change to a
    module the stage never executes, knocking the corpus back."""
    assert "attacks" not in pl.stage_modules("degraded")
    assert "emit" not in pl.stage_modules("materialised")
    assert "wps_roundtrip" not in pl.stage_modules("scored")


def test_the_command_line_is_never_part_of_a_stages_code():
    """How a stage is *asked for* is not how it is done, and `cli` imports
    everything — including it would make every stage stale on every commit."""
    for stage in pl.STAGES:
        assert "cli" not in pl.stage_modules(stage)


def test_orchestration_is_reached_but_not_expanded():
    """`agent` is a harness: it routes an agent stage, it does not judge it.
    Expanding it drags in all twenty modules (it imports `pipeline`, which
    imports the world), so an edit to `emit.py` would re-run `propose` on every
    deck at agent prices."""
    assert pl.stage_modules("proposed") == ("agent",)
    assert pl.stage_modules("reconciled") == ("agent",)
    assert "comparators" not in pl.stage_modules("solvable")


def test_ingest_has_no_code_fingerprint():
    """Registering a file is not a producer stage; there is nothing under it to
    invalidate, and `None` says so rather than a hash of nothing."""
    assert pl.stage_modules("ingested") == ()
    assert pl.code_digest("ingested") is None


def test_a_module_edit_moves_exactly_the_stages_that_run_it(monkeypatch):
    """The real question, asked without editing the repo: change one module's
    bytes and see which digests follow."""
    before = {s: pl.code_digest(s) for s in pl.STAGES}
    real = pl._digest
    target = Path(pl.__file__).parent / "pkg_check.py"

    def fake(path):
        return "0" * 16 if Path(path) == target else real(path)

    monkeypatch.setattr(pl, "_digest", fake)
    pl._CODE_DIGESTS.clear()
    after = {s: pl.code_digest(s) for s in pl.STAGES}

    assert after["degraded"] != before["degraded"]      # pkg_check is its gate
    for stage in ("inspected", "materialised", "scored", "hardened"):
        assert after[stage] == before[stage], stage


# --------------------------------------------------------------------------- #
# what the fingerprint does to a tick
# --------------------------------------------------------------------------- #


def test_the_code_digest_is_recorded_beside_the_input_digests(tmp_path):
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x"})
    d.mark("degraded", "ok")
    recorded = d.state()["degraded"]["_in"]
    assert recorded[pl.CODE_KEY] == pl.code_digest("degraded")
    assert recorded["recipe.json"]                      # and the old keys stay


def test_a_producer_fix_unmakes_the_tick_it_was_produced_under(tmp_path):
    """deck0001 in miniature: the inputs are byte-identical and the stage is
    still out of date."""
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x"})
    d.mark("degraded", "ok")
    assert d.done("degraded")

    pl._CODE_DIGESTS["degraded"] = "a-fixed-executor"
    assert not d.done("degraded")
    assert d.status_of("degraded") == "stale"
    assert pl.CODE_KEY in d.stale("degraded")


def test_a_fix_to_someone_elses_stage_leaves_this_tick_alone(tmp_path):
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x"})
    d.mark("degraded", "ok")
    pl._CODE_DIGESTS["scored"] = "a-fixed-comparator"
    assert d.done("degraded")


def test_a_producer_fix_travels_down_the_chain(tmp_path):
    """A stage below the fix reads none of the files the fix will move — until
    the stage above is re-run — so without inheritance it keeps its tick while
    standing on a build nobody would defend."""
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x",
                           "input.pptx": "y", "delta.json": "{}",
                           "assets__manifest.json": "{}"})
    d.mark("degraded", "ok")
    d.mark("reconciled", "ok")
    assert d.done("reconciled")

    pl._CODE_DIGESTS["degraded"] = "a-fixed-executor"
    assert d.status_of("reconciled") == "stale"
    assert "<degraded>" in d.stale("reconciled")


def test_a_stage_recorded_before_code_fingerprints_keeps_its_tick(tmp_path):
    """Otherwise shipping this marks every stage of every deck in every work
    directory stale at once — four agent stages per deck of re-run, to
    establish a baseline nobody has evidence for.  Same reading
    `_model_changed` takes of a missing `model_asked`."""
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x"})
    d.mark("degraded", "ok")
    st = d.state()
    st["degraded"]["_in"].pop(pl.CODE_KEY)              # as an old record looks
    (d.root / "state.json").write_text(json.dumps(st))

    assert d.done("degraded")
    pl._CODE_DIGESTS["degraded"] = "a-fixed-executor"
    assert d.done("degraded"), "an unfingerprinted record must not go stale"


def test_a_rerun_under_the_same_code_is_not_a_change(tmp_path):
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x"})
    d.mark("degraded", "ok")
    d.mark("degraded", "ok")
    assert d.done("degraded")


# --------------------------------------------------------------------------- #
# the repair budget
# --------------------------------------------------------------------------- #


def _parked_deck(tmp_path, attempts=3) -> pl.Deck:
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x",
                           "input.pptx": "y", "delta.json": "{}",
                           "assets__manifest.json": "{}"})
    d.mark("degraded", "ok")
    for n in range(1, attempts + 1):
        (d.root / f"repair-{n:02d}.jsonl").write_text(
            json.dumps({"type": "result", "subtype": "success"}) + "\n")
    d.mark("reconciled", "needs_human", attempts=attempts,
           rejected_by="solvability.json:leaked")
    return d


def test_a_deck_parked_against_code_we_have_since_fixed_is_unparked(tmp_path):
    d = _parked_deck(tmp_path)
    assert pl.repairs_done(d) == 3 >= pl.MAX_REPAIRS
    pl._CODE_DIGESTS["degraded"] = "a-fixed-executor"

    why = pl.retire_park_after_code_fix(d)
    assert why and "degraded" in why
    assert d.state().get("reconciled") is None, "the park is withdrawn"
    assert pl.repairs_done(d) == 0, "and the budget it spent comes back"


def test_the_verdict_that_ordered_the_repairs_retires_with_them(tmp_path):
    """Otherwise the refund is a trap.  `_rework_of` reads the gate files off
    disk, so a stale `solvability.json` beside an unparked deck is an open
    work order against a bundle that no longer exists — deck0001 and deck0009
    were unparked without this and both went straight back round the loop."""
    d = _parked_deck(tmp_path)
    (d.root / "solvability.json").write_text(json.dumps(
        {"verdict": "leaked",
         "rework": [{"stage": "materialise", "what": "strip the thumbnail"}]}))
    pl._CODE_DIGESTS["degraded"] = "a-fixed-executor"

    pl.retire_park_after_code_fix(d)
    assert not (d.root / "solvability.json").exists()
    assert (d.root / "attempts" / "repairs-01" / "solvability.json").exists()


def test_task_json_is_never_retired(tmp_path):
    """The verdict belongs to reconcile and the instruction lives in the same
    file; taking it away would delete the task to save the deck."""
    d = _parked_deck(tmp_path)
    (d.root / "task.json").write_text('{"instruction": "put it back"}')
    pl._CODE_DIGESTS["degraded"] = "a-fixed-executor"
    pl.retire_park_after_code_fix(d)
    assert (d.root / "task.json").exists()


def test_the_refunded_attempts_are_archived_not_deleted(tmp_path):
    """'Fixed it' and 'laundered the verdict' have to stay tellable apart."""
    d = _parked_deck(tmp_path)
    pl._CODE_DIGESTS["degraded"] = "a-fixed-executor"
    pl.retire_park_after_code_fix(d)
    kept = sorted(p.name for p in (d.root / "attempts" / "repairs-01").iterdir())
    assert kept == ["repair-01.jsonl", "repair-02.jsonl", "repair-03.jsonl"]


def test_a_park_nobody_invalidated_stays_parked(tmp_path):
    """The whole guard: without a code move this must do nothing at all, or it
    is a machine for handing out extra repair attempts."""
    d = _parked_deck(tmp_path)
    assert pl.retire_park_after_code_fix(d) is None
    assert d.state()["reconciled"]["status"] == "needs_human"
    assert pl.repairs_done(d) == 3


def test_a_fix_below_the_park_does_not_refund_it(tmp_path):
    """`packaged` is downstream of a deck parked at `reconciled`; fixing it
    changes nothing under the park, so the park still holds."""
    d = _parked_deck(tmp_path)
    pl._CODE_DIGESTS["packaged"] = "a-fixed-emitter"
    d.mark("packaged", "ok")
    pl._CODE_DIGESTS["packaged"] = "a-differently-fixed-emitter"
    assert "packaged" in pl.stale_by_code(d)
    assert pl.retire_park_after_code_fix(d) is None


def test_rebuilding_after_a_code_fix_spends_no_repair_budget(tmp_path):
    """The property the whole mechanism turns on.  A repair is for a deck that
    got something wrong; a rebuild is for one we invalidated ourselves, and it
    must not be charged for the privilege."""
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x"})
    d.mark("degraded", "ok")
    before = pl.repairs_done(d)

    pl._CODE_DIGESTS["degraded"] = "a-fixed-executor"
    assert d.status_of("degraded") == "stale"
    d.mark("degraded", "ok")                            # the stage simply re-ran
    assert d.done("degraded")
    assert pl.repairs_done(d) == before == 0


def test_stale_by_code_names_only_the_stages_the_fix_reached(tmp_path):
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x",
                           "input.pptx": "y", "delta.json": "{}",
                           "assets__manifest.json": "{}"})
    d.mark("degraded", "ok")
    d.mark("reconciled", "ok")
    assert pl.stale_by_code(d) == []
    pl._CODE_DIGESTS["degraded"] = "a-fixed-executor"
    assert pl.stale_by_code(d) == ["degraded"]
