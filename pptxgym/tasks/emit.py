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

**Every package says where it came from.**  `work/emitted/` is one flat
directory, and after a ten-deck run it held nine tasks that were byte-for-byte
the same *kind* of thing: eight freshly packaged, and one built hours earlier
from a deck the pipeline had since rejected — promising two assets that deck's
own proposal now forbids by name, one of them the deleted picture's bytes.
Nothing on the file said so, and it was nearly pushed.  So `provenance()`
records the deck, the deck's state at the moment of emission, when, from which
commit and from which run, `provenance.json` carries it beside the assets and
`PROVENANCE` carries it inside the `.py` that travels; `provenance_problems`
answers "has the deck moved since?" with the same content-hash comparison
`Deck.stale` already uses for stages, rather than a second mechanism that can
disagree with the first.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE.parent
REPO_ROOT = PACKAGE_ROOT.parent

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

# Where the supplied files actually are, said once and in full.
#
# The per-deck prose is written by the proposal stage, so it names the folder
# however that deck's sentence happened to come out: "the assets folder"
# (task_1100011), "the folder beside the deck" (task_1100012).  The second is
# true by accident; the first names a directory that does not exist on the
# machine, and an agent that goes looking for it by name finds nothing and
# spends steps discovering that the only folder on the Desktop is the one it
# wanted.  On a rollout whose whole job is to tell a task defect from an agent
# failure, that is noise we chose to add.
#
# It cannot be fixed deck by deck — the prose varies per deck and is not ours
# to write — so it is fixed here, where the placement and the words are both
# known: whatever the sentence above calls it, the solver is told the real
# path once, explicitly.
#
# **Only when there is something there.**  A task with no materials would
# otherwise carry a sentence promising a folder `setup` never creates, which
# is worse than the wrong name: the agent has no way to conclude it does not
# exist except by looking.
INSTRUCTION_MATERIALS = """

The files supplied with this task are in {materials_path} — that folder, under
that name, whatever the wording above calls it."""

PPTX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".presentationml.presentation")

EVALUATOR_ID = "pptxgym.delta-derived.v1"

# --------------------------------------------------------------------------- #
# where the agent's materials come from
# --------------------------------------------------------------------------- #
#
# The deck and the reference material used to be committed to the benchmark
# repository beside the `.py`, and `setup` uploaded them off local disk.  At
# three tasks that is 11-13 MB each and merely untidy; at the hundreds this
# pipeline is built to produce it is a gigabyte of binary in a git repository
# that every contributor to the benchmark has to clone.  So they move to a
# dataset, and `setup` fetches them.
#
# **Not through `desktop_env.file_source.asset()`.**  That helper resolves
# against one global base — `OSWORLD_FILE_BASE_URL`, defaulting to
# `xlangai/osworld_v2_assets` — shared by every task in the benchmark.  Ours
# are not in that dataset, and pointing the global at ours would redirect all
# ~950 other tasks to a repository that does not hold their files.  A task
# that needs a different dataset has to name it, and 216 task files in the
# rollout repository already do exactly that with a literal URL.
#
# `PPTXGYM_ASSET_BASE` overrides it for offline or mirrored runs.  It is our
# own variable rather than the global one for the same reason: setting it can
# only affect the tasks this emitter wrote.
# Packaging happens before publication and publication re-emits the package
# with its configured destination. Keep the pre-publication package visibly
# unconfigured rather than silently baking in one operator's repository.
UNCONFIGURED_ASSET_REPO = "unconfigured"
ASSET_BASE_ENV = "PPTXGYM_ASSET_BASE"


def hf_asset_dir(task_id: str) -> str:
    """The dataset folder one task's materials live in.

    `task_<id>/<file>`, which is the layout the other tasks in the rollout
    repository that fetch from this dataset already use.  A second convention
    in one dataset is a second thing to remember.
    """
    return f"task_{task_id}"

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
# provenance
# --------------------------------------------------------------------------- #
#
# What an emitted package has to be able to answer about itself, and why each
# one is on the list rather than a nice-to-have:
#
#   * **which deck** — the flat ship directory names tasks by a checksum of
#     the source, which is stable across re-runs and therefore says nothing
#     about *which build* of that deck this is.
#   * **the deck's state at the moment of emission** — the one that was
#     nearly pushed came from a deck whose `reconciled` and `scored` now read
#     `rejected`.  A package built while those said `ok` is not the same
#     object as a package built after, and only the record can say which.
#   * **when, from which commit, from which run** — the failing task predates
#     the commit that added the materials paragraph, and that is *why* it was
#     missing it.  Without the commit that is a mystery; with it, it is a
#     one-line explanation.
#   * **the digests of everything the emitter read** — so "the deck has moved
#     on" is a comparison and not an opinion.
#
# Staleness deliberately reuses `Deck.fingerprint`: the same digests, of the
# same files, in the same hash that `state.json` records when a stage is
# marked.  A second staleness mechanism is a second thing that can be right
# when the first is wrong.

PROVENANCE_FILE = "provenance.json"

#: Bumped when the shape below changes in a way a reader has to know about.
PROVENANCE_SCHEMA = 1

#: What `emit` reads out of the deck on top of what the `packaged` stage
#: already fingerprints (`plan.json`, `attacks.json`, `task.json`,
#: `bundle.json`).  `bundle.json` covers the bundle's *contents* by digesting
#: them, but `bundle/input.pptx` is the file that becomes the agent's deck and
#: is worth naming in its own right; `source.pptx` is the answer key the two
#: baked inventories come from.
EMIT_INPUTS = ("source.pptx", "input.pptx", "bundle/input.pptx")


