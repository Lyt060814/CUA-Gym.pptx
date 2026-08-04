"""Publish finished tasks to a Hugging Face dataset repository.

A deck that has passed `solvable` already carries the delivery shape: its
`bundle/` holds exactly what a solver is given and nothing else.  Publishing is
therefore not a matter of assembling anything — it is a matter of not adding
things back.

Three decisions are baked in here, each for a reason worth keeping:

**Batch, not stream.**  The obvious design pushes each deck as it finishes.  It
is wrong twice over.  A bundle is not final when it is first written: a deck
that the solvability probe rejects goes round the repair loop and its bundle is
rebuilt, so streaming would publish drafts and then churn them.  And commits are
the scarce resource — Hugging Face deliberately does not document the commit
rate limit — so several hundred single-deck commits is the one shape to avoid.
`CommitScheduler` has the same problem from the other end: it is tied to a live
process, and this pipeline stops and restarts constantly (rate limits, job
timeouts), which would fragment the history unpredictably.

**No Storage Bucket.**  A mutable staging area earns its keep when producers are
spread across machines.  They are not: the Anthropic rate limit is per account,
so adding machines buys no throughput and one machine produces everything.
`work/` is already the accumulation area, and it is already resumable.  A second
storage system would be a second thing to keep consistent for no gain.

**The public record is built by whitelist.**  `task.json` describes the damage —
`what_the_file_looks_like`, `notes`, `verdict_reason` — and shipping it verbatim
would hand over the answer with the question.  Fields are copied in explicitly;
nothing is published by deleting the parts we remembered to delete.

    python3 -m pptxgym.publish --repo you/pptx-tasks            # dry run
    python3 -m pptxgym.publish --repo you/pptx-tasks --push
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path

# Folder-entry ceiling is 10k; 500 tasks a shard keeps three orders of
# magnitude of headroom while staying browsable.
SHARD = 500
# Hugging Face recommends fewer than 100 files in a commit.  Budgeting in
# *tasks* was the wrong unit: a task is four fixed files plus however many
# assets it declares, and at eight files a task a "safe" batch of eighteen
# came to 108 files.  Count what the limit counts.
FILES_PER_COMMIT = 90

# Everything in the deck directory that is answer key.  Staging is built from
# `bundle/` alone, so none of these can arrive by accident — the guard exists
# because "cannot happen" is how the last leak got shipped.
NEVER_PUBLISH = ("source.pptx", "delta.json", "recipe.json", "proposal.json",
                 "manifest.json", "solvability.json", "roundtrip.json",
                 "roundtrip-wps.json", "repair.md")

ZENODO_API = "https://zenodo.org/api/records/"


class PublishError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# provenance
# --------------------------------------------------------------------------- #


def provenance_of(deck) -> dict:
    """Where this deck came from, or an explanation of why we cannot say.

    Attribution is a licence condition, not a nicety, so a task whose source
    cannot be named is not publishable.  The corpus loader writes this file
    when it fetches a deck; decks that arrived by other routes (the ten pilot
    decks were mined from GitHub) legitimately have none, and refusing them is
    the correct outcome rather than an inconvenience.
    """
    f = deck / "provenance.json"
    if not f.exists():
        raise PublishError(
            f"{deck.name}: no provenance.json — the source, its licence and "
            f"its creators are a condition of redistributing a derivative, "
            f"and none of them can be reconstructed from the deck")
    p = json.loads(f.read_text())
    for key in ("doi", "license"):
        if not (p.get(key) or "").strip():
            raise PublishError(f"{deck.name}: provenance.json has no `{key}`")
    return p


def zenodo_creators(doi: str, cache: Path) -> list[str]:
    """Creator names for a DOI, fetched once and remembered.

    The corpus metadata carries the DOI but not the authors, and the licence
    asks for attribution by name.  One call per selected deck is nothing; one
    call per publish run over the same decks is rude, hence the cache.
    """
    store = {}
    if cache.exists():
        try:
            store = json.loads(cache.read_text())
        except json.JSONDecodeError:
            store = {}
    if doi in store:
        return store[doi]

    m = re.search(r"zenodo\.(\d+)", doi)
    if not m:
        return []
    try:
        with urllib.request.urlopen(ZENODO_API + m.group(1), timeout=30) as r:
            rec = json.load(r)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    names = [c.get("name", "") for c in
             (rec.get("metadata") or {}).get("creators") or []]
    names = [n for n in names if n]
    store[doi] = names
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(store, ensure_ascii=False, indent=1))
    return names


# --------------------------------------------------------------------------- #
# the public record
# --------------------------------------------------------------------------- #


def public_record(task: dict, meta: dict, prov: dict, creators: list[str],
                  task_id: str, assets: list[str]) -> dict:
    """What a solver may know about its own task.

    Built by naming every field, never by removing fields from `task.json`.
    That file holds `what_the_file_looks_like` and `notes`, which describe the
    damage in plain language; a blacklist would have shipped them the first
    time somebody added a field.
    """
    return {
        "id": task_id,
        "name": task.get("name"),
        "instruction": task.get("instruction"),
        "difficulty": task.get("difficulty"),
        "est_steps": task.get("est_steps"),
        "n_slides": meta.get("slides"),
        "files": {"deck": "input.pptx", "instruction": "instruction.md",
                  "assets": assets},
        "source": {
            "doi": prov.get("doi"),
            "title": prov.get("title"),
            "license": prov.get("license"),
            "creators": creators,
            "url": prov.get("url"),
            "checksum": prov.get("checksum"),
        },
    }


ATTRIBUTION = """\
# Source and licence

