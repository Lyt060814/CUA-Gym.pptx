"""Put an approved task where the benchmark can run it — in both places at once.

A packaged task is two artefacts with two homes, and neither is optional:

* the **`.py`** goes to git, into `evaluation_examples/task_class/` of the
  rollout repository.  It is the whole task apart from its materials — the
  embedded scoring runtime, the save contract, the harness attributes.
* the **materials** go to Hugging Face, into `xlangai/recommendation`, under
  `task_<id>/`.  They are 2-13 MB a task; the thirteen already in git are
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
`pptxgym.vmsmoke`.

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

    python3 -m pptxgym.publish --rollout ../osworld2.0-rollout      # dry run
    python3 -m pptxgym.publish --rollout ../osworld2.0-rollout --push
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

# --------------------------------------------------------------------------- #
# the two destinations
# --------------------------------------------------------------------------- #
#
# Named here rather than left to the caller.  A destination passed on the
# command line every time is a destination that is eventually typo'd into a
# repository nobody meant to create — `create_repo` makes whatever it is given,
# and a public dataset made by accident cannot be unmade.  `--repo` and
# `--rollout` still override; what they no longer do is *require* a fresh
# answer to a question that has one settled answer.

#: The dataset the emitted tasks fetch their materials from.  This is the
#: scaling-phase dataset, and 216 task files already in the rollout repository
#: fetch from it by literal URL.
HF_REPO = "xlangai/recommendation"

#: The git repository the `.py` files live in.
ROLLOUT_REMOTE = "https://github.com/yuanmengqi/osworld2.0-rollout"

TASK_CLASS_REL = "evaluation_examples/task_class"
TASK_ASSETS_REL = "evaluation_examples/task_assets"

#: Where the id registry lives.  See `Registry` for why it is here.
REGISTRY_REL = f"{TASK_ASSETS_REL}/pptxgym-ids.json"

#: Our slice of the id space.  114, 115, 117 and 118 belong to other people;
#: this allocator must never leave 110.
SERIES = "110"
SERIES_FIRST = 1100001
SERIES_LAST = 1109999

#: Hugging Face recommends fewer than a hundred files in a commit.  Budgeting
#: in *tasks* was the wrong unit — a task is one deck plus however many
#: materials it declares, and at eight files a task a "safe" batch of eighteen
#: came to 108.  Count what the limit counts.
FILES_PER_COMMIT = 90

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


class PublishError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #


REGISTRY_NOTE = (
    "Which 110xxxx each pptxgym deck was published as. Keyed by the deck's "
    "content checksum, never by its directory number: a deck number is a local "
    "sequence position that moves when a corpus is re-ingested, and a published "
    "id that moves is worse than an ugly one. This file is the authority on "
    "which numbers are taken; the task files beside it are where the result "
    "goes, not where the next number comes from. Written by pptxgym.publish; "
    "do not edit by hand while a publish is running."
)


def registry_path(rollout: Path) -> Path:
    return Path(rollout) / REGISTRY_REL


def empty_registry() -> dict:
    return {"schema": 1, "series": SERIES, "note": REGISTRY_NOTE,
            "next": SERIES_FIRST, "by_checksum": {}, "reserved": {}}


def load_registry(rollout: Path) -> dict:
    """The registry as it stands, or an empty one.

    **It lives in the rollout repository**, and the alternatives are worse for
    the same reason in two directions.

    Under `work/`: `work/` is scratch.  It is re-ingestible by design and gets
    wiped; a registry there would take the whole series with it, and the next
    run would re-allocate numbers that are already published.

    Under `pptxgym/`: the allocation would then be recorded in a different
    repository from the artefact it names, so a push that succeeds and a
    registry commit that fails leave the two disagreeing, with no way to tell
    which is right.

    In the rollout repository the allocation and the thing it names land in the
    same commit, a second machine that clones the repository inherits the
    allocations, and — the deciding reason — the repository is *already* the
    authority on which numbers are taken: thirteen of them are in it with no
    registry at all.  Putting the file anywhere else would make it a rival
    opinion about a fact the repository already states.  Here it is a cache of
    that fact, and `reconcile` is what keeps it honest.
    """
    f = registry_path(rollout)
    if not f.exists():
        return empty_registry()
    try:
        reg = json.loads(f.read_text())
    except json.JSONDecodeError as error:
        raise PublishError(f"{f} will not parse ({error}); refusing to "
                           f"allocate ids against a registry that cannot be "
                           f"read, because the failure mode is reissuing a "
                           f"number that is already published")
    base = empty_registry()
    base.update({k: v for k, v in reg.items() if k in base})
    return base


def save_registry(rollout: Path, reg: dict) -> Path:
    f = registry_path(rollout)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(reg, ensure_ascii=False, indent=1) + "\n")
    return f


def ids_in_repo(rollout: Path) -> dict[str, Path]:
    """Every `110xxxx` the rollout repository already holds, by id."""
    d = Path(rollout) / TASK_CLASS_REL
    out = {}
    if not d.is_dir():
        return out
    for f in sorted(d.glob(f"task_{SERIES}????.py")):
        tid = f.stem[len("task_"):]
        if tid.isdigit() and SERIES_FIRST <= int(tid) <= SERIES_LAST:
            out[tid] = f
    return out


def _shipped_source_key(rollout: Path, tid: str, work: Path) -> tuple[str, str]:
    """Which deck a task already in the repository came from: `(key, how)`.

    Used to *describe an occupant*, never to adopt one.  A number in the
    repository that the registry does not know about was put there by
    something other than this path, and the answer to that is to refuse and
    say so; guessing an owner for it would let this script overwrite work it
    did not write.

    Two routes, in descending order of what they are worth:

      * **its own provenance record** — `source_key` is the deck's content
        checksum, written by the emitter.  Exact, and needs nothing else.
      * **its name plus a digest that agrees** — packages emitted before that
        record exists say `source_deck: deck0002` in `metadata.json`, and a
        deck number is not an identity.  It is only believed when the deck of
        that name is still in `work/` *and* the `INIT_SHA256` baked into the
        shipped `.py` is that deck's current `bundle/input.pptx`, which is
        content evidence rather than an assumption that the numbering has not
        moved.

    Neither route firing is a normal answer, and the reason is returned so the
    refusal can say which of the two it was.
    """
    adir = Path(rollout) / TASK_ASSETS_REL / f"task_{tid}"
    prov = adir / "provenance.json"
    if prov.exists():
        try:
            key = (json.loads(prov.read_text()) or {}).get("source_key")
        except json.JSONDecodeError:
            key = None
        if key:
            return str(key), "its own provenance record"

    meta = adir / "metadata.json"
    deck_id = None
    if meta.exists():
        try:
            deck_id = (json.loads(meta.read_text()) or {}).get("source_deck")
        except json.JSONDecodeError:
            deck_id = None
    if not deck_id or not str(deck_id).startswith("deck"):
        return "", "nothing in the package names a pptxgym deck"

    deck_root = Path(work) / str(deck_id)
    bundle = deck_root / "bundle" / "input.pptx"
    py = Path(rollout) / TASK_CLASS_REL / f"task_{tid}.py"
    if not bundle.exists():
        return "", f"names {deck_id}, which is not in {work} to check against"
    want = _init_sha_of(py)
    if not want:
        return "", f"names {deck_id} but declares no INIT_SHA256 to check"
    if _sha256(bundle) != want:
        return "", (f"names {deck_id}, but that deck's bundle is no longer the "
                    f"deck this task ships — it has been rebuilt since")
    from . import pipeline as pl
    return pl.task_id_for(pl.Deck(deck_root)), f"{deck_id}, digest agrees"


def _init_sha_of(py: Path) -> str:
    if not py.exists():
        return ""
    m = re.search(r"^INIT_SHA256 = ['\"]([0-9a-f]{64})['\"]", py.read_text(),
                  re.M)
    return m.group(1) if m else ""


def survey_repo(reg: dict, rollout: Path, work: Path) -> tuple[list[str],
                                                               dict[str, str]]:
    """What the repository holds in our series, and who this path thinks owns it.

    Returns `(notes, occupants)` — `occupants` maps id to the checksum this
    path believes is behind it, `""` when it cannot tell.  Nothing here changes
    the registry, and that is the whole design:

    **The registry decides the next number; the repository never does.**  The
    obvious allocator is `max(files in task_class/) + 1`, and it is wrong in
    both directions.  Thirteen tasks in this series were published and are
    being deleted as unfit; while the deletion is in flight the maximum says
    1100013, and after it lands the directory is empty and the maximum says
    nothing at all — so the same registry would hand out different numbers
    depending on when it was run, and a deck's published id would move.  A
    number is taken when it has been *allocated*, and a file being deleted does
    not un-allocate it.

    What the repository is still asked is a different question: whether the
    number we are about to write is already occupied by something this path
    did not put there.  `conflicts` answers that; this only gathers the facts.
    """
    notes, occupants = [], {}
    by_csum = reg.get("by_checksum") or {}
    known = {rec["id"]: key for key, rec in by_csum.items()}
    for tid in sorted(ids_in_repo(rollout)):
        key, how = _shipped_source_key(rollout, tid, work)
        occupants[tid] = key
        if tid in known:
            continue
        notes.append(f"task_{tid} is in the repository but not in the "
                     f"registry — {how}")
    pending = sorted(tid for tid in known if tid not in occupants)
    for tid in pending:
        notes.append(f"task_{tid} is allocated to {known[tid]} but is not in "
                     f"the repository: either its push never landed, or it was "
                     f"published and has since been deleted. Either way the "
                     f"number stays with that deck and is not handed out again")
    return notes, occupants


def registered_ids(reg: dict) -> dict[str, str]:
    """`{id: checksum}` for everything the registry already knew about."""
    return {rec["id"]: key
            for key, rec in (reg.get("by_checksum") or {}).items()}


def conflicts(mapping: dict[str, str], occupants: dict[str, str],
              known_before: dict[str, str]) -> list[str]:
    """Numbers this batch wants that something else already holds.

    Only the numbers *this batch wants* are checked.  A repository full of
    other people's tasks in our series is a thing to report, but it does not
    stop a publish that does not touch them.

    Two shapes, both refusals:

      * the occupant is a task this path published for a different deck — one
        number, two tasks;
      * the occupant cannot be identified at all — it was published by
        something other than this path, and overwriting it is not a decision a
        script gets to take.

    An occupant that is the *same* deck is not a conflict: that is what
    re-publishing looks like, and `build` skips or rebuilds it depending on
    `--republish`.

    `known_before` is the registry as it stood *before* this run allocated
    anything.  Taken after, every number in the batch is in it by construction
    and the third case below could never fire — which is the case that actually
    happens, so it is the one that must not be papered over.
    """
    out = []
    known = known_before
    for key, tid in sorted(mapping.items(), key=lambda kv: kv[1]):
        if tid not in occupants:
            continue
        held = occupants[tid]
        if held == key:
            continue
        if held:
            out.append(
                f"task_{tid} is wanted for checksum {key} but the repository "
                f"already holds a task built from {held} — one number, two "
                f"tasks, and only a human can say which one keeps it")
        elif tid in known:
            out.append(
                f"task_{tid} is registered to {known[tid]} and occupied by a "
                f"file that cannot say which deck it came from")
        else:
            out.append(
                f"task_{tid} is wanted for checksum {key} but the repository "
                f"already holds a task_{tid}.py this path did not publish — "
                f"refusing to overwrite it")
    return out


def allocate(reg: dict, keys: list[str]) -> tuple[dict, dict[str, str],
                                                  list[str]]:
    """Give every checksum a published number, in one pass.

    *One pass* is the point, and it is why this is not a per-deck call.  A
    hundred decks finishing at once would otherwise each read the counter,
    each see the same value, and each take it; the registry is written once,
    from one process, after every number in the batch is decided.

    *Idempotent*: a checksum that already has a number gets the same number
    back.  Publishing the same deck twice is a normal thing to do — a fixed
    evaluator, a corrected instruction — and it must not burn an id, because
    an id that moves breaks every rollout result recorded against the old one.

    *From the registry alone*: `next` comes out of this file and goes back into
    it, and the repository is not consulted.  A published task that is later
    deleted leaves a hole in the repository and no hole here, which is correct
    — the number was spent, and re-spending it would give two different tasks
    the same name in every result table that already mentions the first.

    Returns `(registry, {checksum: id}, newly allocated ids)`.
    """
    reg = json.loads(json.dumps(reg))
    by_csum = reg.setdefault("by_checksum", {})
    reserved = reg.setdefault("reserved", {})
    nxt = int(reg.get("next") or SERIES_FIRST)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    mapping, fresh = {}, []
    for key in keys:                        # caller's order, so runs replay
        rec = by_csum.get(key)
        if rec:
            mapping[key] = rec["id"]
            continue
        while str(nxt) in reserved or str(nxt) in {r["id"] for r
                                                   in by_csum.values()}:
            nxt += 1
        if nxt > SERIES_LAST:
            raise PublishError(
                f"the {SERIES} series is full at {SERIES_LAST}; the next task "
                f"needs a series of its own, and choosing one is not this "
                f"script's decision")
        tid = str(nxt)
        by_csum[key] = {"id": tid, "deck": None, "allocated_at": now}
        mapping[key] = tid
        fresh.append(tid)
        nxt += 1
    reg["next"] = nxt
    return reg, mapping, fresh


# --------------------------------------------------------------------------- #
# what may be published
# --------------------------------------------------------------------------- #


def approved(work: Path, only: list[str] | None = None) -> tuple[list, list[str]]:
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

    `only` narrows the batch to named deck ids. Publishing is still a batch
    step; what this admits is that approval is *per deck* — a batch where one
    deck has been audited and its siblings are still being argued over should
    not have to choose between shipping the unaudited and shipping nothing.
    """
    from . import pipeline as pl

    out, refused = [], []
    for deck in pl.decks_in(Path(work)):
        if only and deck.id not in only:
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


