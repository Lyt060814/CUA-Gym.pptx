"""The foreman, held to its own claim: it judges nothing.

Every case is a boundary of the mechanical verdict. A deck ships when the
record says so *and* the re-executed measurements agree — `shipped` reads
the record as the cheap pre-filter, `verify` runs `score` and `harden` from
the artefacts and gets the last word. Anything else parks, with the reason
named. The one thing the foreman is allowed to *do* to a deck's content is
revert an edit to the shared tools, and that path is tested the way the
repair loop's was: the guard fires, the deck parks, shipping state
notwithstanding.

    python3 -m pytest tests/test_foreman.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import foreman as fm                                # noqa: E402
from pptxgym import pipeline as pl                               # noqa: E402
from pptxgym import agent as agentmod                            # noqa: E402


def _args(**over):
    base = dict(workers=1, max_turns=150, timeout=300, model="opus",
                effort="high", specialist_model=None,
                specialist_effort=None, assign=None, roundtrip=False,
                force=False, deck=None)
    base.update(over)
    return Namespace(**base)


def _deck(tmp_path, state=None, review=False, slides=5,
          name="deck0001") -> pl.Deck:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "meta.json").write_text(json.dumps({"name": "t.pptx",
                                                "slides": slides}))
    (root / "state.json").write_text(json.dumps(state or {}))
    if review:
        (root / "REVIEW.md").write_text("# REVIEW\n")
    return pl.Deck(root)


#: The record of a deck that really shipped, as trial 1 wrote it.
SHIPPED = {
    "inspected": {"status": "ok"},
    "scored": {"status": "ok", "gt": 1.0, "input": 0.0},
    "packaged": {"status": "ok", "task_id": "abc123"},
}


def _run(deck, tmp_path, args, monkeypatch, result=None, on_spawn=None,
         verify=(True, "")):
    """Drive run_deck with the agent, the guard and re-execution stubbed."""
    async def fake_agent(spec):
        if on_spawn:
            on_spawn(spec)
        return dict(result or {"status": "exited", "returncode": 0})

    monkeypatch.setattr(agentmod, "run_agent", fake_agent)
    monkeypatch.setattr(pl, "tool_tree_state", lambda: "clean")
    monkeypatch.setattr(pl, "revert_tool_changes", lambda d, b, l: None)
    monkeypatch.setattr(pl, "bundle_problems", lambda d: [])
    monkeypatch.setattr(fm, "verify", lambda d, w=True: verify)
    return asyncio.run(fm.run_deck(deck, tmp_path, args))


# --------------------------------------------------------------------------- #
# the mission says what the run needs and nothing it must not
# --------------------------------------------------------------------------- #


def test_mission_names_the_deck_the_budget_and_the_boundaries(tmp_path):
    deck = _deck(tmp_path, SHIPPED, review=True, slides=19)
    text = fm.mission(deck, tmp_path, 150, fm.ASSIGN)
    assert deck.id in text
    assert str(tmp_path.resolve()) in text
    assert "150" in text
    assert "not yours to change" in text          # the no-code-edit clause
    assert "do not read them" in text             # other decks are off limits
    assert "REVIEW.md" in text


def test_mission_briefs_one_lane_per_specialist(tmp_path):
    deck = _deck(tmp_path, SHIPPED)
    text = fm.mission(deck, tmp_path, 80, fm.ASSIGN)
    assert "reconcile: --model opus --effort high" in text
    assert "recipe: --model sonnet --effort medium" in text
    assert "solvable: no model flags" in text
    assert "sealed probe pinned to claude/haiku" in text


def test_assignment_defaults_edits_and_override_all():
    assert fm.assignment(_args()) == fm.ASSIGN
    edited = fm.assignment(_args(assign="recipe=haiku:low"))
    assert edited["recipe"] == ("haiku", "low")
    assert edited["propose"] == fm.ASSIGN["propose"]      # others untouched
    flat = fm.assignment(_args(specialist_model="opus",
                               specialist_effort="max"))
    assert set(flat.values()) == {("opus", "max")}


# --------------------------------------------------------------------------- #
# the mechanical verdict
# --------------------------------------------------------------------------- #


def test_a_deck_with_the_full_record_ships(tmp_path, monkeypatch):
    deck = _deck(tmp_path, SHIPPED, review=True)
    rec = _run(deck, tmp_path, _args(), monkeypatch)
    assert rec["outcome"] == "shipped"
    assert rec["task"] == "abc123"
    assert json.loads((deck.root / "foreman.json").read_text())["outcome"] \
        == "shipped"


def test_no_review_md_means_parked_and_says_so(tmp_path, monkeypatch):
    deck = _deck(tmp_path, SHIPPED, review=False)
    rec = _run(deck, tmp_path, _args(), monkeypatch)
    assert rec["outcome"] == "parked"
    assert "REVIEW.md" in rec["why"]


def test_a_recorded_score_short_of_the_floor_parks(tmp_path, monkeypatch):
    state = dict(SHIPPED, scored={"status": "ok", "gt": 1.0, "input": 0.35})
    deck = _deck(tmp_path, state, review=True)
    rec = _run(deck, tmp_path, _args(), monkeypatch)
    assert rec["outcome"] == "parked"
    assert "input=0.35" in rec["why"]


def test_not_packaged_parks_with_the_orchestrators_last_words(tmp_path,
                                                              monkeypatch):
    deck = _deck(tmp_path, {"inspected": {"status": "ok"}}, review=True)
    rec = _run(deck, tmp_path, _args(), monkeypatch,
               result={"status": "exited", "returncode": 0,
                       "why": "no task worth shipping on this deck"})
    assert rec["outcome"] == "parked"
    assert "packaged" in rec["why"]
    assert "no task worth shipping" in rec.get("last", "")


def test_a_truncated_orchestrator_with_an_incomplete_deck_parks(tmp_path,
                                                                monkeypatch):
    deck = _deck(tmp_path, {"inspected": {"status": "ok"}}, review=True)
    rec = _run(deck, tmp_path, _args(), monkeypatch,
               result={"status": "truncated", "kind": "max_turns",
                       "why": "the run stopped on max_turns"})
    assert rec["outcome"] == "parked"
    assert "max_turns" in rec["why"]


def test_the_goods_outrank_the_messenger(tmp_path, monkeypatch):
    """deck0003 on the first Jobs run: all nine stages done, verify clean,
    orchestrator spent its last turn on the summary — that ships. force
    bypasses the prep shortcut so the truncated agent actually runs."""
    deck = _deck(tmp_path, SHIPPED, review=True)
    rec = _run(deck, tmp_path, _args(force=True), monkeypatch,
               result={"status": "truncated", "kind": "max_turns",
                       "why": "the run stopped on max_turns"})
    assert rec["outcome"] == "shipped"


def test_a_fingerprint_that_moved_does_not_unship_a_deck(tmp_path,
                                                         monkeypatch):
    """`packaged` fingerprints `attacks.json`, and collect rewrites it by
    re-executing `harden` to verify the deck — so the stale-downgraded read
    said "not shipped" about precisely the decks that had shipped, and a
    resume sweep re-ran them from scratch at full price."""
    deck = _deck(tmp_path, SHIPPED, review=True)
    monkeypatch.setattr(pl, "bundle_problems", lambda d: [])
    monkeypatch.setattr(pl.Deck, "stale", lambda self, stage: ["attacks.json"])
    assert deck.status_of("packaged") == "stale"
    assert fm.shipped(deck)[0]


def test_a_complete_record_ships_at_prep_without_an_orchestrator(tmp_path,
                                                                 monkeypatch):
    deck = _deck(tmp_path, SHIPPED, review=True)
    spawned = []
    rec = _run(deck, tmp_path, _args(), monkeypatch,
               on_spawn=lambda s: spawned.append(s.name))
    assert rec["outcome"] == "shipped"
    assert spawned == []                     # verified from the record alone
    assert rec["agent"] == "record"


# --------------------------------------------------------------------------- #
# the guard: tools are everybody's, and shipping does not launder an edit
# --------------------------------------------------------------------------- #


def test_a_tool_edit_parks_the_deck_even_when_the_record_ships(tmp_path,
                                                               monkeypatch):
    # force, so the prep shortcut does not ship the complete record before
    # an agent ever runs — the case under test is an agent that ran and
    # edited the shared tools.
    deck = _deck(tmp_path, SHIPPED, review=True)

    async def fake_agent(spec):
        return {"status": "exited", "returncode": 0}

    monkeypatch.setattr(agentmod, "run_agent", fake_agent)
    monkeypatch.setattr(pl, "tool_tree_state", lambda: "before")
    monkeypatch.setattr(pl, "revert_tool_changes",
                        lambda d, b, l: "1 tool file(s): 1 reverted "
                                        "(pptxgym/comparators.py)")
    monkeypatch.setattr(pl, "bundle_problems", lambda d: [])
    rec = asyncio.run(fm.run_deck(deck, tmp_path, _args(force=True)))
    assert rec["outcome"] == "parked"
    assert "shared tools" in rec["why"]
    assert "comparators.py" in rec["why"]


# --------------------------------------------------------------------------- #
# collect re-executes: the scoreboard cannot ship a deck the measurements
# would not
# --------------------------------------------------------------------------- #


def test_a_shipping_record_still_parks_when_reexecution_disagrees(
        tmp_path, monkeypatch):
    # Both trial-2 orchestrators wrote a shipping record over an overridden
    # measurement. The record passes `shipped`; `verify` gets the last word.
    deck = _deck(tmp_path, SHIPPED, review=True)
    rec = _run(deck, tmp_path, _args(), monkeypatch,
               verify=(False, "harden re-executed: beaten by full_copy"))
    assert rec["outcome"] == "parked"
    assert "beaten by full_copy" in rec["why"]


def test_verify_runs_the_verbs_not_the_record(tmp_path, monkeypatch):
    deck = _deck(tmp_path, SHIPPED, review=True)
    calls = []

    def fake_score(d):
        calls.append("score")
        return {"gt": 1.0, "input": 0.0}

    def fake_harden(d, wps=True):
        calls.append("harden")
        return {"beaten": [], "problems": []}

    monkeypatch.setattr(pl, "score_task", fake_score)
    monkeypatch.setattr(pl, "harden", fake_harden)
    ok, why = fm.verify(deck)
    assert ok and calls == ["score", "harden"]


def test_verify_stops_at_a_failing_score(tmp_path, monkeypatch):
    deck = _deck(tmp_path, SHIPPED, review=True)
    monkeypatch.setattr(pl, "score_task",
                        lambda d: {"gt": 1.0, "input": 0.35})
    monkeypatch.setattr(
        pl, "harden",
        lambda d, wps=True: (_ for _ in ()).throw(
            AssertionError("must not run")))
    ok, why = fm.verify(deck)
    assert not ok and "input=0.35" in why


# --------------------------------------------------------------------------- #
# the prefilter: a doomed deck parks before it costs agent money
# --------------------------------------------------------------------------- #


def _paint(path, colour, size=(60, 40), speck=False):
    from PIL import Image
    im = Image.new("RGB", size, colour)
    if speck:
        im.putpixel((3, 3), (0, 0, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def test_blank_renders_are_detected_and_content_is_not(tmp_path):
    deck = _deck(tmp_path, {})
    _paint(deck.root / "renders" / "p-01.png", (255, 255, 255))
    _paint(deck.root / "renders" / "p-02.png", (255, 255, 255))
    assert fm.renders_blank(deck)
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (60, 40), (255, 255, 255))
    ImageDraw.Draw(im).rectangle([10, 10, 40, 30], fill=(30, 60, 200))
    im.save(deck.root / "renders" / "p-02.png")
    assert not fm.renders_blank(deck)        # one real page clears the deck


def test_missing_fonts_park_and_standins_do_not(tmp_path, monkeypatch):
    deck = _deck(tmp_path, {})
    monkeypatch.setattr(fm, "_fonts_wanted",
                        lambda p: {"calibri", "made-up grotesk"})
    monkeypatch.setattr(fm, "_fonts_installed",
                        lambda: {"carlito", "dejavu sans"})
    assert fm.fonts_missing(deck) == ["made-up grotesk"]
    why = fm.prefilter(deck)
    assert why and "made-up grotesk" in why
    monkeypatch.setattr(fm, "_fonts_wanted", lambda p: {"calibri"})
    assert fm.fonts_missing(deck) == []      # carlito covers calibri


def test_a_cut_of_a_known_family_is_not_a_missing_font(tmp_path, monkeypatch):
    """The three false parks of the first Jobs run, pinned: a weight suffix,
    a hyphenated weight, and a 'neue' rebrand are cuts of families the
    substitution table already covers."""
    deck = _deck(tmp_path, {})
    monkeypatch.setattr(fm, "_fonts_wanted",
                        lambda p: {"helvetica neue", "open sans light",
                                   "calibri-light"})
    monkeypatch.setattr(fm, "_fonts_installed", lambda: {"dejavu sans"})
    assert fm.fonts_missing(deck) == []


def test_a_cjk_face_is_covered_by_any_installed_cjk_family(tmp_path,
                                                           monkeypatch):
    deck = _deck(tmp_path, {})
    monkeypatch.setattr(fm, "_fonts_wanted", lambda p: {"microsoft yahei"})
    monkeypatch.setattr(fm, "_fonts_installed",
                        lambda: {"noto sans cjk sc", "dejavu sans"})
    assert fm.fonts_missing(deck) == []
    monkeypatch.setattr(fm, "_fonts_installed", lambda: {"dejavu sans"})
    assert fm.fonts_missing(deck) == ["microsoft yahei"]   # tofu for real


def test_an_unanswerable_font_check_parks_nothing(tmp_path, monkeypatch):
    deck = _deck(tmp_path, {})
    monkeypatch.setattr(fm, "_fonts_wanted", lambda p: {"anything"})
    monkeypatch.setattr(fm, "_fonts_installed", lambda: None)
    assert fm.fonts_missing(deck) == []


def test_a_prefiltered_deck_parks_before_the_agent_spawns(tmp_path,
                                                          monkeypatch):
    deck = _deck(tmp_path, {"inspected": {"status": "ok"}}, review=True)
    spawned = []
    monkeypatch.setattr(fm, "prefilter", lambda d: "missing fonts: x")
    rec = _run(deck, tmp_path, _args(), monkeypatch,
               on_spawn=lambda s: spawned.append(s.name))
    assert rec["outcome"] == "parked"
    assert rec["kind"] == "prefilter"
    assert spawned == []


# --------------------------------------------------------------------------- #
# prep: deterministic work happens before the owner is spawned
# --------------------------------------------------------------------------- #


def test_an_uninspected_deck_is_inspected_first(tmp_path, monkeypatch):
    deck = _deck(tmp_path, {}, review=True)
    calls = []
    monkeypatch.setattr(pl, "inspect",
                        lambda d, roundtrip=False: calls.append(d.id))
    rec = _run(deck, tmp_path, _args(), monkeypatch)
    assert calls == ["deck0001"]
    assert rec["outcome"] == "parked"        # nothing packaged; that is fine


def test_inspect_failure_parks_without_spawning(tmp_path, monkeypatch):
    deck = _deck(tmp_path, {})
    spawned = []

    def broken(d, roundtrip=False):
        raise pl.StageError("render produced 0 pages")

    monkeypatch.setattr(pl, "inspect", broken)
    rec = _run(deck, tmp_path, _args(), monkeypatch,
               on_spawn=lambda s: spawned.append(s.name))
    assert rec["outcome"] == "parked"
    assert "inspect failed" in rec["why"]
    assert spawned == []


# --------------------------------------------------------------------------- #
# a dirty tool tree refuses to launch: the guard cannot attribute around one
# --------------------------------------------------------------------------- #


def test_a_dirty_tool_tree_refuses_to_launch(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(fm, "dirty_tool_paths",
                        lambda: ["pptxgym/foreman.py"])
    monkeypatch.setattr(fm, "run_batch",
                        lambda *a, **k: ran.append(1))
    rc = fm.main(["--work", str(tmp_path)])
    assert rc == 2
    assert ran == []


def test_allow_dirty_takes_the_risk_knowingly(tmp_path, monkeypatch):
    monkeypatch.setattr(fm, "dirty_tool_paths",
                        lambda: ["pptxgym/foreman.py"])
    rc = fm.main(["--work", str(tmp_path), "--allow-dirty"])
    assert rc == 0                      # empty work root: "nothing to do"


def test_dirty_tool_paths_reads_the_fingerprint(monkeypatch):
    state = json.dumps({"head": "abc", "entries": {
        "pptxgym/a.py": [" M", "d1"], "pptxgym/b.py": ["??", "d2"]}})
    monkeypatch.setattr(pl, "tool_tree_state", lambda: state)
    assert fm.dirty_tool_paths() == ["pptxgym/a.py", "pptxgym/b.py"]
    monkeypatch.setattr(pl, "tool_tree_state", lambda: None)
    assert fm.dirty_tool_paths() == []


# --------------------------------------------------------------------------- #
# picking decks: shipped ones stay shipped unless somebody insists
# --------------------------------------------------------------------------- #


def test_pick_decks_skips_only_what_is_booked_shipped(tmp_path, monkeypatch):
    deck = _deck(tmp_path, SHIPPED, review=True)
    monkeypatch.setattr(pl, "bundle_problems", lambda d: [])
    # complete record, no shipped booking (deck0003's shape): still picked,
    # so the prep shortcut can verify and book it
    assert [d.id for d in fm.pick_decks(tmp_path, _args())] == ["deck0001"]
    (deck.root / "foreman.json").write_text(json.dumps(
        {"deck": "deck0001", "outcome": "shipped"}))
    assert fm.pick_decks(tmp_path, _args()) == []
    picked = fm.pick_decks(tmp_path, _args(force=True))
    assert [d.id for d in picked] == ["deck0001"]


def test_pick_decks_honours_an_explicit_list(tmp_path):
    _deck(tmp_path, {"inspected": {"status": "ok"}}, name="deck0042")
    picked = fm.pick_decks(tmp_path, _args(deck=["deck0042"]))
    assert [d.id for d in picked] == ["deck0042"]


def test_pick_decks_refuses_a_target_missing_from_the_resume(tmp_path):
    with pytest.raises(ValueError, match="absent from the restored work tree"):
        fm.pick_decks(tmp_path, _args(deck=["deck0042"]))


# --------------------------------------------------------------------------- #
# a timeout with work left gets one fresh session, not a park
# --------------------------------------------------------------------------- #


def test_timeout_with_work_left_gets_one_fresh_session(tmp_path, monkeypatch):
    # fast50 lost three decks whose orchestrators hung inside a tool call
    # "awaiting" a probe whose verdict was already in state.json: the wall
    # clock killed them and the kill parked them, though a fresh session
    # reading the state was minutes from done.
    deck = _deck(tmp_path, {"inspected": {"status": "ok"}}, review=True)
    (deck.root / "proposal.json").write_text(json.dumps(
        {"tasks": [{"name": "t"}]}))
    calls = []

    async def fake_agent(spec):
        calls.append(spec.prompt)
        return {"status": "timeout", "why": "wall clock"}

    monkeypatch.setattr(agentmod, "run_agent", fake_agent)
    monkeypatch.setattr(pl, "tool_tree_state", lambda: "clean")
    monkeypatch.setattr(pl, "revert_tool_changes", lambda d, b, l: None)
    monkeypatch.setattr(pl, "bundle_problems", lambda d: [])
    rec = asyncio.run(fm.run_deck(deck, tmp_path, _args()))
    # exactly one retry — a second timeout parks, it does not loop
    assert len(calls) == 2
    assert calls[0] == calls[1]          # same mission, fresh budget
    assert rec["outcome"] == "parked"


def test_timeout_on_a_reasoned_decline_is_not_retried(tmp_path, monkeypatch):
    # an empty proposal plus a REVIEW.md is a finished argument — a hung
    # session on top of it changes nothing, so no fresh session is owed
    deck = _deck(tmp_path, {"inspected": {"status": "ok"}}, review=True)
    (deck.root / "proposal.json").write_text(json.dumps({"tasks": []}))
    calls = []

    async def fake_agent(spec):
        calls.append(1)
        return {"status": "timeout", "why": "wall clock"}

    monkeypatch.setattr(agentmod, "run_agent", fake_agent)
    monkeypatch.setattr(pl, "tool_tree_state", lambda: "clean")
    monkeypatch.setattr(pl, "revert_tool_changes", lambda d, b, l: None)
    monkeypatch.setattr(pl, "bundle_problems", lambda d: [])
    rec = asyncio.run(fm.run_deck(deck, tmp_path, _args()))
    assert len(calls) == 1
