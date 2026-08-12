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
moves **0.0% of shapes** on open-and-save across all ten decks (docs/design/reward.md
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

docs/design/reward.md §3③ ("judge relations, not absolute coordinates") is the right
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
  the machine happens to have (docs/design/reward.md §2.4)

Both are dropped from every comparison and from the untouched-page gate.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Callable

from .inventory import inventory_pptx
from .matching import (
    Scene,
    Target,
    Unscorable,
    _ORDER_MARGIN,
    _ORDER_EVIDENCE,
    _WEAK_KEY_PREFIXES,
    _overlap as _matching_overlap,
    _page_signature,
    bbox as _bbox,
    boxes_meet as _boxes_meet,
    centre_ok as _centre_ok,
    extent_ok as _extent_ok,
    is_strong as _is_strong,
    norm as _norm,
    page_is_itself as _page_is_itself,
    pair_slide,
    pair_slide_detail,
)

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

_STEP_DEG_RE = re.compile(r"\b(d\d+)\b")
_STEP_N_RE = re.compile(
    r"~\s*(\d+)"
    r"|\b(?:about|roughly|around|approximately|approx\.?)\s+(\d+)\b"
    r"|\b(\d+)\s+steps?\b", re.I)



# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #




def _frac(hit: int, total: int) -> float:
    return 1.0 if total <= 0 else hit / float(total)


def _overlap(a: set, b: set) -> float:
    return _matching_overlap(a, b)


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
        out.append(tuple(sorted((str(e.get("class") or ""),
                                 str(e.get("preset") or ""),
                                 str(e.get("subtype") or ""),
                                 str(e.get("target_key") or ""),
                                 str(e.get("trigger") or ""),
                                 str(e.get("dur_ms") or ""),
                                 str(e.get("motion_path") or ""),
                                 str(e.get("path_edit_mode") or ""),
                                 str(e.get("repeat") or ""),
                                 str(e.get("auto_reverse") or ""))
                                for e in step.get("effects", []))))
    return out


def _transition_facts(slide: dict | None) -> tuple:
    tr = (slide or {}).get("transition")
    if not tr:
        return (None, None)
    detail = tr.get("detail") or {}
    return (tr.get("type"), tuple(sorted(detail.items())))




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
        # font sets on one box (docs/design/reward.md §2.4).  Judging it scores noise.
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


def _resolve_colour(token: Any, theme: dict) -> Any:
    """`scheme:ACCENT1` -> `srgb:4472C4`, where the theme says so and only then.

    docs/design/reward.md §1's first example of equivalence, and a measured one: a
    candidate that wrote out the sRGB a colour picker reports for the theme
    colour it was told to restore scored **0.00** on that component while
    every pixel of the slide was right.  Resolution is refused for a colour
    carrying `lumMod` / `shade` / `alpha` modifiers — that arithmetic is the
    renderer's, and guessing at it would make two different colours look the
    same, which is the direction that gives a cheat somewhere to hide.
    """
    if not theme or not isinstance(token, str) or "+" in token:
        return token
    if not token.startswith("scheme:"):
        return token
    rgb = theme.get(token.split(":", 1)[1].lower())
    return f"srgb:{rgb}" if rgb else token


def _theme_of(scene: "Scene | None") -> dict:
    """The ground truth's dictionary, used for both sides.

    Both, deliberately: the question is whether the candidate's colour *is*
    the answer's colour, and the answer's theme is what names it.
    """
    if scene is None:
        return {}
    return (scene.gt.get("package") or {}).get("theme_colors") or {}


def _same_colour(a: Any, b: Any, theme: dict) -> bool:
    return a == b or _resolve_colour(a, theme) == _resolve_colour(b, theme)


def _runs_match(want_runs: list[dict], mine_runs: list[dict],
                props: tuple[str, ...], theme: dict | None = None
                ) -> tuple[int, int]:
    """(properties matched, properties the answer states) over one run bundle.

    Runs are paired by **their words first and their index second**.  A
    paragraph retyped in the application comes back split differently — three
    runs where the original had one, or the reverse — and an index-only pairing
    reads that as every property missing on a paragraph whose visible state is
    exactly right.  Only properties the answer states explicitly are counted;
    the rest are inherited and not decidable here (see `_facet_run_props`).
    """
    hit = 0
    total = 0
    for index, run in enumerate(want_runs):
        peer = mine_runs[index] if index < len(mine_runs) else None
        if peer is None or peer.get("t") != run.get("t"):
            peer = next((m for m in mine_runs if m.get("t") == run.get("t")),
                        peer)
        for prop in props:
            if run.get(prop) is None:
                continue              # inherited in the answer: not decidable
            total += 1
            if peer is None:
                continue
            if prop == "color":
                if _same_colour(peer.get(prop), run.get(prop), theme or {}):
                    hit += 1
            elif peer.get(prop) == run.get(prop):
                hit += 1
    return hit, total


def _facet_run_props(gt_shape: dict, shape: dict | None,
                     props: tuple[str, ...],
                     theme: dict | None = None) -> tuple[float, str]:
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
        got, want_n = _runs_match(runs, have.get(address, []), props, theme)
        hit += got
        total += want_n
    if total == 0:
        raise Unscorable(
            f"the ground truth states none of [{'+'.join(props)}] explicitly on "
            f"this shape — it inherits them, and requiring an *absent* "
            f"attribute is not reachable through any application's UI")
    return _frac(hit, total), f"runs {hit}/{total} [{'+'.join(props)}]"


def _same_stops(a: Any, b: Any, theme: dict) -> bool:
    """A gradient's stops, colour by colour.

    The `color` key covers a solid fill and nothing else — a gradient carries
    a `stops` list, so the whole-dict comparison decided it and `_same_colour`
    was never asked.  deck0005's `c003` holds a gradient whose stops mix
    `scheme:TX1`, which resolves, with `scheme:ACCENT1+lumMod+lumOff`, which
    `_resolve_colour` refuses to resolve and which any writer therefore has to
    leave alone on both sides.  It cost the `colour_written_out` legitimate
    variant 1.9% of that deck the moment group members started being scored at
    all.
    """
    if a is None and b is None:
        return True
    if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b):
        return False
    return all(_same_colour(x, y, theme) for x, y in zip(a, b))


