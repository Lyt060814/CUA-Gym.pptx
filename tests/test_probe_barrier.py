"""Where the solvability probe stands, and what it can reach from there.

The barrier used to be a paragraph in the prompt plus a scan of the log
afterwards, with the probe's working directory set to the repository root —
one level above `work/`.  That arrangement never made the answer key
unreachable; it made reaching it *detectable*, and detection throws the whole
run away.  Two of the last ten probes were voided that way, deck0007 twice, and
the call that did it was `ls -la work/deck0007/`: a probe looking at the
directory it had been told to write its report into.

So these tests are about a property, not about a preference: **a probe that
tried to open the answer key would fail.**  The log scan is still here and
still strict, one section down, as the backstop it is now allowed to be.

    python3 -m pytest tests/test_probe_barrier.py -q
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym import agent                                          # noqa: E402
from pptxgym import cli                                            # noqa: E402
from pptxgym import pipeline as pl                                 # noqa: E402


REPORT = {
    "verdict": "solvable",
    "verdict_reason": "table line 5: nothing found",
    "degradations": [{"id": "d1", "slides": [3],
                      "end_state": "the twin is back",
                      "checks": {"E1": "slide 2 carries the same card",
                                 "E2": "", "E3": "", "E4": "", "E5": "",
                                 "E6": ""},
                      "evidence": "slide 2", "determinate": True,
                      "rivals": [], "undetermined": "", "tolerance": [],
                      "est_steps_measured": 60, "overdetermined": False}],
    "leaks": [], "residue": [], "rework": [],
    "est_steps_measured": 60, "est_steps_declared": 60,
}


def _deck(tmp_path, name="deck0007", origin: Path | None = None) -> pl.Deck:
    """A deck sitting in a work directory, reconciled, bundle and all."""
    work = tmp_path / "work"
    d = pl.Deck(work / name)
    (d.root / "assets").mkdir(parents=True)
    (d.root / "input.pptx").write_text("the broken deck")
    (d.root / "source.pptx").write_text("THE ANSWER: the deck as it was")
    (d.root / "delta.json").write_text('{"THE ANSWER": "every change"}')
    (d.root / "recipe.json").write_text('{"THE ANSWER": "how it was broken"}')
    (d.root / "assets" / "manifest.json").write_text(json.dumps({"unmet": []}))
    (d.root / "assets" / "reference-p03.png").write_text("a render")
    (d.root / "meta.json").write_text(json.dumps(
        {"slides": 3, "name": "a deck", "origin": str(origin or "")}))
    (d.root / "task.json").write_text(json.dumps({
        "name": "t", "instruction": "put it back", "difficulty": "medium",
        "est_steps": 200, "degradations": [{"id": "d1", "slides": [3]}],
        "assets": [{"kind": "reference_image", "file": "reference-p03.png"}],
        "instruction_changed": False, "notes": "", "verdict": "ready"}))
    d.mark("reconciled", "ok")
    pl.bundle(d)
    return d


def _needs_mask():
    ok, why = pl.mask_available()
    if not ok:
        pytest.skip(f"this machine cannot mask a directory: {why}")


# --------------------------------------------------------------------------- #
# what counts as the answer key
# --------------------------------------------------------------------------- #


def test_the_whole_work_tree_is_the_answer_key_not_one_deck(tmp_path):
    """`work/deck0003/delta.json` answers deck0003's task, and a barrier drawn
    round deck0007 alone would hand it over while probing deck0007."""
    d = _deck(tmp_path)
    assert d.root.parent in pl.answer_key_roots(d)


def test_the_corpus_the_deck_came_from_is_the_answer_key_too(tmp_path):
    """`meta.json` records an `origin`: the pristine file exists outside
    `work/` as well, under a name the pipeline itself wrote down."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "deck.pptx").write_text("the pristine deck")
    d = _deck(tmp_path, origin=corpus / "deck.pptx")
    assert corpus.resolve() in pl.answer_key_roots(d)


def test_an_operator_can_name_the_corpus_root(tmp_path, monkeypatch):
    """The same deck appears in six or seven sibling directories of the corpus,
    and nothing records where the corpus starts."""
    corpus = tmp_path / "elsewhere"
    corpus.mkdir()
    monkeypatch.setenv("PPTXGYM_CORPUS", str(corpus))
    assert corpus.resolve() in pl.answer_key_roots(_deck(tmp_path))


# --------------------------------------------------------------------------- #
# the workspace
# --------------------------------------------------------------------------- #


