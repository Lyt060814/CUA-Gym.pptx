"""Per-deck state machine.

Every stage reads a directory and writes a directory.  Nothing is carried in a
conversation, which is what makes the run resumable, inspectable, and equally
runnable by a person or by a headless agent.

    ingested -> inspected -> proposed -> recipe -> degraded
             -> materialised -> reconciled

`materialised` produces the files the instruction promises; `reconciled` is the
last judgement gate, checking that the broken file, the instruction and the
assets still describe each other.

Stages after that (reward authoring, verification, packaging) are deliberately
absent: they exist as code elsewhere but have not been through a batch run yet,
and a stage that has never been exercised does not belong in a pipeline that
other people are meant to trust.

A deck directory:

    work/deck0001/
      meta.json        where the source came from, how many slides
      source.pptx      the untouched deck — also the ground truth
      digest.json      structural digest for the proposer
      digest.min.json  same, compact, for an agent's context
      renders/p-NN.png one per slide
      proposal.json    what tasks this deck should yield  (agent)
      recipe.json      how to actually break it           (agent)
      input.pptx       the broken file
      delta.json       every change, with its prior value
      assets/          what the solver gets besides the broken file
      task.json        the final record: instruction, assets, verdict (agent)
      bundle/          the deliverable: input.pptx, instruction.md, assets/
      bundle.json      what the bundle holds and which inputs it was built from
      state.json       stage -> {status, at, detail}

And, one level up, three files that belong to the batch rather than to any one
deck.  A corpus of ten hand-picked decks needed none of them; ten thousand real
uploads cannot be registered without them:

    work/
      rejects.jsonl    every file that could not be registered, and why
      .by-content/     source checksum -> the deck holding it (deduplication)
      .next-deck-id    the id allocator's counter
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

STAGES = ["ingested", "inspected", "proposed", "recipe", "degraded",
          "materialised", "reconciled", "solvable"]
AGENT_STAGES = {"proposed", "recipe", "reconciled", "solvable"}

# What each stage read to reach its verdict.  A stage is `ok` only while these
# are the same bytes it saw: `invalidate_from` handles the repair path, but any
# other way of touching an upstream file — a hand re-run, a fixed executor, an
# edited recipe — used to leave every downstream tick standing, and a stale
# tick is worse than a missing one because it claims the deck was judged.
STAGE_INPUTS = {
    "inspected": ["source.pptx"],
    "proposed": ["digest.json"],
    "recipe": ["proposal.json", "digest.json"],
    "degraded": ["recipe.json", "source.pptx"],
    "materialised": ["proposal.json", "delta.json"],
    "reconciled": ["input.pptx", "delta.json", "assets/manifest.json"],
    "solvable": ["input.pptx", "task.json", "assets/manifest.json"],
}

# stages whose run may continue from something other than a clean `ok`
PROMOTES = {"materialised": ("ok", "partial")}

# The verdicts that let a deck through.  Everything else — `needs_rework`,
# `leaked`, `ambiguous`, `overdetermined`, and `undetermined` — sends it back.
# `undetermined` used to pass, which reads the wrong way round: it means the
# probe could not decide, and an undecided gate is not a passed gate.
PASSING_VERDICTS = {"ready", "solvable"}

# what the solvability probe is not allowed to open: all of it is the answer
FORBIDDEN_TO_PROBE = ("source.pptx", "delta.json", "recipe.json",
                      "proposal.json")
MAX_REPAIRS = 3

# what a repair to each stage invalidates, in order
DOWNSTREAM = {
    "proposed": ["recipe", "degraded", "materialised", "reconciled", "solvable"],
    "recipe": ["degraded", "materialised", "reconciled", "solvable"],
    "materialise": ["materialised", "reconciled", "solvable"],
    "materialised": ["materialised", "reconciled", "solvable"],
}


class StageError(RuntimeError):
    pass


_DIGESTS: dict[tuple, str] = {}


def _digest(path: Path) -> str:
    """Content hash, memoised on (size, mtime) so a status table is cheap."""
    import hashlib
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    hit = _DIGESTS.get(key)
    if hit:
        return hit
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    _DIGESTS[key] = out = h.hexdigest()[:16]
    return out


@dataclass
class Deck:
    root: Path

    @property
    def id(self) -> str:
        return self.root.name

    # -- paths -------------------------------------------------------------- #
    @property
    def source(self):
        return self.root / "source.pptx"

    @property
    def digest(self):
        return self.root / "digest.json"

    @property
    def digest_min(self):
        return self.root / "digest.min.json"

    @property
    def renders(self):
        return self.root / "renders"

    @property
    def proposal(self):
        return self.root / "proposal.json"

    @property
    def recipe(self):
        return self.root / "recipe.json"

    @property
    def input_pptx(self):
        return self.root / "input.pptx"

    @property
    def delta(self):
        return self.root / "delta.json"

    # -- state -------------------------------------------------------------- #
    def state(self) -> dict:
        f = self.root / "state.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def fingerprint(self, stage: str) -> dict:
        """Digest of everything `stage` reads, as it stands right now."""
        out = {}
        for rel in STAGE_INPUTS.get(stage, []):
            p = self.root / rel
            out[rel] = _digest(p) if p.exists() else None
        return out

    def mark(self, stage: str, status: str, **detail):
        st = self.state()
        st[stage] = {"status": status, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     **detail, "_in": self.fingerprint(stage)}
        (self.root / "state.json").write_text(
            json.dumps(st, ensure_ascii=False, indent=1))

    def stale(self, stage: str) -> list[str]:
        """Inputs that have changed since the stage last ran.

        Staleness is inherited.  A changed `recipe.json` only moves the files
        `degraded` reads; `reconciled` still sees the same `input.pptx` until
        the degrade is re-run, and would otherwise keep its tick while sitting
        on top of a stage everyone can see is out of date.
        """
        st = self.state()
        rec = st.get(stage, {})
        was = rec.get("_in")
        if was is None:                     # ran before fingerprints existed
            return []
        now = self.fingerprint(stage)
        out = [k for k in set(was) | set(now) if was.get(k) != now.get(k)]
        for up in STAGES[:STAGES.index(stage)]:
            if st.get(up, {}).get("status") in ("ok", "partial") and self.stale(up):
                out.append(f"<{up}>")
        return sorted(out)

    def status_of(self, stage: str) -> str | None:
        """The recorded status, downgraded to "stale" if its inputs moved."""
        s = self.state().get(stage, {}).get("status")
        if s in ("ok", "partial") and self.stale(stage):
            return "stale"
        return s

    def done(self, stage: str) -> bool:
        return self.status_of(stage) == "ok"

    def promoted(self, stage: str) -> bool:
        """May the run move past this stage?

        Not the same question as `done`.  `materialised` is designed to end at
        `partial` — an asset the proposal promised and the deck cannot supply
        is a mismatch for `reconciled` to judge, not a reason to stop.  Driving
        the run off `done` alone re-ran those decks and then parked them as
        `needs_human` for reaching exactly the state they were meant to reach.
        """
        return self.status_of(stage) in PROMOTES.get(stage, ("ok",))

    def stage_now(self) -> str:
        """The furthest stage completed, or "" if nothing has run."""
        last = ""
        for s in STAGES:
            if self.done(s):
                last = s
            else:
                break
        return last

    def meta(self) -> dict:
        f = self.root / "meta.json"
        return json.loads(f.read_text()) if f.exists() else {}


# --------------------------------------------------------------------------- #
# registration
#
# Ingestion is the one stage that meets the corpus as it really is.  Ten decks
# were hand-picked and every one of them opened; 10,448 conference uploads are
# not, and among them are truncated zips, `.ppt` files renamed `.pptx`,
# password-protected packages and decks python-pptx simply refuses.  Two
# properties follow from that, and neither was needed at ten:
#
#   * a file that cannot be registered is a normal outcome, not an error.  It
#     is written down with its reason and the batch continues.
#   * ids are allocated without reading the work directory, and two processes
#     ingesting at once cannot be handed the same one.
# --------------------------------------------------------------------------- #

CLAIMS = ".by-content"        # work/.by-content/<hash> -> the deck that holds it
NEXT_ID = ".next-deck-id"     # allocator hint; correctness does not depend on it
REJECTS = "rejects.jsonl"     # append-only: every file the corpus would not give up


def _write_atomic(path: Path, text: str):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _alloc_deck_id(work: Path) -> str:
    """Take the next free `deckNNNN` and create its directory, atomically.

    The name stays sequential and stays four digits.  It is not an identity —
    `publish.task_id_for` already refuses to build one out of it, because a
    deck number is a local sequence position that moves when a corpus is
    re-ingested in a different order — it is a directory name, and `deck0007`
    being readable is worth keeping for the humans and the hundred paths under
    `work/` that assume the shape.  Content identity is handled separately, by
    the claim index below, which is where idempotency belongs.

    Two things are wrong with deriving it from a scan.  It is quadratic:
    `glob("deck[0-9]*")` on every call is ~50M directory entries over a 10k
    corpus, for an answer that changes by one each time.  And it is a
    read-then-write with nothing in between, so two processes read the same
    maximum and the second one copies its source over the first one's.

    The counter file removes the scan; `mkdir` — which fails rather than
    succeeds twice — removes the race.  The counter may lag (a crash, an
    explicit `deck_id`, another process); when it does the loop walks forward
    over the taken names and writes the corrected value back, so being wrong
    costs a few failed `mkdir`s and fixes itself.
    """
    counter = work / NEXT_ID
    try:
        n = int(counter.read_text().strip())
    except (OSError, ValueError):
        n = 0
    if n <= 0:                       # first allocation in this work directory
        n = 1 + max([int(p.name[4:]) for p in work.glob("deck[0-9]*")
                     if p.name[4:].isdigit()] or [0])
    while True:
        deck_id = f"deck{n:04d}"
        try:
            (work / deck_id).mkdir()
        except FileExistsError:
            n += 1
            continue
        try:
            _write_atomic(counter, str(n + 1))
        except OSError:
            pass                     # a hint that failed to save is still only a hint
        return deck_id


def _claims_dir(work: Path) -> Path:
    """`work/.by-content`, seeded from decks that predate it.

    The seed is a one-off full hash of every registered source, which is why it
    happens once and behind a directory that either exists or does not: a fresh
    corpus run pays nothing, and a work directory that already holds decks pays
    it a single time rather than losing them from deduplication forever.
    Built aside and renamed into place so that two processes racing to seed
    cannot leave a half-built index visible.
    """
    d = work / CLAIMS
    if d.exists():
        return d
    tmp = work / f"{CLAIMS}.{os.getpid()}.tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    for p in sorted(work.glob("deck[0-9]*")):
        src = p / "source.pptx"
        if not (src.exists() and (p / "meta.json").exists()):
            continue
        try:
            (tmp / _digest(src)).write_text(p.name)
        except OSError:
            pass
    try:
        os.rename(tmp, d)
    except OSError:                  # somebody else got there first
        shutil.rmtree(tmp, ignore_errors=True)
    return d


def _registered_as(work: Path, key: str) -> str | None:
    """The deck already holding these bytes, if there is one. O(1), no listing."""
    f = _claims_dir(work) / key
    try:
        prior = f.read_text().strip()
    except OSError:
        return None
    # a claim whose deck never finished registering is not a claim
    return prior if (work / prior / "meta.json").exists() else None


def _claim(work: Path, key: str, deck_id: str) -> str | None:
    """Claim these bytes for `deck_id`; return the prior holder if there is one."""
    f = _claims_dir(work) / key
    try:
        fd = os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        prior = _registered_as(work, key)
        if prior:
            return prior
        _write_atomic(f, deck_id)    # stale claim on a deck that never landed
        return None
    except OSError:
        return None
    with os.fdopen(fd, "w") as fh:
        fh.write(deck_id)
    return None


def _release(work: Path, key: str, deck_id: str):
    f = _claims_dir(work) / key
    try:
        if f.read_text().strip() == deck_id:
            f.unlink(missing_ok=True)
    except OSError:
        pass


def register(pptx: Path, work: Path, deck_id: str | None = None) -> tuple[Deck, str]:
    """Register a source deck; say whether it was new.

    Returns `(deck, "registered" | "duplicate")`.  The same bytes ingested
    twice — the same conference deck uploaded under two filenames, or a
    re-run over a corpus directory — return the deck that already holds them,
    untouched, rather than a second copy with a second id and half the
    pipeline's work missing.  That is what makes `ingest` safe to re-run over
    a directory, which at 10k files is not an optional property.

    A registration that fails part-way leaves nothing behind: a directory
    holding a `source.pptx` and no `meta.json` would be picked up by
    `decks_in` and reported by `status` as a deck stuck before its first
    stage, which is a lie about a file that was never a deck.
    """
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    src = Path(pptx)
    key = _digest(src)

    fresh = False
    if deck_id is None:
        prior = _registered_as(work, key)
        if prior:
            return Deck(work / prior), "duplicate"
        deck_id = _alloc_deck_id(work)
        fresh = True
        held_by = _claim(work, key, deck_id)
        if held_by:                  # lost the race by a hair; keep theirs
            try:
                (work / deck_id).rmdir()
            except OSError:
                pass
            return Deck(work / held_by), "duplicate"

    deck = Deck(work / deck_id)
    deck.root.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy(src, deck.source)
        from pptx import Presentation
        prs = Presentation(str(deck.source))
        (deck.root / "meta.json").write_text(json.dumps({
            "id": deck_id, "origin": str(src.resolve()),
            "name": src.name, "slides": len(prs.slides),
            "checksum": key,
            "size_in": [round(prs.slide_width / 914400, 1),
                        round(prs.slide_height / 914400, 1)],
        }, ensure_ascii=False, indent=1))
        deck.mark("ingested", "ok", slides=len(prs.slides))
    except BaseException:
        if fresh:
            _release(work, key, deck_id)
            shutil.rmtree(deck.root, ignore_errors=True)
        raise
    if not fresh:                    # an explicit id still gets to be deduplicated
        _claim(work, key, deck_id)
    return deck, "registered"


def ingest(pptx: Path, work: Path, deck_id: str | None = None) -> Deck:
    """Register a source deck. The source is also the ground truth — nothing
    downstream ever writes to it."""
    return register(pptx, work, deck_id)[0]


# --------------------------------------------------------------------------- #
# what a file can fail to be
# --------------------------------------------------------------------------- #

# Every reason is a shape the Zenodo corpus actually contains.  They are kept
# apart because they mean different things to whoever reads the log: `encrypted`
# and `legacy_ppt` are the corpus being the corpus and nothing can be done about
# them, while a run full of `pptx_error` is a signal about our own toolchain.
REJECT_REASONS = {
    "missing": "the path is not there any more",
    "empty": "zero bytes",
    "unreadable": "the filesystem would not give it up",
    "legacy_ppt": "binary PowerPoint 97–2003 wearing a .pptx name",
    "encrypted": "password-protected package",
    "not_a_zip": "not a zip container at all",
    "corrupt_zip": "a zip that will not open — usually a truncated upload",
    "not_a_deck": "a valid OOXML package, but not a presentation",
    "pptx_error": "python-pptx opened it and refused it",
}

# the OLE2/CFB directory holds stream names as UTF-16LE
_ENCRYPTED_STREAM = "EncryptedPackage".encode("utf-16-le")


def reject_reason(path: Path, exc: BaseException | None = None) -> tuple[str, str]:
    """Why this file is not a deck, in terms someone can act on.

    `str(exception)` is not that: python-pptx says "Package not found at ..."
    for a truncated download and for a `.ppt`, and a thousand-line log of that
    tells you only that a thousand files failed.  The file itself says which.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(1 << 16)
    except FileNotFoundError:
        return "missing", str(path)
    except OSError as e:
        return "unreadable", f"{type(e).__name__}: {e}"[:200]
    detail = f"{type(exc).__name__}: {exc}"[:200] if exc else ""

    if not size:
        return "empty", "zero bytes"
    if head[:4] == b"\xd0\xcf\x11\xe0":
        try:
            blob = head + open(path, "rb").read(1 << 20)
        except OSError:
            blob = head
        if _ENCRYPTED_STREAM in blob:
            return "encrypted", "OLE2 container holding an EncryptedPackage stream"
        return "legacy_ppt", "OLE2 container (PowerPoint 97–2003)"
    if head[:2] != b"PK":
        return "not_a_zip", f"leading bytes {head[:8]!r}"

    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except Exception as e:                                       # noqa: BLE001
        return "corrupt_zip", f"{type(e).__name__}: {e}"[:200]
    if not any(n == "ppt/presentation.xml" for n in names):
        looks = next((k for k in ("word/", "xl/", "visio/")
                      if any(n.startswith(k) for n in names)), None)
        return "not_a_deck", (f"no ppt/presentation.xml"
                              + (f"; looks like {looks.rstrip('/')}" if looks else ""))
    return "pptx_error", detail or "python-pptx refused it"


