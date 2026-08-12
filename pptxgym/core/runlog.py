"""Append-only run event streams and their readback helpers."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


RUNS = "runs"
RUN_EVENTS = "events.jsonl"
RUN_SCHEMA = 1

EVENTS = {
    "run_started": "the header: run id, argv, resolved limits, commit",
    "stage_started": "a deck took a pool slot and began working",
    "stage_finished": "a stage recorded a status",
    "stage_skipped": "nothing was done, and why — usually a cache hit",
    "stage_retried": "an attempt died on infrastructure and was retried",
    "sent_back": "a gate's verdict sent a deck to an earlier stage",
    "note": "anything a command wants on the record",
    "run_finished": "the footer: how it ended, and how long it took",
}

STATUS_MEANING = {
    "ok": "finished", "partial": "finished with a gap the next gate judges",
    "skipped": "did not apply to this deck", "rejected": "a gate said no",
    "failed": "the output did not pass its checker",
    "infra": "the API failed; nothing about the deck was judged",
    "needs_human": "parked", "crashed": "an exception nobody expected",
    "stale": "retired because something upstream moved",
}

EVENT_STR_MAX = 240
EVENT_LIST_MAX = 3
NEVER_CLIPPED = ("argv", "limits", "to")
RESERVED = ("t", "ts", "run", "event", "deck", "stage", "status", "ms")


def _small(value):
    """Clip a value to something that belongs on one event line."""
    if isinstance(value, str):
        return value[:EVENT_STR_MAX]
    if isinstance(value, (list, tuple)):
        return [_small(item) for item in list(value)[:EVENT_LIST_MAX]]
    if isinstance(value, dict):
        return {key: _small(item)
                for key, item in list(value.items())[:8]}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:EVENT_STR_MAX]


class RunLog:
    """One append-only, per-record-flushed event stream for a whole run."""

    def __init__(self, path: Path, run_id: str):
        self.path = Path(path)
        self.run_id = run_id
        self.started = time.time()
        self.counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", buffering=1, encoding="utf-8")

    def emit(self, event: str, deck: str | None = None,
             stage: str | None = None, **fields) -> dict:
        """Write one record; a logging failure never masks the real event."""
        record = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  "ts": round(time.time(), 3), "run": self.run_id,
                  "event": event}
        if deck:
            record["deck"] = deck
        if stage:
            record["stage"] = stage
        for key, value in fields.items():
            if value is not None and key not in record:
                record[key] = value if key in NEVER_CLIPPED else _small(value)
        with self._lock:
            self.counts[event] = self.counts.get(event, 0) + 1
            try:
                self._fh.write(json.dumps(record, ensure_ascii=False,
                                          default=str) + "\n")
                self._fh.flush()
            except (OSError, ValueError):
                pass
        return record

    def close(self, **fields) -> None:
        self.emit("run_finished",
                  wall_s=round(time.time() - self.started, 1),
                  events=dict(self.counts), **fields)
        with self._lock:
            try:
                self._fh.close()
            except OSError:
                pass


_RUN: RunLog | None = None


def open_run(work, argv=None, limits=None, decks=None, cmd: str | None = None,
             run_id: str | None = None, version: dict | None = None) -> RunLog:
    """Start a stream under ``work/runs/<run-id>`` and make it current."""
    global _RUN
    work = Path(work)
    run_id = run_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    _RUN = RunLog(work / RUNS / run_id / RUN_EVENTS, run_id)
    version = version or {"commit": None, "dirty": None}
    _RUN.emit("run_started", schema=RUN_SCHEMA, pid=os.getpid(),
              work=str(work), argv=list(argv or []), cmd=cmd,
              limits=limits or {}, decks=decks,
              commit=version["commit"] or "unversioned",
              dirty=bool(version["dirty"]))
    return _RUN


def close_run(**fields) -> None:
    global _RUN
    if _RUN is not None:
        _RUN.close(**fields)
    _RUN = None


def run_log() -> RunLog | None:
    return _RUN


def log_event(event: str, **fields) -> None:
    if _RUN is not None:
        _RUN.emit(event, **fields)


def run_dirs(work) -> list[Path]:
    directory = Path(work) / RUNS
    if not directory.is_dir():
        return []
    return sorted((path for path in directory.iterdir()
                   if path.is_dir() and (path / RUN_EVENTS).exists()),
                  key=lambda path: path.name)


def latest_run(work) -> Path | None:
    runs = run_dirs(work)
    return runs[-1] if runs else None


def read_events(path) -> list[dict]:
    """Read a stream, ignoring malformed or half-written records."""
    path = Path(path)
    if path.is_dir():
        path = path / RUN_EVENTS
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    out.append(record)
    except OSError:
        return []
    return out