def _facet_fill(gt_shape: dict, shape: dict | None,
                theme: dict | None = None) -> tuple[float, str]:
    want = gt_shape.get("fill")
    have = (shape or {}).get("fill")
    if want == have:
        return 1.0, "fill"
    theme = theme or {}
    if (isinstance(want, dict) and isinstance(have, dict)
            and {k: v for k, v in want.items() if k not in ("color", "stops")}
            == {k: v for k, v in have.items() if k not in ("color", "stops")}
            and _same_colour(want.get("color"), have.get("color"), theme)
            and _same_stops(want.get("stops"), have.get("stops"), theme)):
        return 1.0, "fill (theme colour written out)"
    return 0.0, "wrong fill"


def _facet_line(gt_shape: dict, shape: dict | None,
                theme: dict | None = None) -> tuple[float, str]:
    """Outline compared the way `_facet_fill` compares fill.

    The `theme` argument is docs/design/reward.md §1's equivalence, and it is not
    hypothetical here: the `colour_written_out` legitimate variant — the same
    answer with every theme colour written as the sRGB a colour picker reports
    — dropped deck0002 to 0.951 and deck0009 to 0.947 the moment the outline
    became a scored facet of a restored connector, because `scheme:TX1` and
    `srgb:000000` are the same black.  `_resolve_colour` refuses the resolution
    wherever the modifiers make it a guess, so this widens nothing a cheat can
    stand in.
    """
    want, mine = _line_facts(gt_shape), _line_facts(shape)
    if want == mine:
        return 1.0, "outline"
    if (want[0] == mine[0] and want[2:] == mine[2:]
            and _same_colour(want[1], mine[1], theme or {})):
        return 1.0, "outline (theme colour written out)"
    return 0.0, "wrong outline"


def _facet_picture(gt_shape: dict, shape: dict | None) -> tuple[float, str]:
    """Exact bytes, and **multiplied by** the geometry in `_cmp_restored_shape`.

    Both halves of that are deliberate, and this is the note for whoever comes
    back to widen it, because the cost is real and measured.  An application
    that re-encodes on save — LibreOffice does it to every image, and a rollout
    has already been seen with the `.pptx` handler bound to Impress — takes a
    **perfect** deck to 0.637 (deck0006), 0.739 (deck0001), 0.784 (deck0010),
    0.561 (deck0003), 0.643 (deck0008), 0.705 (deck0007). Every point of that
    is this facet: the picture is in exactly the right place at exactly the
    right size and scores 0 for its whole component.

    **The multiply is right.**  Averaging content with geometry would pay half
    a component for a picture-shaped rectangle of the *wrong* image in the
    right place, and "paste something roughly there" being cheaper than
    restoring the thing is the move a training run finds first — the same
    arithmetic that made an earlier version of `_cmp_restored_shape` award
    0.667 to a shape that was out of place, over-sized, repainted and re-worded.
    The fragility is not in the multiply. It is that **the only identity this
    file has for an image is its exact bytes**.

    **No tolerance is built, and none may be, until the inventory records
    something else.**  `inventory._keys` and `_picture_of` record the blob
    digest and the crop — no pixel dimensions, no format, no perceptual hash —
    so there is nothing to fall back to. The bar docs/design/reward.md §4 sets is that a
    tolerance must not pay a cheat, and here the cheat is concrete: swap in a
    *different* image and call it re-encoding. Dimensions and format would not
    stop it (a different photo of the same size passes both), so the only
    discriminator that would is a perceptual hash — and that needs a decoder
    for what the corpus actually contains, measured over the ten decks' slide
    pictures: **177 PNG, 73 JPEG, 29 EMF, 19 GIF, 15 TIFF, 5 WMF**. 49 of those
    are vector metafiles with no pixels at all until something renders them,
    and this module is stdlib-only by contract because `emit` pastes it into a
    task file that runs where "`python-pptx` may not be installed"
    (`tests/test_inventory.py::test_the_embedded_runtime_imports_nothing_but_the
    _standard_library`). Pillow is a dependency of the pipeline and cannot be
    one of this file.

    So the cost of leaving it is bounded and priced above; the cost of a
    tolerance built on anything weaker than a perceptual hash is the whole task
    family. **And the premise is not live**: a real WPS open-and-save re-encodes
    0/51 blobs on deck0003 and 0/29 on deck0008, and the four blobs it does
    rewrite on deck0004 and the two on deck0005 belong to masters, layouts and
    EMF/SVG alternates — not to a picture shape on a slide — so the real
    WPS-saved ground truth of both scores 1.0000. If the file starts passing
    through LibreOffice, the fix is the environment, not a number here.
    """
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