# --------------------------------------------------------------------------- #
# a batch of them
# --------------------------------------------------------------------------- #


def pptx_files(paths) -> list[Path]:
    """Expand what a caller pointed at into deck files, in a stable order.

    Directories are walked, not globbed one level deep: the corpus arrives
    sorted into subdirectories and a shallow glob quietly ingests none of it.
    Office lock files (`~$deck.pptx`) are not decks and are skipped in silence
    rather than rejected, because a reject log full of them is a reject log
    nobody reads.
    """
    given = [paths] if isinstance(paths, (str, Path)) else list(paths)
    out, seen = [], set()
    for raw in given:
        p = Path(raw)
        found = (sorted(f for f in p.rglob("*")
                        if f.is_file() and f.suffix.lower() == ".pptx")
                 if p.is_dir() else [p])
        for f in found:
            if f.name.startswith("~$") or f.name.startswith("."):
                continue
            r = str(f.resolve()) if f.exists() else str(f)
            if r not in seen:
                seen.add(r)
                out.append(f)
    return out


def record_reject(work: Path, rec: dict) -> Path:
    """Append one rejected file to `work/rejects.jsonl`.

    It lives in `work/` and not beside the corpus because it is a fact about
    this run, not about the source: the same file may be ingestible once the
    toolchain moves.  It is JSONL and append-only for three reasons — a single
    short `write` to a file opened `O_APPEND` does not interleave, so parallel
    ingest workers need no lock; it can be read while it is being written, so a
    six-hour batch is inspectable at minute five; and it grows by a line
    instead of being rewritten, so a crash costs the last record rather than
    all of them.  Nothing here ever raises: failing to write down a failure
    must not become a second failure.
    """
    f = Path(work) / REJECTS
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return f


