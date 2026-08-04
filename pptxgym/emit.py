"""Turn an approved deck into a task the benchmark harness can run.

Everything judged happens before this: the deck was inspected, a task was
proposed, the file was damaged to a recipe, assets were produced, the record
was reconciled, a blind probe confirmed the task is solvable, a plan was
derived from the delta and an adversarial battery failed to cheat it.  This
stage is a deterministic translation of that into one self-contained `.py`
plus the files it needs — no judgement, and no agent.

**Self-contained means stdlib-only at runtime.**  The evaluator is embedded
verbatim rather than imported, because the harness runs the task file in an
environment where `pptxgym` does not exist and `python-pptx` may not either.
`inventory.py` and `comparators.py` are written to be embeddable for exactly
this reason; the alternative — a task that imports a library living somewhere
else — is a task that can silently drift away from the evaluator it was
calibrated against.

**Embedded is not the same as concatenated.**  This used to paste both
modules into one namespace, which is a different program from the one the
pipeline scores with: they both define `_para_runs`, `_sha` and `main` at top
level, the later copy won, and inventory's calls to its own `_para_runs`
landed in comparators' — which takes a dict, so `Element.get("text")` returned
None instead of raising and every run in the deck fell out of the inventory
computed on the VM.  `gt_inventory.json` is baked here by the real module and
still had them, so an agent that restored a deck perfectly scored 0.82 on
`deck0010` and nothing anywhere raised.  Each module now gets a namespace of
its own and the only names that cross between them are the ones its own
`from .x import y` asked for — `_module_namespaces` is where that happens, and
it is the reason the class of accident cannot recur rather than the three
names it happened to hit.

The save contract is the part that took the longest to get right, and it is
documented at `_persist_if_unsaved` because it is the one piece of this file
that is not obvious.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent

# What the instruction adds on top of what the task itself asks for.  Each of
# these three sentences exists because its absence cost a real rollout:
#
#   - a second application opening the same deck produced a save collision
#     that multiplied one deck from 19 slides to 61;
#   - work left unsaved in a GUI is invisible to the evaluator, which reads
#     the file;
#   - a deck saved under a new name is a deck the evaluator cannot find.
INSTRUCTION_SUFFIX = """