def _code_version() -> dict:
    """The commit this was emitted from, and whether the emitter was dirty.

    Deliberately asked here rather than borrowed: a run made from an
    uncommitted tree is not reproducible and the record has to say so, and
    that claim must not depend on another module's helper being present.  If
    the pipeline grows one canonical answer to "which code was this", this is
    three lines to delete.

    `None` outside a git tree — unknown is said, not guessed.
    """
    root = REPO_ROOT
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse",
                               "--short=12", "HEAD"],
                              capture_output=True, text=True, timeout=30)
        dirt = subprocess.run(["git", "-C", str(root), "status", "--porcelain",
                               "--", "pptxgym"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}
    if head.returncode:
        return {"commit": None, "dirty": None}
    return {"commit": head.stdout.strip() or None,
            "dirty": bool(dirt.stdout.strip()) if not dirt.returncode else None}


def provenance(deck, task_id: str, *, run_id: str | None = None) -> dict:
    """Where a package came from, in one dict a human and a script both read.

    `run` is the slot rather than the value: the pipeline is growing a
    run-level event log with an id of its own, and this is shaped so that id
    drops straight in — `emit(..., run_id=...)` when the caller knows it,
    `null` and honest when it does not.  A field that is absent means "this
    build predates the idea"; a field that is null means "nobody told us",
    and those are different facts.
    """
    from ..core import pipeline as pl

    state = deck.state()
    inputs = dict(deck.fingerprint("packaged"))
    for rel in EMIT_INPUTS:
        p = deck.root / rel
        inputs[rel] = pl._digest(p) if p.exists() else None
    return {
        "schema": PROVENANCE_SCHEMA,
        "task_id": task_id,
        # What the deck *is*, as opposed to what this build of it is called.
        #
        # `task_id` is a publication name now — `publish` allocates a 110xxxx
        # out of a registry and hands it in — so the two are no longer the same
        # string, and the registry is keyed on this one.  Without it the only
        # way back from a shipped task to the deck it came from is the deck
        # *number*, which is a local sequence position and moves when a corpus
        # is re-ingested.  `pipeline.task_id_for` is asked rather than
        # recomputed so there is one definition of content identity.
        "source_key": pl.task_id_for(deck),
        "deck": deck.id,
        "emitted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "run": run_id,
        "code": _code_version(),
        "evaluator": EVALUATOR_ID,
        # Coverage the run could not obtain, carried out of `work/` with the
        # task rather than left behind in it. `harden` distinguishes a defect
        # (which parks the deck) from a caveat (which does not) — the only
        # caveat today being `gt_roundtrip` when WPS was off — and a caveat
        # that stayed in `state.json` would be invisible to anyone reading a
        # shipped task and asking what was actually checked.
        "caveats": list((state.get("hardened") or {}).get("caveats") or []),
        # The furthest stage that was complete, and every stage's verdict.
        # Both, because "packaged from a deck that got to `hardened`" and
        # "packaged from a deck whose `reconciled` said no" are the same
        # sentence until you can see the second one.
        "deck_stage": deck.stage_now(),
        "deck_state": {s: state.get(s, {}).get("status")
                       for s in pl.STAGES if s in state},
        "inputs": inputs,
    }


def read_provenance(adir: Path) -> dict | None:
    """The record beside a packaged task's assets, or None if it has none.

    None is a real answer and means "emitted before this existed", which is
    exactly the state the nine artefacts were in.
    """
    f = Path(adir) / PROVENANCE_FILE
    if not f.exists():
        return None
    try:
        rec = json.loads(f.read_text())
    except json.JSONDecodeError:
        return None
    return rec if isinstance(rec, dict) else None


def provenance_problems(record: dict | None, deck) -> list[str]:
    """Why this package no longer describes the deck it claims, or [].

    Three questions, in the order a reader wants them:

      * is it from this deck at all;
      * has the deck's own verdict on itself changed since — a plan that is
        now rejected, a stage that no longer reads `ok`, or an upstream gate
        that has since said no (which `Deck.stale` reports and the
        fingerprints alone cannot see, because a refusal usually changes
        nothing on disk);
      * have the bytes the emitter read moved.

    Nothing here opens the shipped package: the package is a copy of what the
    deck held, so the deck is the thing to ask.  A package with no record is
    *reported*, not passed — "we cannot tell" is the state that let a stale
    artefact sit in the ship directory looking like the others — and the two
    questions the deck can still answer without one are asked anyway, because
    the artefact this was written for is exactly that case: no record, and a
    deck whose plan is now rejected.
    """
    out = []
    if record is None:
        out.append("no provenance.json — this package predates the record, so "
                   "which deck and which build it came from cannot be told "
                   "from the files")
    else:
        claimed = record.get("deck")
        if claimed != deck.id:
            return [f"this package records deck {claimed!r}, not {deck.id!r}"]

    plan = deck.root / "plan.json"
    if plan.exists():
        try:
            rejected = json.loads(plan.read_text()).get("rejected") or []
        except json.JSONDecodeError:
            rejected = []
        if rejected:
            out.append(f"{deck.id}'s plan is now rejected — {rejected[0]}")

    # A verdict that turned against the deck still blocks; a fingerprint that
    # moved does not. `Deck.stale` reports both in one list — `<stage:status>`
    # markers are refusals it can see and the digests cannot, everything else
    # is drift — and reading the two as one refused seven of nine finished
    # tasks: collect re-executes `harden` on every deck it ships, rewriting
    # the `attacks.json` that `packaged` fingerprints, so the decks verified
    # hardest were the ones declared stale.
    status = (deck.state().get("packaged") or {}).get("status")
    if status != "ok":
        out.append(f"{deck.id}'s packaged stage now reads "
                   f"{status!r}, not 'ok'")
    for reason in deck.stale("packaged"):
        if reason.startswith("<") and ":" in reason:
            out.append(f"{deck.id} stands on a refused stage: {reason}")

    if record is None:
        # The digest comparison is the one question that needs the record.
        # The two above do not, which is the point: the artefact this exists
        # for had no record *and* a deck whose plan had been rejected.
        return out

    was = record.get("inputs") or {}
    now = dict(deck.fingerprint("packaged"))
    from ..core import pipeline as pl
    for rel in EMIT_INPUTS:
        p = deck.root / rel
        now[rel] = pl._digest(p) if p.exists() else None
    # Two keys move for reasons that are not drift, and both would otherwise
    # block every honest package: `attacks.json` is rewritten (timestamps and
    # all) every time collect re-executes `harden` to verify the deck, and
    # `<code>` moves whenever the instruments are fixed — provenance, not
    # freshness, as `Deck.stale` already reads it. The verdicts they carry are
    # checked live by the foreman and by the smoke test, on the bytes.
    ignore = {"attacks.json", pl.CODE_KEY}
    for key in sorted((set(was) | set(now)) - ignore):
        if was.get(key) != now.get(key):
            out.append(f"{key} has changed since this was emitted "
                       f"({was.get(key)} -> {now.get(key)})")
    return out


def emitted_packages(out_root: Path) -> list[Path]:
    """Every packaged task's asset directory under a ship root."""
    root = Path(out_root) / "task_assets"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and p.name.startswith("task_"))


