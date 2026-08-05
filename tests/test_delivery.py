"""What is handed over, and what is not allowed to follow it out of the door.

Three things a batch of ten decks got away with, none of which any gate saw:

  * `bundle()` copied the `assets/` **directory**, so deck0006 delivered
    thirteen files against a manifest recording seven — five byte-identical
    duplicates of the blot strips under the names an earlier recipe gave them,
    plus a masked render the current recipe had replaced.  The instruction says
    "the strip images are in the assets folder"; the solver counted eight.
  * The duplicates were there because `materialise` never clears `assets/`
    before producing.  Filtering at delivery hides that; clearing at production
    removes it.
  * `revert_tool_changes` — the rule that a repair may not edit the pipeline's
    own code — was blind to a file some *other* agent had already modified, and
    deck0001's second repair edited `pptxgym/assets.py` straight through it.

    python3 -m pytest tests/test_delivery.py -q
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import assets as A                                   # noqa: E402
from pptxgym import pipeline as pl                                # noqa: E402


def _deck(tmp_path, manifest, files, task=None) -> pl.Deck:
    """A deck at the point `bundle()` runs: a broken file, some assets and a
    record of what the solver is promised."""
    d = pl.Deck(tmp_path / "deck0001")
    (d.root / "assets").mkdir(parents=True, exist_ok=True)
    d.input_pptx.write_text("the broken deck")
    if manifest is not None:
        (d.root / "assets" / "manifest.json").write_text(
            manifest if isinstance(manifest, str)
            else json.dumps(manifest))
    for rel, body in files.items():
        f = d.root / "assets" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    (d.root / "task.json").write_text(json.dumps({
        "name": "t", "instruction": "put it back", "difficulty": "medium",
        "est_steps": 200, "instruction_changed": False, "notes": "",
        "verdict": "ready", "assets": task or []}))
    return d


def _delivered(deck) -> set[str]:
    a = deck.root / "bundle" / "assets"
    return {str(p.relative_to(a)) for p in a.rglob("*") if p.is_file()}


# --------------------------------------------------------------------------- #
# the delivery is the manifest's `produced` list, not the directory
# --------------------------------------------------------------------------- #


def test_a_file_nobody_recorded_producing_is_not_delivered(tmp_path):
    """deck0006, in miniature.  Two names for one picture is not a redundant
    copy — it is the deck's own history, and an instruction that names its
    files by name now names one of two identical things."""
    d = _deck(tmp_path,
              {"produced": [{"kind": "image", "file": "p03-strip-4.png"}]},
              {"p03-strip-4.png": "the strip",
               "p03--4.png": "the strip",              # same bytes, older name
               "reference-p03-masked.png": "a superseded render"})
    pl.bundle(d)

    assert _delivered(d) == {"p03-strip-4.png"}
    omitted = json.loads((d.root / "bundle.json").read_text())["omitted"]
    assert [o["file"] for o in omitted] == ["p03--4.png",
                                            "reference-p03-masked.png"]
    assert all("superseded" in o["why"] for o in omitted), (
        "a file held back is a delivery decision; recording only that it is "
        "absent leaves the reader to diff two directories")


def test_a_deck_with_no_produced_list_is_delivered_whole(tmp_path):
    """Decks built before the manifest recorded what it produced have nothing
    to gate on.  Reading that as "nothing was produced" would empty their
    bundles and park them for a bookkeeping gap — a worse failure than the one
    being fixed here."""
    for manifest in ({"unmet": []},                 # no `produced` key at all
                     "{not json",                   # unparseable
                     None):                         # no manifest
        d = _deck(tmp_path / str(hash(str(manifest))), manifest,
                  {"reference-p03.png": "a render", "stray.png": "who knows"})
        assert pl.produced_assets(d) is None
        pl.bundle(d)
        assert _delivered(d) == {"reference-p03.png", "stray.png"}
        assert json.loads((d.root / "bundle.json").read_text())[
            "gated_on_manifest"] is False


def test_a_keyframe_build_travels_by_its_members(tmp_path):
    """`build-pNN/` is the one directory a producer writes, and its frames are
    listed under `frames` rather than as `produced` entries of their own — a
    delivery list that read only `file` would ship the build's manifest and
    none of its pictures.  A frame left over from a shorter build is still
    dropped."""
    d = _deck(tmp_path, {"produced": [
        {"kind": "reference_keyframes", "file": "build-p04/build.json",
         "frames": ["build-p04/step-01.png", "build-p04/step-02.png"]}]},
        {"build-p04/build.json": "{}",
         "build-p04/step-01.png": "one",
         "build-p04/step-02.png": "two",
         "build-p04/step-03.png": "left over from a longer build"})
    pl.bundle(d)

    assert _delivered(d) == {"build-p04/build.json", "build-p04/step-01.png",
                             "build-p04/step-02.png"}
    assert [o["file"] for o in
            json.loads((d.root / "bundle.json").read_text())["omitted"]] == \
        ["build-p04/step-03.png"]


def test_a_promised_file_that_was_never_produced_sends_the_deck_back(tmp_path):
    """The intended consequence: gating the copy turns a silent surplus into a
    loud absence, and a deck whose record names a file the manifest never
    claimed is parked rather than shipped.  What it must not do is park it
    anonymously — the message names the file and says which of the two ways it
    is missing, because they need different work."""
    d = _deck(tmp_path,
              {"produced": [{"kind": "image", "file": "p03-strip-4.png"}]},
              {"p03-strip-4.png": "the strip", "p03--4.png": "the strip"},
              task=[{"kind": "image", "file": "p03--4.png"}])
    pl.bundle(d)

    bad = pl.bundle_problems(d, verify_bytes=False)
    assert len(bad) == 1 and "'p03--4.png'" in bad[0]
    assert "manifest.json does not list it" in bad[0] and "produced" in bad[0]
    assert "materialise" in bad[0]


def test_a_promised_file_that_is_not_on_disk_says_so_instead(tmp_path):
    """The other half of the same question, and the older failure: nothing made
    it.  Same complaint, different work."""
    d = _deck(tmp_path,
              {"produced": [{"kind": "image", "file": "p03-strip-4.png"}]},
              {"p03-strip-4.png": "the strip"},
              task=[{"kind": "data", "file": "p19-table.csv"}])
    pl.bundle(d)

    bad = pl.bundle_problems(d, verify_bytes=False)
    assert len(bad) == 1 and "'p19-table.csv'" in bad[0]
    assert "not in assets/ either" in bad[0]


def test_a_bundle_built_before_the_gate_is_sent_back_to_be_rebuilt(tmp_path):
    """Otherwise the fix reaches no deck that already has a bundle: deck0006's
    `bundle.json` agrees with its own thirteen files and every promised asset is
    present, so nothing above notices.  `_solvable_one` rebuilds a bundle with a
    problem without re-probing — bundling is deterministic and the fingerprints
    say it is the same deck — so naming the surplus here is what heals it."""
    d = _deck(tmp_path,
              {"produced": [{"kind": "image", "file": "p03-strip-4.png"}]},
              {"p03-strip-4.png": "the strip"},
              task=[{"kind": "image", "file": "p03-strip-4.png"}])
    pl.bundle(d)
    assert pl.bundle_problems(d) == []

    # what an ungated bundle looks like: a name the manifest never claimed
    (d.root / "bundle" / "assets" / "p03--4.png").write_text("the strip")
    bad = pl.bundle_problems(d, verify_bytes=False)
    assert len(bad) == 1 and "p03--4.png" in bad[0] and "rebuild" in bad[0]

    pl.bundle(d)
    assert pl.bundle_problems(d) == [] and _delivered(d) == {"p03-strip-4.png"}


def test_rebundling_a_gated_deck_is_a_rebuild_and_not_a_mismatch(tmp_path):
    """`bundle.json` records the omissions beside the digests, so the pair
    stays self-consistent: the bundle a deck ships is the one its manifest
    describes, immediately after being rebuilt."""
    d = _deck(tmp_path,
              {"produced": [{"kind": "image", "file": "p03-strip-4.png"}]},
              {"p03-strip-4.png": "the strip", "p03--4.png": "the strip"},
              task=[{"kind": "image", "file": "p03-strip-4.png"}])
    pl.bundle(d)
    assert pl.bundle_problems(d) == []


# --------------------------------------------------------------------------- #
# where the duplicates come from
# --------------------------------------------------------------------------- #


def _materialisable(tmp_path):
    """A deck `assets.materialise` can run on without a renderer: a real (empty)
    package for `source.pptx` and `input.pptx`, and a task that declares no
    assets, so the run produces nothing and every previous file is superseded."""
    from pptx import Presentation

    d = pl.Deck(tmp_path / "deck0001")
    d.root.mkdir(parents=True, exist_ok=True)
    Presentation().save(str(d.source))
    Presentation().save(str(d.input_pptx))
    d.proposal.write_text(json.dumps({"tasks": [
        {"name": "t", "degradations": [{"id": "d1", "slides": [1]}],
         "assets": []}]}))
    d.delta.write_text(json.dumps({"slides": {}}))
    return d


def test_output_from_a_superseded_run_does_not_survive_into_the_next(tmp_path):
    """The source of deck0006's duplicates.  Every producer writes by name and
    the names move, so a recipe that renames a shape leaves the previous
    recipe's file sitting beside the new one for ever."""
    d = _materialisable(tmp_path)
    (d.root / "assets").mkdir()
    (d.root / "assets" / "p03--4.png").write_text("an older recipe's strip")
    (d.root / "assets" / "manifest.json").write_text(json.dumps(
        {"produced": [{"kind": "image", "file": "p03--4.png"}]}))

    m = A.materialise(d)
    assert not (d.root / "assets" / "p03--4.png").exists()
    assert m["superseded"]["not_reproduced"] == ["p03--4.png"]
    assert (d.root / m["superseded"]["dir"] / "p03--4.png").read_text() == \
        "an older recipe's strip", (
        "swept, not deleted: a repair ordered at `materialise` may fix the "
        "asset by hand, and that repair is what re-runs this stage")