Work in WPS Presentation only — do not open this file in another application.
When you are finished, save the file in place at {deck_path} (Ctrl+S).
Do not rename or move it."""

PPTX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".presentationml.presentation")

EVALUATOR_ID = "pptxgym.delta-derived.v1"

# The class attributes the harness reads off a task, fixed to the values every
# validated Linux/WPS task in the rollout repo declares (`task_1170001` ..
# `task_1170013`).  Two of them are not decoration:
#
#   `intermediate_eval_safe = False` — `evaluate` force-saves and then kills
#   WPS.  Run mid-episode by `lib_run_single._run_inline_checkpoint_eval`
#   (which `--checkpoint_eval_mode` turns on) that closes the application the
#   agent is working in, so a task that leaves the `BaseTask` default of True
#   is one checkpoint flag away from destroying its own rollout.
#
#   `snapshot = "wps"` — no runner *in the benchmark repo* reads it (the AMI
#   comes from `IMAGE_ID_MAP` keyed by screen size), but it is the only field
#   on the task that says which image it needs, every shipped WPS task sets
#   it, and the empty string BaseTask defaults to names no image at all.
#
# `related_apps` is `["wps"]` and not `["wps_office"]` because that is the
# token all 177 shipped tasks use; `volume_size = 60` because 30 GB is the
# provider default and the WPS image the reference tasks run on asks for 60.
HARNESS_ATTRS = {
    "snapshot": "wps",
    "related_apps": ["wps"],
    "platform": "linux",
    "proxy": False,
    "fixed_ip": False,
    "possibility_of_env_change": "low",
    "intermediate_eval_safe": False,
    "volume_size": 60,
}


class EmitError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# embedding
# --------------------------------------------------------------------------- #


# The scoring runtime, in dependency order: each module may only import from
# the ones before it.
EMBEDDED_MODULES = ("inventory", "comparators")

# The one thing a module's source is quoted with, so it can be carried into
# the generated file without being rewritten.
_DELIM = "'''"


def _embeddable(path: Path) -> tuple[str, list[tuple[str, list[tuple[str, str]]]]]:
    """A module's source, unchanged, and the sibling names it needs handed to it.

    Nothing here rewrites Python.  The text is carried across byte for byte
    apart from its intra-package imports, which cannot resolve outside the
    package: those lines are blanked — keeping the line *count*, so a
    traceback from the generated file still points at the right line of the
    real module — and returned, so the loader can rebind exactly the names
    they bound and nothing else.

    `from __future__ import annotations` now stays, because each module is
    compiled as its own unit rather than pasted into one; stripping it was
    only ever a symptom of the concatenation.
    """
    src = path.read_text()
    if _DELIM in src:
        raise EmitError(f"{path.name}: contains {_DELIM!r}, the delimiter its "
                        f"source is quoted with in the generated file")
    lines = src.splitlines(keepends=True)
    deps: list[tuple[str, list[tuple[str, str]]]] = []
    for node in ast.parse(src, filename=str(path)).body:
        if not (isinstance(node, ast.ImportFrom) and node.level):
            continue
        if node.module is None:
            raise EmitError(f"{path.name}: `from . import ...` binds a module, "
                            f"not values, and cannot be embedded")
        names = []
        for alias in node.names:
            if alias.name == "*":
                raise EmitError(f"{path.name}: `from .{node.module} import *` "
                                f"does not say what it binds, so the loader "
                                f"cannot rebind it")
            names.append((alias.name, alias.asname or alias.name))
        deps.append((node.module, names))
        for i in range(node.lineno - 1, (node.end_lineno or node.lineno)):
            lines[i] = "\n"
    out = "".join(lines)
    return (out if out.endswith("\n") else out + "\n"), deps


def _quoted(src: str) -> str:
    """`src` as a Python literal that evaluates back to `src`, exactly.

    Checked rather than assumed: a raw triple-quoted string is the only
    quoting under which a module's source survives with its line numbers
    intact, and the one thing that can break it — the delimiter appearing in
    the text — is the thing `_embeddable` refuses.  Proving the round trip
    here costs a millisecond and removes the whole question.
    """
    literal = "r" + _DELIM + src + _DELIM
    try:
        back = ast.literal_eval(literal)
    except SyntaxError as error:                               # pragma: no cover
        raise EmitError(f"embedded source does not quote cleanly: {error}")
    if back != src:                                            # pragma: no cover
        raise EmitError("embedded source does not survive quoting verbatim")
    return literal


_LOADER = '''

def _module_namespaces():
    """Run each embedded module in a namespace of its own.

    This is the whole point of the block above.  Both modules define
    `_para_runs`, `_sha` and `main` at top level, so pasting them into one
    namespace silently resolved inventory's calls to its own `_para_runs`
    against comparators' — which takes a dict, so `Element.get("text")`
    returned None instead of raising and every run in the deck disappeared
    from the inventory computed here, while the ground truth beside it (baked
    by the real module) still had them.  A perfect answer scored 0.82 and
    nothing raised.

    A namespace per module removes the mechanism, not the three names it hit:
    a module's globals are its own, and the only names that cross are the ones
    its own `from .x import y` asked for, recorded when this file was written
    and rebound here.
    """
    loaded = {}
    for name in _EMBEDDED_ORDER:
        module = _types.ModuleType("pptxgym_embedded." + name)
        module.__file__ = "<embedded pptxgym/%s.py>" % name
        for source, names in _EMBEDDED_IMPORTS.get(name, ()):
            for original, bound in names:
                setattr(module, bound, getattr(loaded[source], original))
        exec(compile(_EMBEDDED_SOURCE[name], module.__file__, "exec"),
             module.__dict__)
        loaded[name] = module
    return loaded


_EMBEDDED = _module_namespaces()

inventory = _EMBEDDED["inventory"]
comparators = _EMBEDDED["comparators"]

# The two entry points this file uses.  Everything else stays behind the
# module it belongs to and is reached as `inventory.x` / `comparators.y`: a
# flat re-export is the shape that hid the collision for as long as it did.
inventory_pptx = inventory.inventory_pptx
score = comparators.score
'''


_HEADER = """# --------------------------------------------------------------------------- #
# the scoring runtime
# --------------------------------------------------------------------------- #
#
# `pptxgym.inventory` and `pptxgym.comparators`, carried here verbatim and
# executed into one namespace each -- the way an import would.  Quoted rather
# than pasted so that "one namespace each" is enforced by the language instead
# of by everyone remembering not to reuse a helper name; the text inside is
# unmodified and its line numbers still match the real files, which is what
# tracebacks out of a rollout are read against.

