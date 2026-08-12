"""The code digest is provenance now, not freshness.

(Historical header below: this file was written when code drift *did* unmake
ticks.  That default sent finished decks back through agent-priced stages on
every instrument fix and was reversed on 2026-08-07 — the digest is still
recorded with every verdict, `stale_by_code` still names the stages a fix
reached, but re-running them is a person's `--force` decision.)

The defect this is about is measurable rather than theoretical.
`degrade_exec.strip_thumbnail` — which deletes `docProps/thumbnail.jpeg`, a
render of the *undamaged* slide 1 that Office leaves in the package — landed at
08:52 on 2026-08-05.  `work/deck0001/input.pptx` was built at 06:57, five
stages earlier and an hour before, and nothing in the pipeline could tell that
the file was made by code that no longer exists: `STAGE_INPUTS` fingerprints
`recipe.json` and `source.pptx`, neither of which moved.  The deck kept
`degraded: ok`, shipped the thumbnail, and the solvability probe correctly
called it a leak — by which time three attempts had been spent on unrelated
complaints and the deck was parked as though it were a bad deck.

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

from pptxgym.core import pipeline as pl                                # noqa: E402


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


def test_nested_modules_are_keyed_by_their_full_package_path(monkeypatch,
                                                              tmp_path):
    """Two subpackages may legitimately use the same implementation name."""
    left = tmp_path / "left" / "codec.py"
    right = tmp_path / "right" / "codec.py"
    left.parent.mkdir()
    right.parent.mkdir()
    left.write_text("VALUE = 'left'\n")
    right.write_text("VALUE = 'right'\n")
    monkeypatch.setattr(pl, "_module_sources", lambda: {
        "left.codec": left,
        "right.codec": right,
    })
    assert set(pl._module_sources()) == {"left.codec", "right.codec"}


def test_orchestration_is_reached_but_not_expanded():
    """`agent` is a harness: it routes an agent stage, it does not judge it.
    Expanding it drags in all twenty modules (it imports `pipeline`, which
    imports the world), so an edit to `emit.py` would re-run `propose` on every
    deck at agent prices."""
    assert pl.stage_modules("proposed") == ("agent", "profiles", "prompts")
    assert pl.stage_modules("reconciled") == ("agent", "prompts")
    assert pl.stage_modules("solvable") == ("agent", "prompts", "solvability")
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
    target = pl._module_sources()["office.pkg_check"]

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


def test_a_producer_fix_leaves_the_tick_standing(tmp_path):
    """The reversal of this file's original doctrine, on purpose.  Binding
    freshness to the code digest sent finished decks back through
    agent-priced stages after every instrument fix — deck0001 spent thirty
    minutes re-establishing a verdict nobody doubted.  The digest is still
    *recorded* (provenance: which commit produced this), but code drift no
    longer unmakes a tick; a fix that truly voids old decks is applied by a
    person with `--force`.  `stale_by_code` still names the reached stages
    for that person to read."""
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x"})
    d.mark("degraded", "ok")
    assert d.done("degraded")

    pl._CODE_DIGESTS["degraded"] = "a-fixed-executor"
    assert d.done("degraded")
    assert d.status_of("degraded") == "ok"
    assert pl.CODE_KEY not in d.stale("degraded")
    assert pl.stale_by_code(d) == ["degraded"]      # named, not enforced


def test_a_fix_to_someone_elses_stage_leaves_this_tick_alone(tmp_path):
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x"})
    d.mark("degraded", "ok")
    pl._CODE_DIGESTS["scored"] = "a-fixed-comparator"
    assert d.done("degraded")


def test_a_producer_fix_does_not_cascade_down_the_chain(tmp_path):
    """The cascade was the expensive half of the old rule: one instrument fix
    marked a finished deck stale at `degraded` and the inheritance walk took
    every downstream tick with it — the full agent-priced refresh chain.  With
    code drift out of freshness there is nothing to inherit; the chain still
    cascades for *artefact* changes, which `test_staleness_is_inherited`-style
    cases elsewhere pin."""
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x",
                           "input.pptx": "y", "delta.json": "{}",
                           "assets__manifest.json": "{}"})
    d.mark("degraded", "ok")
    d.mark("reconciled", "ok")
    assert d.done("reconciled")

    pl._CODE_DIGESTS["degraded"] = "a-fixed-executor"
    assert d.status_of("reconciled") == "ok"
    assert "<degraded>" not in d.stale("reconciled")


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
# what a fix reaches, as an advisory list
#
# `retire_park_after_code_fix` used to live here: a deck parked by the repair
# loop for a defect that turned out to be ours had its park withdrawn and its
# repair budget refunded.  Both the loop and the budget are gone — an
# orchestrator agent owns a deck now — and with them the only caller.  What
# remains is the list itself, which is what a person reads before deciding to
# re-run anything.
# --------------------------------------------------------------------------- #


def test_a_fix_is_named_even_where_it_is_not_enforced(tmp_path):
    """A deck that stopped against code we have since fixed is stale, not bad.
    Nothing acts on that automatically any more; being able to say it is the
    point."""
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x",
                           "input.pptx": "y", "delta.json": "{}",
                           "assets__manifest.json": "{}"})
    d.mark("degraded", "ok")
    d.mark("reconciled", "needs_human", rejected_by="solvability.json:leaked")
    assert pl.stale_by_code(d) == []

    pl._CODE_DIGESTS["degraded"] = "a-fixed-executor"
    assert pl.stale_by_code(d) == ["degraded"]
    assert d.state()["reconciled"]["status"] == "needs_human", \
        "naming it is not withdrawing it"


def test_stale_by_code_names_only_the_stages_the_fix_reached(tmp_path):
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x",
                           "input.pptx": "y", "delta.json": "{}",
                           "assets__manifest.json": "{}"})
    d.mark("degraded", "ok")
    d.mark("reconciled", "ok")
    assert pl.stale_by_code(d) == []
    pl._CODE_DIGESTS["degraded"] = "a-fixed-executor"
    assert pl.stale_by_code(d) == ["degraded"]
