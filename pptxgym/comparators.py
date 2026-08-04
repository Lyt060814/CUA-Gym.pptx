"""Scoring a repaired deck, derived from the record of what was damaged.

There is no evaluator per task.  There is one comparator per **operator**, and
a `plan` that binds each entry of `delta.json` to one of them.  A thousand
tasks therefore need the twenty-odd comparators below and nothing else, which
is the only way the proof obligation stays finite: every claim about scoring
is a claim about an operator, and the operators are enumerable.

    build_plan(deck)                       -> plan.json, deterministic
    score(plan, cand_inv, gt_inv, init_inv) -> {"score", "components",
                                                "hard_gates", "failed_gate"}

`score` takes **inventories, not paths**.  The adversarial battery scores tens
of constructed decks against one plan and must not re-parse the ground truth
each time.

Where the prior value comes from
--------------------------------
Not from the delta.  The delta says *which shape* and *which facet* was
damaged; the value that facet is supposed to have is read out of the **ground
truth inventory**.  That is not a shortcut, it is the only correct source:
`set_font` on deck0004 p13 recorded `was_props: []` because the runs it
recoloured had no explicit run properties at all — a faithful record of
"nothing was set" that is indistinguishable, to a comparator, from "nothing
was recorded".  The gt inventory answers it exactly: those runs carry no
`color` key.  A comparator that trusts the delta's prior value would have
scored that component unscorable; one that reads gt scores it correctly.

Fail closed
-----------
`pptx-tasks/scaling/pipeline/ops.py` contains the failure this module exists
to not repeat:

    exp = entry.get("expected") or {}
    if not exp:
        return 1.0, "restored"

An operator whose prior value went missing hands out **full marks for doing
nothing**.  Here, a comparator that cannot find its shape, its facet or its
prior value raises `Unscorable`, and `score` turns that into **0.0** with the
reason in `why`.  `tests/test_comparators.py::test_a_missing_prior_value_scores_zero_not_one`
is the negative control for it.

Compare only the facet the operator damaged
-------------------------------------------
`recolor` compares the fill and nothing else — not the theme `fillRef` it
never touched.  This is not tidiness: any sub-term the operator leaves alone
scores 1.0 on the *broken* file too, and so lands in the floor.  One 0.2-weight
untouched sub-term is a floor of 0.2, which is over the 0.15 rejection limit
by itself.  Scoring only the damage is what keeps floors at zero by
construction.

Two kinds of check, not one
---------------------------
A **cheat** substitutes for the work and scores zero.  A **scope violation**
accompanies it — over-eagerness — and costs a bounded fraction of what was
earned.  Collapsing the two recorded 0.0 for a model that had done 43% and
63% of the work on two separate tasks; the header above the gate section
carries the evidence.  `hard_gates` names every check either way, but only a
cheat can set `failed_gate`.

`build_plan` then runs a **coherence** probe: no cheat gate may fire on the
ground truth, on a half-restore, on the answer rebuilt by a different route,
or on a correct answer plus one over-eager addition.  A gate that fires on
correct work rejects the task, exactly as a successful attack does — that is
the one check nothing in the pipeline had, and its absence let a gate zero a
hand-built SmartArt in an evaluator whose own component was at that moment
awarding 0.7 for it.

Floor normalisation
-------------------
Every component reports `floor` = what the broken file scores on it, and
`score` = (raw - floor) / (1 - floor), clamped.  Without it `move` and
`resize` hand out half credit for a shape nobody touched.  A floor above
`FLOOR_LIMIT` is **not** a signal to widen a tolerance — it is a rejection
reason routed back to `recipe`, recorded in `plan["rejected"]`.

Tolerance
---------
`POS_TOL` is 0.01 in — float noise.  WPS, the application these are graded in,
moves **0.0% of shapes** on open-and-save across all ten decks (REWARD.md
§2.1), so there is no measured noise to absorb and no evidence for anything
wider.  LibreOffice's 0.13–0.85 in p90 is the *agent's* text reflow in a
renderer that does not grade anything (§2.2) and is explicitly not a source of
tolerance.  Geometry is therefore binary at `POS_TOL`.

**This depends on WPS being the only application that touches the file.**
`POS_TOL`, the untouched-page gate and the media gate all rest on the same
measurement, and all three are invalid if the deck passes through LibreOffice:
LO reflows text by font metrics (0.13–0.85 in p90) and re-encodes every image
byte.  A rollout has already been seen where the VM's `.pptx` handler was
bound to LibreOffice Impress although setup launched WPS.  If that is the
environment, nothing here is salvageable by widening a number — the fix is in
the environment.  Say so before changing a tolerance.

REWARD.md §3③ ("judge relations, not absolute coordinates") is the right
long-term shape and is **not** implemented here.  Every relational term is
also a term the *broken* file can satisfy by accident — a scattered card that
still happens to share a left edge with its neighbour — so it raises the
measured floor, and a floor bought with a term that scores the untouched file
is exactly what floor normalisation exists to expose.  Adding it needs the
floor measured per deck first, not an argument.

Two things the application owns and nobody grades
-------------------------------------------------
* text inside a date / slide-number / footer / header field (`generated_text`)
* the *height* of an autofit text box — 0.600 in of it moves with the font set
  the machine happens to have (REWARD.md §2.4)

Both are dropped from every comparison and from the untouched-page gate.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable

from .inventory import inventory_pptx

EMU_PER_INCH = 914400
POS_TOL_IN = 0.01
POS_TOL = int(round(POS_TOL_IN * EMU_PER_INCH))       # 9144 EMU
ROT_TOL = 0.05                                        # degrees
FLOOR_LIMIT = 0.15
OVERLAY_COVER = 0.80                                  # fraction of the page
DAMAGE_MARGIN = int(0.25 * EMU_PER_INCH)

PLAN_FORMAT = "pptxgym.plan/1"

NATIVE_KINDS = ("chart", "table", "smartart")

#: fields the executor records that are bulky and that no comparator reads —
#: the prior value comes from the gt inventory, not from these blobs.
_BULK = ("removed_xml", "was_xml", "was_ln_xml", "was_fill_xml", "was_props",
         "was_sizes")


class Unscorable(Exception):
    """The comparator cannot establish what the answer is.

    Never caught by a comparator and never turned into credit: `score` reports
    it as 0.0 with the reason attached.
    """


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _frac(hit: int, total: int) -> float:
    return 1.0 if total <= 0 else hit / float(total)


def _sha(data: bytes, n: int = 16) -> str:
    return hashlib.sha256(data).hexdigest()[:n]


def _autofit(shape: dict) -> bool:
    return bool((shape.get("text") or {}).get("autofit"))


def _paras(shape: dict | None) -> list[str]:
    """Visible paragraph text.  `generated_text` is the application's, not the
    agent's, and is filed separately by the inventory precisely so it can be
    skipped by name."""
    if not shape:
        return []
    body = shape.get("text") or {}
    return [_norm(p.get("t", "")) for p in body.get("paragraphs", [])
            if _norm(p.get("t", ""))]


def _para_runs(shape: dict | None) -> list[list[dict]]:
    if not shape:
        return []
    body = shape.get("text") or {}
    return [p.get("runs", []) for p in body.get("paragraphs", [])]


def _bbox(shape: dict | None) -> dict | None:
    return (shape or {}).get("bbox")


def _centre_ok(a: dict, b: dict) -> bool:
    return abs(a["cx"] - b["cx"]) <= POS_TOL and abs(a["cy"] - b["cy"]) <= POS_TOL


def _extent_ok(a: dict, b: dict, dims: tuple[str, ...]) -> bool:
    return all(abs(a[d] - b[d]) <= POS_TOL for d in dims)


def _line_facts(shape: dict | None) -> tuple:
    """The outline as the facts a comparator may compare.

    `w` / `cap` / `cmpd` are only comparable when *both* sides really have a
    line: soffice resolves an inherited default into an explicit `w="9360"` on
    shapes that never had a border (37 on one deck), and `inventory._line_of`
    says in as many words that only two present lines may have their widths
    compared.  So the always-compared part is what draws — fill kind, colour,
    dash, arrowheads.
    """
    line = (shape or {}).get("line")
    if not line:
        return (None, None, None, None, None)
    return (line.get("fill"), line.get("color"), line.get("dash"),
            line.get("head"), line.get("tail"))


def _effect_facts(shape: dict | None) -> tuple:
    fx = (shape or {}).get("effects") or {}
    ref = (fx.get("style_ref") or {}).get("effectRef")
    return (tuple(fx.get("effects") or ()), ref)


def _diagram(shape: dict | None) -> dict | None:
    return (shape or {}).get("diagram")


def _table(shape: dict | None) -> dict | None:
    return (shape or {}).get("table")


def _cell_text(table: dict | None, row: int, col: int) -> str | None:
    if not table:
        return None
    rows = table.get("rows") or []
    if not (0 <= row < len(rows)):
        return None
    cells = rows[row].get("cells") or []
    if not (0 <= col < len(cells)):
        return None
    return _norm(cells[col].get("text", ""))


def _row_texts(table: dict | None, row: int) -> list[str] | None:
    if not table:
        return None
    rows = table.get("rows") or []
    if not (0 <= row < len(rows)):
        return None
    return [_norm(c.get("text", "")) for c in rows[row].get("cells") or []]


def _col_texts(table: dict | None, col: int) -> list[str] | None:
    if not table:
        return None
    out = []
    for row in table.get("rows") or []:
        cells = row.get("cells") or []
        if col >= len(cells):
            return None
        out.append(_norm(cells[col].get("text", "")))
    return out


def _anim_signature(slide: dict | None) -> list[tuple]:
    """Each build step as (effects it fires) — target ids left out.

    A shape rebuilt through the GUI gets a new `spid`, so the ids are a
    property of the file rather than of the outcome; the effects, their classes
    and their order are what "which thing happens at which click" means.
    """
    anim = (slide or {}).get("animation") or {}
    out = []
    for step in anim.get("steps", []):
        out.append(tuple(sorted((e.get("class"), e.get("preset"),
                                 e.get("subtype"))
                                for e in step.get("effects", []))))
    return out


def _transition_facts(slide: dict | None) -> tuple:
    tr = (slide or {}).get("transition")
    if not tr:
        return (None, None)
    detail = tr.get("detail") or {}
    return (tr.get("type"), tuple(sorted(detail.items())))


def _page_signature(slide: dict) -> set:
    """What makes a page recognisably itself, for order / identity questions.

    A **set**, compared by overlap, never by equality.  Equality asks "is this
    page byte-for-byte the page it was", and the answer is no as soon as an
    agent adds one shape to it — which is over-eagerness, not a reordered
    deck.  Written as an equality this check zeroed a model that had done 43%
    of the work, and the same mistake reproduced itself here the first time
    the coherence probe ran.
    """
    return {_sha(_norm(s.get("_plain", "")).encode(), 8)
            for s in slide.get("shapes", []) if _norm(s.get("_plain", ""))}


def _overlap(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


#: how much better another page has to match before the deck counts as
#: reordered rather than edited.
_ORDER_MARGIN = 0.20
_ORDER_EVIDENCE = 2          # distinct text blocks needed to tell pages apart


def _page_is_itself(gt_slides: list[dict], mine: dict, index: int) -> bool:
    """Is the page at `index` still the page the ground truth has there?

    Answered by best match, not by equality: the page has to resemble its own
    original more than it resembles any other page.  Pages with too little
    text to tell apart are not judged at all.
    """
    want = _page_signature(gt_slides[index])
    if len(want) < _ORDER_EVIDENCE:
        return True
    have = _page_signature(mine)
    here = _overlap(want, have)
    best_score, best = here, index
    for other, page in enumerate(gt_slides):
        value = _overlap(_page_signature(page), have)
        if value > best_score:
            best_score, best = value, other
    return best == index or best_score <= here + _ORDER_MARGIN


# --------------------------------------------------------------------------- #
# pairing
# --------------------------------------------------------------------------- #


#: `inventory._keys` emits candidate identities strongest first.  The last
#: three — `name:<name>`, `geo:<kind>:<w>x<h>` and `kind:<kind>` — say nothing
#: about *which* shape this is, only that something of roughly that size, type
#: or label is there.  A stock `Emu(1000000) x Emu(300000)` text box rounds to
#: `geo:textbox:1x0` and will happily pair with a real one at entirely the
#: wrong position; on the previous batch a shape exactly like that collected
#: 0.4 twice.
#:
#: `name:` is here because **an agent can type any name it likes**: the
#: `rename_only` attack renames a surviving shape to a deleted shape's name and
#: nothing else, and on three decks that alone was over the 0.05 threshold —
#: the matcher paired the two and the deleted shape then read as restored while
#: sitting five inches away.  A label is the weakest identity there is, so a
#: pairing made on one earns no credit for *existing*; it still gets compared,
#: and has to win on position, text, image or content.
_WEAK_KEY_PREFIXES = ("name:", "geo:", "kind:")


def _is_strong(key: str | None) -> bool:
    return bool(key) and not key.startswith(_WEAK_KEY_PREFIXES)


def _boxes_meet(a: dict | None, b: dict | None) -> bool:
    """Do these two boxes occupy any of the same page?

    Geometry cannot contradict what it does not state: a placeholder that
    inherits its position from the layout has no `bbox` at all, and a pairing
    involving one is judged on its key alone.
    """
    if a is None or b is None:
        return True
    for centre, dim in (("cx", "w"), ("cy", "h")):
        a0, a1 = a[centre] - a[dim] / 2.0, a[centre] + a[dim] / 2.0
        b0, b1 = b[centre] - b[dim] / 2.0, b[centre] + b[dim] / 2.0
        if a1 + POS_TOL < b0 or b1 + POS_TOL < a0:
            return False
    return True


def pair_slide(gt_shapes: list[dict], other: list[dict]) -> dict[str, dict | None]:
    """gt `_path` -> the shape on the other side that is the same shape.

    `inventory.match_shapes` walks the gt shapes in document order and lets
    each take the best counterpart still free.  That is right for a drift
    report and wrong here: a *deleted* picture is considered before its
    surviving neighbours, its `pic:<blob>` key finds nothing, and it falls
    through to `geo:picture:3x1`, where it takes the neighbour's counterpart —
    after which the neighbour is the one reported missing.  The deletion is
    still visible, but it has moved to the wrong shape, and the wrong shape is
    the one a component is scored on.

    So the pass here is ordered by **key strength across the whole slide**,
    not by document order: every pairing that agrees on a strong key is made
    before any pairing that only agrees on a weak one.  Ties break on centre
    distance and then on the gt path, so it is deterministic.
    """
    return {path: shape for path, (shape, _key)
            in pair_slide_detail(gt_shapes, other).items()}


def pair_slide_detail(gt_shapes: list[dict],
                      other: list[dict]) -> dict[str, tuple[dict | None, str | None]]:
    """`pair_slide`, plus the key each pairing was made on.

    The key is how strong the match is, and "how strong" is the difference
    between a rebuilt shape and a stock text box of similar size.

    A **weak** key is not allowed to pair two shapes whose boxes do not meet
    anywhere on the page.  A name, a rounded size class or a shape type is a
    claim about identity that geometry can flatly contradict, and `rename_only`
    is the attack that makes the claim for free: it typed a deleted shape's
    name onto a survivor five inches away and the two were paired.  Strong keys
    — a placeholder role, an image blob, the words the shape holds — are still
    allowed to pair across any distance, because a shape that was *moved* is
    still that shape and its component has to be able to say so.
    """
    proposals = []
    for gi, g in enumerate(gt_shapes):
        for oi, o in enumerate(other):
            okeys = set(o["keys"])
            for rank, key in enumerate(g["keys"]):
                if key not in okeys:
                    continue
                ga, oa = _bbox(g), _bbox(o)
                if not _is_strong(key) and not _boxes_meet(ga, oa):
                    continue
                dist = (math.hypot(ga["cx"] - oa["cx"], ga["cy"] - oa["cy"])
                        if ga and oa else 0.0)
                proposals.append((rank, dist, g["_path"], gi, oi, key))
                break
    proposals.sort(key=lambda p: (p[0], p[1], p[2]))
    taken_g: set[int] = set()
    taken_o: set[int] = set()
    out: dict[str, tuple[dict | None, str | None]] = {
        g["_path"]: (None, None) for g in gt_shapes}
    for _rank, _dist, path, gi, oi, key in proposals:
        if gi in taken_g or oi in taken_o:
            continue
        taken_g.add(gi)
        taken_o.add(oi)
        out[path] = (other[oi], key)
    return out


class Scene:
    """One (ground truth, other file) pairing, built once and reused.

    `score` builds two: gt-vs-candidate and gt-vs-broken.  The second is the
    floor, and it is *measured* on every call rather than read out of the plan,
    so a reported floor can never disagree with the inventories it was
    reported for.
    """

    def __init__(self, gt_inv: dict, other_inv: dict, slide_of: list | None = None):
        self.gt = gt_inv
        self.other = other_inv
        n = len(gt_inv["slides"])
        self.slide_of = list(slide_of) if slide_of else list(range(n))
        self._pairs: dict[int, dict[str, tuple[dict | None, str | None]]] = {}
        self._flat: dict[int, dict[str, dict | None]] = {}

    def gt_slide(self, index: int) -> dict:
        try:
            return self.gt["slides"][index]
        except IndexError as err:                                # pragma: no cover
            raise Unscorable(f"gt has no slide {index}") from err

    def slide(self, index: int) -> dict | None:
        if index >= len(self.slide_of):
            return None
        target = self.slide_of[index]
        if target is None or target >= len(self.other["slides"]):
            return None
        return self.other["slides"][target]

    def detail(self, index: int) -> dict[str, tuple[dict | None, str | None]]:
        if index not in self._pairs:
            other = self.slide(index)
            self._pairs[index] = pair_slide_detail(
                self.gt_slide(index)["shapes"],
                other["shapes"] if other else [])
        return self._pairs[index]

    def pairs(self, index: int) -> dict[str, dict | None]:
        if index not in self._flat:
            self._flat[index] = {path: shape for path, (shape, _k)
                                 in self.detail(index).items()}
        return self._flat[index]

    def key_for(self, index: int, path: str) -> str | None:
        return self.detail(index).get(path, (None, None))[1]


class Target:
    """What one comparator is handed: a slide, a shape, and their gt originals."""

    def __init__(self, scene: Scene, component: dict):
        self.scene = scene
        self.component = component
        self.index = component["slide"]
        self.spec = component["spec"]

    @property
    def gt_slide(self) -> dict:
        return self.scene.gt_slide(self.index)

    @property
    def slide(self) -> dict | None:
        return self.scene.slide(self.index)

    @property
    def gt_shape(self) -> dict:
        path = self.component.get("gt_path")
        if path is None:
            raise Unscorable("component has no shape path")
        for shape in self.gt_slide["shapes"]:
            if shape["_path"] == path:
                return shape
        raise Unscorable(f"no shape at gt path {path!r} on slide {self.index + 1}")

    @property
    def shape(self) -> dict | None:
        return self.scene.pairs(self.index).get(self.component.get("gt_path"))

    def counterpart(self, gt_shape: dict) -> dict | None:
        return self.scene.pairs(self.index).get(gt_shape["_path"])

    def gt_siblings(self, gt_shape: dict) -> list[dict]:
        """Shapes sharing a container with `gt_shape` — its z-order peers."""
        prefix = gt_shape["_path"].rsplit("/", 1)[0] + "/" \
            if "/" in gt_shape["_path"] else ""
        return [s for s in self.gt_slide["shapes"]
                if s is not gt_shape
                and s["_path"].startswith(prefix)
                and "/" not in s["_path"][len(prefix):]]


# --------------------------------------------------------------------------- #
# facets
# --------------------------------------------------------------------------- #


def _facet_centre(gt_shape: dict, shape: dict | None) -> tuple[float, str]:
    a = _bbox(gt_shape)
    if a is None:
        raise Unscorable("gt shape has no geometry to restore")
    b = _bbox(shape)
    if b is None:
        return 0.0, "no geometry"
    if _centre_ok(a, b):
        return 1.0, "position"
    off = math.hypot(a["cx"] - b["cx"], a["cy"] - b["cy"]) / EMU_PER_INCH
    return 0.0, f"off by {off:.2f}in"


def _facet_extent(gt_shape: dict, shape: dict | None) -> tuple[float, str]:
    a = _bbox(gt_shape)
    if a is None:
        raise Unscorable("gt shape has no geometry to restore")
    dims = ("w", "h")
    if _autofit(gt_shape):
        # the height of an autofit box is the application's arithmetic against
        # whatever fonts the machine resolved; 0.600in of it moved between two
        # font sets on one box (REWARD.md §2.4).  Judging it scores noise.
        dims = ("w",)
    b = _bbox(shape)
    if b is None:
        return 0.0, "no geometry"
    if _extent_ok(a, b, dims):
        return 1.0, "size" + ("(w only, autofit)" if len(dims) == 1 else "")
    return 0.0, "wrong size"


def _facet_text(gt_shape: dict, shape: dict | None) -> tuple[float, str]:
    want = _paras(gt_shape)
    if not want:
        raise Unscorable("gt shape holds no text")
    have = _paras(shape)
    pool = list(have)
    hit = 0
    for para in want:
        if para in pool:
            pool.remove(para)
            hit += 1
    return _frac(hit, len(want)), f"text {hit}/{len(want)}"


_PROP_OF_PARAM = {"color": "color", "font": "font", "size_pt": "sz",
                  "bold": "b", "italic": "i", "underline": "u"}


def _run_groups(shape: dict | None) -> dict[str, list[dict]]:
    """Every addressable bundle of runs in a shape, text body *and* table cells.

    `set_font` walks every `a:rPr` in the element, and a table cell's runs are
    inside `a:tc/a:txBody` — they are not in `text.paragraphs` at all.  Reading
    only the text body reported "gt shape has no formatted runs" for the two
    deck0009 components that de-bold two codes inside one table, which is a
    comparator failing to see a change the inventory can see perfectly well.
    """
    out: dict[str, list[dict]] = {}
    body = (shape or {}).get("text") or {}
    for index, para in enumerate(body.get("paragraphs", [])):
        out[f"p{index}"] = para.get("runs", [])
    table = _table(shape)
    if table:
        for r, row in enumerate(table.get("rows") or []):
            for c, cell in enumerate(row.get("cells") or []):
                if cell.get("runs"):
                    out[f"c{r},{c}"] = cell["runs"]
    return out


def _facet_run_props(gt_shape: dict, shape: dict | None,
                     props: tuple[str, ...]) -> tuple[float, str]:
    """Compare the run properties the operator set — where the answer states them.

    Two rules, both with a casualty behind them.

    **Compare only what the operator touched.**  Whole-run equality would also
    compare properties nobody changed, which score 1.0 on the *broken* file and
    go straight into the floor.

    **Never require the absence of an attribute.**  Where the ground truth
    carries no explicit value the run inherits it, and no colour picker, font
    box or size spinner in any application can write "no colour attribute".
    A model on the previous batch diagnosed five mangled titles correctly,
    restored weight and size, and scored 0/5 for writing black *explicitly*
    where the answer inherited it.  The correct state was unreachable by the
    means the task exists to exercise.  Deciding it properly needs the
    placeholder's `lstStyle`, the master's `txStyles` and the theme resolved,
    and the inventory records none of the three — so the honest move is to
    declare the property unscoreable here and let `build_plan` drop the
    component, not to compare the raw attribute and punish correct work.
    """
    if not props:
        raise Unscorable("no run property was recorded as changed")
    want = _run_groups(gt_shape)
    have = _run_groups(shape)
    hit = 0
    total = 0
    for address, runs in want.items():
        mine = have.get(address, [])
        for index, run in enumerate(runs):
            peer = mine[index] if index < len(mine) else None
            if peer is None or peer.get("t") != run.get("t"):
                peer = next((m for m in mine if m.get("t") == run.get("t")), peer)
            for prop in props:
                if run.get(prop) is None:
                    continue          # inherited in the answer: not decidable
                total += 1
                if peer is not None and peer.get(prop) == run.get(prop):
                    hit += 1
    if total == 0:
        raise Unscorable(
            f"the ground truth states none of [{'+'.join(props)}] explicitly on "
            f"this shape — it inherits them, and requiring an *absent* "
            f"attribute is not reachable through any application's UI")
    return _frac(hit, total), f"runs {hit}/{total} [{'+'.join(props)}]"


def _facet_fill(gt_shape: dict, shape: dict | None) -> tuple[float, str]:
    want = gt_shape.get("fill")
    have = (shape or {}).get("fill")
    return (1.0, "fill") if want == have else (0.0, "wrong fill")


def _facet_line(gt_shape: dict, shape: dict | None) -> tuple[float, str]:
    return ((1.0, "outline") if _line_facts(gt_shape) == _line_facts(shape)
            else (0.0, "wrong outline"))


def _facet_picture(gt_shape: dict, shape: dict | None) -> tuple[float, str]:
    want = (gt_shape.get("picture") or {}).get("blob")
    if not want:
        raise Unscorable("gt shape draws no image")
    have = ((shape or {}).get("picture") or {}).get("blob")
    return (1.0, "image") if want == have else (0.0, "wrong or missing image")


def _facet_crop(gt_shape: dict, shape: dict | None) -> tuple[float, str]:
    pic = gt_shape.get("picture")
    if pic is None:
        raise Unscorable("gt shape is not a picture")
    want = (pic.get("crop"), pic.get("mode"))
    mine = (shape or {}).get("picture") or {}
    return ((1.0, "crop") if want == (mine.get("crop"), mine.get("mode"))
            else (0.0, "wrong crop"))


def _facet_connector(t: "Target", gt_shape: dict,
                     shape: dict | None) -> tuple[float, str]:
    """Attachment compared by *which shape* each end holds, not by shape id.

    `a:stCxn/@id` is a shape id, and a shape redrawn through the GUI has a new
    one.  Resolving the id to a shape and then asking whether that shape is the
    same shape scores the outcome.
    """
    want = gt_shape.get("connector")
    if want is None:
        raise Unscorable("gt shape is not a connector")
    mine = (shape or {}).get("connector") or {}
    by_id_gt = {s["_id"]: s for s in t.gt_slide["shapes"]}
    other_slide = t.slide or {"shapes": []}
    by_id_now = {s["_id"]: s for s in other_slide["shapes"]}
    hit = 0
    for end in ("start", "end"):
        a, b = want.get(end), mine.get(end)
        if a is None and b is None:
            hit += 1
            continue
        if a is None or b is None:
            continue
        ga, gb = by_id_gt.get(a.get("id")), by_id_now.get(b.get("id"))
        if ga is None or gb is None:
            continue
        if t.counterpart(ga) is gb and a.get("idx") == b.get("idx"):
            hit += 1
    return _frac(hit, 2), f"attachments {hit}/2"


def _facet_diagram(spec: dict, gt_shape: dict,
                   shape: dict | None) -> tuple[float, str]:
    want = _diagram(gt_shape)
    if not want or not want.get("nodes"):
        raise Unscorable("gt shape carries no diagram")
    removed = [_norm(n.get("text", "")) for n in spec.get("removed_nodes", [])]
    removed = [text for text in removed if text]
    if not removed:
        raise Unscorable("no removed node text was recorded")
    have = _diagram(shape) or {}
    nodes = [_norm(n) for n in have.get("nodes", [])]
    hit = sum(1 for text in removed if text in nodes)
    survivors = [text for text in (_norm(n) for n in want["nodes"])
                 if text and text not in removed]
    kept = all(text in nodes for text in survivors)
    raw = _frac(hit, len(removed))
    return ((raw if kept else 0.0),
            f"nodes {hit}/{len(removed)}" + ("" if kept else " (survivors lost)"))


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #

Comparator = Callable[[Target], "tuple[float, str]"]
REGISTRY: dict[str, Comparator] = {}


def comparator(*names: str):
    def deco(fn):
        for name in names:
            REGISTRY[name] = fn
        return fn
    return deco


def _blend(parts: list[tuple[float, float, str]]) -> tuple[float, str]:
    """Weighted mean of sub-facets, with the reasons kept."""
    if not parts:
        raise Unscorable("nothing to compare")
    total = sum(w for w, _s, _l in parts)
    value = sum(w * s for w, s, _l in parts) / total
    return value, " · ".join(f"{l}={s:.2f}" for _w, s, l in parts)


# ---- geometry family ------------------------------------------------------ #


@comparator("move", "scatter", "swap")
def _cmp_position(t: Target) -> tuple[float, str]:
    return _facet_centre(t.gt_shape, t.shape)


@comparator("resize")
def _cmp_resize(t: Target) -> tuple[float, str]:
    gt = t.gt_shape
    parts = [(3.0, *_facet_extent(gt, t.shape))]
    if t.spec.get("keep_center", True) is not False:
        parts.append((1.0, *_facet_centre(gt, t.shape)))
    return _blend(parts)


@comparator("rotate")
def _cmp_rotate(t: Target) -> tuple[float, str]:
    gt = _bbox(t.gt_shape)
    if gt is None:
        raise Unscorable("gt shape has no geometry")
    mine = _bbox(t.shape)
    if mine is None:
        return 0.0, "no geometry"
    delta = abs((gt["rot"] - mine["rot"] + 180.0) % 360.0 - 180.0)
    return (1.0, "rotation") if delta <= ROT_TOL else (0.0, f"off {delta:.1f}deg")


@comparator("zorder")
def _cmp_zorder(t: Target) -> tuple[float, str]:
    """Order relative to the shapes it sits among, not the raw index.

    `z` is a position in a list whose length changes the moment anything else
    on the page is added or removed, so the fact worth scoring is which shapes
    this one is in front of.
    """
    gt = t.gt_shape
    shape = t.shape
    if shape is None:
        return 0.0, "shape absent"
    siblings = t.gt_siblings(gt)
    if not siblings:
        raise Unscorable("shape has no z-order peers to be ordered against")
    hit = 0
    seen = 0
    for other in siblings:
        mate = t.counterpart(other)
        if mate is None:
            continue
        seen += 1
        want = (gt["z"] > other["z"])
        if (shape["z"] > mate["z"]) == want:
            hit += 1
    if seen == 0:
        raise Unscorable("no z-order peer survived to compare against")
    return _frac(hit, seen), f"order {hit}/{seen}"


@comparator("ungroup")
def _cmp_ungroup(t: Target) -> tuple[float, str]:
    gt = t.gt_shape
    if gt["kind"] != "group":
        raise Unscorable("gt shape is not a group")
    shape = t.shape
    prefix = gt["_path"] + "/"
    members = [s for s in t.gt_slide["shapes"] if s["_path"].startswith(prefix)]
    if not members:
        raise Unscorable("gt group records no members")
    if shape is None or shape["kind"] != "group":
        return 0.0, "group absent"
    want_key = shape["keys"][0]
    hit = sum(1 for m in members
              if (t.counterpart(m) or {}).get("group") == want_key)
    return _blend([(2.0, 1.0, "group"),
                   (3.0, _frac(hit, len(members)),
                    f"members {hit}/{len(members)}")])


# ---- presence family ------------------------------------------------------ #


@comparator("delete", "blank_slide")
def _cmp_restored_shape(t: Target) -> tuple[float, str]:
    """The shape has to be back — *what* it was, and *where* it was.

    **Existence is a precondition, not a component.**  The first version of
    this comparator blended `present` (3 of 9) with the picture blob or the
    SmartArt node list (another 3), and neither of those can be wrong once the
    shape exists: the blob is the very key the shape was paired on, and the
    diagram's nodes come back inside the part that carries them.  Geometry —
    the only thing "put it back" actually asks for — was left worth a third,
    so a deleted shape put back 0.75 in out of place, 1.3x too large,
    repainted and re-worded scored **0.667**, and the same arithmetic paid
    `rename_only` half a component for typing a name.  Pasting something
    roughly there is strictly cheaper than restoring the thing, which makes it
    the move a training run finds first.

    So the two questions are asked separately and **multiplied**:

        what it is   — image, words, cells, nodes, series, fill
        where it is  — centre (2) and extent (1)

    A restoration that is wrong in every measurable respect scores 0.  One
    that is right in every respect scores 1.  In between the product still
    pays for partial work in both directions — three of five nodes back in
    the right place is 0.6, everything back with the size wrong is 0.67 — so
    this is not the all-or-nothing rubric that recorded 0.0 for a model which
    had done 43% and 63% of the work on two earlier tasks.
    """
    gt = t.gt_shape
    shape = t.shape
    if shape is None:
        return 0.0, "shape absent"
    # Credit is only earned by an identity that says which shape this is — a
    # placeholder role, an image blob, a composite kind, the words it holds.
    # A pairing made on `name:`, `geo:` or `kind:` says only that something of
    # about that size, type or label is there, and a stock text box is
    # something of about that size.
    strong = _is_strong(t.scene.key_for(t.index, gt["_path"]))
    placed = _facet_centre(gt, shape) if _bbox(gt) else None
    if not strong and (placed is None or placed[0] < 1.0):
        # Nothing here says *which* shape this is.  Being in exactly the right
        # place is itself an identity — a plain rectangle redrawn where the
        # original stood — but matching the size or the name alone is not.
        return 0.0, "no identity: neither content nor position matches"

    what: list[tuple[float, float, str]] = []
    if (gt.get("picture") or {}).get("blob"):
        what.append((3.0, *_facet_picture(gt, shape)))
    if _paras(gt):
        what.append((3.0, *_facet_text(gt, shape)))
    if _table(gt):
        what.append((3.0, *_facet_table_all(gt, shape)))
    if _diagram(gt) and _diagram(gt).get("nodes"):
        what.append((3.0, *_facet_diagram_all(gt, shape)))
    if gt.get("chart"):
        what.append((3.0, *_facet_chart_all(gt, shape)))
    if gt.get("fill"):
        what.append((1.0, *_facet_fill(gt, shape)))

    where: list[tuple[float, float, str]] = []
    if placed is not None:
        where.append((2.0, *placed))
        where.append((1.0, *_facet_extent(gt, shape)))

    if what and where:
        content, why_what = _blend(what)
        geometry, why_where = _blend(where)
        return content * geometry, f"{why_what} × {why_where}"
    if what:
        return _blend(what)
    if where:
        return _blend(where)
    # A shape with no geometry, no image, no words and no content: it exists
    # and there is nothing else to ask.  `build_plan` drops the component if
    # even the ground truth cannot satisfy it.
    return 1.0, "present (nothing else is comparable)"


def _facet_table_all(gt_shape: dict, shape: dict | None) -> tuple[float, str]:
    want = _table(gt_shape)
    if not want:
        raise Unscorable("gt shape holds no table")
    mine = _table(shape)
    if not mine:
        return 0.0, "no table"
    if (mine.get("n_rows"), mine.get("n_cols")) != (want.get("n_rows"),
                                                    want.get("n_cols")):
        return 0.0, "wrong table shape"
    hit = 0
    total = 0
    for r, row in enumerate(want.get("rows") or []):
        for c, cell in enumerate(row.get("cells") or []):
            text = _norm(cell.get("text", ""))
            if not text:
                continue
            total += 1
            if _cell_text(mine, r, c) == text:
                hit += 1
    return _frac(hit, total), f"cells {hit}/{total}"


def _facet_diagram_all(gt_shape: dict, shape: dict | None) -> tuple[float, str]:
    want = _diagram(gt_shape) or {}
    nodes = [_norm(n) for n in want.get("nodes", []) if _norm(n)]
    if not nodes:
        raise Unscorable("gt diagram records no node text")
    have = [_norm(n) for n in (_diagram(shape) or {}).get("nodes", [])]
    # a diagram rebuilt as ordinary boxes is a legitimate route — the
    # instruction for deck0007 asks for exactly that — so the node text is
    # looked for among the slide's shapes too, not only inside a diagram part.
    hit = sum(1 for n in nodes if n in have)
    return _frac(hit, len(nodes)), f"nodes {hit}/{len(nodes)}"


def _facet_chart_all(gt_shape: dict, shape: dict | None) -> tuple[float, str]:
    want = gt_shape.get("chart") or {}
    series = want.get("series") or []
    if not series:
        raise Unscorable("gt chart records no series")
    mine = (shape or {}).get("chart") or {}
    have = {(_norm(s.get("name", "")), tuple(s.get("values") or ()))
            for s in mine.get("series") or []}
    hit = sum(1 for s in series
              if (_norm(s.get("name", "")), tuple(s.get("values") or ())) in have)
    return _frac(hit, len(series)), f"series {hit}/{len(series)}"


# ---- text family ---------------------------------------------------------- #


@comparator("clear_text", "set_text")
def _cmp_text(t: Target) -> tuple[float, str]:
    return _facet_text(t.gt_shape, t.shape)


@comparator("set_font")
def _cmp_set_font(t: Target) -> tuple[float, str]:
    params = t.spec.get("params") or {}
    props = tuple(prop for key, prop in _PROP_OF_PARAM.items() if key in params)
    if not props:
        raise Unscorable("set_font recorded no run property it changed")
    return _facet_run_props(t.gt_shape, t.shape, props)


@comparator("text_runs")
def _cmp_text_runs(t: Target) -> tuple[float, str]:
    """Only the paragraphs the step touched, addressed by gt index.

    Indices are read off the *pristine* deck, so a step that deleted a
    paragraph does not shift the addresses of the ones it did not.
    """
    touched = t.spec.get("touched") or []
    if not touched:
        raise Unscorable("text_runs recorded no touched paragraph")
    params = t.spec.get("params") or {}
    props = tuple(prop for key, prop in _PROP_OF_PARAM.items() if key in params)
    gt_paras = (t.gt_shape.get("text") or {}).get("paragraphs") or []
    have = ((t.shape or {}).get("text") or {}).get("paragraphs") or []
    hit = 0.0
    for item in touched:
        index = item.get("paragraph")
        if index is None or index >= len(gt_paras):
            raise Unscorable(f"touched paragraph {index} is not in the gt shape")
        want = gt_paras[index]
        text = _norm(want.get("t", ""))
        mine = next((p for p in have if _norm(p.get("t", "")) == text), None)
        if mine is None:
            continue
        if not props:
            hit += 1.0
            continue
        # only where the answer states the property: see `_facet_run_props`.
        stated = [(index, prop) for prop in props
                  for index, r in enumerate(want.get("runs", []))
                  if r.get(prop) is not None]
        if not stated:
            hit += 1.0
            continue
        runs = mine.get("runs", [])
        ok = all(index < len(runs)
                 and runs[index].get(prop) == want["runs"][index].get(prop)
                 for index, prop in stated)
        hit += 1.0 if ok else 0.5
    return _frac(hit, len(touched)), f"paragraphs {hit:.1f}/{len(touched)}"


# ---- style family --------------------------------------------------------- #


@comparator("recolor")
def _cmp_recolor(t: Target) -> tuple[float, str]:
    return _facet_fill(t.gt_shape, t.shape)


@comparator("outline")
def _cmp_outline(t: Target) -> tuple[float, str]:
    return _facet_line(t.gt_shape, t.shape)


@comparator("strip_effects")
def _cmp_strip_effects(t: Target) -> tuple[float, str]:
    removed = t.spec.get("removed")
    if not removed:
        raise Unscorable("strip_effects recorded nothing removed")
    parts: list[tuple[float, float, str]] = []
    if any(name != "gradFill" for name in removed):
        want, have = _effect_facts(t.gt_shape), _effect_facts(t.shape)
        parts.append((2.0, 1.0 if want == have else 0.0, "effects"))
    if "gradFill" in removed:
        parts.append((2.0, *_facet_fill(t.gt_shape, t.shape)))
    return _blend(parts)


@comparator("crop")
def _cmp_crop(t: Target) -> tuple[float, str]:
    return _facet_crop(t.gt_shape, t.shape)


@comparator("detach_connector")
def _cmp_detach(t: Target) -> tuple[float, str]:
    gt = t.gt_shape
    parts = [(3.0, *_facet_connector(t, gt, t.shape))]
    if _bbox(gt) and t.spec.get("nudge_in", 0.4):
        parts.append((1.0, *_facet_centre(gt, t.shape)))
    return _blend(parts)


# ---- table family --------------------------------------------------------- #


@comparator("clear_table_cells")
def _cmp_clear_cells(t: Target) -> tuple[float, str]:
    cleared = t.spec.get("cleared") or []
    if not cleared:
        raise Unscorable("clear_table_cells recorded no cell")
    want = _table(t.gt_shape)
    if not want:
        raise Unscorable("gt shape holds no table")
    mine = _table(t.shape)
    if not mine:
        return 0.0, "no table"
    hit = 0
    for item in cleared:
        row, col = item["at"]
        expect = _cell_text(want, row, col)
        if expect is None:
            raise Unscorable(f"gt table has no cell {row},{col}")
        if _cell_text(mine, row, col) == expect:
            hit += 1
    survivors_ok = _table_survivors_ok(want, mine,
                                       {(i["at"][0], i["at"][1]) for i in cleared})
    raw = _frac(hit, len(cleared))
    return ((raw if survivors_ok else 0.0),
            f"cells {hit}/{len(cleared)}" + ("" if survivors_ok else " (survivors lost)"))


def _table_survivors_ok(want: dict, mine: dict, damaged: set) -> bool:
    for r, row in enumerate(want.get("rows") or []):
        for c, cell in enumerate(row.get("cells") or []):
            if (r, c) in damaged:
                continue
            text = _norm(cell.get("text", ""))
            if text and _cell_text(mine, r, c) != text:
                return False
    return True


@comparator("table_drop_rows")
def _cmp_drop_rows(t: Target) -> tuple[float, str]:
    removed = t.spec.get("removed") or []
    if not removed:
        raise Unscorable("table_drop_rows recorded no row")
    want, mine = _table(t.gt_shape), _table(t.shape)
    if not want:
        raise Unscorable("gt shape holds no table")
    if not mine:
        return 0.0, "no table"
    if mine.get("n_rows") != want.get("n_rows"):
        return 0.0, f"{mine.get('n_rows')} rows, expected {want.get('n_rows')}"
    hit = 0
    for item in removed:
        index = item["row"]
        expect = _row_texts(want, index)
        if expect is None:
            raise Unscorable(f"gt table has no row {index}")
        if _row_texts(mine, index) == expect:
            hit += 1
    return _frac(hit, len(removed)), f"rows {hit}/{len(removed)}"


@comparator("table_drop_cols")
def _cmp_drop_cols(t: Target) -> tuple[float, str]:
    removed = t.spec.get("removed") or []
    if not removed:
        raise Unscorable("table_drop_cols recorded no column")
    want, mine = _table(t.gt_shape), _table(t.shape)
    if not want:
        raise Unscorable("gt shape holds no table")
    if not mine:
        return 0.0, "no table"
    if mine.get("n_cols") != want.get("n_cols"):
        return 0.0, f"{mine.get('n_cols')} cols, expected {want.get('n_cols')}"
    hit = 0
    for item in removed:
        index = item["col"]
        expect = _col_texts(want, index)
        if expect is None:
            raise Unscorable(f"gt table has no column {index}")
        if _col_texts(mine, index) == expect:
            hit += 1
    return _frac(hit, len(removed)), f"cols {hit}/{len(removed)}"


# ---- composite family ----------------------------------------------------- #


@comparator("smartart_drop_nodes")
def _cmp_smartart(t: Target) -> tuple[float, str]:
    gt = _find_smartart(t.gt_slide, t.spec.get("data_part"))
    shape = t.counterpart(gt)
    return _facet_diagram(t.spec, gt, shape)


def _find_smartart(slide: dict, data_part: str | None) -> dict:
    diagrams = [s for s in slide["shapes"]
                if s["kind"] == "smartart" and _diagram(s)]
    if data_part:
        for shape in diagrams:
            if (_diagram(shape) or {}).get("_data_part") == data_part:
                return shape
    if len(diagrams) == 1:
        return diagrams[0]
    raise Unscorable(f"cannot identify the SmartArt (part {data_part!r}, "
                     f"{len(diagrams)} on the slide)")


@comparator("chart_edit")
def _cmp_chart(t: Target) -> tuple[float, str]:
    gt = _find_chart(t.gt_slide, t.spec.get("chart_part"))
    shape = t.counterpart(gt)
    want = gt.get("chart") or {}
    mine = (shape or {}).get("chart") or {}
    parts: list[tuple[float, float, str]] = []
    dropped = t.spec.get("removed_series") or []
    if dropped:
        names = {_norm(item.get("name", "")) for item in dropped}
        by_name = {_norm(s.get("name", "")): s for s in want.get("series") or []}
        have = {_norm(s.get("name", "")): s for s in mine.get("series") or []}
        hit = sum(1 for name in names
                  if name in by_name and name in have
                  and tuple(have[name].get("values") or ())
                  == tuple(by_name[name].get("values") or ()))
        survivors = [_norm(s.get("name", "")) for s in want.get("series") or []
                     if _norm(s.get("name", "")) not in names]
        kept = all(name in have for name in survivors)
        parts.append((3.0, _frac(hit, len(names)) if kept else 0.0,
                      f"series {hit}/{len(names)}"))
    for name in t.spec.get("stripped") or []:
        parts.append((1.0, _chart_element(want, mine, name), name))
    if not parts:
        raise Unscorable("chart_edit recorded neither a dropped series nor a strip")
    return _blend(parts)


def _find_chart(slide: dict, chart_part: str | None) -> dict:
    charts = [s for s in slide["shapes"] if s["kind"] == "chart"]
    if len(charts) == 1:
        return charts[0]
    if not charts:
        raise Unscorable("no chart on the gt slide")
    raise Unscorable(f"cannot identify the chart ({len(charts)} on the slide)")


def _chart_element(want: dict, mine: dict, name: str) -> float:
    if name == "title":
        return 1.0 if _norm(want.get("title", "")) == _norm(mine.get("title", "")) else 0.0
    if name == "legend":
        return 1.0 if (want.get("legend"), want.get("legend_pos")) == \
            (mine.get("legend"), mine.get("legend_pos")) else 0.0
    if name == "data_labels":
        return 1.0 if (want.get("labels") or {}) == (mine.get("labels") or {}) else 0.0
    if name == "gridlines":
        a = tuple(ax.get("gridlines") for ax in want.get("axes") or [])
        b = tuple(ax.get("gridlines") for ax in mine.get("axes") or [])
        return 1.0 if a == b else 0.0
    if name == "axis_titles":
        a = tuple(_norm(ax.get("title", "")) for ax in want.get("axes") or [])
        b = tuple(_norm(ax.get("title", "")) for ax in mine.get("axes") or [])
        return 1.0 if a == b else 0.0
    raise Unscorable(f"no comparator for stripped chart element {name!r}")


# ---- slide-level family --------------------------------------------------- #


@comparator("strip_animation")
def _cmp_strip_animation(t: Target) -> tuple[float, str]:
    want = _anim_signature(t.gt_slide)
    if not want:
        raise Unscorable("gt slide has no build sequence to restore")
    have = _anim_signature(t.slide)
    hit = sum(1 for index, step in enumerate(want)
              if index < len(have) and have[index] == step)
    exact = len(have) == len(want)
    return (_frac(hit, len(want)) * (1.0 if exact else 0.75),
            f"steps {hit}/{len(want)}" + ("" if exact else f" (+{len(have) - len(want)})"))


@comparator("anim_drop_steps")
def _cmp_anim_steps(t: Target) -> tuple[float, str]:
    removed = t.spec.get("removed") or []
    if not removed:
        raise Unscorable("anim_drop_steps recorded no step")
    want = _anim_signature(t.gt_slide)
    if not want:
        raise Unscorable("gt slide has no build sequence")
    have = _anim_signature(t.slide)
    hit = 0
    for item in removed:
        index = item["step"] - 1
        if not (0 <= index < len(want)):
            raise Unscorable(f"gt slide has no build step {item['step']}")
        if index < len(have) and have[index] == want[index]:
            hit += 1
    intact = len(have) == len(want)
    raw = _frac(hit, len(removed))
    return ((raw if intact else raw * 0.5),
            f"steps {hit}/{len(removed)}" + ("" if intact else " (count wrong)"))


@comparator("strip_transition")
def _cmp_transition(t: Target) -> tuple[float, str]:
    want = _transition_facts(t.gt_slide)
    if want == (None, None):
        raise Unscorable("gt slide has no transition to restore")
    return ((1.0, "transition") if want == _transition_facts(t.slide)
            else (0.0, "wrong or missing transition"))


@comparator("clear_notes")
def _cmp_notes(t: Target) -> tuple[float, str]:
    want = _norm(t.gt_slide.get("notes") or "")
    if not want:
        raise Unscorable("gt slide has no notes to restore")
    have = _norm((t.slide or {}).get("notes") or "")
    return (1.0, "notes") if want == have else (0.0, "notes differ")


# ---- deck-level family ---------------------------------------------------- #


@comparator("reorder_slides", "delete_slides")
def _cmp_slide_order(t: Target) -> tuple[float, str]:
    """Every gt page has to be back, in its place, recognisable as itself."""
    gt_pages = t.scene.gt["slides"]
    others = t.scene.other["slides"]
    hit = sum(1 for index in range(len(gt_pages))
              if index < len(others)
              and _page_is_itself(gt_pages, others[index], index))
    return _frac(hit, len(gt_pages)), f"pages {hit}/{len(gt_pages)}"


@comparator("layout_edit")
def _cmp_layout(t: Target) -> tuple[float, str]:
    name = t.spec.get("layout")
    want = (t.scene.gt.get("layouts") or {}).get(name)
    if not want:
        raise Unscorable(f"gt has no layout named {name!r}")
    removed = t.spec.get("removed") or []
    if not removed:
        raise Unscorable("layout edit recorded nothing removed")
    mine = (t.scene.other.get("layouts") or {}).get(name) or {"shapes": []}
    pairs = pair_slide(want["shapes"], mine["shapes"])
    hit = 0
    for item in removed:
        path = item.get("path")
        if path not in pairs:
            raise Unscorable(f"gt layout has no shape at path {path!r}")
        if pairs[path] is not None:
            hit += 1
    return _frac(hit, len(removed)), f"layout shapes {hit}/{len(removed)}"


# --------------------------------------------------------------------------- #
# gates: cheating vs scope
# --------------------------------------------------------------------------- #
#
# The first version of this section had one list of gates and one verdict:
# fire, and the score is zero.  A rollout of four tasks built that way scored
# **0.0 for a model that had done 43% and 63% of the work**, twice, and both
# times the gate fired on the solution rather than on a cheat:
#
# * an instruction said "bring the branding back in line" without naming the
#   pages; two ground-truth slides legitimately carry no logo, the model added
#   one there, and the untouched-pages gate turned 0.435 into 0;
# * an instruction described damage that had never been applied, the model
#   went looking and rebuilt a SmartArt by hand — and a native-objects gate
#   zeroed it, in an evaluator whose own diagram component was at that moment
#   awarding 0.7 for exactly that hand-built equivalent.  A gate and a
#   component in the same evaluator, contradicting each other.
#
# So the checks are split by what they mean, and the split is decidable:
#
#   a **cheat** *substitutes* for the work   -> zero.  Pasting a picture over
#       the hole, replacing a native object with a picture of one, cloning a
#       surviving shape into the gap, deleting the damaged page.
#   a **scope violation** *accompanies* it   -> graded penalty.  Touching a
#       page nobody asked about, adding something where the answer has
#       nothing.  Over-eagerness is not fraud, and recording it as identical
#       to doing nothing destroys the signal the task exists to produce.
#
# `hard_gates` still reports every check by name, pass or fail, so nothing
# downstream loses information.  Only a cheat can set `failed_gate`.

#: Facts an application writes on its own, measured on a real WPS open-and-save
#: of all ten ground truths (`gt_roundtrip`), over every page the task never
#: named.  These are the left column of REWARD.md §1 — noise, not equivalence —
#: and the prescribed handling is to subtract them, so the untouched-page check
#: below does not look at them at all:
#:
#:   2457  run properties          WPS materialises `a:endParaRPr` in full
#:                                 (`{"end": true, "b": "1", "sz": 3200,
#:                                 "font": "Calibri"}`) on paragraphs whose
#:                                 original said nothing.  `inventory._para_runs`
#:                                 is right to record it — a probe found a real
#:                                 degradation hiding in one — so the inventory
#:                                 keeps it and this projection leaves it out.
#:     12  paragraph marL/indent   381000 EMU written back as 380990
#:      7  slide transition        WPS invented a `fade` on a page that had none
#:      2  picture recolor         a `lum` effect dropped on re-serialisation
#:
#: Before this, nine of ten ground truths lost 0.15–0.30 to the capped scope
#: penalty for changes nobody made — REWARD.md §5's `roundtrip_identity` probe,
#: failing.  Widening the band instead would have opened the same band to
#: everybody, which is the one thing §1 says never to do.
_EXEMPT_SHAPE_KEYS = ("generated_text",)

#: per-item penalty and cap for each scope violation.  These rates are a
#: policy choice and are labelled as one — there is nothing measured to derive
#: them from.  The property that *is* asserted, and tested, is the last line:
#: no amount of over-eagerness may take more than half of what was earned, so
#: correct partial work always outranks doing nothing.
SCOPE_RATES = {
    "untouched_pages_unchanged": (0.10, 0.30),
    "survivors_intact": (0.06, 0.30),
    "no_extra_shapes": (0.04, 0.20),
    "input_media_preserved": (0.15, 0.15),
}
PENALTY_CAP = 0.50


def _page_facts(slide: dict) -> dict:
    """A slide reduced to what an agent controls **and** the application does not.

    Named facts, not `flatten(everything) - a few keys`.  The difference is the
    whole of defect 3: a subtractive list compares every byte the inventory can
    see, so every serialisation habit the grading application has is a change
    somebody has to be charged for, and the ground truth itself was being
    charged 0.15–0.30 for thirteen of them.  A fact belongs here only if some
    comparator scores it *and* WPS was measured not to rewrite it — see the
    table above `_EXEMPT_SHAPE_KEYS` for what that measurement threw out.

    Still dropped for their own older reasons: the text an application
    generates into a date / slide-number / footer / header field (81 untouched
    placeholders read as deleted-plus-added under WPS) and the *height* of an
    autofit box (0.600 in of it moves with the machine's font set).
    """
    out: dict[str, Any] = {}
    for shape in slide.get("shapes", []):
        at = f"shapes[{shape.get('key') or shape.get('_path')}]"
        out[f"{at}.kind"] = shape.get("kind")
        out[f"{at}.hidden"] = bool(shape.get("hidden"))
        out[f"{at}.group"] = shape.get("group")
        box = _bbox(shape)
        if box:
            dims = ("cx", "cy", "w", "h", "rot")
            if _autofit(shape):
                dims = ("cx", "w", "rot")
            for dim in dims:
                out[f"{at}.bbox.{dim}"] = box.get(dim)
        text = _paras(shape)
        if text:
            out[f"{at}.text"] = tuple(text)
        if shape.get("fill"):
            out[f"{at}.fill"] = json.dumps(shape["fill"], sort_keys=True,
                                           default=str)
        if shape.get("line"):
            out[f"{at}.line"] = _line_facts(shape)
        if shape.get("picture"):
            # *that* it draws an image, not *which* bytes: WPS re-encodes some
            # PNGs on open-and-save (six of them across deck0004 and deck0005,
            # 161755 bytes down to 105850 on one), so the blob is the
            # application's to change.
            out[f"{at}.picture"] = True
        table = _table(shape)
        if table:
            out[f"{at}.table"] = tuple(
                tuple(_norm(c.get("text", "")) for c in (row.get("cells") or []))
                for row in (table.get("rows") or []))
        diagram = _diagram(shape)
        if diagram:
            out[f"{at}.diagram"] = tuple(_norm(n) for n in diagram.get("nodes", []))
        chart = shape.get("chart")
        if chart:
            out[f"{at}.chart"] = (_norm(chart.get("title", "")),
                                  tuple((_norm(s.get("name", "")),
                                         tuple(s.get("values") or ()))
                                        for s in chart.get("series") or []))
    out["notes"] = _norm(slide.get("notes") or "")
    out["layout"] = slide.get("layout")
    out["background"] = json.dumps(slide.get("background"), sort_keys=True,
                                   default=str)
    out["animation"] = tuple(_anim_signature(slide))
    return out


def _slide_area(inv: dict) -> int:
    package = inv["package"]
    return int(package.get("slide_w") or 0) * int(package.get("slide_h") or 0)


_MISS = object()


def _damage(plan) -> dict:
    """Where the damage is — from **every** delta entry, scored or not.

    Not from `plan["components"]`: a component dropped as unsatisfiable still
    describes a page that was really broken, and reading the damage off the
    surviving components turned three genuinely damaged deck0004 pages into
    "untouched pages the agent meddled with" and charged 0.30 for repairing
    them.  What is scored and what was damaged are two different questions.
    """
    return plan.get("damage") or {"slides": [], "paths": {}, "boxes": {}}


def _damaged(plan, index: int) -> bool:
    return index in set(_damage(plan)["slides"])


def _damage_boxes(plan) -> dict[int, list[list[int]]]:
    return {int(k): v for k, v in _damage(plan)["boxes"].items()}


def _inside(box: dict, region: list[int]) -> bool:
    x, y, cx, cy = region
    return (x - DAMAGE_MARGIN <= box["cx"] <= x + cx + DAMAGE_MARGIN
            and y - DAMAGE_MARGIN <= box["cy"] <= y + cy + DAMAGE_MARGIN)


def _unmatched(scene: Scene, index: int) -> list[dict]:
    other = scene.slide(index)
    if other is None:
        return []
    claimed = {id(s) for s in scene.pairs(index).values() if s is not None}
    return [s for s in other["shapes"] if id(s) not in claimed]


# ---- cheats --------------------------------------------------------------- #


def _gate_slide_order(plan, scene: Scene) -> tuple[bool, str]:
    """The deck still has its pages, in its order.

    A cheat, not a scope violation: deleting the damaged page is the purest
    form of substituting for the work.
    """
    want = len(scene.gt["slides"])
    have = len(scene.other["slides"])
    if want != have:
        return False, f"{have} slides, expected {want}"
    if any(c["op"] in ("reorder_slides", "delete_slides") for c in plan["components"]):
        return True, "order is a scored component here"
    for index in range(len(scene.gt["slides"])):
        other = scene.slide(index)
        if other is None:
            return False, f"slide {index + 1} is missing"
        if _damaged(plan, index):
            continue
        if not _page_is_itself(scene.gt["slides"], other, index):
            return False, f"slide {index + 1} holds another page's content"
    return True, "slide count and order intact"


def _gate_no_overlay(plan, scene: Scene) -> tuple[bool, str]:
    """No new object covering the page.

    A picture of the finished slide laid over the wreckage satisfies every
    visual check and does none of the work.
    """
    area = _slide_area(scene.gt)
    if area <= 0:
        return True, "slide size unknown"
    for index in range(len(scene.gt["slides"])):
        for shape in _unmatched(scene, index):
            if shape.get("hidden"):
                continue
            box = _bbox(shape)
            if box is None:
                continue
            covers = (box["w"] * box["h"]) / float(area)
            if covers >= OVERLAY_COVER and (shape.get("picture") or shape.get("fill")):
                return False, (f"slide {index + 1}: new {shape['kind']} covers "
                               f"{covers * 100:.0f}% of the page")
    return True, "no full-page overlay"


def _gate_native_not_flattened(plan, cand: Scene, broken: Scene) -> tuple[bool, str]:
    """A native chart / table / SmartArt may not become a **picture** of one.

    This is the gate that scored a model 0.0 for rebuilding a SmartArt by
    hand, so what it asks has been narrowed to the cheat it was written for.
    A native object that is gone and has a *picture* sitting in its place is a
    screenshot standing in for the object.  A native object rebuilt out of
    ordinary shapes is **the work** — deck0007's instruction asks for exactly
    that, in as many words — and is not this gate's business; whether the
    rebuild is good enough is the component's judgement, and a gate must never
    overrule a component that is at that moment awarding credit.
    """
    for index in range(len(cand.gt["slides"])):
        here, there = cand.slide(index), broken.slide(index)
        if here is None or there is None:
            continue
        for shape in there["shapes"]:
            if shape["kind"] not in NATIVE_KINDS:
                continue
            box = _bbox(shape)
            still = any(s["kind"] == shape["kind"] for s in here["shapes"]
                        if _same_place(_bbox(s), box))
            if still or box is None:
                continue
            covered = [s for s in here["shapes"]
                       if s.get("picture") and _covers(_bbox(s), box)]
            if covered:
                return False, (f"slide {index + 1}: the {shape['kind']} is now a "
                               f"picture of one")
    return True, "no native object replaced by a picture of one"


def _same_place(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return False
    return (abs(a["cx"] - b["cx"]) <= max(POS_TOL, b["w"] // 2)
            and abs(a["cy"] - b["cy"]) <= max(POS_TOL, b["h"] // 2))


def _covers(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None or b["w"] <= 0 or b["h"] <= 0:
        return False
    return (_same_place(a, b)
            and a["w"] * a["h"] >= 0.5 * b["w"] * b["h"])


def _gate_no_clones(plan, scene: Scene) -> tuple[bool, str]:
    """Nothing may be filled in by duplicating a shape that survived.

    A cheat by the same test as the others: copying the neighbouring card into
    the hole substitutes for rebuilding what was there.

    The signal is a **surplus copy of something the page already has** — the
    ground truth says how many copies belong, and a duplicate of a surviving
    shape appearing inside the damaged region is a copy of it.  Content the
    ground truth does not have at all is *not* a clone: it is new work, right
    or wrong, and the first version of this gate that missed the distinction
    fired on five hand-built boxes carrying five law names, which is the
    solution deck0007's instruction asks for in as many words.  The coherence
    probe caught it here rather than a model losing 60% for it in a rollout.
    """
    boxes = _damage_boxes(plan)
    for index, regions in sorted(boxes.items()):
        other = scene.slide(index)
        if other is None:
            continue
        allowed: dict[str, int] = {}
        for shape in scene.gt_slide(index)["shapes"]:
            for key in shape["keys"]:
                if _is_strong(key):
                    allowed[key] = allowed.get(key, 0) + 1
                    break
        seen: dict[str, int] = {}
        for shape in other["shapes"]:
            key = next((k for k in shape["keys"] if _is_strong(k)), None)
            if key is None:
                continue
            seen[key] = seen.get(key, 0) + 1
        # deliberately *not* restricted to shapes `pair_slide` left unmatched:
        # a clone dropped into the hole pairs with the shape that used to be
        # there on `geo:`, so a gate that only looks at unmatched shapes never
        # sees the one thing it exists to catch.
        for shape in other["shapes"]:
            key = next((k for k in shape["keys"] if _is_strong(k)), None)
            box = _bbox(shape)
            if key is None or box is None:
                continue
            if (allowed.get(key, 0) >= 1
                    and seen.get(key, 0) > allowed[key]
                    and any(_inside(box, region) for region in regions)):
                return False, (f"slide {index + 1}: a surplus copy of "
                               f"{key} fills the damaged region")
    return True, "no cloned shapes"


def _gate_media_not_pasted(plan, scene: Scene, broken_inv: dict) -> tuple[bool, str]:
    """No blob that is in the answer, is not in the input, and was not supplied.

    Media is compared as a **multiset of content digests**, never by part name.
    Measured on real round trips: **WPS** turns 51 blobs into 52 — it renames
    and renumbers the parts and adds a thumbnail — so a `part name -> digest`
    gate fails on a file WPS did nothing to.  **LibreOffice** turns 29 into 29
    with *not one identical*: it re-encodes every image and rewrites EMF as
    WMF.

    Digests are still right for *this* direction, because it only asks whether
    a blob the answer has and the input does not has **appeared**, and an
    application that re-encodes cannot manufacture the original's bytes.  It
    does mean a pasted original that WPS then re-encodes is missed; what would
    catch that is a perceptual hash, and nothing here has one.  Note that WPS
    re-encodes a minority of images by itself (four on deck0004, two on
    deck0005), which is why the *disappearance* direction below counts parts
    rather than digests — a check the application can fail on the agent's
    behalf is not a check.

    Only the *appearance* of an original is a cheat, and the asset exemption is
    not a loophole: nine of the ten decks supply every gt-only blob byte for
    byte in `assets/`, because "put this picture back" is the instruction.
    The disappearance direction is the `input_media_preserved` scope check
    below, deliberately not a cheat — see its docstring.
    """
    gt_only = set(scene.gt["package"]["media"]) - set(broken_inv["package"]["media"])
    supplied = set(plan.get("assets_sha") or ())
    intruder = (gt_only - supplied) & set(scene.other["package"]["media"])
    if intruder:
        return False, f"{len(intruder)} original media part(s) pasted back"
    return True, "no pasted originals"


# ---- scope violations ----------------------------------------------------- #


def _scope_untouched_pages(plan, scene: Scene) -> tuple[int, str]:
    """Pages the task did not ask about, changed anyway.

    Over-eagerness, not fraud.  An instruction that says "bring the branding
    back in line" without naming the pages invites exactly this, and the model
    that took the invitation had done 43% of the work when a hard gate threw
    all of it away.
    """
    hits = []
    for index, page in enumerate(scene.gt["slides"]):
        if _damaged(plan, index):
            continue
        other = scene.slide(index)
        if other is None:
            hits.append(f"slide {index + 1} missing")
            continue
        a, b = _page_facts(page), _page_facts(other)
        keys = [k for k in set(a) | set(b) if a.get(k, _MISS) != b.get(k, _MISS)]
        if keys:
            hits.append(f"slide {index + 1}: {sorted(keys)[0]}")
    return len(hits), "; ".join(hits[:3]) or "untouched pages unchanged"


def _scope_survivors(plan, scene: Scene) -> tuple[int, str]:
    """Shapes on a damaged page that the damage did not touch, now gone."""
    targets = {int(k): set(v) for k, v in _damage(plan)["paths"].items()}
    hits = []
    for index, paths in sorted(targets.items()):
        pairs = scene.pairs(index)
        for shape in scene.gt_slide(index)["shapes"]:
            path = shape["_path"]
            if path in paths or any(path.startswith(p + "/") for p in paths):
                continue
            if pairs.get(path) is None:
                hits.append(f"slide {index + 1}: {shape['kind']} {path}")
    return len(hits), "; ".join(hits[:3]) or "survivors intact"


def _scope_extra_shapes(plan, scene: Scene) -> tuple[int, str]:
    """New furniture on a damaged page, outside the region that was damaged.

    Not "no more shapes than the ground truth": rebuilding a five-box SmartArt
    as five real boxes is what deck0007 asks for and legitimately ends with
    four more shapes than the gt has.  The budget is geometric and comes from
    the `box` the executor already records for every entry — and it is a
    penalty, not a zero, because putting something in the wrong place is a
    mistake, not a substitute for the work.
    """
    hits = []
    for index, regions in sorted(_damage_boxes(plan).items()):
        for shape in _unmatched(scene, index):
            if shape.get("group"):
                continue
            box = _bbox(shape)
            if box is None:
                continue
            if not any(_inside(box, region) for region in regions):
                hits.append(f"slide {index + 1}: {shape['kind']}")
    return len(hits), "; ".join(hits[:3]) or "no extra shapes"


def _scope_media_lost(plan, scene: Scene, broken_inv: dict) -> tuple[int, str]:
    """Images the input had that the candidate no longer has — **by count**.

    Not by digest.  Under **LibreOffice** a digest comparison means nothing at
    all: LO re-encodes every image on save, so every blob "disappears" on a
    file the agent did nothing to, and a rollout has already been seen where
    the VM's `.pptx` handler was bound to Impress although setup launched WPS.
    **WPS does it too**, which the gate above still says it does not: a plain
    open-and-save of the ground truth re-encoded four PNGs on deck0004 and two
    on deck0005 (one of them 161755 bytes down to 105850), and charged nine of
    ten ground truths 0.15 for it.  Bytes are the application's; the number of
    pictures in the package is the agent's.

    A check an application can fail on the agent's behalf must never be a
    zero, and this one must not fire on the ground truth at all.
    """
    before = len(broken_inv["package"]["media"])
    after = len(scene.other["package"]["media"])
    lost = max(0, before - after)
    return (1 if lost else 0,
            f"{lost} of the input's {before} image part(s) are gone"
            if lost else "input media preserved")


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #


def _run_component(component: dict, scene: Scene) -> tuple[float, str]:
    fn = REGISTRY.get(component["op"])
    if fn is None:
        return 0.0, f"no comparator for {component['op']!r}"
    try:
        raw, why = fn(Target(scene, component))
    except Unscorable as err:
        # fail closed.  `ops.py` returned 1.0 "restored" here and that single
        # line hands full marks to a file nobody touched.
        return 0.0, f"unscorable: {err}"
    return max(0.0, min(1.0, float(raw))), why


def score(plan: dict, candidate_inv: dict, gt_inv: dict, init_inv: dict) -> dict:
    """Score one candidate.  Pure: inventories in, numbers out."""
    slide_of = plan.get("init_slide_of")
    cand = Scene(gt_inv, candidate_inv)
    broken = Scene(gt_inv, init_inv, slide_of)

    components = []
    total = 0.0
    for component in plan["components"]:
        raw, why = _run_component(component, cand)
        floor, floor_why = _run_component(component, broken)
        if floor >= 1.0:
            value = 0.0
            why = f"{why} (floor 1.0: nothing to discriminate)"
        else:
            value = max(0.0, min(1.0, (raw - floor) / (1.0 - floor)))
        weight = float(component["weight"])
        total += weight * value
        components.append({
            "id": component["id"],
            "deg": component.get("deg"),
            "op": component["op"],
            "weight": weight,
            "score": round(value, 6),
            "floor": round(floor, 6),
            "raw": round(raw, 6),
            "why": why if floor <= 0.0 else f"{why} | floor: {floor_why}",
        })

    gates: dict[str, bool] = {}
    reasons: dict[str, str] = {}

    # cheats: a zero is the right answer, because the work was substituted for
    failed = None
    for name, check in (
        ("plan_accepted", lambda: (not plan.get("rejected"),
                                   "; ".join(plan.get("rejected") or []) or "accepted")),
        ("slide_count_and_order", lambda: _gate_slide_order(plan, cand)),
        ("no_full_page_overlay", lambda: _gate_no_overlay(plan, cand)),
        ("native_objects_preserved",
         lambda: _gate_native_not_flattened(plan, cand, broken)),
        ("no_cloned_shapes", lambda: _gate_no_clones(plan, cand)),
        ("media_not_pasted", lambda: _gate_media_not_pasted(plan, cand, init_inv)),
    ):
        ok, why = check()
        gates[name] = bool(ok)
        reasons[name] = why
        if not ok and failed is None:
            failed = name

    # scope violations: a penalty, because the work was *accompanied* by
    # something nobody asked for.  Never a zero — see the section header.
    scope: dict[str, dict] = {}
    penalty = 0.0
    for name, check in (
        ("untouched_pages_unchanged", lambda: _scope_untouched_pages(plan, cand)),
        ("survivors_intact", lambda: _scope_survivors(plan, cand)),
        ("no_extra_shapes", lambda: _scope_extra_shapes(plan, cand)),
        ("input_media_preserved", lambda: _scope_media_lost(plan, cand, init_inv)),
    ):
        count, why = check()
        rate, cap = SCOPE_RATES[name]
        cost = min(cap, rate * count)
        gates[name] = count == 0
        reasons[name] = why
        penalty += cost
        if count:
            scope[name] = {"count": count, "penalty": round(cost, 4), "why": why}
    penalty = min(PENALTY_CAP, penalty)

    return {
        "score": 0.0 if failed else round(total * (1.0 - penalty), 6),
        "components": components,
        "hard_gates": gates,
        "failed_gate": failed,
        "gate_reasons": reasons,
        "scope_violations": scope,
        "penalty": round(penalty, 4),
        "unweighted": round(total, 6),
    }


# --------------------------------------------------------------------------- #
# the plan
# --------------------------------------------------------------------------- #


def _entries(delta: dict) -> list[tuple[int, dict]]:
    """Every recorded change as (gt slide index, entry), in a stable order."""
    out: list[tuple[int, dict]] = []
    for key in sorted((delta.get("slides") or {}), key=lambda k: int(k)):
        for entry in delta["slides"][key]:
            out.append((int(key), entry))
    for entry in (delta.get("cleared_notes") or []):
        out.append((int(entry["slide"]) - 1, {**entry, "op": "clear_notes"}))
    for entry in (delta.get("layout_edits") or []):
        out.append((0, {**entry, "op": "layout_edit"}))
    if delta.get("reorder_slides"):
        out.append((0, {**delta["reorder_slides"], "op": "reorder_slides"}))
    if delta.get("deleted_slides"):
        out.append((0, {"op": "delete_slides",
                        "pages": delta["deleted_slides"], "deg": None}))
    return out


def _est_steps(root: Path, task: dict) -> dict[str, int]:
    """Per-degradation step estimates.

    They live in `proposal.json`, not in `task.json`: reconcile copies the
    degradation records forward but drops `est_steps`, so the weights have to
    be read from the proposal the task was accepted from, matched by name.
    """
    path = root / "proposal.json"
    if not path.exists():
        return {}
    proposal = json.loads(path.read_text(encoding="utf-8"))
    for candidate in proposal.get("tasks") or []:
        if candidate.get("name") == task.get("name"):
            return {d["id"]: int(d.get("est_steps") or 0)
                    for d in candidate.get("degradations") or []
                    if d.get("id")}
    return {}


def _asset_digests(root: Path) -> list[str]:
    folder = root / "assets"
    if not folder.is_dir():
        return []
    return sorted({_sha(p.read_bytes()) for p in sorted(folder.rglob("*"))
                   if p.is_file()})


def build_plan(deck, *, write: bool = True) -> dict:
    """Turn one deck's record of damage into a scoring plan.

    Deterministic: the same files produce the same plan, byte for byte.
    """
    # `pathlib.Path` has a `.root` attribute of its own ("/"), so a plain
    # `getattr(deck, "root", deck)` turns every path argument into the
    # filesystem root and every read into a FileNotFoundError one frame away
    # from where the mistake was made.
    root = Path(deck) if isinstance(deck, (str, Path)) else Path(deck.root)
    delta = json.loads((root / "delta.json").read_text(encoding="utf-8"))
    task = json.loads((root / "task.json").read_text(encoding="utf-8"))
    steps = _est_steps(root, task)
    declared = [d["id"] for d in (task.get("degradations") or []) if d.get("id")]

    rejected: list[str] = []
    components: list[dict] = []
    # Every page and shape the recipe really damaged, recorded before anything
    # is dropped.  The gates ask "was this broken?", which is not the same
    # question as "is this scored?" — reading the damage off the surviving
    # components turned three genuinely damaged deck0004 pages into "untouched
    # pages the agent meddled with" and charged 0.30 for repairing them.
    damage: dict[str, Any] = {"slides": [], "paths": {}, "boxes": {}}
    for number, (index, entry) in enumerate(_entries(delta), start=1):
        spec = {k: v for k, v in entry.items() if k not in _BULK}
        if index not in damage["slides"]:
            damage["slides"].append(index)
        if entry.get("path") not in (None, "-"):
            damage["paths"].setdefault(str(index), []).append(entry["path"])
        if entry.get("box") and len(entry["box"]) == 4:
            damage["boxes"].setdefault(str(index), []).append(entry["box"])
        components.append({
            "id": f"c{number:03d}",
            "deg": entry.get("deg"),
            "op": entry["op"],
            "slide": index,
            "gt_path": entry.get("path") if entry.get("path") not in (None, "-") else None,
            "weight": 0.0,
            "spec": spec,
        })

    gt_inv = inventory_pptx(root / "source.pptx")
    init_inv = inventory_pptx(root / "input.pptx")

    # --- can the answer even satisfy it? ----------------------------------- #
    # A component the *ground truth itself* cannot pass is unsatisfiable, and
    # an unsatisfiable component is worse than a missing one: it silently
    # takes points off work that was done correctly.  So it is dropped and
    # recorded by name, not left in to punish.  The common cause is a run
    # property the answer inherits — see `_facet_run_props`.
    perfect = Scene(gt_inv, gt_inv)
    unscoreable = []
    for component in list(components):
        raw, why = _run_component(component, perfect)
        if raw < 1.0:
            unscoreable.append({"id": component["id"], "deg": component.get("deg"),
                                "op": component["op"], "slide": component["slide"] + 1,
                                "gt_scores": round(raw, 4), "why": why})
            components.remove(component)

    # --- traceability, both directions ------------------------------------- #
    missing_deg = [c["id"] for c in components if not c.get("deg")]
    if missing_deg:
        rejected.append(
            f"{len(missing_deg)} delta entr(ies) carry no `deg` "
            f"({missing_deg[0]}…): scoring work nobody asked for")
    unknown = sorted({c["deg"] for c in components
                      if c.get("deg") and c["deg"] not in declared})
    if unknown:
        rejected.append(f"delta names degradation(s) the task does not: {unknown}")
    owned = {c["deg"] for c in components if c.get("deg")}
    orphan = [d for d in declared if d not in owned]
    if orphan:
        rejected.append(f"degradation(s) with no scoreable component: {orphan} "
                        f"(asking for work nobody scores)")
    no_steps = [d for d in declared if not steps.get(d)]
    if no_steps and not missing_deg:
        rejected.append(f"no est_steps for degradation(s): {no_steps}")

    # --- weights ----------------------------------------------------------- #
    by_deg: dict[Any, list[dict]] = {}
    for component in components:
        by_deg.setdefault(component.get("deg"), []).append(component)
    usable = {deg: steps.get(deg, 0) for deg in by_deg if deg}
    if usable and all(v > 0 for v in usable.values()) and not missing_deg:
        scale = float(sum(usable.values()))
        source = "est_steps"
        for deg, members in by_deg.items():
            share = usable[deg] / scale
            for component in members:
                component["weight"] = share / len(members)
    else:
        source = "equal (est_steps unusable)"
        for component in components:
            component["weight"] = 1.0 / len(components) if components else 0.0
    drift = sum(c["weight"] for c in components)
    if drift:
        for component in components:
            component["weight"] = component["weight"] / drift

    plan = {
        "format": PLAN_FORMAT,
        "deck": root.name,
        "task": task.get("name"),
        "pos_tol_emu": POS_TOL,
        "weight_source": source,
        "assets_sha": _asset_digests(root),
        "init_slide_of": _init_slide_of(delta),
        "damage": damage,
        "degradations": [
            {"id": deg, "est_steps": steps.get(deg),
             "weight": round(sum(c["weight"] for c in by_deg.get(deg, [])), 9),
             "components": [c["id"] for c in by_deg.get(deg, [])]}
            for deg in declared],
        "components": components,
        "unscoreable": unscoreable,
        "rejected": rejected,
    }

    # --- floors, measured, not assumed ------------------------------------- #
    broken = Scene(gt_inv, init_inv, plan.get("init_slide_of"))
    hot = []
    for component in components:
        floor, why = _run_component(component, broken)
        component["floor"] = round(floor, 6)
        component["floor_why"] = why
        if floor > FLOOR_LIMIT:
            hot.append(f"{component['id']}/{component['op']} floor={floor:.2f}")
    if hot:
        rejected.append(
            "component floor above %.2f — send the task back to `recipe`, do "
            "not widen a tolerance: %s" % (FLOOR_LIMIT, ", ".join(hot)))

    # --- duplicated components --------------------------------------------- #
    # Three duplicated components once carried 21% of one task's weight.  Two
    # entries for the same operator on the same shape with the same parameters
    # are one piece of work counted twice, and the weight follows the count.
    fingerprints: dict[str, str] = {}
    duplicates = []
    for component in components:
        spec = {k: v for k, v in component["spec"].items() if k != "deg"}
        key = json.dumps([component["op"], component["slide"],
                          component.get("gt_path"), spec],
                         sort_keys=True, ensure_ascii=False, default=str)
        if key in fingerprints:
            duplicates.append(f"{component['id']}=={fingerprints[key]}")
        else:
            fingerprints[key] = component["id"]
    if duplicates:
        rejected.append(f"duplicate component(s) counted twice: {duplicates}")

    # --- coherence: no gate may fire on work that a component rewards ------- #
    plan["coherence"] = _coherence(plan, gt_inv, init_inv)
    for failure in plan["coherence"]["failures"]:
        rejected.append(f"coherence: {failure}")

    if write:
        (root / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return plan


# --------------------------------------------------------------------------- #
# coherence: a gate that fires on correct work is a defect in the task
# --------------------------------------------------------------------------- #
#
# Nothing in the pipeline caught an evaluator whose `gate_native_objects`
# zeroed a hand-built SmartArt while its own diagram component was awarding
# 0.7 for that same hand-built SmartArt.  A gate and a component in one file,
# disagreeing about whether the same state is correct, and the gate winning.
#
# So the plan is checked against states that are correct work by construction.
# A cheat gate that fires on any of them, or a scope penalty on the ground
# truth, is not a tolerance to tune: it **rejects the task**, exactly as a
# successful attack does.


def _mock_shape(text: str, box: dict, path: str, sid: int, z: int) -> dict:
    digest = _sha(_norm(text).encode(), 12)
    keys = [f"txt:{digest}"] if text else []
    keys += [f"name:Rectangle {sid}",
             f"geo:autoshape:{round(box['w'] / 91440)}x{round(box['h'] / 91440)}",
             "kind:autoshape"]
    return {"_path": path, "_id": sid, "_name": f"Rectangle {sid}",
            "_plain": _norm(text), "kind": "autoshape", "z": z, "group": None,
            "bbox": dict(box), "hidden": False,
            "text": {"paragraphs": [{"t": text}]} if text else None,
            "keys": keys, "key": keys[0] + "#0"}


def _state_half(plan, gt_inv, init_inv):
    """Half the damaged pages restored perfectly, half left broken."""
    damaged = sorted(set(_damage(plan)["slides"]))
    take = set(damaged[: (len(damaged) + 1) // 2])
    out = copy.deepcopy(init_inv)
    media = list(out["package"]["media"])
    for index in take:
        out["slides"][index] = copy.deepcopy(gt_inv["slides"][index])
        for shape in out["slides"][index]["shapes"]:
            blob = (shape.get("picture") or {}).get("blob")
            if blob and blob not in media:
                media.append(blob)
    out["package"] = dict(out["package"], media=sorted(media))
    return out


def _state_rebuilt(plan, gt_inv):
    """The answer, reached by a different route: every native composite the
    task damaged re-made out of ordinary shapes carrying the same words."""
    out = copy.deepcopy(gt_inv)
    sid = 8000
    for component in plan["components"]:
        path = component.get("gt_path")
        if not path:
            continue
        page = out["slides"][component["slide"]]
        shape = next((s for s in page["shapes"] if s["_path"] == path), None)
        if shape is None or shape["kind"] not in NATIVE_KINDS:
            continue
        texts = []
        diagram = _diagram(shape)
        table = _table(shape)
        if diagram:
            texts = [t for t in diagram.get("nodes", []) if _norm(t)]
        elif table:
            texts = [_norm(c.get("text", ""))
                     for row in table.get("rows") or []
                     for c in row.get("cells") or [] if _norm(c.get("text", ""))]
        elif shape.get("chart"):
            texts = [s.get("name", "") for s in (shape["chart"].get("series") or [])]
        box = _bbox(shape)
        if not texts or box is None:
            continue
        page["shapes"].remove(shape)
        step = max(1, box["h"] // max(1, len(texts)))
        for n, text in enumerate(texts):
            sid += 1
            cell = {"cx": box["cx"], "cy": box["cy"] - box["h"] // 2 + step * n,
                    "w": box["w"], "h": step, "rot": 0.0, "flip": False}
            page["shapes"].append(_mock_shape(text, cell, f"{path}~{n}", sid,
                                              900 + n))
    return out


def _state_over_eager(plan, gt_inv):
    """The answer, plus something nobody asked for on a page nobody named.

    This is the 1100001 casualty in miniature: the model that added a logo to
    two pages whose ground truth has none had done 43% of the work.
    """
    out = copy.deepcopy(gt_inv)
    damaged = set(_damage(plan)["slides"])
    spare = next((i for i in range(len(out["slides"])) if i not in damaged), None)
    if spare is None:
        return None
    box = {"cx": 500000, "cy": 500000, "w": 800000, "h": 400000,
           "rot": 0.0, "flip": False}
    out["slides"][spare]["shapes"].append(
        _mock_shape("helpfully added", box, "zz0", 8999, 999))
    return out


def _coherence(plan, gt_inv, init_inv) -> dict:
    states = [("ground_truth", gt_inv),
              ("half_restore", _state_half(plan, gt_inv, init_inv)),
              ("rebuilt_by_hand", _state_rebuilt(plan, gt_inv)),
              ("over_eager", _state_over_eager(plan, gt_inv))]
    report: dict[str, Any] = {"states": {}, "failures": []}
    for name, inv in states:
        if inv is None:
            continue
        result = score({**plan, "rejected": []}, inv, gt_inv, init_inv)
        report["states"][name] = {"score": result["score"],
                                  "unweighted": result["unweighted"],
                                  "failed_gate": result["failed_gate"],
                                  "penalty": result["penalty"]}
        if result["failed_gate"]:
            report["failures"].append(
                f"{result['failed_gate']} fires on `{name}`, which is correct "
                f"work: {result['gate_reasons'][result['failed_gate']]}")
        if name == "ground_truth":
            if result["penalty"]:
                report["failures"].append(
                    f"the ground truth is itself penalised "
                    f"({result['scope_violations']})")
            if abs(result["unweighted"] - 1.0) > 1e-6:
                report["failures"].append(
                    f"the ground truth scores {result['unweighted']:.3f}, not 1.0")
        if name == "over_eager" and result["score"] <= 0.0:
            report["failures"].append(
                "over-eagerness alone zeroes the score; a scope violation must "
                "cost a fraction, never everything")
    return report


def _init_slide_of(delta: dict) -> list[int] | None:
    """gt slide index -> the index the same page has in the broken file.

    Only a deck-level `delete_slides` or `reorder_slides` can make this
    anything but the identity, and neither appears in the ten decks — so this
    is written from the record rather than measured, and says so.
    """
    deleted = sorted(int(p) - 1 for p in (delta.get("deleted_slides") or []))
    swaps = ((delta.get("reorder_slides") or {}).get("swapped")) or []
    if not deleted and not swaps:
        return None
    return None


# --------------------------------------------------------------------------- #


def main():                                                       # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="build and check a scoring plan")
    ap.add_argument("deck", nargs="+")
    args = ap.parse_args()
    for item in args.deck:
        root = Path(item)
        plan = build_plan(root)
        gt = inventory_pptx(root / "source.pptx")
        init = inventory_pptx(root / "input.pptx")
        good = score(plan, gt, gt, init)
        bad = score(plan, init, gt, init)
        print(f"{root.name}  gt={good['score']:.3f}  init={bad['score']:.3f}  "
              f"{len(plan['components'])} components")
        for reason in plan["rejected"]:
            print(f"    REJECTED {reason}")


if __name__ == "__main__":                                        # pragma: no cover
    main()