import types as _types
"""


def runtime_source() -> str:
    """The scoring runtime, as stdlib-only Python with one namespace per module."""
    sources, imports = {}, {}
    for position, name in enumerate(EMBEDDED_MODULES):
        f = HERE / f"{name}.py"
        if not f.exists():
            raise EmitError(f"cannot embed missing module {name}.py")
        src, deps = _embeddable(f)
        for module, _ in deps:
            if module not in EMBEDDED_MODULES[:position]:
                raise EmitError(
                    f"{name}.py imports .{module}, which is not embedded "
                    f"before it — add it to EMBEDDED_MODULES in order")
        sources[name] = src
        if deps:
            imports[name] = deps

    parts = [_HEADER,
             f"_EMBEDDED_ORDER = {EMBEDDED_MODULES!r}",
             "",
             "# module -> [(module it imports from, [(name, bound as)])], read",
             "# off the real modules' own imports when this file was written.",
             f"_EMBEDDED_IMPORTS = {imports!r}",
             "",
             "_EMBEDDED_SOURCE = {}",
             ""]
    for name in EMBEDDED_MODULES:
        parts.append(f"# --- pptxgym/{name}.py, verbatim " + "-" * 30)
        parts.append(f"_EMBEDDED_SOURCE[{name!r}] = " + _quoted(sources[name]))
        parts.append("")
    return "\n".join(parts) + _LOADER


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# the generated task file
# --------------------------------------------------------------------------- #


TASK_TEMPLATE = '''"""{title}

Generated by pptxgym from {deck_id}; do not edit by hand — regenerate.

The scoring runtime below is embedded verbatim so this file is the whole
task.  It reads the plan and the two inventories from `tests/assets/`, which
`setup()` never uploads: the plan and the ground truth are the answer key,
and a task that ships its answer key to the machine the agent is working on
is not a task.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from desktop_env.task_base import BaseTask
from desktop_env.evaluators.getters import get_vm_file, get_vm_command_line

if TYPE_CHECKING:
    from desktop_env.controllers.setup import SetupController
    from desktop_env.desktop_env import DesktopEnv

TASK_DIR = Path(__file__).resolve().parent
ASSETS_DIR = TASK_DIR.parent / "task_assets" / "task_{task_id}"
AGENT_ASSETS = ASSETS_DIR / "assets"
TEST_ASSETS = ASSETS_DIR / "tests" / "assets"

DECK_NAME = {deck_name!r}
DECK_VM_PATH = {deck_vm_path!r}
MATERIALS_VM_DIR = {materials_vm_dir!r}
DESKTOP = "/home/user/Desktop"

# Recorded when this file was generated, so `evaluate` can tell "the agent has
# not saved" from "the agent saved something" without setup passing it state.
INIT_SHA256 = {init_sha!r}

EVALUATOR_ID = {evaluator_id!r}


{runtime}


# --------------------------------------------------------------------------- #
# save contract
# --------------------------------------------------------------------------- #


