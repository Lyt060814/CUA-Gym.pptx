"""Emit the ten rollout task files and their assets.

The shared runtime is embedded verbatim into every task file rather than
imported: the harness loads a task as one module, and a task whose evaluator
lives somewhere else is a task that can silently drift from the file it ships
with.  Generating all ten from one source is what keeps the ten copies
identical — ten hand-written evaluators would not stay equally strict.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import rollout_eval                                            # noqa: E402
import rollout_lib                                            # noqa: E402

WORK = HERE.parent / "work"
OUT_REPO = Path("/home/yitongli/XLANG/osworld2.0-rollout/evaluation_examples")
CLASS_DIR = OUT_REPO / "task_class"
ASSETS_DIR = OUT_REPO / "task_assets"
EVALUATOR = "pptx.degradation-restoration.v1"

# delta op -> which check answers "has this damage been undone"
OP_CHECK = {
    "delete": "restored_shape",
    "move": "restored_shape",
    "scatter": "restored_shape",
    "resize": "restored_shape",
    "rotate": "restored_shape",
    "swap": "restored_shape",
    "zorder": "restored_shape",
    "set_font": "text_style",
    "text_runs": "text_style",
    "clear_text": "text_style",
    "outline": "outline",
    "recolor": "outline",
    "strip_effects": "effects",
    "clear_table_cells": "table_cells",
    "table_drop_rows": "table_cells",
    "table_drop_cols": "table_cells",
    "smartart_drop_nodes": "diagram_nodes",
    "chart_edit": "chart_series",
}

def _tolerances(gt_inv, init_inv, page, path):
    """Tolerance scaled to the damage this component actually did.

    A fixed 0.35in band is generous for a shape flung across the slide and
    useless for one nudged a tenth of an inch — the degraded file already sits
    inside the band, so the check can never tell a solver from someone who did
    nothing.  Half the real displacement keeps the band as wide as the damage
    allows while guaranteeing the broken file falls outside it.
    """
    default = {"tol_in": 0.35, "size_frac": 0.25}
    if init_inv is None:
        return default

    def _find(inv, pg, pth):
        for slide in inv["slides"]:
            if slide["page"] != pg:
                continue
            for rec in slide["shapes"]:
                if rec["path"] == pth:
                    return rec
        return None

    gt_rec, init_rec = _find(gt_inv, page, path), _find(init_inv, page, path)
    if not gt_rec or not init_rec or not gt_rec.get("box") or not init_rec.get("box"):
        return default
    gb, ib = gt_rec["box"], init_rec["box"]
    moved = max(abs((gb[0] + gb[2] / 2) - (ib[0] + ib[2] / 2)),
                abs((gb[1] + gb[3] / 2) - (ib[1] + ib[3] / 2))) / 914400.0
    grew = max(abs(gb[2] - ib[2]) / max(gb[2], 1), abs(gb[3] - ib[3]) / max(gb[3], 1))
    out = dict(default)
    if moved > 0:
        # never finer than 0.08in — below that a mouse drag cannot land reliably
        # and the score becomes noise rather than signal
        out["tol_in"] = round(max(0.08, min(0.35, moved * 0.5)), 3)
    if grew > 0:
        out["size_frac"] = round(max(0.06, min(0.25, grew * 0.5)), 3)
    return out


HEADER = '''from __future__ import annotations

# Generated rollout task — the evaluator runtime below is shared verbatim by
# every task in this series. Regenerate with tools/build_rollout.py rather than
# editing here.

import hashlib
import json
import logging
import os
import posixpath
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from desktop_env.task_base import BaseTask
from desktop_env.evaluators.getters import get_vm_command_line

if TYPE_CHECKING:  # annotations only — never imported at runtime
    from desktop_env.controllers.setup import SetupController
    from desktop_env.desktop_env import DesktopEnv

logger = logging.getLogger("desktopenv.pptx_restoration_task")

TASK_CLASS_DIR = Path(__file__).resolve().parent
TASK_ASSETS_ROOT = TASK_CLASS_DIR.parent / "task_assets"
LINUX_DESKTOP = "/home/user/Desktop"
'''

# Force-save the open WPS deck. Same recipe as the validated OSWorld-V2 WPS
# tasks (077/087/090): xdotool ctrl+s, wmctrl+pyautogui fallback, then pkill.
SAVE_RUNTIME = r'''
_SAVE_SH = (
    "set -e; XOK=0; "
    "if ! command -v xdotool >/dev/null 2>&1; then "
    "  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xdotool "
    "    >/dev/null 2>&1 || true; "
    "fi; "
    "if command -v xdotool >/dev/null 2>&1 "
    "   && xdotool search --name 'WPS' >/dev/null 2>&1; then "
    "  xdotool search --name 'WPS' windowactivate --sync 2>/dev/null || true; "
    "  sleep 1; "
    "  xdotool key ctrl+s 2>/dev/null && XOK=1; "
    "  sleep 3; "
    "fi; "
    "if [ \"$XOK\" != \"1\" ]; then "
    "  if ! command -v wmctrl >/dev/null 2>&1; then "
    "    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq wmctrl "
    "      >/dev/null 2>&1 || true; "
    "  fi; "
    "  command -v wmctrl >/dev/null 2>&1 && wmctrl -a WPS 2>/dev/null || true; "
    "  sleep 1; "
    "  /usr/bin/python3 -c \"import pyautogui; pyautogui.hotkey('ctrl','s')\" "
    "    >/dev/null 2>&1 || true; "
    "  sleep 3; "
    "fi; "
    "echo SAVE_DONE"
)


def _persist_open_deck(env) -> str:
    """Force-save whatever WPS is holding, then close it.

    An agent that edited but left the file unsaved would otherwise be scored
    against the bytes it started with — a false zero that looks exactly like a
    failed attempt. Never raises: if every path fails, evaluation falls through
    to whatever is on disk.
    """
    try:
        out = get_vm_command_line(
            env, {"command": ["bash", "-lc", _SAVE_SH], "timeout": 120}) or ""
        status = out.strip().splitlines()[-1] if out.strip() else "EMPTY"
    except Exception as error:                                   # noqa: BLE001
        status = f"SAVE_ERR:{type(error).__name__}"
    try:
        get_vm_command_line(
            env,
            {"command": ["bash", "-c", "pkill -f wpp || true; pkill -f wps || true"],
             "timeout": 20})
    except Exception:                                            # noqa: BLE001
        pass
    time.sleep(3)
    return status


def _read_json(path: Path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _failed(reason: str, evaluator: str) -> dict:
    return {"evaluator": evaluator, "score": 0.0,
            "mean_component_progress": 0.0, "minimum_component_progress": 0.0,
            "hard_gates": {}, "components": [],
            "violations": {"runtime": [reason]}}
'''

CLASS_TMPL = '''

class Task{cls}(BaseTask):
    """{docstring}"""

    snapshot = "ubuntu"
    id = "{tid}"
    instruction = {instruction!r}
    trajectory = "trajectories/"
    related_apps = ["wps"]
    platform = "linux"
    proxy = False
    fixed_ip = False
    possibility_of_env_change = "low"
    intermediate_eval_safe = False
    volume_size = 60

    @property
    def task_assets_dir(self) -> Path:
        return TASK_ASSETS_ROOT / f"task_{{self.id}}"

    @property
    def test_assets_dir(self) -> Path:
        return self.task_assets_dir / "tests" / "assets"

    @property
    def vm_pptx(self) -> str:
        return f"{{LINUX_DESKTOP}}/pptx_task_{{self.id}}.pptx"

    @property
    def vm_materials(self) -> str:
        return f"{{LINUX_DESKTOP}}/pptx_task_{{self.id}}_materials"

    def setup(self, setup_controller: "SetupController", use_proxy: bool = False) -> None:
        setup_controller.execute(command=f"rm -rf {{self.vm_pptx}} {{self.vm_materials}}",
                                 shell=True)

        uploads = [{{"local_path": str(self.task_assets_dir / "assets" / "init.pptx"),
                    "path": self.vm_pptx}}]
        materials_dir = self.task_assets_dir / "assets" / "materials"
        for item in sorted(materials_dir.glob("*")) if materials_dir.exists() else []:
            uploads.append({{"local_path": str(item),
                            "path": f"{{self.vm_materials}}/{{item.name}}"}})
        setup_controller._upload_file_setup(uploads)

        # xdotool is not on the base AMI and evaluate() needs it to force-save
        # the agent's in-memory WPS edits before the file is pulled.
        setup_controller.execute([
            "bash", "-lc",
            "command -v xdotool >/dev/null 2>&1 || "
            "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xdotool "
            "  >/dev/null 2>&1 || true",
        ])

        # `wpp` directly rather than xdg-open: the AMI's .pptx handler is not
        # guaranteed to be WPS, and the whole evaluation assumes WPS.
        setup_controller.launch(["wpp", self.vm_pptx])
        time.sleep(3)

    def evaluate(self, env: "DesktopEnv") -> dict:
        from desktop_env.evaluators.getters import get_vm_file

        try:
            save_status = _persist_open_deck(env)
            logger.info("task %s: force-save -> %s", self.id, save_status)

            result_path = get_vm_file(
                env, {{"path": self.vm_pptx, "dest": f"pptx_task_{{self.id}}_result.pptx"}})
            if not result_path or not os.path.exists(result_path):
                return _failed("The presentation was not found at its original path",
                               "{evaluator}")

            candidate = inventory_pptx(Path(result_path))
            gt = _read_json(self.test_assets_dir / "gt_inventory.json")
            init = _read_json(self.test_assets_dir / "init_inventory.json")
            plan = _read_json(self.test_assets_dir / "plan.json")
            result = evaluate_candidate(candidate, gt, init, plan)
            result["runtime_evidence"] = {{
                "candidate_sha256": _sha256(Path(result_path)),
                "force_save": save_status,
            }}
            return result
        except Exception as error:
            logger.exception("Evaluation failed for task %s", self.id)
            return _failed(f"Evaluation failed closed: {{type(error).__name__}}: {{error}}",
                           "{evaluator}")


TASK_CLASS = Task{cls}
'''


def build_plan(deck_dir: Path, delta: dict, gt_inv: dict,
               init_inv: dict | None = None) -> dict:
    """One component per (page, op) group — a slide's twenty deletions are one
    piece of work, not twenty."""
    import zipfile as _zf
    from xml.etree import ElementTree as _ET
    with _zf.ZipFile(deck_dir / "source.pptx") as z:
        root = _ET.fromstring(z.read("ppt/presentation.xml"))
        sz = None
        for node in root:
            if node.tag.rsplit("}", 1)[-1] == "sldSz":
                sz = node
        slide_w = int(sz.attrib["cx"]) if sz is not None else 9144000
        slide_h = int(sz.attrib["cy"]) if sz is not None else 6858000

    groups: dict[tuple[int, str], list[dict]] = {}
    for page_key, entries in (delta.get("slides") or {}).items():
        page = int(page_key) + 1
        for e in entries:
            groups.setdefault((page, e["op"]), []).append(e)

    components = []
    for (page, op), entries in sorted(groups.items()):
        check = OP_CHECK.get(op)
        if check is None:
            continue
        if check == "restored_shape":
            for e in entries:
                if not e.get("path") or e["path"] == "-":
                    continue
                components.append({
                    "id": f"p{page:02d}-{op}-{e['path'].replace('/', '_')}",
                    "check": check, "page": page, "gt_path": e["path"],
                    "op": op,
                    **_tolerances(gt_inv, init_inv, page, e["path"]),
                    "what": (e.get("text") or e.get("name") or e.get("kind") or "")[:60],
                })
        elif check in ("text_style", "outline", "effects"):
            for e in entries:
                if not e.get("path") or e["path"] == "-":
                    continue
                components.append({
                    "id": f"p{page:02d}-{op}-{e['path'].replace('/', '_')}",
                    "check": check, "page": page, "gt_path": e["path"], "op": op,
                    "what": (e.get("text") or e.get("name") or "")[:60],
                })
        elif check == "table_cells":
            cells = []
            for e in entries:
                cells += e.get("cleared") or []
                for row in e.get("removed") or []:
                    for ci, val in enumerate(row.get("cells") or []):
                        cells.append({"at": [row.get("row", 0), ci], "was": val})
            components.append({"id": f"p{page:02d}-{op}", "check": check,
                               "page": page, "op": op,
                               "gt_path": entries[0].get("path"), "cells": cells})
        elif check == "diagram_nodes":
            nodes = []
            for e in entries:
                nodes += [n["text"] for n in (e.get("removed_nodes") or [])]
            components.append({"id": f"p{page:02d}-{op}", "check": check,
                               "page": page, "op": op, "nodes": nodes})
        elif check == "chart_series":
            series = []
            for e in entries:
                series += [s["name"] for s in (e.get("removed_series") or [])]
            components.append({"id": f"p{page:02d}-{op}", "check": check,
                               "page": page, "op": op, "series": series})

    return {
        "evaluator": EVALUATOR,
        "affected_pages": sorted({int(k) + 1 for k in (delta.get("slides") or {})}),
        "slide_w": slide_w, "slide_h": slide_h,
        "components": components,
    }


def emit(deck_dir: Path, task_id: str) -> dict:
    ns: dict = {}
    exec(compile("import hashlib, json, posixpath, re, zipfile\n"
                 "import xml.etree.ElementTree as ET\n"
                 + rollout_lib.RUNTIME + rollout_eval.RUNTIME,
                 "<runtime>", "exec"), ns)

    task = json.loads((deck_dir / "task.json").read_text())
    delta = json.loads((deck_dir / "delta.json").read_text())
    meta = json.loads((deck_dir / "meta.json").read_text())

    gt_inv = ns["inventory_pptx"](str(deck_dir / "source.pptx"))
    init_inv = ns["inventory_pptx"](str(deck_dir / "input.pptx"))
    plan = build_plan(deck_dir, delta, gt_inv, init_inv)

    # measure the floor: what each component already scores on the broken file
    for spec in plan["components"]:
        fn = ns["CHECKERS"].get(spec["check"])
        if fn is None:
            continue
        try:
            raw, _ = fn(init_inv, gt_inv, spec)
        except Exception:                                        # noqa: BLE001
            raw = 0.0
        spec["floor"] = round(float(raw), 4)

    # A component the broken file already satisfies cannot tell a solver apart
    # from someone who did nothing, so it must not be scored — the degradation
    # is real but invisible to this check (a scatter that landed inside the
    # positional tolerance, most often). Dropped, and recorded so the gap is
    # visible rather than silently averaged in.
    unscoreable = [c for c in plan["components"] if c.get("floor", 0.0) >= 0.999]
    plan["components"] = [c for c in plan["components"]
                          if c.get("floor", 0.0) < 0.999]
    plan["unscoreable"] = [{"id": c["id"], "op": c["op"], "page": c["page"],
                            "why": "degraded file already satisfies this check"}
                           for c in unscoreable]

    tdir = ASSETS_DIR / f"task_{task_id}"
    (tdir / "assets" / "materials").mkdir(parents=True, exist_ok=True)
    (tdir / "tests" / "assets").mkdir(parents=True, exist_ok=True)
    shutil.copy(deck_dir / "input.pptx", tdir / "assets" / "init.pptx")
    listed = {a["file"] for a in task["assets"] if a.get("file")}
    for item in sorted((deck_dir / "assets").glob("*")):
        if item.name in listed:
            shutil.copy(item, tdir / "assets" / "materials" / item.name)
    for name, payload in (("gt_inventory.json", gt_inv),
                          ("init_inventory.json", init_inv),
                          ("plan.json", plan)):
        (tdir / "tests" / "assets" / name).write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    instruction = task["instruction"].strip()
    if "materials" not in instruction.lower():
        instruction += (f" The reference material for this job is in the "
                        f"pptx_task_{task_id}_materials folder on the Desktop.")
    if "save" not in instruction.lower():
        instruction += " Save your changes to the open presentation."

    body = (HEADER + rollout_lib.RUNTIME + rollout_eval.RUNTIME + SAVE_RUNTIME
            + CLASS_TMPL.format(cls=task_id, tid=task_id, instruction=instruction,
                                evaluator=EVALUATOR,
                                docstring=f"Restore a degraded WPS deck "
                                          f"({len(plan['components'])} components)."))
    (CLASS_DIR / f"task_{task_id}.py").write_text(body)

    (tdir / "metadata.json").write_text(json.dumps({
        "id": task_id, "source_deck": meta.get("name"),
        "origin": meta.get("origin"), "domain": "pptx_degradation_restoration",
        "difficulty": task["difficulty"], "estimated_gui_actions": task["est_steps"],
        "environment": "WPS Presentation", "platform": "linux",
        "input_file": f"/home/user/Desktop/pptx_task_{task_id}.pptx",
        "materials_dir": f"/home/user/Desktop/pptx_task_{task_id}_materials",
        "affected_slides": plan["affected_pages"],
        "component_count": len(plan["components"]),
        "component_checks": sorted({c["check"] for c in plan["components"]}),
        "evaluator": EVALUATOR,
        "full_credit_contract": "score == 1.0 requires every component complete "
                                "and every hard gate passing",
        "degradations": task["degradations"],
        "notes": task.get("notes", ""),
        "tags": ["wps-presentation", "pptx-restoration", "long-horizon"],
    }, ensure_ascii=False, indent=1))

    files = []
    for item in sorted(tdir.rglob("*")):
        if item.is_file() and item.name != "asset_manifest.json":
            files.append({"path": str(item.relative_to(tdir)),
                          "bytes": item.stat().st_size,
                          "sha256": hashlib.sha256(item.read_bytes()).hexdigest()})
    (tdir / "asset_manifest.json").write_text(json.dumps(
        {"task_id": task_id, "schema": "pptx-rollout-assets-v1", "files": files},
        ensure_ascii=False, indent=1))

    return {"task": task_id, "deck": deck_dir.name,
            "components": len(plan["components"]),
            "pages": plan["affected_pages"],
            "materials": len(list((tdir / "assets" / "materials").glob("*")))}


def main():
    CLASS_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    for i, deck in enumerate(sorted(WORK.glob("deck0*")), start=1):
        if not (deck / "task.json").exists():
            print(f"skip {deck.name} — not reconciled")
            continue
        info = emit(deck, f"110{i:04d}")
        print(f"  task_{info['task']}  <- {info['deck']}  "
              f"{info['components']} components  pages={info['pages']}  "
              f"materials={info['materials']}")


if __name__ == "__main__":
    main()
