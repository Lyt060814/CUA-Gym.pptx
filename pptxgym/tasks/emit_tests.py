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

    python3 -m pptxgym.tasks.emit_tests --out OUT --task-id 9900003
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
        "**The half that is left, and it is a priced decision rather than a "
        "defect.** `_facet_picture` compares image bytes exactly and is "
        "*multiplied* by the geometry in `_cmp_restored_shape`, so a picture "
        "put back in exactly the right place at exactly the right size scores "
        "**0** for its whole component if the bytes moved. Measured today, "
        "with the identity half fixed and the scope penalty at 0.0: a "
        "perfect deck whose every image was re-encoded scores **0.5614 "
        "(deck0003), 0.6429 (deck0008), 0.7051 (deck0007)** — the loss is "
        "entirely this facet, and no gate fires.\n\n"
        "**The multiply is not the fragile part and must not be softened.** "
        "Averaging content with geometry would pay half a component for a "
        "picture-shaped rectangle of the *wrong* image in the right place, "
        "and \"paste something roughly there\" being cheaper than restoring "
        "the thing is the move a training run finds first. The fragile part "
        "is that the exact bytes are the only identity this evaluator has for "
        "an image: `inventory._keys` and `_picture_of` record the blob digest "
        "and the crop — no pixel dimensions, no format, no perceptual hash — "
        "so there is nothing to fall back to.\n\n"
        "**Why no tolerance is built.** docs/design/reward.md §4's bar is that a "
        "tolerance must not pay a cheat, and the cheat here is concrete: swap "
        "in a *different* image and call it re-encoding. Dimensions and "
        "format would not stop it — a different photo of the same size passes "
        "both — so the only discriminator is a perceptual hash, which needs a "
        "decoder for what the corpus actually holds: across the ten decks' "
        "slide pictures, **29 EMF, 19 GIF, 15 TIFF and 5 WMF** beside the PNG "
        "and JPEG, and 34 of those are vector metafiles with no pixels at all "
        "until something renders them. `inventory` and `comparators` are "
        "stdlib-only by contract, because `emit` pastes them into a task file "
        "that runs where python-pptx may not be installed, so Pillow can be a "
        "dependency of the pipeline and not of them. The cost of leaving it "
        "is bounded and priced above; the cost of a tolerance built on "
        "anything weaker than a perceptual hash is the whole task family.\n\n"
        "**And the premise is synthetic** — see the note reproduced above "
        "this section. A real WPS open-and-save re-encodes no slide picture "
        "at all, so this becomes live only if the file passes through "
        "LibreOffice, and then the fix is the environment rather than a "
        "number here."),
}