def test_a_hand_prepared_assets_directory_is_left_alone(tmp_path):
    """Only a directory `materialise` is known to have written before is swept.
    Without a `manifest.json` beside them the files came from somewhere else,
    and moving them would be this function guessing."""
    d = _materialisable(tmp_path)
    (d.root / "assets").mkdir()
    (d.root / "assets" / "by-hand.png").write_text("placed deliberately")

    m = A.materialise(d)
    assert (d.root / "assets" / "by-hand.png").exists()
    assert "superseded" not in m


def test_a_second_sweep_does_not_overwrite_the_first(tmp_path):
    """Two re-runs, two directories: the evidence of what the first one moved
    aside is not the price of running it again."""
    d = _materialisable(tmp_path)
    (d.root / "assets").mkdir()
    for n, body in (("one.png", "first"), ("two.png", "second")):
        (d.root / "assets" / "manifest.json").write_text('{"produced": []}')
        (d.root / "assets" / n).write_text(body)
        A.materialise(d)
    assert (d.root / "attempts" / "assets-01" / "one.png").read_text() == "first"
    assert (d.root / "attempts" / "assets-02" / "two.png").read_text() == "second"


# --------------------------------------------------------------------------- #
# the repair red line
# --------------------------------------------------------------------------- #


def _tool_tree(tmp_path, monkeypatch) -> Path:
    """A throwaway git tree standing in for the pipeline's own code."""
    root = tmp_path / "tools"
    (root / "pptxgym").mkdir(parents=True)
    (root / "pptxgym" / "assets.py").write_text("def materialise():\n    pass\n")
    for cmd in (["init", "-q", "."], ["add", "-A"],
                ["-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-qm", "base"]):
        subprocess.run(["git", "-C", str(root), *cmd], check=True,
                       capture_output=True)
    monkeypatch.setattr(pl, "_tool_root", lambda: root)
    return root