_SAVE_SCRIPT = (
    "set +e; "
    "if ! command -v xdotool >/dev/null 2>&1; then "
    "  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xdotool "
    "    >/dev/null 2>&1; "
    "fi; "
    "export DISPLAY=${{DISPLAY:-:0}}; "
    "OK=0; "
    "if command -v xdotool >/dev/null 2>&1 "
    "   && xdotool search --name %(win)s windowactivate --sync 2>/dev/null; then "
    "  sleep 0.5; xdotool key ctrl+s 2>/dev/null && OK=1; "
    "fi; "
    "if [ $OK -eq 0 ]; then "
    "  if ! command -v wmctrl >/dev/null 2>&1; then "
    "    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq wmctrl "
    "      >/dev/null 2>&1; "
    "  fi; "
    "  wmctrl -a %(win)s 2>/dev/null; sleep 0.5; "
    "  python3 -c \\"import pyautogui; pyautogui.hotkey('ctrl','s')\\" "
    "    2>/dev/null && OK=1; "
    "fi; "
    "sleep 4; "
    "[ $OK -eq 1 ] && echo SAVED || echo SAVE_FAILED"
)


def _vm_sha256(env, path: str) -> str:
    """Digest of a file as it stands on the VM, or "" if it is not there."""
    out = get_vm_command_line(env, {{
        "command": ["bash", "-c",
                    f"test -f '{{path}}' && sha256sum '{{path}}' | cut -d' ' -f1 "
                    f"|| echo MISSING"],
        "timeout": 30,
    }}) or ""
    line = out.strip().splitlines()[-1] if out.strip() else ""
    return "" if line in ("", "MISSING") else line


def _persist_if_unsaved(env) -> dict:
    """Force a save **only when nothing has been written to disk yet**.

    Two failures pull in opposite directions and a single hash comparison
    settles both.

    Saving unconditionally is what the reference tasks do, and it destroys
    work: if the agent edited the deck somewhere else and saved, the copy
    still held in the launched application is the *original*, and telling it
    to save writes the original back over the agent's result.  One rollout
    lost a measured 0.53 that way and recorded 0.0, which in training data is
    indistinguishable from an agent that did nothing.

    Never saving is what the other reference tasks do, and it loses work too:
    edits live in the application until something writes them out, and the
    evaluator reads the file.

    So: if the file on disk still matches what setup uploaded, nobody has
    saved and forcing one can only help — there is nothing to overwrite.  If
    it differs, somebody already wrote the agent's work out, and the only
    thing a save could do is undo it.
    """
    before = _vm_sha256(env, DECK_VM_PATH)
    evidence = {{"disk_changed_before_save": bool(before and before != INIT_SHA256)}}

    if before and before == INIT_SHA256:
        res = get_vm_command_line(env, {{
            "command": ["bash", "-c", _SAVE_SCRIPT % {{"win": "'" + DECK_NAME + "'"}}],
            "timeout": 180,
        }}) or ""
        evidence["save_attempted"] = True
        evidence["save_status"] = (res.strip().splitlines()[-1]
                                   if res.strip() else "EMPTY")
    else:
        evidence["save_attempted"] = False
        evidence["save_status"] = "not needed — the file on disk already moved"

    # Close the applications *without* asking them to save.  Anything they
    # still hold is either already on disk or was never wanted.
    get_vm_command_line(env, {{
        "command": ["bash", "-c",
                    "pkill -f wpp || true; pkill -f wps || true; "
                    "pkill -f soffice || true"],
        "timeout": 30,
    }})
    time.sleep(3)
    d, _, fname = DECK_VM_PATH.rpartition("/")
    get_vm_command_line(env, {{
        "command": ["bash", "-c", f"rm -f '{{d}}/.~lock.{{fname}}#' || true"],
        "timeout": 15,
    }})
    evidence["disk_sha_after"] = _vm_sha256(env, DECK_VM_PATH)
    return evidence