#: Findings that *were* here and are now fixed, kept as a record of what the
#: assertion is for.  A test named here that fails again is a regression, not
#: a rediscovery, and the difference matters: the entry above it was softened
#: into prose about "the comparator half" and "the identity half" back when
#: both failed, and half of that prose stayed true-looking for a release after
#: it stopped being true.
FIXED_FINDINGS = {
    "test_a_re_encoded_image_on_a_page_nobody_touched_costs_nothing": (
        "**Fixed.** `_page_facts` used to file every fact about a shape at "
        "`shape['key']`, which for a picture *is* the blob digest, so "
        "re-encoding moved every fact to a new address and "
        "`_scope_untouched_pages` read one untouched picture as a deletion "
        "plus an addition — 13 such 'changes' on deck0003 and 6 on deck0008, "
        "both hitting the 0.30 cap, taking a perfect deck to 0.393 and 0.450. "
        "The address now comes from the pairing (`pair_slide_detail`), the "
        "same one every component is scored through, so both sides of a "
        "comparison name the same shape by the same name. Measured today: "
        "re-encoding every image on the pages the task never named scores "
        "1.0000 with a scope penalty of 0.0."),
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

Do not edit by hand — regenerate with `python3 -m pptxgym.tasks.emit_tests`.

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


def local_material(repo_path):
    """A local copy of one of the files `setup` fetches, as a `Path`.

    The materials used to sit beside this suite in `assets/` and `setup`
    uploaded them from there.  They are fetched from a dataset now -- 2-13 MB
    a task, which does not belong in a repository the whole benchmark clones
    -- so the copy this suite reads is the one left in the staging directory
    the package was built in, and it is found by the name in the task's own
    `FETCH` list rather than by a path spelled again here.

    Skipped rather than failed when there is none: run against a checkout of
    the benchmark repository, "the materials are in the dataset" is the
    correct state and not a defect.  `PPTXGYM_ASSET_BASE` points this at a
    local mirror.
    """
    import os
    mod = task_module()
    rel = repo_path.split("/", 1)[1] if "/" in repo_path else repo_path
    candidates = [mod.ASSETS_DIR / "assets" / rel]
    base = (os.environ.get(mod.ASSET_BASE_ENV) or "").strip().rstrip("/")
    if base and "://" not in base:
        candidates.insert(0, Path(base) / repo_path)
    for candidate in candidates:
        if Path(candidate).exists():
            return Path(candidate)
    import pytest as _pytest
    _pytest.skip("%s is fetched from %s at run time and there is no local copy "
                 "beside this suite; point %s at a mirror to run this"
                 % (rel, mod.HF_ASSET_REPO, mod.ASSET_BASE_ENV))


def init_pptx():
    """The deck `setup` puts on the VM, as a local file."""
    return str(local_material(task_module().FETCH[0][0]))


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeEnv:
    """Serves one local file per VM path and records what `evaluate` runs.

    `disk_sha` is what `sha256sum` reports for the deck at the pinned path —
    the single fact the save contract turns on.  `saved_sha`, when given, is
    what it reports *after* the forced save, which is how a save that reached
    the disk is told from a keystroke that went nowhere.

    `strays` is `{path: digest}`: the scan digests what it finds, because the
    digest is what tells the agent's result from a file the task supplied.

    `reference` is what the machine reports it measured "newer" against.  The
    scan resolves that on the VM -- the marker `setup` writes, else the
    materials folder, else the pinned deck -- and `"none"` is a real answer
    meaning none of the three survived, which is not the same as finding no
    files.  `stray_lines` is raw scan output, for the one question a
    `{path: digest}` dict cannot ask: `find` is given two overlapping roots
    and reports the same Desktop file twice.
    """

    def __init__(self, disk_sha, files, keystroke="KEYSTROKE_SENT", stray="",
                 strays=None, saved_sha=None, reference="/home/user/.setup",
                 stray_lines=None):
        self.disk_sha = disk_sha
        self.files = files
        self.keystroke = keystroke
        self.saved_sha = saved_sha
        self.strays = dict(strays or {})
        self.reference = reference
        self.stray_lines = stray_lines
        if stray:
            self.strays.setdefault(stray, "c" * 64)
        self.commands = []
        self.fetched = []

    def install(self, mod):
        def command_line(_env, config):
            command = config["command"][-1]
            kind = self.kind(command)
            self.commands.append(kind)
            if kind == "scan":
                head = ("REFERENCE %s\n" % self.reference
                        if self.reference else "")
                if self.stray_lines is not None:
                    return head + "".join("%s\n" % ln for ln in self.stray_lines)
                if self.reference == "none":
                    return head
                return head + "".join("%s  %s\n" % (digest, path)
                                      for path, digest in self.strays.items())
            if kind == "sha":
                return (self.disk_sha or "MISSING") + "\n"
            if kind == "save":
                if self.saved_sha is not None:
                    self.disk_sha = self.saved_sha
                return self.keystroke + "\n"
            return ""

        def vm_file(_env, config):
            self.fetched.append(config["path"])
            return self.files.get(config["path"])

        mod.get_vm_command_line = command_line
        mod.get_vm_file = vm_file
        return self

    @staticmethod
    def kind(command):
        # the scan digests what it finds, so it is asked about before the
        # plain `sha256sum` of the pinned deck: its command contains one too.
        # Matched on what it looks for rather than on its first word: the scan
        # resolves its own reference point in the shell before it runs `find`.
        if "-name '*.pptx'" in command:
            return "scan"
        if "sha256sum" in command:
            return "sha"
        if "ctrl+s" in command:
            return "save"
        if command.startswith("pkill"):
            return "kill"
        if ".~lock." in command:
            return "unlock"
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


class FetchController(FakeController):
    """A controller with the harness's download cache, so the digest runs.

    `SetupController._download_setup` writes what it fetched into `cache_dir`
    before uploading it to the machine, and that copy is the only place on
    this side of the wire where the bytes can be checked.  A controller
    without a `cache_dir` -- `FakeController` above -- makes the task's
    `_verify_fetched` return early, so the plain fake can prove the files were
    *asked for* and nothing more.  This one proves they were the right ones.

    `corrupt` serves something else under the right name, which is what a
    Git-LFS pointer, a half-written mirror or a rewritten dataset folder all
    look like: a fetch that succeeds and returns bytes that are not the task's.
    """

    def __init__(self, cache, corrupt=False):
        FakeController.__init__(self)
        self.cache_dir = str(cache)
        self.corrupt = corrupt
        Path(cache).mkdir(parents=True, exist_ok=True)

    def download(self, files):
        import uuid
        FakeController.download(self, files)
        mod = task_module()
        for spec, entry in zip(files, mod.FETCH):
            dest = Path(self.cache_dir) / "{:}_{:}".format(
                uuid.uuid5(uuid.NAMESPACE_URL, spec["url"]),
                Path(spec["path"]).name)
            if self.corrupt:
                dest.write_bytes(b"not the file this task asked for")
            else:
                dest.write_bytes(local_material(entry[0]).read_bytes())


class BrokenFetchController(FakeController):
    """A dataset that will not serve. `download` raises; so must `setup`."""

    def download(self, files):
        FakeController.download(self, files)
        raise RuntimeError("404 Client Error for url")


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
    env = FakeEnv(disk_sha, files, keystroke="KEYSTROKE_NOT_SENT",
                  stray=stray).install(mod)
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


def test_setup_fetches_the_deck_and_the_materials_and_nothing_else():
    """The upload list is the answer-key question, and it got sharper.

    A delta-derived evaluator must read the ground truth, so the usual "no
    fixtures in the evaluator" rule cannot hold; what replaces it is that
    nothing the evaluator reads may reach the machine the agent works on.

    That list is no longer a set of local paths uploaded to one VM. It is a
    set of URLs in a **public** Hugging Face dataset, so a ground-truth file
    that reaches `FETCH` is not merely handed to one agent -- it is published,
    to anyone, permanently, and a dataset commit cannot be taken back the way
    a local file can. The same names, checked harder.
    """
    mod = task_module()
    controller = FakeController()
    mod.TASK_CLASS().setup(controller, use_proxy=False)
    fetched = [f for batch in controller.of("download") for f in batch]
    assert fetched, "setup fetched nothing, so the agent gets an empty machine"
    assert fetched[0]["path"] == mod.DECK_VM_PATH
    assert [f["path"] for f in fetched] == [vm for _, vm, _ in mod.FETCH]
    assert controller.of("upload") == [], (
        "the materials are still being uploaded from beside the task")
    secret = {"plan.json", "gt_inventory.json", "init_inventory.json"}
    for item in fetched:
        name = item["url"].rsplit("/", 1)[-1]
        assert name not in secret, (
            "%s would be published to %s, where the answer key is readable by "
            "anyone and the commit cannot be withdrawn"
            % (name, mod.HF_ASSET_REPO))
        assert "/tests/" not in item["url"], "%s is answer-key material" % item
        assert item["path"].startswith("/home/user/Desktop/")
    assert controller.of("launch") == [["wpp", mod.DECK_VM_PATH]]
    for _repo_path, _vm, digest in mod.FETCH:
        assert len(digest) == 64, (
            "a fetched file with no recorded digest cannot be told from the "
            "wrong file arriving under the right name")


def test_the_fetched_bytes_are_checked_against_their_digests():
    """"Fetched" and "fetched the right thing" are different answers.

    `download` raises when the fetch itself fails. What it cannot notice is a
    fetch that *succeeded* and returned something else -- a Git-LFS pointer, a
    mirror one commit behind, a dataset folder rewritten under the same names.
    Those arrive as bytes, upload cleanly, and hand the agent somebody else's
    deck; the score that comes back is a real number about the wrong task.
    """
    import tempfile
    mod = task_module()
    local_material(mod.FETCH[0][0])              # skip early if none is here
    with tempfile.TemporaryDirectory() as cache:
        controller = FetchController(cache)
        mod.TASK_CLASS().setup(controller, use_proxy=False)
        assert controller.of("launch") == [["wpp", mod.DECK_VM_PATH]]


def test_a_fetch_that_returns_the_wrong_bytes_stops_the_episode():
    import tempfile
    mod = task_module()
    local_material(mod.FETCH[0][0])
    with tempfile.TemporaryDirectory() as cache:
        controller = FetchController(cache, corrupt=True)
        try:
            mod.TASK_CLASS().setup(controller, use_proxy=False)
        except mod.AssetFetchError as error:
            assert "did not arrive intact" in str(error)
        else:
            raise AssertionError(
                "setup accepted materials whose digests do not match the ones "
                "recorded when this task was built")
        assert controller.of("launch") == [], (
            "WPS was opened on a machine holding the wrong deck")


def test_a_dataset_that_will_not_serve_stops_the_episode():
    """An agent handed no materials looks exactly like an agent that did not do
    the work -- and it is a *stable* zero, so it reads as a capability floor
    rather than as the infrastructure failure it is."""
    mod = task_module()
    controller = BrokenFetchController()
    try:
        mod.TASK_CLASS().setup(controller, use_proxy=False)
    except mod.AssetFetchError as error:
        assert "no deck on it" in str(error)
    else:
        raise AssertionError("setup carried on after the fetch failed")
    assert controller.of("launch") == []


# =========================================================================== #
# 2. hard gates
# =========================================================================== #


def test_a_missing_deck_scores_zero_with_a_reason():
    mod = task_module()
    env = FakeEnv("", {}, keystroke="KEYSTROKE_NOT_SENT").install(mod)
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
                  keystroke="KEYSTROKE_NOT_SENT").install(mod)
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