def rejects(work: Path) -> list[dict]:
    """The rejected files, latest verdict per path."""
    f = Path(work) / REJECTS
    if not f.exists():
        return []
    out: dict[str, dict] = {}
    with open(f, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            out[rec.get("path") or rec.get("name") or line] = rec
    return list(out.values())


def ingest_many(paths, work: Path, progress=None) -> dict:
    """Register everything registrable and write down everything else.

    The loop `cmd_ingest` used to run let the first unreadable file end the
    batch with a traceback, and recorded nothing about which file it was — over
    10,448 uploads that is not an edge case but the guaranteed first five
    minutes.  A batch ends here with a summary and a reject log, always.

    `progress` is called per file so a long run says something while it runs;
    the return value is the same records, for whoever wants them at the end.
    """
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    files = pptx_files(paths)
    out = {"scanned": len(files), "registered": [], "duplicate": [],
           "rejected": [], "rejects_file": str(work / REJECTS)}

    for f in files:
        try:
            deck, how = register(f, work)
        except Exception as e:                                   # noqa: BLE001
            reason, detail = reject_reason(f, e)
            rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "path": str(Path(f).resolve()) if Path(f).exists() else str(f),
                   "name": Path(f).name, "reason": reason,
                   "why": REJECT_REASONS.get(reason, reason), "detail": detail,
                   "bytes": Path(f).stat().st_size if Path(f).exists() else 0}
            record_reject(work, rec)
            out["rejected"].append(rec)
            if progress:
                progress({"event": "rejected", **rec})
            continue
        rec = {"deck": deck.id, "name": Path(f).name, "path": str(f),
               "slides": deck.meta().get("slides")}
        out["duplicate" if how == "duplicate" else "registered"].append(rec)
        if progress:
            progress({"event": how, **rec})
    return out


def _report(deck: Deck, name: str) -> dict | None:
    """One of the round-trip reports beside the deck, or None."""
    f = deck.root / name
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:
        return None


def drift_of(deck: Deck) -> dict:
    """Both round trips as they stand on disk, in the digest's terms.

    Reading, never measuring.  Which renderer governs is decided in
    `deck_digest.renderer_drift`; here we only hand it the two files.
    """
    from . import deck_digest
    return deck_digest.renderer_drift(_report(deck, "roundtrip.json"),
                                      _report(deck, "roundtrip-wps.json"))


def rebuild_digest(deck: Deck) -> bool:
    """Rewrite `digest.json` from what is on disk now, renders untouched.

    The WPS round trip cannot run at ingest (see `inspect`), so it arrives
    after the digest has already been written — and the digest is the only
    place the proposer ever sees it.  Without this the number would sit in
    `roundtrip-wps.json` reaching nobody, and the only way to fold it in would
    be `inspect --force`, which re-renders every slide to change one paragraph.
    """
    if not deck.source.exists():
        return False
    from . import deck_digest
    d = deck_digest.digest(str(deck.source), drift=drift_of(deck))
    deck.digest.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    deck.digest_min.write_text(
        json.dumps(d, ensure_ascii=False, separators=(",", ":")))
    return True