def test_the_probe_works_from_a_copy_of_the_bundle_and_nothing_else(tmp_path):
    _needs_mask()
    d = _deck(tmp_path)
    with pl.probe_workspace(d) as ws:
        assert (ws.bundle / "input.pptx").read_text() == "the broken deck"
        assert (ws.bundle / "instruction.md").exists()
        assert (ws.bundle / "assets" / "reference-p03.png").exists()
        here = {p.name for p in ws.dir.rglob("*")}
        for answer in ("source.pptx", "delta.json", "recipe.json",
                       "task.json", "manifest.json"):
            assert answer not in here
        # nothing to climb to: the workspace is not under the work tree
        assert d.root.parent not in ws.dir.parents


def test_the_job_contract_travels_with_it(tmp_path):
    """`--agent solver-probe` resolves against the working directory.  A probe
    launched somewhere with no `.claude/agents` is a probe running without the
    contract that tells it what it may open."""
    _needs_mask()
    d = _deck(tmp_path)
    with pl.probe_workspace(d) as ws:
        assert (ws.dir / ".claude" / "agents" / "solver-probe.md").exists()


def test_the_workspace_is_gone_afterwards(tmp_path):
    _needs_mask()
    d = _deck(tmp_path)
    with pl.probe_workspace(d) as ws:
        where = ws.dir
    assert not where.exists()


def test_a_workspace_inside_the_answer_key_is_refused(tmp_path, monkeypatch):
    """`TMPDIR` pointing into `work/` would put the probe's own directory
    inside the thing it must not see, and the mask would then hide its bundle
    from it — a barrier that works by breaking the run is not one."""
    d = _deck(tmp_path)
    inside = d.root.parent / "tmp"
    inside.mkdir()
    monkeypatch.setenv("PPTXGYM_PROBE_TMP", str(inside))
    with pytest.raises(pl.StageError) as e:
        with pl.probe_workspace(d):
            pass
    assert "PPTXGYM_PROBE_TMP" in str(e.value)


# --------------------------------------------------------------------------- #
# the barrier itself
# --------------------------------------------------------------------------- #


def _inside(ws, script: str) -> subprocess.CompletedProcess:
    """Run a shell command exactly where the probe would run."""
    return subprocess.run([*ws.launcher, "/bin/sh", "-c", script, "sh"],
                          env={**os.environ, **ws.env}, cwd=str(ws.dir),
                          capture_output=True, text=True, timeout=60)


def test_the_answer_key_does_not_exist_where_the_probe_runs(tmp_path):
    """The property, stated as the kernel sees it: not "refused" — *absent*.
    `open()` returns ENOENT, so no absolute path, no `..` and no `find /` gets
    round it, and neither does anything the probe spawns."""
    _needs_mask()
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "deck.pptx").write_text("the pristine deck")
    d = _deck(tmp_path, origin=corpus / "deck.pptx")
    with pl.probe_workspace(d) as ws:
        assert ws.kind == "namespace+deny"
        for answer in (d.source, d.delta, d.root / "task.json",
                       corpus / "deck.pptx"):
            r = _inside(ws, f'cat "{answer}"')
            assert r.returncode != 0, f"{answer} was readable"
            assert "THE ANSWER" not in r.stdout
        # the work tree is still a directory in there, and it is empty: every
        # other deck's answer key went with this one
        listed = _inside(ws, f'ls -A "{d.root.parent}"')
        assert listed.stdout.strip() == ""
        # and the bundle it is there to judge is readable as ever
        got = _inside(ws, f'cat "{ws.bundle / "input.pptx"}"')
        assert got.stdout == "the broken deck"


def test_the_pipeline_tooling_still_works_in_there(tmp_path):
    """The probe's idioms are `python3 -m pptxgym.…`, and they used to resolve
    only because its cwd *was* the repository.  Moving it without carrying
    `PYTHONPATH` would leave it unable to open the file it is judging."""
    _needs_mask()
    d = _deck(tmp_path)
    with pl.probe_workspace(d) as ws:
        r = _inside(ws, f'{sys.executable} -c "import pptxgym, sys; '
                        f'print(pptxgym.__file__)"')
        assert r.returncode == 0, r.stderr
        assert "pptxgym" in r.stdout


def test_the_deny_rules_name_absolute_paths_the_way_the_harness_reads_them(
        tmp_path):
    """Measured, and the reason this is a test: `Read(/abs/path/**)` is read as
    a path *relative to the settings file* and denies nothing at all, while
    `Read(//abs/path/**)` denies the Read tool and Bash commands naming that
    path alike.  One slash is the difference between a barrier and a belief."""
    _needs_mask()
    d = _deck(tmp_path)
    with pl.probe_workspace(d) as ws:
        deny = json.loads(ws.settings)["permissions"]["deny"]
        assert deny
        for rule in deny:
            assert rule.startswith("Read(//"), rule
        assert any(str(d.root.parent.resolve()) in r for r in deny)