def _facet_connector(t: "Target", gt_shape: dict, shape: dict | None,
                     ends: tuple[str, ...] = ("start", "end")
                     ) -> tuple[float, str]:
    """Attachment compared by *which shape* each end holds, not by shape id.

    `a:stCxn/@id` is a shape id, and a shape redrawn through the GUI has a new
    one.  Resolving the id to a shape and then asking whether that shape is the
    same shape scores the outcome.

    `ends` is the ends the operator actually detached.  Three of deck0002 p5's
    connectors are drawn lines with no `a:stCxn` at all, and deck0004 p3's are
    attached at one end only — so scoring both ends regardless awards the
    untouched end 0.5 on the *broken* file, which is a measured floor of
    **0.375** and rejects the task.  An end nobody detached is not this
    comparator's business.
    """
    want = gt_shape.get("connector")
    if want is None:
        raise Unscorable("gt shape is not a connector")
    if not ends:
        raise Unscorable("no connector end was recorded as detached")
    mine = (shape or {}).get("connector") or {}
    by_id_gt = {s["_id"]: s for s in t.gt_slide["shapes"]}
    other_slide = t.slide or {"shapes": []}
    by_id_now = {s["_id"]: s for s in other_slide["shapes"]}
    hit = 0
    for end in ends:
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
    return _frac(hit, len(ends)), f"attachments {hit}/{len(ends)}"


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

    **Only the peers the move actually passed.**  Sending a shape to the back
    inverts its order with the shapes it was in front of and leaves every
    other pair exactly as it was — and those untouched pairs are satisfied by
    the *broken* file, which measured a floor of **0.27** on deck0001 p8 and
    rejected the task.  The direction is in the record (`to`), so the damaged
    set is derivable without looking at the broken file.
    """
    gt = t.gt_shape
    shape = t.shape
    if shape is None:
        return 0.0, "shape absent"
    siblings = t.gt_siblings(gt)
    to = t.spec.get("to")
    if to == "back":
        siblings = [s for s in siblings if s["z"] < gt["z"]]
    elif to in ("front", "top"):
        siblings = [s for s in siblings if s["z"] > gt["z"]]
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


@comparator("delete", "blank_slide", "drop_equation")
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
    **A shape is never scored on its bounding box alone.**  The `what` facets
    above are all drawn from the ground-truth shape's *own* content, and a
    **group carries none of them** — its content is its children.  So `what`
    came out empty, the function fell through to `return _blend(where)`, and
    the component became a pure box test: 44 of the 229 `delete` components in
    the ten-deck corpus had the ground-truth `why` string `position=1.00 ·
    size=1.00` and nothing else.  Measured, on `input.pptx` plus one empty
    shape of the right kind at the right coordinates per such component:
    **deck0003 0.4035, deck0005 0.3906**, deck0004 0.1889, deck0006 0.1875,
    deck0009 0.1279, deck0002 0.0685 — no gate firing on any of them.  The two
    worst are the most valuable components in their decks (deck0003 `c005` at
    0.2632 is a four-member group whose picture was never compared; deck0005
    `c003` at 0.2651 is a sixteen-member group carrying that deck's largest
    single chunk of declared work).  Drawing an empty rectangle is the cheapest
    action there is, which makes it the move a training run finds first.

    So when nothing the shape *holds* is comparable, `_identity_facets` asks
    what the shape *is* instead — its members, its preset geometry, its
    outline, its effect style.  Every one of those is something the hollow
    shape does not have, and none of them is `kind` or the box: a facet the
    cheat already satisfies would pay it a share rather than deny it one.
    """
    return _restored(t, t.gt_shape, t.shape, 0)


#: How far `_facet_members` will follow a container's children.  Three is
#: deeper than any group nesting in the corpus and stops a cycle in a malformed
#: inventory from being an infinite loop rather than a bad score.
MEMBER_DEPTH = 3

_NO_LINE = (None, None, None, None, None)


def _gt_members(t: "Target", gt: dict) -> list[dict]:
    """The direct children of a container, as the inventory flattens them.

    `inventory` writes group members as ordinary slide shapes whose `_path` is
    the parent's plus one more segment, and whose `bbox` is already resolved
    into slide space by `_group_matrix` — so a child can be paired and placed
    exactly like any other shape.
    """
    prefix = gt["_path"] + "/"
    return [s for s in t.gt_slide["shapes"]
            if s["_path"].startswith(prefix)
            and "/" not in s["_path"][len(prefix):]]


def _facet_members(t: "Target", gt: dict, depth: int) -> tuple[float, str]:
    """A container scored on its children, each by the same rubric as a shape.

    The average, not the count: half a group's members back is half the
    component.  A group that pairs but holds nothing scores 0, which is what an
    absent group scores — the rule the fix exists to enforce.
    """
    members = _gt_members(t, gt)
    if not members:
        raise Unscorable("gt shape has no members")
    scores = [_restored(t, kid, t.counterpart(kid), depth + 1)[0]
              for kid in members]
    hit = sum(scores)
    return (hit / len(members),
            f"members {hit:.2f}/{len(members)}")


def _facet_geom(gt_shape: dict, shape: dict | None) -> tuple[float, str]:
    want = gt_shape.get("geom")
    if not want:
        raise Unscorable("gt shape records no preset geometry")
    return ((1.0, f"geometry {want.get('prst')}")
            if want == (shape or {}).get("geom") else (0.0, "wrong geometry"))