# --------------------------------------------------------------------------- #
# how big the pictures the proposer reads have to be
#
# The renders, not the digest, are the largest thing an agent reads on this
# corpus: 195 PNGs across the ten decks come to ~299k image tokens against
# ~258k for every digest combined, and the DPI producing them had never been
# looked at.  A 16:9 slide at 110 DPI is 1467x825 px = ~1,614 tokens.
#
# The knob only turns down.  Claude downsamples anything past 1568 px on the
# long edge, so above ~117 DPI a 13.3-inch slide buys neither tokens nor
# resolution; 110 already sits at 94% of that ceiling.
#
# And it cannot be turned down flat.  16% of all text runs in this corpus are
# 5.0 pt — deck0006's figure panels — which is 7.6 px tall at 110 DPI, 6.7 at
# 96 and 5.0 at 72, i.e. below where antialiased text survives at all.  The
# proposal skill's own rule is that a value illegible at export DPI is a value
# the solver cannot read, so a flat cut would make the proposer reject anchors
# that are perfectly legible in the deck itself.
#
# Hence per-deck: 96 DPI (a free 24% of the image bill) unless this deck's own
# smallest text needs the extra pixels.
# --------------------------------------------------------------------------- #

RENDER_DPI = 110            # decks with text small enough to need it
RENDER_DPI_COARSE = 96      # everything else, which is most of them
SMALL_TEXT_PT = 8.0         # below this, 96 DPI starts eating the glyphs


def smallest_text_pt(digest: dict | None) -> float | None:
    """The smallest run size the digest can see, or None if it reports none.

    Both levels are consulted.  `deck_summary.font_sizes_pt` is a top-ten, so
    a rare small size can fall off the end of it — but a size that is rare in
    the deck is usually common on the one slide that uses it, and that slide's
    own `typography.sizes_pt` still names it.  Neither list is guaranteed
    exhaustive, which is why the fallback is the *higher* DPI: the cost of
    guessing wrong upward is 24% of one deck's image tokens, and the cost of
    guessing wrong downward is a proposer that cannot read the deck.
    """
    sizes = [sz for sz, _ in
             ((digest or {}).get("deck_summary") or {}).get("font_sizes_pt") or []]
    for s in (digest or {}).get("slides") or []:
        typo = ((s.get("visual_system") or {}).get("typography") or {})
        sizes += [sz for sz, _ in typo.get("sizes_pt") or []]
    sizes = [s for s in sizes if isinstance(s, (int, float)) and s > 0]
    return min(sizes) if sizes else None


def render_dpi_for(digest: dict | None) -> int:
    """The DPI this particular deck's renders need."""
    smallest = smallest_text_pt(digest)
    if smallest is None:
        return RENDER_DPI
    return RENDER_DPI_COARSE if smallest >= SMALL_TEXT_PT else RENDER_DPI


def inspect(deck: Deck, dpi: int | None = None, force: bool = False,
            roundtrip: bool = False) -> dict:
    """Digest + one render per slide. Deterministic; no agent involved.

    `roundtrip` is off by default, and that is a change of policy rather than
    a saving of a few seconds.  The LibreOffice round trip earned its place in
    the hot path when its number set the position tolerances the reward would
    use.  It no longer does: WPS — the application these tasks are solved and
    graded in — was measured at 0.0% drift on all ten pilot decks, while the
    proxy ran 7.6%–61.5% on the same files, essentially all of it textbox and
    table reflow WPS does not reproduce.  What is left is a corpus-fragility
    hint that nothing gates on, bought with a *second* whole soffice document
    conversion per deck on top of the one the renders already pay for.  At ten
    decks that is invisible; at ten thousand it is hours of the ingest budget
    for a field no stage reads.  So it is opt-in — `pptxgym inspect
    --roundtrip`, on a sample of the corpus when the question is asked — and an
    existing `roundtrip.json` is still read whether or not it is asked for.

    `dpi=None` means "let the deck decide" — see `render_dpi_for`.  An explicit
    number still wins, because a person asking for one has a reason.  The
    digest is written before the renders precisely so the choice can be made
    from it: the deck's own smallest text is what decides.
    """
    from . import render

    rt_f = deck.root / "roundtrip.json"
    if roundtrip and (force or not rt_f.exists()):
        from . import roundtrip as rtmod
        try:
            rt_pre = rtmod.check(str(deck.source))
        except Exception as e:                                   # noqa: BLE001
            rt_pre = {"verdict": "unmeasured", "error": str(e)[:160]}
        rt_f.write_text(json.dumps(rt_pre, ensure_ascii=False, indent=1))

    # WPS is what the tasks are solved and graded in, so its round trip — not
    # the proxy's — is what bounds position work.  It has no headless converter
    # on Linux: measuring it drives a GUI at 60–90 s a deck, strictly serial on
    # one virtual display, which would wreck ingestion — so it is NEVER run
    # from here.  It is measured by `pptxgym wps`, which is a batch pass of its
    # own, and picked up from the file if it is there; if it is not, the digest
    # reports it as unmeasured instead of letting the LibreOffice number pass
    # for it.
    rt_now = _report(deck, "roundtrip.json")
    wps_now = _report(deck, "roundtrip-wps.json")
    drift = drift_of(deck)

    if deck.digest.exists() and not force:
        pass
    else:
        from . import deck_digest
        d = deck_digest.digest(str(deck.source), drift=drift)
        deck.digest.write_text(json.dumps(d, ensure_ascii=False, indent=1))
        # the compact copy is what an agent reads: same content, ~half the
        # tokens, and the indented one stays for humans
        deck.digest_min.write_text(
            json.dumps(d, ensure_ascii=False, separators=(",", ":")))

    deck.renders.mkdir(exist_ok=True)
    have = sorted(deck.renders.glob("p-*.png"))
    n_slides = deck.meta().get("slides", 0)
    use_dpi = dpi or render_dpi_for(_report(deck, "digest.json"))
    if force or len(have) < n_slides:
        for f in have:
            f.unlink()
        render.render_pptx(str(deck.source), str(deck.renders), "p", dpi=use_dpi)
        have = sorted(deck.renders.glob("p-*.png"))

    # Recorded, not gated.  The proxy touched 8%–61% of shapes across the ten
    # decks measured so far while WPS touched 0% on every one of them, so the
    # spread is mostly LibreOffice import behaviour rather than deck fragility —
    # any threshold on it today would be a number picked to look decisive.
    rt = rt_now or {}
    detail = {"digest_kb": deck.digest.stat().st_size // 1024,
              "digest_min_kb": deck.digest_min.stat().st_size // 1024,
              "renders": len(have), "dpi": use_dpi,
              "roundtrip": rt.get("verdict") or "not-measured",
              "roundtrip_changed_frac": rt.get("changed_frac"),
              "roundtrip_wps": (wps_now or {}).get("verdict") or "unmeasured"}
    if len(have) < n_slides:
        deck.mark("inspected", "failed", **detail,
                  error=f"rendered {len(have)} of {n_slides} slides")
        raise StageError(f"{deck.id}: rendered {len(have)} of {n_slides} slides")
    deck.mark("inspected", "ok", **detail)
    return detail