def check_emitted(out_root: Path, work: Path) -> list[dict]:
    """Every package under `out_root`, and why each one is or is not current.

    This is the script half of "a human and a script can both read it".  It
    is the check that would have caught the artefact this whole record exists
    for, and it is cheap enough to run before every push.
    """
    from ..core import pipeline as pl

    out = []
    for adir in emitted_packages(out_root):
        rec = read_provenance(adir)
        deck_id = (rec or {}).get("deck")
        if deck_id is None:
            meta = adir / "metadata.json"
            if meta.exists():
                try:
                    deck_id = json.loads(meta.read_text()).get("source_deck")
                except json.JSONDecodeError:
                    deck_id = None
        row = {"task": adir.name, "deck": deck_id,
               "emitted_at": (rec or {}).get("emitted_at"),
               "run": (rec or {}).get("run"),
               "commit": ((rec or {}).get("code") or {}).get("commit")}
        if not deck_id:
            row["problems"] = ["no provenance.json and no source deck in "
                               "metadata.json — nothing says where this came "
                               "from"]
        else:
            deck_root = Path(work) / deck_id
            if not deck_root.is_dir():
                row["problems"] = [f"{deck_id} is not in {work} any more"]
            else:
                row["problems"] = provenance_problems(rec, pl.Deck(deck_root))
        row["current"] = not row["problems"]
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# embedding
# --------------------------------------------------------------------------- #


# The scoring runtime, in dependency order: each module may only import from
# the ones before it.
EMBEDDED_MODULES = ("inventory", "comparators")
EMBEDDED_ROOT = PACKAGE_ROOT / "evaluation"

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
# `pptxgym.evaluation.inventory` and `pptxgym.evaluation.comparators`, carried
# here verbatim and
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
        f = EMBEDDED_ROOT / f"{name}.py"
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

{provenance_head}

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
import uuid
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
# Only what the *evaluator* reads is committed beside the task.  The deck and
# the reference material the agent is handed are fetched -- see `FETCH`.
ASSETS_DIR = TASK_DIR.parent / "task_assets" / "task_{task_id}"
TEST_ASSETS = ASSETS_DIR / "tests" / "assets"

# Where the agent's materials live, and how to say so.
#
# A literal URL rather than `desktop_env.file_source.asset()`: that helper
# resolves against one global base shared by every task in the benchmark, and
# ours are not in that dataset.  Redirecting the global to reach three tasks
# would send the other nine hundred somewhere their files are not.
#
# `PPTXGYM_ASSET_BASE` overrides the base for an offline or mirrored run.  It
# is deliberately not the global variable: setting it can only affect tasks
# this emitter wrote.
HF_ASSET_REPO = {hf_asset_repo!r}
HF_ASSET_BASE_URL = {hf_asset_base!r}
ASSET_BASE_ENV = {asset_base_env!r}

# (path in the dataset, path on the VM, sha256 of the bytes) for every file
# `setup` puts on the machine.  The digest is here so that "fetched" and
# "fetched the right thing" are different answers: a task handed no materials,
# or the wrong ones, looks from the outside exactly like an agent that did not
# do the work, and that is the one failure this file must never be silent
# about.
FETCH = {fetch}

DECK_NAME = {deck_name!r}
DECK_VM_PATH = {deck_vm_path!r}
MATERIALS_VM_DIR = {materials_vm_dir!r}
DESKTOP = "/home/user/Desktop"

# Written by `setup` once everything is uploaded, and read by nothing but the
# stray scan.  It exists because the one fact that scan needs -- when this
# task's setup finished -- was previously taken from the pinned deck, and the
# scan only ever runs when the pinned deck is gone.  A dotfile in the home
# directory: the agent is never told about it and it says nothing about the
# task.  See `_strays`.
SETUP_STAMP = {setup_stamp!r}

# Recorded when this file was generated, so `evaluate` can tell "the agent has
# not saved" from "the agent saved something" without setup passing it state.
INIT_SHA256 = {init_sha!r}

# The digests of everything `setup` uploads besides the deck.  A file the task
# handed the agent is not the agent's answer, however new it looks, and the
# stray scan is the one place that distinction is not obvious: a `.pptx` among
# the materials sits in the same folder tree, is newer than the pinned deck
# because setup wrote it after, and would otherwise be scored as the result.
MATERIAL_SHA256 = {material_shas}

EVALUATOR_ID = {evaluator_id!r}

# Where this file came from, carried by the file itself.
#
# Nine emitted tasks once sat in one flat directory: eight freshly packaged
# and one built hours earlier from a deck the pipeline had since rejected,
# promising two assets that deck's own proposal now forbids by name.  It was
# byte-for-byte the same *kind* of artefact as the good ones and was nearly
# pushed.  `provenance.json` beside the assets holds the same record, but this
# copy is the one that travels with the `.py`, which is the thing somebody
# copies into a benchmark repo.
#
# `inputs` are content digests of everything the emitter read, in the hash
# `state.json` records for a stage, so "the deck has moved on since" is
# answered by `pptxgym.tasks.emit --check` as a comparison rather than an opinion.
# `run` is null when nobody told the emitter which run this was.
PROVENANCE = {provenance!r}


# --------------------------------------------------------------------------- #
# materials
# --------------------------------------------------------------------------- #


