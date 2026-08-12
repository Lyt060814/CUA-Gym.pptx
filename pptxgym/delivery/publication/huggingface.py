"""Hugging Face asset staging, upload, and fetch verification."""

from __future__ import annotations

import shutil
import urllib.error
import urllib.request
from pathlib import Path


FILES_PER_COMMIT = 90


def chunk_by_files(rows: list[dict]) -> list[list[dict]]:
    """Group tasks into commits by file count, greedily."""
    out, cur, count = [], [], 0
    for row in rows:
        size = len(row["hf_files"])
        if cur and count + size > FILES_PER_COMMIT:
            out.append(cur)
            cur, count = [], 0
        cur.append(row)
        count += size
    if cur:
        out.append(cur)
    return out


def build_tree(rows: list[dict], staging: Path) -> Path:
    """Lay out the exact dataset tree that will be uploaded."""
    tree = Path(staging) / "hf"
    for row in rows:
        for src, rel in row["hf_files"]:
            dest = tree / row["hf_dir"] / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    return tree


def upload_assets(rows: list[dict], repo: str, staging: Path,
                  token: str | None = None, *,
                  private: bool | None = None) -> None:
    """Upload task materials in commits bounded by file count."""
    from huggingface_hub import HfApi

    api = HfApi(token=token)
    api.create_repo(repo, repo_type="dataset", exist_ok=True,
                    private=private)
    tree = Path(staging) / "hf"
    chunks = chunk_by_files(rows)
    for index, chunk in enumerate(chunks, 1):
        api.upload_folder(
            repo_id=repo, repo_type="dataset", folder_path=str(tree),
            allow_patterns=[f"{row['hf_dir']}/**" for row in chunk],
            commit_message=f"pptxgym materials {index}/{len(chunks)} "
                           f"({len(chunk)} tasks)")


def prepare_repo(repo: str, token: str | None = None, *,
                 private: bool | None = None) -> None:
    """Ensure the target dataset exists before per-task uploads."""
    from huggingface_hub import HfApi

    HfApi(token=token).create_repo(repo, repo_type="dataset", exist_ok=True,
                                  private=private)


def upload_one(row: dict, repo: str, staging: Path,
               token: str | None = None) -> None:
    """Upload one task's materials in one commit."""
    from huggingface_hub import HfApi

    HfApi(token=token).upload_folder(
        repo_id=repo, repo_type="dataset",
        folder_path=str(Path(staging) / "hf"),
        allow_patterns=[f"{row['hf_dir']}/**"],
        commit_message=f"pptxgym materials for task_{row['id']}")


def verify_fetchable(rows: list[dict], repo: str,
                     token: str | None = None) -> list[str]:
    """Check every URL used by task setup before publishing task code."""
    problems = []
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for row in rows:
        for repo_path, _vm, _sha in row["fetch"]:
            url = f"https://huggingface.co/datasets/{repo}/resolve/main/{repo_path}"
            request = urllib.request.Request(
                url, method="HEAD", headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    if response.status >= 400:
                        problems.append(
                            f"{row['id']}: {url} -> {response.status}")
            except urllib.error.HTTPError as error:
                problems.append(f"{row['id']}: {url} -> HTTP {error.code}")
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                problems.append(f"{row['id']}: {url} -> {error}")
    return problems


def hf_url(repo: str, hf_dir: str, rel: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{hf_dir}/{rel}"
