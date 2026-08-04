"""The rules that decide whether a deck moves forward.

Everything here was found by running ten decks and reading the wreckage
afterwards, which is an expensive way to discover that `undetermined` counts
as a pass.  None of it is expensive to check.

    python3 -m pytest tests/ -q
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import pipeline as pl                                # noqa: E402


def _deck(tmp_path, **files) -> pl.Deck:
    d = pl.Deck(tmp_path / "deck0001")
    d.root.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        f = d.root / name.replace("__", "/")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    return d


# --------------------------------------------------------------------------- #
# staleness
# --------------------------------------------------------------------------- #


def test_a_changed_input_unmakes_the_tick(tmp_path):
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x"})
    d.mark("degraded", "ok")
    assert d.done("degraded")

    (d.root / "recipe.json").write_text('{"slides": {}}')
    assert not d.done("degraded")
    assert d.status_of("degraded") == "stale"
    assert "recipe.json" in d.stale("degraded")


def test_the_same_bytes_written_twice_is_not_a_change(tmp_path):
    """Fingerprints are content, not mtime: re-running a stage that produces
    an identical artefact must not invalidate everything below it."""
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x"})
    d.mark("degraded", "ok")
    (d.root / "recipe.json").write_text("{}")
    assert d.done("degraded")


def test_staleness_travels_down_the_chain(tmp_path):
    """`reconciled` reads none of the files a changed recipe touches, so
    without inheritance it keeps its tick while sitting on a stale degrade."""
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x",
                           "input.pptx": "y", "delta.json": "{}",
                           "assets__manifest.json": "{}"})
    d.mark("degraded", "ok")
    d.mark("reconciled", "ok")
    (d.root / "recipe.json").write_text("changed")
    assert d.status_of("reconciled") == "stale"
    assert "<degraded>" in d.stale("reconciled")


def test_a_stage_that_predates_fingerprints_is_left_alone(tmp_path):
    d = _deck(tmp_path, **{"recipe.json": "{}", "source.pptx": "x"})
    (d.root / "state.json").write_text(json.dumps({"degraded": {"status": "ok"}}))
    assert d.done("degraded")


# --------------------------------------------------------------------------- #
# what counts as "may continue"
# --------------------------------------------------------------------------- #


def test_materialise_may_continue_from_partial(tmp_path):
    """An asset the deck cannot supply is for `reconciled` to judge.  Driving
    the run off `done` alone re-ran those decks and parked them as
    `needs_human` for reaching the state they were designed to reach."""
    d = _deck(tmp_path, **{"proposal.json": "{}", "delta.json": "{}"})
    d.mark("materialised", "partial")
    assert not d.done("materialised")
    assert d.promoted("materialised")


def test_no_other_stage_may_continue_from_partial(tmp_path):
    d = _deck(tmp_path, **{"input.pptx": "y", "delta.json": "{}",
                           "assets__manifest.json": "{}"})
    d.mark("reconciled", "partial")
    assert not d.promoted("reconciled")


def test_undetermined_is_not_a_pass():
    """It means the probe could not decide.  An undecided gate is not a
    passed gate — this one shipped a task before it was noticed."""
    assert "solvable" in pl.PASSING_VERDICTS
    assert "ready" in pl.PASSING_VERDICTS
    for v in ("undetermined", "ambiguous", "leaked", "overdetermined",
              "needs_rework"):
        assert v not in pl.PASSING_VERDICTS


# --------------------------------------------------------------------------- #
# the information barrier
# --------------------------------------------------------------------------- #


def _log(deck, *calls) -> Path:
    """A stream-json log holding the given tool calls."""
    f = deck.root / "solvable.jsonl"
    with open(f, "w") as fh:
        for name, inp in calls:
            fh.write(json.dumps({"message": {"content": [
                {"type": "tool_use", "name": name, "input": inp}]}}) + "\n")
    return f


def test_reading_the_answer_key_is_caught(tmp_path):
    d = _deck(tmp_path)
    f = _log(d, ("Read", {"file_path": str(d.root / "source.pptx")}))
    assert pl.barrier_breaches(d, f)


def test_grepping_our_own_source_is_not_a_peek(tmp_path):
    """`grep -rn "source.pptx" pptxgym` reads the pipeline's code.  The old
    substring scan voided two finished probes for exactly this."""
    d = _deck(tmp_path)
    f = _log(d, ("Bash", {"command": 'grep -rn "source.pptx" --include=*.py pptxgym'}))
    assert not pl.barrier_breaches(d, f)


def test_naming_the_files_in_a_report_is_not_a_peek(tmp_path):
    """A probe that writes "source.pptx and delta.json were never opened" was
    failed for saying so."""
    d = _deck(tmp_path)
    f = _log(d, ("Write", {"file_path": str(d.root / "solvability.json"),
                           "content": "source.pptx, delta.json, recipe.json "
                                      "and proposal.json were never opened"}))
    assert not pl.barrier_breaches(d, f)


def test_the_bundle_and_its_own_report_are_allowed(tmp_path):
    """Every probe re-reads the report it just wrote to check the JSON
    parses; calling that a peek voided four more runs."""
    d = _deck(tmp_path)
    f = _log(d, ("Read", {"file_path": str(d.root / "bundle" / "input.pptx")}),
             ("Bash", {"command": f"cat {d.root / 'solvability.json'}"}))
    assert not pl.barrier_breaches(d, f)


def test_climbing_out_of_the_bundle_is_caught(tmp_path):
    d = _deck(tmp_path)
    f = _log(d, ("Bash", {"command": "unzip -l ../source.pptx"}))
    assert pl.barrier_breaches(d, f)