class AssetFetchError(RuntimeError):
    """The materials this task promises could not be put on the machine.

    Raised out of `setup`, which stops the episode, and that is the point.  An
    agent given a deck that is not there, or reference images that are not the
    ones the instruction describes, produces a trajectory indistinguishable
    from an agent that could not do the work — and it is a *stable* zero, so it
    reads as a capability floor rather than as the infrastructure failure it
    is.  There is no partial version of this to fall back to: a task whose
    materials did not arrive is not this task.
    """


def _asset_base() -> str:
    base = (os.environ.get(ASSET_BASE_ENV) or "").strip().rstrip("/")
    return base or HF_ASSET_BASE_URL


def _asset_url(repo_path: str) -> str:
    """Where one file is fetched from, honouring a local or mirrored base."""
    base = _asset_base()
    if "://" not in base:                      # a directory, not a URL
        return os.path.join(base, *repo_path.split("/"))
    return f"{{base}}/{{repo_path}}"


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _harness_cache_path(setup_controller, url: str, vm_path: str):
    """Where `SetupController._download_setup` leaves what it fetched.

    Recomputed rather than asked for, because the controller does not report
    it.  This is the only copy of the fetched bytes that exists on this side of
    the wire, so it is the only place the digest can be checked before the
    agent is handed the result.  `None` when the controller does not expose a
    cache directory at all — see `_verify_fetched` for what that means.
    """
    cache = getattr(setup_controller, "cache_dir", None)
    if not cache:
        return None
    return os.path.join(str(cache), "{{:}}_{{:}}".format(
        uuid.uuid5(uuid.NAMESPACE_URL, url), os.path.basename(vm_path)))


def _verify_fetched(setup_controller, files: list) -> str:
    """Refuse to continue unless what arrived is what this task recorded.

    `download` already raises when the fetch itself fails — a 404, a dead
    network, ten exhausted retries.  What it cannot notice is a fetch that
    *succeeded* and returned something else: a Git-LFS pointer instead of the
    file, a dataset folder rewritten under the same names, a mirror one commit
    behind.  Those arrive as bytes, upload cleanly, and are only visible as a
    digest that does not match the one written here when the task was built.

    Returns a one-line description of what was checked, so a run log says
    which of the two arms ran.  When the controller exposes no cache directory
    there is nothing on this side to hash: that is reported rather than
    treated as a failure, because the fetch's own error path is unaffected by
    it and refusing a controller shape we do not own would turn a harness
    difference into a task defect.
    """
    if not files:
        return "nothing to fetch"
    wrong = []
    checked = 0
    for (_, vm_path, want), f in zip(FETCH, files):
        cached = _harness_cache_path(setup_controller, f["url"], vm_path)
        if cached is None:
            return ("the controller exposes no download cache, so the fetched "
                    "bytes could not be checked against their digests")
        if not os.path.exists(cached):
            wrong.append(f"{{f['url']}} -> nothing at {{cached}}")
            continue
        got = _sha256_file(cached)
        checked += 1
        if got != want:
            wrong.append(f"{{f['url']}} -> sha256 {{got}}, expected {{want}}")
    if wrong:
        raise AssetFetchError(
            "the materials this task supplies did not arrive intact from "
            f"{{_asset_base()}}: " + "; ".join(wrong))
    return f"{{checked}} file(s) matched their recorded sha256"


def _fetch_assets(setup_controller) -> list:
    """Put every supplied file on the VM, or stop the episode saying why."""
    files = [{{"url": _asset_url(repo_path), "path": vm_path}}
             for repo_path, vm_path, _ in FETCH]
    if not files:
        return files
    try:
        setup_controller.download(files)
    except Exception as error:                 # noqa: BLE001 - re-raised named
        raise AssetFetchError(
            f"could not fetch this task's materials from {{_asset_base()}} "
            f"({{type(error).__name__}}: {{error}}) — the agent would have been "
            f"given a machine with no deck on it") from error
    _verify_fetched(setup_controller, files)
    return files


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
    "[ $OK -eq 1 ] && echo KEYSTROKE_SENT || echo KEYSTROKE_NOT_SENT"
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


