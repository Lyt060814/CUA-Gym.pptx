"""Cross-process worker slots shared by orchestrators and nested commands."""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from dataclasses import dataclass
from pathlib import Path


CPU_WORKERS_ENV = "PPTXGYM_CPU_WORKERS"


@dataclass(frozen=True)
class Lease:
    slot: int
    slots: int
    waited_s: float


def workers_from_env(name: str, *, default: int | None = None) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        workers = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if workers < 1:
        raise ValueError(f"{name} must be at least 1")
    return workers


@contextlib.contextmanager
def claim(work: str | Path, pool: str, workers: int, *, poll_s: float = 0.2):
    """Claim one advisory-lock slot visible to every process in a run."""
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if not pool or not all(c.isalnum() or c in "-_" for c in pool):
        raise ValueError(f"invalid slot pool {pool!r}")
    lock_dir = Path(work) / f".{pool}-slots"
    lock_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    while True:
        for slot in range(workers):
            fd = os.open(lock_dir / f"slot-{slot:02d}.lock",
                         os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            lease = Lease(slot + 1, workers,
                          round(time.monotonic() - started, 1))
            try:
                yield lease
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            return
        time.sleep(poll_s)