def provenance_of(deck) -> dict | None:
    """Where the source deck came from, or None if the deck cannot say.

    Attribution is a licence condition, not a nicety.  It is *recorded and
    reported* here rather than enforced, and that is a change from when this
    file published a standalone research dataset: the destination is now the
    benchmark's own asset store, the ten pilot decks were mined from GitHub and
    legitimately have no `provenance.json`, and a hard refusal would publish
    nothing at all.  What must not happen is the question disappearing — every
    task without a source record is counted in the summary and named in the
    plan, so the decision to ship one is taken by a person and not by silence.
    """
    f = deck.root / "provenance.json"
    if not f.exists():
        return None
    try:
        p = json.loads(f.read_text())
    except json.JSONDecodeError:
        return None
    return p if isinstance(p, dict) else None


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

    m = re.search(r"zenodo\.(\d+)", doi or "")
    if not m:
        return []
    try:
        with urllib.request.urlopen(ZENODO_API + m.group(1), timeout=30) as r:
            rec = json.load(r)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return []
    names = [c.get("name", "") for c in
             (rec.get("metadata") or {}).get("creators") or []]
    names = [n for n in names if n]
    store[doi] = names
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(store, ensure_ascii=False, indent=1))
    return names


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
          cache: Path | None = None) -> tuple[list[dict], list[str]]:
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
    from . import emit

    rows, refused = [], []
    cache = cache or (Path(work) / ".zenodo-cache.json")
    for deck in decks:
        from . import pipeline as pl
        key = pl.task_id_for(deck)
        tid = mapping[key]
        try:
            out = emit.emit(deck, staging, tid, run_id=run_id)
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