def test_the_save_status_reports_the_disk_and_not_the_keystroke():
    """`xdotool` succeeds once a key has been *sent*, and the fallback sends
    `ctrl+s` through pyautogui to whatever window has focus — so the script's
    own verdict is equally consistent with the deck being saved, another
    window being saved, and nothing at all happening.  The digests either side
    of the attempt are the evidence, and both directions are checked because
    either alone would pass on a status that merely echoed the keystroke."""
    mod = task_module()
    sent = FakeEnv(mod.INIT_SHA256, {mod.DECK_VM_PATH: init_pptx()},
                   keystroke="KEYSTROKE_SENT").install(mod)
    evidence = mod.TASK_CLASS().evaluate(sent)["evidence"]
    assert evidence["keystroke"] == "KEYSTROKE_SENT"
    assert evidence["save_status"].startswith("NOT_SAVED"), evidence
    assert evidence["disk_sha_after"] == mod.INIT_SHA256

    written = FakeEnv(mod.INIT_SHA256, {mod.DECK_VM_PATH: init_pptx()},
                      keystroke="KEYSTROKE_NOT_SENT",
                      saved_sha="f" * 64).install(mod)
    evidence = mod.TASK_CLASS().evaluate(written)["evidence"]
    assert evidence["keystroke"] == "KEYSTROKE_NOT_SENT"
    assert evidence["save_status"].startswith("SAVED"), evidence