def _save_status(attempted: bool, before: str, after: str) -> str:
    """What reached the *disk*, which is the only thing that can be acted on.

    The script above can only report whether it managed to deliver a
    keystroke.  That is not the question: `xdotool` returns success once the
    key has been sent to a window, and the fallback sends `ctrl+s` through
    pyautogui to whatever happens to have focus — so `KEYSTROKE_SENT` is
    consistent with the deck being saved, with a different window being saved,
    and with nothing happening at all.  The two digests either side of the
    attempt settle it without asking the application anything.

    What still cannot be settled from here, and is therefore said rather than
    guessed: when the bytes have not moved, "the keystroke missed the window"
    and "the application had nothing left to write" look identical.  Both mean
    the same thing to the evaluator — the file on disk is still the one setup
    uploaded — so the distinction only matters to somebody debugging the VM,
    and it needs the VM.
    """
    if not attempted:
        return "not needed — the file on disk had already moved"
    if not after:
        return "NOT_SAVED — nothing is at the pinned path any more"
    if after != before:
        return "SAVED — the bytes at the pinned path changed"
    return ("NOT_SAVED — the bytes at the pinned path are unchanged; whether "
            "the keystroke missed the window or the application had nothing "
            "to write cannot be told apart from here")


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

    The same hash answers the other question this used to guess at: whether
    the forced save actually saved anything.  See `_save_status`.
    """
    before = _vm_sha256(env, DECK_VM_PATH)
    evidence = {{"disk_changed_before_save": bool(before and before != INIT_SHA256)}}

    if before and before == INIT_SHA256:
        res = get_vm_command_line(env, {{
            "command": ["bash", "-c", _SAVE_SCRIPT % {{"win": "'" + DECK_NAME + "'"}}],
            "timeout": 180,
        }}) or ""
        evidence["save_attempted"] = True
        # A hint, and labelled as one: it says a key was sent, not that a file
        # was written.  `save_status` below is read off the disk instead.
        evidence["keystroke"] = (res.strip().splitlines()[-1]
                                 if res.strip() else "EMPTY")
    else:
        evidence["save_attempted"] = False
        evidence["keystroke"] = "not sent"

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
    after = _vm_sha256(env, DECK_VM_PATH)
    evidence["disk_sha_after"] = after
    evidence["save_status"] = _save_status(
        evidence["save_attempted"], before, after)
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


# The reference point for "newer than setup", in order of preference.
#
# This used to be `DECK_VM_PATH` alone, and that is dead in exactly the case
# the scan exists for.  `find -newer X` needs X to exist -- with X missing,
# `find` errors out, prints nothing, and the `2>/dev/null` swallows it -- and
# the scan is only ever run *because* the pinned deck is missing.  So the
# commonest shape of the mistake this path forgives (save somewhere else, then
# remove or rename the original) returned an empty list and scored 0.0 with
# the recovery machinery sitting right there.
#
# `SETUP_STAMP` is written by `setup` after the uploads, so it dates the end
# of setup with the machine's own clock -- no host/VM clock agreement to
# assume, and nothing the agent can do to the deck moves it.  The materials
# folder is the fallback for a package emitted before the stamp existed (its
# mtime moves when files are added to it, which is why it is second and not
# first), and the pinned deck is the last resort rather than the only one.
#
# **If none of the three is there, nothing is scored.**  Dropping the `-newer`
# clause and relying on the digests instead would admit every `.pptx` that
# came with the image, and a single one of those is indistinguishable from an
# agent's lone Save-As.  Saying "there was no reference point" is the honest
# answer and it is one line in the evidence.
_SCAN_REFERENCES = (SETUP_STAMP, MATERIALS_VM_DIR, DECK_VM_PATH)

_SCAN_SCRIPT = (
    "set +e; REF=''; "
    "for c in " + " ".join("'%s'" % r for r in _SCAN_REFERENCES) + "; do "
    "  if [ -e \\"$c\\" ]; then REF=\\"$c\\"; break; fi; "
    "done; "
    "if [ -z \\"$REF\\" ]; then echo 'REFERENCE none'; exit 0; fi; "
    "echo \\"REFERENCE $REF\\"; "
    "find '" + DESKTOP + "' /home/user -maxdepth 2 -name '*.pptx' -type f "
    "  -newer \\"$REF\\" -not -path '" + DECK_VM_PATH + "' -print0 2>/dev/null "
    "| xargs -0 -r sha256sum 2>/dev/null"
)


def _strays(env) -> tuple:
    """`([(path, sha256)], reference)` for every `.pptx` newer than setup.

    Digested on the machine in the same pass that finds them, because the
    digest is what tells one of these files from another and fetching each
    one to ask is a round trip per file.

    The reference the scan measured "newer" against is reported rather than
    assumed: which of the three candidates survived is the difference between
    a scan that looked at the right window and one that could not run, and it
    belongs in the evidence beside the result.  `"none"` means no candidate
    was there and no file was considered; `""` means the machine answered
    without saying, which is what an old fake does.
    """
    out = get_vm_command_line(env, {{
        "command": ["bash", "-c", _SCAN_SCRIPT],
        "timeout": 60,
    }}) or ""
    reference, found, seen = "", [], set()
    for line in out.strip().splitlines():
        line = line.strip()
        if line.startswith("REFERENCE "):
            reference = line[len("REFERENCE "):].strip()
            continue
        digest, _, path = line.partition(" ")
        path = path.strip()                    # sha256sum writes "<sha>  <path>"
        if len(digest) != 64 or not path:
            continue
        # One path, once.  The two roots overlap -- `/home/user/Desktop` is
        # itself at depth 1 under `/home/user`, so at `-maxdepth 2` every
        # Desktop deck is found down both -- and a duplicate is not a second
        # candidate.  Undeduplicated, a lone Save-As on the Desktop read as
        # "2 files could each be the result and nothing distinguishes them"
        # and was never scored, which is the recovery failing in the one
        # place it is most likely to be needed.  Invisible to a fake that
        # serves strays out of a dict, which is why it survived being driven
        # through every branch.
        if path in seen:
            continue
        seen.add(path)
        found.append((path, digest))
    if reference == "none":
        return [], "none"
    return found, reference


def _stray_candidate(env) -> tuple:
    """The deck the agent saved somewhere other than where it was asked to.

    Losing an agent's whole result to a Save-As is a scoring artefact, not a
    capability signal: the work still has to be right, only the filename was
    wrong.  But "the newest `.pptx` anywhere near the home directory" is not a
    description of the agent's result, and this used to score whichever one
    `find` happened to list first.  Two files are eligible for that by
    construction and neither is an answer:

      * **a `.pptx` among the materials setup uploaded.**  It is in the same
        tree, and it is *newer* than the pinned deck because setup wrote it
        afterwards.  Scoring the file we handed the agent as the agent's work
        is the worst kind of wrong: it is stable, so it would look like a
        capability floor rather than a bug.
      * **a copy of the untouched input**, saved out under another name
        without being edited.  Byte-identical to `init.pptx` is the one thing
        `evaluate` already refuses to score at the pinned path, and the
        refusal cannot be worth avoiding by renaming.

    Both are settled by the digest: this task knows exactly what it uploaded.
    What is left is a file whose contents nobody here supplied, which is the
    only thing a stray answer can be.

    **If more than one survives, none is scored.**  The alternative — take the
    best — turns a Save-As recovery into free retries: leave three attempts on
    the Desktop and be graded on the luckiest.  That is a strictly better
    strategy than doing the work once, so it is the strategy a training run
    would find.  Recovering *the* result the agent produced is what this is
    for; choosing among several candidates is a judgement the evaluator has no
    evidence for, and the honest answer is the instruction, which said to save
    in place.  The reason is recorded either way.

    Returns `(path, notes, reference)` — the path is `""` when nothing
    qualifies, and `reference` is what the scan measured "newer" against.
    """
    found, reference = _strays(env)
    keep, notes = [], []
    if reference == "none":
        notes.append(
            "nothing on the machine could date the end of setup — neither the "
            "marker setup writes, nor the materials folder, nor the deck "
            "itself is still there — so no file was scored: every .pptx that "
            "shipped with the image would otherwise have been a candidate")
        return "", notes, reference
    for path, digest in found:
        if digest == INIT_SHA256:
            notes.append(f"{{path}}: byte-identical to the deck setup uploaded, "
                         f"so nothing was written to it either")
        elif digest in MATERIAL_SHA256:
            notes.append(f"{{path}}: byte-identical to a file this task supplied "
                         f"in the materials folder, so it is not an answer")
        else:
            keep.append(path)
    if len(keep) == 1:
        return keep[0], notes, reference
    if len(keep) > 1:
        notes.append(
            "%d files could each be the result and nothing distinguishes "
            "them, so none is scored — the instruction asked for the deck to "
            "be saved in place: %s" % (len(keep), ", ".join(sorted(keep))))
    return "", notes, reference


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
        # An empty folder is not created: the instruction names this directory
        # only when something is in it, and a task that ships no materials
        # must not leave an empty folder on the Desktop for the agent to open.
        if any(vm.startswith(MATERIALS_VM_DIR + "/") for _, vm, _ in FETCH):
            setup_controller.execute(
                command=f"mkdir -p {{MATERIALS_VM_DIR}}", shell=True)
        # Fetched from the dataset rather than uploaded from beside this file:
        # the deck and its reference material are 2-13 MB a task and do not
        # belong in a git repository the whole benchmark clones.  This raises
        # rather than continuing if anything is missing or has the wrong bytes.
        _fetch_assets(setup_controller)

        # Date the end of setup, on the machine's own clock.  Written *after*
        # the uploads so that everything this task put on the VM is older than
        # it, and so the stray scan's window starts where the agent's work
        # does.  It is the reference `find -newer` uses; the deck used to be,
        # and the deck is missing in exactly the case the scan runs.
        setup_controller.execute(
            command=f"touch {{SETUP_STAMP}}", shell=True)

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
            stray, notes, reference = _stray_candidate(env)
            evidence["stray_reference"] = reference
            if notes:
                evidence["stray_rejected"] = notes
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


def _materials_not_in_manifest(deck, shipped: list) -> list[str]:
    """Shipped material names the deck's own manifest never claims to produce.

    Recorded, not refused.  `bundle()` copies the whole assets directory
    rather than the manifest's `produced` list, so a file left behind by an
    earlier `materialise` run ships under its old name: deck0006's bundle
    carries eight blot strips where four exist, five of them byte-identical
    duplicates, and the instruction says "the strip images are in the assets
    folder".  Fixing that is `bundle()`'s job — it is the same directory the
    solvability probe was shown, so a fix here would leave the probe judging a
    different delivery from the one that ships.  What belongs here is the
    fact, written down where a reader of the package can find it instead of
    having to diff two directories.
    """
    f = deck.root / "assets" / "manifest.json"
    if not f.exists():
        return []
    try:
        manifest = json.loads(f.read_text())
    except json.JSONDecodeError:
        return []
    produced = {a.get("file") for a in manifest.get("produced") or []
                if a.get("file")}
    return sorted(name for name in shipped if name not in produced)


def emit(deck, out_root: Path, task_id: str, *,
         run_id: str | None = None,
         hf_asset_repo: str | None = None) -> dict:
    """Write one runnable task. Deterministic; nothing here is judged.

    `run_id` is the run this package was built in, when the caller knows it.
    It is recorded rather than used, and it is null when nobody said — the
    field exists so the answer can be dropped in without re-shaping anything
    that already reads a provenance record.
    """
    hf_asset_repo = (hf_asset_repo
                     or os.environ.get("PPTXGYM_ASSETS_REPO")
                     or UNCONFIGURED_ASSET_REPO)
    hf_asset_base = (f"https://huggingface.co/datasets/{hf_asset_repo}"
                     "/resolve/main")
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
        # The bundle's tree is flattened onto basenames, because `setup`
        # uploads one folder.  Two files in different subdirectories with the
        # same basename would therefore silently overwrite each other and the
        # package would ship one file where the instruction names two — the
        # `keyframes` producer, which writes into `build-pNN/` directories, is
        # where that fires first.  A collision is refused rather than resolved
        # here: renaming is a decision about what the instruction says, and
        # this stage has no judgement in it.
        seen = {}
        for f in sorted(src_assets.rglob("*")):
            if not f.is_file():
                continue
            clash = seen.get(f.name)
            if clash is not None:
                raise EmitError(
                    f"{deck.id}: the bundle holds two files called {f.name!r} "
                    f"({clash} and {f.relative_to(src_assets)}) and the "
                    f"materials folder is flat, so one would silently replace "
                    f"the other")
            seen[f.name] = f.relative_to(src_assets)
            shutil.copy2(f, adir / "assets" / "materials" / f.name)

    # what only the evaluator gets
    from ..evaluation import inventory
    gt = inventory.inventory_pptx(str(deck.source))
    init = inventory.inventory_pptx(str(deck.input_pptx))
    (adir / "tests" / "assets" / "gt_inventory.json").write_text(
        json.dumps(gt, ensure_ascii=False, separators=(",", ":")))
    (adir / "tests" / "assets" / "init_inventory.json").write_text(
        json.dumps(init, ensure_ascii=False, separators=(",", ":")))
    (adir / "tests" / "assets" / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=1))

    # Written as a sorted tuple rather than a set literal: `repr` on a set of
    # strings follows the hash order, which moves with PYTHONHASHSEED, and an
    # emitted file that differs run to run is one nobody can diff.
    uploaded = [f for f in sorted((adir / "assets" / "materials").iterdir())
                if f.is_file()]
    material_shas = tuple(sorted(_sha256(f) for f in uploaded))

    deck_name = f"task_{task_id}.pptx"
    deck_vm = f"/home/user/Desktop/{deck_name}"
    materials_vm = f"/home/user/Desktop/task_{task_id}_materials"
    setup_stamp = f"/home/user/.task_{task_id}_setup"

    # The fetch list, in the order `setup` puts the files on the machine: the
    # deck first, then the materials by name.  Written out one entry per line
    # rather than as a bare `repr` because this is the part of the generated
    # file a reader is most likely to want to check by eye against the dataset.
    hf_dir = hf_asset_dir(task_id)
    fetch = [(f"{hf_dir}/init.pptx", deck_vm,
              _sha256(adir / "assets" / "init.pptx"))]
    for f in uploaded:
        fetch.append((f"{hf_dir}/materials/{f.name}",
                      f"{materials_vm}/{f.name}", _sha256(f)))
    fetch_src = ("(\n"
                 + "".join(f" {row!r},\n" for row in fetch)
                 + ")") if fetch else "()"

    weights = {c["id"]: round(float(c["weight"]), 6) for c in plan["components"]}
    descs = {c["id"]: _describe(c, plan) for c in plan["components"]}

    # The record of where this came from, written before the file that carries
    # it: `provenance.json` is the machine-readable copy and `PROVENANCE` in
    # the `.py` is the copy that travels when somebody moves the task file.
    prov = provenance(deck, task_id, run_id=run_id)
    prov["materials"] = [f.name for f in uploaded]
    prov["materials_not_in_manifest"] = _materials_not_in_manifest(
        deck, [f.name for f in uploaded])
    prov["init_sha256"] = _sha256(adir / "assets" / "init.pptx")
    # Where `setup` will look for the files under `assets/`, recorded here so
    # that a package and the dataset folder it depends on can be compared
    # without reading the generated Python.
    prov["assets"] = {"repo": hf_asset_repo, "dir": hf_dir,
                      "files": [row[0] for row in fetch]}
    code = prov["code"]
    prov_head = (
        f"Emitted {prov['emitted_at']} from {deck.id}, which was at stage\n"
        f"{prov['deck_stage'] or 'none'}, by commit "
        f"{code.get('commit') or 'unknown'}"
        f"{' (tree dirty)' if code.get('dirty') else ''}"
        f", in run {prov['run'] or '<unrecorded>'}.\n"
        f"`PROVENANCE` below and `provenance.json` beside the assets carry "
        f"that in full,\nincluding the content digests of everything the "
        f"emitter read, so that\n"
        f"`python3 -m pptxgym.tasks.emit --check <out-root>` can say when the deck "
        f"has moved\npast this package instead of leaving the two "
        f"indistinguishable.")

    # The materials sentence is written from the same value `setup` uploads to
    # and only when something was uploaded, so the instruction cannot name a
    # folder that will not be there.
    instruction = task["instruction"].rstrip()
    if uploaded:
        instruction += INSTRUCTION_MATERIALS.format(materials_path=materials_vm)
    instruction += INSTRUCTION_SUFFIX.format(deck_path=deck_vm)
    body = TASK_TEMPLATE.format(
        title=task.get("name", deck.id),
        deck_id=deck.id,
        task_id=task_id,
        deck_name=deck_name,
        deck_vm_path=deck_vm,
        materials_vm_dir=materials_vm,
        setup_stamp=setup_stamp,
        provenance=prov,
        provenance_head=prov_head,
        init_sha=prov["init_sha256"],
        material_shas=f"frozenset({material_shas!r})",
        hf_asset_repo=hf_asset_repo,
        hf_asset_base=hf_asset_base,
        asset_base_env=ASSET_BASE_ENV,
        fetch=fetch_src,
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
        # Not committed beside the task: 2-13 MB a task of deck and reference
        # material, fetched by `setup` from the dataset named here.
        "assets": {"repo": hf_asset_repo, "dir": hf_dir,
                   "base_url": hf_asset_base,
                   "override_env": ASSET_BASE_ENV,
                   "files": [row[0] for row in fetch]},
        # A pointer, not a copy: one record, in one place, that the check
        # reads.  Two copies of a provenance record is two things that can
        # disagree about which build this is.
        "provenance": PROVENANCE_FILE,
    }, ensure_ascii=False, indent=1))
    # Beside the assets, never inside them: `assets/` is the upload list.
    (adir / PROVENANCE_FILE).write_text(
        json.dumps(prov, ensure_ascii=False, indent=1))
    (adir / "README.md").write_text(_readme(task_id, task, plan, descs, prov))

    problems = check_package(py, adir)
    if problems:
        raise EmitError(f"{deck.id}: {problems[0]}")
    return {"task_id": task_id, "py": str(py), "assets": str(adir),
            "components": len(plan["components"])}


def _readme(task_id: str, task: dict, plan: dict, descs: dict,
            prov: dict) -> str:
    rows = "\n".join(
        f"| `{c['id']}` | {descs[c['id']][:110]} | {float(c['weight']):.4f} |"
        for c in plan["components"])
    code = prov.get("code") or {}
    deck_state = ", ".join(f"{k}={v}"
                           for k, v in (prov.get("deck_state") or {}).items())
    strays = prov.get("materials_not_in_manifest") or []
    extra = ("\n**Warning** — %d of the files under `assets/materials/` are not "
             "in the deck's own\n`manifest.json` `produced` list, so they were "
             "not decided on by the proposal:\n%s. See `bundle()`.\n"
             % (len(strays), ", ".join(f"`{n}`" for n in strays))) if strays else ""
    return f"""# task_{task_id} — {task.get('name', '')}