def check_proposal(deck: Deck) -> dict:
    """Promote `proposed` only if the file is a usable proposal, not merely
    present.  File-existence as a success test is how a batch run ends up full
    of plausible rubbish."""
    if not deck.proposal.exists():
        raise StageError(f"{deck.id}: no proposal.json")
    try:
        p = json.loads(deck.proposal.read_text())
    except json.JSONDecodeError as e:
        raise StageError(f"{deck.id}: proposal.json is not valid JSON ({e})")

    tasks = p.get("tasks")
    if tasks is None:
        raise StageError(f"{deck.id}: proposal has no `tasks` key")
    if not tasks:
        # an empty proposal is a legitimate answer, but it has to say why
        if not (p.get("no_task_reason") or "").strip():
            raise StageError(f"{deck.id}: empty `tasks` with no no_task_reason")
        return {"tasks": 0, "reason": p["no_task_reason"][:120]}

    n_slides = deck.meta().get("slides", 10 ** 6)
    out = []
    for t in tasks:
        for key in ("name", "difficulty", "est_steps", "instruction",
                    "degradations"):
            if not t.get(key):
                raise StageError(f"{deck.id}: task missing `{key}`")
        if not t["degradations"]:
            raise StageError(f"{deck.id}: task {t['name']} has no degradations")
        for g in t["degradations"]:
            for page in g.get("slides", []):
                if not 1 <= page <= n_slides:
                    raise StageError(
                        f"{deck.id}: {g.get('id')} targets slide {page}, "
                        f"deck has {n_slides}")
        total = sum(g.get("est_steps", 0) for g in t["degradations"])
        band = ("easy" if t["est_steps"] <= 100
                else "medium" if t["est_steps"] <= 300 else "hard")
        if band != t["difficulty"]:
            raise StageError(
                f"{deck.id}: task {t['name']} says {t['difficulty']} but "
                f"{t['est_steps']} steps is {band}")
        _check_disclosure(deck, t)
        out.append({"name": t["name"], "difficulty": t["difficulty"],
                    "est_steps": t["est_steps"], "sum_of_parts": total,
                    "degradations": len(t["degradations"])})
    return {"tasks": len(tasks), "detail": out}


# a degradation's disclosure -> the asset kind that has to be declared for it
DISCLOSURE_ASSET = {
    "reference_image": "reference_image",
    "reference_image_masked": "reference_image",
    "reference_keyframes": "reference_keyframes",
}


def _check_disclosure(deck: Deck, task: dict):
    """Does every degradation actually get the evidence it says it needs?

    The commonest reason a finished task came back rejected: a degradation
    declares `disclosure: reference_image` for slide 12, the `assets` list
    never mentions slide 12, `materialise` produces nothing for it, and the
    solvability probe is the first thing to notice — five stages and two
    agents after the mistake was made.  Four of the first ten decks failed
    this way, all on the same point: the solver was not given enough to pin
    the answer.

    It is mechanical, so it belongs here rather than in the skill's prose,
    where the same requirement already sat as a self-check question and was
    answered "yes" four times out of ten by proposals that had not met it.
    """
    declared = {}
    for a in task.get("assets") or []:
        kind = a.get("kind")
        kind = {"reference_image_masked": "reference_image",
                "picture": "image", "asset_image": "image",
                "csv": "data", "keyframes": "reference_keyframes"}.get(kind, kind)
        declared.setdefault(kind, set()).update(a.get("slides") or [])

    wanted = {}
    for g in task["degradations"]:
        for key in ("anchor", "disclosure", "disclosure_detail"):
            if not (g.get(key) or "").strip():
                raise StageError(
                    f"{deck.id}: {g.get('id')} has no `{key}` — what the "
                    f"solver is told is not an afterthought, it decides "
                    f"whether the task has an answer")
        need = DISCLOSURE_ASSET.get(g["disclosure"])
        if need:
            wanted.setdefault(need, set()).update(g.get("slides") or [])

    for kind, slides in wanted.items():
        have = declared.get(kind, set())
        missing = sorted(slides - have)
        if missing:
            raise StageError(
                f"{deck.id}: a degradation on slide{'s' if len(missing) > 1 else ''} "
                f"{', '.join(map(str, missing))} says the solver gets a "
                f"{kind}, but `assets` never declares one for "
                f"{'those slides' if len(missing) > 1 else 'that slide'} — "
                f"nothing will be produced and the answer stays unpinned")

    # The mirror image, but only the indefensible version of it.  Disclosure
    # layers: a degradation whose primary anchor is elsewhere on the deck may
    # still need a render of its own page for the part that is unique to it,
    # and three good proposals do exactly that with the reason written down.
    # A first version of this check demanded one-to-one agreement and rejected
    # all three, including a deck the probe had passed — it punished precisely
    # the care it was meant to enforce.  What stays is the case with no
    # defence: material for a page nothing was broken on, which is how one
    # deck shipped tables the instruction never mentioned.
    broken = {p for g in task["degradations"] for p in g.get("slides") or []}
    for kind, slides in declared.items():
        stray = sorted(slides - broken)
        if stray:
            raise StageError(
                f"{deck.id}: `assets` declares a {kind} for slide "
                f"{', '.join(map(str, stray))}, where nothing is broken — "
                f"material for an untouched page is not a reference, it is an "
                f"extra copy of the deck handed to the solver")


def degradation_ids(deck: Deck) -> list[str]:
    """The degradation ids this deck's proposal defines, in order."""
    if not deck.proposal.exists():
        raise StageError(f"{deck.id}: no proposal.json — a recipe that cannot "
                         f"be traced to a proposal cannot be scored")
    try:
        p = json.loads(deck.proposal.read_text())
    except json.JSONDecodeError as e:
        raise StageError(f"{deck.id}: proposal.json is not valid JSON ({e})")
    ids = []
    for t in p.get("tasks") or []:
        for g in t.get("degradations") or []:
            gid = g.get("id")
            if gid and gid not in ids:
                ids.append(gid)
    return ids


def check_recipe(deck: Deck) -> dict:
    """The recipe has to name ops that exist, slides that exist, and — for
    every step — the degradation it implements."""
    from . import degrade_exec

    if not deck.recipe.exists():
        raise StageError(f"{deck.id}: no recipe.json")
    r = json.loads(deck.recipe.read_text())
    n_slides = deck.meta().get("slides", 10 ** 6)
    steps = []
    for page, page_steps in (r.get("slides") or {}).items():
        if not str(page).isdigit() or not 1 <= int(page) <= n_slides:
            raise StageError(f"{deck.id}: recipe targets slide {page!r}")
        for i, st in enumerate(page_steps):
            if st.get("op") not in degrade_exec.REGISTRY:
                raise StageError(
                    f"{deck.id}: unknown op {st.get('op')!r} "
                    f"(known: {', '.join(sorted(degrade_exec.REGISTRY))})")
            steps.append((f"slide {page} step {i + 1} ({st['op']})", st))
    for key in ("smartart", "chart"):
        for i, spec in enumerate(r.get(key) or []):
            if not 1 <= spec.get("slide", 0) <= n_slides:
                raise StageError(f"{deck.id}: {key} targets slide "
                                 f"{spec.get('slide')!r}")
            steps.append((f"{key} entry {i + 1} (slide {spec.get('slide')})",
                          spec))
    if not steps:
        raise StageError(f"{deck.id}: recipe does nothing")
    return {"steps": len(steps), "slides": len(r.get("slides") or {}),
            **_check_traceability(deck, steps)}