def test_a_file_this_task_supplied_is_not_scored_as_the_agent_s_answer():
    """The stray scan exists so a Save-As does not destroy a whole result.
    What it must not become is "score the newest .pptx near the home
    directory": a `.pptx` among the materials is newer than the pinned deck —
    setup wrote it afterwards — and scoring it would be *stable*, so it would
    read as a capability floor rather than as an evaluator that never looked
    at the agent's work."""
    mod = task_module()
    supplied = mod.MATERIALS_VM_DIR + "/supplied.pptx"
    digest = "d" * 64
    original = mod.MATERIAL_SHA256
    mod.MATERIAL_SHA256 = frozenset({digest})
    try:
        env = FakeEnv(mod.INIT_SHA256,
                      {mod.DECK_VM_PATH: init_pptx(), supplied: init_pptx()},
                      strays={supplied: digest}).install(mod)
        result = mod.TASK_CLASS().evaluate(env)
    finally:
        mod.MATERIAL_SHA256 = original
    assert env.fetched == [mod.DECK_VM_PATH], env.fetched
    assert result["score"] == 0.0
    assert any("materials folder" in note
               for note in result["evidence"]["stray_rejected"])


def test_two_candidate_files_are_not_free_retries():
    """Scoring the best stray beats doing the work once: leave three attempts
    on the Desktop and be graded on the luckiest.  With two equally eligible
    files there is no evidence for which is the result, so the instruction —
    save it in place — stands, and the reason is recorded."""
    mod = task_module()
    first = "/home/user/Desktop/attempt one.pptx"
    second = "/home/user/Desktop/attempt two.pptx"
    env = FakeEnv(mod.INIT_SHA256,
                  {mod.DECK_VM_PATH: init_pptx(), first: init_pptx(),
                   second: init_pptx()},
                  strays={first: "a" * 64, second: "b" * 64}).install(mod)
    result = mod.TASK_CLASS().evaluate(env)
    assert env.fetched == [mod.DECK_VM_PATH], env.fetched
    assert result["score"] == 0.0
    note = " ".join(result["evidence"]["stray_rejected"])
    assert "2 files" in note and first in note and second in note