def _not_a_deck(path: str) -> str:
    """Why the bytes at `path` are not a PowerPoint package, or "".

    Asked before the inventory runs, so that the exceptions the inventory is
    then allowed to raise are only the ones that mean "malformed OOXML".  A
    `.ppt` saved from the Save-As dialog is not a zip at all and an `.odp` is
    a zip with none of the right parts; both are the agent's doing and both
    have a real answer, which is zero.  Anything the inventory raises past
    this point is this file's bug, and stays an exception.
    """
    if not zipfile.is_zipfile(path):
        return "it is not a zip container, so it is not a .pptx at all"
    try:
        with zipfile.ZipFile(path) as package:
            names = set(package.namelist())
    except (zipfile.BadZipFile, OSError) as error:
        return f"the package will not open ({{type(error).__name__}}: {{error}})"
    if "ppt/presentation.xml" not in names:
        return ("the package has no ppt/presentation.xml, so whatever it is, "
                "it is not a presentation")
    return ""


def _stray_candidate(env) -> str:
    """A deck the agent saved somewhere other than where it was asked to.

    Losing an agent's whole result to a Save-As is a scoring artefact, not a
    capability signal.  The work still has to be right; only the filename was
    wrong.
    """
    out = get_vm_command_line(env, {{
        "command": ["bash", "-c",
                    f"find {{DESKTOP}} /home/user -maxdepth 2 -name '*.pptx' "
                    f"-newer '{{DECK_VM_PATH}}' -not -path '{{DECK_VM_PATH}}' "
                    f"2>/dev/null | head -1"],
        "timeout": 30,
    }}) or ""
    return out.strip().splitlines()[-1].strip() if out.strip() else ""