def test_a_repair_editing_a_clean_tool_file_is_reverted(tmp_path, monkeypatch):
    """The case that always worked, kept honest."""
    root = _tool_tree(tmp_path, monkeypatch)
    d = pl.Deck(tmp_path / "deck0001")
    d.root.mkdir(parents=True)
    before = pl.tool_tree_state()

    f = root / "pptxgym" / "assets.py"
    f.write_text("def materialise():\n    return 'quieter'\n")
    said = pl.revert_tool_changes(d, before, "repair-01")

    assert said and "1 reverted" in said and "assets.py" in said
    assert f.read_text() == "def materialise():\n    pass\n"
    assert "NOT reverted" not in said
    assert (d.root / "repair-01-tool-change.diff").exists()


def test_a_repair_editing_an_already_modified_tool_file_is_not_invisible(
        tmp_path, monkeypatch):
    """deck0001's second repair edited `pptxgym/assets.py` and the guard never
    fired.  It reverted only paths that were clean at the start, and a
    porcelain line reads ` M pptxgym/assets.py` whether one line changed or
    four hundred — so with somebody else's edit already in the file, the
    fingerprint was unchanged, the guard's first test answered "nothing moved"
    and the repair passed as clean.

    A repairer editing a file another agent is in the middle of is worse than
    one editing a clean file, not better.  It still cannot be reverted —
    `git checkout` would take the other agent's work with it — so it is
    reported, loudly enough that the caller parks the deck."""
    root = _tool_tree(tmp_path, monkeypatch)
    d = pl.Deck(tmp_path / "deck0001")
    d.root.mkdir(parents=True)

    f = root / "pptxgym" / "assets.py"
    f.write_text("def materialise():\n    pass  # another agent, mid-change\n")
    before = pl.tool_tree_state()

    f.write_text("def materialise():\n    pass  # another agent, mid-change\n"
                 "    # and the repairer, on top of it\n")
    said = pl.revert_tool_changes(d, before, "repair-02")

    assert said, "an edit to an already-modified tool file used to read as clean"
    assert "NOT reverted" in said and "assets.py" in said
    assert "and the repairer" in f.read_text(), (
        "reverting would have destroyed the other agent's uncommitted work")
    assert "NOT reverted" in (d.root / "repair-02-tool-change.diff").read_text()