def _facet_effects(gt_shape: dict, shape: dict | None) -> tuple[float, str]:
    want = _effect_facts(gt_shape)
    if want == ((), None):
        raise Unscorable("gt shape carries no effect")
    return ((1.0, "effects") if want == _effect_facts(shape)
            else (0.0, "wrong effects"))


def _facet_equation(gt_shape: dict, shape: dict | None) -> tuple[float, str]:
    want = gt_shape.get("equation") or {}
    have = (shape or {}).get("equation") or {}
    if not want.get("native") or not want.get("structure"):
        raise Unscorable("gt shape carries no native equation")
    if not have.get("native"):
        return 0.0, "not a native equation"
    text = 1.0 if want.get("text") == have.get("text") else 0.0
    structure = 1.0 if want.get("structure") == have.get("structure") else 0.0
    return _blend([(1.0, text, "symbols"), (2.0, structure, "structure")])


def _identity_facets(t: "Target", gt: dict, shape: dict | None,
                     depth: int) -> list[tuple[float, float, str]]:
    """What a shape *is*, for a shape that holds nothing.

    Deliberately excludes `kind` and the bounding box.  Both are already true
    of an empty shape drawn in the right place, so scoring them would hand the
    hollow-shape cheat a share of the component instead of denying it one.
    Connector *attachment* is excluded too: `detach` is its own operator with
    its own comparator, and a connector redrawn by hand without re-anchoring
    its ends is a legitimate restoration of a deleted line.
    """
    out: list[tuple[float, float, str]] = []
    if depth < MEMBER_DEPTH and _gt_members(t, gt):
        out.append((3.0, *_facet_members(t, gt, depth)))
    if gt.get("geom"):
        out.append((1.0, *_facet_geom(gt, shape)))
    if _line_facts(gt) != _NO_LINE:
        out.append((1.0, *_facet_line(gt, shape, _theme_of(t.scene))))
    if _effect_facts(gt) != ((), None):
        out.append((1.0, *_facet_effects(gt, shape)))
    return out


def _restored(t: "Target", gt: dict, shape: dict | None,
              depth: int) -> tuple[float, str]:
    """`_cmp_restored_shape` for one shape — the component's own, or a member."""
    if shape is None:
        return _facet_rebuilt_composite(t, gt, depth)
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
    if gt.get("equation"):
        what.append((3.0, *_facet_equation(gt, shape)))
    if gt.get("fill"):
        what.append((1.0, *_facet_fill(gt, shape, _theme_of(t.scene))))
    if not what:
        what = _identity_facets(t, gt, shape, depth)

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


def _composite_texts(gt_shape: dict) -> list[str]:
    """The words a native composite carries, in the order a rebuild lays out.

    One list for the three kinds, because "rebuilt out of ordinary shapes"
    means the same thing to all three: the words are on the page, in boxes,
    where the object was.

    **Only** the three, and this empty list is the whole of the guard: it is
    what says a deleted *picture* cannot be put back by typing its caption
    where it stood.  Nothing else about a shape — not its name, not its own
    `_plain` — is a word a rebuild has to reproduce.
    """
    diagram = _diagram(gt_shape)
    if diagram:
        return [t for t in (_norm(n) for n in diagram.get("nodes") or ()) if t]
    table = _table(gt_shape)
    if table:
        return [t for t in (_norm(cell.get("text", ""))
                            for row in table.get("rows") or ()
                            for cell in row.get("cells") or ()) if t]
    chart = gt_shape.get("chart")
    if chart:
        return [t for t in (_norm(s.get("name", ""))
                            for s in chart.get("series") or ()) if t]
    return []