def _check_traceability(deck: Deck, steps: list[tuple[str, dict]]) -> dict:
    """Does every step name a degradation, and every degradation get a step?

    `deg` is the only mechanical link between the prose a solver is given and
    the changes a file actually took: the executor stamps it onto every delta
    entry the step produces, so `delta.json` says which part of the instruction
    each change belongs to.  Both directions have to hold, and each fails in
    its own way:

      * a step naming nothing, or naming an id the proposal does not define,
        puts a change in the delta that no degradation asked for — the
        evaluator would score work nobody was told to do;
      * a degradation no step implements is a paragraph of the instruction that
        breaks nothing, so it can earn nothing however well it is done.

    Free text cannot stand in for this.  Every one of the first ten recipes
    happened to open its `_why` with the id, and reading that back is a
    convention, not a check — the next writer's "seals the leak from d2/d3"
    matches two ids, and "restores the row" matches none.
    """
    ids = degradation_ids(deck)
    known = ", ".join(ids) or "none"
    named = set()
    for where, st in steps:
        deg = st.get("deg")
        if not deg:
            raise StageError(
                f"{deck.id}: {where} has no `deg` — every step must name the "
                f"degradation it implements (this proposal has {known}), or "
                f"the change it makes cannot be traced back to anything the "
                f"instruction asked for")
        if deg not in ids:
            raise StageError(
                f"{deck.id}: {where} names deg {deg!r}, which this proposal "
                f"does not define (it has {known}) — the delta would carry a "
                f"change no degradation asked for, and the evaluator would "
                f"score work nobody was told to do")
        named.add(deg)
    missing = [i for i in ids if i not in named]
    if missing:
        raise StageError(
            f"{deck.id}: degradation {missing[0]!r} is in the proposal and no "
            f"recipe step implements it — the instruction asks for work that "
            f"breaks nothing, so nothing it produces can be scored"
            + (f" (also {', '.join(missing[1:])})" if len(missing) > 1 else ""))
    return {"degradations": len(ids)}


def degrade(deck: Deck) -> dict:
    """Apply the recipe, then gate the package. A broken or leaky file never
    reaches the next stage."""
    from . import degrade_exec, pkg_check

    recipe = json.loads(deck.recipe.read_text())
    delta = degrade_exec.run(str(deck.source), recipe, str(deck.input_pptx))
    deck.delta.write_text(json.dumps(delta, ensure_ascii=False, indent=1))

    integ = pkg_check.check(str(deck.input_pptx))
    leak = pkg_check.leak_check(str(deck.input_pptx), delta, str(deck.source))
    problems = (integ["problems"] + integ["duplicate_ids"]
                + leak["leaks"] + leak["dead_rels"])
    n = sum(len(v) for v in delta["slides"].values())
    detail = {"changes": n, "slides": len(delta["slides"]),
              "gate": "ok" if not problems else "FAILED",
              "problems": problems[:8]}
    if problems:
        deck.mark("degraded", "failed", **detail)
        raise StageError(f"{deck.id}: package gate failed — {problems[0]}")
    deck.mark("degraded", "ok", **detail)
    return detail


def materialise(deck: Deck) -> dict:
    """Produce the files the task promises. Deterministic.

    Refuses to promote while anything the proposal declared is missing: an
    unmet asset is not a cosmetic gap, it is a task nobody can attempt.
    """
    from . import assets

    m = assets.materialise(deck)
    detail = {"produced": len(m["produced"]), "unmet": len(m["unmet"]),
              "kinds": sorted({p["kind"] for p in m["produced"]})}
    if m["unmet"]:
        detail["problems"] = [f"{u['kind']}: {u['why']}" for u in m["unmet"]][:6]
    if not m["produced"] and m["unmet"]:
        deck.mark("materialised", "failed", **detail)
        raise StageError(f"{deck.id}: no asset could be produced — "
                         f"{m['unmet'][0]['why']}")
    if m["unmet"]:
        # An asset that cannot be produced is usually not a tooling failure but
        # a promise the proposal could not keep — here, chart numbers for a
        # figure that was always a bitmap.  That is a mismatch between the
        # instruction and the deck, which is precisely what `reconciled` judges,
        # so it goes forward marked `partial` rather than dying here.  The
        # reconciler is required to address every unmet asset.
        deck.mark("materialised", "partial", **detail)
        return detail
    deck.mark("materialised", "ok", **detail)
    return detail


def check_reconcile(deck: Deck) -> dict:
    """The reconciler's output has to be a task record that stands on its own."""
    f = deck.root / "task.json"
    if not f.exists():
        raise StageError(f"{deck.id}: no task.json")
    try:
        t = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        raise StageError(f"{deck.id}: task.json is not valid JSON ({e})")

    for key in ("name", "instruction", "difficulty", "est_steps",
                "assets", "instruction_changed", "notes"):
        if key not in t:
            raise StageError(f"{deck.id}: task.json missing `{key}`")
    if not (t["instruction"] or "").strip():
        raise StageError(f"{deck.id}: empty instruction")

    # every file the record hands to the solver has to be on disk
    adir = deck.root / "assets"
    missing = [a for a in t["assets"]
               if a.get("file") and not (adir / a["file"]).exists()]
    if missing:
        raise StageError(f"{deck.id}: task.json lists {missing[0].get('file')!r} "
                         f"but it is not in assets/")
    if t.get("verdict") == "needs_rework":
        rw = t.get("rework")
        if not rw:
            raise StageError(f"{deck.id}: needs_rework with no `rework` list — "
                             f"a verdict nobody can act on is a dead end")
        for r in rw:
            if r.get("stage") not in ("proposed", "recipe", "materialise"):
                raise StageError(
                    f"{deck.id}: rework targets {r.get('stage')!r}; must be one "
                    f"of proposed / recipe / materialise")
            if not (r.get("what") or "").strip():
                raise StageError(f"{deck.id}: rework entry with no `what`")
    if t["instruction_changed"] and not (t.get("notes") or "").strip():
        raise StageError(f"{deck.id}: instruction was changed with no note "
                         f"saying why")

    # anything materialise could not produce has to be dealt with, not ignored:
    # the instruction promised it to the solver
    man_f = deck.root / "assets" / "manifest.json"
    if man_f.exists():
        unmet = json.loads(man_f.read_text()).get("unmet") or []
        if unmet and t.get("verdict") != "needs_rework":
            blob = ((t.get("notes") or "") + " "
                    + " ".join(d.get("note") or "" for d in t.get("degradations") or [])
                    ).lower()
            unaddressed = [u["kind"] for u in unmet
                           if u["kind"].lower() not in blob]
            if unaddressed:
                raise StageError(
                    f"{deck.id}: asset {unaddressed[0]!r} could not be produced "
                    f"and the task record never mentions it")
    return {"assets": len(t["assets"]), "verdict": t.get("verdict"),
            "instruction_changed": bool(t["instruction_changed"]),
            "difficulty": t["difficulty"], "est_steps": t["est_steps"]}


