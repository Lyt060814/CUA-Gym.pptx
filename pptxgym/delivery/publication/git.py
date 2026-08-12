"""Rollout checkout validation, task placement, and atomic git publication."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def git(rollout: Path, *args: str, check: bool = True,
        error_type=RuntimeError) -> str:
    result = subprocess.run(
        ["git", "-C", str(rollout), *args], capture_output=True, text=True)
    if check and result.returncode:
        raise error_type(
            f"git {' '.join(args)} failed: "
            f"{(result.stderr or result.stdout).strip()}")
    return result.stdout


def rollout_problems(rollout: Path, *, task_class_rel: str,
                     error_type=RuntimeError) -> list[str]:
    """Reasons a checkout is not fit to receive a publication commit."""
    out = []
    rollout = Path(rollout)
    if not (rollout / ".git").exists():
        return [f"{rollout} is not a git checkout"]
    if not (rollout / task_class_rel).is_dir():
        out.append(f"{rollout} has no {task_class_rel}/ — this is not the "
                   f"rollout repository")
    dirty = git(rollout, "status", "--porcelain",
                error_type=error_type).strip()
    if dirty:
        out.append(f"{rollout} has uncommitted changes "
                   f"({len(dirty.splitlines())} path(s)); publishing would "
                   f"sweep them into our commit")
    return out


def place_task_files(rows: list[dict], rollout: Path, *,
                     task_class_rel: str, task_assets_rel: str) -> list[str]:
    """Copy the git half of every package into the checkout."""
    written = []
    rollout = Path(rollout)
    for row in rows:
        py_dest = rollout / task_class_rel / f"task_{row['id']}.py"
        py_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(row["py"], py_dest)
        written.append(str(py_dest.relative_to(rollout)))
        for src, rel in row["git_files"]:
            dest = rollout / task_assets_rel / f"task_{row['id']}" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            written.append(str(dest.relative_to(rollout)))
    return written


def commit_and_push(rollout: Path, paths: list[str], message: str, *,
                    push: bool = True, error_type=RuntimeError) -> str:
    """Commit one batch and rebase-retry when the shared remote moves."""
    call = lambda *args: git(rollout, *args, error_type=error_type)
    call("add", "--", *paths)
    if not call("diff", "--cached", "--name-only").strip():
        return "nothing to commit — the repository already holds these files"
    call("commit", "-m", message)
    head = call("rev-parse", "--short", "HEAD").strip()
    if not push:
        return f"committed {head}, not pushed"

    branch = call("rev-parse", "--abbrev-ref", "HEAD").strip()
    for attempt in range(3):
        try:
            call("push", "origin", branch)
            break
        except Exception:
            if attempt == 2:
                raise
            call("pull", "--rebase", "origin", branch)
    head = call("rev-parse", "--short", "HEAD").strip()
    return f"committed {head} and pushed to origin/{branch}"
