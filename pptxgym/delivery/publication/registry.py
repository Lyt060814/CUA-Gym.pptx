"""Persistent task-ID allocation and rollout-repository reconciliation."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

REGISTRY_NOTE = (
    "Which 110xxxx each pptxgym deck was published as. Keyed by the deck's "
    "content checksum, never by its directory number: a deck number is a local "
    "sequence position that moves when a corpus is re-ingested, and a published "
    "id that moves is worse than an ugly one. This file is the authority on "
    "which numbers are taken; the task files beside it are where the result "
    "goes, not where the next number comes from. Written by pptxgym.delivery.publish; "
    "do not edit by hand while a publish is running."
)


@dataclass(frozen=True)
class Layout:
    task_class_rel: str
    task_assets_rel: str
    registry_rel: str
    task_list_rels: tuple[str, ...]
    series: str
    first: int
    last: int


def registry_path(rollout: Path, layout: Layout) -> Path:
    return Path(rollout) / layout.registry_rel


def empty_registry(layout: Layout) -> dict:
    return {"schema": 1, "series": layout.series, "note": REGISTRY_NOTE,
            "next": layout.first, "by_checksum": {}, "reserved": {}}


def load_registry(rollout: Path, layout: Layout, error_type=RuntimeError) -> dict:
    path = registry_path(rollout, layout)
    if not path.exists():
        return empty_registry(layout)
    try:
        registry = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise error_type(
            f"{path} will not parse ({error}); refusing to allocate ids against "
            "a registry that cannot be read, because the failure mode is "
            "reissuing a number that is already published") from error
    base = empty_registry(layout)
    base.update({key: value for key, value in registry.items() if key in base})
    return base


def save_registry(rollout: Path, registry: dict, layout: Layout) -> Path:
    path = registry_path(rollout, layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, ensure_ascii=False, indent=1) + "\n")
    return path


def refresh_task_lists(rollout: Path, layout: Layout,
                       error_type=RuntimeError) -> list[Path]:
    rollout = Path(rollout)
    task_dir = rollout / layout.task_class_rel
    ids = sorted(
        (match.group(1) for path in task_dir.glob(f"task_{layout.series}*.py")
         if (match := re.fullmatch(
             rf"task_({layout.series}\d{{4}})\.py", path.name))),
        key=int,
    )
    written = []
    for rel in layout.task_list_rels:
        path = rollout / rel
        if path.exists():
            try:
                document = json.loads(path.read_text())
            except json.JSONDecodeError as error:
                raise error_type(f"{path} will not parse ({error})") from error
        else:
            document = {"tasks": []}
        tasks = document.get("tasks")
        if not isinstance(tasks, list):
            raise error_type(f"{path} has no task list")
        document["tasks"] = [task for task in tasks if not re.fullmatch(
            rf"{layout.series}\d{{4}}", str(task))]
        document["tasks"].extend(ids)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n")
        written.append(path)
    return written


def ids_in_repo(rollout: Path, layout: Layout) -> dict[str, Path]:
    task_dir = Path(rollout) / layout.task_class_rel
    if not task_dir.is_dir():
        return {}
    result = {}
    for path in sorted(task_dir.glob(f"task_{layout.series}????.py")):
        task_id = path.stem[len("task_"):]
        if task_id.isdigit() and layout.first <= int(task_id) <= layout.last:
            result[task_id] = path
    return result


def _init_sha_of(path: Path) -> str:
    if not path.exists():
        return ""
    match = re.search(r"^INIT_SHA256 = ['\"]([0-9a-f]{64})['\"]",
                      path.read_text(), re.M)
    return match.group(1) if match else ""


def _sha256(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _shipped_source_key(rollout: Path, task_id: str, work: Path,
                        layout: Layout) -> tuple[str, str]:
    assets = Path(rollout) / layout.task_assets_rel / f"task_{task_id}"
    provenance = assets / "provenance.json"
    if provenance.exists():
        try:
            key = (json.loads(provenance.read_text()) or {}).get("source_key")
        except json.JSONDecodeError:
            key = None
        if key:
            return str(key), "its own provenance record"

    metadata = assets / "metadata.json"
    deck_id = None
    if metadata.exists():
        try:
            deck_id = (json.loads(metadata.read_text()) or {}).get("source_deck")
        except json.JSONDecodeError:
            deck_id = None
    if not deck_id or not str(deck_id).startswith("deck"):
        return "", "nothing in the package names a pptxgym deck"

    deck_root = Path(work) / str(deck_id)
    bundle = deck_root / "bundle" / "input.pptx"
    task_file = Path(rollout) / layout.task_class_rel / f"task_{task_id}.py"
    if not bundle.exists():
        return "", f"names {deck_id}, which is not in {work} to check against"
    expected = _init_sha_of(task_file)
    if not expected:
        return "", f"names {deck_id} but declares no INIT_SHA256 to check"
    if _sha256(bundle) != expected:
        return "", (f"names {deck_id}, but that deck's bundle is no longer the "
                    "deck this task ships — it has been rebuilt since")
    from ...core import pipeline
    return pipeline.task_id_for(pipeline.Deck(deck_root)), f"{deck_id}, digest agrees"


def survey_repo(registry: dict, rollout: Path, work: Path,
                layout: Layout) -> tuple[list[str], dict[str, str]]:
    notes, occupants = [], {}
    known = {record["id"]: key for key, record in
             (registry.get("by_checksum") or {}).items()}
    for task_id in sorted(ids_in_repo(rollout, layout)):
        key, how = _shipped_source_key(rollout, task_id, work, layout)
        occupants[task_id] = key
        if task_id not in known:
            notes.append(f"task_{task_id} is in the repository but not in the "
                         f"registry — {how}")
    for task_id in sorted(task_id for task_id in known if task_id not in occupants):
        notes.append(f"task_{task_id} is allocated to {known[task_id]} but is not "
                     "in the repository: either its push never landed, or it was "
                     "published and has since been deleted. Either way the number "
                     "stays with that deck and is not handed out again")
    return notes, occupants


def registered_ids(registry: dict) -> dict[str, str]:
    return {record["id"]: key for key, record in
            (registry.get("by_checksum") or {}).items()}


def conflicts(mapping: dict[str, str], occupants: dict[str, str],
              known_before: dict[str, str]) -> list[str]:
    problems = []
    for key, task_id in sorted(mapping.items(), key=lambda item: item[1]):
        if task_id not in occupants or occupants[task_id] == key:
            continue
        held = occupants[task_id]
        if held:
            problems.append(
                f"task_{task_id} is wanted for checksum {key} but the repository "
                f"already holds a task built from {held} — one number, two "
                "tasks, and only a human can say which one keeps it")
        elif task_id in known_before:
            problems.append(
                f"task_{task_id} is registered to {known_before[task_id]} and "
                "occupied by a file that cannot say which deck it came from")
        else:
            problems.append(
                f"task_{task_id} is wanted for checksum {key} but the repository "
                f"already holds a task_{task_id}.py this path did not publish — "
                "refusing to overwrite it")
    return problems


def allocate(registry: dict, keys: list[str], layout: Layout,
             error_type=RuntimeError) -> tuple[dict, dict[str, str], list[str]]:
    registry = json.loads(json.dumps(registry))
    by_checksum = registry.setdefault("by_checksum", {})
    reserved = registry.setdefault("reserved", {})
    next_id = int(registry.get("next") or layout.first)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    mapping, fresh = {}, []
    for key in keys:
        record = by_checksum.get(key)
        if record:
            mapping[key] = record["id"]
            continue
        occupied = {record["id"] for record in by_checksum.values()}
        while str(next_id) in reserved or str(next_id) in occupied:
            next_id += 1
        if next_id > layout.last:
            raise error_type(
                f"the {layout.series} series is full at {layout.last}; the next "
                "task needs a series of its own, and choosing one is not this "
                "script's decision")
        task_id = str(next_id)
        by_checksum[key] = {"id": task_id, "deck": None, "allocated_at": now}
        mapping[key] = task_id
        fresh.append(task_id)
        next_id += 1
    registry["next"] = next_id
    return registry, mapping, fresh