def test_the_mask_is_verified_from_inside_before_the_agent_starts(tmp_path):
    """A mount that silently did not happen is worse than no mount: the run
    proceeds believing it is sealed.  The launcher looks for the answer key
    itself and refuses to `exec` while it can still see it."""
    _needs_mask()
    d = _deck(tmp_path)
    with pl.probe_workspace(d) as ws:
        assert d.root / "task.json" in ws.sentinels()
        # nothing masked, sentinel still there: the launcher must not run it
        r = subprocess.run([*ws.launcher, "/bin/echo", "the agent ran"],
                           env={**os.environ, **ws.env,
                                "PPTXGYM_PROBE_MASKED": str(tmp_path / "nope")},
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == agent.BARRIER_FAILED
        assert "the agent ran" not in r.stdout


def test_the_weaker_barrier_has_to_be_asked_for_by_name(tmp_path, monkeypatch):
    """A machine that cannot give us a namespace does not silently get the old
    arrangement back — the whole defect was a barrier everybody believed was in
    force."""
    d = _deck(tmp_path)
    monkeypatch.setattr(pl, "mask_available", lambda: (False, "no namespaces"))
    with pytest.raises(pl.StageError) as e:
        with pl.probe_workspace(d):
            pass
    assert "PPTXGYM_PROBE_BARRIER=cwd" in str(e.value)

    monkeypatch.setenv("PPTXGYM_PROBE_BARRIER", "cwd")
    with pl.probe_workspace(d) as ws:
        assert ws.kind == "deny"
        assert ws.launcher == []
        assert json.loads(ws.settings)["permissions"]["deny"]


# --------------------------------------------------------------------------- #
# the record, and what the gate does with it
# --------------------------------------------------------------------------- #


def test_the_report_comes_back_with_a_record_of_where_it_was_written(tmp_path):
    _needs_mask()
    d = _deck(tmp_path)
    with pl.probe_workspace(d) as ws:
        ws.report.write_text(json.dumps(REPORT))
        assert ws.collect() is True
    rec = pl.probe_record(d)
    assert json.loads((d.root / "solvability.json").read_text())["verdict"] \
        == "solvable"
    assert rec["barrier"] == "namespace+deny"
    assert str(d.root.parent.resolve()) in rec["masked"]
    assert rec["report_returned"] is True


def test_a_verdict_with_no_record_of_the_barrier_is_not_a_verdict(tmp_path):
    """A report that arrives with no `probe.json` beside it was produced by
    something that never went through `probe_workspace` — which is exactly the
    arrangement whose verdicts were worth nothing."""
    _needs_mask()
    d = _deck(tmp_path)
    (d.root / "solvability.json").write_text(json.dumps(REPORT))
    with pytest.raises(pl.StageError) as e:
        pl.check_solvability(d)
    assert "probe.json" in str(e.value)


def test_the_gate_records_which_barrier_produced_the_verdict(tmp_path):
    _needs_mask()
    d = _deck(tmp_path)
    with pl.probe_workspace(d) as ws:
        ws.report.write_text(json.dumps(REPORT))
        ws.collect()
    assert pl.check_solvability(d)["barrier"] == "namespace+deny"


# --------------------------------------------------------------------------- #
# the scan, which is now the backstop and not the barrier
# --------------------------------------------------------------------------- #


def _log(deck, *calls) -> Path:
    f = deck.root / "solvable.jsonl"
    with open(f, "w") as fh:
        for name, inp in calls:
            fh.write(json.dumps({"message": {"content": [
                {"type": "tool_use", "name": name, "input": inp}]}}) + "\n")
    return f


def test_another_decks_answer_key_is_a_breach_too(tmp_path):
    """The old pattern knew only `work/deck0007` while probing deck0007, so
    `cat work/deck0003/delta.json` read as clean."""
    d = _deck(tmp_path)
    other = d.root.parent / "deck0003" / "delta.json"
    assert pl.barrier_breaches(d, _log(d, ("Bash", {"command": f"cat {other}"})))
    assert pl.barrier_breaches(
        d, _log(d, ("Bash", {"command": "cat work/deck0003/delta.json"})))


def test_the_corpus_is_a_breach(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "deck.pptx").write_text("the pristine deck")
    d = _deck(tmp_path, origin=corpus / "deck.pptx")
    assert pl.barrier_breaches(
        d, _log(d, ("Bash", {"command": f"unzip -l {corpus / 'deck.pptx'}"})))


def test_the_bundle_and_the_report_are_still_not_breaches(tmp_path):
    """Unchanged, and deliberately: calling a probe's re-read of its own report
    a peek voided four runs, and the strictness that is wanted is about the
    answer key, not about paperwork."""
    d = _deck(tmp_path)
    assert not pl.barrier_breaches(d, _log(
        d, ("Read", {"file_path": str(d.root / "bundle" / "input.pptx")}),
        ("Bash", {"command": f"cat {d.root / 'solvability.json'}"}),
        ("Bash", {"command": 'grep -rn "source.pptx" --include=*.py pptxgym'})))


# --------------------------------------------------------------------------- #
# the whole stage, with a probe that tries it on
# --------------------------------------------------------------------------- #


PEEKING_CLAUDE = '''
import json, os, re, sys

argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1]
open(os.environ["PEEK_PROMPT"], "w").write(prompt)

# what an agent that decided to look would get
try:
    open(os.environ["PEEK_AT"]).read()
    saw = "read it"
except OSError as e:
    saw = type(e).__name__
open(os.environ["PEEK_RESULT"], "w").write(saw)

# the report goes where the prompt says, which is the only place it can go
out = re.findall(r"\\S*solvability\\.json", prompt)[-1]
open(out, "w").write(os.environ["PEEK_REPORT"])
print(json.dumps({"type": "system", "subtype": "init", "model": "m"}))
print(json.dumps({"type": "result", "subtype": "success",
                  "terminal_reason": "end_turn", "result": "done",
                  "modelUsage": {"m": {"outputTokens": 10}}}))
'''


@pytest.fixture
def peeking_claude(tmp_path, monkeypatch):
    """A `claude` on PATH that opens the answer key and says whether it could."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(f"#!{sys.executable}\n" + PEEKING_CLAUDE)
    (bin_dir / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PEEK_RESULT", str(tmp_path / "peek.txt"))
    monkeypatch.setenv("PEEK_PROMPT", str(tmp_path / "prompt.txt"))
    monkeypatch.setenv("PEEK_REPORT", json.dumps(REPORT))
    return lambda: (tmp_path / "peek.txt").read_text()


def _args(work, **kw):
    base = dict(work=str(work), deck=None, workers=1, cpu_workers=None,
                force=False, dpi=110, model=None, timeout=1, api_retries=0)
    base.update(kw)
    return argparse.Namespace(**base)


def test_a_probe_that_tried_to_read_the_answer_key_would_fail(
        tmp_path, peeking_claude, monkeypatch):
    """The negative control in one assertion.  Reintroduce the hole — run the
    probe from the repository root, without the workspace — and this line goes
    from `FileNotFoundError` to `read it`."""
    _needs_mask()
    d = _deck(tmp_path)
    monkeypatch.setenv("PEEK_AT", str(d.delta))
    line = cli._solvable_one(d, _args(tmp_path / "work"))

    assert peeking_claude() == "FileNotFoundError"
    assert d.status_of("solvable") == "ok", line
    assert d.state()["solvable"]["barrier"] == "namespace+deny"
    assert pl.probe_record(d)["report_returned"] is True


def test_the_prompt_no_longer_hands_over_the_deck_directory(
        tmp_path, peeking_claude, monkeypatch):
    """The `ls -la work/deck0007/` that voided a real run was a probe looking
    at the directory the prompt had named four times, including as the place to
    write its report."""
    _needs_mask()
    d = _deck(tmp_path)
    monkeypatch.setenv("PEEK_AT", str(d.source))
    cli._solvable_one(d, _args(tmp_path / "work"))

    prompt = (tmp_path / "prompt.txt").read_text()
    assert str(d.root) not in prompt
    assert "solvability.json" in prompt
    assert not pl.barrier_breaches(d, d.root / "solvable.jsonl")


def test_a_barrier_that_cannot_be_established_stops_the_stage(
        tmp_path, peeking_claude, monkeypatch):
    """Nothing runs, nothing is judged, and the deck keeps no verdict — the one
    outcome that must never be "probe it anyway"."""
    d = _deck(tmp_path)
    monkeypatch.setattr(pl, "mask_available", lambda: (False, "no namespaces"))
    monkeypatch.setenv("PEEK_AT", str(d.delta))
    line = cli._solvable_one(d, _args(tmp_path / "work"))

    assert "FAILED" in line
    assert d.status_of("solvable") == "failed"
    assert not (tmp_path / "peek.txt").exists()