def chunk_by_files(rows: list[dict]) -> list[list[dict]]:
    """Group tasks into Hugging Face commits by file count, greedily.

    A single task larger than the budget still gets its own commit rather than
    being dropped — exceeding the recommendation is survivable, omitting a
    finished task is not.
    """
    out, cur, n = [], [], 0
    for r in rows:
        size = len(r["hf_files"])
        if cur and n + size > FILES_PER_COMMIT:
            out.append(cur)
            cur, n = [], 0
        cur.append(r)
        n += size
    if cur:
        out.append(cur)
    return out


# --------------------------------------------------------------------------- #
# Hugging Face
# --------------------------------------------------------------------------- #


def hf_url(repo: str, hf_dir: str, rel: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{hf_dir}/{rel}"


def upload_assets(rows: list[dict], repo: str, staging: Path,
                  token: str | None = None) -> None:
    """Put every task's materials in the dataset, in a handful of commits.

    `upload_folder` is resumable, splits large payloads itself and skips files
    whose contents are already there, which is what makes re-running safe.
    `upload_large_folder` is deprecated.  The chunking here is about commit
    *size* rather than total volume: the guidance is fewer than a hundred files
    in a commit, and a task is about five.
    """
    from huggingface_hub import HfApi

    api = HfApi(token=token)
    api.create_repo(repo, repo_type="dataset", exist_ok=True)
    tree = staging / "hf"
    chunks = chunk_by_files(rows)
    for n, chunk in enumerate(chunks, 1):
        api.upload_folder(
            repo_id=repo, repo_type="dataset", folder_path=str(tree),
            allow_patterns=[f"{r['hf_dir']}/**" for r in chunk],
            commit_message=f"pptxgym materials {n}/{len(chunks)} "
                           f"({len(chunk)} tasks)")


def prepare_repo(repo: str, token: str | None = None) -> None:
    """Make sure the dataset exists, once, before any per-task upload.

    `upload_assets` folds this into its own call because it is one call.  The
    per-task path is many, and `create_repo` on every one of them would be N
    round trips to say the same thing.
    """
    from huggingface_hub import HfApi

    HfApi(token=token).create_repo(repo, repo_type="dataset", exist_ok=True)


def upload_one(row: dict, repo: str, staging: Path,
               token: str | None = None) -> None:
    """One task's materials, in one commit of its own.

    A commit per task is more commits than `chunk_by_files` would make, and
    that is the price of the VM check: a deck cannot be smoke-tested until
    *its* materials are actually in the dataset, and a batched commit makes
    every deck wait for the slowest upload in its chunk.  The trade is paid
    only when `--aws-verify` is on; the default path still batches.

    `upload_folder` skips files whose contents are already there, so re-running
    after a partial batch costs a listing rather than a re-upload.
    """
    from huggingface_hub import HfApi

    HfApi(token=token).upload_folder(
        repo_id=repo, repo_type="dataset",
        folder_path=str(Path(staging) / "hf"),
        allow_patterns=[f"{row['hf_dir']}/**"],
        commit_message=f"pptxgym materials for task_{row['id']}")


def build_hf_tree(rows: list[dict], staging: Path) -> Path:
    """The exact tree that will be uploaded, laid out as the dataset sees it.

    Built on disk rather than uploaded file by file so that a dry run shows the
    thing itself: `--stage` keeps it, and what a reviewer walks is byte for
    byte what a real run would push.
    """
    tree = staging / "hf"
    for row in rows:
        for src, rel in row["hf_files"]:
            dest = tree / row["hf_dir"] / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    return tree


def verify_fetchable(rows: list[dict], repo: str,
                     token: str | None = None) -> list[str]:
    """Check every URL the tasks will fetch actually resolves, before git.

    This is the hinge of "both or neither".  An upload that reported success
    and a dataset that serves the file are not the same claim — a repository
    can be private, a path can be case-folded, an LFS pointer can be committed
    instead of its content — and the difference is invisible until an agent is
    sitting in front of a machine with no deck on it.  Asking the same URL the
    task will ask, from outside, is the only check that answers the question
    that matters.

    Returns the problems; empty means every task's materials are reachable.
    """
    problems = []
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for row in rows:
        for repo_path, _vm, _sha in row["fetch"]:
            url = f"https://huggingface.co/datasets/{repo}/resolve/main/{repo_path}"
            req = urllib.request.Request(url, method="HEAD", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    if r.status >= 400:
                        problems.append(f"{row['id']}: {url} -> {r.status}")
            except urllib.error.HTTPError as error:
                problems.append(f"{row['id']}: {url} -> HTTP {error.code}")
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                problems.append(f"{row['id']}: {url} -> {error}")
    return problems


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
    r = subprocess.run(["git", "-C", str(rollout), *args],
                       capture_output=True, text=True)
    if check and r.returncode:
        raise PublishError(f"git {' '.join(args)} failed: "
                           f"{(r.stderr or r.stdout).strip()}")
    return r.stdout


def rollout_problems(rollout: Path) -> list[str]:
    """Reasons this checkout is not fit to be published into.

    A dirty tree is the one that matters.  `git commit` on a repository with
    somebody else's uncommitted work in it sweeps that work into our commit if
    the paths overlap, and leaves it stranded if they do not; either way the
    commit no longer says what it claims to.
    """
    out = []
    rollout = Path(rollout)
    if not (rollout / ".git").exists():
        return [f"{rollout} is not a git checkout"]
    if not (rollout / TASK_CLASS_REL).is_dir():
        out.append(f"{rollout} has no {TASK_CLASS_REL}/ — this is not the "
                   f"rollout repository")
    dirty = _git(rollout, "status", "--porcelain").strip()
    if dirty:
        out.append(f"{rollout} has uncommitted changes ({len(dirty.splitlines())} "
                   f"path(s)); publishing would sweep them into our commit")
    return out


def place_task_files(rows: list[dict], rollout: Path) -> list[str]:
    """Copy the git half of every package into the checkout. Returns the paths."""
    written = []
    for row in rows:
        py_dest = Path(rollout) / TASK_CLASS_REL / f"task_{row['id']}.py"
        py_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(row["py"], py_dest)
        written.append(str(py_dest.relative_to(rollout)))
        for src, rel in row["git_files"]:
            dest = Path(rollout) / TASK_ASSETS_REL / f"task_{row['id']}" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            written.append(str(dest.relative_to(rollout)))
    return written


def commit_and_push(rollout: Path, paths: list[str], message: str,
                    *, push: bool = True) -> str:
    """One commit for the batch, and nothing if there is nothing to commit.

    `git commit` with no staged change fails, and a publish run that finds
    everything already published is a *success* — it is what re-running looks
    like.  So emptiness is checked and reported rather than raised.
    """
    _git(rollout, "add", "--", *paths)
    if not _git(rollout, "diff", "--cached", "--name-only").strip():
        return "nothing to commit — the repository already holds these files"
    _git(rollout, "commit", "-m", message)
    head = _git(rollout, "rev-parse", "--short", "HEAD").strip()
    if push:
        branch = _git(rollout, "rev-parse", "--abbrev-ref", "HEAD").strip()
        # The rollout repository is shared: other task families push to it
        # between our clone and our push, and a rejected non-fast-forward
        # once stranded fifty-nine tasks' materials on the hub with no git
        # half. Our files are ours alone (task_class/task_11*, our asset
        # dirs, the id registry), so a rebase onto whatever landed is
        # conflict-free by construction; three rounds outlasts any burst.
        for attempt in range(3):
            try:
                _git(rollout, "push", "origin", branch)
                break
            except Exception:
                if attempt == 2:
                    raise
                _git(rollout, "pull", "--rebase", "origin", branch)
        head = _git(rollout, "rev-parse", "--short", "HEAD").strip()
        return f"committed {head} and pushed to origin/{branch}"
    return f"committed {head}, not pushed"


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


def build(work: Path, staging: Path, rollout: Path, repo: str, *,
          republish: bool = False, run_id: str | None = None,
          only: list[str] | None = None) -> dict:
    """Everything a publish would do, decided but not done."""
    work, staging, rollout = Path(work), Path(staging), Path(rollout)
    decks, refused = approved(work, only=only)
    if not decks:
        raise PublishError(
            f"no deck in {work} has reached `packaged` — publishing is a batch "
            f"step over approved tasks and there are none"
            + (f" ({refused[0]})" if refused else ""))

    from . import pipeline as pl
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

    reg = load_registry(rollout)
    known_before = registered_ids(reg)
    notes, occupants = survey_repo(reg, rollout, work)
    reg, mapping, fresh = allocate(reg, keys)
    clashes = conflicts(mapping, occupants, known_before)
    if clashes:
        raise PublishError(
            "the numbers this batch was allocated are not free in "
            f"{rollout}:\n  " + "\n  ".join(clashes))

    rows, more_refused = stage(work, staging, mapping, decks, run_id=run_id)
    refused += more_refused

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
             f"git       {plan['rollout']}  ({ROLLOUT_REMOTE})",
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
                         "at `packaged`)")
    ap.add_argument("--rollout", required=True,
                    help=f"a checkout of {ROLLOUT_REMOTE}")
    ap.add_argument("--repo", default=HF_REPO,
                    help=f"the dataset the materials go to (default {HF_REPO})")
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

    import tempfile
    staging = Path(args.stage) if args.stage else Path(tempfile.mkdtemp(
        prefix="pptxgym-publish-"))
    staging.mkdir(parents=True, exist_ok=True)

    try:
        plan = build(Path(args.work), staging, Path(args.rollout), args.repo,
                     republish=args.republish, run_id=args.run_id,
                     only=args.deck)
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