class Task{task_id}(BaseTask):
    id = "{task_id}"
    snapshot = {snapshot!r}
    instruction = {instruction!r}
    source = {source!r}
    trajectory = "trajectories/"
    related_apps = {related_apps!r}
    platform = {platform!r}
    proxy = {proxy!r}
    fixed_ip = {fixed_ip!r}
    possibility_of_env_change = {possibility_of_env_change!r}
    # `evaluate` kills WPS, so it must never be run mid-episode.
    intermediate_eval_safe = {intermediate_eval_safe!r}
    volume_size = {volume_size!r}

    WEIGHTS = {weights!r}
    DESCRIPTIONS = {descriptions!r}

    # -- setup ------------------------------------------------------------- #
    def setup(self, setup_controller: "SetupController",
              use_proxy: bool = False) -> None:
        setup_controller.execute(command=f"rm -f {{DECK_VM_PATH}}", shell=True)
        uploads = [{{"local_path": str(AGENT_ASSETS / "init.pptx"),
                    "path": DECK_VM_PATH}}]
        materials = AGENT_ASSETS / "materials"
        if materials.is_dir():
            setup_controller.execute(
                command=f"mkdir -p {{MATERIALS_VM_DIR}}", shell=True)
            for f in sorted(materials.iterdir()):
                if f.is_file():
                    uploads.append({{"local_path": str(f),
                                    "path": f"{{MATERIALS_VM_DIR}}/{{f.name}}"}})
        setup_controller._upload_file_setup(uploads)

        # Point the desktop's own file association at WPS.  The instruction
        # asks the agent to stay in one application, but an `xdg-open` from
        # habit routes to whatever the image happens to associate, and a
        # second application holding the same deck is how one rollout turned
        # 19 slides into 61.  This makes the agent's own action harmless.
        setup_controller.execute(
            command=f"xdg-mime default wps-office-wpp.desktop {pptx_mime} "
                    f"2>/dev/null || true",
            shell=True)

        # Launch WPS directly rather than through `_open_setup`, which goes
        # via xdg-open and cannot be relied on to pick the right application.
        setup_controller.launch(["wpp", DECK_VM_PATH])

    # The one way an exception here is the agent's doing rather than this
    # file's: a package that opens but whose XML is malformed.  What is not
    # even a package is settled by `_not_a_deck` before this is reached.
    #
    # Everything else `evaluate` can raise — a getter that cannot reach the
    # machine, an answer key that is not beside the task, a comparator that
    # trips over its own input — is a bug in this file or its environment, and
    # a bug returned as 0.0 is filed under the same label as an agent that did
    # nothing.  There is no way to tell the two apart afterwards, so those are
    # deliberately not caught: they leave a traceback, which a human and a
    # test can both see, instead of a plausible zero nobody looks at again.
    UNREADABLE_DECK = (zipfile.BadZipFile, zipfile.LargeZipFile,
                       ElementTree.ParseError, UnicodeDecodeError, OSError)

    # -- evaluate ---------------------------------------------------------- #
    def evaluate(self, env: "DesktopEnv") -> dict[str, Any]:
        evidence = _persist_if_unsaved(env)

        result_path = get_vm_file(env, {{
            "path": DECK_VM_PATH,
            "dest": f"task_{{self.id}}_result.pptx",
        }})
        used = DECK_VM_PATH

        unchanged = (evidence.get("disk_sha_after") or "") == INIT_SHA256
        if unchanged or not result_path or not os.path.exists(result_path):
            stray = _stray_candidate(env)
            if stray:
                alt = get_vm_file(env, {{
                    "path": stray,
                    "dest": f"task_{{self.id}}_stray.pptx",
                }})
                if alt and os.path.exists(alt):
                    result_path, used, unchanged = alt, stray, False
        evidence["scored_file"] = used

        if not result_path or not os.path.exists(result_path):
            return self._fail_all("the deck was not on the VM at the "
                                  "path the task pinned", evidence)
        if unchanged:
            # Byte-identical to what setup uploaded: nothing was written
            # out.  The protection against losing real work to this lives
            # earlier — the save is forced when nothing has been written,
            # never when something has, and a deck saved elsewhere is
            # recovered by the scan above.  By the time we are here the
            # machinery has done what it can, and the score is 0.
            return self._fail_all(
                "the deck on disk is byte-identical to the one supplied, "
                "so nothing was ever written out", evidence)

        why = _not_a_deck(result_path)
        if why:
            return self._fail_all(
                f"the file at {{used}} is not a readable .pptx — {{why}}",
                evidence)
        try:
            cand = inventory_pptx(result_path)
        except self.UNREADABLE_DECK as error:
            return self._fail_all(
                f"the file at {{used}} is not a readable .pptx "
                f"({{type(error).__name__}}: {{error}})", evidence)

        plan = json.loads((TEST_ASSETS / "plan.json").read_text())
        gt = json.loads((TEST_ASSETS / "gt_inventory.json").read_text())
        init = json.loads((TEST_ASSETS / "init_inventory.json").read_text())
        evidence["slide_count"] = cand["package"]["slide_count"]
        evidence["slide_count_expected"] = gt["package"]["slide_count"]

        return self._render(score(plan, cand, gt, init), evidence)

    # -- shaping ----------------------------------------------------------- #
    def _render(self, out: dict, evidence: dict) -> dict[str, Any]:
        partial = {{}}
        for c in out["components"]:
            partial[c["id"]] = {{
                "score": round(float(c["score"]), 6),
                "weight": round(float(c["weight"]), 6),
                "description": self.DESCRIPTIONS.get(c["id"], c.get("why", "")),
            }}
        total = round(float(out["score"]), 6)
        # `score` is the only verdict this returns, so it has one job: be a
        # number in 0-1.  A scorer that hands back anything else is broken in
        # a way no reader downstream can detect, and the loudest place to say
        # so is here.
        if not 0.0 <= total <= 1.0:
            raise AssertionError(
                f"the scoring runtime returned {{total!r}}, which is not a "
                f"score in 0-1")
        return {{
            "evaluator": EVALUATOR_ID,
            "score": total,
            "partial_scores": partial,
            "hard_gates": out["hard_gates"],
            "failed_gate": out.get("failed_gate"),
            "gate_reasons": out.get("gate_reasons"),
            "scope_penalty": out.get("penalty"),
            "evidence": evidence,
        }}

    def _fail_all(self, reason: str, evidence: dict) -> dict[str, Any]:
        """Zero, with the breakdown and the reason kept beside it.

        `score` is always a number in 0–1; nothing here competes with it.
        The extra keys are diagnostics — they make a zero explicable after
        the fact, which is all they can do.  What actually prevents a zero
        the agent did not earn happens earlier: the save is forced only when
        nothing has been written to disk, the file association points at WPS
        so a stray `xdg-open` cannot bring a second application in, and a
        deck saved under another name is found rather than lost.
        """
        return {{
            "evaluator": EVALUATOR_ID,
            "score": 0.0,
            "failure_reason": reason,
            "partial_scores": {{
                pid: {{"score": 0.0, "weight": float(w),
                      "description": f"{{self.DESCRIPTIONS.get(pid, '')}} "
                                     f"[failed: {{reason}}]"}}
                for pid, w in self.WEIGHTS.items()
            }},
            "evidence": evidence,
        }}