def test_a_deck_moved_away_rather_than_copied_is_still_recovered():
    """The recovery used to run `find ... -newer '<the pinned deck>'`, and it
    is only ever called when that deck is *missing*.  `find -newer` needs its
    reference to exist: with it gone `find` errors out, prints nothing, the
    `2>/dev/null` swallows it, and the recovery returns empty.  So the
    commonest shape of the mistake it forgives -- save elsewhere, then remove
    or rename the original -- scored 0.0 with the machinery to rescue it
    sitting right there.  Save-As proper, which leaves the original in place,
    worked, which is why it read as tested.

    `setup` now leaves a marker that dates its own end, so losing the deck
    cannot take the reference with it."""
    mod = task_module()
    moved = "/home/user/Desktop/restored deck.pptx"
    env = FakeEnv("", {moved: init_pptx()}, keystroke="KEYSTROKE_NOT_SENT",
                  strays={moved: "f" * 64},
                  reference=mod.SETUP_STAMP).install(mod)
    result = mod.TASK_CLASS().evaluate(env)
    assert result["evidence"]["stray_reference"] == mod.SETUP_STAMP
    assert result["evidence"]["scored_file"] == moved, result["evidence"]
    assert mod.SETUP_STAMP in mod._SCAN_REFERENCES
    assert mod._SCAN_REFERENCES.index(mod.DECK_VM_PATH) == 2, (
        "the pinned deck may be a reference but never the only one")