{task['instruction']}

**Difficulty** {task.get('difficulty')} · **est_steps** {task.get('est_steps')}
· **components** {len(plan['components'])} · derived from `{plan.get('deck')}`

## Where this came from

A package that cannot say which build of which deck it is looks exactly like
one that can. This one says so, and `provenance.json` beside this file carries
the same record with the content digests of everything the emitter read —
`python3 -m pptxgym.tasks.emit --check <out-root>` compares them against the deck and
reports any package the deck has since moved past.

| | |
| --- | --- |
| deck | `{prov.get('deck')}` — furthest stage `{prov.get('deck_stage') or 'none'}` |
| deck state at emission | {deck_state or 'unrecorded'} |
| emitted | {prov.get('emitted_at')} |
| commit | `{code.get('commit') or 'unknown'}`{' (tree dirty)' if code.get('dirty') else ''} |
| run | {prov.get('run') or '_unrecorded_'} |
{extra}
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


def _repo_path_local(repo_path: str) -> str:
    """`task_<id>/x` -> `x`: the same file's place under the staging `assets/`.

    The dataset folder and the staging directory hold the same tree under
    different roots, and this is the one line that says so.  Everything under
    `task_<id>/` maps straight through, which keeps `materials/foo.png` and
    `init.pptx` in one rule instead of two special cases.
    """
    return repo_path.split("/", 1)[1] if "/" in repo_path else repo_path