def _facet_rebuilt_composite(t: "Target", gt: dict,
                             depth: int = 0) -> tuple[float, str]:
    """A native composite that pairs with nothing, looked for as a **rebuild**.

    Pairing is one-to-one and a rebuild is one-to-many: a SmartArt redrawn as
    five ordinary boxes leaves the gt graphic frame matched to nothing, and
    `_cmp_restored_shape` read that as "shape absent" and scored 0.  On
    deck0007 — the one deck whose instruction asks for a rebuild in as many
    words — that cost the hand-rebuilt answer three of its five components and
    **0.5897 of a perfect score**, with no gate firing.  The same event twice:
    `_gate_native_not_flattened` had already been narrowed so it would not zero
    a rebuild, on the stated grounds that "whether the rebuild is good enough
    is the component's judgement" — and the component was judging it 0.
    `_facet_diagram_all`'s comment claimed the node text was looked for among
    the slide's shapes, and no code did it.

    What is asked is the content, in the place the object stood:

    * the shapes considered are the ones that pair with **no** gt shape, so
      nothing already scored as itself can be counted twice, and
    * each has to sit inside the composite's own box (`DAMAGE_MARGIN`, the
      same budget `_scope_extra_shapes` gives new furniture), so retyping the
      words in a corner of the page earns nothing, and
    * each word is **consumed** by the first box that carries it, so one box
      holding all five node texts scores one fifth, not five fifths.

    This cannot be the cheap route.  Drawing N boxes and typing N labels into
    the hole is strictly more work than putting the object back, so nothing is
    made easier by taking it; and on the broken file the object and its words
    are simply gone, so the component's measured floor stays 0 and floor
    normalisation would cancel the credit if it did not.
    """
    # A **container** has no words of its own, and the route that matters for
    # it is the mirror image of the one above: the members put back but not
    # re-grouped.  That is the `ungrouped` legitimate variant, it is strictly
    # more work than restoring the group, and every member still has to be
    # right individually — so it is paid, while the empty group of the right
    # shape that pairs with the component is not (`_identity_facets`).
    if depth < MEMBER_DEPTH and _gt_members(t, gt):
        value, why = _facet_members(t, gt, depth)
        return value, f"{gt['kind']} absent, {why} restored loose"
    want = _composite_texts(gt)
    box = _bbox(gt)
    if not want or box is None:
        return 0.0, "shape absent"
    region = [box["cx"] - box["w"] // 2, box["cy"] - box["h"] // 2,
              box["w"], box["h"]]
    claimed = {id(s) for s in t.scene.pairs(t.index).values() if s is not None}
    loose = [s for s in (t.slide or {}).get("shapes", [])
             if id(s) not in claimed and _bbox(s)
             and _inside(_bbox(s), region) and _norm(s.get("_plain") or "")]
    if not loose:
        return 0.0, "shape absent"
    spent: set[int] = set()
    hit = 0
    for text in want:
        for n, other in enumerate(loose):
            if n in spent or _norm(other.get("_plain") or "") != text:
                continue
            spent.add(n)
            hit += 1
            break
    return (_frac(hit, len(want)),
            f"rebuilt out of {len(loose)} shape(s): {hit}/{len(want)} of the "
            f"{gt['kind']}'s words, in its place")


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
    return _facet_run_props(t.gt_shape, t.shape, props,
                            _theme_of(t.scene))


@comparator("text_runs")
def _cmp_text_runs(t: Target) -> tuple[float, str]:
    """Only the paragraphs the step touched, addressed by gt index.

    Indices are read off the *pristine* deck, so a step that deleted a
    paragraph does not shift the addresses of the ones it did not.

    **A restyle is scored on the style, and on nothing else.**  The first
    version paid 0.5 for a paragraph whose *text* was there and whose
    properties were wrong — but a restyle never touches the text, so that half
    is a term the broken file satisfies by construction.  Measured on
    deck0008 p3: floor **0.50**, which is over `FLOOR_LIMIT` by itself and
    rejects every restyle task ever written.  A paragraph the step *deleted*
    is the opposite case: there the text is exactly what went missing, and
    there is no style to compare.

    **A restyle the answer does not state is not scored at all.**  It returned
    1.0 for it, unconditionally, which made the floor 1.0, which made
    `score` report "nothing to discriminate" — and the **ground truth scored
    0.000**.  A property the answer inherits is not decidable here
    (`_facet_run_props` says why); the honest move is `Unscorable`, so
    `build_plan` drops the component and the degradation that owns it is
    reported as unscoreable instead of silently paying everybody.
    """
    touched = t.spec.get("touched") or []
    if not touched:
        raise Unscorable("text_runs recorded no touched paragraph")
    params = t.spec.get("params") or {}
    props = tuple(prop for key, prop in _PROP_OF_PARAM.items() if key in params)
    gt_paras = (t.gt_shape.get("text") or {}).get("paragraphs") or []
    have = ((t.shape or {}).get("text") or {}).get("paragraphs") or []
    hit = 0.0
    total = 0
    inherited = 0
    for item in touched:
        index = item.get("paragraph")
        if index is None or index >= len(gt_paras):
            raise Unscorable(f"touched paragraph {index} is not in the gt shape")
        want = gt_paras[index]
        text = _norm(want.get("t", ""))
        mine = next((p for p in have if _norm(p.get("t", "")) == text), None)
        if item.get("action") in ("deleted", "emptied") or not props:
            # the paragraph itself is what went missing: its presence is the
            # fact, and there is no style term to weigh it against.
            total += 1
            hit += 1.0 if (mine is not None and text) else 0.0
            continue
        got, stated = _runs_match(want.get("runs", []),
                                  (mine or {}).get("runs", []), props,
                                  _theme_of(t.scene))
        if stated == 0:
            inherited += 1
            continue
        total += 1
        hit += got / float(stated)
    if total == 0:
        raise Unscorable(
            f"the ground truth states none of [{'+'.join(props)}] on the "
            f"{inherited} paragraph(s) this step restyled — it inherits them, "
            f"and requiring an *absent* attribute is not reachable through any "
            f"application's UI")
    return (_frac(hit, total),
            f"paragraphs {hit:.1f}/{total}"
            + (f" ({inherited} inherited, not scoreable)" if inherited else ""))


# ---- style family --------------------------------------------------------- #


@comparator("recolor")
def _cmp_recolor(t: Target) -> tuple[float, str]:
    return _facet_fill(t.gt_shape, t.shape, _theme_of(t.scene))


@comparator("outline")
def _cmp_outline(t: Target) -> tuple[float, str]:
    return _facet_line(t.gt_shape, t.shape, _theme_of(t.scene))


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
        parts.append((2.0, *_facet_fill(t.gt_shape, t.shape,
                                        _theme_of(t.scene))))
    return _blend(parts)


@comparator("crop")
def _cmp_crop(t: Target) -> tuple[float, str]:
    return _facet_crop(t.gt_shape, t.shape)


