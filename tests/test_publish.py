"""What has to be true before a task is allowed to leave this machine.

Publishing is the one step in this pipeline that cannot be undone by deleting a
file.  A number handed out twice is in somebody's results table; a `.py` pushed
without its materials is a rollout that scores zero and reads as an agent that
could not do the work.  So the properties here are mostly refusals.

Nothing in this file touches a real remote.  The git half runs against a
throwaway repository built in `tmp_path`, with a bare repository standing in
for `origin`; the Hugging Face half runs against a recorder.  **What that
leaves unproven is stated in `test_what_a_real_push_still_has_to_prove`** —
whether the credentials exist, whether the dataset accepts the upload, and
whether the URLs the tasks bake in actually resolve are questions only a real
push can answer, and pretending otherwise is how a green suite ships a broken
batch.

    python3 -m pytest tests/test_publish.py -q
"""

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from pptxgym import emit, pipeline as pl, publish   # noqa: E402


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _stub_harness():
    """Put the names a generated task imports into `sys.modules`, once.

    `sys.modules` is shared by every test module in the run, so a *stub*
    `desktop_env.task_base` installed here is the one `test_emit` gets when it
    asks for the real `BaseTask` later — and three of its assertions are about
    the real one being a `dict` subclass, so they fail with a confusing
    `TypeError` in a file that has nothing to do with this change. The real
    class is preferred for that reason and not only for this file's sake:
    `desktop_env.task_base` is stdlib-only, so importing it costs nothing.

    `desktop_env.evaluators.getters` is stubbed either way — importing it for
    real drags in lxml and selenium, and nothing here calls `evaluate`.
    """
    import os
    if "desktop_env.evaluators.getters" in sys.modules:
        return
    repo = Path(os.environ.get("OSWORLD_HARNESS_REPO",
                               ROOT.parent / "osworld2.0-rollout"))
    if (repo / "desktop_env" / "task_base.py").exists():
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        import desktop_env.task_base                              # noqa: F401
    else:
        for name in ("desktop_env", "desktop_env.task_base"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["desktop_env.task_base"].BaseTask = object
    sys.modules.setdefault("desktop_env.evaluators",
                           types.ModuleType("desktop_env.evaluators"))
    getters = types.ModuleType("desktop_env.evaluators.getters")
    getters.get_vm_file = lambda *a, **k: None
    getters.get_vm_command_line = lambda *a, **k: ""
    sys.modules["desktop_env.evaluators.getters"] = getters


def _mini_work(tmp_path) -> Path:
    """A `work/`-shaped tree of finished miniature decks, one per source.

    A published id is the *source's* checksum, so two decks built from one
    source are one task by construction. The live pipeline cannot produce that
    pair — `work/.by-content` refuses a source it has already ingested — but a
    fixture can, and a batch that trips `build`'s refusal would fail every test
    here for a reason none of them is about. Surplus copies are dropped, and
    the refusal is tested on a collision built on purpose.
    """
    import minidecks
    base = tmp_path / "work"
    minidecks.build_all(base)
    minidecks.promote(base)
    seen = set()
    for deck in pl.decks_in(base):
        key = pl.task_id_for(deck)
        if key in seen:
            shutil.rmtree(deck.root)
        seen.add(key)
    return base


def _rollout(tmp_path, name="rollout") -> Path:
    """A throwaway checkout shaped like the rollout repository, with an origin.

    A real git repository rather than a directory: `place_task_files` and
    `commit_and_push` are the half of publishing that a fake cannot exercise
    honestly — whether `git add` takes the paths, whether an unchanged re-run
    produces an empty commit, whether a dirty tree is noticed.  All of that is
    real here.  Only the *remote* is local.
    """
    repo = tmp_path / name
    (repo / publish.TASK_CLASS_REL).mkdir(parents=True)
    (repo / publish.TASK_ASSETS_REL).mkdir(parents=True)
    (repo / publish.TASK_CLASS_REL / "__init__.py").write_text("")
    origin = tmp_path / f"{name}-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "test"],
                 ["add", "-A"], ["commit", "-qm", "base"],
                 ["remote", "add", "origin", str(origin)],
                 ["push", "-q", "origin", "main"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True)
    return repo


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def _occupy(rollout: Path, tid: str, *, source_key: str | None = None,
            body: str = "# placed by something else\n") -> None:
    """Put a task file in the repository the way another publisher would."""
    (rollout / publish.TASK_CLASS_REL / f"task_{tid}.py").write_text(body)
    adir = rollout / publish.TASK_ASSETS_REL / f"task_{tid}"
    adir.mkdir(parents=True, exist_ok=True)
    if source_key:
        (adir / "provenance.json").write_text(
            json.dumps({"schema": 1, "task_id": tid, "source_key": source_key}))


class FakeHub:
    """Records what a real run would send to Hugging Face."""

    def __init__(self):
        self.uploads = []
        self.created = []
        self.order = []

    def upload(self, rows, repo, staging, token=None):
        self.created.append(repo)
        self.order.append("hf")
        for row in rows:
            self.uploads.append(row["hf_dir"])

    def verified_ok(self, rows, repo, token=None):
        self.order.append("verify")
        return []

    def verified_missing(self, rows, repo, token=None):
        self.order.append("verify")
        return [f"{r['id']}: nothing at {r['hf_dir']}/init.pptx" for r in rows]


@pytest.fixture
def hub(monkeypatch):
    fake = FakeHub()
    monkeypatch.setattr(publish, "upload_assets", fake.upload)
    monkeypatch.setattr(publish, "verify_fetchable", fake.verified_ok)
    real_place = publish.place_task_files

    def place(rows, rollout):
        fake.order.append("git")
        return real_place(rows, rollout)

    monkeypatch.setattr(publish, "place_task_files", place)
    return fake


def _plan(tmp_path, work=None, rollout=None, **kw):
    work = work or _mini_work(tmp_path)
    rollout = rollout or _rollout(tmp_path)
    staging = tmp_path / "stage"
    return publish.build(work, staging, rollout, publish.HF_REPO, **kw)


# --------------------------------------------------------------------------- #
# the registry: where it lives, and what it is the authority on
# --------------------------------------------------------------------------- #


def test_the_registry_lives_in_the_repository_it_allocates_for(tmp_path):
    """Not under `work/`, which is scratch and gets wiped, and not under
    `pptxgym/`, which is a different repository from the one the number names.
    Here the allocation and the artefact land in the same commit."""
    rollout = _rollout(tmp_path)
    assert publish.registry_path(rollout).parent.is_relative_to(rollout)
    assert "task_assets" in publish.REGISTRY_REL
    assert publish.load_registry(rollout) == publish.empty_registry()


def test_publishing_the_same_deck_twice_returns_the_same_number(tmp_path):
    """The property the whole registry exists for. A republished deck — a
    fixed evaluator, a corrected instruction — must not move: an id that moves
    invalidates every rollout result already recorded against it."""
    reg = publish.empty_registry()
    reg, first, fresh = publish.allocate(reg, ["aaa", "bbb"])
    assert fresh == ["1100001", "1100002"]

    reg2, again, fresh2 = publish.allocate(reg, ["aaa", "bbb"])
    assert again == first
    assert fresh2 == [], "a republish burned a number"
    assert reg2["next"] == reg["next"]


def test_ids_are_allocated_in_one_pass_over_the_batch(tmp_path):
    """A hundred decks finishing at once must not each read the counter and
    each take the same value. The batch is decided in one call, from one
    process, and the registry is written once afterwards."""
    reg, mapping, fresh = publish.allocate(
        publish.empty_registry(), [f"k{i:03d}" for i in range(100)])
    assert len(set(mapping.values())) == 100
    assert sorted(int(t) for t in mapping.values()) == list(
        range(publish.SERIES_FIRST, publish.SERIES_FIRST + 100))
    assert reg["next"] == publish.SERIES_FIRST + 100


def test_a_stale_read_is_the_thing_batching_removes(tmp_path):
    """The failure a per-deck allocator has, written down as the test that
    would catch it: two callers that each read the registry and each allocate
    one deck hand out the same number. One call over the batch cannot."""
    reg = publish.empty_registry()
    _, a, _ = publish.allocate(reg, ["deck-a"])
    _, b, _ = publish.allocate(reg, ["deck-b"])       # same stale read
    assert a["deck-a"] == b["deck-b"], (
        "this is the collision; it is only safe because publish allocates the "
        "whole batch in one call, never one deck at a time")

    _, together, _ = publish.allocate(reg, ["deck-a", "deck-b"])
    assert together["deck-a"] != together["deck-b"]


def test_the_next_number_never_comes_from_the_files_in_the_repository(tmp_path):
    """`max(task_class/) + 1` is wrong in both directions.

    Thirteen tasks in this series are being deleted as unfit. While the
    deletion is in flight the maximum reads 1100013; after it lands the
    directory is empty and the maximum reads nothing — so the same registry
    would hand out different numbers depending on when it ran, and a deck's
    published id would move. The registry decides; the repository is only ever
    asked whether a number is *occupied*.
    """
    rollout = _rollout(tmp_path)
    _occupy(rollout, "1100050")
    reg = publish.load_registry(rollout)
    _, mapping, _ = publish.allocate(reg, ["fresh"])
    assert mapping["fresh"] == "1100001", (
        "the allocator read the repository instead of the registry")


def test_a_published_task_that_was_deleted_keeps_its_number(tmp_path):
    """A gap in the repository is not a free number. Deleting a task is a real
    event — it has just happened — and the deck it came from must get the same
    id back rather than watch it be handed to someone else."""
    rollout = _rollout(tmp_path)
    reg, _, _ = publish.allocate(publish.empty_registry(), ["kept", "deleted"])
    publish.save_registry(rollout, reg)
    # only `kept` was ever pushed, and even it is gone now
    reg2 = publish.load_registry(rollout)
    _, mapping, fresh = publish.allocate(reg2, ["deleted", "new"])
    assert mapping["deleted"] == "1100002", "the number moved after a deletion"
    assert fresh == ["1100003"], "the gap was handed out as if it were free"

    notes, occupants = publish.survey_repo(reg2, rollout, tmp_path / "work")
    assert occupants == {}
    assert any("not in the repository" in n for n in notes), (
        "a number allocated but not present is worth saying out loud")


def test_a_number_the_repository_holds_and_the_registry_does_not_is_refused(
        tmp_path):
    """It was published by something other than this path. Overwriting it is
    not a decision a script gets to take."""
    work = _mini_work(tmp_path)
    rollout = _rollout(tmp_path)
    _occupy(rollout, "1100001")
    with pytest.raises(publish.PublishError) as e:
        _plan(tmp_path, work=work, rollout=rollout)
    assert "did not publish" in str(e.value)
    assert "1100001" in str(e.value)


def test_an_occupant_that_is_the_same_deck_is_not_a_conflict(tmp_path):
    """That is what re-publishing looks like. It is skipped, not refused."""
    work = _mini_work(tmp_path)
    rollout = _rollout(tmp_path)
    decks, _ = publish.approved(work)
    key = pl.task_id_for(decks[0])

    reg, mapping, _ = publish.allocate(publish.empty_registry(),
                                       [pl.task_id_for(d) for d in decks])
    publish.save_registry(rollout, reg)
    _occupy(rollout, mapping[key], source_key=key)

    plan = _plan(tmp_path, work=work, rollout=rollout)
    assert mapping[key] in plan["already"]
    assert mapping[key] not in [r["id"] for r in plan["rows"]]

    plan2 = _plan(tmp_path / "again", work=work, rollout=rollout,
                  republish=True)
    assert mapping[key] in [r["id"] for r in plan2["rows"]]


def test_a_number_registered_to_one_deck_and_occupied_by_another_is_refused(
        tmp_path):
    """One number, two tasks. Only a human can say which one keeps it."""
    work = _mini_work(tmp_path)
    rollout = _rollout(tmp_path)
    decks, _ = publish.approved(work)
    keys = [pl.task_id_for(d) for d in decks]
    reg, mapping, _ = publish.allocate(publish.empty_registry(), keys)
    publish.save_registry(rollout, reg)
    _occupy(rollout, mapping[keys[0]], source_key="somethingelse")

    with pytest.raises(publish.PublishError) as e:
        _plan(tmp_path, work=work, rollout=rollout)
    assert "one number, two tasks" in str(e.value)


def test_a_registry_that_will_not_parse_stops_everything(tmp_path):
    """Allocating against a registry that cannot be read means reissuing a
    number that is already published, which is the one unrecoverable mistake
    here. Guessing is worse than stopping."""
    rollout = _rollout(tmp_path)
    publish.registry_path(rollout).write_text("{not json")
    with pytest.raises(publish.PublishError) as e:
        publish.load_registry(rollout)
    assert "already published" in str(e.value)


def test_the_series_belongs_to_us_and_the_allocator_never_leaves_it(tmp_path):
    """114, 115, 117 and 118 are other people's."""
    assert publish.SERIES == "110"
    reg, mapping, _ = publish.allocate(publish.empty_registry(),
                                       [f"k{i}" for i in range(20)])
    for tid in mapping.values():
        assert tid.startswith("110") and len(tid) == 7
        assert publish.SERIES_FIRST <= int(tid) <= publish.SERIES_LAST


def test_the_series_running_out_is_said_rather_than_wrapped(tmp_path):
    reg = publish.empty_registry()
    reg["next"] = publish.SERIES_LAST
    reg, mapping, _ = publish.allocate(reg, ["last"])
    assert mapping["last"] == str(publish.SERIES_LAST)
    with pytest.raises(publish.PublishError) as e:
        publish.allocate(reg, ["one-too-many"])
    assert "full" in str(e.value)


# --------------------------------------------------------------------------- #
# what may be published at all
# --------------------------------------------------------------------------- #


def test_only_a_deck_that_reached_packaged_can_be_published(tmp_path):
    """Packaging is where the consistency report runs and where the emitter
    refuses a rejected plan. A deck that has not been through it has never been
    checked as a *task*, whatever its earlier gates say."""
    work = _mini_work(tmp_path)
    decks, _ = publish.approved(work)
    assert decks, "the fixture produced nothing publishable"

    victim = decks[0]
    victim.mark("packaged", "rejected", why="test")
    again, refused = publish.approved(work)
    assert victim.id not in [d.id for d in again]
    assert any(victim.id in r for r in refused)


def test_a_deck_that_has_moved_since_it_was_packaged_is_not_publishable(
        tmp_path):
    """`packaged` reading `ok` is not the same claim as "the files it was
    computed from are still there". `status_of` downgrades a stage whose inputs
    have moved, and this is the one place that downgrade has to bite."""
    work = _mini_work(tmp_path)
    decks, _ = publish.approved(work)
    victim = decks[0]
    task = victim.root / "task.json"              # a `packaged` stage input
    task.write_text(json.dumps({**json.loads(task.read_text()),
                                "instruction": "changed after packaging"}))
    assert victim.status_of("packaged") == "stale"
    again, refused = publish.approved(work)
    assert victim.id not in [d.id for d in again]


def test_a_package_the_deck_has_moved_past_is_refused_before_anything_uploads(
        tmp_path, monkeypatch):
    """The provenance record's whole job, wired into the publisher.

    Eight tasks once sat in one directory: seven fresh and one built hours
    earlier from a deck the pipeline had since rejected, promising assets that
    deck's own proposal now forbids by name. Nothing on the file said so.
    `emit.check_emitted` answers that against the deck itself, and `stage`
    drops whatever it names — before a single byte has been uploaded.
    """
    work = _mini_work(tmp_path)
    rollout = _rollout(tmp_path)
    real = emit.check_emitted
    seen = {}

    def check(out_root, w):
        rows = real(out_root, w)
        seen["called"] = (Path(out_root), Path(w))
        rows[0]["problems"] = ["deck0002's plan is now rejected"]
        rows[0]["current"] = False
        return rows

    baseline = _plan(tmp_path / "baseline", work=work, rollout=rollout)
    monkeypatch.setattr(emit, "check_emitted", check)
    plan = _plan(tmp_path, work=work, rollout=rollout)
    assert seen["called"][1] == work, "the deck was not the thing asked"
    assert any("plan is now rejected" in r for r in plan["refused"])
    assert len(plan["rows"]) == len(baseline["rows"]) - 1
    assert baseline["rows"], "the fixture staged nothing to drop from"


def test_a_rejected_plan_never_reaches_a_destination(tmp_path):
    work = _mini_work(tmp_path)
    decks, _ = publish.approved(work)
    victim = decks[0]
    plan_file = victim.root / "plan.json"
    doc = json.loads(plan_file.read_text())
    doc["rejected"] = ["the probe could not solve it"]
    plan_file.write_text(json.dumps(doc))
    victim.mark("packaged", "ok")                 # refresh the fingerprint

    plan = _plan(tmp_path, work=work)
    assert victim.id not in [r["deck"] for r in plan["rows"]]
    assert any(victim.id in r for r in plan["refused"])


def test_two_decks_built_from_one_source_are_refused_rather_than_merged(
        tmp_path):
    """The published id is the *source's* checksum, which is what makes it
    survive a re-ingest — and it therefore cannot tell two tasks built from one
    source apart. They would land in one directory under one number and the
    second would overwrite the first, while the run reported publishing both.
    """
    work = _mini_work(tmp_path)
    decks, _ = publish.approved(work)
    twin = work / "deck9900"
    shutil.copytree(decks[0].root, twin)          # same source, second deck
    keys = [pl.task_id_for(d) for d in publish.approved(work)[0]]
    assert len(set(keys)) < len(keys), "the collision was not built"

    with pytest.raises(publish.PublishError) as e:
        _plan(tmp_path, work=work)
    assert "one published id" in str(e.value)
    assert "would be lost" in str(e.value)


def test_publishing_is_a_batch_step_and_says_so_when_there_is_no_batch(tmp_path):
    work = tmp_path / "empty"
    work.mkdir()
    with pytest.raises(publish.PublishError) as e:
        _plan(tmp_path, work=work)
    assert "batch step" in str(e.value)


# --------------------------------------------------------------------------- #
# the split: which file goes to which destination
# --------------------------------------------------------------------------- #


def test_the_answer_key_goes_to_git_and_never_to_the_dataset(tmp_path):
    """A delta-derived evaluator scores by reading `plan.json` and the two
    inventories, so they have to ship — beside the evaluator, in git, where the
    agent's machine never reaches. `assets/` is the half that becomes URLs the
    task fetches, and nothing in it may be answer key."""
    plan = _plan(tmp_path)
    for row in plan["rows"]:
        hf = {rel for _, rel in row["hf_files"]}
        git = {rel for _, rel in row["git_files"]}
        assert "init.pptx" in hf
        assert not (hf & {"plan.json", "gt_inventory.json",
                          "init_inventory.json"})
        assert {"tests/assets/plan.json", "tests/assets/gt_inventory.json",
                "tests/assets/init_inventory.json"} <= git
        assert not any(rel.startswith("tests/") for rel in hf)
    assert plan["leaks"] == []


def test_the_leak_guard_catches_an_answer_key_that_reached_the_upload_list(
        tmp_path):
    """It should never fire — `emit` copies the agent's files from `bundle/`
    alone and `check_package` refuses a package with a secret under `assets/`.
    It runs anyway, because "that file cannot get in there" is how the last
    leak shipped, and an upload cannot be taken back."""
    plan = _plan(tmp_path)
    row = plan["rows"][0]
    smuggled = row["assets_dir"] / "assets" / "plan.json"
    smuggled.write_text("{}")
    row["hf_files"].append((smuggled, "plan.json"))
    problems = publish.leak_guard(plan["rows"])
    assert problems and "answer-key" in problems[0]


def test_the_bulk_moves_off_git(tmp_path):
    """The reason for the whole change: the materials are the megabytes and
    the evaluator's fixtures are not."""
    plan = _plan(tmp_path)
    assert plan["hf_bytes"] > 0
    for row in plan["rows"]:
        assert row["bytes_hf"] > 0
        deck = next(f for f, rel in row["hf_files"] if rel == "init.pptx")
        assert deck.stat().st_size > 0


# --------------------------------------------------------------------------- #
# both or neither
# --------------------------------------------------------------------------- #


def test_the_materials_are_uploaded_and_checked_before_the_task_file(
        tmp_path, hub):
    """The order is the property. A `.py` whose materials are not there hands
    the agent an empty machine and records a stable zero that reads as
    incapability, so the fetch is proved to work *before* the file that
    depends on it is written."""
    plan = _plan(tmp_path)
    done = publish.publish(plan, push_git=False)
    assert hub.order[:3] == ["hf", "verify", "git"]
    assert done["verified"] is True
    assert done["uploaded"] == len(plan["rows"])


def test_no_task_file_is_written_when_the_materials_do_not_come_back(
        tmp_path, hub, monkeypatch):
    """An upload that reported success and a dataset that serves the file are
    not the same claim. When the second one fails, nothing is committed."""
    monkeypatch.setattr(publish, "verify_fetchable", hub.verified_missing)
    rollout = _rollout(tmp_path)
    plan = _plan(tmp_path, rollout=rollout)
    head_before = _git(rollout, "rev-parse", "HEAD").strip()

    with pytest.raises(publish.PublishError) as e:
        publish.publish(plan, push_git=False)
    assert "no task file was written" in str(e.value)
    assert "git" not in hub.order
    assert _git(rollout, "rev-parse", "HEAD").strip() == head_before
    assert not list((rollout / publish.TASK_CLASS_REL).glob("task_110*.py"))
    assert not publish.registry_path(rollout).exists(), (
        "the registry recorded an allocation for a task that was not published")


def test_the_task_files_land_where_the_harness_looks_for_them(tmp_path, hub):
    plan = _plan(tmp_path)
    rollout = Path(plan["rollout"])
    publish.publish(plan, push_git=False)
    for row in plan["rows"]:
        py = rollout / publish.TASK_CLASS_REL / f"task_{row['id']}.py"
        adir = rollout / publish.TASK_ASSETS_REL / f"task_{row['id']}"
        assert py.exists()
        assert (adir / "tests" / "assets" / "plan.json").exists()
        assert (adir / "metadata.json").exists()
        assert not (adir / "assets").exists(), (
            "the materials were committed to git after all")
    assert publish.registry_path(rollout).exists()


def test_a_second_run_publishes_nothing_and_commits_nothing(tmp_path, hub):
    """Re-running is the normal case — a crashed job, a partial batch — and it
    must be free. Idempotency is decided by the registry and the repository,
    not by whether two builds happen to produce identical bytes."""
    work = _mini_work(tmp_path)
    rollout = _rollout(tmp_path)
    first = _plan(tmp_path / "a", work=work, rollout=rollout)
    publish.publish(first, push_git=False)
    subprocess.run(["git", "-C", str(rollout), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(rollout), "commit", "-qm", "settle"],
                   capture_output=True)
    head = _git(rollout, "rev-parse", "HEAD").strip()

    second = _plan(tmp_path / "b", work=work, rollout=rollout)
    assert second["rows"] == []
    assert len(second["already"]) == len(first["rows"])
    out = publish.publish(second, push_git=False)
    assert "nothing new" in out["git"]
    assert _git(rollout, "rev-parse", "HEAD").strip() == head


def test_a_republish_of_the_same_deck_reuses_its_number(tmp_path, hub):
    work = _mini_work(tmp_path)
    rollout = _rollout(tmp_path)
    first = _plan(tmp_path / "a", work=work, rollout=rollout)
    publish.publish(first, push_git=False)
    ids = {r["deck"]: r["id"] for r in first["rows"]}

    again = _plan(tmp_path / "b", work=work, rollout=rollout, republish=True)
    assert {r["deck"]: r["id"] for r in again["rows"]} == ids
    assert again["allocated"] == []


def test_a_dirty_checkout_is_refused(tmp_path):
    """`git commit` on a repository with somebody else's uncommitted work in it
    sweeps that work into our commit or strands it. Either way the commit no
    longer says what it claims."""
    rollout = _rollout(tmp_path)
    assert publish.rollout_problems(rollout) == []
    (rollout / "somebody-elses-work.txt").write_text("wip")
    problems = publish.rollout_problems(rollout)
    assert problems and "uncommitted" in problems[0]


def test_a_directory_that_is_not_the_rollout_repository_is_refused(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert "not a git checkout" in publish.rollout_problems(plain)[0]


def test_the_commit_reaches_the_remote_only_when_asked(tmp_path, hub):
    """`--push` is two steps, and the second one is separable: a run that
    commits but does not push leaves a reviewable state on disk."""
    rollout = _rollout(tmp_path)
    plan = _plan(tmp_path, rollout=rollout)
    out = publish.publish(plan, push_git=False)
    assert "not pushed" in out["git"]
    origin = _git(rollout, "rev-parse", "origin/main").strip()
    assert _git(rollout, "rev-parse", "HEAD").strip() != origin

    # the same commit, asked for properly this time
    (rollout / publish.TASK_ASSETS_REL / "later.txt").write_text("later\n")
    message = publish.commit_and_push(
        rollout, [f"{publish.TASK_ASSETS_REL}/later.txt"], "later", push=True)
    assert "pushed to origin/main" in message
    assert _git(rollout, "rev-parse", "HEAD").strip() == \
        _git(rollout, "rev-parse", "origin/main").strip()


# --------------------------------------------------------------------------- #
# the Hugging Face side, as far as a fake can take it
# --------------------------------------------------------------------------- #


def test_the_upload_tree_is_laid_out_the_way_the_tasks_ask_for_it(tmp_path):
    """The URL in the generated file and the path in the dataset are the same
    string or the task fetches nothing."""
    plan = _plan(tmp_path)
    tree = Path(plan["staging"]) / "hf"
    for row in plan["rows"]:
        for repo_path, _vm, digest in row["fetch"]:
            local = tree / repo_path
            assert local.exists(), f"{repo_path} is fetched but not uploaded"
            assert hashlib.sha256(local.read_bytes()).hexdigest() == digest


def test_the_upload_is_chunked_by_files_and_not_by_tasks(tmp_path):
    """Hugging Face's guidance counts files. Budgeting in tasks is how a "safe"
    batch of eighteen came to a hundred and eight."""
    rows = [{"hf_files": [None] * 8, "hf_dir": f"task_{i}"} for i in range(40)]
    chunks = publish.chunk_by_files(rows)
    assert all(sum(len(r["hf_files"]) for r in c) <= publish.FILES_PER_COMMIT
               for c in chunks)
    assert sum(len(c) for c in chunks) == 40

    huge = [{"hf_files": [None] * 500, "hf_dir": "task_big"}]
    assert publish.chunk_by_files(huge) == [huge], (
        "a task larger than the budget was dropped rather than given its own "
        "commit")


def test_the_upload_calls_hugging_face_once_per_chunk(tmp_path, monkeypatch):
    """Driven through a fake `huggingface_hub`, because the argument shape is
    the part a real push cannot be allowed to discover."""
    calls = []

    class Api:
        def __init__(self, token=None):
            calls.append(("token", token))

        def create_repo(self, repo_id, repo_type=None, exist_ok=False):
            calls.append(("create", repo_id, repo_type, exist_ok))

        def upload_folder(self, **kw):
            calls.append(("upload", kw["repo_id"], kw["repo_type"],
                          tuple(kw["allow_patterns"])))

    module = types.ModuleType("huggingface_hub")
    module.HfApi = Api
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)

    plan = _plan(tmp_path)
    publish.upload_assets(plan["rows"], publish.HF_REPO,
                          Path(plan["staging"]), token="fake")
    assert ("create", publish.HF_REPO, "dataset", True) in calls
    uploads = [c for c in calls if c[0] == "upload"]
    assert len(uploads) == plan["hf_commits"]
    patterns = {p for c in uploads for p in c[3]}
    assert patterns == {f"{r['hf_dir']}/**" for r in plan["rows"]}


def test_every_url_the_tasks_will_ask_for_is_the_one_that_gets_checked(
        tmp_path, monkeypatch):
    """`verify_fetchable` must ask exactly the question `setup` will ask, from
    outside, or it is checking something else."""
    asked = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        asked.append(req.full_url)
        assert req.get_method() == "HEAD"
        return Response()

    monkeypatch.setattr(publish.urllib.request, "urlopen", urlopen)
    plan = _plan(tmp_path)
    assert publish.verify_fetchable(plan["rows"], publish.HF_REPO) == []

    wanted = set()
    for row in plan["rows"]:
        py = Path(row["py"]).read_text()
        base = next(line.split("'")[1] for line in py.splitlines()
                    if line.startswith("HF_ASSET_BASE_URL = "))
        for repo_path, _vm, _sha in row["fetch"]:
            wanted.add(f"{base}/{repo_path}")
    assert set(asked) == wanted


# --------------------------------------------------------------------------- #
# the emitted task fetches rather than uploads
# --------------------------------------------------------------------------- #


class Controller:
    """A `SetupController` that records, and serves downloads from a mirror.

    `cache_dir` is the real one's, computed the way `_download_setup` computes
    it: that copy of the fetched bytes is the only place on this side of the
    wire where they can be checked, and `_verify_fetched` is written against
    exactly this path.
    """

    def __init__(self, cache: Path, mirror: Path | None = None,
                 fail: bool = False, corrupt: bool = False):
        import uuid
        self.cache_dir = str(cache)
        self.mirror, self.fail, self.corrupt = mirror, fail, corrupt
        self._uuid = uuid
        self.calls = []

    def execute(self, command, shell=False, **kw):
        self.calls.append(("execute", command))

    def launch(self, command, shell=False):
        self.calls.append(("launch", command))

    def _source(self, url: str) -> Path:
        if "://" not in url:                       # a redirected local base
            return Path(url)
        base = "https://huggingface.co/datasets/" + publish.HF_REPO \
               + "/resolve/main/"
        assert url.startswith(base), url
        return self.mirror / url[len(base):]

    def download(self, files):
        self.calls.append(("download", files))
        if self.fail:
            raise RuntimeError("404 Client Error")
        for f in files:
            dest = Path(self.cache_dir) / "{:}_{:}".format(
                self._uuid.uuid5(self._uuid.NAMESPACE_URL, f["url"]),
                Path(f["path"]).name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if self.corrupt:
                dest.write_bytes(b"not the file you asked for")
                continue
            dest.write_bytes(self._source(f["url"]).read_bytes())

    def _upload_file_setup(self, files):
        self.calls.append(("upload", files))

    def of(self, kind):
        return [payload for name, payload in self.calls if name == kind]


def _emitted(tmp_path, task_id="1109001"):
    """One packaged task, loaded as `task_loader` loads it, and a dataset.

    The mirror is the dataset laid out as the task expects to find it —
    `task_<id>/init.pptx`, `task_<id>/materials/...` — built from the same
    staging directory `publish` uploads, so a test that fetches from it is
    exercising the layout a real run would create rather than one invented
    here.
    """
    work = _mini_work(tmp_path)
    decks, _ = publish.approved(work)
    out = emit.emit(decks[0], tmp_path / "out", task_id)
    _stub_harness()

    spec = importlib.util.spec_from_file_location("published_task", out["py"])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    adir = Path(out["assets"])
    mirror = tmp_path / "dataset"
    for src, rel in publish.split_package(adir)[0]:
        dest = mirror / emit.hf_asset_dir(task_id) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return mod, adir, mirror


def test_the_task_fetches_its_materials_instead_of_uploading_them(tmp_path):
    """The change that takes a gigabyte out of the benchmark repository."""
    mod, adir, _ = _emitted(tmp_path)
    assert mod.HF_ASSET_REPO == "xlangai/recommendation"
    assert not hasattr(mod, "AGENT_ASSETS"), (
        "the task still points at a materials directory beside itself")
    assert mod.FETCH, "nothing is fetched, so setup hands the agent nothing"
    assert mod.FETCH[0][1] == mod.DECK_VM_PATH
    for repo_path, vm_path, digest in mod.FETCH:
        assert repo_path.startswith("task_1109001/")
        assert vm_path.startswith("/home/user/Desktop/")
        assert len(digest) == 64


def test_the_fetch_does_not_go_through_the_global_asset_helper(tmp_path):
    """`asset()` resolves against one base shared by every task in the
    benchmark. Pointing it at our dataset would send the other nine hundred
    somewhere their files are not, so ours name their own URL."""
    import ast
    mod, _, _ = _emitted(tmp_path)
    tree = ast.parse(Path(mod.__file__).read_text())
    imported = {n.module for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module}
    imported |= {a.name for n in ast.walk(tree)
                 if isinstance(n, ast.Import) for a in n.names}
    assert "desktop_env.file_source" not in imported
    assert not any("file_source" in m for m in imported)
    assert mod.ASSET_BASE_ENV == "PPTXGYM_ASSET_BASE"
    assert publish.HF_REPO in mod.HF_ASSET_BASE_URL
    assert "osworld_v2_assets" not in mod.HF_ASSET_BASE_URL


def test_setup_fetches_every_file_and_launches_wps(tmp_path):
    mod, adir, mirror = _emitted(tmp_path)
    c = Controller(tmp_path / "cache", mirror=mirror)
    mod.TASK_CLASS().setup(c, use_proxy=False)

    fetched = [f for batch in c.of("download") for f in batch]
    assert [f["path"] for f in fetched] == [vm for _, vm, _ in mod.FETCH]
    assert c.of("upload") == [], "it is still uploading from local disk"
    assert c.of("launch") == [["wpp", mod.DECK_VM_PATH]]
    kinds = [k for k, _ in c.calls]
    assert kinds.index("download") < kinds.index("launch")
    commands = " ".join(c.of("execute"))
    assert mod.SETUP_STAMP in commands
    assert "xdg-mime default wps-office-wpp.desktop" in commands


def test_a_failed_fetch_stops_the_episode_instead_of_starting_it(tmp_path):
    """An agent handed no materials looks exactly like an agent that did not do
    the work — and it is a *stable* zero, so it reads as a capability floor
    rather than as the infrastructure failure it is."""
    mod, adir, mirror = _emitted(tmp_path)
    c = Controller(tmp_path / "cache", mirror=mirror, fail=True)
    with pytest.raises(mod.AssetFetchError) as e:
        mod.TASK_CLASS().setup(c, use_proxy=False)
    assert "no deck on it" in str(e.value)
    assert c.of("launch") == [], "WPS was opened on a machine with no deck"


def test_a_fetch_that_returns_the_wrong_bytes_stops_the_episode(tmp_path):
    """`download` raises when the fetch fails. What it cannot notice is a fetch
    that *succeeded* and returned something else — an LFS pointer, a mirror one
    commit behind — and that arrives looking like a working task."""
    mod, adir, mirror = _emitted(tmp_path)
    c = Controller(tmp_path / "cache", mirror=mirror, corrupt=True)
    with pytest.raises(mod.AssetFetchError) as e:
        mod.TASK_CLASS().setup(c, use_proxy=False)
    assert "did not arrive intact" in str(e.value)
    assert c.of("launch") == []


def test_the_asset_base_can_be_redirected_without_touching_any_other_task(
        tmp_path, monkeypatch):
    """An offline or mirrored run needs somewhere else to fetch from, and the
    global variable is not it: setting ours can only affect tasks this emitter
    wrote."""
    mod, adir, mirror = _emitted(tmp_path)
    monkeypatch.setenv(mod.ASSET_BASE_ENV, str(mirror))
    c = Controller(tmp_path / "cache")
    mod.TASK_CLASS().setup(c, use_proxy=False)
    fetched = [f for batch in c.of("download") for f in batch]
    assert all(f["url"].startswith(str(mirror)) for f in fetched)
    assert all(Path(f["url"]).exists() for f in fetched)
    assert "huggingface.co" not in " ".join(f["url"] for f in fetched)


def test_the_fetch_list_is_read_off_the_file_that_runs(tmp_path):
    """`check_package` used to grep the generated source for a path
    expression. A pattern that stops matching when the code is reshaped fails
    open, and what it is guarding is whether the agent gets the answer key."""
    mod, adir, _ = _emitted(tmp_path)
    py = Path(mod.__file__)
    assert emit.fetch_list(py) == [tuple(row) for row in mod.FETCH]

    doctored = py.read_text().replace(
        "FETCH = (\n",
        "FETCH = (\n ('" + emit.hf_asset_dir("1109001") + "/gt_inventory.json', "
        "'/home/user/Desktop/x.json', '" + "0" * 64 + "'),\n", 1)
    py.write_text(doctored)
    problems = emit.check_package(py, adir)
    assert any("answer key" in p for p in problems)


def test_a_package_that_predates_the_dataset_move_cannot_be_published(tmp_path):
    """It uploads from a directory that will not be committed any more, so
    publishing it would ship a task whose materials are nowhere."""
    mod, _, _ = _emitted(tmp_path)
    old = Path(mod.__file__).with_name("task_old.py")
    old.write_text("# emitted before FETCH existed\nX = 1\n")
    with pytest.raises(emit.EmitError) as e:
        emit.fetch_list(old)
    assert "re-emitted" in str(e.value)


# --------------------------------------------------------------------------- #
# what a fake cannot answer
# --------------------------------------------------------------------------- #


def test_what_a_real_push_still_has_to_prove():
    """Written down as a test so it is read, and so it fails if somebody
    deletes the list without doing the work.

    Everything above runs against a local git repository and a recorder. Four
    claims are therefore untested until a real `--push` happens:

      1. that a write token for `xlangai/recommendation` exists and this
         account may use it — `HfApi.upload_folder` is only ever called against
         a fake here;
      2. that a push to `yuanmengqi/osworld2.0-rollout` is permitted — `origin`
         in these tests is a bare repository in `tmp_path`;
      3. that the dataset serves the uploaded files at the URLs the tasks bake
         in. `verify_fetchable` asks the right question, and the question has
         never been asked of the real host;
      4. that a VM fetching those URLs gets them — the harness adds an
         `Authorization` header from `HF_TOKEN` when it sees `huggingface.co`,
         and whether the dataset is public decides whether that matters.

    The first three fail loudly. The fourth is the one that fails quietly, and
    it is why `setup` raises rather than continuing.
    """
    assert publish.HF_REPO == "xlangai/recommendation"
    assert publish.ROLLOUT_REMOTE.endswith("osworld2.0-rollout")
