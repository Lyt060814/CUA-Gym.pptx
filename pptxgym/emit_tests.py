"""Generate the test suite that ships beside a packaged task.

`emit` writes a task; this writes the thing that says whether the task is
worth running.  The shape is the archived `task-tester` skill's — plain
`test_*` functions in `tests/test_task.py`, a committed
`tests/task-test-report-<id>.md`, runner artefacts gitignored — and four of
its five categories are mechanical: sanity, hard gates, the two endpoints,
the rule methods in isolation.

**The fifth category is why this file exists.**  On the last rollout, of the
four tasks a real model attempted, three scored **0.0 while having completed
43%, 53% and 63% of the work**.  A suite that checks only the two endpoints —
the untouched input and the perfect answer — cannot see that: both endpoints
were right in every one of those evaluators.  What was wrong was everything
in between, and nothing measured it.

So the generated suite builds states that are *partially* right and asserts
the score lands where it should:

    a quarter repaired  <  half repaired  <  1.0
    one degradation fixed, another missed -> the weight of the one, not zero
    saved under another name              -> recovered and scored, not zeroed
    an image re-encoded on save           -> the application's doing, not the
                                             agent's, and it must not cost

The states are built at **component granularity** by copying the ground
truth's own record of a shape back over the broken one.  `comparators`
already ships `_state_half`, `_state_rebuilt` and `_state_over_eager`, and
the first two are reused where they fit — but `_state_half` restores whole
*pages*, so on a deck with one damaged page it *is* the ground truth, and on
a page carrying four components it grants all four.  Neither can express "a
quarter of the work", which is the assertion that matters, hence
`_partially_restored` below.

The reconstruction is checked rather than trusted: restoring **every**
component has to score exactly 1.0.  If it does not, the helper — not the
reward — is what is wrong, and the generated suite says so in those words.

    python3 -m pptxgym.emit_tests --out OUT --task-id 9900003
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import traceback
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent


class EmitTestsError(RuntimeError):
    pass


#: What a real WPS open-and-save does to picture bytes, measured rather than
#: assumed — the two findings below are about an event, and an event nobody
#: has measured is a hypothesis.  `wps_roundtrip.roundtrip_wps` on four decks,
#: comparing media parts as a multiset of content digests:
#:
#:   deck0003   51/51 blobs survive byte-identical   0 re-encoded
#:   deck0008   29/29                                0
#:   deck0004   30/34                                4
#:   deck0005   27/29                                2
#:
#: So WPS does re-encode — the claim in `_scope_media_lost`'s docstring
#: reproduces exactly.  Two things narrow it, and both were measured the same
#: way:
#:
#:   * **None of the re-encoded parts is the blob of a picture shape on a
#:     slide.**  They live in masters, layouts or alternate representations
#:     (the EMF and SVG parts), which is why the real WPS-saved ground truth
#:     of deck0004 and deck0005 scores **1.0000, penalty 0.0** against its own
#:     plan even with four and two blobs rewritten.
#:   * **An asset inserted as a new picture part survives byte-identical**:
#:     10 of 10 across the four decks, inserted and then round-tripped.  The
#:     commonest legitimate action in this task family — "the replacement
#:     photos are in the assets folder" — is not punished.
#:
#: The two assertions below are therefore **probes against a synthetic
#: premise**, and they are labelled as such wherever they are reported.  They
#: stay because the premise is not hypothetical for the whole environment:
#: LibreOffice re-encodes *every* image and rewrites EMF as WMF, and a rollout
#: has already been seen where the VM's `.pptx` handler was bound to Impress
#: although setup launched WPS.  What they measure is how much the scorer
#: loses when a slide picture's bytes move — 0.55 to 0.61 of a perfect deck,
#: through two independent charges — which is worth knowing before that
#: happens rather than after.
MEASURED_PREMISE = (
    "Measured, not assumed: a real WPS open-and-save re-encodes 0/51 blobs on "
    "deck0003, 0/29 on deck0008, 4/34 on deck0004 and 2/29 on deck0005 — and "
    "*none* of the re-encoded parts is the blob of a picture shape on a slide, "
    "so the real WPS-saved ground truth of deck0004 and deck0005 scores 1.0000 "
    "with no penalty. An asset inserted as a new picture part survived "
    "byte-identical 10 times out of 10. **This assertion therefore fails on a "
    "synthetic premise**, and what it measures is the scorer's brittleness "
    "rather than a live loss — brittleness that becomes live the moment the "
    "file passes through LibreOffice, which re-encodes every image, and a "
    "rollout has already been seen with the `.pptx` handler bound to Impress.")

#: Assertions that are expected to fail today, and what the failure *means*.
#: A category-5 assertion that fails is a finding about the reward, not a bug
#: in the test, and the one thing not to do with it is soften it until it
#: passes — that is how an evaluator ends up with two endpoints and nothing in
#: between.  It stays as written; the report separates it from a real defect
#: so a red suite still tells you which kind of red it is.
KNOWN_FINDINGS = {
    "test_a_re_encoded_image_the_task_asked_to_restore_costs_nothing": (
        "**The comparator half.** `_facet_picture` compares image bytes "
        "exactly, and it is multiplied by the geometry rather than averaged "
        "with it, so a picture restored in exactly the right place at exactly "
        "the right size scores **0** for its component if the bytes moved. "
        "The inventory records nothing else about a picture — no pixel size, "
        "no format, no perceptual hash — so there is no fallback to reach "
        "for. Measured: this costs 0.4386 of deck0003 and 0.3571 of deck0008 "
        "before the scope penalty below is applied on top.\n\n"
        "The identity chain, for whoever fixes it: the shape still *pairs*, "
        "but not on `pic:<digest>` — that key is gone. It falls through to "
        "`name:Picture 3`, a weak key, which is only allowed to pair shapes "
        "whose boxes meet. So a picture put back in place keeps its identity "
        "by luck; a picture *redrawn* (stock name) or one whose repair also "
        "moves it pairs on nothing, and then the same event is charged a "
        "third time as `survivors_intact` + `no_extra_shapes`."),
    "test_a_re_encoded_image_on_a_page_nobody_touched_costs_nothing": (
        "**The identity half, and the more serious one.** `_page_facts` keys "
        "every fact it records about a shape on `shape['key']`, which for a "
        "picture *is* the blob digest — `shapes[pic:279bd563df27c9f8#0]"
        ".bbox.cx`. Re-encoding therefore changes the address every fact is "
        "filed at, and `_scope_untouched_pages` reads one shape at a new "
        "address as a deleted shape plus an added one, on a page the task "
        "never named. Measured: 13 such 'changes' on deck0003 and 6 on "
        "deck0008, both hitting the 0.30 cap, which multiplies the whole "
        "score.\n\n"
        "The docstring one line above says the value is deliberately recorded "
        "as `picture: True` rather than the bytes, 'because the blob is the "
        "application's to change'. That reasoning is right and it is defeated "
        "by the key, not by the value. This half needs no tolerance and no "
        "policy decision — a picture's facts want an address the application "
        "cannot rewrite."),
}


# --------------------------------------------------------------------------- #
# the generated suite
# --------------------------------------------------------------------------- #
#
# Built by concatenation, not by `str.format`: the body is a few hundred lines
# of Python with braces on most of them, and doubling every one of them to
# survive a format call is how a template stops being reviewable.  Everything
# that varies between tasks lives in the header.


def _header(task_id: str, deck_id: str, plan: dict) -> str:
    return f'''"""Tests for task_{task_id}, generated by pptxgym from {deck_id}.