#: how the executor names a connector end -> the inventory's name for it.
_CXN_END_OF_TAG = {"a:stCxn": "start", "a:endCxn": "end"}


@comparator("detach_connector")
def _cmp_detach(t: Target) -> tuple[float, str]:
    gt = t.gt_shape
    recorded = t.spec.get("was_attachments")
    ends = tuple(end for tag, end in _CXN_END_OF_TAG.items()
                 for item in (recorded or ()) if tag in item)
    parts: list[tuple[float, float, str]] = []
    if ends:
        parts.append((3.0, *_facet_connector(t, gt, t.shape, ends)))
    elif recorded is None:
        # an entry that predates the record: fall back to both ends and let
        # the measured floor say whether that is scoreable on this deck.
        parts.append((3.0, *_facet_connector(t, gt, t.shape)))
    if _bbox(gt) and t.spec.get("nudge_in", 0.4):
        parts.append((1.0, *_facet_centre(gt, t.shape)))
    if not parts:
        raise Unscorable("the connector was attached at neither end and was "
                         "not nudged: nothing was damaged to restore")
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


#: Restoring one of two dropped rows leaves the table one row short, and a
#: comparator that opens with `if n_rows != want: return 0` scores that
#: **exactly the same as doing nothing** — the all-or-nothing rubric this
#: module was written to stop, measured: two rows dropped from deck0009 p5,
#: one put back, score 0.000.
#:
#: Absolute row indices cannot replace the guard either, and for the same
#: reason: a row that is still missing shifts every row behind it, so the one
#: that *was* put back sits one place early and reads as absent.  What the
#: task actually asks is that each lost row is back **in its place among the
#: rows that survived**, which is an order question, not an index one.
def _lines_restored(want_lines: list, mine_lines: list,
                    damaged: set) -> tuple[int, int, bool, bool]:
    """(lost lines back, lost lines worth asking about, in order, survivors ok).

    Lines are matched by their cells, greedily and in gt order.  `in order` is
    the anti-cheat half: a table whose rows carry the right words in the wrong
    sequence is not a repaired table, and appending the missing row at the
    bottom is the cheapest wrong answer there is.
    """
    used = [False] * len(mine_lines)
    keyed = [tuple(line) for line in mine_lines]
    at: dict[int, int] = {}
    for index, line in enumerate(want_lines):
        key = tuple(line)
        if not any(key):
            continue                      # a blank row/column says nothing
        for other, mine in enumerate(keyed):
            if not used[other] and mine == key:
                used[other] = True
                at[index] = other
                break
    seq = [at[index] for index in sorted(at)]
    ordered = all(a < b for a, b in zip(seq, seq[1:]))
    asked = [index for index in sorted(damaged)
             if index < len(want_lines) and any(want_lines[index])]
    hit = sum(1 for index in asked if index in at)
    survivors = all(index in at for index, line in enumerate(want_lines)
                    if index not in damaged and any(line))
    return hit, len(asked), ordered, survivors


def _all_rows(table: dict | None) -> list[list[str]]:
    if not table:
        return []
    return [[_norm(c.get("text", "")) for c in row.get("cells") or []]
            for row in table.get("rows") or []]


def _all_cols(table: dict | None) -> list[list[str]]:
    rows = _all_rows(table)
    width = max((len(r) for r in rows), default=0)
    return [[row[c] if c < len(row) else "" for row in rows]
            for c in range(width)]


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
    for item in removed:
        if _row_texts(want, item["row"]) is None:
            raise Unscorable(f"gt table has no row {item['row']}")
    hit, asked, ordered, kept = _lines_restored(
        _all_rows(want), _all_rows(mine), {i["row"] for i in removed})
    if not asked:
        raise Unscorable("every dropped row was empty: nothing to tell apart")
    raw = _frac(hit, asked)
    fault = ("" if ordered else " (rows out of order)") \
        + ("" if kept else " (survivors lost)")
    return ((raw if ordered and kept else 0.0), f"rows {hit}/{asked}{fault}")


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
    for item in removed:
        if not (0 <= item["col"] < (want.get("n_cols") or 0)):
            raise Unscorable(f"gt table has no column {item['col']}")
    hit, asked, ordered, kept = _lines_restored(
        _all_cols(want), _all_cols(mine), {i["col"] for i in removed})
    if not asked:
        raise Unscorable("every dropped column was empty: nothing to tell apart")
    raw = _frac(hit, asked)
    fault = ("" if ordered else " (columns out of order)") \
        + ("" if kept else " (survivors lost)")
    return ((raw if ordered and kept else 0.0), f"cols {hit}/{asked}{fault}")


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
    if chart_part:
        for shape in charts:
            if (shape.get("chart") or {}).get("_part") == chart_part:
                return shape
    if len(charts) == 1:
        return charts[0]
    if not charts:
        raise Unscorable("no chart on the gt slide")
    raise Unscorable(f"cannot identify the chart (part {chart_part!r}, "
                     f"{len(charts)} on the slide)")


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
    """Every **displaced** gt page has to be back, in its place, itself.

    Not every page in the deck.  Swapping two pages of nineteen leaves
    seventeen where they were, and those seventeen are satisfied by the broken
    file: measured floor **0.89** on deck0001, which rejects the task for a
    component that discriminates perfectly on the two pages that moved.  A
    page too thin to tell from its neighbours is dropped as well — it answers
    `True` for everybody, which is the same free credit by another route.
    """
    gt_pages = t.scene.gt["slides"]
    others = t.scene.other["slides"]
    moved = _displaced(t.spec, len(gt_pages))
    judged = [i for i in moved
              if len(_page_signature(gt_pages[i])) >= _ORDER_EVIDENCE]
    if not judged:
        raise Unscorable(
            "no page this step displaced carries enough text to be told apart "
            "from its neighbours")
    hit = sum(1 for index in judged
              if index < len(others)
              and _page_is_itself(gt_pages, others[index], index))
    return _frac(hit, len(judged)), f"pages {hit}/{len(judged)}"