def test_one_file_found_down_two_roots_is_one_candidate():
    """`find /home/user/Desktop /home/user -maxdepth 2` reports every Desktop
    deck twice -- Desktop is itself at depth 1 under /home/user.  Left
    undeduplicated that reads as "2 files could each be the result", so a lone
    Save-As on the Desktop was never scored."""
    mod = task_module()
    saved = "/home/user/Desktop/my version.pptx"
    env = FakeEnv("", {saved: init_pptx()}, keystroke="KEYSTROKE_NOT_SENT",
                  reference=mod.SETUP_STAMP,
                  stray_lines=["%s  %s" % ("f" * 64, saved)] * 2).install(mod)
    result = mod.TASK_CLASS().evaluate(env)
    assert result["evidence"]["scored_file"] == saved, result["evidence"]
    assert "stray_rejected" not in result["evidence"], result["evidence"]


def test_with_nothing_to_date_setup_by_no_file_is_scored():
    """Dropping the `-newer` clause instead would admit every .pptx that came
    with the image, and one of those is indistinguishable from a lone
    Save-As.  "There was no reference point" is the honest answer."""
    mod = task_module()
    sample = "/home/user/Desktop/sample.pptx"
    env = FakeEnv("", {sample: init_pptx()}, keystroke="KEYSTROKE_NOT_SENT",
                  strays={sample: "f" * 64}, reference="none").install(mod)
    result = mod.TASK_CLASS().evaluate(env)
    assert env.fetched == [mod.DECK_VM_PATH], env.fetched
    assert result["score"] == 0.0
    assert result["evidence"]["stray_reference"] == "none"
    assert any("date the end of setup" in note
               for note in result["evidence"]["stray_rejected"])


def test_setup_leaves_a_marker_that_dates_its_own_end():
    mod = task_module()
    controller = FakeController()
    mod.TASK_CLASS().setup(controller, use_proxy=False)
    kinds = [name for name, _ in controller.calls]
    stamped = [i for i, c in enumerate(controller.of("execute"))
               if mod.SETUP_STAMP in c]
    assert stamped, "setup never writes %s" % mod.SETUP_STAMP
    assert kinds.index("download") < len(kinds) - 1, (
        "the marker has to be written after the materials arrive, or the "
        "stray scan's window starts before the agent's work does")
    assert mod.SETUP_STAMP not in mod.TASK_CLASS().instruction