def archive_attempt(deck: Deck, stage: str) -> str | None:
    """Move a stage's artefacts into attempts/ before it runs again.

    Agent logs were opened with "w" and `task.json` was overwritten, so a
    second run destroyed the evidence of the first.  When a repair loop is
    allowed to retry until a gate passes, that is the difference between
    "it was fixed" and "the verdict was laundered" — and afterwards nobody,
    including the pipeline, can tell which happened.
    """
    art = {"proposed": ["proposal.json", "proposed.jsonl"],
           "recipe": ["recipe.json", "recipe.jsonl"],
           "reconciled": ["task.json", "reconciled.jsonl"],
           "degraded": ["delta.json"],
           "solvable": ["solvability.json", "solvable.jsonl"],
           "materialised": []}.get(stage, [])
    live = [f for f in art if (deck.root / f).exists()]
    if not live:
        return None
    n = 1 + len(list((deck.root / "attempts").glob(f"{stage}-*")))
    dest = deck.root / "attempts" / f"{stage}-{n:02d}"
    dest.mkdir(parents=True, exist_ok=True)
    for f in live:
        shutil.copy2(deck.root / f, dest / f)
    prev = deck.state().get(stage, {})
    (dest / "state.json").write_text(json.dumps(prev, ensure_ascii=False, indent=1))
    return str(dest.relative_to(deck.root))


TOOL_PATHS = ("pptxgym", ".claude")


def tool_tree_state() -> str | None:
    """Fingerprint of the pipeline's own code and prompts.

    A repair fixes one deck; the tools are shared by all of them.  One repair
    agent patched `degrade_exec` mid-run — correctly, as it happens, but
    nobody reviewed it and it silently changed what every other deck would be
    degraded into.  The failure mode this guards against is the one that looks
    identical to success: quieting the gate instead of fixing the deck.

    None when this is not a git tree, in which case the check is skipped
    rather than guessed at.
    """
    import subprocess
    root = Path(__file__).resolve().parents[1]
    try:
        r = subprocess.run(["git", "-C", str(root), "status", "--porcelain",
                            "--", *TOOL_PATHS],
                           capture_output=True, text=True, timeout=30)
        h = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode or h.returncode:
        return None
    return h.stdout.strip() + "\n" + r.stdout


def revert_tool_changes(deck: Deck, before: str, label: str) -> str | None:
    """Undo an agent's edits to the shared tools, keeping the diff as evidence.

    Only touches paths that were clean before the run: reverting a change
    somebody was in the middle of making would be a worse bug than the one
    being prevented.
    """
    import subprocess
    if before is None or tool_tree_state() == before:
        return None
    root = Path(__file__).resolve().parents[1]
    was_dirty = {ln[3:].split(" -> ")[-1]
                 for ln in before.splitlines()[1:] if ln[3:]}
    now = subprocess.run(["git", "-C", str(root), "status", "--porcelain",
                          "--", *TOOL_PATHS], capture_output=True, text=True)
    touched = [ln[3:].split(" -> ")[-1] for ln in now.stdout.splitlines()
               if ln[3:] and ln[3:].split(" -> ")[-1] not in was_dirty]
    if not touched:
        return None
    diff = subprocess.run(["git", "-C", str(root), "diff", "--", *touched],
                          capture_output=True, text=True).stdout
    out = deck.root / f"{label}-tool-change.diff"
    out.write_text(f"# reverted: a repair may not edit the shared tools\n"
                   f"# files: {', '.join(touched)}\n\n{diff}")
    subprocess.run(["git", "-C", str(root), "checkout", "--", *touched],
                   capture_output=True)
    return f"{len(touched)} tool file(s) ({', '.join(touched[:3])})"


def repairs_done(deck: Deck) -> int:
    """How many times the repairer has actually run on this deck.

    Counting archived `reconciled-*` attempts was a proxy for it, and a poor
    one in both directions: reconcile is re-run for reasons that have nothing
    to do with a repair, and a repair that fails before reconcile gets to run
    is not counted at all — so `MAX_REPAIRS` was never quite the limit it
    claimed to be.  The repairer's own log is the thing being counted.
    """
    return len(list(deck.root.glob("repair-*.jsonl")))


def invalidate_from(deck: Deck, stage: str):
    """Drop the stage states a repair has made stale, so they re-run."""
    st = deck.state()
    for s in DOWNSTREAM.get(stage, []):
        st.pop(s, None)
    (deck.root / "state.json").write_text(
        json.dumps(st, ensure_ascii=False, indent=1))


BUNDLE = "bundle"
# Beside the bundle, never inside it: `bundle/` is exactly the delivery shape
# and an extra file in there is an extra file the solver is handed.
BUNDLE_MANIFEST = "bundle.json"


def bundle_contents(deck: Deck) -> dict[str, str]:
    """Every file in the bundle, hashed — the delivery as it stands now."""
    b = deck.root / BUNDLE
    if not b.is_dir():
        return {}
    return {str(p.relative_to(b)): _digest(p)
            for p in sorted(b.rglob("*")) if p.is_file()}


def bundle(deck: Deck) -> Path:
    """Everything a solver is given, and nothing else, in one directory.

    The information barrier used to be a request plus a substring scan of the
    probe's log, and the scan was wrong in both directions: it failed a run for
    `grep -rn "source.pptx" pptxgym` (reading *our* code) and for a report that
    named the forbidden files in prose to say it had not opened them, while a
    probe that quietly opened `../source.pptx` would read as clean.

    A directory containing only the broken file, the assets and the instruction
    makes the barrier structural.  The scan stays as a backstop, but it now has
    something unambiguous to look for: the probe works here, so any path that
    climbs back into the deck is a deliberate reach for the answer key.

    It is also the shape the task ships in, so this is not scaffolding — it is
    the deliverable, and `check_solvability` refuses to pass a deck without
    one.  It used to be built inside `_solvable_one` purely so the probe had
    somewhere blind to work, which is how three decks ended up carrying
    `solvable: ok` with nothing to hand over and no gate noticing.

    `bundle.json` is written beside it recording the fingerprint of everything
    `solvable` reads.  That is what ties the delivery to the verdict: the same
    digests go into `state.json` when the stage is marked, so a bundle built
    from other bytes than the ones that were judged is detectable rather than
    merely unlikely.
    """
    b = deck.root / BUNDLE
    if b.exists():
        shutil.rmtree(b)
    (b / "assets").mkdir(parents=True)
    shutil.copy2(deck.input_pptx, b / "input.pptx")
    src = deck.root / "assets"
    for f in sorted(src.iterdir()) if src.exists() else []:
        # the manifest records *why* an asset could not be produced, in terms
        # of what was broken — solver-visible only by accident
        if f.name == "manifest.json":
            continue
        (shutil.copytree if f.is_dir() else shutil.copy2)(f, b / "assets" / f.name)
    t = json.loads((deck.root / "task.json").read_text())
    (b / "instruction.md").write_text(
        f"# {t.get('name', deck.id)}\n\n{t.get('instruction', '')}\n",
        encoding="utf-8")
    (deck.root / BUNDLE_MANIFEST).write_text(json.dumps(
        {"built": time.strftime("%Y-%m-%dT%H:%M:%S"),
         "inputs": deck.fingerprint("solvable"),
         "files": bundle_contents(deck)},
        ensure_ascii=False, indent=1))
    return b


