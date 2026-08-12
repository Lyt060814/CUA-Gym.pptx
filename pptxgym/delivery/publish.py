"""Put an approved task where the benchmark can run it — in both places at once.

A packaged task is two artefacts with two homes, and neither is optional:

* the **`.py`** goes to git, into `evaluation_examples/task_class/` of the
  rollout repository.  It is the whole task apart from its materials — the
  embedded scoring runtime, the save contract, the harness attributes.
* the **materials** go to the configured Hugging Face dataset under
  `task_<id>/`.  They are 2-13 MB a task; the first thirteen were
  ~100 MB, and the hundreds this pipeline exists to produce would be a
  gigabyte of binary in a repository every contributor to the benchmark
  clones.

**Both or neither.**  A `.py` whose materials did not upload is broken in the
worst available way: `setup` hands the agent a machine with no deck on it, the
episode records a stable zero, and the trajectory is indistinguishable from an
agent that could not do the work.  So the order is fixed — upload the
materials, *check they can be fetched back*, and only then write the `.py`.
Anything whose materials did not verify is dropped from the git commit and
named in the summary rather than shipped and hoped for.

**`--aws-verify` makes that check the strongest one available**: instead of
asking whether a URL resolves, it runs the task's own `setup()` on a real VM
against the URLs this run just baked in.  The order does not change and the
consequence does not change — it is the same slot, asking a better question, and
a task that does not pass is still dropped and still named.  What it adds is
*which kind* of failure it was: a broken task and a failed instance both come
back as a non-zero exit, and only one of them is the task's fault.  See
`pptxgym.delivery.vmsmoke`.

**Publishing is a batch step over approved tasks.**  Never a side effect of
packaging: a bundle is not final when it is first written (a deck the
solvability probe rejects is worked on again and its bundle rebuilt), and
commits are the scarce resource at both destinations — Hugging
Face deliberately does not document its commit rate limit.  A task that has not
reached `packaged` cannot be published at all, and a package whose deck has
moved on since is refused rather than shipped: `emit.check_emitted` answers
that question against the deck itself, so "current" is a comparison and not an
opinion.

**The published name is allocated once and remembered.**  See `Registry`.

    python3 -m pptxgym.delivery.publish --rollout ../osworld2.0-rollout      # dry run
    python3 -m pptxgym.delivery.publish --rollout ../osworld2.0-rollout --push
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .publication import attribution as publication_attribution
from .publication import git as publication_git
from .publication import registry as publication_registry
from .publication.huggingface import (
    FILES_PER_COMMIT,
    build_tree as build_hf_tree,
    chunk_by_files,
    hf_url,
    prepare_repo,
    upload_assets,
    upload_one,
    verify_fetchable,
)

# --------------------------------------------------------------------------- #
# the two destinations
# --------------------------------------------------------------------------- #
#
# Publication destinations are deliberately absent here. The managed CLI
# reads them from config and the expert CLI requires both explicitly; importing
# this module must never select or create a repository on the user's behalf.

TASK_CLASS_REL = "evaluation_examples/task_class"
TASK_ASSETS_REL = "evaluation_examples/task_assets"
SCALING_LIST_REL = "evaluation_examples/test_cua_scaling.json"
PPTXGYM_LIST_REL = "evaluation_examples/test_pptxgym.json"
TASK_LIST_RELS = (PPTXGYM_LIST_REL, SCALING_LIST_REL)

#: Where the id registry lives.  See `Registry` for why it is here.
REGISTRY_REL = f"{TASK_ASSETS_REL}/pptxgym-ids.json"

#: Our slice of the id space.  114, 115, 117 and 118 belong to other people;
#: this allocator must never leave 110.
SERIES = "110"
SERIES_FIRST = 1100001
SERIES_LAST = 1109999


def configure_layout(*, task_class_dir: str | None = None,
                     task_assets_dir: str | None = None,
                     registry: str | None = None,
                     task_lists: list[str] | tuple[str, ...] | None = None,
                     series: str | None = None,
                     series_first: int | None = None,
                     series_last: int | None = None) -> None:
    """Configure one compatible rollout layout for the current process."""
    global TASK_CLASS_REL, TASK_ASSETS_REL, REGISTRY_REL, TASK_LIST_RELS
    global SERIES, SERIES_FIRST, SERIES_LAST
    if task_class_dir:
        TASK_CLASS_REL = task_class_dir
    if task_assets_dir:
        TASK_ASSETS_REL = task_assets_dir
    if registry:
        REGISTRY_REL = registry
    if task_lists is not None:
        TASK_LIST_RELS = tuple(task_lists)
    if series:
        SERIES = str(series)
    if series_first is not None:
        SERIES_FIRST = int(series_first)
    if series_last is not None:
        SERIES_LAST = int(series_last)
    if SERIES_FIRST > SERIES_LAST:
        raise PublishError("series_first must not exceed series_last")

#: Everything in a deck directory that is answer key.  The staging tree is
#: built by `emit`, which copies from `bundle/` alone, so none of these can
#: arrive by accident — the guard exists because "cannot happen" is how the
#: last leak got shipped, and an upload is not reversible the way a local
#: mistake is.
NEVER_PUBLISH = ("source.pptx", "delta.json", "recipe.json", "proposal.json",
                 "manifest.json", "solvability.json", "roundtrip.json",
                 "roundtrip-wps.json", "repair.md", "plan.json",
                 "gt_inventory.json", "init_inventory.json")

ZENODO_API = "https://zenodo.org/api/records/"
REGISTRY_NOTE = publication_registry.REGISTRY_NOTE
ATTRIBUTION = publication_attribution.ATTRIBUTION


class PublishError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #


def _registry_layout() -> publication_registry.Layout:
    return publication_registry.Layout(
        task_class_rel=TASK_CLASS_REL,
        task_assets_rel=TASK_ASSETS_REL,
        registry_rel=REGISTRY_REL,
        task_list_rels=tuple(TASK_LIST_RELS),
        series=SERIES,
        first=SERIES_FIRST,
        last=SERIES_LAST,
    )


def registry_path(rollout: Path) -> Path:
    return publication_registry.registry_path(rollout, _registry_layout())


def empty_registry() -> dict:
    return publication_registry.empty_registry(_registry_layout())


def load_registry(rollout: Path) -> dict:
    return publication_registry.load_registry(
        rollout, _registry_layout(), PublishError)


def save_registry(rollout: Path, reg: dict) -> Path:
    return publication_registry.save_registry(rollout, reg, _registry_layout())


def refresh_task_lists(rollout: Path) -> list[Path]:
    return publication_registry.refresh_task_lists(
        rollout, _registry_layout(), PublishError)


def ids_in_repo(rollout: Path) -> dict[str, Path]:
    return publication_registry.ids_in_repo(rollout, _registry_layout())


def _init_sha_of(py: Path) -> str:
    return publication_registry._init_sha_of(py)


def _shipped_source_key(rollout: Path, tid: str,
                        work: Path) -> tuple[str, str]:
    return publication_registry._shipped_source_key(
        rollout, tid, work, _registry_layout())


def survey_repo(reg: dict, rollout: Path, work: Path) -> tuple[list[str],
                                                               dict[str, str]]:
    return publication_registry.survey_repo(reg, rollout, work,
                                            _registry_layout())


def registered_ids(reg: dict) -> dict[str, str]:
    return publication_registry.registered_ids(reg)


def conflicts(mapping: dict[str, str], occupants: dict[str, str],
              known_before: dict[str, str]) -> list[str]:
    return publication_registry.conflicts(mapping, occupants, known_before)


def allocate(reg: dict, keys: list[str]) -> tuple[dict, dict[str, str],
                                                  list[str]]:
    return publication_registry.allocate(reg, keys, _registry_layout(),
                                         PublishError)


# --------------------------------------------------------------------------- #
# what may be published
# --------------------------------------------------------------------------- #


def approved(work: Path, only: list[str] | None = None, *,
             recover_packaged: bool = False) -> tuple[list, list[str]]:
    """Decks that have passed the last gate, in a stable order.

    `packaged` and not merely `solvable`: packaging is where the consistency
    report runs and where the emitter refuses a rejected plan, and a task that
    has not been through it has never been checked as a *task*.

    Staleness warns; it does not refuse.  It used to, and that read exactly
    backwards here: the foreman's collect *re-executes* `harden`, which
    rewrites `attacks.json`, which `packaged` fingerprints — so every deck the
    foreman verified marked itself stale by the very act of being verified,
    and seven of nine finished tasks were refused publication on the strength
    of their own re-verification.  What actually guards this batch is
    `bundle_problems` and the smoke test, and both read the bytes on disk; a
    fingerprint saying "something moved" is a note for the operator.

    A normal batch also requires ``foreman.json`` to say ``shipped``. Merely
    reaching ``packaged`` is not a terminal decision: a run interrupted after
    packaging can still have a failed probe, and publishing every package once
    one sibling shipped leaked exactly those tasks.

    ``recover_packaged`` is the explicit disaster-recovery exception. It is
    accepted only with an ``only`` allowlist, so an operator can recover named
    packages whose earlier shipped record was overwritten without turning the
    exception into the default for every other deck in the archive.
    """
    from ..core import pipeline as pl

    if recover_packaged and only is None:
        raise PublishError(
            "recovering packaged tasks requires an explicit deck allowlist")

    requested = set(only) if only is not None else None
    out, refused, found = [], [], set()
    for deck in pl.decks_in(Path(work)):
        if requested is not None and deck.id not in requested:
            continue
        found.add(deck.id)
        if not recover_packaged:
            try:
                foreman = json.loads((deck.root / "foreman.json").read_text())
            except (OSError, ValueError):
                foreman = {}
            if foreman.get("outcome") != "shipped":
                refused.append(
                    f"{deck.id}: foreman outcome is "
                    f"{foreman.get('outcome')!r}, not 'shipped'")
                continue
        raw = (deck.state().get("packaged") or {}).get("status")
        if raw == "ok":
            moved = deck.stale("packaged")
            if moved:
                print(f"    · {deck.id}: packaged is stale ({', '.join(moved)})"
                      f" — publishing anyway; the bundle check and the smoke "
                      f"test read the bytes, not the fingerprint")
            out.append(deck)
        elif raw is not None:
            refused.append(f"{deck.id}: packaged is {raw!r}, not 'ok'")
    missing = sorted((requested or set()) - found)
    if missing:
        raise PublishError("the publish allowlist names unknown deck(s): "
                           + ", ".join(missing))
    return out, refused


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# attribution
# --------------------------------------------------------------------------- #


provenance_of = publication_attribution.provenance_of


def zenodo_creators(doi: str, cache: Path) -> list[str]:
    publication_attribution.ZENODO_API = ZENODO_API
    return publication_attribution.zenodo_creators(doi, cache)


def attribution_md(prov: dict, creators: list[str]) -> str:
    publication_attribution.ATTRIBUTION = ATTRIBUTION
    return publication_attribution.attribution_md(prov, creators)


# --------------------------------------------------------------------------- #
# staging
# --------------------------------------------------------------------------- #
#
# `emit` writes one directory per task holding both destinations' files mixed
# together, because that is the shape a package has on its own.  Splitting them
# is this module's job and it is done by naming what goes where, never by
# deleting what should not have gone:
#
#   assets/**   -> Hugging Face, under task_<id>/
#   everything else (metadata.json, README.md, provenance.json, tests/**)
#     -> git, under evaluation_examples/task_assets/task_<id>/
#
# The `.py` goes to git on its own, under evaluation_examples/task_class/.


def split_package(adir: Path) -> tuple[list[tuple[Path, str]],
                                       list[tuple[Path, str]]]:
    """`(to Hugging Face, to git)` as `(file, relative destination)` pairs."""
    hf, git = [], []
    for f in sorted(adir.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(adir)
        if rel.parts[0] == "assets":
            hf.append((f, "/".join(rel.parts[1:])))
        else:
            git.append((f, "/".join(rel.parts)))
    return hf, git


def leak_guard(rows: list[dict]) -> list[str]:
    """Refuse to upload anything that is, or contains, the answer.

    Only the Hugging Face side is checked for the answer key, because only that
    side reaches the machine the agent works on.  `tests/assets/plan.json` and
    the two inventories go to git on purpose — a delta-derived evaluator scores
    by reading them — and a check that could not tell those two directories
    apart would have to be switched off, which is worse than not having one.

    This should find nothing: the staging tree is written by `emit`, which
    copies the agent's files from `bundle/` alone, and `emit.check_package`
    already refuses a package with a secret under `assets/`.  It runs anyway,
    because the same reasoning — "that file cannot get in there" — is what
    shipped an answer key once already, and an upload is not reversible.
    """
    bad = []
    for row in rows:
        for src, rel in row["hf_files"]:
            if src.name in NEVER_PUBLISH:
                bad.append(f"{row['id']}: {rel} is answer-key material")
    return bad


def stage(work: Path, staging: Path, mapping: dict[str, str],
          decks: list, *, run_id: str | None = None,
          cache: Path | None = None,
          hf_asset_repo: str | None = None) -> tuple[list[dict], list[str]]:
    """Re-emit every approved deck under its published number.

    Re-emitted rather than renamed.  The id is not a label on the outside of
    the package: it is the class name, the path the deck lands at on the VM,
    the name of the materials folder and every URL in `FETCH`.  Renaming files
    afterwards would leave a task called 1100014 that fetches `task_9c4d67ef`
    and puts its deck at a path with a different name in it.

    This is also where the freshness gate bites: `emit` refuses a rejected
    plan, and `emit.check_emitted` then asks the deck whether the package still
    describes it.  A deck that has moved on is refused here, before anything
    has been uploaded anywhere.
    """
    from ..tasks import emit

    rows, refused = [], []
    cache = cache or (Path(work) / ".zenodo-cache.json")
    for deck in decks:
        from ..core import pipeline as pl
        key = pl.task_id_for(deck)
        tid = mapping[key]
        try:
            out = emit.emit(deck, staging, tid, run_id=run_id,
                            hf_asset_repo=hf_asset_repo)
        except emit.EmitError as error:
            refused.append(f"{deck.id}: {error}")
            continue
        adir = Path(out["assets"])
        hf, git = split_package(adir)

        prov = provenance_of(deck)
        creators = zenodo_creators((prov or {}).get("doi", ""), cache) if prov \
            else []
        if prov:
            (adir / "ATTRIBUTION.md").write_text(attribution_md(prov, creators))
            git.append((adir / "ATTRIBUTION.md", "ATTRIBUTION.md"))
        else:
            # Attribution is a licence condition. Publishing without it is
            # allowed to keep a batch moving, but silently is how forty
            # CC-BY tasks once went out with no source record at all.
            print(f"    · {deck.id}: no provenance.json — {tid} publishes "
                  f"WITHOUT ATTRIBUTION.md; backfill it", flush=True)

        rows.append({
            "id": tid, "key": key, "deck": deck.id,
            "py": Path(out["py"]),
            "assets_dir": adir,
            "hf_dir": emit.hf_asset_dir(tid),
            "hf_files": hf, "git_files": git,
            "fetch": emit.fetch_list(Path(out["py"])),
            "components": out["components"],
            "source": (prov or {}).get("doi") or None,
            "license": (prov or {}).get("license") or None,
            "bytes_hf": sum(f.stat().st_size for f, _ in hf),
            "bytes_git": (Path(out["py"]).stat().st_size
                          + sum(f.stat().st_size for f, _ in git)),
        })

    # The freshness question, asked of the deck rather than of the package.
    stale = {r["task"]: r["problems"]
             for r in emit.check_emitted(staging, Path(work))
             if not r["current"]}
    kept = []
    for row in rows:
        problems = stale.get(f"task_{row['id']}")
        if problems:
            refused.append(f"{row['deck']} (task_{row['id']}): "
                           f"{problems[0]}")
        else:
            kept.append(row)
    return kept, refused


# --------------------------------------------------------------------------- #
# the VM check
# --------------------------------------------------------------------------- #


@dataclass
class VmCheck:
    """The settings for the strongest available `verify_fetchable`.

    A value object rather than eight keyword arguments threaded through
    `publish`: the flags are read once, in `main`, and the same object is what
    a test hands in with `smoke` replaced by a fake.  `smoke` is the only
    injected member, and it is injected because the real one costs an EC2
    instance and four minutes — everything else about the stage (the order,
    the pools, the retries, the classification) is exercised for free.
    """

    artefacts: Path
    osworld: Path | None = None
    uv: str | None = None
    aws_workers: int = 4
    hf_workers: int = 4
    attempts: int = 3
    instance_type: str | None = None
    region: str | None = None
    cooldown: float = 60.0
    upload: object | None = None            # tests: skip the real dataset
    fetch_check: object | None = None       # tests: skip the real URLs
    smoke: object | None = None             # tests: skip the real VM
    log: object | None = None

    def ready(self) -> tuple[Path, str]:
        """Resolve and check the runner *before* anything is spent.

        Every deck would fail identically for a missing `uv` or a missing
        checkout, and forty identical `unverified` records is forty minutes
        spent proving one thing about this machine.
        """
        from . import vmsmoke

        osworld = vmsmoke.resolve_osworld(self.osworld)
        uv = vmsmoke.resolve_uv(self.uv)
        vmsmoke.preflight(osworld, uv)
        return osworld, uv

    def runner(self):
        """The `smoke(row, out_dir)` this stage will actually call."""
        from . import vmsmoke

        if self.smoke is not None:
            return self.smoke
        osworld, uv = self.ready()
        runner = vmsmoke.runner_path(osworld)

        def smoke(row, out_dir):
            return vmsmoke.run_smoke(
                Path(row["py"]), out_dir, runner=runner, osworld=osworld,
                uv=uv, instance_type=self.instance_type, region=self.region)
        return smoke


def vm_check(rows: list[dict], repo: str, staging: Path, vm: VmCheck,
             token: str | None = None):
    """Upload, fetch-check and smoke-test every task; return the report.

    The three phases are per deck and the only barrier is the caller's commit.
    This function is where the `.py` under test comes from, and it is the
    *staging* copy on purpose: the repository must not hold the file until the
    file has been proved, and the staging tree already has the rollout's shape
    (`task_class/` beside `task_assets/`), so the task resolves its evaluator
    fixtures exactly as it will once committed.
    """
    from . import vmsmoke

    smoke = vm.runner()                       # raises before anything is spent
    if vm.upload is None:
        prepare_repo(repo, token)
        def upload(row): upload_one(row, repo, staging, token)
    else:
        upload = vm.upload

    return vmsmoke.verify_batch(
        rows,
        upload=upload,
        fetch_check=(vm.fetch_check
                     or (lambda row: verify_fetchable([row], repo, token))),
        smoke=smoke,
        artefacts=Path(vm.artefacts),
        aws_workers=vm.aws_workers, hf_workers=vm.hf_workers,
        attempts=vm.attempts, cooldown=vm.cooldown, log=vm.log)


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #


def _git(rollout: Path, *args: str, check: bool = True) -> str:
    return publication_git.git(
        rollout, *args, check=check, error_type=PublishError)


def rollout_problems(rollout: Path) -> list[str]:
    """Reasons this checkout is not fit to be published into.

    A dirty tree is the one that matters.  `git commit` on a repository with
    somebody else's uncommitted work in it sweeps that work into our commit if
    the paths overlap, and leaves it stranded if they do not; either way the
    commit no longer says what it claims to.
    """
    return publication_git.rollout_problems(
        rollout, task_class_rel=TASK_CLASS_REL, error_type=PublishError)


def place_task_files(rows: list[dict], rollout: Path) -> list[str]:
    """Copy the git half of every package into the checkout. Returns the paths."""
    return publication_git.place_task_files(
        rows, rollout, task_class_rel=TASK_CLASS_REL,
        task_assets_rel=TASK_ASSETS_REL)


def commit_and_push(rollout: Path, paths: list[str], message: str,
                    *, push: bool = True) -> str:
    """One commit for the batch, and nothing if there is nothing to commit.

    `git commit` with no staged change fails, and a publish run that finds
    everything already published is a *success* — it is what re-running looks
    like.  So emptiness is checked and reported rather than raised.
    """
    return publication_git.commit_and_push(
        rollout, paths, message, push=push, error_type=PublishError)


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


def build(work: Path, staging: Path, rollout: Path, repo: str, *,
          republish: bool = False, run_id: str | None = None,
          only: list[str] | None = None,
          recover_packaged: bool = False) -> dict:
    """Everything a publish would do, decided but not done."""
    work, staging, rollout = Path(work), Path(staging), Path(rollout)
    decks, refused = approved(work, only=only,
                              recover_packaged=recover_packaged)
    if not decks:
        raise PublishError(
            f"no deck in {work} has reached `packaged` — publishing is a batch "
            f"step over approved tasks and there are none"
            + (f" ({refused[0]})" if refused else ""))

    from ..core import pipeline as pl
    keys = [pl.task_id_for(d) for d in decks]

    # One source deck, one published task.
    #
    # The id is the *source's* checksum, which is what makes it survive a
    # re-ingest — and it therefore cannot tell two tasks built from the same
    # source apart.  The pipeline's claim index (`work/.by-content`) stops the
    # same file being ingested twice, so this should be unreachable; when it is
    # not, the two decks would be emitted into one directory under one number
    # and the second would silently overwrite the first.  Silently is the
    # problem: the batch would look like it published both.
    seen: dict[str, str] = {}
    duplicates = []
    for deck, key in zip(decks, keys):
        if key in seen:
            duplicates.append(f"{seen[key]} and {deck.id} were built from the "
                              f"same source ({key}), so they would be "
                              f"published as one task and one would be lost")
        seen[key] = deck.id
    if duplicates:
        raise PublishError("two approved decks claim one published id:\n  "
                           + "\n  ".join(duplicates))

    base_reg = load_registry(rollout)
    known_before = registered_ids(base_reg)
    notes, occupants = survey_repo(base_reg, rollout, work)

    # ``stage`` is itself a gate: emit can reject a stale or incoherent
    # package. Allocating every approved deck before that gate left permanent
    # holes whenever one was refused. Re-run the local, token-free emit with a
    # compact allocation until the set of surviving rows is stable. Existing
    # allocations in ``base_reg`` are retained; only fresh, unshipped numbers
    # are reconsidered.
    candidate_decks = decks
    candidate_keys = keys
    for _ in range(len(decks) + 1):
        reg, mapping, fresh = allocate(base_reg, candidate_keys)
        rows, more_refused = stage(
            work, staging, mapping, candidate_decks, run_id=run_id,
            hf_asset_repo=repo)
        refused += more_refused
        surviving = {row["key"] for row in rows}
        if surviving == set(candidate_keys):
            break
        candidate_decks = [deck for deck in candidate_decks
                           if pl.task_id_for(deck) in surviving]
        candidate_keys = [pl.task_id_for(deck) for deck in candidate_decks]
    else:  # pragma: no cover - each pass must remove at least one candidate
        raise PublishError("publication staging did not converge")

    clashes = conflicts(mapping, occupants, known_before)
    if clashes:
        raise PublishError(
            "the numbers this batch was allocated are not free in "
            f"{rollout}:\n  " + "\n  ".join(clashes))

    already = [r for r in rows if r["id"] in occupants]
    if not republish:
        rows = [r for r in rows if r["id"] not in occupants]

    build_hf_tree(rows, staging)
    leaks = leak_guard(rows)

    return {
        "work": str(work), "staging": str(staging), "rollout": str(rollout),
        "repo": repo,
        "registry": str(registry_path(rollout)),
        "registry_next": reg["next"],
        "notes": notes,
        "allocated": fresh,
        "mapping": mapping,
        "rows": rows,
        "already": [r["id"] for r in already],
        "refused": refused,
        "leaks": leaks,
        "no_source_record": [r["id"] for r in rows if not r["source"]],
        "hf_files": sum(len(r["hf_files"]) for r in rows),
        "hf_bytes": sum(r["bytes_hf"] for r in rows),
        "hf_commits": len(chunk_by_files(rows)) if rows else 0,
        "git_files": sum(len(r["git_files"]) + 1 for r in rows),
        "git_bytes": sum(r["bytes_git"] for r in rows),
        "_registry": reg,
    }


def publish(plan: dict, *, token: str | None = None,
            push_git: bool = True, vm: VmCheck | None = None) -> dict:
    """Do it, in the order that makes a half-done publish impossible.

    Materials first, then a check that every URL a task will ask for answers,
    and only then the `.py`.  The reverse order — or no verification between
    them — ships a task file whose materials are not there, which is a silent
    zero rather than a visible failure.

    `vm` replaces the middle step with `vmsmoke`: same slot, same consequence,
    a better question.  The one thing it changes is that verification becomes
    *per task* rather than all-or-nothing, so a batch can be partly good — the
    failures are dropped and named, which is what this function already did
    for a package the deck had moved past.
    """
    rows, repo = plan["rows"], plan["repo"]
    rollout = Path(plan["rollout"])
    out = {"uploaded": 0, "verified": False, "git": "", "written": [],
           "vm": None, "dropped": []}
    if not rows:
        out["git"] = "nothing new to publish"
        return out

    if vm is not None:
        report = vm_check(rows, repo, Path(plan["staging"]), vm, token)
        out["vm"] = report
        shipping = set(report.shipping)
        out["dropped"] = [o.id for o in report.outcomes.values()
                          if not o.ships]
        out["uploaded"] = sum(1 for o in report.outcomes.values() if o.uploaded)
        rows = [r for r in rows if r["id"] in shipping]
        out["verified"] = bool(rows)
        if not rows:
            out["git"] = ("nothing was verified on a VM, so no task file was "
                          "written")
            return out
    else:
        upload_assets(rows, repo, Path(plan["staging"]), token)
        out["uploaded"] = len(rows)

        problems = verify_fetchable(rows, repo, token)
        if problems:
            raise PublishError(
                "the materials did not come back from the dataset, so no task "
                "file was written — a `.py` without its materials hands the "
                "agent an empty machine and records a zero it did not earn:\n  "
                + "\n  ".join(problems[:10]))
        out["verified"] = True

    save_registry(rollout, plan["_registry"])
    written = place_task_files(rows, rollout)
    written.append(str(registry_path(rollout).relative_to(rollout)))
    written.extend(str(path.relative_to(rollout))
                   for path in refresh_task_lists(rollout))
    ids = ", ".join(r["id"] for r in rows[:6])
    more = "" if len(rows) <= 6 else f" and {len(rows) - 6} more"
    out["written"] = written
    out["git"] = commit_and_push(
        rollout, written,
        f"feat: {len(rows)} pptxgym WPS deck-repair task(s) — {ids}{more}",
        push=push_git)
    return out


def render(plan: dict) -> str:
    lines = [f"work      {plan['work']}",
             f"staging   {plan['staging']}",
             f"git       {plan['rollout']}",
             f"dataset   {plan['repo']}",
             f"registry  {plan['registry']}  next {plan['registry_next']}"]
    for n in plan["notes"]:
        lines.append(f"    · {n}")
    lines.append("")
    lines.append(f"{len(plan['rows'])} task(s) to publish"
                 + (f", {len(plan['already'])} already in the repository "
                    f"({', '.join(plan['already'])})" if plan["already"] else ""))
    for r in plan["rows"]:
        lines.append(f"  task_{r['id']}  <- {r['deck']}  ({r['key']})  "
                     f"{r['components']} components")
        lines.append(f"      hf   {r['hf_dir']}/  {len(r['hf_files'])} files, "
                     f"{r['bytes_hf'] / 1e6:.1f} MB")
        lines.append(f"      git  {TASK_CLASS_REL}/task_{r['id']}.py + "
                     f"{len(r['git_files'])} files, "
                     f"{r['bytes_git'] / 1e6:.1f} MB")
    lines.append("")
    lines.append(f"hugging face  {plan['hf_files']} files, "
                 f"{plan['hf_bytes'] / 1e6:.1f} MB, "
                 f"{plan['hf_commits']} commit(s)")
    lines.append(f"git           {plan['git_files']} files, "
                 f"{plan['git_bytes'] / 1e6:.1f} MB, 1 commit")
    if plan["allocated"]:
        lines.append(f"newly allocated ids: {', '.join(plan['allocated'])}")
    if plan["no_source_record"]:
        lines.append(f"no source/licence record for {len(plan['no_source_record'])} "
                     f"task(s): {', '.join(plan['no_source_record'])}")
    if plan["refused"]:
        lines.append(f"refused {len(plan['refused'])}:")
        for r in plan["refused"][:8]:
            lines.append(f"    x {r}")
    if plan["leaks"]:
        lines.append("LEAK GUARD FAILED:")
        for l in plan["leaks"][:8]:
            lines.append(f"    x {l}")
    else:
        lines.append("leak guard    clean")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default="work")
    ap.add_argument("--deck", nargs="*", default=None,
                    help="publish only these deck ids (default: every deck "
                         "whose foreman outcome is `shipped`)")
    ap.add_argument(
        "--recover-packaged", action="store_true",
        help="with an explicit --deck allowlist, publish named packaged tasks "
             "whose terminal foreman record was lost or overwritten")
    ap.add_argument("--rollout", required=True,
                    help="checkout where generated task files are committed")
    ap.add_argument("--repo", required=True,
                    help="Hugging Face dataset that receives task materials")
    ap.add_argument("--stage", default=None,
                    help="where to build the tree (default: a temp dir)")
    ap.add_argument("--push", action="store_true",
                    help="actually upload and commit; without it this is a dry "
                         "run and nothing leaves this machine")
    ap.add_argument("--no-git-push", action="store_true",
                    help="with --push: commit the task files but do not push")
    ap.add_argument("--republish", action="store_true",
                    help="rebuild tasks whose id is already in the repository")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--token", default=None)
    layout = ap.add_argument_group("rollout layout")
    layout.add_argument("--task-class-dir", default=TASK_CLASS_REL)
    layout.add_argument("--task-assets-dir", default=TASK_ASSETS_REL)
    layout.add_argument("--registry", default=REGISTRY_REL)
    layout.add_argument("--task-list", action="append", default=None,
                        help="task-list JSON to refresh; repeat for multiple lists")
    layout.add_argument("--series", default=SERIES)
    layout.add_argument("--series-first", type=int, default=SERIES_FIRST)
    layout.add_argument("--series-last", type=int, default=SERIES_LAST)

    vmg = ap.add_argument_group(
        "the VM check",
        "run each task's own setup() on a real AWS instance, against the URLs "
        "this run just baked in, before its .py is committed. Costs about "
        "$0.005 and 3-4 minutes per task.")
    vmg.add_argument("--aws-verify", action="store_true",
                     help="with --push: replace the URL check with a real "
                          "setup() on a VM")
    vmg.add_argument("--aws-workers", type=int, default=4,
                     help="how many instances at once. A starting point, not "
                          "a measurement: the vCPU quota cannot be read by "
                          "this IAM user, so the pool narrows itself when AWS "
                          "refuses (default 4 = 8 vCPU)")
    vmg.add_argument("--hf-workers", type=int, default=4,
                     help="how many material uploads at once (default 4)")
    vmg.add_argument("--aws-attempts", type=int, default=3,
                     help="tries per task after an infrastructure failure. "
                          "A capacity refusal does not consume one (default 3)")
    vmg.add_argument("--aws-instance-type", default=None)
    vmg.add_argument("--aws-region", default=None)
    vmg.add_argument("--osworld", default=None,
                     help="the OSWorld-V2 checkout that owns the smoke runner")
    vmg.add_argument("--uv", default=None, help="path to `uv`")
    args = ap.parse_args(argv)
    configure_layout(
        task_class_dir=args.task_class_dir,
        task_assets_dir=args.task_assets_dir,
        registry=args.registry,
        task_lists=args.task_list if args.task_list is not None else TASK_LIST_RELS,
        series=args.series,
        series_first=args.series_first,
        series_last=args.series_last)

    import tempfile
    staging = Path(args.stage) if args.stage else Path(tempfile.mkdtemp(
        prefix="pptxgym-publish-"))
    staging.mkdir(parents=True, exist_ok=True)

    try:
        plan = build(Path(args.work), staging, Path(args.rollout), args.repo,
                     republish=args.republish, run_id=args.run_id,
                     only=args.deck,
                     recover_packaged=args.recover_packaged)
    except PublishError as error:
        raise SystemExit(f"nothing to publish: {error}")

    print(render(plan))
    if plan["leaks"]:
        raise SystemExit(2)

    from . import vmsmoke
    vm = None
    if args.aws_verify:
        vm = VmCheck(artefacts=staging / "aws",
                     osworld=args.osworld, uv=args.uv,
                     aws_workers=args.aws_workers,
                     hf_workers=args.hf_workers,
                     attempts=args.aws_attempts,
                     instance_type=args.aws_instance_type,
                     region=args.aws_region,
                     log=lambda line: print(f"    · {line}", flush=True))
        # Asked here, before the upload, because every task would fail
        # identically for a missing `uv` and none of that would be about the
        # tasks.  A dry run asks it too: finding out on the real run that the
        # runner is not there is finding out after the materials are already
        # public.
        try:
            osworld, uv = vm.ready()
        except vmsmoke.SmokeUnavailable as error:
            raise SystemExit(f"the VM check cannot run: {error}")
        print(f"vm check      {vmsmoke.runner_path(osworld)}\n"
              f"              {uv}, {args.aws_workers} instance(s) at once, "
              f"artefacts under {staging / 'aws'}\n"
              f"              materials go up one commit per task rather than "
              f"{plan['hf_commits']} batched: a deck cannot be tested until "
              f"its own files are in the dataset")

    if not args.push:
        print("\ndry run — nothing uploaded, nothing committed. "
              "add --push to publish.")
        if vm is not None:
            print("the VM check runs *after* the upload — a task cannot fetch "
                  "materials that are not in the dataset yet — so a dry run "
                  "checks the runner and stops there.")
        return 0

    problems = rollout_problems(Path(args.rollout))
    if problems:
        raise SystemExit("refusing to publish:\n  " + "\n  ".join(problems))
    try:
        done = publish(plan, token=args.token, push_git=not args.no_git_push,
                       vm=vm)
    except PublishError as error:
        raise SystemExit(str(error))
    print(f"\nmaterials  {done['uploaded']} task(s) uploaded to {plan['repo']}"
          f"{', all fetchable' if done['verified'] else ''}")
    if done["vm"] is not None:
        print(vmsmoke.render(done["vm"]))
    print(f"tasks      {done['git']}")
    # A batch where nothing could be verified is not a successful publish, and
    # a caller that only reads the exit status has to be able to tell.
    return 0 if (done["vm"] is None or done["vm"].shipping) else 3


if __name__ == "__main__":
    raise SystemExit(main())