TASK_CLASS = Task{task_id}
'''


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


def _describe(comp: dict, plan: dict) -> str:
    deg = next((d for d in plan.get("degradations", [])
                if d.get("id") == comp.get("deg")), {})
    what = (deg.get("what_the_file_looks_like") or deg.get("what_breaks")
            or "").strip()
    head = f"{comp['op']} on slide {comp.get('slide', '?')}"
    return f"{head} — {what[:160]}" if what else head


def emit(deck, out_root: Path, task_id: str) -> dict:
    """Write one runnable task. Deterministic; nothing here is judged."""
    plan = json.loads((deck.root / "plan.json").read_text())
    if plan.get("rejected"):
        raise EmitError(f"{deck.id}: plan was rejected — {plan['rejected'][0]}")
    task = json.loads((deck.root / "task.json").read_text())
    bundle = deck.root / "bundle"
    if not (bundle / "input.pptx").exists():
        raise EmitError(f"{deck.id}: no bundle to package")

    out_root = Path(out_root)
    adir = out_root / "task_assets" / f"task_{task_id}"
    (adir / "assets" / "materials").mkdir(parents=True, exist_ok=True)
    (adir / "tests" / "assets").mkdir(parents=True, exist_ok=True)

    # what the agent gets
    shutil.copy2(bundle / "input.pptx", adir / "assets" / "init.pptx")
    src_assets = bundle / "assets"
    if src_assets.is_dir():
        for f in sorted(src_assets.rglob("*")):
            if f.is_file():
                shutil.copy2(f, adir / "assets" / "materials" / f.name)

    # what only the evaluator gets
    from . import inventory
    gt = inventory.inventory_pptx(str(deck.source))
    init = inventory.inventory_pptx(str(deck.input_pptx))
    (adir / "tests" / "assets" / "gt_inventory.json").write_text(
        json.dumps(gt, ensure_ascii=False, separators=(",", ":")))
    (adir / "tests" / "assets" / "init_inventory.json").write_text(
        json.dumps(init, ensure_ascii=False, separators=(",", ":")))
    (adir / "tests" / "assets" / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=1))

    deck_name = f"task_{task_id}.pptx"
    deck_vm = f"/home/user/Desktop/{deck_name}"
    weights = {c["id"]: round(float(c["weight"]), 6) for c in plan["components"]}
    descs = {c["id"]: _describe(c, plan) for c in plan["components"]}

    instruction = (task["instruction"].rstrip()
                   + INSTRUCTION_SUFFIX.format(deck_path=deck_vm))
    body = TASK_TEMPLATE.format(
        title=task.get("name", deck.id),
        deck_id=deck.id,
        task_id=task_id,
        deck_name=deck_name,
        deck_vm_path=deck_vm,
        materials_vm_dir=f"/home/user/Desktop/task_{task_id}_materials",
        init_sha=_sha256(adir / "assets" / "init.pptx"),
        runtime=runtime_source(),
        instruction=instruction,
        source=f"pptxgym/{deck.id}",
        pptx_mime=PPTX_MIME,
        evaluator_id=EVALUATOR_ID,
        weights=weights,
        descriptions=descs,
        **HARNESS_ATTRS,
    )
    (out_root / "task_class").mkdir(parents=True, exist_ok=True)
    py = out_root / "task_class" / f"task_{task_id}.py"
    py.write_text(body)

    # Nothing in the harness reads this file — it is what a human reads to
    # decide which image to boot and where the deck lands, so `instruction` is
    # the instruction the agent actually receives (suffix included) rather
    # than the one the proposer wrote.  The two drifting apart is how a
    # reviewer ends up approving constraints the task does not carry.
    (adir / "metadata.json").write_text(json.dumps({
        "id": task_id, "instruction": instruction,
        "domain": "office", "platform": "linux",
        "environment": "WPS Presentation",
        "requires_image": "OSWorld Linux WPS snapshot",
        "input_file": deck_vm,
        "evaluator": EVALUATOR_ID,
        "uses_user_simulator": False,
        "tags": ["pptx", "wps", "restoration"],
        "related_apps": list(HARNESS_ATTRS["related_apps"]),
        "task_path": f"task_class/task_{task_id}.py",
        "difficulty": task.get("difficulty"),
        "est_steps": task.get("est_steps"),
        "source_deck": deck.id,
        "components": len(plan["components"]),
    }, ensure_ascii=False, indent=1))
    (adir / "README.md").write_text(_readme(task_id, task, plan, descs))

    problems = check_package(py, adir)
    if problems:
        raise EmitError(f"{deck.id}: {problems[0]}")
    return {"task_id": task_id, "py": str(py), "assets": str(adir),
            "components": len(plan["components"])}


def _readme(task_id: str, task: dict, plan: dict, descs: dict) -> str:
    rows = "\n".join(
        f"| `{c['id']}` | {descs[c['id']][:110]} | {float(c['weight']):.4f} |"
        for c in plan["components"])
    return f"""# task_{task_id} — {task.get('name', '')}

{task['instruction']}

**Difficulty** {task.get('difficulty')} · **est_steps** {task.get('est_steps')}
· **components** {len(plan['components'])} · derived from `{plan.get('deck')}`

## What the agent gets

`assets/init.pptx` opened in WPS Presentation, and everything under
`assets/materials/` in a folder on the Desktop. Nothing else.

## Scoring

Derived from the delta record, not written by hand: every component below
corresponds to one recorded change, weighted by the GUI work it represents.
Hard gates are checked first and a failure zeroes the score while keeping the
breakdown and the reason. `score` is always a number in 0–1; the other keys
beside it are diagnostics, and what actually prevents an undeserved zero
happens in `setup`/`evaluate` rather than in a label.

| partial id | what it checks | weight |
| --- | --- | ---: |
{rows}
"""


def check_package(py: Path, adir: Path) -> list[str]:
    """Refuse to ship a task that hands the agent its own answer key.

    The rule everyone states is "the evaluator must not read test fixtures",
    but a delta-derived evaluator has to: the plan and the ground-truth
    inventory *are* how it scores.  The real invariant is the other way round
    — nothing the evaluator reads may be uploaded to the machine the agent
    works on — so this checks the upload list, not the read list.
    """
    out = []
    src = py.read_text()
    uploaded = set(re.findall(r'AGENT_ASSETS / "([^"]+)"', src))
    uploaded |= {"materials"}
    secret = {"plan.json", "gt_inventory.json", "init_inventory.json"}
    for name in secret:
        if re.search(rf'AGENT_ASSETS\s*/\s*"{re.escape(name)}"', src):
            out.append(f"{name} is read from the agent-visible assets folder")
    agent_dir = adir / "assets"
    for f in agent_dir.rglob("*"):
        if f.is_file() and f.name in secret:
            out.append(f"{f.name} is sitting in assets/, which setup uploads")
    if "TEST_ASSETS" not in src:
        out.append("the evaluator does not read from tests/assets/")
    if not (adir / "tests" / "assets" / "plan.json").exists():
        out.append("no plan.json beside the task")
    return out


def main():
    import argparse
    from . import pipeline as pl

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("deck")
    ap.add_argument("--work", default="work")
    ap.add_argument("--out", required=True)
    ap.add_argument("--task-id", required=True)
    args = ap.parse_args()
    r = emit(pl.Deck(Path(args.work) / args.deck), Path(args.out), args.task_id)
    print(json.dumps(r, indent=1))


if __name__ == "__main__":
    main()
