"""The escalation channel, and mostly the one thing it has to get right.

`signature` is what turns "forty decks blocked" into "one bug to check". If it
is too loose, two different defects merge and a fix that closes one resumes
forty decks against the other. If it is too tight, nothing dedups and the
channel is just the old per-deck rejections with a new filename.

So the cases below are real messages from runs 7, 9 and 10, not invented ones —
the same defect as it actually appeared on different decks, and pairs of
different defects that must not collapse.

    python3 -m pytest tests/test_escalate.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import escalate                                     # noqa: E402


# --------------------------------------------------------------------------- #
# the signature, against messages that really happened
# --------------------------------------------------------------------------- #

#: The same defect, as it reached us on three different decks. Verbatim.
SAME_DEFECT = [
    "wrong_params: unproven gate — this attack was credited for ground it did "
    "not touch: d5/text_runs on slide 1 (no perturbation branch for this "
    "operator)",
    "wrong_params: unproven gate — this attack was credited for ground it did "
    "not touch: d2/text_runs on slide 14 (no perturbation branch for this "
    "operator)",
    "wrong_params: unproven gate — this attack was credited for ground it did "
    "not touch: d11/text_runs on slide 7 (no perturbation branch for this "
    "operator)",
]


def test_one_defect_on_three_decks_is_one_signature():
    sigs = {escalate.signature("attack", d) for d in SAME_DEFECT}
    assert len(sigs) == 1, (
        f"the deck, the degradation id and the slide number are what differ "
        f"between these three, and none of them is the defect: {sigs}")


def test_two_different_defects_do_not_collapse():
    """The dangerous direction. Merging these would let a fix for one resume
    every deck blocked by the other."""
    a = escalate.signature("attack", SAME_DEFECT[0])
    b = escalate.signature("attack", (
        "wrong_params: unproven gate — this attack was credited for ground it "
        "did not touch: d4/move on slide 1 (the branch for this operator "
        "changed nothing)"))
    assert a != b


def test_the_two_wordings_of_the_same_gate_stay_apart():
    """"no branch" and "the branch changed nothing" are different bugs with
    the same headline — one is a missing branch, the other a branch that
    cannot act. Reading them as one cost a whole run to discover."""
    no_branch = escalate.signature("attack", "d5/rotate (no perturbation "
                                             "branch for this operator)")
    no_effect = escalate.signature("attack", "d5/rotate (the branch for this "
                                             "operator changed nothing)")
    assert no_branch != no_effect


def test_an_explicit_signature_is_used_verbatim():
    """A caller that knows the class exactly should not have its prose
    hashed — the operator name *is* the class."""
    sig = escalate.signature("attack", "any prose at all",
                             explicit="attack/wrong_params/no-branch/text_runs")
    assert sig == "attack/wrong_params/no-branch/text_runs"


@pytest.mark.parametrize("text,expect_same", [
    ("deck0003: asset 'reference_image' could not be produced",
     "deck0009: asset 'reference_image' could not be produced"),
    ("component floor above 0.15: c001/resize floor=0.25",
     "component floor above 0.15: c014/resize floor=0.31"),
    ("half_restore: 0.305 outside 0.35..0.65",
     "half_restore: 0.481 outside 0.35..0.65"),
])
def test_real_pairs_from_the_runs_dedup(text, expect_same):
    assert escalate.signature("gate", text) == escalate.signature("gate",
                                                                  expect_same)


# --------------------------------------------------------------------------- #
# a claim is not a finding
# --------------------------------------------------------------------------- #


def test_an_agent_may_not_certify_itself():
    """"the pipeline is broken" is the answer that ends a repair the agent
    cannot finish, so it is exactly the claim that must not be self-serving.
    Whatever it writes, the record says `unknown` until somebody checks."""
    rec = escalate.record("deck0003", "hardened", "attack",
                          "this is definitely a pipeline bug",
                          source="repair-agent", who="pipeline")
    assert rec["who"] == "unknown"


def test_a_gate_may_state_a_fact():
    """An operator the executor emits and the battery cannot perturb is our
    gap by definition, on whatever deck it turned up."""
    rec = escalate.record("deck0003", "hardened", "attack",
                          "no perturbation branch for text_runs",
                          source="gate", who="pipeline")
    assert rec["who"] == "pipeline"


def test_an_unknown_source_is_refused():
    with pytest.raises(ValueError):
        escalate.record("deck0001", "recipe", "x", "y", source="the vibes")


# --------------------------------------------------------------------------- #
# reading what an agent left
# --------------------------------------------------------------------------- #


def test_a_malformed_escalation_is_a_failure_to_escalate_not_a_crash(tmp_path):
    """The most volatile input to a stage must not be what ends it — the same
    rule the prompt summaries follow, after three decks died of it."""
    (tmp_path / escalate.FILENAME).write_text("{ not json at all")
    assert escalate.read(tmp_path) is None


def test_a_json_list_is_not_an_escalation(tmp_path):
    (tmp_path / escalate.FILENAME).write_text("[]")
    assert escalate.read(tmp_path) is None


def test_no_file_is_not_an_escalation(tmp_path):
    assert escalate.read(tmp_path) is None


def test_the_file_existing_is_not_a_verdict(tmp_path):
    """An agent that escalates and then repairs the deck anyway has not given
    up. Reading presence as "stop" would park a deck that was just fixed."""
    escalate.write(tmp_path, escalate.record(
        "deck0001", "recipe", "note", "for the record", source="repair-agent"))
    assert escalate.is_blocked(escalate.read(tmp_path)) is False


def test_blocked_is_what_stops_a_deck(tmp_path):
    rec = escalate.record("deck0001", "recipe", "note", "stuck",
                          source="repair-agent")
    rec["verdict"] = "blocked"
    escalate.write(tmp_path, rec)
    assert escalate.is_blocked(escalate.read(tmp_path)) is True


# --------------------------------------------------------------------------- #
# grouping: the whole economic argument
# --------------------------------------------------------------------------- #


def test_grouping_collapses_decks_and_orders_by_how_many_are_blocked():
    records = [
        escalate.record(f"deck000{i}", "hardened", "attack", text,
                        source="gate", who="pipeline")
        for i, text in enumerate(SAME_DEFECT, start=1)
    ] + [
        escalate.record("deck0009", "recipe", "gate",
                        "half_restore: 0.305 outside 0.35..0.65",
                        source="gate", who="pipeline"),
    ]
    groups = escalate.group(records)

    assert len(groups) == 2
    assert groups[0]["decks"] == ["deck0001", "deck0002", "deck0003"]
    assert groups[1]["decks"] == ["deck0009"]


def test_a_gate_fact_outranks_an_agent_claim_about_the_same_defect():
    """If anything mechanical reported this signature, the group is a finding
    and should be presented as one — the agent merely agreed."""
    text = SAME_DEFECT[0]
    claim = escalate.record("deck0001", "hardened", "attack", text,
                            source="repair-agent")
    fact = escalate.record("deck0002", "hardened", "attack", text,
                           source="gate", who="pipeline")
    for order in ([claim, fact], [fact, claim]):
        got = escalate.group(order)[0]
        assert got["source"] == "gate" and got["who"] == "pipeline"


def test_collect_reads_every_deck_and_skips_the_ones_with_nothing(tmp_path):
    for name in ("deck0001", "deck0002", "deck0003"):
        (tmp_path / name).mkdir()
    escalate.write(tmp_path / "deck0002", escalate.record(
        "deck0002", "recipe", "x", "y", source="gate"))
    got = escalate.collect(tmp_path)
    assert [r["deck"] for r in got] == ["deck0002"]


def test_the_run_stream_is_append_only(tmp_path):
    run = tmp_path / "runs" / "r1"
    for i in (1, 2):
        escalate.append_to_run(run, escalate.record(
            f"deck000{i}", "recipe", "x", "y", source="gate"))
    lines = (run / escalate.RUN_FILE).read_text().strip().splitlines()
    assert [json.loads(x)["deck"] for x in lines] == ["deck0001", "deck0002"]


# --------------------------------------------------------------------------- #
# what an agent may and may not decide about its own escalation
# --------------------------------------------------------------------------- #


def test_sanitise_takes_back_the_three_fields_that_are_not_the_agents():
    """An agent writing the file could otherwise mark its own claim a finding,
    and choose the key that decides which decks a fix resumes."""
    raw = {
        "verdict": "blocked",
        "who": "pipeline",                       # not its call
        "source": "gate",                        # certainly not its call
        "signature": "everything/is/one/bug",    # would merge unrelated decks
        "detail": "wrong_params keeps rejecting d4/move on slide 1",
        "tried": "rewrote the recipe twice",
    }
    got = escalate.sanitise("deck0003", "hardened", raw, attempt=2)

    assert got["who"] == "unknown"
    assert got["source"] == "repair-agent"
    assert got["signature"] != "everything/is/one/bug"
    assert got["signature"] == escalate.signature("repair", raw["detail"])
    # and what it *did* observe is kept, labelled as a lead rather than a fact
    assert got["agent_says_who"] == "pipeline"
    assert got["tried"] == "rewrote the recipe twice"
    assert got["attempt"] == 2


def test_sanitise_survives_an_agent_that_wrote_almost_nothing():
    got = escalate.sanitise("deck0001", "recipe", {"verdict": "blocked"})
    assert got["who"] == "unknown" and got["verdict"] == "blocked"
    assert got["signature"]


def test_sanitise_ignores_evidence_that_is_not_a_record():
    got = escalate.sanitise("deck0001", "recipe",
                            {"detail": "x", "evidence": "a string"})
    assert got["evidence"] == {}