def _fetch_list(src: str) -> list[tuple[str, str, str]] | None:
    """`FETCH` as the generated file defines it, or None if it has none.

    Read with `ast` off the module's own top level, so this answers what the
    file that runs will do rather than what a pattern happens to match.  None
    is a real answer and means the file predates the dataset move — every
    package emitted before it uploaded from local disk instead.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "FETCH"):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            return None
        try:
            return [(str(a), str(b), str(c)) for a, b, c in value]
        except (TypeError, ValueError):
            return None
    return None


def fetch_list(py: Path) -> list[tuple[str, str, str]]:
    """What one emitted task fetches, as `(dataset path, VM path, sha256)`.

    The publisher's half of the same question `check_package` asks: the files
    it has to put in the dataset are exactly the ones the task will look for,
    and the only authority on that is the task file.
    """
    rows = _fetch_list(Path(py).read_text())
    if rows is None:
        raise EmitError(f"{Path(py).name}: no readable FETCH list — this "
                        f"package predates the move to a dataset and cannot "
                        f"be published without being re-emitted")
    return rows


def check_package(py: Path, adir: Path) -> list[str]:
    """Refuse to ship a task that hands the agent its own answer key.

    The rule everyone states is "the evaluator must not read test fixtures",
    but a delta-derived evaluator has to: the plan and the ground-truth
    inventory *are* how it scores.  The real invariant is the other way round
    — nothing the evaluator reads may be uploaded to the machine the agent
    works on — so this checks the upload list, not the read list.

    Since the materials moved to a dataset, the upload list is `FETCH` in the
    generated file rather than a directory listing, and it is read out of the
    file by `ast` rather than by regex: the answer to "what does this task put
    on the machine" has to come from the thing that runs, and a pattern that
    stops matching when the surrounding code is reshaped fails open.  The
    staging directory is still checked as well — it is what `publish` uploads
    to the dataset, so a secret sitting in it reaches the agent one step later.
    """
    out = []
    src = py.read_text()
    secret = {"plan.json", "gt_inventory.json", "init_inventory.json"}

    fetch = _fetch_list(src)
    if fetch is None:
        out.append("the generated file has no readable FETCH list, so what it "
                   "puts on the machine cannot be checked")
        fetch = []
    for repo_path, vm_path, _digest in fetch:
        name = repo_path.rsplit("/", 1)[-1]
        if name in secret:
            out.append(f"{name} is in the fetch list, so setup would put the "
                       f"answer key on the agent's machine")
    agent_dir = adir / "assets"
    for f in agent_dir.rglob("*"):
        if f.is_file() and f.name in secret:
            out.append(f"{f.name} is sitting in assets/, which is what gets "
                       f"published to the dataset setup fetches from")
    if "TEST_ASSETS" not in src:
        out.append("the evaluator does not read from tests/assets/")
    if not (adir / "tests" / "assets" / "plan.json").exists():
        out.append("no plan.json beside the task")
    # The deck is the one file without which there is no task at all, and a
    # fetch list that has lost it is the failure that reads as agent
    # incapability rather than as a broken package.
    if fetch and not any(_repo_path_local(p) == "init.pptx" for p, _, _ in fetch):
        out.append("the fetch list does not include the deck")
    for repo_path, vm_path, digest in fetch:
        if len(digest) != 64:
            out.append(f"{repo_path} has no usable sha256, so a fetch that "
                       f"returned the wrong bytes could not be noticed")
        local = adir / "assets" / _repo_path_local(repo_path)
        if not local.exists():
            out.append(f"{repo_path} is fetched but there is nothing at "
                       f"{local.relative_to(adir)} to publish under that name")
    # A package that cannot say where it came from is the one that got
    # shipped, so its absence is a refusal and not a warning.  It sits beside
    # `assets/`, never inside it — the record names the deck and its stage,
    # which is not the agent's business.
    if not (adir / PROVENANCE_FILE).exists():
        out.append(f"no {PROVENANCE_FILE} beside the task — nothing says "
                   f"which deck, which build or which commit this is")
    if (adir / "assets" / PROVENANCE_FILE).exists():
        out.append(f"{PROVENANCE_FILE} is sitting in assets/, which setup "
                   f"uploads")
    return out


def main(argv=None):
    import argparse
    from ..core import pipeline as pl

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("deck", nargs="?")
    ap.add_argument("--work", default="work")
    ap.add_argument("--out")
    ap.add_argument("--task-id")
    ap.add_argument("--run-id", default=None,
                    help="the run this package was built in, recorded in its "
                         "provenance")
    ap.add_argument("--asset-repo", default=None,
                    help="Hugging Face dataset baked into the emitted task; "
                         "publication supplies this from configuration")
    ap.add_argument("--check", metavar="OUT_ROOT",
                    help="report every package under OUT_ROOT and whether the "
                         "deck it came from has moved on since; exits non-zero "
                         "if any has")
    args = ap.parse_args(argv)

    if args.check:
        rows = check_emitted(Path(args.check), Path(args.work))
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 1 if any(not r["current"] for r in rows) else 0

    if not (args.deck and args.out and args.task_id):
        ap.error("deck, --out and --task-id are required unless --check is given")
    r = emit(pl.Deck(Path(args.work) / args.deck), Path(args.out),
             args.task_id, run_id=args.run_id,
             hf_asset_repo=args.asset_repo)
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