Do not edit by hand — regenerate with `python3 -m pptxgym.emit_tests`.

Every function here is a complete test: it builds its own fake env, its own
files and its own scoring states, and it passes by returning without raising.
That is the archived `task-tester` runner's contract, and it is also plain
pytest, so both can run this file.

The interesting half is the partial-credit block at the bottom.  The two
endpoints — untouched input and perfect answer — were *right* in every
evaluator of the last rollout, in which three of four tasks scored 0.0 for
work that was 43%, 53% and 63% done.  What those suites could not see is what
this one measures.
"""

TASK_ID = {task_id!r}
DECK_ID = {deck_id!r}
COMPONENTS = {len(plan["components"])}
DEGRADATIONS = {[d["id"] for d in plan.get("degradations") or []]!r}
'''


TEST_BODY = r'''
import copy
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
TASK_ASSETS = TESTS_DIR / "assets"
TASK_DIR = TESTS_DIR.parent                      # task_assets/task_<id>/
OUT_ROOT = TASK_DIR.parent.parent                # holds task_class/
TASK_PY = Path(os.environ.get(
    "PPTXGYM_TASK_PY", OUT_ROOT / "task_class" / ("task_%s.py" % TASK_ID)))

_MODULE = []


# --------------------------------------------------------------------------- #
# loading the task the way the harness does
# --------------------------------------------------------------------------- #


def _stub_harness():
    """Put the two names the task imports at module level into `sys.modules`.

    `get_vm_file` / `get_vm_command_line` are stubbed even when the benchmark
    checkout is reachable: importing the real module drags in lxml, selenium
    and a dozen more, and those two functions are the seam every test here
    drives `evaluate` through anyway.  `BaseTask` is left real when it can be
    found, because the runner reads a task through the dict it builds.
    """
    if "desktop_env.task_base" not in sys.modules:
        try:
            import desktop_env.task_base                     # noqa: F401
        except Exception:                                    # noqa: BLE001
            for name in ("desktop_env", "desktop_env.task_base"):
                sys.modules.setdefault(name, types.ModuleType(name))
            sys.modules["desktop_env.task_base"].BaseTask = object
    sys.modules.setdefault("desktop_env.evaluators",
                           types.ModuleType("desktop_env.evaluators"))
    getters = types.ModuleType("desktop_env.evaluators.getters")
    getters.get_vm_file = lambda *a, **k: None
    getters.get_vm_command_line = lambda *a, **k: ""
    sys.modules["desktop_env.evaluators.getters"] = getters


def task_module():
    """The generated task file, imported once per process."""
    if not _MODULE:
        if not TASK_PY.exists():
            raise AssertionError("no task file at %s" % TASK_PY)
        _stub_harness()
        name = "packaged_task_%s" % TASK_ID
        spec = importlib.util.spec_from_file_location(name, TASK_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODULE.append(mod)
    return _MODULE[0]


def comparators_of(mod):
    """The scoring runtime's namespace inside the generated task.

    The task carries `inventory` and `comparators` as two separate namespaces
    so that a helper name in one cannot silently shadow a helper name in the
    other, and re-exports only the two entry points `evaluate` calls. Anything
    finer-grained than `score` — the pairing, the gates, the coherence states —
    is reached through the namespace, and this accessor also keeps these tests
    working against a task emitted before that split existed.
    """
    return getattr(mod, "comparators", mod)


def fixtures():
    """(module, plan, ground-truth inventory, broken inventory).

    Read from `tests/assets/`, which `setup` never uploads: they are the
    answer key, and the evaluator is the only thing allowed to see them.
    """
    mod = task_module()
    plan = json.loads((mod.TEST_ASSETS / "plan.json").read_text())
    gt = json.loads((mod.TEST_ASSETS / "gt_inventory.json").read_text())
    init = json.loads((mod.TEST_ASSETS / "init_inventory.json").read_text())
    return mod, plan, gt, init


def init_pptx():
    return str(task_module().AGENT_ASSETS / "init.pptx")


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeEnv:
    """Serves one local file per VM path and records what `evaluate` runs.

    `disk_sha` is what `sha256sum` reports for the deck at the pinned path —
    the single fact the save contract turns on.
    """

    def __init__(self, disk_sha, files, save="SAVED", stray=""):
        self.disk_sha = disk_sha
        self.files = files
        self.save = save
        self.stray = stray
        self.commands = []
        self.fetched = []

    def install(self, mod):
        def command_line(_env, config):
            command = config["command"][-1]
            self.commands.append(self.kind(command))
            if "sha256sum" in command:
                return (self.disk_sha or "MISSING") + "\n"
            if "SAVE_FAILED" in command:
                return self.save + "\n"
            if command.startswith("find "):
                return self.stray + "\n"
            return ""

        def vm_file(_env, config):
            self.fetched.append(config["path"])
            return self.files.get(config["path"])

        mod.get_vm_command_line = command_line
        mod.get_vm_file = vm_file
        return self

    @staticmethod
    def kind(command):
        if "sha256sum" in command:
            return "sha"
        if "SAVE_FAILED" in command:
            return "save"
        if command.startswith("pkill"):
            return "kill"
        if ".~lock." in command:
            return "unlock"
        if command.startswith("find "):
            return "scan"
        return command[:40]


class FakeController:
    """A `SetupController` that records instead of touching a machine."""

    def __init__(self):
        self.calls = []

    def execute(self, command, stdout="", stderr="", shell=False, until=None,
                quiet=False, timeout=120):
        self.calls.append(("execute", command))

    def launch(self, command, shell=False):
        self.calls.append(("launch", command))

    def download(self, files):
        self.calls.append(("download", files))

    def _upload_file_setup(self, files):
        self.calls.append(("upload", files))

    def of(self, kind):
        return [payload for name, payload in self.calls if name == kind]


def evaluate_on(mod, inventory, *, disk_sha="changed" * 8, stray=""):
    """Run the real `evaluate` against a state expressed as an inventory.

    The deck served is `init.pptx` — a real file has to be at the path, and
    the ground truth deliberately is not shipped — and `inventory_pptx` is
    replaced so the *state* under test is the one passed in.  That is the same
    seam `get_vm_file` is patched at, one layer down, and it is what makes a
    half-finished deck expressible without a half-finished .pptx.
    """
    files = {mod.DECK_VM_PATH: init_pptx()}
    if stray:
        files[stray] = init_pptx()
    env = FakeEnv(disk_sha, files, save="SAVE_FAILED", stray=stray).install(mod)
    original = mod.inventory_pptx
    mod.inventory_pptx = lambda _path: copy.deepcopy(inventory)
    try:
        return mod.TASK_CLASS().evaluate(env), env
    finally:
        mod.inventory_pptx = original


# --------------------------------------------------------------------------- #
# building a partially repaired deck
# --------------------------------------------------------------------------- #


def _target_path(mod, component, gt_slide):
    """Which ground-truth shape this component is about.

    Most components name it.  A SmartArt or chart component does not — its
    comparator finds the object by its data part instead — so the same
    question is asked the same way here rather than guessed at.
    """
    if component.get("gt_path"):
        return component["gt_path"]
    spec = component.get("spec") or {}
    rules = comparators_of(mod)
    try:
        if component["op"] == "smartart_drop_nodes":
            return rules._find_smartart(gt_slide, spec.get("data_part"))["_path"]
        if component["op"] == "chart_edit":
            return rules._find_chart(gt_slide, spec.get("chart_part"))["_path"]
    except Exception:                                        # noqa: BLE001
        return None
    return None


def restore_component(mod, out, gt, component, paired=None):
    """Do the work of one component, perfectly, and nothing else.

    The shape the component is scored on is replaced by the ground truth's
    record of it — which is what a solver who repairs that one thing and stops
    produces.  Media digests follow the shape, because a picture put back is a
    picture part present.

    `paired` is the per-slide pairing, cached across the components that share
    a page: the pairing is quadratic in the shapes on a slide, and a deck with
    a hundred components spends all its time recomputing it.  Every shape this
    inserts is written back into the cache under its own path, so a component
    repaired after one that swallowed it — a group child, say — still finds
    the thing that is now standing in for it instead of adding a duplicate.
    """
    index = component["slide"]
    slide = out["slides"][index]
    gt_slide = gt["slides"][index]
    path = _target_path(mod, component, gt_slide)

    if path:
        want = next((s for s in gt_slide["shapes"] if s["_path"] == path), None)
        if want is None:
            return False
        if paired is None:
            paired = {}
        if index not in paired:
            paired[index] = dict(comparators_of(mod).pair_slide(
                gt_slide["shapes"], slide["shapes"]))
        mate = paired[index].get(path)
        if mate is not None:
            dead = {id(mate)} | {id(s) for s in slide["shapes"]
                                 if s["_path"].startswith(mate["_path"] + "/")}
            slide["shapes"] = [s for s in slide["shapes"] if id(s) not in dead]
        family = [want] + [s for s in gt_slide["shapes"]
                           if s["_path"].startswith(path + "/")]
        fresh = [copy.deepcopy(s) for s in family]
        slide["shapes"].extend(fresh)
        for shape in fresh:
            paired[index][shape["_path"]] = shape
        media = list(out["package"]["media"])
        for shape in family:
            blob = (shape.get("picture") or {}).get("blob")
            if blob and blob not in media:
                media.append(blob)
        out["package"] = dict(out["package"], media=sorted(media))
        return True

    op = component["op"]
    if op == "clear_notes":
        slide["notes"] = copy.deepcopy(gt_slide.get("notes"))
        return True
    if op in ("strip_animation", "anim_drop_steps"):
        slide["animation"] = copy.deepcopy(gt_slide.get("animation"))
        return True
    if op == "strip_transition":
        slide["transition"] = copy.deepcopy(gt_slide.get("transition"))
        return True
    if op == "layout_edit":
        name = (component.get("spec") or {}).get("layout")
        out.setdefault("layouts", {})[name] = copy.deepcopy(
            (gt.get("layouts") or {}).get(name))
        return True
    if op in ("reorder_slides", "delete_slides"):
        out["slides"] = copy.deepcopy(gt["slides"])
        return True
    return False


def partially_restored(mod, plan, gt, init, components):
    out = copy.deepcopy(init)
    paired = {}
    missed = [c["id"] for c in components
              if not restore_component(mod, out, gt, c, paired)]
    return out, missed


def by_fraction(mod, plan, gt, init, fraction):
    """The first `fraction` of the components repaired, in plan order."""
    components = plan["components"]
    take = components[:int(len(components) * fraction)]
    state, missed = partially_restored(mod, plan, gt, init, take)
    return state, len(take), missed


def scored(mod, plan, gt, init, state):
    return mod.score(plan, state, gt, init)["score"]


def re_encoded(plan, inventory, *, only_untouched):
    """Every picture's bytes replaced, as an application that re-encodes does.

    Not a hypothetical: `comparators` records WPS turning 51 media parts into
    52 and re-encoding six PNGs across two decks on a plain open-and-save.

    **The shape's identity moves with the bytes**, and getting that wrong
    understates the damage by more than half. `inventory` gives a picture the
    strongest key it has as `pic:<digest of the blob>`, and files every fact
    about the shape under that key; re-encoding therefore does not only change
    a value the comparator reads, it changes the *address* the value is filed
    at. A version of this helper that swapped the blob and left `keys` alone
    measured a 0.44 loss where the real one is 0.61.
    """
    out = copy.deepcopy(inventory)
    damaged = set(plan["damage"]["slides"])
    swapped = {}
    for index, slide in enumerate(out["slides"]):
        if only_untouched and index in damaged:
            continue
        for shape in slide["shapes"]:
            picture = shape.get("picture") or {}
            blob = picture.get("blob")
            if not blob:
                continue
            fresh = "reenc" + blob[5:]
            swapped[blob] = fresh
            picture["blob"] = fresh
            shape["keys"] = [k.replace(blob, fresh)
                             for k in shape.get("keys") or []]
            if shape.get("key"):
                shape["key"] = shape["key"].replace(blob, fresh)
    out["package"] = dict(
        out["package"],
        media=sorted(swapped.get(b, b) for b in out["package"]["media"]))
    return out


def degradation_groups(plan):
    """degradation id -> its components, for degradations that have any."""
    out = {}
    for component in plan["components"]:
        out.setdefault(component.get("deg"), []).append(component)
    return {k: v for k, v in out.items() if k}


# =========================================================================== #
# 1. sanity / shape
# =========================================================================== #


def test_the_task_class_instantiates():
    mod = task_module()
    task = mod.TASK_CLASS()
    assert task.id == TASK_ID
    assert task.instruction.strip()
    assert task.platform == "linux"
    assert task.related_apps == ["wps"]


def test_the_weights_sum_to_one_and_match_the_descriptions():
    """Within the rounding the emitter does: every weight is written to six
    decimal places, so a hundred of them can miss the sum by 5e-5 and that is
    arithmetic, not a defect."""
    task = task_module().TASK_CLASS()
    slack = max(1e-6, 1e-6 * len(task.WEIGHTS))
    assert abs(sum(task.WEIGHTS.values()) - 1.0) < slack, task.WEIGHTS
    assert set(task.WEIGHTS) == set(task.DESCRIPTIONS)
    assert all(w > 0 for w in task.WEIGHTS.values())
    assert all(str(d).strip() for d in task.DESCRIPTIONS.values())


def test_the_class_matches_its_metadata():
    """Nothing in the harness reads `metadata.json`, which is why it drifts —
    and it is what a reviewer reads before approving the task."""
    mod = task_module()
    task = mod.TASK_CLASS()
    meta = json.loads((TASK_DIR / "metadata.json").read_text())
    assert meta["id"] == task.id
    assert meta["instruction"] == task.instruction
    assert meta["platform"] == task.platform
    assert meta["related_apps"] == task.related_apps
    assert meta["input_file"] == mod.DECK_VM_PATH
    assert meta["evaluator"] == mod.EVALUATOR_ID
    assert meta["components"] == COMPONENTS


def test_evaluate_returns_a_score_and_a_partial_for_every_weight():
    mod, plan, gt, init = fixtures()
    result, _env = evaluate_on(mod, gt)
    assert isinstance(result["score"], float) and 0.0 <= result["score"] <= 1.0
    assert set(result["partial_scores"]) == set(mod.TASK_CLASS().WEIGHTS)
    for pid, item in result["partial_scores"].items():
        assert isinstance(item["score"], float), pid
        assert isinstance(item["weight"], float), pid
        assert isinstance(item["description"], str) and item["description"], pid
        assert 0.0 <= item["score"] <= 1.0, pid


def test_the_instruction_pins_the_application_and_the_path():
    mod = task_module()
    text = mod.TASK_CLASS().instruction
    assert "WPS Presentation only" in text
    assert mod.DECK_VM_PATH in text
    assert "Do not rename or move it." in text


def test_setup_uploads_the_deck_and_the_materials_and_nothing_else():
    mod = task_module()
    controller = FakeController()
    mod.TASK_CLASS().setup(controller, use_proxy=False)
    uploads = [f for batch in controller.of("upload") for f in batch]
    assert uploads, "setup uploaded nothing"
    assert uploads[0]["path"] == mod.DECK_VM_PATH
    assert Path(uploads[0]["local_path"]).name == "init.pptx"
    secret = {"plan.json", "gt_inventory.json", "init_inventory.json"}
    for item in uploads:
        local = Path(item["local_path"])
        assert local.name not in secret, "%s was uploaded" % local.name
        assert "tests" not in local.parts, "%s is answer-key material" % local
        assert local.exists(), "%s does not exist to upload" % local
    assert controller.of("launch") == [["wpp", mod.DECK_VM_PATH]]


# =========================================================================== #
# 2. hard gates
# =========================================================================== #


def test_a_missing_deck_scores_zero_with_a_reason():
    mod = task_module()
    env = FakeEnv("", {}, save="SAVE_FAILED").install(mod)
    result = mod.TASK_CLASS().evaluate(env)
    assert result["score"] == 0.0
    assert result["failure_reason"]
    assert all(p["score"] == 0.0 for p in result["partial_scores"].values())
    assert all("failed:" in p["description"]
               for p in result["partial_scores"].values())


def test_a_file_that_is_not_a_pptx_scores_zero_with_a_reason():
    mod = task_module()
    junk = TESTS_DIR / "test-work" / "not-a-deck.pptx"
    junk.parent.mkdir(parents=True, exist_ok=True)
    junk.write_bytes(b"this is not a presentation")
    env = FakeEnv("b" * 64, {mod.DECK_VM_PATH: str(junk)},
                  save="SAVE_FAILED").install(mod)
    result = mod.TASK_CLASS().evaluate(env)
    assert result["score"] == 0.0
    assert result["failure_reason"], "a zero has to be explicable"


def test_a_deck_byte_identical_to_the_input_scores_zero():
    """Nothing was ever written out.  The protections against losing real
    work to this — the conditional save, the file association, the stray
    scan — all run before this point; by here there is nothing left to do."""
    mod = task_module()
    env = FakeEnv(mod.INIT_SHA256, {mod.DECK_VM_PATH: init_pptx()}).install(mod)
    result = mod.TASK_CLASS().evaluate(env)
    assert result["score"] == 0.0
    assert "byte-identical" in result["failure_reason"]


def test_a_deck_with_the_wrong_slide_count_scores_zero():
    mod, plan, gt, init = fixtures()
    short = copy.deepcopy(gt)
    short["slides"] = short["slides"][:-1]
    short["package"] = dict(short["package"],
                            slide_count=len(short["slides"]))
    out = mod.score(plan, short, gt, init)
    assert out["score"] == 0.0
    assert out["failed_gate"] == "slide_count_and_order", out["failed_gate"]


def test_a_picture_laid_over_the_page_is_a_cheat_and_scores_zero():
    """A render of the finished slide pasted over the wreckage satisfies
    every visual check and does none of the work."""
    mod, plan, gt, init = fixtures()
    index = sorted(set(plan["damage"]["slides"]))[0]
    package = gt["package"]
    width = int(package.get("slide_w") or 0)
    height = int(package.get("slide_h") or 0)
    if not (width and height):
        return
    state = copy.deepcopy(gt)
    state["slides"][index]["shapes"].append({
        "_path": "overlay", "_id": 9998, "_name": "Picture 9998",
        "_plain": "", "kind": "picture", "z": 9998, "group": None,
        "hidden": False, "text": None,
        "bbox": {"cx": width // 2, "cy": height // 2, "w": width, "h": height,
                 "rot": 0.0, "flip": False},
        "picture": {"blob": "overlaypasted"},
        "keys": ["pic:overlaypasted"], "key": "pic:overlaypasted#0",
    })
    out = mod.score(plan, state, gt, init)
    assert out["failed_gate"] == "no_full_page_overlay", out["gate_reasons"]
    assert out["score"] == 0.0


# =========================================================================== #
# 3. end-to-end calibration
# =========================================================================== #


def test_the_ground_truth_scores_one_with_every_partial_one():
    mod, plan, gt, init = fixtures()
    out = mod.score(plan, gt, gt, init)
    assert abs(out["score"] - 1.0) < 1e-6, out["score"]
    assert out["failed_gate"] is None, out["gate_reasons"]
    assert out["penalty"] == 0.0, out["scope_violations"]
    for component in out["components"]:
        assert abs(component["score"] - 1.0) < 1e-6, component


def test_the_untouched_input_scores_zero():
    mod, plan, gt, init = fixtures()
    out = mod.score(plan, init, gt, init)
    assert out["score"] == 0.0, out["score"]
    assert all(c["score"] == 0.0 for c in out["components"]), out["components"]


def test_the_ground_truth_scores_one_through_evaluate_as_well():
    """Through the whole `evaluate` path, not only through `score`: the save
    contract, the stray scan and the shaping all sit between them."""
    mod, plan, gt, init = fixtures()
    result, _env = evaluate_on(mod, gt)
    assert abs(result["score"] - 1.0) < 1e-6, result["score"]
    assert result["failed_gate"] is None
    assert all(abs(p["score"] - 1.0) < 1e-6
               for p in result["partial_scores"].values())


def test_every_component_floor_is_below_the_limit():
    """A floor is what the *broken* file already scores.  A high one is not a
    tolerance to widen — it is a component that does not discriminate."""
    mod, plan, gt, init = fixtures()
    hot = [(c["id"], c["op"], c.get("floor")) for c in plan["components"]
           if (c.get("floor") or 0.0) > comparators_of(mod).FLOOR_LIMIT]
    assert not hot, hot


# =========================================================================== #
# 4. the rules in isolation
# =========================================================================== #


def test_every_operator_in_the_plan_has_a_comparator():
    mod, plan, gt, init = fixtures()
    missing = sorted({c["op"] for c in plan["components"]
                      if c["op"] not in comparators_of(mod).REGISTRY})
    assert not missing, missing


def test_a_component_whose_prior_value_is_missing_scores_zero_not_one():
    """The `ops.py` failure this evaluator exists not to repeat: an operator
    whose prior value went missing handed out full marks for doing nothing.

    Only the components that name a shape are mutated — a slide-level operator
    reads nothing off `gt_path`, so pointing it at a shape that is not there
    proves nothing about it either way.
    """
    mod, plan, gt, init = fixtures()
    addressed = [c for c in plan["components"] if c.get("gt_path")]
    if not addressed:
        return
    broken = copy.deepcopy(plan)
    broken["components"] = [dict(c, gt_path="no-such-shape", spec={})
                            for c in addressed]
    out = mod.score(broken, gt, gt, init)
    assert out["score"] == 0.0, (
        "a component that cannot find its answer paid out anyway: %s"
        % out["components"][:2])
    for component in out["components"]:
        assert "unscorable" in component["why"], component
        assert component["score"] == 0.0, component


def test_a_name_is_not_an_identity():
    """An agent can type any name it likes, so a pairing made on one earns
    nothing by itself — the `rename_only` attack was over the threshold on
    three decks before this held."""
    rules = comparators_of(task_module())
    assert rules._is_strong("txt:0123456789ab")
    assert rules._is_strong("ph:title:0")
    assert not rules._is_strong("name:Rectangle 7")
    assert not rules._is_strong("geo:textbox:1x0")
    assert not rules._is_strong("kind:autoshape")
    assert not rules._is_strong(None)


def test_a_weak_key_cannot_pair_shapes_that_are_nowhere_near_each_other():
    rules = comparators_of(task_module())
    here = {"cx": 1000000, "cy": 1000000, "w": 500000, "h": 500000}
    far = {"cx": 6000000, "cy": 1000000, "w": 500000, "h": 500000}
    assert rules._boxes_meet(here, dict(here))
    assert not rules._boxes_meet(here, far)
    assert rules._boxes_meet(here, None), "a placeholder states no geometry"


def test_a_theme_colour_written_out_as_rgb_is_the_same_colour():
    """A candidate that typed the sRGB a colour picker reports for the theme
    colour it was told to restore once scored 0.00 with every pixel right."""
    mod, plan, gt, init = fixtures()
    rules = comparators_of(mod)
    theme = (gt.get("package") or {}).get("theme_colors") or {}
    name = next(iter(theme), None)
    if name is None:
        assert rules._resolve_colour("scheme:accent1", {}) == "scheme:accent1"
        return
    token = "scheme:%s" % name.upper()
    assert rules._resolve_colour(token, theme) == "srgb:%s" % theme[name]
    assert rules._same_colour(token, "srgb:%s" % theme[name], theme)
    assert rules._resolve_colour(token + "+lumMod", theme) == token + "+lumMod", (
        "a modified colour is the renderer's arithmetic, not ours to guess")


def test_whitespace_is_not_a_difference_in_text():
    rules = comparators_of(task_module())
    assert rules._norm("Getting  Help\n") == "Getting Help"
    assert rules._norm(None) == ""


# =========================================================================== #
# 5. what a partly finished deck scores
# =========================================================================== #
#
# The category the last rollout needed and no suite had.  Three of four tasks
# scored 0.0 for work that was 43%, 53% and 63% done, and both endpoints were
# correct in every one of those evaluators.


def test_the_reconstruction_is_faithful():
    """Every component repaired must be worth exactly 1.0.

    This tests the helper, not the reward.  If it fails, `restore_component`
    cannot express some operator in this plan and every number below it is
    meaningless — fix the helper, do not touch the tolerances.
    """
    mod, plan, gt, init = fixtures()
    state, taken, missed = by_fraction(mod, plan, gt, init, 1.0)
    assert not missed, "no way to restore component(s) %s" % missed
    assert taken == len(plan["components"])
    out = mod.score(plan, state, gt, init)
    assert out["failed_gate"] is None, out["gate_reasons"]
    assert abs(out["score"] - 1.0) < 1e-6, (
        "a fully repaired deck scores %.4f: the reconstruction is not faithful"
        % out["score"])


def test_half_the_damage_repaired_lands_strictly_between_the_endpoints():
    mod, plan, gt, init = fixtures()
    if len(plan["components"]) < 2:
        return
    state, _taken, missed = by_fraction(mod, plan, gt, init, 0.5)
    assert not missed, missed
    out = mod.score(plan, state, gt, init)
    assert out["failed_gate"] is None, (
        "a gate fired on half-finished but correct work: %s" % out["gate_reasons"])
    assert out["score"] > 0.0, "half the work done scores the same as nothing"
    assert out["score"] < 1.0, "half the work done scores the same as all of it"


def test_more_repair_scores_more_than_less():
    mod, plan, gt, init = fixtures()
    if len(plan["components"]) < 4:
        return
    ladder = []
    for fraction in (0.25, 0.5, 0.75):
        state, taken, missed = by_fraction(mod, plan, gt, init, fraction)
        assert not missed, missed
        ladder.append((taken, scored(mod, plan, gt, init, state)))
    (n1, s1), (n2, s2), (n3, s3) = ladder
    assert n1 < n2 < n3
    assert s1 < s2 < s3, "the score does not rise with the work done: %s" % ladder
    assert 0.0 < s1 < 1.0


def test_one_degradation_fixed_and_another_missed_pays_for_the_one():
    """Credit for the one, and none for the other.  A task whose degradations
    cannot be scored apart is a task that cannot tell a model which half of
    the job it did."""
    mod, plan, gt, init = fixtures()
    groups = degradation_groups(plan)
    if len(groups) < 2:
        return
    declared = {d["id"]: d["weight"] for d in plan.get("degradations") or []}
    first = sorted(groups)[0]
    mine = {c["id"] for c in groups[first]}
    state, missed = partially_restored(mod, plan, gt, init, groups[first])
    assert not missed, missed
    out = mod.score(plan, state, gt, init)
    assert out["failed_gate"] is None, out["gate_reasons"]
    assert 0.0 < out["score"] < 1.0, out["score"]
    for component in out["components"]:
        if component["id"] in mine:
            assert abs(component["score"] - 1.0) < 1e-6, (
                "the degradation that was fixed is not paid for: %s" % component)
        else:
            assert component["score"] == 0.0, (
                "a degradation nobody touched is paid for: %s" % component)
    if declared.get(first):
        assert abs(out["score"] - declared[first]) < 1e-3, (
            "%s is worth %.4f but scored %.4f"
            % (first, declared[first], out["score"]))


def test_a_deck_saved_under_another_name_is_scored_not_zeroed():
    """Losing a whole result to a Save-As is a scoring artefact, not a
    capability signal: the work was done, only the filename was wrong."""
    mod, plan, gt, init = fixtures()
    state, _taken, missed = by_fraction(mod, plan, gt, init, 0.5)
    assert not missed, missed
    direct = scored(mod, plan, gt, init, state)
    stray = "/home/user/Desktop/presentation copy.pptx"
    result, env = evaluate_on(mod, state, disk_sha=mod.INIT_SHA256, stray=stray)
    assert env.fetched == [mod.DECK_VM_PATH, stray], env.fetched
    assert result["evidence"]["scored_file"] == stray
    assert result["score"] > 0.0, "a Save-As zeroed work that was done"
    assert abs(result["score"] - direct) < 1e-6, (
        "the recovered file scores differently from the same state in place")


def test_a_re_encoded_image_on_a_page_nobody_touched_costs_nothing():
    """WPS re-encodes a minority of images on open-and-save.  On a page the
    task never named that is the application's doing and must cost nothing —
    `_page_facts` records *that* a shape draws an image, never which bytes.

    It records the value that way and then addresses the record by the blob,
    which is what this measures.
    """
    mod, plan, gt, init = fixtures()
    state = re_encoded(plan, gt, only_untouched=True)
    if state["package"]["media"] == gt["package"]["media"]:
        return
    out = mod.score(plan, state, gt, init)
    assert out["failed_gate"] is None, out["gate_reasons"]
    assert abs(out["score"] - 1.0) < 1e-6, (
        "an application re-encoding images on pages nobody asked about costs "
        "%.4f: scope penalty %s, %s"
        % (1.0 - out["score"], out["penalty"],
           out["scope_violations"] or "no scope violation"))


def test_a_re_encoded_image_the_task_asked_to_restore_costs_nothing():
    """The same application, the same re-encoding, on the picture the agent
    was asked to put back — inserted byte-for-byte from `assets/materials/`
    and then re-encoded by WPS on save.

    The work is correct and complete; the bytes are the application's.  This
    assertion is the one that says so.
    """
    mod, plan, gt, init = fixtures()
    state = re_encoded(plan, gt, only_untouched=False)
    if state["package"]["media"] == gt["package"]["media"]:
        return
    out = mod.score(plan, state, gt, init)
    assert out["failed_gate"] is None, out["gate_reasons"]
    assert abs(out["score"] - 1.0) < 1e-6, (
        "a perfect repair whose images the application re-encoded scores "
        "%.4f — the agent is charged %.4f for something it did not do"
        % (out["score"], 1.0 - out["score"]))


def test_over_eagerness_costs_a_fraction_not_everything():
    """The 1100001 casualty in miniature: a model that added a logo to two
    pages whose ground truth has none had done 43% of the work when a hard
    gate threw all of it away."""
    mod, plan, gt, init = fixtures()
    state = comparators_of(mod)._state_over_eager(plan, gt)
    if state is None:
        return
    out = mod.score(plan, state, gt, init)
    assert out["failed_gate"] is None, (
        "over-eagerness fired a cheat gate: %s" % out["gate_reasons"])
    assert out["score"] > 0.0, "over-eagerness alone zeroed the score"
    assert out["score"] < 1.0, "a scope violation cost nothing"
    assert out["score"] >= 0.5 * out["unweighted"], (
        "over-eagerness took more than half of what was earned")


def test_the_answer_reached_by_another_route_still_scores():
    """A native object rebuilt out of ordinary shapes is the work, not a
    cheat — one deck's instruction asks for exactly that — and a gate must
    never overrule a component that is at that moment awarding credit."""
    mod, plan, gt, init = fixtures()
    state = comparators_of(mod)._state_rebuilt(plan, gt)
    out = mod.score(plan, state, gt, init)
    assert out["failed_gate"] is None, (
        "a gate fired on a hand-rebuilt answer: %s" % out["gate_reasons"])
    assert out["score"] > 0.0


# =========================================================================== #
# the save contract
# =========================================================================== #
#
# Two failures pull in opposite directions and one hash settles both.  Saving
# unconditionally writes the application's stale copy over work the agent
# already saved — one rollout lost a measured 0.53 that way and recorded 0.0.
# Never saving loses work that only exists in the GUI.  The branches have to
# differ in what they *do*, so the assertion is on the command sequence.


def test_a_save_is_forced_only_when_nothing_has_been_written_out():
    mod = task_module()
    env = FakeEnv(mod.INIT_SHA256, {mod.DECK_VM_PATH: init_pptx()}).install(mod)
    result = mod.TASK_CLASS().evaluate(env)
    assert env.commands == ["sha", "save", "kill", "unlock", "sha", "scan"], (
        env.commands)
    assert result["evidence"]["save_attempted"] is True
    assert result["evidence"]["disk_changed_before_save"] is False


def test_a_deck_already_written_to_disk_is_never_saved_over():
    mod = task_module()
    env = FakeEnv("b" * 64, {mod.DECK_VM_PATH: init_pptx()}).install(mod)
    mod.TASK_CLASS().evaluate(env)
    assert env.commands == ["sha", "kill", "unlock", "sha"], env.commands
    assert "save" not in env.commands, (
        "the agent's saved work was about to be overwritten")


def test_the_application_is_closed_without_being_asked_to_save():
    """`pkill` and then delete the lock file: anything the application still
    holds is either already on disk or was never wanted."""
    mod = task_module()
    env = FakeEnv("b" * 64, {mod.DECK_VM_PATH: init_pptx()}).install(mod)
    mod.TASK_CLASS().evaluate(env)
    assert env.commands.index("kill") < env.commands.index("unlock")
'''


# --------------------------------------------------------------------------- #
# running the generated suite
# --------------------------------------------------------------------------- #


def run_generated(test_py: Path, task_py: Path) -> list[dict]:
    """Import the file and call every `test_*` in source order.

    The archived runner's contract exactly: a test passes if it returns
    without raising.  Running it here rather than shelling out to pytest is
    what lets the report carry the results instead of pointing at them.
    """
    test_py = Path(test_py)
    order = _test_order(test_py)
    name = f"generated_task_tests_{test_py.parent.parent.name}"
    spec = importlib.util.spec_from_file_location(name, test_py)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    import os

    previous = os.environ.get("PPTXGYM_TASK_PY")
    os.environ["PPTXGYM_TASK_PY"] = str(task_py)
    try:
        spec.loader.exec_module(module)
        out = []
        for fname in order:
            fn = getattr(module, fname, None)
            if fn is None:
                continue
            row = {"test": fname, "ok": True, "error": ""}
            try:
                fn()
            except Exception as error:                        # noqa: BLE001
                row["ok"] = False
                row["error"] = f"{type(error).__name__}: {error}".strip()
                row["trace"] = traceback.format_exc(limit=3)
            out.append(row)
        return out
    finally:
        sys.modules.pop(name, None)
        if previous is None:
            os.environ.pop("PPTXGYM_TASK_PY", None)
        else:
            os.environ["PPTXGYM_TASK_PY"] = previous


def _test_order(test_py: Path) -> list[str]:
    import ast

    tree = ast.parse(test_py.read_text())
    return [n.name for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]


# --------------------------------------------------------------------------- #
# the calibration table
# --------------------------------------------------------------------------- #


def _load_task(task_py: Path):
    """Import a generated task the way the harness would, stubs and all."""
    for name in ("desktop_env", "desktop_env.task_base",
                 "desktop_env.evaluators"):
        sys.modules.setdefault(name, types.ModuleType(name))
    if not hasattr(sys.modules["desktop_env.task_base"], "BaseTask"):
        sys.modules["desktop_env.task_base"].BaseTask = object
    getters = types.ModuleType("desktop_env.evaluators.getters")
    getters.get_vm_file = lambda *a, **k: None
    getters.get_vm_command_line = lambda *a, **k: ""
    sys.modules["desktop_env.evaluators.getters"] = getters
    name = f"packaged_task_report_{task_py.stem}"
    spec = importlib.util.spec_from_file_location(name, task_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def calibration(task_py: Path, adir: Path) -> dict:
    """What the reward pays for states between the two endpoints.

    The point of the whole exercise, in one table: a reward that is right at
    both ends and flat in the middle is the reward that scored three models
    0.0 for 43%, 53% and 63% of a job.
    """
    import copy as _copy

    mod = _load_task(Path(task_py))
    tests = Path(adir) / "tests" / "assets"
    plan = json.loads((tests / "plan.json").read_text())
    gt = json.loads((tests / "gt_inventory.json").read_text())
    init = json.loads((tests / "init_inventory.json").read_text())

    # the generated suite owns the reconstruction; reuse it rather than
    # reimplement it, so the report and the tests can never disagree.
    suite = _import_helpers(Path(adir) / "tests" / "test_task.py", task_py)
    rules = suite.comparators_of(mod)

    rows: dict[str, dict] = {}

    def put(label, state):
        if state is None:
            return
        out = mod.score(plan, state, gt, init)
        rows[label] = {"score": round(out["score"], 4),
                       "gate": out["failed_gate"],
                       "penalty": out["penalty"]}

    put("untouched input (nothing done)", _copy.deepcopy(init))
    for fraction in (0.25, 0.5, 0.75):
        state, taken, missed = suite.by_fraction(mod, plan, gt, init, fraction)
        label = f"{int(fraction * 100)}% of the components repaired ({taken})"
        put(label, state)
        if missed:
            rows[label]["gate"] = f"unrestorable: {missed}"
    put("every component repaired", suite.by_fraction(mod, plan, gt, init, 1.0)[0])
    put("ground truth", gt)
    put("ground truth, half the pages (library `_state_half`)",
        rules._state_half(plan, gt, init))
    put("ground truth, composites rebuilt by hand (`_state_rebuilt`)",
        rules._state_rebuilt(plan, gt))
    put("ground truth + one shape nobody asked for (`_state_over_eager`)",
        rules._state_over_eager(plan, gt))
    put("ground truth, untouched pages' images re-encoded",
        suite.re_encoded(plan, gt, only_untouched=True))
    put("ground truth, every image re-encoded",
        suite.re_encoded(plan, gt, only_untouched=False))

    groups = suite.degradation_groups(plan)
    per_deg = {}
    declared = {d["id"]: d["weight"] for d in plan.get("degradations") or []}
    for deg in sorted(groups):
        state, _missed = suite.partially_restored(mod, plan, gt, init,
                                                  groups[deg])
        out = mod.score(plan, state, gt, init)
        per_deg[deg] = {"score": round(out["score"], 4),
                        "declared": round(float(declared.get(deg, 0.0)), 4),
                        "components": len(groups[deg])}
    return {"states": rows, "per_degradation": per_deg}


def _import_helpers(test_py: Path, task_py: Path):
    """The generated suite as a module, for its reconstruction helpers."""
    import os

    previous = os.environ.get("PPTXGYM_TASK_PY")
    os.environ["PPTXGYM_TASK_PY"] = str(task_py)
    try:
        name = f"generated_helpers_{Path(test_py).parent.parent.name}"
        spec = importlib.util.spec_from_file_location(name, test_py)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            os.environ.pop("PPTXGYM_TASK_PY", None)
        else:
            os.environ["PPTXGYM_TASK_PY"] = previous


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #


def _issues(plan: dict, cal: dict, results: list[dict]) -> list[str]:
    """What writing and running the tests turned up, derived from the data.

    The archived skill calls this section the most valuable output of a test
    pass, and it is right: everything above it is a number that was expected,
    and this is where the cross-checks against the plan and the evaluator
    surface.  Nothing here is boilerplate — every line is either measured on
    this task or absent.
    """
    out = []
    states = cal["states"]

    def score_of(label):
        for key, row in states.items():
            if key.startswith(label):
                return row["score"]
        return None

    perfect = score_of("ground truth, every image re-encoded")
    idle = score_of("ground truth, untouched pages' images re-encoded")
    row = states.get("ground truth, every image re-encoded") or {}
    if perfect is not None and perfect < 1.0:
        out.append(
            f"**A perfect repair whose images the application re-encoded "
            f"scores {perfect:.4f}** — the agent is charged {1 - perfect:.4f} "
            f"for something it did not do, and is charged it precisely when it "
            f"does the *right* thing, which is to insert the original supplied "
            f"in `assets/materials/`. Two independent charges, and they need "
            f"different fixes: `_facet_picture` compares blob digests exactly "
            f"and is *multiplied* by the geometry, so the component goes to 0 "
            f"however well the picture is placed; and `_page_facts` files "
            f"every fact about a shape under `shape['key']`, which for a "
            f"picture is the digest itself, so a re-encode on a page nobody "
            f"touched reads as a deleted shape plus an added one and costs a "
            f"scope penalty of {row.get('penalty')}. No hard gate fires — the "
            f"loss is graded, not a zeroing.")
    if idle is not None and idle < 1.0:
        out.append(
            f"The same re-encoding confined to pages the task never named "
            f"still scores {idle:.4f}. That isolates the second charge: it is "
            f"an identity problem, not a comparison policy. `_page_facts` "
            f"deliberately records *that* a shape draws an image rather than "
            f"which bytes — 'the blob is the application's to change' — and "
            f"then addresses the record by the blob.")

    rebuilt = score_of("ground truth, composites rebuilt by hand")
    if rebuilt is not None and rebuilt < 0.9:
        out.append(
            f"The answer reached by a different route — every native composite "
            f"the task damaged re-made out of ordinary shapes carrying the same "
            f"words — scores {rebuilt:.4f}. No gate fires on it, which is the "
            f"property the coherence probe exists to protect; what it does not "
            f"protect is the *component*, and a solver who rebuilds rather than "
            f"restores is paid {1 - rebuilt:.0%} less for an outcome that looks "
            f"the same on the slide. Worth knowing before reading a rollout "
            f"score as a capability signal.")

    unscoreable = plan.get("unscoreable") or []
    if unscoreable:
        first = unscoreable[0]
        out.append(
            f"{len(unscoreable)} component(s) were dropped because the ground "
            f"truth itself cannot satisfy them — e.g. `{first['op']}` on slide "
            f"{first['slide']}: {first['why']}. They are damage the agent can "
            f"see and repair for no credit, which is the quiet half of an "
            f"instruction that asks for more than it scores.")

    if plan.get("weight_source") != "est_steps":
        out.append(
            f"Weights come from `{plan.get('weight_source')}`, not from the "
            f"estimated GUI steps. Every component is then worth the same "
            f"regardless of how much work it represents, so the partial score "
            f"stops being a measure of progress and becomes a count.")

    floors = [(c.get("floor") or 0.0, c["id"], c["op"])
              for c in plan["components"]]
    top = max(floors, default=(0.0, "", ""))
    if top[0] > 0.0:
        out.append(
            f"The highest measured floor is {top[0]:.3f} on `{top[1]}` "
            f"({top[2]}) — that fraction of the component is already satisfied "
            f"by the broken file and is normalised away rather than paid for.")

    weights = sorted(((float(c["weight"]), c["id"], c["op"])
                      for c in plan["components"]), reverse=True)
    if weights and weights[0][0] >= 0.30:
        out.append(
            f"One component carries {weights[0][0]:.1%} of the weight "
            f"(`{weights[0][1]}`, {weights[0][2]}). Partial credit is only as "
            f"fine-grained as the weights: an agent that does everything else "
            f"and misses that one is capped at {1 - weights[0][0]:.2f}.")

    damaged = sorted(set(plan["damage"]["slides"]))
    if len(damaged) < 2:
        out.append(
            "The deck has one damaged page, so `comparators._state_half` — "
            "which restores half the damaged *pages* — is the ground truth "
            "here and cannot express a half-finished job. The partial-credit "
            "tests use a component-granular reconstruction instead.")
    else:
        multi = {}
        for component in plan["components"]:
            multi[component["slide"]] = multi.get(component["slide"], 0) + 1
        crowded = max(multi.values())
        if crowded > 1:
            out.append(
                f"One page carries {crowded} components, so `_state_half`'s "
                f"page granularity awards all {crowded} at once; the "
                f"partial-credit tests reconstruct per component instead, "
                f"which is what \"a quarter of the work\" means to a solver.")

    if plan.get("init_slide_of") is None:
        out.append(
            "`comparators._init_slide_of` returns `None` on **both** of its "
            "branches — a deck whose recipe deleted or reordered slides gets "
            "the identity mapping anyway, and the floor would then be measured "
            "against the wrong pages. Harmless on this task (nothing in the "
            "recipe moves a page) and a landmine for the first task that does.")

    if any(not c.get("gt_path") for c in plan["components"]):
        n = sum(1 for c in plan["components"] if not c.get("gt_path"))
        out.append(
            f"{n} component(s) name no shape path and are resolved by their "
            f"data part instead (`_find_smartart` / `_find_chart`). A second "
            f"SmartArt appearing on that slide — which is a thing an agent may "
            f"legitimately do — makes the object unidentifiable, and an "
            f"unidentifiable component scores 0 rather than being skipped.")

    out.append(
        "`_stray_candidate` takes the first `.pptx` under `~/Desktop` or "
        "`~` at depth 2 that is newer than the pinned deck, and scores it. "
        "Nothing checks that it is the agent's result: a `.pptx` among the "
        "supplied materials, or one left behind by a previous task, is "
        "equally eligible, and `head -1` picks by directory order.")

    out.append(
        "`_SAVE_SCRIPT` reports `SAVED` when the *keystroke* was delivered, "
        "not when the file changed — the fallback path sends `ctrl+s` through "
        "pyautogui to whatever window has focus. The evidence that matters is "
        "`disk_sha_after`, which `evaluate` does record; `save_status` should "
        "not be read as more than a hint.")

    unexpected = [r for r in results
                  if not r["ok"] and r["test"] not in KNOWN_FINDINGS]
    for row in unexpected:
        out.append(f"**{row['test']} failed unexpectedly:** {row['error']}")
    return out


def report(task_id: str, task_py: Path, adir: Path, plan: dict,
           cal: dict, results: list[dict]) -> str:
    findings = [r for r in results if not r["ok"] and r["test"] in KNOWN_FINDINGS]
    unexpected = [r for r in results
                  if not r["ok"] and r["test"] not in KNOWN_FINDINGS]
    passed = sum(1 for r in results if r["ok"])

    if unexpected:
        verdict = (f"**fail** — {len(unexpected)} unexpected failure(s); "
                   f"these are defects, not findings")
    elif findings:
        verdict = (f"**fail** — {len(findings)} assertion(s) fail, and each is "
                   f"a finding about the reward rather than a bug in the test. "
                   f"They are listed below and deliberately not softened.")
    else:
        verdict = "**pass**"

    lines = [f"# task_{task_id} — test report", "",
             f"- **task** `{task_py}`",
             f"- **tests** `{Path(adir) / 'tests' / 'test_task.py'}`",
             f"- **source deck** `{plan.get('deck')}` · "
             f"{len(plan['components'])} components · "
             f"{len(plan.get('degradations') or [])} degradations",
             f"- **verdict** {verdict}",
             f"- **result** {passed}/{len(results)} passing", "",
             "## How it was run", "",
             "```", f"python3 -m pptxgym.emit_tests --out <out> "
                    f"--task-id {task_id}",
             f"python3 -m pytest {Path(adir).name}/tests/test_task.py -q",
             "```", "",
             "Both do the same thing: import the generated task, call every "
             "`test_*` in source order, and count a test as passed if it "
             "returns without raising.", "",
             "## What the reward pays for a partly finished deck", "",
             "The table the last rollout needed. Three of four tasks scored "
             "0.0 for work that was 43%, 53% and 63% done, and both endpoints "
             "were correct in every one of those evaluators — a suite that "
             "checks only the endpoints cannot see the failure.", "",
             "| state | score | failed gate | scope penalty |",
             "| --- | ---: | --- | ---: |"]
    for label, row in cal["states"].items():
        lines.append(f"| {label} | {row['score']:.4f} | "
                     f"{row['gate'] or '—'} | {row['penalty']} |")
    if cal["per_degradation"]:
        lines += ["", "One degradation repaired and the rest left alone:", "",
                  "| degradation | components | score | declared weight |",
                  "| --- | ---: | ---: | ---: |"]
        for deg, row in cal["per_degradation"].items():
            lines.append(f"| `{deg}` | {row['components']} | "
                         f"{row['score']:.4f} | {row['declared']:.4f} |")

    lines += ["", "## Tests", "", "| test | result | detail |",
              "| --- | --- | --- |"]
    for row in results:
        if row["ok"]:
            mark, detail = "pass", ""
        elif row["test"] in KNOWN_FINDINGS:
            mark, detail = "**fail (finding)**", row["error"]
        else:
            mark, detail = "**fail**", row["error"]
        lines.append(f"| `{row['test']}` | {mark} | {detail.replace('|', '/')} |")

    if findings:
        lines += ["", "## The failing assertions, and why they stay", "",
                  MEASURED_PREMISE, ""]
        for row in findings:
            lines += [f"### `{row['test']}`", "",
                      f"```\n{row['error']}\n```", "",
                      KNOWN_FINDINGS[row["test"]], ""]

    lines += ["", "## Issues noticed while writing the tests", ""]
    for item in _issues(plan, cal, results):
        lines.append(f"- {item}")
    lines += ["",
              "## What these tests cannot reach", "",
              "- The ground-truth deck is deliberately not shipped, so the "
              "end-to-end calibration runs against the ground-truth "
              "*inventory* — which is what the evaluator itself compares "
              "against, and one parse short of the file.",
              "- Every state between the endpoints is built as an inventory, "
              "not as a `.pptx` opened and saved by WPS. What an application "
              "does to a file on the way through is measured in "
              "`roundtrip-wps.json`, not here; the re-encoding row above is "
              "this suite's one simulation of it.",
              "- Nothing here proves the agent could do the work through a "
              "GUI. That is the solvability probe's question.", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


GITIGNORE = """\
# regenerated by the test runner — the report beside them is the committed one
task-test-results.json
task-test-results.md
test-work/
__pycache__/
"""


def emit_tests(py, adir, task_id: str, *, run: bool = True) -> dict:
    """Write `tests/test_task.py` and its report for one packaged task."""
    py = Path(py)
    adir = Path(adir)
    tests = adir / "tests"
    plan_file = tests / "assets" / "plan.json"
    if not py.exists():
        raise EmitTestsError(f"no task file at {py}")
    if not plan_file.exists():
        raise EmitTestsError(f"no plan beside the task at {plan_file}")
    plan = json.loads(plan_file.read_text())
    if not plan.get("components"):
        raise EmitTestsError(f"{task_id}: the plan scores nothing")

    tests.mkdir(parents=True, exist_ok=True)
    test_py = tests / "test_task.py"
    test_py.write_text(_header(task_id, plan.get("deck") or "?", plan)
                       + TEST_BODY)
    (tests / ".gitignore").write_text(GITIGNORE)

    out = {"task_id": task_id, "tests": str(test_py),
           "report": None, "results": [], "passed": 0, "failed": 0,
           "findings": []}
    if not run:
        return out

    results = run_generated(test_py, py)
    cal = calibration(py, adir)
    text = report(task_id, py, adir, plan, cal, results)
    report_file = tests / f"task-test-report-{task_id}.md"
    report_file.write_text(text)

    out["report"] = str(report_file)
    out["results"] = results
    out["calibration"] = cal
    out["passed"] = sum(1 for r in results if r["ok"])
    out["failed"] = sum(1 for r in results if not r["ok"])
    out["findings"] = [r["test"] for r in results
                       if not r["ok"] and r["test"] in KNOWN_FINDINGS]
    out["unexpected"] = [r for r in results
                         if not r["ok"] and r["test"] not in KNOWN_FINDINGS]
    return out


def for_emitted(emitted: dict, *, run: bool = True) -> dict:
    """Generate the suite for what `emit.emit` just returned."""
    return emit_tests(emitted["py"], emitted["assets"], emitted["task_id"],
                      run=run)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True,
                    help="the directory `emit` wrote the task into")
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--no-run", action="store_true",
                    help="write the tests without running them (no report)")
    args = ap.parse_args(argv)

    out = Path(args.out)
    py = out / "task_class" / f"task_{args.task_id}.py"
    adir = out / "task_assets" / f"task_{args.task_id}"
    result = emit_tests(py, adir, args.task_id, run=not args.no_run)
    print(f"tests   {result['tests']}")
    if result["report"]:
        print(f"report  {result['report']}")
        print(f"        {result['passed']} passed, {result['failed']} failed"
              + (f" ({len(result['findings'])} of them known reward findings)"
                 if result["findings"] else ""))
        for row in result.get("unexpected") or []:
            print(f"  ✗ {row['test']}: {row['error']}")
    return 0 if not (result.get("unexpected") or []) else 2


if __name__ == "__main__":
    raise SystemExit(main())