def _displaced(spec: dict, n_pages: int) -> list[int]:
    """The gt page indices the deck-level edit moved out of place.

    A swap moves exactly the pages named.  A deletion moves the deleted page
    *and everything after it*, because the pages behind it all shift forward.
    """
    swapped = spec.get("swapped") or []
    if swapped:
        return sorted({int(page) - 1 for pair in swapped for page in pair
                       if 0 < int(page) <= n_pages})
    pages = [int(p) - 1 for p in (spec.get("pages") or [])]
    if pages:
        return [i for i in range(min(pages), n_pages)]
    return list(range(n_pages))


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
#: named.  These are the left column of docs/design/reward.md §1 — noise, not equivalence —
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
#: penalty for changes nobody made — docs/design/reward.md §5's `roundtrip_identity` probe,
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


def _page_facts(slide: dict, address: list[str]) -> dict:
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

    **`address` is the caller's, and it is required, not defaulted.**  A
    default would be `shape["key"]`, and `shape["key"]` is the whole bug.  The
    value below records
    *that* a shape draws an image rather than which bytes, because the blob is
    the application's to change — and `shape["key"]`, the address this used to
    file every fact at, **is that same blob**: `inventory._keys` gives a
    picture `pic:<digest>` as its strongest identity and `_number` turns it
    into `pic:279bd563df27c9f8#0`.  So re-encoding did not change a value here,
    it moved every fact about the shape to a new address, and the comparison
    below read one untouched picture as a deletion plus an addition.  Thirteen
    such "changes" on deck0003 and six on deck0008, each hitting the 0.30 cap:
    a perfect deck scoring 0.393 and 0.450 for something no agent did.

    The intent was already right; only the addressing defeated it.  So the
    address is now supplied by `_scope_untouched_pages` from the **pairing** —
    the same `pair_slide_detail` every component is scored through — and both
    sides of a comparison name the same shape by the same name.  No tolerance
    is involved: a shape that pairs is compared fact by fact exactly as before,
    and a shape that pairs with nothing still reads as a deletion, which is
    what it is.

    What it does give up, stated rather than discovered later: on a page the
    task never named, **one picture swapped for a different picture of the same
    size in the same place is now invisible here**.  It was only ever caught by
    accident — by the blob being in the address — and catching it on purpose
    needs the inventory to record something about an image that survives
    re-encoding, which is the same missing thing `_facet_picture` documents.
    It earns nothing either way; it only stops costing 0.10 of a page.  What
    still fires on it: `_gate_media_not_pasted` if the intruder is one of the
    ground truth's own blobs, `input_media_preserved` if the package ends up
    with fewer image parts, and every geometric fact below if it is not a
    pixel-perfect stand-in.
    """
    shapes = slide.get("shapes", [])
    out: dict[str, Any] = {}
    for shape, name in zip(shapes, address):
        at = f"shapes[{name}]"
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
        a = _page_facts(page, [s["_path"] for s in page["shapes"]])
        b = _page_facts(other, _mirrored_paths(scene, index, other["shapes"]))
        keys = [k for k in set(a) | set(b) if a.get(k, _MISS) != b.get(k, _MISS)]
        if keys:
            hits.append(f"slide {index + 1}: {sorted(keys)[0]}")
    return len(hits), "; ".join(hits[:3]) or "untouched pages unchanged"


def _mirrored_paths(scene: Scene, index: int, shapes: list[dict]) -> list[str]:
    """Name each shape on the candidate page after the gt shape it *is*.

    An address is a claim about identity, and the only identity this file
    trusts anywhere else is `pair_slide_detail`'s.  Reusing it here is what
    stops a picture whose bytes the application rewrote from reading as a
    deletion plus an addition (`_page_facts`), and it costs nothing in
    detection: a shape that pairs is still compared fact by fact, and a shape
    that pairs with **nothing** gets a `+`-prefixed address no gt fact is filed
    at, so it still reads as an addition — as does the gt shape it failed to
    match, as a deletion.  A gt `_path` never contains `+`, and the pairing is
    one-to-one, so no two shapes can collide on one address.
    """
    mate = {shape["_path"]: path
            for path, (shape, _key) in scene.detail(index).items()
            if shape is not None}
    return [mate.get(s["_path"], "+" + s["_path"]) for s in shapes]


def _scope_survivors(plan, scene: Scene) -> tuple[int, str]:
    """Shapes on a damaged page that the damage did not touch, now gone.

    A **group whose children are all still there** is not gone: ungrouping is
    a thing applications offer and it draws exactly the same page.  Charging
    for the container cost the `ungrouped` variant 0.06 on two decks and the
    full 0.30 cap on deck0006, which dissolves 28 of them — with every
    component still scoring 1.00.  A group that really took its children with
    it is still charged, and so is each child.
    """
    targets = {int(k): set(v) for k, v in _damage(plan)["paths"].items()}
    hits = []
    for index, paths in sorted(targets.items()):
        pairs = scene.pairs(index)
        gone = {s["_path"] for s in scene.gt_slide(index)["shapes"]
                if pairs.get(s["_path"]) is None}
        for shape in scene.gt_slide(index)["shapes"]:
            path = shape["_path"]
            if path in paths or any(path.startswith(p + "/") for p in paths):
                continue
            if pairs.get(path) is not None:
                continue
            if shape["kind"] == "group" and not any(
                    other.startswith(path + "/") for other in gone):
                continue                  # dissolved, not lost
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
        unmatched = _unmatched(scene, index)
        for shape in unmatched:
            if shape.get("group"):
                continue
            if _is_empty_container(shape, scene, index, unmatched):
                continue
            box = _bbox(shape)
            if box is None:
                continue
            if not any(_inside(box, region) for region in regions):
                hits.append(f"slide {index + 1}: {shape['kind']}")
    return len(hits), "; ".join(hits[:3]) or "no extra shapes"


def _is_empty_container(shape: dict, scene: Scene, index: int,
                        unmatched: list[dict]) -> bool:
    """A group that holds only shapes the answer has is not new furniture.

    Grouping the shapes you have just put back is a thing applications invite
    you to do, and it draws nothing: the group's own box is its children's
    extent, and every child is scored on its own.  Charging for the container
    cost a candidate 0.12 whose every restored shape was exact — a penalty for
    tidiness.  A group holding something the answer does *not* have still pays:
    its unmatched children are the evidence, not the box around them.
    """
    if shape.get("kind") != "group":
        return False
    prefix = shape["_path"] + "/"
    kids = [s for s in (scene.slide(index) or {}).get("shapes", [])
            if s["_path"].startswith(prefix)]
    if not kids:
        return False
    surplus = {id(s) for s in unmatched}
    return not any(id(kid) in surplus for kid in kids)


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


STEP_DISAGREEMENT = 3.0
DECLARATION_SPLIT = 0.25


def _planning_module():
    # Function-local so the standalone emitted scorer has no planning/runtime
    # dependency. Generated tasks call score(), never build_plan().
    from . import planning
    return planning


def _entries(delta: dict) -> list[tuple[int, dict]]:
    return _planning_module()._entries(delta)


def _est_steps(root: Path, task: dict) -> dict[str, int]:
    return _planning_module()._est_steps(root, task)


def _agrees(part: float, whole: float | None) -> bool:
    return _planning_module()._agrees(part, whole)


def _num_or_none(value: Any) -> int | None:
    return _planning_module()._num_or_none(value)


def _measured_steps(root: Path,
                    declared: list[str]) -> tuple[dict[str, int], str, bool]:
    return _planning_module()._measured_steps(root, declared)


def _asset_digests(root: Path) -> list[str]:
    return _planning_module()._asset_digests(root)


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


def _state_position_slip(plan, gt_inv):
    """The answer, every restored shape twice the tolerance out of place.

    Not correct work and not scored as such — a **sensitivity reading**, and
    the one number deck0009 needed and nobody had.  Its `c016` is a deleted
    table worth **0.3934**, and the instruction gives all 27 of its cells
    verbatim, so the content half is fully determined.  The other half is
    `_facet_centre`, which is binary, and the bundle discloses that centre
    nowhere — the solvability probe wrote *"any sensible placement in the empty
    left half must be accepted"* in prose and passed the deck anyway.  A
    perfect table placed **0.02 in** out therefore scores the deck 0.7377, and
    so does one placed a foot out: past the tolerance the test says nothing
    about how far.

    **The instrument is not the defect.**  Binary is what stops "paste it
    roughly there" being cheaper than restoring the thing, which is the failure
    `_cmp_restored_shape` was rewritten to prevent, and 14 components across
    the corpus are scored `content × position` — on all but this one the
    position is disclosed by a masked reference, a twin slide or the shape's
    own surviving neighbours.  Grading the centre by distance to rescue
    deck0009 would loosen the other thirteen and pay `wrong_params`, which
    moves things.  The fix belongs where the coordinate is missing: the deck
    ships a reference render for slide 10, or the probe marks that degradation
    `determinate: false` instead of writing the freedom down in prose.

    What this adds is that the exposure is now a number in `plan.json` on every
    deck, at build time, instead of a sentence in a report nobody re-read.
    """
    out = copy.deepcopy(gt_inv)
    moved = 0
    for component in plan["components"]:
        path = component.get("gt_path")
        if component["op"] not in ("delete", "blank_slide") or not path:
            continue
        page = out["slides"][component["slide"]]
        for shape in page["shapes"]:
            if (shape["_path"] == path or shape["_path"].startswith(path + "/")) \
                    and shape.get("bbox"):
                shape["bbox"]["cx"] += 2 * POS_TOL
                moved += 1
    return out if moved else None


def _coherence(plan, gt_inv, init_inv) -> dict:
    return _planning_module()._coherence(plan, gt_inv, init_inv)


def _init_slide_of(delta: dict, n_pages: int) -> list[int | None] | None:
    return _planning_module()._init_slide_of(delta, n_pages)


def build_plan(deck, *, write: bool = True) -> dict:
    # Preserve the historical injection point used by diagnostics and tests.
    return _planning_module().build_plan(
        deck, write=write, init_slide_of=_init_slide_of)


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
