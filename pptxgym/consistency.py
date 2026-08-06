"""Mechanical pre-check for `reconcile`: does the instruction describe *this*
file, and is the ground truth a legal solution to its own task?

Ten tasks built from these materials shipped and were run against a real
model.  Two of the four trajectories that were read afterwards died on a
defect the reconcile gate exists to catch, and reconcile passed both:

  **The instruction described damage that was never applied.**  ``deck0004``
  told the solver that on slide 9 "the illustrations that sat above the other
  two went with them".  Both of slide 9's pictures are present in the degraded
  file — the only degradation there was ``smartart_drop_nodes``.  The model
  spent dozens of steps hunting the phantom, deleted the SmartArt trying to
  make the page make sense, and scored 0 on work worth 0.63.

  **The instruction contradicted the ground truth.**  ``deck0006`` demanded
  that slide 6 come back as "a real, editable table (not a pasted picture)".
  Ground-truth slide 6 *is* a picture, and the reference material shipped with
  the task is that picture byte for byte.  Obeying the instruction scores 0;
  the only winning move is the one the instruction forbids.  The model obeyed.

Both are decidable from artefacts that already exist — ``delta.json``,
``source.pptx``, ``input.pptx``, ``assets/`` — so neither should ever have
needed a human to notice it.  The general rule underneath them:

    **The ground truth must be a legal solution to its own task, and every
    claim the instruction makes about what is broken must correspond to a
    change that was actually made.**

What this module is not
-----------------------
It is a *pre*-check, not a replacement for the judgement call.  It answers the
subset of reconcile's questions that survive contact with a regex.  Two of
reconcile's jobs are deliberately absent because they are not mechanically
decidable and a check that half-works is worse than an honest gap:

* **Is the wording of the damage description accurate?**  "Knocked apart",
  "in a mess", "no longer matches the rest of the deck" are true of many
  different deltas and false of none in a way a lexicon can see.
* **Is a masked reference masked enough?**  Whether a surviving twin element
  gives the answer away needs the render looked at.

A third used to be on that list — *"is the difficulty still right?  Step
counts come from a human's model of GUI work; nothing in the artefacts measures
it"* — and it is no longer true.  ``solvability.json`` measures it, and the
number it measures is not the number the weights use; see
``check_step_estimate`` below and ``comparators._measured_steps``.

**One line here moved on purpose.**  "Is the wording accurate" stays out, but
``check_excused_work`` does read the instruction's prose and can fail the deck
on it.  The two are different questions.  Judging accuracy means deciding
whether a fuzzy description is *true of* a delta, which a lexicon cannot do.
An **exemption** — "you do not need to re-create any animation" — is not fuzzy
and is not a description: it is a promise about what is *not* being asked for,
and the plan answers it exactly, with one question that needs no judgement at
all — *is there a scored component doing that work?*  It is the same shape as
``named_file_absent``: a promise to the solver, checked against the artefact
that has to honour it.

Severity
--------
``fail``   a contradiction between two artefacts, or a promise with nothing
           behind it.  Every ``fail`` this module raises on the ten-deck
           corpus is a real defect; there are no known false positives.
``warn``   evidence that is usually benign but is where a real defect hides —
           a reconciler has to look, not the pipeline.
``info``   recorded so a reader can see the check ran and what it saw.

    python3 -m pptxgym.consistency work/deck0004
    python3 -m pptxgym.consistency work/ --json
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .inventory import inventory_pptx

# --------------------------------------------------------------------------- #
# lexicon
# --------------------------------------------------------------------------- #

#: Object nouns, bucketed by whether the *file* can settle a claim about them.
#: `picture` and `table` are the only buckets that get enforced: a picture is
#: identified by the sha of its media part and a table by its `graphicData`
#: uri, so "there is one fewer of these on slide N" is a fact.  Everything
#: else lands in `other`, which exists only so those nouns can *win* the
#: attribution below and stop an enforced noun from being blamed for a loss it
#: had nothing to do with.  "chart", "graph", "plot" and "figure" sit in
#: `other` on purpose: in this corpus every one of them names a bitmap export
#: of a chart, not a chart part, and enforcing them fired four false alarms.
NOUNS = [
    ("picture", r"pictures?|images?|photos?|photographs?|screenshots?"
                r"|screen-?grabs?|illustrations?|artwork|bitmaps?|logos?|maps?"),
    ("table",   r"tables?"),
    ("other",   r"charts?|graphs?|plots?|diagrams?|smartart|captions?|labels?"
                r"|headings?|titles?|text|wording|bullets?|paragraphs?|boxes|box"
                r"|arrows?|rules?|lines?|rectangles?|callouts?|connectors?"
                r"|markers?|strips?|panels?|bubbles?|shapes?|graphics?|figures?"
                r"|annotations?|frames?|banner|tags?|cards?|options?|milestones?"
                r"|stages?|steps?|columns?|rows?|cells?|framing|styling|colou?rs?"
                r"|coding|typefaces?|fonts?|formatting|highlighting|treatment"
                r"|animations?|scans?|layers?|rings?|pointers?|montages?"),
]
NOUN_RE = re.compile(
    "|".join(f"(?P<{k}>\\b(?:{v})\\b)" for k, v in NOUNS), re.I)

#: Loss cues whose object *precedes* them ("the figure is gone").
CUE_BACK = re.compile(
    r"\b(?:gone|deleted|removed|disappeared|vanished|wiped|stripped|emptied"
    r"|absent|went with)\b", re.I)
#: Loss cues whose object *follows* them ("has lost its figure").
CUE_FWD = re.compile(
    r"\b(?:(?:has |have |had )?lost|is missing|are missing"
    r"|no longer (?:has|have|shows?|carr\w+|contains?))\b", re.I)

#: "as a real, editable table", "a native chart rather than a pasted picture".
#: The noun has to be named — a bare "not a picture" says what the answer is
#: not, and there is nothing to compare the ground truth against.
DEMAND_RE = re.compile(
    r"(?:real|actual|native|genuine|editable|proper|true|live)"
    r"[\w\s,\-]{0,30}?\b(?P<k1>tables?|charts?|graphs?|diagrams?|smartart)\b"
    r"|\b(?P<k2>tables?|charts?|graphs?|diagrams?|smartart)\b[\w\s,\-]{0,40}?"
    r"(?:not|rather than|instead of)\s+(?:a |an )?"
    r"(?:pasted |flat |static |mere )?"
    r"(?:pictures?|images?|screenshots?|bitmaps?|photos?)", re.I)
DEMAND_KIND = {"table": "table", "chart": "chart", "graph": "chart",
               "diagram": "smartart", "smartart": "smartart"}

#: Exemption cues, in the two grammars an exemption comes in.  `FWD` names the
#: excused work *after* the cue ("you do not need to re-create any animation");
#: `BACK` names it *before* ("the illustrations … are not expected back").
#:
#: Only forms that state an exemption outright are here.  "not as a picture of
#: the render laid over the page" (deck0007) and "the picture file itself was
#: not recovered" (deck0008) are a prohibition and a fact about an asset; both
#: contain "not" and neither excuses anything.
EXCUSE_FWD = re.compile(
    r"\b(?:(?:you|we|it)\s+)?(?:do|does|did)\s+not\s+(?:need|have)\s+to"
    r"|\bdon'?t\s+(?:need|have)\s+to"
    r"|\bno\s+need\s+to"
    r"|\bneed\s+not\b"
    r"|\b(?:you|we)(?:\s+are|'re)?\s+not\s+(?:being\s+)?(?:asked|expected|"
    r"required)\s+to"
    r"|\b(?:there\s+is|there's)\s+no\s+need\s+to"
    r"|\b(?:do\s+not|don'?t)\s+(?:bother|worry)\s+(?:with|about)"
    # "leave the older logo alone" — the object sits between the two halves of
    # the cue, so the cue is the first half and the second is only looked for.
    r"|\bleave\s+(?=[^.;]{0,60}?\b(?:alone|untouched"
    r"|as\s+(?:it|they)\s+(?:is|are))\b)", re.I)
EXCUSE_BACK = re.compile(
    r"\b(?:is|are|was|were)\s+not\s+(?:expected|required|needed)\b"
    r"|\b(?:is|are)\s+not\s+(?:yours|your)\s+to\b"
    r"|\bcan\s+(?:stay|remain)\s+(?:alone|untouched"
    r"|as\s+(?:it|they)\s+(?:is|are))\b"
    r"|\bneed\s+not\s+(?:come|go)\s+back\b"
    # "…, so leave that one as it is" — anaphoric, so the object is behind.
    r"|\bleave\s+(?:it|them|that\s+one|those|these)\s+"
    r"(?:alone|untouched|(?:exactly\s+)?as\s+(?:it|they)\s+(?:is|are))\b",
    re.I)

#: A deck-wide exemption says so: "any animation", "all of the transitions".
#: Without one of these the exemption is scoped to the slide under discussion,
#: which is what stops deck0004's "the small illustrations … are not expected
#: back" from being read as an amnesty on every picture in the deck.
EXCUSE_WIDE = re.compile(
    r"\b(?:any|all|every|each|anywhere|throughout|none\s+of\s+the)\b", re.I)

#: The buckets an exemption can name, and the operators each one excuses.
#: `kinds`, where it is not `None`, additionally restricts `delete` to the
#: shape kind the delta recorded — an amnesty on pictures is not an amnesty on
#: the text boxes deleted beside them.
#:
#: The same discipline as `NOUNS` above: a bucket is here only when the plan
#: can settle it.  A cue whose nearest noun is not one of these is dropped
#: rather than guessed at, which is why deck0004's "the small illustrations
#: that used to sit above those two stages … are not expected back" raises
#: nothing: the nearest noun behind the cue is "stages", and no operator
#: family is called stages.
EXCUSABLE: list[tuple[str, str, tuple[str, ...], tuple[str, ...] | None]] = [
    ("animation",
     r"animations?|click-?builds?|build-?ins?|entrance effects?|timings?",
     ("strip_animation", "anim_steps"), None),
    ("transition", r"transitions?", ("transition",), None),
    ("notes", r"speaker notes|notes pages?",
     ("clear_notes", "notes"), None),
    ("picture",
     r"pictures?|images?|photos?|photographs?|screenshots?|screen-?grabs?"
     r"|illustrations?|artwork|bitmaps?|logos?|maps?",
     ("delete", "resize", "scatter", "move"), ("picture",)),
    ("table", r"tables?",
     ("delete", "table_drop_rows", "table_drop_cols", "clear_table_cells"),
     ("table",)),
    ("typography",
     r"typefaces?|fonts?|text colou?rs?|colou?r coding|styling|formatting",
     ("set_font", "text_runs", "recolor", "outline", "strip_effects"), None),
    ("placement", r"positions?|placement|arrangement",
     ("move", "scatter", "resize", "rotate", "zorder"), None),
]
EXCUSE_NOUN_RE = re.compile(
    "|".join(f"(?P<{k}>\\b(?:{v})\\b)" for k, v, _, _ in EXCUSABLE), re.I)
EXCUSE_OPS = {k: (ops, kinds) for k, _, ops, kinds in EXCUSABLE}

SLIDE_RE = re.compile(
    r"\bslides?\s+((?:\d+)(?:\s*(?:,|and|&|to|-|–|through)\s*\d+)*)", re.I)
FILENAME_RE = re.compile(
    r"[A-Za-z0-9][\w\-.]*\.(?:png|jpe?g|csv|gif|tiff?|svg|emf|pptx|txt|pdf)",
    re.I)
_EXT_RE = re.compile(r"\.(png|jpe?g|csv|pptx|gif|tiff?|svg|emf|txt|pdf)\b", re.I)
_DOT = "␟"          # a stand-in for dots that do not end a sentence


# --------------------------------------------------------------------------- #
# instruction parsing
# --------------------------------------------------------------------------- #


def sentences(text: str) -> list[str]:
    """Split on sentence ends only.

    `reference-p10.png` and `0.06 in` are full stops that end nothing.  The
    first version of this split them and attributed slide 10's claim to a
    sentence that read "png shows where they belong."
    """
    protected = _EXT_RE.sub(lambda m: _DOT + m.group(0)[1:], text)
    protected = re.sub(r"(\d)\.(\d)", r"\1" + _DOT + r"\2", protected)
    return [s.replace(_DOT, ".").strip()
            for s in re.split(r"(?<=[.;!?])\s+|\n+", protected) if s.strip()]


def slide_refs(sentence: str) -> list[tuple[int, list[int]]]:
    """`(offset, [slide numbers])` for every "slide N" / "slides N and M"."""
    return [(m.start(), sorted({int(x) for x in re.findall(r"\d+", m.group(1))}))
            for m in SLIDE_RE.finditer(sentence)]


def damage_claims(instruction: str) -> list[dict[str, Any]]:
    """Every "<object> on slide N is gone" the instruction makes, restricted
    to objects whose absence the file can settle.

    Three rules, each of which was paid for by a false alarm on a deck that is
    correct on the point:

    * the cue takes the **nearest** noun on the side its grammar points at,
      and if that noun is not one the file can settle, the cue is dropped
      rather than passed along.  "the ring that singles out one of the six
      scans and the labelled pointers into it are gone" belongs to the
      pointers; blaming the scans failed a deck whose six scans are all there.
    * `no longer has` / `has lost` / `is missing` take the object that
      **follows**.  "the table screenshot no longer has the green, unfilled
      box" is about the box; read backwards it accuses the screenshot.
    * the slide is the nearest reference **before** the cue.  "Slides 11 and
      12 now have blank gaps …, even though the rest of the build-up across
      slides 8-13 is intact" must not put a claim on 8 and 13.
    """
    claims = []
    for sentence in sentences(instruction):
        refs = slide_refs(sentence)
        if not refs:
            continue
        nouns = [(m.start(), m.lastgroup) for m in NOUN_RE.finditer(sentence)]
        for pattern, backwards in ((CUE_BACK, True), (CUE_FWD, False)):
            for cue in pattern.finditer(sentence):
                at = cue.start()
                side = [n for n in nouns
                        if (n[0] < at if backwards else n[0] > at)]
                if not side:
                    continue
                kind = side[-1][1] if backwards else side[0][1]
                if kind == "other":
                    continue
                before = [r for r in refs if r[0] <= at]
                for slide in (before[-1][1] if before else refs[0][1]):
                    claims.append({"slide": slide, "kind": kind,
                                   "cue": cue.group(0),
                                   "text": sentence.strip()})
    return claims


def excused_work(instruction: str) -> list[dict[str, Any]]:
    """Every "you do not need to X" the instruction makes, as operator families.

    deck0002's instruction ends *"The click-build animations on several of the
    mangled slides went out with the deleted shapes; **you do not need to
    re-create any animation, only the artwork.**"* — and its plan scores three
    `strip_animation` components worth **0.0801** between them.  The candidate
    that does exactly what it was told (perfect artwork, animation left as the
    broken file has it) was built and scored: **0.919901**.  8% of that task is
    unreachable by obedience, and neither existing gate can see it — the
    coherence probe scores the ground truth at 1.0 because `source.pptx` still
    has its animations, and nothing anywhere compares the prose to the plan.

    Three rules, each of which decides a real sentence in the ten-deck corpus:

    * **the nearest noun on the side the grammar points at**, and dropped if
      it is not a bucket the plan can settle.  This is `damage_claims`' rule
      and it is what keeps deck0004 quiet.
    * **the scope is the slide under discussion**, carried over from the
      previous sentence when this one names none — unless the exemption is
      written deck-wide ("any animation", "all the transitions"), which is
      exactly how deck0002 writes it, and how anyone means it.
    * an exemption is **not** a prohibition.  "not as a picture of the render
      laid over the page" tells the solver how to do the work, not that the
      work is excused; only the cue forms in `EXCUSE_FWD` / `EXCUSE_BACK` count.
    """
    out: list[dict[str, Any]] = []
    carried: list[int] = []
    for sentence in sentences(instruction):
        refs = slide_refs(sentence)
        for pattern, backwards in ((EXCUSE_BACK, True), (EXCUSE_FWD, False)):
            for cue in pattern.finditer(sentence):
                window = (sentence[:cue.start()] if backwards
                          else sentence[cue.end():])
                # The nearest noun overall has to *be* the excusable one; a
                # bucket further away has lost the attribution.  Both lexicons
                # are read: `NOUNS` covers the objects that can steal the
                # attribution ("… above those two **stages** … are not expected
                # back"), `EXCUSABLE` the ones that can be settled, and some
                # words — "animations" — are in both.
                by_start: dict[int, Any] = {}
                for m in NOUN_RE.finditer(window):
                    by_start[m.start()] = None
                for m in EXCUSE_NOUN_RE.finditer(window):
                    by_start[m.start()] = m
                if not by_start:
                    continue
                near = max(by_start) if backwards else min(by_start)
                hit = by_start[near]
                if hit is None:
                    continue
                before = [r for r in refs if r[0] <= cue.start()]
                if refs:
                    slides: list[int] | None = (before[-1][1] if before
                                                else refs[0][1])
                elif EXCUSE_WIDE.search(
                        sentence[cue.start():cue.end() + 80]):
                    slides = None                     # the whole deck
                else:
                    slides = list(carried)
                ops, kinds = EXCUSE_OPS[hit.lastgroup]
                out.append({"bucket": hit.lastgroup, "ops": list(ops),
                            "kinds": list(kinds) if kinds else None,
                            "slides": slides, "cue": cue.group(0).strip(),
                            "text": sentence.strip()})
        if refs:
            carried = sorted({n for _, ns in refs for n in ns})
    return out


def excused_components(instruction: str,
                       components: list[dict]) -> list[dict[str, Any]]:
    """The scored components each exemption covers.  `[]` when the two agree.

    `components` are `plan["components"]`, whose `slide` is **0-based** while
    every slide number in an instruction is 1-based; the conversion happens
    here so that neither caller has to remember which convention it is holding.
    """
    out = []
    for excuse in excused_work(instruction):
        wanted = set(excuse["ops"])
        kinds = set(excuse["kinds"]) if excuse["kinds"] else None
        pages = set(excuse["slides"]) if excuse["slides"] is not None else None
        covered = [c for c in components
                   if c.get("op") in wanted
                   and (pages is None or (c.get("slide") or 0) + 1 in pages)
                   and (kinds is None or c.get("op") != "delete"
                        or (c.get("spec") or {}).get("kind") in kinds)]
        if covered:
            out.append({**excuse, "components": [c["id"] for c in covered],
                        "weight": round(sum(float(c.get("weight") or 0.0)
                                            for c in covered), 6)})
    return out


def native_demands(instruction: str) -> list[dict[str, Any]]:
    """Every "rebuild it as a real, editable <table|chart|diagram>".

    The slide carries over from the previous sentence: deck0001 says "Slide 4
    has lost its 'World production' figure completely." and then "It needs to
    come back … as a real editable chart … rather than a pasted picture."
    """
    demands = []
    current: list[int] = []
    for sentence in sentences(instruction):
        refs = slide_refs(sentence)
        if refs:
            current = sorted({n for _, ns in refs for n in ns})
        for m in DEMAND_RE.finditer(sentence):
            word = (m.group("k1") or m.group("k2")).lower().rstrip("s")
            kind = DEMAND_KIND.get(word)
            if not kind:
                continue
            for slide in current:
                demands.append({"slide": slide, "kind": kind,
                                "text": m.group(0).strip()})
    return demands


# --------------------------------------------------------------------------- #
# the facts a check reads
# --------------------------------------------------------------------------- #


@dataclass
class SlideFacts:
    """One slide, 0-based, as the two files disagree about it."""
    lost_pictures: collections.Counter = field(default_factory=collections.Counter)
    gt_kinds: collections.Counter = field(default_factory=collections.Counter)
    in_kinds: collections.Counter = field(default_factory=collections.Counter)


@dataclass
class DeckFacts:
    """Everything the checks read, as plain data.

    Kept separate from the files so a check can be exercised on a deck that
    would take a 4 MB `.pptx` to express.
    """
    deck: str = "deck"
    task: dict = field(default_factory=dict)
    delta: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    assets: dict[str, str] = field(default_factory=dict)      # name -> sha16
    slides: list[SlideFacts] = field(default_factory=list)
    drawn_in_input: collections.Counter = field(default_factory=collections.Counter)
    have_decks: bool = False        # source.pptx and input.pptx were both read

    def slide(self, one_based: int) -> SlideFacts | None:
        i = one_based - 1
        return self.slides[i] if 0 <= i < len(self.slides) else None

    @property
    def instruction(self) -> str:
        return self.task.get("instruction") or ""


@dataclass
class Finding:
    check: str
    severity: str            # fail | warn | info
    message: str
    slide: int | None = None
    deg: str | None = None
    evidence: str = ""

    def as_dict(self) -> dict:
        return {"check": self.check, "severity": self.severity,
                "slide": self.slide, "deg": self.deg,
                "message": self.message, "evidence": self.evidence}

    def __str__(self) -> str:
        where = f"p{self.slide}" if self.slide else ("  " + (self.deg or ""))
        return (f"{self.severity.upper():5} {self.check:26} {where:>5}  "
                f"{self.message}" + (f"\n{'':40}{self.evidence}"
                                     if self.evidence else ""))


def _sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _blobs(inv: dict, i: int) -> collections.Counter:
    return collections.Counter(
        s["picture"]["blob"] for s in inv["slides"][i]["shapes"]
        if s.get("picture") and s["picture"].get("blob"))


def _kinds(inv: dict, i: int) -> collections.Counter:
    return collections.Counter(s["kind"] for s in inv["slides"][i]["shapes"])


def read_facts(deck_dir) -> DeckFacts:
    """Load a deck directory.  Missing decks are tolerated — the checks that
    need them say so rather than crashing, because this is meant to be
    runnable the moment `task.json` exists."""
    root = Path(deck_dir)
    facts = DeckFacts(deck=root.name)
    task_f, delta_f = root / "task.json", root / "delta.json"
    if task_f.exists():
        facts.task = json.loads(task_f.read_text())
    if delta_f.exists():
        facts.delta = json.loads(delta_f.read_text())
    plan_f = root / "plan.json"
    if plan_f.exists():
        facts.plan = json.loads(plan_f.read_text())
    adir = root / "assets"
    if adir.is_dir():
        # Relative to `assets/`, and recursive, because that is what an asset
        # name *is* here — `pipeline.emit_assets` says so in as many words:
        # "Names are relative to `assets/`, so the keyframes producer's
        # `build-pNN/` contributes `build-pNN/build.json` and one entry per
        # frame". This scan read `iterdir()` and keyed on the basename, so a
        # directory was skipped for not being a file and everything under it
        # was invisible.
        #
        # deck0005 escalated it in run 14 rather than spend its last two
        # attempts: `task.json lists 'build-p03/build.json', which is not in
        # assets/` — four times, against a file that was there all along. No
        # repair could have fixed that; the task was right and the check was
        # blind.
        #
        # The quieter half is worse. `asset_is_the_answer` compares supplied
        # bytes against the pictures the degradation removed, and it reads the
        # same table: every nested asset was exempt from an anti-cheat check
        # without anyone choosing that.
        for f in sorted(adir.rglob("*")):
            if f.is_file():
                facts.assets[f.relative_to(adir).as_posix()] = \
                    _sha16(f.read_bytes())
    gt_f, in_f = root / "source.pptx", root / "input.pptx"
    if gt_f.exists() and in_f.exists():
        gt, init = inventory_pptx(gt_f), inventory_pptx(in_f)
        facts.have_decks = True
        for i in range(min(len(gt["slides"]), len(init["slides"]))):
            facts.slides.append(SlideFacts(
                lost_pictures=_blobs(gt, i) - _blobs(init, i),
                gt_kinds=_kinds(gt, i), in_kinds=_kinds(init, i)))
        for i in range(len(init["slides"])):
            facts.drawn_in_input += _blobs(init, i)
    return facts


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #


def check_deg_attribution(facts: DeckFacts) -> list[Finding]:
    """Every change has to belong to a degradation, and every degradation has
    to own at least one change.

    Left of this: deck0001's delta carries 23 entries with no `deg` at all,
    so nothing connects a sentence of the instruction to a line of the file —
    the reward stage then scores work nobody asked for, and *any* claim in the
    instruction is unfalsifiable.  Right of it: a degradation the recipe
    silently skipped is failure #1 at its coarsest, and needs no language
    model to see.
    """
    out: list[Finding] = []
    slides = facts.delta.get("slides") or {}
    entries = [(int(k), e) for k, v in slides.items() for e in v]
    if not entries and not facts.delta:
        return out
    unattributed = [e for _, e in entries if not e.get("deg")]
    if unattributed and len(unattributed) == len(entries):
        out.append(Finding(
            "deg_unattributed", "fail",
            f"none of the {len(entries)} delta entries names a degradation",
            evidence="delta.json has no `deg` field; no claim in the "
                     "instruction can be checked against it"))
        return out
    if unattributed:
        out.append(Finding(
            "deg_unattributed", "fail",
            f"{len(unattributed)} of {len(entries)} delta entries name no "
            f"degradation",
            evidence=", ".join(sorted({f"p{k + 1}:{e['op']}"
                                       for k, e in entries
                                       if not e.get("deg")})[:6])))
    by_deg: dict[str, set[int]] = collections.defaultdict(set)
    for k, e in entries:
        if e.get("deg"):
            by_deg[e["deg"]].add(int(e.get("slide") or (k + 1)))

    # Attribution and location are two different questions, and conflating
    # them is why this fired on decks it should not have.
    #
    # The deck-level operators do not write under `slides`: a reorder, a notes
    # clear and a layout edit each live under their own key. Reading only
    # `slides` reports a degradation implemented by one of them as having
    # changed nothing at all. deck0004 escalated exactly this, with the line
    # number — the fix an hour earlier had taught `pipeline.degrade` to fold in
    # these keys and left this gate, the one actually rejecting the deck,
    # reading `slides` alone. Third time in a day that a fix moved the copy and
    # left the original: the caveat split did it, `_left_over` did it, and now
    # this.
    #
    # Kept as a separate set rather than an empty entry in `by_deg`, because
    # `if not touched` cannot tell "no entry" from "an entry with no page" —
    # and a deck-level change genuinely has no single page it belongs to. So it
    # attributes without locating, which is the honest shape: we know the
    # degradation was implemented, and not that any particular slide is where.
    attributed = set(by_deg)
    for key in ("cleared_notes", "layout_edits"):
        for e in (facts.delta.get(key) or []):
            if isinstance(e, dict) and e.get("deg"):
                attributed.add(e["deg"])
                if str(e.get("slide", "")).isdigit():
                    by_deg[e["deg"]].add(int(e["slide"]))
    rec = facts.delta.get("reorder_slides")
    if isinstance(rec, dict) and rec.get("deg"):
        attributed.add(rec["deg"])

    for deg in facts.task.get("degradations") or []:
        did = deg.get("id")
        if deg.get("implemented") == "skipped":
            continue
        touched = by_deg.get(did)
        if did not in attributed:
            out.append(Finding(
                "deg_without_delta", "fail",
                f"{did} is recorded as {deg.get('implemented')!r} but the "
                f"delta records nothing for it", deg=did,
                evidence="the instruction describes damage that was never "
                         "applied — the failure that cost deck0004 its run"))
            continue
        if not touched:
            # Attributed by a deck-level operator and located nowhere. The
            # per-slide check below asks "did anything change on page N", and
            # a reorder changes the deck rather than a page — so answering it
            # would mean warning about every page the degradation names, for
            # a degradation that was implemented correctly.
            continue
        claimed = set(deg.get("slides") or [])
        for slide in sorted(claimed - touched):
            out.append(Finding(
                "deg_slide_without_delta", "warn",
                f"{did} lists slide {slide} but nothing on that slide "
                f"changed", slide=slide, deg=did,
                evidence="benign when the slide is named as the surviving "
                         "reference; a defect when the instruction tells the "
                         "solver that slide is damaged"))
    return out


def check_damage_claims(facts: DeckFacts) -> list[Finding]:
    """A picture or a table the instruction says is missing has to be missing.

    deck0004: *"On slide 9 … the illustrations that sat above the other two
    went with them"*.  Ground-truth slide 9 has two pictures; the degraded
    file has the same two.  The model hunted the phantom for dozens of steps
    and then deleted the real SmartArt to make the page make sense.

    deck0006: *"On the Supplementary Table 1 page (slide 6) the entire table
    is gone"*.  There is no table on ground-truth slide 6 and never was — the
    object is a picture — so no candidate can restore one.
    """
    out: list[Finding] = []
    if not facts.have_decks:
        return out
    for claim in damage_claims(facts.instruction):
        sf = facts.slide(claim["slide"])
        if sf is None:
            continue
        if claim["kind"] == "picture":
            ok = bool(sf.lost_pictures)
            n = sf.gt_kinds.get("picture", 0)
            what = (f"ground truth draws {n} picture(s) here and the degraded "
                    f"file draws the same {n}")
        else:
            ok = sf.gt_kinds["table"] > sf.in_kinds["table"]
            what = (f"ground truth holds {sf.gt_kinds['table']} table "
                    f"object(s), degraded file {sf.in_kinds['table']}")
        if not ok:
            out.append(Finding(
                "damage_claim_unsupported", "fail", slide=claim["slide"],
                message=f"instruction says a {claim['kind']} on slide "
                        f"{claim['slide']} is {claim['cue']!r}, the files "
                        f"disagree",
                evidence=f"{what} | {claim['text'][:120]}"))
    return out


def check_ground_truth_is_a_solution(facts: DeckFacts) -> list[Finding]:
    """If the instruction demands a native object, the ground truth must have
    one.

    deck0006 slide 6: *"rebuild it there as a real, editable table (not a
    pasted picture)"* — ground truth is a picture.  deck0001 slide 4: *"as a
    real editable chart … rather than a pasted picture"* — ground truth is a
    picture.  A reward graded against that ground truth pays for the picture
    and pays nothing for the table, so obeying the instruction scores 0 and
    the only winning move is the one the instruction forbids.
    """
    out: list[Finding] = []
    if not facts.have_decks:
        return out
    for demand in native_demands(facts.instruction):
        sf = facts.slide(demand["slide"])
        if sf is None:
            continue
        if sf.gt_kinds.get(demand["kind"], 0) == 0:
            held = ", ".join(f"{n}x {k}" for k, n in sf.gt_kinds.most_common())
            out.append(Finding(
                "gt_not_a_solution", "fail", slide=demand["slide"],
                message=f"instruction demands a {demand['kind']} on slide "
                        f"{demand['slide']}; the ground truth has none",
                evidence=f"{demand['text']!r} | gt slide holds {held}"))
    return out


def check_assets(facts: DeckFacts) -> list[Finding]:
    """Three things about what the solver is handed.

    *Named but absent* — a filename written into the instruction has to be in
    `assets/`, and so does every file `task.json` lists.

    *The asset is the answer* — a supplied file byte-identical to a picture
    the degradation removed.  Usually that is the design: the bitmap cannot
    be redrawn, so it ships.  It becomes a defect when the same slide carries
    a native-object demand: deck0006 hands over `p06--2.png`, which is the
    removed table bitmap byte for byte, while telling the solver to build a
    table instead.  Inserting the file is forbidden and scores 1; obeying the
    instruction is required and scores 0.
    """
    out: list[Finding] = []
    # An instruction writes the name a person would type — `build.json`, not
    # `build-p03/build.json`. The table is keyed by the path relative to
    # `assets/`, so a bare name has to be allowed to match the tail of one;
    # otherwise widening the scan to nested files would have swapped one false
    # failure for another, on decks that were passing.
    basenames = {k.rsplit("/", 1)[-1] for k in facts.assets}
    for name in sorted(set(FILENAME_RE.findall(facts.instruction))):
        if name not in facts.assets and name not in basenames:
            out.append(Finding(
                "named_file_absent", "fail",
                f"the instruction names {name!r}, which is not in assets/",
                evidence="a promise to the solver with nothing behind it"))
    for asset in facts.task.get("assets") or []:
        f = asset.get("file")
        if f and f not in facts.assets:
            out.append(Finding(
                "listed_asset_absent", "fail",
                f"task.json lists {f!r}, which is not in assets/"))
    if not facts.have_decks:
        return out
    by_sha = {}
    for name, sha in facts.assets.items():
        by_sha.setdefault(sha, name)
    demanded = {d["slide"] for d in native_demands(facts.instruction)}
    for i, sf in enumerate(facts.slides):
        for blob in sf.lost_pictures:
            if blob in by_sha:
                if i + 1 in demanded:
                    out.append(Finding(
                        "asset_is_the_answer", "fail", slide=i + 1,
                        message=f"{by_sha[blob]!r} is the removed picture on "
                                f"slide {i + 1} byte for byte, and the "
                                f"instruction forbids pasting a picture there",
                        evidence="the ground truth is that picture; the two "
                                 "requirements cannot both be met"))
                else:
                    out.append(Finding(
                        "asset_is_the_answer", "info", slide=i + 1,
                        message=f"{by_sha[blob]!r} is the removed picture on "
                                f"slide {i + 1} byte for byte",
                        evidence="intended when the bitmap cannot be redrawn; "
                                 "check the instruction does not also ask for "
                                 "it to be rebuilt as something else"))
            elif not facts.drawn_in_input.get(blob):
                out.append(Finding(
                    "media_unreachable", "fail", slide=i + 1,
                    message=f"slide {i + 1} lost a picture whose bytes are "
                            f"nowhere the solver can reach",
                    evidence=f"blob {blob} is not in assets/ and is drawn "
                             f"nowhere in input.pptx — only source.pptx has "
                             f"it, so the ground truth is the one file that "
                             f"could produce it"))
    return out


def check_excused_work(facts: DeckFacts) -> list[Finding]:
    """Work the instruction excuses must not be work the plan scores.

    The rule is general — *the plan may not score something the instruction
    says is not being asked for* — and it is not about animation.  A component
    nobody was asked to do is a component nobody can earn, so its weight is a
    ceiling below 1.0 on an agent that did exactly as it was told.  deck0002 is
    the measured case: three `strip_animation` components, 0.0801 of the task,
    and an obedient candidate that tops out at **0.9199**.

    The finding **refuses** rather than zeroing the components, and that is the
    point.  A `strip_animation` step of that kind usually exists to seal a leak
    another degradation opened — deleting a shape orphans its animation
    reference, and a surviving reference tells the solver what was removed — so
    the sentence is right and the components should go.  But on a deck whose
    animation genuinely *is* the job, the components are right and the sentence
    should go.  Nothing here can tell those apart, and a rule that silently
    zeroed the weight would get the second case wrong without saying so.  What
    is decidable is that the two contradict each other, so that is what is
    reported, and a human or the repairer picks which one is wrong.
    """
    out: list[Finding] = []
    components = facts.plan.get("components") or []
    if not facts.instruction or not components:
        return out
    for hit in excused_components(facts.instruction, components):
        where = ("the whole deck" if hit["slides"] is None
                 else "slide(s) " + ", ".join(str(s) for s in hit["slides"]))
        out.append(Finding(
            "excused_work_is_scored", "fail",
            f"the instruction excuses {hit['bucket']} work on {where} "
            f"({hit['cue']!r}), and the plan scores "
            f"{len(hit['components'])} {hit['bucket']} component(s) worth "
            f"{hit['weight']:.4f}",
            slide=(hit["slides"][0] if hit["slides"] else None),
            evidence=f"{', '.join(hit['components'][:6])} | "
                     f"an obedient agent's ceiling is "
                     f"{1.0 - hit['weight']:.4f} | {hit['text'][:150]}"))
    return out


def check_step_estimate(facts: DeckFacts) -> list[Finding]:
    """The number the weights come from, against the number that measured it.

    This module used to say, in as many words, *"Is the difficulty still right?
    Step counts come from a human's model of GUI work.  **Nothing in the
    artefacts measures it.**"*  That stopped being true: the solvability probe
    measures the same work independently and writes its finding into
    `solvability.json` — and on the ten-deck corpus it disagrees with the
    proposer's declaration per degradation by up to **8x**, while the weights
    were derived from the declaration alone.

    `comparators.build_plan` now prefers the measurement and records both, so
    this reads its verdict rather than re-deriving it.  It reports rather than
    blocks in the two benign cases — measured and used, or never measured at
    all — because the first is the fix working and the second is a gap in the
    probe's report, not a defect in the task.
    """
    from .comparators import STEP_DISAGREEMENT

    out: list[Finding] = []
    check = facts.plan.get("weight_check")
    if not check:
        return out
    source, worst = check.get("source"), check.get("worst")
    if not check.get("measured"):
        out.append(Finding(
            "step_estimate_unmeasured", "warn",
            "the weights come from the proposer's declared est_steps and "
            "nothing in the artefacts measures it",
            evidence=f"{check.get('measured_from')} — the same declaration "
                     f"also set the difficulty band, so neither has been "
                     f"checked against the work"))
        return out
    if source != "steps_measured":
        severity = "fail" if (worst or 0) > STEP_DISAGREEMENT else "warn"
        out.append(Finding(
            "step_estimate_contradicted", severity,
            f"the weights come from `{source}` while the probe's measurement "
            f"disagrees by up to {worst}x",
            evidence=f"{check.get('measured_from')}: declared "
                     f"{check.get('declared')} vs measured "
                     f"{check.get('measured')}"))
        return out
    out.append(Finding(
        "step_estimate_measured", "info",
        f"weights come from the probe's measured steps; the proposer's "
        f"declaration was out by up to {worst}x",
        evidence=f"declared {check.get('declared')} vs measured "
                 f"{check.get('measured')} ({check.get('measured_from')})"))
    return out


CHECKS = (check_deg_attribution, check_damage_claims,
          check_ground_truth_is_a_solution, check_assets,
          check_excused_work, check_step_estimate)


def check_facts(facts: DeckFacts) -> list[Finding]:
    return [f for check in CHECKS for f in check(facts)]


def check_deck(deck_dir) -> dict:
    """Run every check over one deck directory."""
    facts = read_facts(deck_dir)
    findings = check_facts(facts)
    worst = ("fail" if any(f.severity == "fail" for f in findings)
             else "suspect" if any(f.severity == "warn" for f in findings)
             else "ok")
    return {"deck": facts.deck, "verdict": worst,
            "checked_files": facts.have_decks,
            "findings": [f.as_dict() for f in findings]}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _deck_dirs(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        root = Path(p)
        if (root / "task.json").exists() or (root / "delta.json").exists():
            out.append(root)
        else:
            out.extend(sorted(d for d in root.glob("deck*") if d.is_dir()))
    return out


def main(argv=None) -> int:                                # pragma: no cover
    ap = argparse.ArgumentParser(
        description="Mechanical consistency pre-check for reconcile.")
    ap.add_argument("path", nargs="+",
                    help="a deck directory, or a work/ directory of them")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    ap.add_argument("--quiet", action="store_true",
                    help="drop `info` findings")
    args = ap.parse_args(argv)

    reports = [check_deck(d) for d in _deck_dirs(args.path)]
    if args.quiet:
        for r in reports:
            r["findings"] = [f for f in r["findings"] if f["severity"] != "info"]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=1))
    else:
        for r in reports:
            mark = {"ok": "ok  ", "suspect": "warn", "fail": "FAIL"}[r["verdict"]]
            print(f"{mark}  {r['deck']}"
                  + ("" if r["checked_files"] else "   (no pptx pair read)"))
            for f in r["findings"]:
                print("      " + str(Finding(**f)))
    return 1 if any(r["verdict"] == "fail" for r in reports) else 0


if __name__ == "__main__":                                 # pragma: no cover
    sys.exit(main())