def test_the_package_says_which_deck_and_which_build_it_is():
    """Nine emitted tasks once sat in one flat directory and one of them was a
    stale build of a deck the pipeline had since rejected -- byte-for-byte the
    same kind of artefact as the eight good ones."""
    mod = task_module()
    assert mod.PROVENANCE["deck"] == DECK_ID
    assert mod.PROVENANCE["task_id"] == TASK_ID
    assert mod.PROVENANCE["emitted_at"] and mod.PROVENANCE["inputs"]
    assert "run" in mod.PROVENANCE
    beside = json.loads((TASK_DIR / "provenance.json").read_text())
    assert beside == mod.PROVENANCE, (
        "the copy that travels with the .py and the copy beside the assets "
        "disagree about which build this is")


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

    One addition to that contract, forced by the materials moving to a
    dataset: a test that calls `pytest.skip` is recorded as *skipped*, not
    failed.  The three fetch tests need a local copy of the deck, which is
    there in the staging directory the package was just built in and is not
    there in a checkout of the benchmark repository — and "the materials are
    in the dataset" is the correct state, not a defect.  Counting it as a
    failure would make the suite red everywhere it is most likely to be run
    by somebody else, which is how a suite gets ignored.
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
            except BaseException as error:                    # noqa: BLE001
                # `pytest.skip` raises out of `BaseException`, not `Exception`,
                # so a plain `except Exception` lets it escape the runner
                # entirely and takes the whole report with it.
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                if type(error).__name__ == "Skipped":
                    row["skipped"] = True
                    row["error"] = str(getattr(error, "msg", error))
                else:
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
    idle_row = states.get("ground truth, untouched pages' images re-encoded") or {}
    if perfect is not None and perfect < 1.0:
        out.append(
            f"**A perfect repair whose images the application re-encoded "
            f"scores {perfect:.4f}** — the agent is charged {1 - perfect:.4f} "
            f"for something it did not do, and is charged it precisely when it "
            f"does the *right* thing, which is to insert the original supplied "
            f"in `assets/materials/`. One charge, not two: `_facet_picture` "
            f"compares blob digests exactly and is *multiplied* by the "
            f"geometry, so the component goes to 0 however well the picture is "
            f"placed. The scope penalty on this state is "
            f"{row.get('penalty')} — the identity half is fixed, addresses in "
            f"`_page_facts` come from the pairing rather than from the blob — "
            f"and no hard gate fires, so the loss is graded rather than a "
            f"zeroing. It is a priced decision and not a defect: see "
            f"`_facet_picture`'s own note for why no tolerance is buildable "
            f"while these modules are stdlib-only and the corpus holds 29 EMF, "
            f"19 GIF, 15 TIFF and 5 WMF slide pictures.")
    if idle is not None and idle < 1.0:
        out.append(
            f"The same re-encoding confined to pages the task never named "
            f"still scores {idle:.4f}, which should not happen: that half was "
            f"fixed when `_page_facts` stopped addressing its records by the "
            f"blob digest, and a loss here means either the fix has regressed "
            f"or something on those pages is being compared that WPS rewrites "
            f"on save. Scope penalty {idle_row.get('penalty')}.")

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

    # `steps_measured` is the solvability probe's own count, which is a better
    # source than the proposer's declaration rather than a worse one — the two
    # disagreed by up to 8x, and weighting by the declaration gave deck0006's
    # cheapest job 12.4x the reward per step of its most expensive.  Reporting
    # it as "every component is then worth the same" was false for exactly the
    # decks whose weights are best founded.
    if plan.get("weight_source") not in ("est_steps", "steps_measured"):
        out.append(
            f"Weights come from `{plan.get('weight_source')}`, not from the "
            f"work each component represents. Every component is then worth "
            f"the same regardless of how much work it is, so the partial "
            f"score stops being a measure of progress and becomes a count.")

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

    if plan.get("init_slide_of") is not None:
        out.append(
            "The recipe moves pages, so every floor on this deck is measured "
            "through `init_slide_of` rather than page for page. "
            "`comparators._init_slide_of` builds it by replaying the deletions "
            "and then the swaps in the order `degrade_exec` applied them — "
            "both position-based and destructive, so any other reading picks "
            "the wrong page as soon as there are two. Worth knowing because a "
            "floor read against the wrong page is silent: the component still "
            "scores, it is just normalised against a page the agent never saw.")

    if any(not c.get("gt_path") for c in plan["components"]):
        n = sum(1 for c in plan["components"] if not c.get("gt_path"))
        out.append(
            f"{n} component(s) name no shape path: the object is found on the "
            f"*ground-truth* side by its data part (`_find_smartart` / "
            f"`_find_chart`) and matched into the candidate through the same "
            f"pairing every other component uses, so a second SmartArt the "
            f"agent adds does not make the component unidentifiable. What "
            f"would — an object the evaluator cannot pick out of the answer "
            f"itself — is refused when the plan is built rather than scored "
            f"zero at rollout time: `build_plan` runs every component against "
            f"the ground truth as its own candidate, drops any that cannot "
            f"reach 1.0 into `unscoreable`, and rejects the plan outright if "
            f"that empties a degradation.")

    out.append(
        "`_stray_candidate` recovers a deck saved under another name only "
        "when exactly one candidate is left after the digests of `init.pptx` "
        "and of every supplied material are set aside — a file this task put "
        "on the machine is not an answer, and two candidates are not free "
        "retries. So a Save-As is still recovered, and a solver who leaves "
        "several files is scored on the pinned path, which is what the "
        "instruction asked for. What it cannot see: a result saved deeper "
        "than two directories down, or outside `~`.")
    out.append(
        "The window that scan searches is \"newer than `SETUP_STAMP`\", the "
        "marker `setup` writes once its uploads are done, falling back to the "
        "materials folder and only then to the pinned deck. It used to be the "
        "pinned deck alone, which is dead in the case the scan exists for: "
        "`find -newer` needs its reference to exist and the scan runs "
        "precisely because the deck is gone, so an agent who saved elsewhere "
        "and removed the original got 0.0. If none of the three survives, "
        "nothing is scored rather than everything being eligible — every "
        "`.pptx` that shipped with the image would otherwise be a candidate.")

    out.append(
        "`save_status` is read off the disk — the digest before the forced "
        "save against the digest after it — and not off the keystroke, which "
        "`xdotool` reports as delivered whatever window received it. The "
        "keystroke is kept beside it as `keystroke` and is a hint only. What "
        "no evidence here can separate, because it needs the VM: a keystroke "
        "that missed the window from an application that had nothing left to "
        "write. Both leave the bytes unchanged, which is all the evaluator "
        "acts on.")

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
    # A skip is not a pass.  Counting one as a pass is how a suite reports
    # green while the assertions that matter never ran — the fetch tests skip
    # wherever the materials are only in the dataset, which is most places.
    skipped = [r for r in results if r.get("skipped")]
    passed = sum(1 for r in results if r["ok"] and not r.get("skipped"))
    # A known finding that passes is a passing test — it is never counted as a
    # failure — but it is also the one thing nobody notices, and a stale entry
    # is how the picture finding kept describing a defect for a release after
    # half of it was fixed.  So it is named, in the report, on the task where
    # it passed.
    stale = [r["test"] for r in results
             if r["ok"] and not r.get("skipped") and r["test"] in KNOWN_FINDINGS]
    fixed = [r["test"] for r in results
             if r["ok"] and not r.get("skipped") and r["test"] in FIXED_FINDINGS]

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
             f"- **result** {passed}/{len(results)} passing"
             + (f" · {len(skipped)} skipped ({skipped[0]['error'][:90]})"
                if skipped else ""), "",
             "## How it was run", "",
             "```", f"python3 -m pptxgym.tasks.emit_tests --out <out> "
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

    if stale or fixed:
        lines += ["", "## Findings that pass here", "",
                  "A named finding that passes is a **pass** — it is not "
                  "counted, listed or reported as a failure anywhere above. "
                  "It is repeated here because a stale entry is the thing "
                  "nobody notices: the picture finding went on describing two "
                  "defects for a release after one of them was fixed.", ""]
        for name in stale:
            lines += [f"- `{name}` passes on this task while still listed in "
                      f"`KNOWN_FINDINGS`. If it passes on every deck the "
                      f"entry is stale and should be moved to "
                      f"`FIXED_FINDINGS`, where a later failure reads as a "
                      f"regression rather than as a rediscovery."]
        for name in fixed:
            lines += [f"- `{name}` passes, and `FIXED_FINDINGS` records what "
                      f"it used to fail for. It is deliberately *not* in "
                      f"`KNOWN_FINDINGS`, so if it ever fails again it is "
                      f"reported as a defect, not excused as a finding.",
                      "", f"  {FIXED_FINDINGS[name]}"]
        lines.append("")

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
    # a named finding that passes is a pass; it is reported so the entry does
    # not go on describing a defect that has been fixed.
    out["stale_findings"] = [r["test"] for r in results
                             if r["ok"] and r["test"] in KNOWN_FINDINGS]
    out["fixed_findings"] = [r["test"] for r in results
                             if r["ok"] and r["test"] in FIXED_FINDINGS]
    out["unexpected"] = [r for r in results
                         if not r["ok"] and r["test"] not in KNOWN_FINDINGS]
    # Reported separately from passes: a suite whose fetch tests all skipped
    # has not checked the fetch, and "green" would be the wrong word for it.
    out["skipped"] = [r["test"] for r in results if r.get("skipped")]
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