This task is a **modified copy** of a third-party presentation. The deck was
deliberately damaged; the original is unaltered at the source below.

- **Title:** {title}
- **Creators:** {creators}
- **DOI:** [{doi}](https://doi.org/{doi_bare})
- **Original licence:** {license}

The original licence governs this derivative. Attribution is to the creators
above, not to this dataset.

**Note on embedded media.** The licence stated by the depositor covers the
presentation. Photographs, figures and logos embedded inside it may belong to
third parties and may carry their own terms; that is not resolvable from the
source metadata, and it is unchanged by our modifications.
"""


def attribution_md(prov: dict, creators: list[str]) -> str:
    doi = (prov.get("doi") or "").replace("https://doi.org/", "")
    return ATTRIBUTION.format(
        title=prov.get("title") or "(untitled)",
        creators="; ".join(creators) or "(not recorded)",
        doi=prov.get("doi") or "(none)", doi_bare=doi,
        license=prov.get("license") or "(unstated)")


# --------------------------------------------------------------------------- #
# staging
# --------------------------------------------------------------------------- #


def task_id_for(deck, prov: dict) -> str:
    """Stable across runs, and traceable back to the source.

    Deck directory numbers are local sequence positions — they shift the moment
    a corpus is re-ingested in a different order, and a published id that moves
    is worse than an ugly one.  The source checksum does not move.
    """
    csum = (prov.get("checksum") or "").strip()
    return f"task-{csum[:12]}" if csum else f"task-{deck.name}"


def publishable(work: Path) -> list[Path]:
    """Decks that have passed the last gate, in a stable order."""
    from . import pipeline as pl

    out = []
    for deck in pl.decks_in(work):
        if not deck.done("solvable"):
            continue
        sv = deck.root / "solvability.json"
        if not sv.exists():
            continue
        if json.loads(sv.read_text()).get("verdict") not in pl.PASSING_VERDICTS:
            continue
        out.append(deck.root)
    return out


def ensure_bundle(deck: Path) -> bool:
    """Rebuild the delivery directory if the deck does not have one.

    `bundle/` is written as a side effect of running the solvability probe, so
    a deck that passed before that mechanism existed — or one probed on another
    machine — holds a passing verdict with nothing to deliver.  Two of the ten
    pilot decks are in exactly that state.

    Rebuilding is safe rather than presumptuous: the bundle is a deterministic
    copy of `input.pptx`, the instruction and the assets, and the stage
    fingerprints guarantee those are the same bytes the verdict was reached on.
    Skipping the deck instead would drop a finished task out of the release
    without saying so, which is the failure this project keeps paying for.
    """
    from . import pipeline as pl

    if (deck / "bundle" / "input.pptx").exists():
        return False
    pl.bundle(pl.Deck(deck))
    return True


def stage_one(deck: Path, dest_root: Path, index: int, cache: Path) -> dict:
    """Copy one task's solver-visible files into the staging tree."""
    rebuilt = ensure_bundle(deck)
    bundle = deck / "bundle"
    task = json.loads((deck / "task.json").read_text())
    meta = json.loads((deck / "meta.json").read_text())
    prov = provenance_of(deck)
    creators = zenodo_creators(prov.get("doi", ""), cache)

    tid = task_id_for(deck, prov)
    shard = f"{index // SHARD:03d}"
    dest = dest_root / "tasks" / shard / tid
    (dest / "assets").mkdir(parents=True, exist_ok=True)

    shutil.copy2(bundle / "input.pptx", dest / "input.pptx")
    shutil.copy2(bundle / "instruction.md", dest / "instruction.md")
    assets = []
    src_assets = bundle / "assets"
    if src_assets.exists():
        for f in sorted(src_assets.rglob("*")):
            if f.is_dir():
                continue
            rel = f.relative_to(src_assets)
            (dest / "assets" / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest / "assets" / rel)
            assets.append(str(rel))

    rec = public_record(task, meta, prov, creators, tid, assets)
    (dest / "task.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=1))
    (dest / "ATTRIBUTION.md").write_text(attribution_md(prov, creators))

    rec["_rebuilt"] = rebuilt
    rec["_path"] = f"tasks/{shard}/{tid}"
    rec["_files"] = 4 + len(assets)
    rec["_bytes"] = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    return rec


def chunk_by_files(rows: list[dict]) -> list[list[dict]]:
    """Group tasks into commits by file count, greedily.

    A single task larger than the budget still gets its own commit rather than
    being dropped — exceeding the recommendation is survivable, omitting a
    finished task is not.
    """
    out, cur, n = [], [], 0
    for r in rows:
        if cur and n + r["_files"] > FILES_PER_COMMIT:
            out.append(cur)
            cur, n = [], 0
        cur.append(r)
        n += r["_files"]
    if cur:
        out.append(cur)
    return out


def leak_guard(dest_root: Path) -> list[str]:
    """Refuse to publish anything that is, or contains, the answer.

    Staging only ever copies from `bundle/`, so this should find nothing.  It
    runs anyway: the same reasoning — "that file cannot get in there" — is what
    shipped an answer key once already, and a publish is not reversible in the
    way a local mistake is.
    """
    bad = []
    for f in dest_root.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(dest_root)
        if f.name in NEVER_PUBLISH:
            bad.append(f"{rel} is answer-key material")
            continue
        if f.name != "task.json" or f.parent.name in ("tasks",):
            continue
        blob = f.read_text(errors="replace")
        for key in ("what_breaks", "what_the_file_looks_like", "degradations",
                    "verdict_reason", "rework", "removed_xml"):
            if f'"{key}"' in blob:
                bad.append(f"{rel} carries `{key}` — that describes the damage")
    return bad


# --------------------------------------------------------------------------- #
# card
# --------------------------------------------------------------------------- #


CARD = """\
---
license: other
license_name: mixed-per-item
task_categories:
- other
tags:
- computer-use
- reinforcement-learning
- powerpoint
configs:
- config_name: default
  data_files: index.jsonl
---

# {repo}

Computer-use tasks built from real PowerPoint decks. Each task is a deck that
has been **deliberately damaged** in a controlled way, plus the material a
solver is given to repair it. The intended agent works in a presentation
application with a mouse and keyboard.

{n} tasks.

## What a task is

```
tasks/<shard>/<task-id>/
  input.pptx        the damaged deck — this is what the agent opens
  instruction.md    everything the agent is told
  assets/           reference renders, extracted pictures, chart data
  task.json         id, instruction, difficulty, estimated GUI steps, source
  ATTRIBUTION.md    the source deck, its creators, its licence
```

`index.jsonl` has one row per task with the same fields, for filtering without
walking the tree.

**Ground truth is not in this repository.** The undamaged deck and the record of
what was changed are what a reward function compares against, and publishing
them beside the task would hand over the answer.

## Licensing — read this before redistributing

Every task derives from a separate third-party presentation with **its own
licence**. There is no single licence for this dataset; `license` in each
task's `source` block, and `ATTRIBUTION.md` beside it, are authoritative.

Source decks were filtered to permissive terms (CC-BY, CC0, public domain and
open-source licences). Items forbidding derivatives (ND) or restricting
commercial use (NC), and items whose licence was unstated, were excluded —
this dataset makes derivative works, so those terms are incompatible with it.

**Embedded third-party media.** A depositor's licence covers the presentation
they deposited. Photographs, figures and logos inside it may belong to others
and may carry their own terms. That cannot be resolved from source metadata,
and it is unchanged by our modifications. Anyone redistributing this material
inherits that unresolved question.

## Provenance

Source corpus: [Forceless/Zenodo10K](https://huggingface.co/datasets/Forceless/Zenodo10K),
itself drawn from Zenodo deposits. Each task records the DOI of the deck it came
from, so any item can be traced to its original record.

Built with [pptxgym](https://github.com/{repo}).
"""


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


def build(work: Path, dest_root: Path, repo: str,
          cache: Path | None = None) -> dict:
    """Stage every publishable task and return the plan."""
    decks = publishable(work)
    if not decks:
        raise PublishError(f"no deck in {work} has passed `solvable`")

    cache = cache or (work / ".zenodo-cache.json")
    rows, refused = [], []
    for i, deck in enumerate(decks):
        try:
            rows.append(stage_one(deck, dest_root, i, cache))
        except PublishError as e:
            refused.append(str(e))

    if not rows:
        raise PublishError(
            "every deck was refused; the first says: " + (refused[0] if refused
                                                          else "?"))

    index = dest_root / "index.jsonl"
    with open(index, "w") as fh:
        for r in rows:
            fh.write(json.dumps({k: v for k, v in r.items()
                                 if not k.startswith("_")},
                                ensure_ascii=False) + "\n")
    (dest_root / "README.md").write_text(CARD.format(repo=repo, n=len(rows)))

    leaks = leak_guard(dest_root)
    files = sum(r["_files"] for r in rows) + 2
    return {
        "tasks": len(rows), "refused": refused,
        "files": files, "bytes": sum(r["_bytes"] for r in rows),
        "shards": sorted({r["_path"].split("/")[1] for r in rows}),
        "commits": len(chunk_by_files(rows)) + 1,
        "leaks": leaks,
        "rebuilt": [r["_path"] for r in rows if r.get("_rebuilt")],
        "chunks": [[r["_path"] for r in c] for c in chunk_by_files(rows)],
        "widest_commit": max(sum(r["_files"] for r in c)
                             for c in chunk_by_files(rows)),
        "paths": [r["_path"] for r in rows],
    }


def push(dest_root: Path, repo: str, plan: dict, token: str | None = None):
    """Upload the staged tree in a handful of commits.

    `upload_folder` is resumable and splits large payloads itself;
    `upload_large_folder` is deprecated.  The chunking here is about commit
    *size* rather than total volume — the guidance is fewer than a hundred
    files in a commit, and a task is about five files.
    """
    from huggingface_hub import HfApi

    api = HfApi(token=token)
    api.create_repo(repo, repo_type="dataset", exist_ok=True)

    chunks = plan["chunks"]
    for n, chunk in enumerate(chunks, 1):
        api.upload_folder(
            repo_id=repo, repo_type="dataset", folder_path=str(dest_root),
            allow_patterns=[f"{p}/**" for p in chunk],
            commit_message=f"tasks {n}/{len(chunks)} ({len(chunk)} tasks)")
    # the card and the index describe the whole set, so they land last
    api.upload_folder(
        repo_id=repo, repo_type="dataset", folder_path=str(dest_root),
        allow_patterns=["README.md", "index.jsonl"],
        commit_message=f"index and card ({plan['tasks']} tasks)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default="work")
    ap.add_argument("--repo", required=True, help="e.g. someone/pptx-tasks")
    ap.add_argument("--stage", default=None,
                    help="where to build the tree (default: a temp dir)")
    ap.add_argument("--push", action="store_true",
                    help="actually upload; without it this is a dry run")
    ap.add_argument("--token", default=None)
    args = ap.parse_args(argv)

    import tempfile
    dest = Path(args.stage) if args.stage else Path(tempfile.mkdtemp(
        prefix="pptxgym-publish-"))
    dest.mkdir(parents=True, exist_ok=True)

    try:
        plan = build(Path(args.work), dest, args.repo)
    except PublishError as e:
        raise SystemExit(f"nothing to publish: {e}")

    print(f"staged  {dest}")
    print(f"  tasks   {plan['tasks']}   files {plan['files']}   "
          f"{plan['bytes'] / 1e6:.1f} MB")
    print(f"  shards  {plan['shards']}   commits {plan['commits']}   "
          f"widest {plan['widest_commit']} files (budget {FILES_PER_COMMIT})")
    for p in plan["paths"][:3]:
        print(f"    {p}/")
    if len(plan["paths"]) > 3:
        print(f"    … {len(plan['paths']) - 3} more")
    if plan["rebuilt"]:
        print(f"  rebuilt {len(plan['rebuilt'])} missing bundle(s) — those decks "
              f"passed before `bundle/` existed")
    if plan["refused"]:
        print(f"  refused {len(plan['refused'])}:")
        for r in plan["refused"][:5]:
            print(f"    ✗ {r}")
    if plan["leaks"]:
        print("  LEAK GUARD FAILED:")
        for l in plan["leaks"][:8]:
            print(f"    ✗ {l}")
        raise SystemExit(2)
    print("  leak guard  clean")

    if not args.push:
        print("\ndry run — nothing uploaded. add --push to publish.")
        return
    if plan["refused"]:
        raise SystemExit("refusing to push while any task lacks provenance")
    push(dest, args.repo, plan, args.token)
    print(f"\npushed {plan['tasks']} tasks to {args.repo}")


if __name__ == "__main__":
    main()