def bundle_problems(deck: Deck, verify_bytes: bool = True) -> list[str]:
    """Why this deck has nothing deliverable, or an empty list.

    Bundling is deterministic, so every one of these is repaired by calling
    `bundle` again — the point is that nothing may pass `solvable` while one
    of them is true.  `verify_bytes` is what makes it a tie to the verdict
    rather than a file-existence check; `status` turns it off because it asks
    the question about every deck in the batch at once.
    """
    b = deck.root / BUNDLE
    bad = []
    if not (b / "input.pptx").exists():
        bad.append("no bundle/input.pptx — there is nothing to hand a solver")
    inst = b / "instruction.md"
    if not (inst.exists() and inst.read_text(encoding="utf-8").strip()):
        bad.append("no bundle/instruction.md — the deck is damaged and the "
                   "solver is told nothing about it")
    for name in FORBIDDEN_TO_PROBE + ("manifest.json",):
        if any(b.rglob(name)) if b.is_dir() else False:
            bad.append(f"bundle/ holds {name}, which is answer-key material")

    f = deck.root / "task.json"
    if f.exists():
        try:
            t = json.loads(f.read_text())
        except json.JSONDecodeError:
            t = {}
        for a in t.get("assets") or []:
            if a.get("file") and not (b / "assets" / a["file"]).exists():
                bad.append(f"the instruction promises {a['file']!r} and the "
                           f"bundle does not contain it")
    if bad or not verify_bytes:
        return bad

    man = deck.root / BUNDLE_MANIFEST
    if not man.exists():
        return ["no bundle.json — the bundle is not tied to any verdict, so "
                "there is no saying which files it was built from"]
    try:
        m = json.loads(man.read_text())
    except json.JSONDecodeError:
        return ["bundle.json is not valid JSON"]
    if m.get("inputs") != deck.fingerprint("solvable"):
        bad.append("the bundle was built from different bytes than the ones "
                   "being judged — rebuild it")
    if m.get("files") != bundle_contents(deck):
        bad.append("bundle/ has been edited since it was built, so what a "
                   "solver would get is not what was probed")
    return bad


def barrier_breaches(deck: Deck, log: Path) -> list[str]:
    """Tool calls in which the probe reached outside its bundle.

    Only tools that *read* count.  A report that mentions `source.pptx` in a
    sentence is not a peek, and treating it as one cost two real verdicts.
    """
    import re

    if not log.exists():
        return []
    reading = {"Read", "Bash", "Grep", "Glob", "NotebookRead"}
    root = str(deck.root.resolve())
    pat = re.compile(re.escape(root) + r"(/[^\s\"']*)?"
                     r"|(?<![\w.])work/" + re.escape(deck.id) + r"(/[^\s\"']*)?")
    bad = []
    with open(log) as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            for c in ((d.get("message") or {}).get("content") or []):
                if not (isinstance(c, dict) and c.get("type") == "tool_use"):
                    continue
                if c.get("name") not in reading:
                    continue
                blob = json.dumps(c.get("input") or {}, ensure_ascii=False)
                for m in pat.finditer(blob):
                    tail = m.group(1) or m.group(2) or ""
                    # the bundle, plus the file it was told to write: every
                    # probe re-reads its own report to check the JSON parses,
                    # and calling that a peek voided four more runs
                    if tail.startswith(("/bundle", "/solvability.json")):
                        continue
                    bad.append(f"{c.get('name')}: …{m.group(0)[-70:]}")
                if any(f"../{f}" in blob or f"/../" in blob
                       for f in FORBIDDEN_TO_PROBE):
                    bad.append(f"{c.get('name')}: climbed out of the bundle")
    return sorted(set(bad))


def check_solvability(deck: Deck) -> dict:
    """Judge the probe's report — and first, whether there is anything to ship
    and whether the probe stayed blind.

    A solvability verdict reached with the answer key open carries no
    information, so the barrier is verified rather than requested.  And a
    verdict of `solvable` on a deck with no bundle is a pass with no product:
    `bundle/` is the whole deliverable, it was previously a side effect of
    running the probe, and three decks reached `ok` without one because nothing
    anywhere checked.
    """
    problems = bundle_problems(deck)
    if problems:
        raise StageError(
            f"{deck.id}: {problems[0]} — `bundle/` is what a solver is given, "
            f"so this deck cannot be passed until it has one that matches the "
            f"files being judged")

    breaches = barrier_breaches(deck, deck.root / "solvable.jsonl")
    if breaches:
        raise StageError(
            f"{deck.id}: the probe read outside its bundle "
            f"({breaches[0]}) — that is the answer key, so its verdict "
            f"means nothing")

    f = deck.root / "solvability.json"
    if not f.exists():
        raise StageError(f"{deck.id}: no solvability.json")
    try:
        r = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        raise StageError(f"{deck.id}: solvability.json is not valid JSON ({e})")

    verdict = r.get("verdict")
    if verdict not in ("solvable", "undetermined", "leaked", "ambiguous",
                       "overdetermined"):
        raise StageError(f"{deck.id}: unknown verdict {verdict!r}")
    if not r.get("degradations"):
        raise StageError(f"{deck.id}: no per-degradation findings")
    for d in r["degradations"]:
        if not (d.get("end_state") or "").strip():
            raise StageError(f"{deck.id}: {d.get('id')} has no end_state")
        if d.get("determinate") and not (d.get("evidence") or "").strip():
            raise StageError(
                f"{deck.id}: {d.get('id')} is called determinate with no "
                f"evidence — that is a guess wearing a verdict")
    if verdict != "solvable":
        rw = r.get("rework") or []
        if not rw:
            raise StageError(f"{deck.id}: verdict {verdict!r} with no `rework`")
        for x in rw:
            if x.get("stage") not in ("proposed", "recipe", "materialise"):
                raise StageError(f"{deck.id}: rework targets {x.get('stage')!r}")
    return {"verdict": verdict, "leaks": len(r.get("leaks") or []),
            "steps_measured": r.get("est_steps_measured"),
            "steps_declared": r.get("est_steps_declared"),
            "undetermined": sum(1 for d in r["degradations"]
                                if not d.get("determinate"))}


def decks_in(work: Path) -> list[Deck]:
    return [Deck(p) for p in sorted(work.glob("deck*")) if p.is_dir()]


class DeckBusy(RuntimeError):
    pass


class lock:
    """Refuse to run two stages on one deck at once.

    Stages hand off through files, so two processes working the same deck will
    happily interleave: one writes a fresh proposal while the other is midway
    through a recipe for the previous one, and the pair that lands on disk has
    never been consistent.  It is invisible afterwards — the files all parse —
    which is exactly why it needs a lock rather than care.
    """

    def __init__(self, deck: Deck, stage: str):
        self.path = deck.root / ".lock"
        self.stage = stage
        self.deck = deck

    def __enter__(self):
        import os
        if self.path.exists():
            try:
                held = json.loads(self.path.read_text())
            except Exception:                                # noqa: BLE001
                held = {}
            pid = held.get("pid")
            alive = False
            if pid:
                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False
            if alive:
                raise DeckBusy(
                    f"{self.deck.id} is locked by pid {pid} running "
                    f"{held.get('stage')!r} since {held.get('at')}")
        self.path.write_text(json.dumps(
            {"pid": os.getpid(), "stage": self.stage,
             "at": time.strftime("%Y-%m-%dT%H:%M:%S")}))
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
        return False