def test_a_repair_adding_an_untracked_tool_file_is_reported(tmp_path,
                                                            monkeypatch):
    """A new module beside the others is as shared as an edited one.  It is not
    deleted — this runs while other agents are working in the same tree, and
    removing a file on the strength of a heuristic is a bigger hammer than the
    problem — but it is named."""
    root = _tool_tree(tmp_path, monkeypatch)
    d = pl.Deck(tmp_path / "deck0001")
    d.root.mkdir(parents=True)
    before = pl.tool_tree_state()

    (root / "pptxgym" / "helper.py").write_text("# a repair's own convenience\n")
    said = pl.revert_tool_changes(d, before, "repair-03")

    assert said and "NOT reverted" in said and "helper.py" in said
    assert (root / "pptxgym" / "helper.py").exists()


def test_the_evidence_survives_an_untracked_file_in_the_same_batch(tmp_path,
                                                                   monkeypatch):
    """One untracked pathspec makes `git diff` fail on the whole list, and the
    evidence for the tracked edit would go with it — leaving a report that says
    two files were touched and shows neither."""
    root = _tool_tree(tmp_path, monkeypatch)
    d = pl.Deck(tmp_path / "deck0001")
    d.root.mkdir(parents=True)
    before = pl.tool_tree_state()

    (root / "pptxgym" / "assets.py").write_text("def materialise():\n    pass\n"
                                                "# quieter\n")
    (root / "pptxgym" / "helper.py").write_text("# and a new one\n")
    said = pl.revert_tool_changes(d, before, "repair-07")

    assert said and "1 reverted" in said and "1 NOT reverted" in said
    assert "# quieter" in (d.root / "repair-07-tool-change.diff").read_text()


def test_a_repair_that_leaves_the_tools_alone_says_nothing(tmp_path,
                                                           monkeypatch):
    """The common case has to stay silent, or every repair parks its deck."""
    root = _tool_tree(tmp_path, monkeypatch)
    d = pl.Deck(tmp_path / "deck0001")
    d.root.mkdir(parents=True)
    (root / "pptxgym" / "assets.py").write_text("# another agent, mid-change\n")
    before = pl.tool_tree_state()

    assert pl.revert_tool_changes(d, before, "repair-04") is None
    assert pl.revert_tool_changes(d, None, "repair-05") is None


def test_a_repair_that_commits_its_edit_is_still_seen(tmp_path, monkeypatch):
    """Committing is not a way out: the working tree comes back clean, which
    used to be indistinguishable from never having touched anything."""
    root = _tool_tree(tmp_path, monkeypatch)
    d = pl.Deck(tmp_path / "deck0001")
    d.root.mkdir(parents=True)
    before = pl.tool_tree_state()

    (root / "pptxgym" / "assets.py").write_text("# quieter\n")
    for cmd in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t",
                                "commit", "-qm", "repaired"]):
        subprocess.run(["git", "-C", str(root), *cmd], check=True,
                       capture_output=True)

    said = pl.revert_tool_changes(d, before, "repair-06")
    assert said and "HEAD moved" in said


def test_the_guard_is_skipped_outside_a_git_tree(tmp_path, monkeypatch):
    """Not a git tree means the check cannot be made, which is not the same as
    passing it — but it must not crash the repair either."""
    monkeypatch.setattr(pl, "_tool_root", lambda: tmp_path / "nowhere")
    assert pl.tool_tree_state() is None


if __name__ == "__main__":                                    # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
