"""What the last gate before git has to be right about.

This stage is the one that spends money and the one that makes a claim about
somebody else's task, so two properties dominate everything else here:

* **a broken task and a broken instance must never be recorded as the same
  thing.** Both come back as a non-zero exit and a `setup_failed`-shaped
  report. One of them means "fix this task"; the other means "run it again".
  Half of this file is that distinction, driven by the two `result.json`
  payloads we have actually seen — a 404 on a missing Hugging Face asset
  (task) and a 502 while uploading (infrastructure).
* **a quota refusal is not a verdict on the task.** The vCPU ceiling cannot be
  read from this account, so the pool discovers it; a run that narrowed must
  not leave behind a pile of tasks marked broken.

Nothing here starts an instance. The AWS half is driven through an injected
`smoke`, and the subprocess half through a shim that stands in for `uv` — so
the argv, the deadline and the process-group kill are exercised for real, on a
fake runner. **`test_what_a_real_vm_run_still_has_to_prove` says what that
leaves open.**

    python3 -m pytest tests/test_vmsmoke.py -q
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from pptxgym.delivery import publish, vmsmoke             # noqa: E402


# --------------------------------------------------------------------------- #
# the two payloads we have actually seen
# --------------------------------------------------------------------------- #


def _result(**kw):
    base = {"task_id": "1100007", "status": "setup_failed", "success": False,
            "error": None, "error_type": None, "traceback": None,
            "phase": "reset"}
    base.update(kw)
    return {"returncode": 1, "timed_out": False, "seconds": 210.0,
            "result": base, "stderr_tail": ""}


#: A stock OSWorld task whose asset is not in the dataset. The task is broken.
FOUR_OH_FOUR = _result(
    error="Failed to download task_example/initial.txt: 404 Client Error: Not "
          "Found for url: https://huggingface.co/datasets/xlangai/"
          "osworld_v2_assets/resolve/main/task_example/initial.txt",
    error_type="EnvironmentSetupError")

#: One of ours, failing while uploading its deck. The task is fine.
FIVE_OH_TWO = _result(
    error="Setup failed: 502 Server Error: Bad Gateway for url: "
          "http://10.0.1.7:5000/setup/upload_file",
    error_type="EnvironmentSetupError",
    traceback="Traceback (most recent call last):\n"
              "  File 'setup.py', line 1, in _upload_file_setup\n"
              "FileNotFoundError: /tmp/cache/deck.pptx\n")


def test_a_404_and_a_502_are_not_the_same_failure(tmp_path):
    """The whole reason this module exists.

    Same exit status, same `setup_failed`, opposite meanings: one task must be
    fixed and the other must be re-run. A summary that calls them both
    "failed" has thrown away the only information in the run.
    """
    broken = vmsmoke.classify(FOUR_OH_FOUR)
    infra = vmsmoke.classify(FIVE_OH_TWO)
    assert broken.kind == vmsmoke.TASK_BROKEN
    assert infra.kind == vmsmoke.INFRASTRUCTURE
    assert "404" in broken.why
    assert not infra.capacity, "a 502 is not a reason to narrow the pool"


#: The same 404, as the harness *actually* reports it. Measured on a real run
#: (task_000000, 2026-08-05): `_download_setup` retries ten times and then
#: raises a message with the status stripped out, so `result.json` cannot tell
#: a missing file from a dataset outage. The deciding fact survives in one
#: place only — `setup.log`.
REAL_404 = {
    "returncode": 1, "timed_out": False, "seconds": 261.0,
    "result": {
        "status": "error_in_reset", "success": False, "error_type": "Exception",
        "error": "Setup step 1 failed: _download_setup - Failed to download "
                 "https://huggingface.co/datasets/xlangai/osworld_v2_assets/"
                 "resolve/main/task_example/initial.txt. No retries left.",
        "traceback": "requests.exceptions.RequestException: Failed to download "
                     "https://huggingface.co/datasets/xlangai/osworld_v2_"
                     "assets/resolve/main/task_example/initial.txt. No retries "
                     "left.\n"},
    "log_causes": [
        "2026-08-05 19:47:21,463 [ERROR] desktopenv.setup: Failed to download "
        "https://huggingface.co/datasets/xlangai/osworld_v2_assets/resolve/"
        "main/task_example/initial.txt caused by 404 Client Error: Not Found "
        "for url: https://huggingface.co/datasets/xlangai/osworld_v2_assets/"
        "resolve/main/task_example/initial.txt. Retrying... (0 attempts left)",
        "2026-08-05 19:47:21,463 [ERROR] desktopenv.setup: SETUP FAILED at "
        "step 1/3: _download_setup({'files': [...]})"],
}


def test_the_status_that_decides_is_not_in_result_json(tmp_path):
    """Measured, not assumed, and it is why `setup.log` is read at all.

    OSWorld's downloader retries ten times and raises `Failed to download
    <url>. No retries left.` — the HTTP status, which is the entire difference
    between "this task names a file that is not there" and "the dataset was
    down for a minute", never reaches `result.json`.

    Without the log this check refuses to attribute the failure, which is the
    right answer to a question it cannot see; with the log it can say plainly
    that the task is broken.
    """
    blind = dict(REAL_404, log_causes=[])
    assert vmsmoke.classify(blind).kind == vmsmoke.UNATTRIBUTED
    assert vmsmoke.classify(REAL_404).kind == vmsmoke.TASK_BROKEN


def test_the_same_failure_with_a_503_in_the_log_is_not_the_task(tmp_path):
    """The pair. Identical `result.json`, opposite verdicts, and the only thing
    that differs is one line of a log the runner nearly threw away."""
    outage = dict(REAL_404, log_causes=[
        REAL_404["log_causes"][0].replace("404 Client Error: Not Found",
                                          "503 Server Error: Service "
                                          "Unavailable")])
    assert vmsmoke.classify(outage).kind == vmsmoke.INFRASTRUCTURE
    assert vmsmoke.classify(REAL_404).kind == vmsmoke.TASK_BROKEN


def test_only_the_cause_lines_of_the_log_are_read(tmp_path):
    """A setup log is mostly the boot loop, which logs connection refusals as a
    matter of routine while it waits for the VM's server. Scanning the tail
    would attribute every task failure to the infrastructure — the exact
    mistake this module exists to stop."""
    log = tmp_path / "setup.log"
    log.write_text(
        "\n".join(["[INFO] waiting for server", "ConnectionRefusedError: [111]",
                   "Max retries exceeded with url: /screenshot"] * 50
                  + ["[ERROR] desktopenv.setup: Failed to download x caused by "
                     "404 Client Error: Not Found for url: x"]))
    causes = vmsmoke.cause_lines(log)
    assert len(causes) == 1 and "404" in causes[0]
    assert not any("Connection" in line for line in causes)

    raw = dict(REAL_404, log_causes=causes)
    assert vmsmoke.classify(raw).kind == vmsmoke.TASK_BROKEN, (
        "the boot loop's connection noise was read as the cause")


def test_a_log_that_is_not_there_is_not_an_error(tmp_path):
    assert vmsmoke.cause_lines(tmp_path / "nothing.log") == []


def test_the_proximate_error_decides_and_not_the_traceback(tmp_path):
    """The 502 arrives with a `FileNotFoundError` further down the same
    traceback — the cached file the failed upload never wrote. A scan over the
    whole blob reads the consequence and calls the task broken."""
    assert "FileNotFoundError" in FIVE_OH_TWO["result"]["traceback"]
    assert vmsmoke.classify(FIVE_OH_TWO).kind == vmsmoke.INFRASTRUCTURE

    # and when the message says nothing, the traceback is still read
    only_tb = _result(error="", error_type="",
                      traceback="ModuleNotFoundError: no module named x")
    assert vmsmoke.classify(only_tb).kind == vmsmoke.TASK_BROKEN

    # the reverse direction, which is what makes the *order* load-bearing
    # rather than a consequence of how the signatures happen to be sorted: a
    # task that refused its own materials, whose traceback happens to mention a
    # connection that was retried, is still a broken task.
    noisy = _result(
        error="AssetFetchError: the deck did not arrive intact",
        error_type="AssetFetchError",
        traceback="urllib3.exceptions.ProtocolError: Connection reset by peer\n"
                  "  ... retrying (2 left)\n")
    assert vmsmoke.classify(noisy).kind == vmsmoke.TASK_BROKEN


@pytest.mark.parametrize("code", ["VcpuLimitExceeded", "InstanceLimitExceeded",
                                  "InsufficientInstanceCapacity"])
def test_the_three_errors_that_mean_we_asked_for_too_much(code):
    """Named in the brief, and the only ones that narrow the pool: they are the
    only ones narrowing the pool can fix."""
    v = vmsmoke.classify(_result(
        error=f"An error occurred ({code}) when calling the RunInstances "
              f"operation: You have requested more vCPU capacity than your "
              f"current vCPU limit allows",
        error_type="ClientError"))
    assert v.kind == vmsmoke.INFRASTRUCTURE
    assert v.capacity is True


def test_a_pass_is_a_pass():
    ok = {"returncode": 0, "timed_out": False, "seconds": 190.0,
          "result": {"status": "ok", "success": True, "setup_success": True}}
    assert vmsmoke.classify(ok).kind == vmsmoke.OK


def test_a_failure_this_check_cannot_attribute_is_not_quietly_called_broken():
    """Refusing to attribute is a claim we can defend. Either of the other two
    would be a claim we cannot — and one of them accuses somebody's task."""
    v = vmsmoke.classify(_result(error="the machine made a noise",
                                 error_type="SomethingElse"))
    assert v.kind == vmsmoke.UNATTRIBUTED
    assert "the machine made a noise" in v.why


def test_a_runner_that_died_without_a_verdict_is_infrastructure():
    """`result.json` still saying `running` means the runner was killed
    mid-flight. Nothing was learned about the task."""
    assert vmsmoke.classify(
        {"returncode": -9, "result": {"status": "running"}}
    ).kind == vmsmoke.INFRASTRUCTURE

    none_at_all = vmsmoke.classify({"returncode": 127, "result": None,
                                    "stderr_tail": "uv: command not found"})
    assert none_at_all.kind == vmsmoke.UNATTRIBUTED
    assert "no result.json" in none_at_all.why


def test_a_deadline_is_infrastructure_and_says_so():
    v = vmsmoke.classify({"timed_out": True, "timeout": 2700, "result": None})
    assert v.kind == vmsmoke.INFRASTRUCTURE
    assert "deadline" in v.why


def test_only_ok_ships():
    """One verdict may be committed, and the table is not open to
    interpretation."""
    assert vmsmoke.SHIPPABLE == (vmsmoke.OK,)
    for kind in (vmsmoke.TASK_BROKEN, vmsmoke.MATERIALS_MISSING,
                 vmsmoke.INFRASTRUCTURE, vmsmoke.UNATTRIBUTED):
        assert not vmsmoke.Outcome("1", kind, "why").ships
    assert vmsmoke.Outcome("1", vmsmoke.OK, "ran").ships


def test_unverified_and_broken_are_different_questions_of_the_outcome():
    assert vmsmoke.Outcome("1", vmsmoke.INFRASTRUCTURE, "").unverified
    assert vmsmoke.Outcome("1", vmsmoke.UNATTRIBUTED, "").unverified
    assert not vmsmoke.Outcome("1", vmsmoke.TASK_BROKEN, "").unverified


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #


def test_nothing_this_module_writes_can_carry_a_credential():
    """A report is exactly the kind of file that gets pasted into a message,
    and a presigned URL in a traceback is a working credential until it
    expires."""
    dirty = ("AKIAIOSFODNN7EXAMPLE / hf_abcdefghijklmnopqrstuvwxyz012345 / "
             "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEX / "
             "https://s3/x?X-Amz-Signature=deadbeefcafe&x=1")
    clean = vmsmoke.redact(dirty)
    for secret in ("AKIAIOSFODNN7EXAMPLE", "hf_abcdefghijklmnopqrstuvwxyz012345",
                   "wJalrXUtnFEMI", "deadbeefcafe"):
        assert secret not in clean
    assert "<redacted>" in clean
    assert "https://s3/x?X-Amz-Signature=" in clean, "the shape is still legible"


def test_a_secret_in_a_runner_error_does_not_reach_the_outcome(tmp_path):
    report = vmsmoke.verify_batch(
        [{"id": "1", "py": tmp_path / "t.py"}],
        upload=lambda row: None, fetch_check=lambda row: [],
        smoke=lambda row, where: _result(
            error="boom AKIAIOSFODNN7EXAMPLE", error_type="Weird"),
        artefacts=tmp_path, attempts=1, cooldown=0)
    blob = json.dumps({k: str(v) for k, v in
                       report.outcomes["1"].__dict__.items()}) \
        + "\n".join(report.events) + vmsmoke.render(report)
    assert "AKIAIOSFODNN7EXAMPLE" not in blob


def test_no_credential_is_ever_on_a_command_line(monkeypatch, tmp_path):
    """An argv is world-readable in `ps` for as long as the process lives. The
    runner takes its credentials from the environment it inherits."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMISECRET")
    monkeypatch.setenv("HF_TOKEN", "hf_abcdefghijklmnopqrstuvwxyz012345")
    cmd = vmsmoke.smoke_command(tmp_path / "task_1.py", tmp_path / "out",
                                runner=tmp_path / "r.py", uv="/usr/bin/uv",
                                instance_type="t3.medium")
    joined = " ".join(cmd)
    assert "wJalrXUtnFEMISECRET" not in joined
    assert "hf_" not in joined
    assert cmd[:3] == ["/usr/bin/uv", "run", "python"]
    assert "--task-path" in cmd and "--output-dir" in cmd


# --------------------------------------------------------------------------- #
# the ceiling nobody will tell us
# --------------------------------------------------------------------------- #


def test_a_proxy_between_here_and_the_vm_is_named_rather_than_blamed_on_the_vm(
        monkeypatch, tmp_path):
    """Measured on the first real run: every task that tried to put a file on
    its instance failed with a 502, and the 502 came from a proxy on *this*
    machine. The verdict was right and the sentence was not — it would have
    sent somebody to look at an AMI that was working perfectly."""
    proxied = {"HTTP_PROXY": "http://127.0.0.1:7897",
               "no_proxy": "10.*,127.*,localhost"}
    note = vmsmoke.proxy_note(proxied)
    assert note and "may be the proxy's and not the VM's" in note
    assert vmsmoke.proxy_note({}) is None
    assert vmsmoke.proxy_note({**proxied, "no_proxy": "*"}) is None, (
        "a blanket bypass is not a problem")

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("no_proxy", "10.*,127.*,localhost")

    report = vmsmoke.verify_batch(
        [{"id": "1", "py": tmp_path / "t.py"}], upload=lambda r: None,
        fetch_check=lambda r: [], smoke=lambda r, w: FIVE_OH_TWO,
        artefacts=tmp_path, attempts=1, cooldown=0)
    assert note in report.notes
    assert note in vmsmoke.render(report)


def test_the_ceiling_narrows_to_just_under_what_was_refused():
    """The refusal happened at a *concurrency*, not at a limit — those differ
    while the pool is draining, and the number proved not to work is the one
    that was running."""
    c = vmsmoke.Ceiling(4, cooldown=0)
    c.too_many(4)
    assert c.limit == 3
    c.too_many(2)                       # refused again while draining
    assert c.limit == 1
    assert c.refusals == 2
    assert all("ceiling" in note for note in c.history)


def test_the_ceiling_never_goes_below_the_floor_and_never_grows_back():
    """Widening after a refusal is a guess in the direction that costs money
    and gets other people's instances refused."""
    c = vmsmoke.Ceiling(2, cooldown=0)
    for _ in range(5):
        c.too_many(1)
    assert c.limit == 1
    assert "no room right now" in c.history[-1]
    assert c.started == 2 and c.limit == 1, "the ceiling grew back"


def test_the_ceiling_is_a_real_limit_on_concurrency():
    c = vmsmoke.Ceiling(2, cooldown=0)
    peak, live, lock = 0, 0, threading.Lock()

    def work():
        nonlocal peak, live
        with c.slot():
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.05)
            with lock:
                live -= 1

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak <= 2, f"{peak} instances were in flight under a ceiling of 2"


def test_a_cooldown_holds_the_next_launch_back():
    c = vmsmoke.Ceiling(2, cooldown=0.2, cooldown_max=0.2)
    c.too_many(2)
    t0 = time.monotonic()
    with c.slot():
        pass
    assert time.monotonic() - t0 >= 0.1, "a refusal was followed straight in"


# --------------------------------------------------------------------------- #
# the batch: order, parallelism, retries
# --------------------------------------------------------------------------- #


class Fake:
    """A batch of decks whose upload, fetch-check and smoke test are scripted."""

    def __init__(self, verdicts: dict, *, upload_fails=(), unfetchable=(),
                 delays=None):
        self.verdicts = verdicts            # id -> list of raw results
        self.upload_fails = set(upload_fails)
        self.unfetchable = set(unfetchable)
        self.delays = delays or {}
        self.calls = []                     # (id, what) in the order seen
        self.finished = []                  # ids, in the order they completed
        self.lock = threading.Lock()

    def rows(self):
        return [{"id": i, "py": Path(f"/tmp/task_{i}.py")} for i in
                sorted(self.verdicts)]

    def _note(self, tid, what):
        with self.lock:
            self.calls.append((tid, what))

    def upload(self, row):
        self._note(row["id"], "upload")
        if row["id"] in self.upload_fails:
            raise RuntimeError("502 Server Error: Bad Gateway")

    def fetch_check(self, row):
        self._note(row["id"], "fetch")
        return ([f"{row['id']}: nothing at init.pptx"]
                if row["id"] in self.unfetchable else [])

    def smoke(self, row, where):
        self._note(row["id"], "smoke")
        time.sleep(self.delays.get(row["id"], 0))
        queue = self.verdicts[row["id"]]
        raw = queue.pop(0) if len(queue) > 1 else queue[0]
        with self.lock:
            self.finished.append(row["id"])
        return raw

    def run(self, tmp_path, **kw):
        kw.setdefault("cooldown", 0)
        return vmsmoke.verify_batch(
            self.rows(), upload=self.upload, fetch_check=self.fetch_check,
            smoke=self.smoke, artefacts=tmp_path, **kw)

    def of(self, tid):
        return [what for i, what in self.calls if i == tid]


_OK = {"returncode": 0, "timed_out": False, "seconds": 1.0,
       "result": {"status": "ok", "success": True}}
_QUOTA = _result(error="An error occurred (VcpuLimitExceeded) when calling "
                       "RunInstances", error_type="ClientError")


def test_every_deck_uploads_then_checks_then_smokes(tmp_path):
    """The order inside a deck is the `both or neither` order, one deck at a
    time: nothing is smoke-tested against materials that are not up yet."""
    fake = Fake({"1": [_OK], "2": [_OK]})
    report = fake.run(tmp_path)
    for tid in ("1", "2"):
        assert fake.of(tid) == ["upload", "fetch", "smoke"]
    assert report.shipping == ["1", "2"]


def test_a_deck_that_finishes_early_does_not_wait_for_a_slow_sibling(tmp_path):
    """The only barrier is the caller's commit. Three batch passes would make
    every deck as slow as the slowest deck in the phase before it."""
    fake = Fake({"slow": [_OK], "fast": [_OK]}, delays={"slow": 0.6})
    report = fake.run(tmp_path, aws_workers=2)
    assert fake.finished == ["fast", "slow"]
    assert report.outcomes["fast"].seconds < report.outcomes["slow"].seconds


def test_the_upload_slot_is_let_go_before_the_vm_is_asked_for(tmp_path):
    """A four-minute VM boot must not hold an upload's turn: with one upload
    slot, a second deck has to be able to upload while the first is on AWS."""
    stamps = {}
    fake = Fake({"1": [_OK], "2": [_OK]}, delays={"1": 0.5})

    def upload(row):
        fake.upload(row)
        stamps[f"upload-{row['id']}"] = time.monotonic()

    def smoke(row, where):
        raw = fake.smoke(row, where)
        stamps[f"smoke-{row['id']}"] = time.monotonic()
        return raw

    report = vmsmoke.verify_batch(
        fake.rows(), upload=upload, fetch_check=fake.fetch_check,
        smoke=smoke, artefacts=tmp_path, hf_workers=1, aws_workers=2,
        cooldown=0)
    assert report.shipping == ["1", "2"]
    assert stamps["upload-2"] < stamps["smoke-1"], (
        "deck 2 could not upload until deck 1's VM was finished with — a "
        "four-minute boot was holding an upload slot")


def test_a_broken_task_is_not_retried(tmp_path):
    """Spending three instances to watch the same 404 three times is $0.015 to
    learn nothing."""
    fake = Fake({"1": [FOUR_OH_FOUR]})
    report = fake.run(tmp_path, attempts=3)
    assert fake.of("1").count("smoke") == 1
    assert report.outcomes["1"].verdict == vmsmoke.TASK_BROKEN
    assert not report.outcomes["1"].ships


def test_an_infrastructure_failure_is_retried_and_can_come_good(tmp_path):
    fake = Fake({"1": [FIVE_OH_TWO, _OK]})
    report = fake.run(tmp_path, attempts=3)
    assert fake.of("1").count("smoke") == 2
    assert report.outcomes["1"].verdict == vmsmoke.OK
    assert [a["verdict"] for a in report.outcomes["1"].attempts] == [
        vmsmoke.INFRASTRUCTURE, vmsmoke.OK]


def test_a_task_that_only_ever_hit_infrastructure_is_unverified_not_failed(
        tmp_path):
    """The claim that must survive to the summary. "We could not check this"
    and "this is broken" are different sentences, and only one of them is
    true."""
    fake = Fake({"1": [FIVE_OH_TWO]})
    report = fake.run(tmp_path, attempts=2)
    out = report.outcomes["1"]
    assert fake.of("1").count("smoke") == 2
    assert out.verdict == vmsmoke.INFRASTRUCTURE
    assert out.unverified and not out.ships
    text = vmsmoke.render(report)
    assert "unverified" in text
    assert "nothing here is a claim that these tasks are broken" in text
    assert "broken, not shipped" not in text, (
        "an infrastructure failure was listed under broken tasks")


def test_a_capacity_refusal_does_not_spend_one_of_the_tasks_attempts(tmp_path):
    """A task that failed because we ran out of quota is not a bad task, and
    recording it as one would be the worst outcome available here."""
    fake = Fake({"1": [_QUOTA, _QUOTA, _OK]})
    report = fake.run(tmp_path, attempts=1)     # one attempt; two refusals
    assert fake.of("1").count("smoke") == 3
    assert report.outcomes["1"].verdict == vmsmoke.OK
    assert report.capacity_refusals == 2
    assert report.ceiling_ended < report.ceiling_started


def test_the_pool_narrows_on_a_refusal_rather_than_hardcoding_a_guess(tmp_path):
    fake = Fake({str(i): [_QUOTA, _OK] for i in range(1, 5)})
    report = fake.run(tmp_path, aws_workers=4, attempts=1)
    assert report.ceiling_started == 4
    assert report.ceiling_ended < 4
    assert report.capacity_refusals >= 1
    assert all(o.verdict == vmsmoke.OK for o in report.outcomes.values())
    assert "aws ceiling" in vmsmoke.render(report)


def test_a_run_that_never_finds_room_says_unverified_and_stops_asking(tmp_path):
    fake = Fake({"1": [_QUOTA]})
    report = fake.run(tmp_path, aws_workers=2, attempts=3, capacity_retries=3)
    out = report.outcomes["1"]
    assert fake.of("1").count("smoke") == 3
    assert out.unverified and "no room" in out.why


def test_the_number_of_instances_never_exceeds_the_pool(tmp_path):
    live, peak, lock = 0, 0, threading.Lock()

    def smoke(row, where):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return _OK

    rows = [{"id": str(i), "py": Path("/tmp/x.py")} for i in range(12)]
    vmsmoke.verify_batch(rows, upload=lambda r: None,
                         fetch_check=lambda r: [], smoke=smoke,
                         artefacts=tmp_path, aws_workers=3, cooldown=0)
    assert peak <= 3, f"{peak} instances at once under --aws-workers 3"


def test_a_deck_whose_materials_did_not_upload_never_reaches_a_vm(tmp_path):
    """No point spending an instance to discover what the upload already
    said — and the deck must not ship either way."""
    fake = Fake({"1": [_OK], "2": [_OK]}, upload_fails=["2"])
    report = fake.run(tmp_path)
    assert fake.of("2") == ["upload"]
    assert report.outcomes["2"].verdict == vmsmoke.MATERIALS_MISSING
    assert not report.outcomes["2"].uploaded
    assert report.shipping == ["1"]


def test_the_cheap_url_check_still_runs_and_still_stops_the_expensive_one(
        tmp_path):
    """`verify_fetchable` is not replaced, it is demoted to a pre-flight: it
    costs a HEAD request and saves four minutes and an instance."""
    fake = Fake({"1": [_OK], "2": [_OK]}, unfetchable=["2"])
    report = fake.run(tmp_path)
    assert fake.of("2") == ["upload", "fetch"]
    out = report.outcomes["2"]
    assert out.verdict == vmsmoke.MATERIALS_MISSING
    assert out.uploaded, "it did upload; what failed was serving it back"
    assert "does not serve" in out.why


def test_every_attempt_keeps_its_own_artefact_directory(tmp_path):
    fake = Fake({"1": [FIVE_OH_TWO, _OK]})
    report = fake.run(tmp_path, attempts=2)
    dirs = [a["dir"] for a in report.outcomes["1"].attempts]
    assert len(set(dirs)) == 2
    assert dirs[0].endswith("attempt-1") and dirs[1].endswith("attempt-2")
    assert all("task_1" in d for d in dirs)


def test_an_empty_batch_is_not_an_error(tmp_path):
    report = vmsmoke.verify_batch([], upload=None, fetch_check=None,
                                  smoke=None, artefacts=tmp_path)
    assert report.outcomes == {} and report.shipping == []


# --------------------------------------------------------------------------- #
# the subprocess, for real, against a fake runner
# --------------------------------------------------------------------------- #
#
# The runner itself provisions EC2, so it is stood in for.  Everything on this
# side of it is real: the argv, the working directory, the deadline, and the
# signal that has to reach the runner rather than the wrapper.


def _shim(tmp_path) -> str:
    """Stand in for `uv`, which is `uv run python <script>`.

    Dropping the first two words is exactly what `uv run python` does, so the
    production argv is exercised unchanged rather than special-cased for the
    test.
    """
    shim = tmp_path / "uv"
    shim.write_text('#!/bin/sh\nshift 2\nexec python3 "$@"\n')
    shim.chmod(0o755)
    return str(shim)


def _runner(tmp_path, body: str) -> Path:
    py = tmp_path / "fake_runner.py"
    py.write_text("import json, os, sys, time\n"
                  "args = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n"
                  "out = args['--output-dir']\n"
                  "os.makedirs(out, exist_ok=True)\n" + body)
    return py


def test_a_passing_runner_is_read_off_result_json(tmp_path):
    runner = _runner(tmp_path, """
json.dump({"status": "ok", "success": True, "setup_success": True},
          open(os.path.join(out, "result.json"), "w"))
print("hello from the runner")
""")
    raw = vmsmoke.run_smoke(tmp_path / "task_1.py", tmp_path / "run",
                            runner=runner, osworld=tmp_path,
                            uv=_shim(tmp_path), timeout=60)
    assert raw["returncode"] == 0
    assert vmsmoke.classify(raw).kind == vmsmoke.OK
    assert Path(raw["result_path"]).exists()
    assert "hello from the runner" in raw["stderr_tail"]


def test_a_runner_that_writes_a_failure_is_read_the_same_way(tmp_path):
    runner = _runner(tmp_path, """
json.dump(%r, open(os.path.join(out, "result.json"), "w"))
sys.exit(1)
""" % FOUR_OH_FOUR["result"])
    raw = vmsmoke.run_smoke(tmp_path / "task_1.py", tmp_path / "run",
                            runner=runner, osworld=tmp_path,
                            uv=_shim(tmp_path), timeout=60)
    assert raw["returncode"] == 1
    assert vmsmoke.classify(raw).kind == vmsmoke.TASK_BROKEN


def test_a_runner_that_writes_nothing_is_unattributed_not_broken(tmp_path):
    runner = _runner(tmp_path, "sys.exit(3)\n")
    raw = vmsmoke.run_smoke(tmp_path / "task_1.py", tmp_path / "run",
                            runner=runner, osworld=tmp_path,
                            uv=_shim(tmp_path), timeout=60)
    assert raw["result"] is None
    assert vmsmoke.classify(raw).kind == vmsmoke.UNATTRIBUTED


def test_a_deadline_kills_the_whole_group_so_the_instance_is_released(tmp_path):
    """`uv run python …` is two processes. SIGTERM to the wrapper alone leaves
    the runner — and therefore the EC2 instance — alive and billing. The
    runner turns SIGTERM into a KeyboardInterrupt precisely so its `finally`
    can call `env.close()`; it only gets the chance if the signal reaches it.
    """
    runner = _runner(tmp_path, """
import subprocess
child = subprocess.Popen(["sleep", "120"])
open(os.path.join(out, "child.pid"), "w").write(str(child.pid))
time.sleep(120)
""")
    raw = vmsmoke.run_smoke(tmp_path / "task_1.py", tmp_path / "run",
                            runner=runner, osworld=tmp_path,
                            uv=_shim(tmp_path), timeout=2)
    assert raw["timed_out"] is True
    assert vmsmoke.classify(raw).kind == vmsmoke.INFRASTRUCTURE

    pid = int((tmp_path / "run" / "child.pid").read_text())
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:                                                # pragma: no cover
        pytest.fail(f"pid {pid} survived the deadline — an instance would have "
                    f"survived with it")


def test_the_runner_is_checked_once_rather_than_failed_forty_times(tmp_path):
    """Marking forty decks unverified because `uv` is missing is forty
    misleading records and forty minutes spent proving one thing about this
    machine."""
    with pytest.raises(vmsmoke.SmokeUnavailable) as e:
        vmsmoke.preflight(tmp_path, "/bin/true")
    assert "OSWorld-V2 checkout" in str(e.value)

    (tmp_path / "task_loader.py").write_text("")
    with pytest.raises(vmsmoke.SmokeUnavailable) as e:
        vmsmoke.preflight(tmp_path, "/bin/true")
    assert "smoke runner is not at" in str(e.value)


def test_missing_aws_credentials_are_not_reported_as_broken_tasks(
        tmp_path, monkeypatch):
    runner = tmp_path / vmsmoke.RUNNER_REL
    runner.parent.mkdir(parents=True)
    runner.write_text("")
    (tmp_path / "task_loader.py").write_text("")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    with pytest.raises(vmsmoke.SmokeUnavailable) as e:
        vmsmoke.preflight(tmp_path, "/bin/true")
    assert "none of it would be about the tasks" in str(e.value)


def test_uv_is_found_where_a_background_shell_cannot_find_it(monkeypatch,
                                                             tmp_path):
    """A background bash does not read the profile, so `~/.local/bin` is not on
    PATH and `uv` exits 127 — which arrives looking like a task that failed to
    start."""
    fake_uv = tmp_path / "uv"
    fake_uv.write_text("#!/bin/sh\n")
    fake_uv.chmod(0o755)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv(vmsmoke.UV_ENV, str(fake_uv))
    assert vmsmoke.resolve_uv() == str(fake_uv)

    monkeypatch.delenv(vmsmoke.UV_ENV)
    monkeypatch.setattr(vmsmoke.Path, "home", staticmethod(lambda: tmp_path))
    with pytest.raises(vmsmoke.SmokeUnavailable):
        vmsmoke.resolve_uv()                    # no ~/.local/bin/uv either


# --------------------------------------------------------------------------- #
# what a fake cannot answer
# --------------------------------------------------------------------------- #


def test_what_a_real_vm_run_still_has_to_prove():
    """Written down so it is read, and so it fails if the list is deleted
    without the work being done.

    Everything above runs against an injected `smoke` or a shim standing in for
    `uv`. What one real batch of six instances (three tasks, two attempts each,
    2026-08-05) did and did not settle:

      * **settled** — the runner's argv, its `result.json`, and the shape of
        both failure payloads. `REAL_404` above is copied from that run, and
        the fact that `result.json` drops the HTTP status was found by it, not
        reasoned out.
      * **settled** — that a 502 is retried and reported as `unverified` while
        a 404 is reported as a broken task. Both happened, on real machines,
        in the same batch.
      * **not settled: a real `ok`.** No task passed, because every task that
        tried to put a file on its instance got a 502 from the HTTP proxy
        configured on the host — see `proxy_note`. The success path below the
        classifier is exercised only against the fake runner, and this file
        does not claim otherwise.
      * **not settled: the account's vCPU ceiling.** It cannot be read
        (`servicequotas:GetServiceQuota` is denied) and 36 vCPUs belong to
        other people. Three instances at once were never refused; that is one
        data point about one afternoon, not a limit. `Ceiling` discovers it
        per run and deliberately forgets it after.
    """
    assert vmsmoke.DEFAULT_AWS_WORKERS == 4
    assert vmsmoke.Ceiling(4).limit == 4
    assert REAL_404["result"]["error"].endswith("No retries left.")


# --------------------------------------------------------------------------- #
# publish, wired to it
# --------------------------------------------------------------------------- #
#
# `test_publish.py` owns the fixtures for a work tree and a throwaway rollout
# checkout; they are imported rather than copied so that a change to the
# package's shape breaks one set of fixtures and not two.


@pytest.fixture
def pub(monkeypatch):
    import test_publish
    return test_publish


def _vm(tmp_path, verdicts, **kw):
    fake = Fake(verdicts)
    vm = publish.VmCheck(artefacts=tmp_path / "aws", cooldown=0,
                         upload=fake.upload, fetch_check=fake.fetch_check,
                         smoke=fake.smoke, **kw)
    return fake, vm


def test_only_the_decks_that_ran_on_a_vm_are_committed(tmp_path, pub):
    """The `both or neither` rule, with a better `both`. A deck whose setup
    did not run is dropped from the commit and named, exactly as an
    unfetchable one already was."""
    work, rollout = pub._mini_work(tmp_path), pub._rollout(tmp_path)
    plan = pub._plan(tmp_path, work=work, rollout=rollout)
    assert len(plan["rows"]) >= 2, "the fixture cannot show a partial batch"

    good = plan["rows"][0]["id"]
    verdicts = {r["id"]: [_OK if r["id"] == good else FOUR_OH_FOUR]
                for r in plan["rows"]}
    fake, vm = _vm(tmp_path, verdicts)
    done = publish.publish(plan, push_git=False, vm=vm)

    py = rollout / publish.TASK_CLASS_REL
    assert (py / f"task_{good}.py").exists()
    for row in plan["rows"][1:]:
        assert not (py / f"task_{row['id']}.py").exists(), (
            "a task whose setup() failed on a real machine was committed")
        assert row["id"] in done["dropped"]
    assert vmsmoke.TASK_BROKEN in [o.verdict for o in
                                   done["vm"].outcomes.values()]


def test_the_commit_is_the_only_barrier(tmp_path, pub, monkeypatch):
    """Every deck's smoke test is finished before the first file is placed —
    and no deck waited for another deck's *stage*, only for the commit."""
    work, rollout = pub._mini_work(tmp_path), pub._rollout(tmp_path)
    plan = pub._plan(tmp_path, work=work, rollout=rollout)
    verdicts = {r["id"]: [_OK] for r in plan["rows"]}
    fake, vm = _vm(tmp_path, verdicts)

    seen = {}
    real_place = publish.place_task_files

    def place(rows, out):
        seen["smoked"] = list(fake.finished)
        return real_place(rows, out)

    monkeypatch.setattr(publish, "place_task_files", place)
    publish.publish(plan, push_git=False, vm=vm)
    assert sorted(seen["smoked"]) == sorted(verdicts), (
        "a task file was written before every deck had been checked")


def test_a_batch_where_nothing_verified_writes_nothing_at_all(tmp_path, pub):
    work, rollout = pub._mini_work(tmp_path), pub._rollout(tmp_path)
    plan = pub._plan(tmp_path, work=work, rollout=rollout)
    head = pub._git(rollout, "rev-parse", "HEAD").strip()
    verdicts = {r["id"]: [FIVE_OH_TWO] for r in plan["rows"]}
    fake, vm = _vm(tmp_path, verdicts, attempts=1)

    done = publish.publish(plan, push_git=False, vm=vm)
    assert "nothing was verified" in done["git"]
    assert not list((rollout / publish.TASK_CLASS_REL).glob("task_110*.py"))
    assert not publish.registry_path(rollout).exists(), (
        "the registry recorded an allocation for a task that was not published")
    assert pub._git(rollout, "rev-parse", "HEAD").strip() == head


def test_a_dropped_deck_keeps_its_number_for_the_next_run(tmp_path, pub):
    """A number is spent when it is *allocated*, and a deck that failed its
    smoke test will be fixed and published under the same id — an id that
    moves breaks every rollout result recorded against it."""
    work, rollout = pub._mini_work(tmp_path), pub._rollout(tmp_path)
    plan = pub._plan(tmp_path / "a", work=work, rollout=rollout)
    ids = {r["deck"]: r["id"] for r in plan["rows"]}
    good, bad = plan["rows"][0]["id"], plan["rows"][1]["id"]
    fake, vm = _vm(tmp_path, {r["id"]: [_OK if r["id"] == good
                                        else FOUR_OH_FOUR]
                              for r in plan["rows"]})
    publish.publish(plan, push_git=False, vm=vm)

    again = pub._plan(tmp_path / "b", work=work, rollout=rollout)
    assert {r["deck"]: r["id"] for r in again["rows"]}.items() <= ids.items()
    assert bad in [r["id"] for r in again["rows"]], "the failed deck vanished"
    assert again["allocated"] == [], "a dropped deck's number was handed out"


def test_the_url_check_is_still_asked_of_the_real_urls(tmp_path, pub,
                                                       monkeypatch):
    """The pre-flight inside the VM check is the same `verify_fetchable`, asked
    one deck at a time so that one deck's missing file does not stop another
    deck's instance."""
    asked = []
    monkeypatch.setattr(publish, "verify_fetchable",
                        lambda rows, repo, token=None: asked.append(
                            [r["id"] for r in rows]) or [])
    work, rollout = pub._mini_work(tmp_path), pub._rollout(tmp_path)
    plan = pub._plan(tmp_path, work=work, rollout=rollout)
    fake = Fake({r["id"]: [_OK] for r in plan["rows"]})
    vm = publish.VmCheck(artefacts=tmp_path / "aws", cooldown=0,
                         upload=fake.upload, smoke=fake.smoke)
    publish.publish(plan, push_git=False, vm=vm)
    assert sorted(asked) == sorted([[r["id"]] for r in plan["rows"]]), (
        "the check either did not run or was asked about the whole batch")


def test_the_flags_exist_and_default_conservatively(tmp_path):
    """4 workers is 8 vCPU. It is a starting point and the code says so; the
    number that matters is the one `Ceiling` ends the run on."""
    vm = publish.VmCheck(artefacts=tmp_path)
    assert vm.aws_workers == 4 and vm.hf_workers == 4 and vm.attempts == 3
