"""Process identity and crash receipts used by WPS display sessions.

Every destructive operation is guarded by a Linux ``(boot, pid, start)``
identity.  A receipt records the exact X server and WPS clients started by one
display claim so a later run can reclaim only processes it can prove it owns.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import tempfile
import time
from pathlib import Path

BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
RECEIPT_VERSION = 1


def boot_id() -> str:
    with contextlib.suppress(OSError):
        return BOOT_ID_PATH.read_text().strip()
    return ""  # pragma: no cover


def proc_start(pid: int) -> int | None:
    """Return Linux process start jiffies, which disambiguate reused PIDs."""
    try:
        with open(f"/proc/{pid}/stat") as stream:
            return int(stream.read().rsplit(") ", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def proc_argv(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as stream:
            return [arg.decode("utf-8", "replace")
                    for arg in stream.read().split(b"\0") if arg]
    except OSError:
        return []


def proc_uid(pid: int) -> int | None:
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None


def proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def proc_env(pid: int) -> dict:
    try:
        with open(f"/proc/{pid}/environ", "rb") as stream:
            raw = stream.read()
    except OSError:
        return {}
    result = {}
    for item in raw.split(b"\0"):
        key, separator, value = item.decode("utf-8", "replace").partition("=")
        if separator:
            result.setdefault(key, value)
    return result


def identity(pid: int, *, start_of=proc_start, argv_of=proc_argv,
             uid_of=proc_uid) -> dict | None:
    start = start_of(pid)
    if start is None:
        return None
    return {"pid": pid, "start": start, "argv": argv_of(pid),
            "uid": uid_of(pid)}


def still_running(record: dict | None, *, start_of=proc_start) -> bool:
    """Whether a receipt still identifies the process currently at its PID."""
    if not record or record.get("start") is None:
        return False
    return start_of(record.get("pid", -1)) == record["start"]


def kill_identified(record: dict, grace: float = 5.0, *, still=still_running,
                    sleep=time.sleep, clock=time.time) -> bool:
    """Terminate one process, checking its identity before every signal."""
    pid = record["pid"]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not still(record):
            return True
        with contextlib.suppress(OSError, ProcessLookupError):
            os.kill(pid, sig)
        deadline = clock() + grace
        while clock() < deadline:
            if not still(record):
                return True
            sleep(0.05)
        grace = 2.0
    return not still(record)


def receipt_path(lock_dir, number: int) -> Path:
    return Path(lock_dir) / f"display{number}.json"


def read_receipt(lock_dir, number: int, *, path_of=receipt_path) -> dict | None:
    try:
        return json.loads(path_of(lock_dir, number).read_text())
    except (OSError, ValueError):
        return None


def write_receipt(lock_dir, number: int, receipt: dict, *,
                  path_of=receipt_path) -> None:
    """Atomically replace a receipt used to authorize later cleanup."""
    directory = Path(lock_dir)
    with contextlib.suppress(OSError):
        directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=directory, prefix=f"display{number}.", suffix=".tmp")
        with os.fdopen(fd, "w") as stream:
            json.dump(receipt, stream)
        os.replace(temporary, path_of(directory, number))


def new_receipt(number: int, *, current_boot=boot_id,
                identify=identity) -> dict:
    return {"version": RECEIPT_VERSION, "boot": current_boot(),
            "host": socket.gethostname(), "display": number,
            "owner": identify(os.getpid()), "claimed": time.time(),
            "server": None, "clients": [], "workdirs": []}


def amend_receipt(lock_dir, number: int, *, read=read_receipt,
                  write=write_receipt, create=new_receipt,
                  current_boot=boot_id, **fields) -> dict:
    receipt = read(lock_dir, number)
    if not receipt or receipt.get("boot") != current_boot():
        receipt = create(number)
    for key, value in fields.items():
        if key in ("clients", "workdirs") and not isinstance(value, list):
            receipt.setdefault(key, [])
            if value not in receipt[key]:
                receipt[key].append(value)
        else:
            receipt[key] = value
    write(lock_dir, number, receipt)
    return receipt


def drop_receipt(lock_dir, number: int, *, path_of=receipt_path) -> None:
    with contextlib.suppress(OSError):
        path_of(lock_dir, number).unlink()
