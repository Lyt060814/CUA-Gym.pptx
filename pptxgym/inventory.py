"""A semantic inventory of a `.pptx`, built with the standard library alone.

This is the substrate the reward stage compares against.  It is deliberately
**stdlib-only** — `zipfile` + `xml.etree.ElementTree` and nothing else — because
the comparator ends up pasted verbatim into a task file that runs inside a
benchmark harness where `python-pptx` may not be installed and where the task
has to be readable as one self-contained file.  The validated precedent is
`task_1170003.py` in the rollout repo, which does exactly this for charts; this
module keeps its shape and generalises it to everything `degrade_exec.REGISTRY`
can damage.

What it has to see
------------------
An operator whose effect is not in the inventory can never be scored.  Every
family the executor touches therefore has a home here:

    geometry            shape.bbox / rot / flip          move scatter resize swap
    z-order, grouping   shape.z / shape.group            zorder ungroup blank_slide
    fill line effects   shape.fill / line / effects      recolor outline strip_effects
    text + run format   shape.text / runs                clear_text set_text set_font
                                                         text_runs
    picture             shape.picture.crop / blob        crop delete
    table               shape.table.rows[].cells[]       clear_table_cells
                                                         table_drop_rows/cols
    SmartArt            shape.diagram.nodes / edges      smartart (partial edits)
    chart               shape.chart.series[].values      chart_edit
    connectors          shape.connector.start / end      detach_connector
    slide order         package.slide_order              reorder_slides delete_slide
    layout reference    slide.layout, inventory.layouts  layout (delete_paths)
    notes               slide.notes                      clear_notes
    animation           slide.animation.steps            strip_animation
                                                         anim_drop_steps
    transition          slide.transition                 strip_transition

Semantic, not byte-level
------------------------
Two files that mean the same thing must produce the same inventory.  The
normalisations below were each paid for by a false positive somewhere upstream;
none of them is a guess:

* `_norm_fill` — `None` (inherited) and `<a:noFill/>` are one absence written
  two ways.  soffice writes the explicit form where the original said nothing.
* `_norm_geom` — an explicit `prst="rect"` is the implicit default.
* line width — recorded as `None` when the shape has no line at all, so a
  comparator can apply the rule that only two *present* lines may be compared.
  soffice resolves an inherited default into an explicit `9360` on shapes that
  never had a border; telling that apart from "a border was added" needs theme
  inheritance resolved, which this module does not attempt (see `_color`).
* runs — adjacent runs whose formatting is identical are merged.  A GUI retype
  splits a paragraph into different runs for the same visible result.
* text — runs inside a paragraph are contiguous character data.  Joining them
  with a space invents words (`@ olivier_pourret`, `deposits ?`); paragraphs and
  `a:br` are the only real separators.  See `census.element_text`.

Application-generated text
--------------------------
A date / slide-number / footer / header placeholder holds whatever the
application put there.  Two different questions get two different answers:

* *matching* — a placeholder is keyed on the **role** it plays (`ph:dt#2`),
  which is unique on a slide and survives whatever the app writes into it.
  Keying on the text made 81 untouched placeholders read as deleted-plus-added.
* *comparing* — the text is exempted **only where the shape really holds an
  `a:fld`**, in which case it is filed under `generated_text` instead of `text`
  so a comparator can skip it by name.  Exempting on the role alone hid 40
  rewritten footers, which authors do type by hand.

Identity
--------
Shape ids are not identity: an agent who deletes a shape and draws it again
gets a new id and a new name.  Each shape therefore carries an ordered list of
candidate keys, strongest first — placeholder role, image blob, composite kind,
text digest, shape name, geometry class — plus an occurrence index among the
shapes on that slide sharing its primary key.  `match_shapes` pairs two slides
by the strongest key both sides agree on, falling back to nearest centre for
twins, which is how `roundtrip.compare` learned to pair repeated content.

Entry points
------------
    inventory_pptx(path) -> dict      the inventory
    flatten(inventory)   -> dict      dotted path -> value, shapes keyed by identity
    diff(a, b)           -> list      the dotted paths where two inventories differ
    match_shapes(sa, sb) -> list      (shape_a | None, shape_b | None) pairs
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Any, Iterable

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "dsp": "http://schemas.microsoft.com/office/drawing/2008/diagram",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}

EMU_PER_INCH = 914400

# placeholder roles the application fills in for itself.  Their *role* is the
# match key; their text is only exempt from comparison when an `a:fld` is
# actually present — see the module docstring.
APP_FILLED = {"dt", "sldNum", "ftr", "hdr"}

CHART_TYPES = {
    "area3DChart", "areaChart", "bar3DChart", "barChart", "bubbleChart",
    "doughnutChart", "line3DChart", "lineChart", "ofPieChart", "pie3DChart",
    "pieChart", "radarChart", "scatterChart", "stockChart", "surface3DChart",
    "surfaceChart",
}


def q(tag: str) -> str:
    prefix, local = tag.split(":")
    return f"{{{NS[prefix]}}}{local}"


SHAPE_TAGS = {q("p:sp"), q("p:pic"), q("p:graphicFrame"), q("p:cxnSp"),
              q("p:grpSp")}
R_ID = q("r:id")
R_EMBED = q("r:embed")
R_DM = q("r:dm")
R_LO = q("r:lo")


# --------------------------------------------------------------------------- #
# package plumbing
# --------------------------------------------------------------------------- #


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _sha(data: bytes, n: int = 16) -> str:
    return hashlib.sha256(data).hexdigest()[:n]


def _resolve(part: str, target: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(part), target))


def _rels_name(part: str) -> str:
    head, _, tail = part.rpartition("/")
    return f"{head}/_rels/{tail}.rels" if head else f"_rels/{tail}.rels"


def _xml(z: zipfile.ZipFile, part: str) -> ET.Element:
    return ET.fromstring(z.read(part))


def _relationships(z: zipfile.ZipFile, part: str) -> dict[str, dict[str, str]]:
    name = _rels_name(part)
    if name not in z.namelist():
        return {}
    out: dict[str, dict[str, str]] = {}
    for node in _xml(z, name).findall("pr:Relationship", NS):
        rid = node.attrib.get("Id")
        if not rid:
            continue
        target = node.attrib.get("Target", "")
        external = node.attrib.get("TargetMode", "Internal") == "External"
        out[rid] = {
            "type": node.attrib.get("Type", "").rsplit("/", 1)[-1],
            "target": target,
            "resolved": target if external else _resolve(part, target),
            "external": external,
        }
    return out


def _slide_no(part: str) -> int:
    m = re.search(r"(\d+)\.xml$", part)
    return int(m.group(1)) if m else 0


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #


def _norm_fill(value):
    """`None` and "none" are the same absence written two ways."""
    return None if value in (None, "none", "noFill") else value


def _norm_geom(value):
    """soffice writes an explicit rect where the original left it implicit."""
    return None if value in (None, "rect") else value


def _norm_text(value: str) -> str:
    return " ".join((value or "").split())


def _flag(value) -> bool | None:
    if value is None:
        return None
    return value not in ("0", "false", "False")


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _color(parent: ET.Element | None) -> str | None:
    """A colour as a token: `srgb:FF0000`, `scheme:accent1`, ...

    Theme colours are *not* resolved to RGB.  Resolving them needs the theme
    part and the shape's style matrix, and a half-resolved comparison is worse
    than an unresolved one: it makes `scheme:accent1` and the sRGB the renderer
    baked out of it look like a change when they are the same colour, and hides
    the reverse.  A comparator that needs RGB equivalence has to resolve both
    sides itself; this records what the file says.
    """
    if parent is None:
        return None
    for name, attr in (("srgbClr", "val"), ("schemeClr", "val"),
                       ("sysClr", "lastClr"), ("prstClr", "val"),
                       ("hslClr", "hue"), ("scrgbClr", "r")):
        node = parent.find(f".//a:{name}", NS)
        if node is not None:
            short = {"srgbClr": "srgb", "schemeClr": "scheme", "sysClr": "sys",
                     "prstClr": "prst", "hslClr": "hsl", "scrgbClr": "scrgb"}[name]
            value = node.attrib.get(attr) or node.attrib.get("val") or ""
            mods = sorted(_local(child.tag) for child in node)
            token = f"{short}:{value.upper()}"
            return f"{token}+{'+'.join(mods)}" if mods else token
    return None


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #


_RUN_ATTRS = (("sz", "sz"), ("b", "b"), ("i", "i"), ("u", "u"),
              ("strike", "strike"), ("cap", "cap"), ("baseline", "baseline"))
_RUN_DEFAULTS = {"strike": ("noStrike",), "u": ("none",), "cap": ("none",),
                 "baseline": ("0",)}


def _run_props(rpr: ET.Element | None) -> dict[str, Any]:
    """The formatting of one run, in the form `set_font` / `text_runs` write."""
    if rpr is None:
        return {}
    out: dict[str, Any] = {}
    for attr, key in _RUN_ATTRS:
        value = rpr.attrib.get(attr)
        if value is None or value in _RUN_DEFAULTS.get(attr, ()):
            # soffice writes the default out in full — `strike="noStrike"` on
            # 1032 table runs of one deck — where the original said nothing.
            # `b="0"` is *not* in this table: it is what `set_font bold=false`
            # writes, and inheritance can make it a real change.
            continue
        out[key] = _int(value, value) if attr in ("sz", "spc", "baseline") else value
    latin = rpr.find("a:latin", NS)
    if latin is not None and latin.attrib.get("typeface"):
        out["font"] = latin.attrib["typeface"]
    fill = next((child for child in rpr if _local(child.tag).endswith("Fill")), None)
    if fill is not None:
        colour = _color(fill)
        out["color"] = colour if colour else _norm_fill(_local(fill.tag))
    if rpr.find("a:hlinkClick", NS) is not None:
        out["link"] = True
    return out


def _para_runs(para: ET.Element) -> list[dict[str, Any]]:
    """Runs of a paragraph, merged where formatting is identical.

    Run boundaries are a fiction of the writer — a spell-check tag or one
    bolded character splits a word in two — so equal-formatted neighbours are
    joined back together.  `a:br` is a real boundary and stays.  `a:fld` is a
    run whose text the application generates; it keeps its field type.
    """
    runs: list[dict[str, Any]] = []

    def push(text: str, props: dict[str, Any], field: str | None):
        if not text:
            # a run with no characters draws nothing.  soffice emits them
            # freely — a fully-styled empty run after every real one — and they
            # would otherwise read as run-level formatting changes.
            return
        item = {"t": text, **props}
        if field:
            item["fld"] = field
        if runs and not field and not runs[-1].get("fld"):
            previous = runs[-1]
            if {k: v for k, v in previous.items() if k != "t"} == \
               {k: v for k, v in item.items() if k != "t"}:
                previous["t"] += text
                return
        runs.append(item)

    for child in para:
        tag = _local(child.tag)
        if tag == "r":
            node = child.find("a:t", NS)
            push(node.text or "" if node is not None else "",
                 _run_props(child.find("a:rPr", NS)), None)
        elif tag == "fld":
            node = child.find("a:t", NS)
            push(node.text or "" if node is not None else "",
                 _run_props(child.find("a:rPr", NS)),
                 child.attrib.get("type") or "fld")
        elif tag == "br":
            push("\n", _run_props(child.find("a:rPr", NS)), None)
    end = para.find("a:endParaRPr", NS)
    if end is not None:
        # the paragraph's trailing run properties are not an `a:rPr`.  A probe
        # found a heading "degraded" to plain black still carrying sz=4800 b=1
        # in here; leaving it out of the inventory leaves that unscorable.
        props = _run_props(end)
        if props:
            runs.append({"t": "", "end": True, **props})
    return runs


def _para_props(para: ET.Element) -> dict[str, Any]:
    ppr = para.find("a:pPr", NS)
    if ppr is None:
        return {}
    out: dict[str, Any] = {}
    for attr in ("algn", "lvl", "marL", "indent", "rtl"):
        if ppr.attrib.get(attr) is not None:
            out[attr] = _int(ppr.attrib[attr], ppr.attrib[attr])
    bullet = next((_local(child.tag) for child in ppr
                   if _local(child.tag).startswith("bu")
                   and _local(child.tag) != "buSzPct"), None)
    if bullet:
        out["bullet"] = bullet
    return out


def _paragraph_text(para: ET.Element) -> str:
    """The text a reader sees, not the text the XML happens to be cut into."""
    chunk = []
    for node in para.iter():
        tag = _local(node.tag)
        if tag == "t" and node.text:
            chunk.append(node.text)
        elif tag == "br":
            chunk.append("\n")
    return "".join(chunk)


def _text_body(el: ET.Element) -> ET.Element | None:
    for name in ("p:txBody", "a:txBody", "p:text"):
        try:
            found = el.find(name, NS)
        except SyntaxError:                                       # pragma: no cover
            found = None
        if found is not None:
            return found
    return None


def _body_props(body: ET.Element) -> dict[str, Any]:
    bpr = body.find("a:bodyPr", NS)
    if bpr is None:
        return {}
    out: dict[str, Any] = {}
    for attr, default in (("anchor", "t"), ("wrap", "square"), ("rot", "0"),
                          ("vert", "horz")):
        value = bpr.attrib.get(attr)
        # `anchor="t"` and `wrap="square"` are the defaults spelled out; soffice
        # writes the first onto 86 boxes of three decks and drops the second
        if value is not None and value != default:
            out[attr] = value
    fit = next((_local(child.tag) for child in bpr
                if _local(child.tag).endswith("AutoFit")), None)
    if fit:
        # the size of an autofit box is the application's arithmetic, not the
        # agent's doing; recording the mode lets a comparator drop it.
        out["autofit"] = fit
    return out


def _shape_text(el: ET.Element) -> dict[str, Any] | None:
    body = _text_body(el)
    if body is None:
        return None
    paragraphs = []
    for para in body.findall("a:p", NS):
        entry: dict[str, Any] = {"t": _paragraph_text(para)}
        entry.update(_para_props(para))
        runs = _para_runs(para)
        if runs:
            entry["runs"] = runs
        paragraphs.append(entry)
    return {"paragraphs": paragraphs, **_body_props(body)}


def _plain_text(text: dict[str, Any] | None) -> str:
    if not text:
        return ""
    return _norm_text(" ".join(p.get("t", "") for p in text["paragraphs"]))


def _has_field(el: ET.Element) -> str | None:
    for node in el.iter(q("a:fld")):
        return node.attrib.get("type") or "fld"
    return None


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mul(m1, m2):
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
            a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)


def _apply(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def _read_xfrm(el: ET.Element) -> dict[str, Any] | None:
    """The shape's own transform, wherever its element type keeps it."""
    xfrm = None
    if el.tag == q("p:graphicFrame"):
        xfrm = el.find("p:xfrm", NS)
    if xfrm is None:
        for holder in ("p:spPr", "p:grpSpPr"):
            parent = el.find(holder, NS)
            if parent is not None:
                xfrm = parent.find("a:xfrm", NS)
                if xfrm is not None:
                    break
    if xfrm is None:
        return None
    off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
    if off is None or ext is None:
        return None
    ch_off, ch_ext = xfrm.find("a:chOff", NS), xfrm.find("a:chExt", NS)
    return {
        "off": (_int(off.attrib.get("x"), 0), _int(off.attrib.get("y"), 0)),
        "ext": (_int(ext.attrib.get("cx"), 0), _int(ext.attrib.get("cy"), 0)),
        "chOff": None if ch_off is None else (_int(ch_off.attrib.get("x"), 0),
                                              _int(ch_off.attrib.get("y"), 0)),
        "chExt": None if ch_ext is None else (_int(ch_ext.attrib.get("cx"), 0),
                                              _int(ch_ext.attrib.get("cy"), 0)),
        "rot": _int(xfrm.attrib.get("rot"), 0) / 60000.0,
        "flipH": xfrm.attrib.get("flipH") == "1",
        "flipV": xfrm.attrib.get("flipV") == "1",
    }


def _group_matrix(xf: dict[str, Any], m):
    """child-space -> slide-space for one group, composed onto `m`."""
    ox, oy = xf["off"]
    ew, eh = xf["ext"]
    ch_off = xf["chOff"] or (ox, oy)
    ch_ext = xf["chExt"] or (ew, eh)
    sx = ew / ch_ext[0] if ch_ext[0] else 1.0
    sy = eh / ch_ext[1] if ch_ext[1] else 1.0
    local = _mul((1.0, 0.0, 0.0, 1.0, ox, oy),
                 _mul((sx, 0.0, 0.0, sy, 0.0, 0.0),
                      (1.0, 0.0, 0.0, 1.0, -ch_off[0], -ch_off[1])))
    rad = math.radians(xf["rot"])
    spin_core = _mul((math.cos(rad), math.sin(rad), -math.sin(rad),
                      math.cos(rad), 0.0, 0.0),
                     (-1.0 if xf["flipH"] else 1.0, 0.0, 0.0,
                      -1.0 if xf["flipV"] else 1.0, 0.0, 0.0))
    cx, cy = ox + ew / 2.0, oy + eh / 2.0
    spin = _mul((1.0, 0.0, 0.0, 1.0, cx, cy),
                _mul(spin_core, (1.0, 0.0, 0.0, 1.0, -cx, -cy)))
    return _mul(m, _mul(spin, local))


def _absolute(xf: dict[str, Any] | None, m) -> dict[str, Any] | None:
    """Slide-absolute geometry.

    Absolute is the semantic level: `ungroup` re-writes every child's local
    coordinates so that nothing moves on screen, and a comparator working in
    local coordinates would read a dozen phantom moves.  Here it reads as what
    it is — a change of grouping.
    """
    if xf is None:
        return None
    ox, oy = xf["off"]
    ew, eh = xf["ext"]
    cx, cy = _apply(m, ox + ew / 2.0, oy + eh / 2.0)
    a, b, c, d, _, _ = m
    sx, sy = math.hypot(a, b), math.hypot(c, d)
    det = 1.0 if (a * d - b * c) >= 0 else -1.0
    rot = (math.degrees(math.atan2(b, a)) + det * xf["rot"]) % 360.0
    return {
        "cx": int(round(cx)), "cy": int(round(cy)),
        "w": int(round(ew * sx)), "h": int(round(eh * sy)),
        "rot": round(rot, 2),
        "flip": bool((det < 0) ^ xf["flipH"] ^ xf["flipV"]),
    }


# --------------------------------------------------------------------------- #
# style
# --------------------------------------------------------------------------- #


def _sp_pr(el: ET.Element) -> ET.Element | None:
    for name in ("p:spPr", "p:grpSpPr"):
        found = el.find(name, NS)
        if found is not None:
            return found
    return None


def _fill_kind(node: ET.Element | None) -> str | None:
    """`solid` / `grad` / `blip` / `patt` / `group`, or `None` for no fill.

    `<a:noFill/>` and no fill element at all are one absence written two ways.
    """
    if node is None:
        return None
    name = _local(node.tag)
    return None if _norm_fill(name) is None else name[: -len("Fill")]


def _fill_of(parent: ET.Element | None, blobs: dict[str, str]) -> dict[str, Any] | None:
    if parent is None:
        return None
    node = next((child for child in parent
                 if _local(child.tag).endswith("Fill")), None)
    kind = _fill_kind(node)
    if kind is None:
        return None                       # inherited — same absence as noFill
    out: dict[str, Any] = {"type": kind}
    if kind == "solid":
        out["color"] = _color(node)
    elif kind == "grad":
        out["stops"] = [_color(gs) for gs in node.findall("a:gsLst/a:gs", NS)]
        lin = node.find("a:lin", NS)
        if lin is not None:
            out["angle"] = _int(lin.attrib.get("ang"))
    elif kind == "patt":
        out["prst"] = node.attrib.get("prst")
        out["fg"] = _color(node.find("a:fgClr", NS))
        out["bg"] = _color(node.find("a:bgClr", NS))
    elif kind == "blip":
        blip = node.find("a:blip", NS)
        rid = blip.attrib.get(R_EMBED) if blip is not None else None
        out["blob"] = blobs.get(rid or "")
    return out


def _line_of(parent: ET.Element | None) -> dict[str, Any] | None:
    """The outline.

    `w` is `None` whenever the shape has no explicit line: a comparator may
    only compare widths when both sides actually have one.  soffice resolves an
    inherited default into an explicit `9360` on shapes that never had a
    border — 37 of them on one deck, every one representation rather than
    change — and this module does not resolve theme inheritance, so it stays
    quiet rather than guessing.
    """
    if parent is None:
        return None
    node = parent.find("a:ln", NS)
    if node is None:
        return None
    fill = next((child for child in node
                 if _local(child.tag).endswith("Fill")), None)
    kind = _fill_kind(fill)
    if fill is not None and kind is None:
        # `<a:ln><a:noFill/></a:ln>` *is* "no outline", and so is no `a:ln` at
        # all: one absence written two ways, one level up from `_norm_fill`.
        # soffice writes the explicit form on 119 shapes of three decks that
        # never had a border, and `outline mode=remove` refuses to file a delta
        # entry for removing one — so neither may read as a change here.
        return None
    dash = node.find("a:prstDash", NS)
    head, tail = node.find("a:headEnd", NS), node.find("a:tailEnd", NS)
    return {
        "w": _int(node.attrib.get("w")),
        "cap": node.attrib.get("cap"),
        "cmpd": node.attrib.get("cmpd"),
        "fill": kind,
        "color": _color(fill) if fill is not None else None,
        "dash": dash.attrib.get("val") if dash is not None else None,
        "head": head.attrib.get("type") if head is not None else None,
        "tail": tail.attrib.get("type") if tail is not None else None,
    }


def _effects_of(el: ET.Element, sp: ET.Element | None) -> dict[str, Any] | None:
    out: dict[str, Any] = {}
    if sp is not None:
        for holder in ("a:effectLst", "a:effectDag"):
            node = sp.find(holder, NS)
            if node is not None and len(node):
                out.setdefault("effects", []).extend(
                    sorted(_local(child.tag) for child in node))
        for holder in ("a:sp3d", "a:scene3d"):
            if sp.find(holder, NS) is not None:
                out.setdefault("effects", []).append(holder.split(":")[1])
    style = el.find("p:style", NS)
    if style is not None:
        refs = {}
        for name in ("a:fillRef", "a:lnRef", "a:effectRef", "a:fontRef"):
            ref = style.find(name, NS)
            if ref is None:
                continue
            idx = ref.attrib.get("idx")
            # `idx="0"` is "no theme fill / line / effect" and `fontRef
            # idx="minor"` is the body font: both are the default written out.
            # soffice adds a whole p:style block saying exactly that to plain
            # text boxes that never had one.  A *non-zero* effectRef is the
            # theme shadow `strip_effects` has to zero, and that is what stays.
            if idx in (None, "0") or (name == "a:fontRef" and idx == "minor"):
                continue
            refs[name.split(":")[1]] = idx
        if refs:
            out["style_ref"] = refs
    if out.get("effects"):
        out["effects"] = sorted(out["effects"])
    return out or None


def _geom_of(sp: ET.Element | None) -> dict[str, Any] | None:
    if sp is None:
        return None
    prst = sp.find("a:prstGeom", NS)
    if prst is not None:
        adjust = {node.attrib.get("name"): node.attrib.get("fmla")
                  for node in prst.findall("a:avLst/a:gd", NS)}
        value = _norm_geom(prst.attrib.get("prst"))
        if value is None and not adjust:
            return None
        return {"prst": value, **({"adj": adjust} if adjust else {})}
    cust = sp.find("a:custGeom", NS)
    if cust is not None:
        paths = cust.findall("a:pathLst/a:path", NS)
        return {"prst": "custom", "n_paths": len(paths),
                "n_commands": sum(len(path) for path in paths)}
    return None


# --------------------------------------------------------------------------- #
# composite content
# --------------------------------------------------------------------------- #


def _table_of(el: ET.Element) -> dict[str, Any] | None:
    tbl = el.find("a:graphic/a:graphicData/a:tbl", NS)
    if tbl is None:
        return None
    grid = [_int(node.attrib.get("w"), 0)
            for node in tbl.findall("a:tblGrid/a:gridCol", NS)]
    pr = tbl.find("a:tblPr", NS)
    rows = []
    for tr in tbl.findall("a:tr", NS):
        cells = []
        for tc in tr.findall("a:tc", NS):
            cell: dict[str, Any] = {"text": _plain_text(_shape_text(tc))}
            body = tc.find("a:txBody", NS)
            if body is not None:
                runs = [run for para in body.findall("a:p", NS)
                        for run in _para_runs(para)]
                # run formatting inside cells is a `set_font` target: deck0009
                # de-bolds two compound codes inside one table
                if any(len(run) > 1 for run in runs):
                    cell["runs"] = runs
            for attr in ("gridSpan", "rowSpan", "hMerge", "vMerge"):
                if tc.attrib.get(attr):
                    cell[attr] = _int(tc.attrib[attr], tc.attrib[attr])
            fill = _fill_of(tc.find("a:tcPr", NS), {})
            if fill:
                cell["fill"] = fill
            cells.append(cell)
        rows.append({"h": _int(tr.attrib.get("h"), 0), "cells": cells})
    return {
        "n_rows": len(rows), "n_cols": len(grid), "col_w": grid,
        "first_row": pr.attrib.get("firstRow") if pr is not None else None,
        "banded": pr.attrib.get("bandRow") if pr is not None else None,
        "rows": rows,
    }


def _cache_values(parent: ET.Element | None) -> list[str]:
    if parent is None:
        return []
    values: list[tuple[int, str]] = []
    for point in parent.findall(".//c:pt", NS):
        index = _int(point.attrib.get("idx"), len(values))
        node = point.find("c:v", NS)
        values.append((index, "" if node is None or node.text is None else node.text))
    return [value for _, value in sorted(values)]


def _series_summary(series: ET.Element) -> dict[str, Any]:
    def formula(kind: str) -> str:
        node = series.find(f"c:{kind}//c:f", NS)
        return "" if node is None or node.text is None else node.text

    tx = series.find("c:tx", NS)
    name = ""
    if tx is not None:
        cached = _cache_values(tx.find(".//c:strCache", NS))
        if cached:
            name = cached[0]
        else:
            node = tx.find("c:v", NS)
            name = "" if node is None or node.text is None else node.text
    cat = series.find("c:cat", NS)
    if cat is None:
        cat = series.find("c:xVal", NS)
    categories: list[str] = []
    if cat is not None:
        for cache in ("strCache", "numCache", "multiLvlStrCache"):
            categories = _cache_values(cat.find(f".//c:{cache}", NS))
            if categories:
                break
    values_parent = series.find("c:val", NS)
    if values_parent is None:
        values_parent = series.find("c:yVal", NS)
    marker = series.find("c:marker/c:symbol", NS)
    points = []
    for point in series.findall("c:dPt", NS):
        index = point.find("c:idx", NS)
        explosion = point.find("c:explosion", NS)
        points.append({
            "index": None if index is None else _int(index.attrib.get("val"), 0),
            "fill": _color(point.find("c:spPr", NS)),
            "line": _color(point.find("c:spPr/a:ln", NS)),
            "explosion": 0 if explosion is None else _int(explosion.attrib.get("val"), 0),
        })
    idx, order = series.find("c:idx", NS), series.find("c:order", NS)
    return {
        "idx": None if idx is None else _int(idx.attrib.get("val"), 0),
        "order": None if order is None else _int(order.attrib.get("val"), 0),
        "name": name,
        "cat_formula": formula("cat") or formula("xVal"),
        "val_formula": formula("val") or formula("yVal"),
        # the cache is what renders; the embedded workbook may be stale
        "categories": categories,
        "values": _cache_values(values_parent.find(".//c:numCache", NS))
        if values_parent is not None else [],
        "fill": _color(series.find("c:spPr", NS)),
        "line": _color(series.find("c:spPr/a:ln", NS)),
        "marker": "" if marker is None else marker.attrib.get("val", ""),
        "points": points,
    }


def _workbook_signature(payload: bytes) -> dict[str, Any]:
    """A digest of the embedded workbook's *cells*, not of its bytes.

    Re-saving a workbook rewrites calcChain, styles and print settings; the
    numbers are what the task is about.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as book:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in book.namelist():
                shared = ["".join(node.itertext())
                          for node in list(ET.fromstring(book.read("xl/sharedStrings.xml")))]
            cells: list[tuple[str, str, str, str]] = []
            for name in sorted(item for item in book.namelist()
                               if re.match(r"^xl/worksheets/[^/]+\.xml$", item)):
                for cell in ET.fromstring(book.read(name)).iter():
                    if _local(cell.tag) != "c":
                        continue
                    kind = cell.attrib.get("t", "")
                    formula = next((child.text or "" for child in cell
                                    if _local(child.tag) == "f"), "")
                    value = next((child.text or "" for child in cell
                                  if _local(child.tag) == "v"), "")
                    if kind == "s" and value.isdigit() and int(value) < len(shared):
                        value = shared[int(value)]
                    if value or formula:
                        cells.append((name, cell.attrib.get("r", ""), formula, value))
            return {"status": "ok", "cells": len(cells),
                    "sha": _sha(json.dumps(cells, ensure_ascii=False,
                                           separators=(",", ":")).encode())}
    except Exception as error:                                    # noqa: BLE001
        return {"status": "invalid", "error": type(error).__name__,
                "sha": _sha(payload)}


def _chart_of(z: zipfile.ZipFile, part: str) -> dict[str, Any]:
    root = _xml(z, part)
    chart = root.find("c:chart", NS)
    plot_area = chart.find("c:plotArea", NS) if chart is not None else None
    groups = ([node for node in list(plot_area) if _local(node.tag) in CHART_TYPES]
              if plot_area is not None else [])
    series: list[dict[str, Any]] = []
    signatures = []
    for group in groups:
        series.extend(_series_summary(item) for item in group.findall("c:ser", NS))
        signatures.append({
            "type": _local(group.tag),
            **{key: (group.find(f"c:{key}", NS).attrib.get("val")
                     if group.find(f"c:{key}", NS) is not None else None)
               for key in ("barDir", "grouping", "radarStyle", "scatterStyle")},
        })
    legend = chart.find("c:legend", NS) if chart is not None else None
    legend_pos = legend.find("c:legendPos", NS) if legend is not None else None
    labels = plot_area.findall(".//c:dLbls", NS) if plot_area is not None else []
    axes = []
    for node in (list(plot_area) if plot_area is not None else []):
        kind = _local(node.tag)
        if kind not in {"catAx", "dateAx", "serAx", "valAx"}:
            continue
        fmt = node.find("c:numFmt", NS)
        low, high = node.find("c:scaling/c:min", NS), node.find("c:scaling/c:max", NS)
        axes.append({
            "kind": kind,
            "title": _norm_text(" ".join(n.text or "" for n in node.findall("c:title//a:t", NS))),
            "min": None if low is None else low.attrib.get("val"),
            "max": None if high is None else high.attrib.get("val"),
            "gridlines": node.find("c:majorGridlines", NS) is not None,
            "num_fmt": None if fmt is None else fmt.attrib.get("formatCode"),
        })
    rels = _relationships(z, part)
    books = sorted(rel["resolved"] for rel in rels.values()
                   if not rel["external"] and rel["type"] == "package")
    title = chart.find("c:title", NS) if chart is not None else None
    style = root.find("c:style", NS)
    first = next((group.find("c:firstSliceAng", NS) for group in groups
                  if group.find("c:firstSliceAng", NS) is not None), None)
    return {
        "plots": signatures,
        "title": _norm_text(" ".join(n.text or "" for n in title.findall(".//a:t", NS)))
        if title is not None else "",
        "style": None if style is None else style.attrib.get("val"),
        "legend": legend is not None,
        "legend_pos": None if legend_pos is None else legend_pos.attrib.get("val"),
        "labels": {
            "present": bool(labels),
            **{key: any(_flag(node.find(f"c:{tag}", NS).attrib.get("val", "1")) is True
                        for node in labels if node.find(f"c:{tag}", NS) is not None)
               for key, tag in (("value", "showVal"), ("category", "showCatName"),
                                ("series", "showSerName"), ("percent", "showPercent"),
                                ("legend_key", "showLegendKey"))},
        },
        "axes": axes,
        "series": series,
        "first_slice_angle": 0 if first is None else _int(first.attrib.get("val"), 0),
        "workbooks": [_workbook_signature(z.read(book)) for book in books
                      if book in z.namelist()],
    }


def _diagram_of(z: zipfile.ZipFile, data_part: str | None,
                layout_part: str | None, rels: dict) -> dict[str, Any]:
    """SmartArt: the nodes, the edges between them, and the cached drawing.

    Node ids are GUIDs a rebuild would not reproduce, so they are informational
    (`_ids`) and the graded content is the node *text* and the edges expressed
    in terms of it.  The drawing cache is listed too: it holds every node's
    text and is the classic answer leak behind a "deleted" diagram.
    """
    out: dict[str, Any] = {}
    if layout_part and layout_part in z.namelist():
        root = _xml(z, layout_part)
        cat = root.find(".//dgm:catLst/dgm:cat", NS)
        name = root.attrib.get("uniqueId") or (cat.attrib.get("type") if cat is not None else None)
        out["layout"] = name.rsplit("/", 1)[-1] if name else None
    if data_part and data_part in z.namelist():
        root = _xml(z, data_part)
        nodes, ids = [], {}
        for pt in root.iter(q("dgm:pt")):
            if (pt.attrib.get("type") or "node") != "node":
                continue
            text = _plain_text(_shape_text(pt)) or _norm_text(
                "".join(node.text or "" for node in pt.iter(q("a:t"))))
            ids[pt.attrib.get("modelId")] = text
            nodes.append(text)
        edges = []
        for cxn in root.iter(q("dgm:cxn")):
            kind = cxn.attrib.get("type") or "parOf"
            if kind.startswith("pres"):
                continue                     # layout-pass artefacts, not content
            src, dst = cxn.attrib.get("srcId"), cxn.attrib.get("destId")
            if src in ids and dst in ids:
                edges.append(f"{kind}:{ids[src]}>{ids[dst]}")
        out["nodes"] = sorted(nodes)
        out["n_nodes"] = len(nodes)
        out["edges"] = sorted(edges)
        out["_ids"] = ids
        out["_data_part"] = data_part
    drawing = next((rel["resolved"] for rel in rels.values()
                    if rel["type"] == "diagramDrawing"), None)
    if drawing and drawing in z.namelist():
        root = _xml(z, drawing)
        shapes = [node for node in root.iter(q("dsp:sp"))]
        out["cache"] = {
            "n_shapes": len(shapes),
            "texts": sorted(filter(None, (_plain_text(_shape_text(sp)) for sp in shapes))),
        }
    return out


def _ole_of(el: ET.Element) -> dict[str, Any] | None:
    for node in el.iter():
        if _local(node.tag) == "oleObj":
            return {"progId": node.attrib.get("progId"),
                    "name": node.attrib.get("name"),
                    "image": any(sub.find(f".//{q('a:blip')}") is not None
                                 for sub in el.iter()
                                 if _local(sub.tag) == "oleObj")}
    return None


# --------------------------------------------------------------------------- #
# shapes
# --------------------------------------------------------------------------- #


def _shape_children(el: ET.Element) -> Iterable[ET.Element]:
    """Drawable children in document order, unwrapping `mc:AlternateContent`.

    Real decks wrap ink, newer geometry and some SmartArt in an
    AlternateContent block; a plain tag filter walks past it and the shapes
    inside simply do not exist as far as the inventory is concerned — and the
    paths would then no longer line up with the ones the recipe addresses.
    """
    for child in el:
        if child.tag == q("mc:AlternateContent"):
            branch = child.find("mc:Choice", NS)
            if branch is None:
                branch = child.find("mc:Fallback", NS)
            if branch is not None:
                yield from _shape_children(branch)
        elif child.tag in SHAPE_TAGS:
            yield child


def _placeholder(el: ET.Element) -> dict[str, Any] | None:
    ph = el.find(".//p:nvPr/p:ph", NS)
    if ph is None:
        return None
    return {"type": ph.attrib.get("type", "body"),
            "idx": _int(ph.attrib.get("idx"), 0)}


def _ident(el: ET.Element) -> tuple[int | None, str]:
    for name in ("p:nvSpPr", "p:nvPicPr", "p:nvGraphicFramePr", "p:nvCxnSpPr",
                 "p:nvGrpSpPr"):
        nv = el.find(name, NS)
        if nv is None:
            continue
        node = nv.find("p:cNvPr", NS)
        if node is not None:
            return _int(node.attrib.get("id")), node.attrib.get("name", "")
    return None, ""


def _kind(el: ET.Element, text: str) -> str:
    if el.tag == q("p:pic"):
        return "picture"
    if el.tag == q("p:cxnSp"):
        return "connector"
    if el.tag == q("p:grpSp"):
        return "group"
    if el.tag == q("p:graphicFrame"):
        data = el.find("a:graphic/a:graphicData", NS)
        uri = data.attrib.get("uri", "") if data is not None else ""
        for suffix, name in (("/table", "table"), ("/chart", "chart"),
                             ("/diagram", "smartart"), ("/ole", "ole")):
            if uri.endswith(suffix):
                return name
        return "graphicframe"
    if _placeholder(el) is not None:
        return "placeholder"
    geom = el.find("p:spPr/a:prstGeom", NS)
    prst = geom.attrib.get("prst") if geom is not None else None
    if text and prst in (None, "rect"):
        return "textbox"
    return "autoshape"


def _blip_blob(el: ET.Element, blobs: dict[str, str]) -> str | None:
    """The identity of the image a shape draws.

    A `p:pic` carries `p:blipFill`; a picture-filled `p:sp` — which is what
    soffice turns a cropped picture into on export — carries
    `p:spPr/a:blipFill`.  Both mean "this image here" and must hash alike.
    """
    for path in ("p:blipFill/a:blip", "p:spPr/a:blipFill/a:blip"):
        blip = el.find(path, NS)
        if blip is not None:
            return blobs.get(blip.attrib.get(R_EMBED) or "")
    return None


def _crop_of(el: ET.Element) -> dict[str, Any] | None:
    bf = el.find("p:blipFill", NS)
    if bf is None:
        bf = el.find("p:spPr/a:blipFill", NS)
    if bf is None:
        return None
    out: dict[str, Any] = {}
    src = bf.find("a:srcRect", NS)
    # PowerPoint writes an empty <a:srcRect/> on plenty of uncropped pictures;
    # empty and absent are the same picture.
    if src is not None and src.attrib:
        out["crop"] = {key: _int(value, 0) for key, value in sorted(src.attrib.items())}
    if bf.find("a:tile", NS) is not None:
        out["mode"] = "tile"
    elif bf.find("a:stretch", NS) is not None:
        out["mode"] = "stretch"
    blip = bf.find("a:blip", NS)
    if blip is not None:
        effects = [_local(child.tag) for child in blip
                   if _local(child.tag) != "extLst"]
        if effects:
            out["recolor"] = sorted(effects)
        if blip.attrib.get("cstate"):
            out["cstate"] = blip.attrib["cstate"]
    return out or None


def _connector_of(el: ET.Element) -> dict[str, Any] | None:
    nv = el.find("p:nvCxnSpPr/p:cNvCxnSpPr", NS)
    if nv is None:
        return None
    out: dict[str, Any] = {}
    for tag, key in (("a:stCxn", "start"), ("a:endCxn", "end")):
        node = nv.find(tag, NS)
        out[key] = ({"id": _int(node.attrib.get("id")),
                     "idx": _int(node.attrib.get("idx"))}
                    if node is not None else None)
    return out


def _hyperlink(el: ET.Element, rels: dict) -> str | None:
    node = el.find(".//p:cNvPr/a:hlinkClick", NS)
    if node is None:
        return None
    rid = node.attrib.get(R_ID)
    if rid and rid in rels:
        rel = rels[rid]
        return rel["target"] if rel["external"] else f"internal:{rel['type']}"
    return node.attrib.get("action") or "?"


def _keys(record: dict[str, Any]) -> list[str]:
    """Candidate identities, strongest first.

    Shape ids and names change when an agent deletes a shape and draws it
    again, so neither may be the primary identity.  What survives a rebuild is
    what the shape *is*: the role it fills, the image it draws, the words it
    holds.  The name is kept as a weak fallback and the geometry class as the
    last resort for shapes that have none of the above.

    `name:` stays here, ahead of `geo:`, because within one file's lineage a
    name really is more specific than a rounded size class, and this list is
    also what `match_shapes` and the round-trip drift reports pair on.  What it
    is **not** is evidence when somebody is being scored: an agent can type any
    name it likes, and `rename_only` does exactly that.  So the judgement about
    what a name is worth lives in `comparators._WEAK_KEY_PREFIXES` — which
    holds `name:`, `geo:` and `kind:`, awards none of them credit for a shape
    merely *existing*, and refuses a pairing made on one when the two boxes do
    not meet anywhere on the page.
    """
    out: list[str] = []
    ph = record.get("placeholder")
    if ph:
        out.append(f"ph:{ph['type']}#{ph['idx']}")
    if record.get("picture", {}).get("blob"):
        out.append(f"pic:{record['picture']['blob']}")
    kind = record["kind"]
    if kind in ("table", "chart", "smartart", "graphicframe", "ole"):
        out.append(kind)
    plain = record.get("_plain") or ""
    if plain:
        out.append(f"txt:{_sha(plain.encode(), 12)}")
    if record.get("_name"):
        out.append(f"name:{record['_name']}")
    bbox = record.get("bbox")
    if bbox:
        out.append(f"geo:{kind}:{round(bbox['w'] / 91440)}x{round(bbox['h'] / 91440)}")
    out.append(f"kind:{kind}")
    return out


def _shape_record(el: ET.Element, path: str, z_index: int, matrix,
                  group: str | None, ctx: dict) -> dict[str, Any]:
    shape_id, name = _ident(el)
    xf = _read_xfrm(el)
    text = _shape_text(el)
    plain = _plain_text(text)
    kind = _kind(el, plain)
    sp = _sp_pr(el)
    ph = _placeholder(el)
    field = _has_field(el)
    record: dict[str, Any] = {
        "_path": path,
        "_id": shape_id,
        "_name": name,
        "_plain": plain,
        "kind": kind,
        "z": z_index,
        "group": group,
        "bbox": _absolute(xf, matrix),
        "hidden": el.find(".//p:cNvPr", NS) is not None
        and (el.find(".//p:cNvPr", NS).attrib.get("hidden") in ("1", "true")),
    }
    if ph:
        record["placeholder"] = ph
        # matching happens on the role; the text is only exempt from comparison
        # where an a:fld really is present
        record["app_role"] = ph["type"] in APP_FILLED
    if text is not None:
        if field:
            record["field"] = field
            record["generated_text"] = text
        else:
            record["text"] = text
    if sp is not None or kind == "picture":
        fill = _fill_of(sp, ctx["blobs"])
        line = _line_of(sp)
        geom = _geom_of(sp)
        effects = _effects_of(el, sp)
        if fill:
            record["fill"] = fill
        if line:
            record["line"] = line
        if geom:
            record["geom"] = geom
        if effects:
            record["effects"] = effects
    if kind == "picture" or _blip_blob(el, ctx["blobs"]):
        picture: dict[str, Any] = {"blob": _blip_blob(el, ctx["blobs"])}
        crop = _crop_of(el)
        if crop:
            picture.update(crop)
        record["picture"] = picture
    if kind == "connector":
        record["connector"] = _connector_of(el)
    if kind == "table":
        record["table"] = _table_of(el)
    if kind == "chart":
        data = el.find("a:graphic/a:graphicData", NS)
        ref = data.find("c:chart", NS) if data is not None else None
        rid = ref.attrib.get(R_ID) if ref is not None else None
        rel = ctx["rels"].get(rid or "")
        if rel and not rel["external"] and rel["resolved"] in ctx["z"].namelist():
            record["chart"] = _chart_of(ctx["z"], rel["resolved"])
        else:
            record["chart"] = {"missing": True}
    if kind == "smartart":
        data = el.find("a:graphic/a:graphicData", NS)
        rel_ids = None
        for child in (data if data is not None else ()):
            if _local(child.tag) == "relIds":
                rel_ids = child
                break
        if rel_ids is not None:
            dm = ctx["rels"].get(rel_ids.attrib.get(R_DM) or "")
            lo = ctx["rels"].get(rel_ids.attrib.get(R_LO) or "")
            record["diagram"] = _diagram_of(
                ctx["z"], dm["resolved"] if dm else None,
                lo["resolved"] if lo else None, ctx["rels"])
    ole = _ole_of(el)
    if ole:
        record["ole"] = ole
    link = _hyperlink(el, ctx["rels"])
    if link:
        record["link"] = link
    record["keys"] = _keys(record)
    return record


def _walk_shapes(tree: ET.Element, ctx: dict) -> list[dict[str, Any]]:
    shapes: list[dict[str, Any]] = []

    def visit(container: ET.Element, matrix, prefix: str, group: str | None):
        for index, el in enumerate(_shape_children(container)):
            path = f"{prefix}{index}"
            record = _shape_record(el, path, index, matrix, group, ctx)
            shapes.append(record)
            if el.tag == q("p:grpSp"):
                xf = _read_xfrm(el)
                child_matrix = _group_matrix(xf, matrix) if xf else matrix
                visit(el, child_matrix, path + "/", record["keys"][0])

    visit(tree, IDENTITY, "", None)
    _number(shapes)
    return shapes


def _number(shapes: list[dict[str, Any]]) -> None:
    """Give shapes sharing a primary key an occurrence index, in document order."""
    seen: dict[str, int] = {}
    for shape in shapes:
        primary = shape["keys"][0]
        count = seen.get(primary, 0)
        seen[primary] = count + 1
        shape["key"] = f"{primary}#{count}"


# --------------------------------------------------------------------------- #
# slide-level facts that are not shapes
# --------------------------------------------------------------------------- #


def _transition_of(root: ET.Element) -> dict[str, Any] | None:
    """The slide transition, including the p14 form PowerPoint 2010+ writes.

    PowerPoint writes it twice — a p14 form in `mc:Choice` and a plain one in
    `mc:Fallback` — so a direct-child lookup misses it on most modern decks and
    the whole `strip_transition` family becomes invisible.
    """
    node = root.find("p:transition", NS)
    if node is None:
        node = next((child for child in root.iter()
                     if _local(child.tag) == "transition"), None)
    if node is None:
        return None
    kids = [child for child in node if _local(child.tag) != "extLst"]
    if not kids:
        # soffice writes `<p:transition spd="slow" p14:dur="2000"/>` with no
        # effect child on slides that have no transition — 1206 of them across
        # the ten decks.  A transition that names no effect is no transition,
        # the same way an empty `<p:timing>` animates nothing.
        return None
    first = kids[0]
    kind = _local(first.tag)
    detail = {}
    for attr in ("dir", "orient", "spokes", "thruBlk", "option", "pattern"):
        if first.attrib.get(attr):
            detail[attr] = first.attrib[attr]
    if first.attrib.get("prst"):
        kind = first.attrib["prst"]          # prstTrans carries the real name
        detail["family"] = "prstTrans"
    duration = next((value for key, value in node.attrib.items()
                     if key.endswith("}dur")), None)
    return {"type": kind, "detail": detail or None,
            "speed": node.attrib.get("spd"),
            "duration_ms": _int(duration),
            "advance_ms": node.attrib.get("advTm"),
            "on_click": node.attrib.get("advClick") != "0"}


def _animation_of(root: ET.Element) -> dict[str, Any] | None:
    """The build sequence as discrete click steps.

    `strip_animation` removes the whole timing tree and `anim_drop_steps`
    removes individual `p:par` children of the main sequence, so a bare
    "animation: yes/no" cannot score the second one.  Targets are recorded by
    shape id *and* by the effect that fired, because the id alone cannot answer
    "which object appears at which click, and with what effect".
    """
    timing = root.find("p:timing", NS)
    if timing is None:
        timing = next((child for child in root.iter()
                       if _local(child.tag) == "timing"), None)
    if timing is None:
        return None
    main = None
    for seq in timing.iter(q("p:seq")):
        ctn = seq.find("p:cTn", NS)
        if ctn is not None and ctn.attrib.get("nodeType") == "mainSeq":
            main = seq
            break
    steps = []
    if main is not None:
        kids = main.find("p:cTn/p:childTnLst", NS)
        for node in (list(kids) if kids is not None else []):
            if _local(node.tag) != "par":
                continue
            effects = []
            for behaviour in node.iter():
                if behaviour.attrib.get("presetID") is None:
                    continue
                target = next((tgt.attrib.get("spid")
                               for tgt in behaviour.iter(q("p:spTgt"))), None)
                effects.append({"preset": behaviour.attrib.get("presetID"),
                                "class": behaviour.attrib.get("presetClass"),
                                "subtype": behaviour.attrib.get("presetSubtype"),
                                "target": target})
            steps.append({"targets": sorted({item["target"] for item in effects
                                             if item["target"]}),
                          "effects": effects})
    total = sum(1 for node in timing.iter() if node.attrib.get("presetID") is not None)
    if not steps and not total:
        # PowerPoint writes an empty <p:timing> on slides that have no
        # animation at all — 3 of deck0001's 19.  An empty tree animates
        # nothing, exactly as an empty <a:effectLst/> renders nothing, and
        # reporting it as "animated" invents a difference on every slide the
        # application happens to have touched.
        return None
    return {"present": True, "n_steps": len(steps), "n_effects": total,
            "steps": steps}


def _background_of(root: ET.Element, blobs: dict[str, str]) -> dict[str, Any] | None:
    bg = root.find("p:cSld/p:bg", NS)
    if bg is None:
        return None
    ref = bg.find("p:bgRef", NS)
    if ref is not None:
        return {"kind": "theme_ref", "idx": ref.attrib.get("idx"),
                "color": _color(ref)}
    pr = bg.find("p:bgPr", NS)
    if pr is not None:
        return {"kind": "own", "fill": _fill_of(pr, blobs)}
    return None


def _notes_of(z: zipfile.ZipFile, rels: dict) -> str | None:
    part = next((rel["resolved"] for rel in rels.values()
                 if rel["type"] == "notesSlide"), None)
    if not part or part not in z.namelist():
        return None
    root = _xml(z, part)
    chunks = []
    for sp in root.iter(q("p:sp")):
        ph = _placeholder(sp)
        if ph and ph["type"] == "sldImg":
            continue
        if ph and ph["type"] == "sldNum":
            continue                    # the app writes the page number in here
        text = _plain_text(_shape_text(sp))
        if text:
            chunks.append(text)
    return _norm_text(" ".join(chunks)) or None


# --------------------------------------------------------------------------- #
# the inventory
# --------------------------------------------------------------------------- #


def _blob_map(z: zipfile.ZipFile, rels: dict) -> dict[str, str]:
    """rId -> digest of the media part it points at."""
    out = {}
    for rid, rel in rels.items():
        if rel["external"]:
            continue
        target = rel["resolved"]
        if "/media/" in target and target in z.namelist():
            out[rid] = _sha(z.read(target))
    return out


def _part_inventory(z: zipfile.ZipFile, part: str) -> dict[str, Any]:
    root = _xml(z, part)
    rels = _relationships(z, part)
    ctx = {"z": z, "rels": rels, "blobs": _blob_map(z, rels), "part": part}
    tree = root.find("p:cSld/p:spTree", NS)
    shapes = _walk_shapes(tree, ctx) if tree is not None else []
    return {"root": root, "rels": rels, "shapes": shapes, "ctx": ctx}


def _slide_order(z: zipfile.ZipFile) -> list[str]:
    """Slide parts in presentation order, which is not part-name order."""
    presentation = "ppt/presentation.xml"
    if presentation not in z.namelist():
        return sorted((name for name in z.namelist()
                       if re.match(r"^ppt/slides/slide\d+\.xml$", name)),
                      key=_slide_no)
    rels = _relationships(z, presentation)
    root = _xml(z, presentation)
    order = []
    for node in root.findall("p:sldIdLst/p:sldId", NS):
        rel = rels.get(node.attrib.get(R_ID) or "")
        if rel and rel["resolved"] in z.namelist():
            order.append(rel["resolved"])
    return order


def _categorise(name: str) -> str | None:
    for prefix, label in (
        ("ppt/slides/slide", "slides"),
        ("ppt/slideLayouts/slideLayout", "layouts"),
        ("ppt/slideMasters/slideMaster", "masters"),
        ("ppt/notesSlides/notesSlide", "notes"),
        ("ppt/charts/chart", "charts"),
        ("ppt/diagrams/data", "diagrams"),
        ("ppt/embeddings/", "embeddings"),
        ("ppt/comments/", "comments"),
        ("ppt/theme/theme", "themes"),
    ):
        if name.startswith(prefix) and name.endswith((".xml", ".xlsx", ".bin",
                                                      ".docx", ".pptx")):
            return label
    if "/media/" in name:
        return "media"
    if "customXml/" in name:
        return "customXml"
    return None


def inventory_pptx(path) -> dict[str, Any]:
    """The semantic inventory of one `.pptx`."""
    path = str(path)
    if not zipfile.is_zipfile(path):
        raise ValueError(f"not a pptx package: {path}")
    with zipfile.ZipFile(path) as z:
        names = [name for name in z.namelist() if not name.endswith("/")]
        counts: dict[str, int] = {}
        for name in names:
            label = _categorise(name)
            if label:
                counts[label] = counts.get(label, 0) + 1
        media = sorted(_sha(z.read(name)) for name in names if "/media/" in name)
        media_parts = {name: _sha(z.read(name)) for name in sorted(names)
                       if "/media/" in name}

        presentation_root = (_xml(z, "ppt/presentation.xml")
                             if "ppt/presentation.xml" in z.namelist() else None)
        size = presentation_root.find("p:sldSz", NS) if presentation_root is not None else None

        order = _slide_order(z)
        layout_parts = sorted((name for name in names
                               if re.match(r"^ppt/slideLayouts/slideLayout\d+\.xml$", name)),
                              key=_slide_no)
        master_parts = sorted((name for name in names
                               if re.match(r"^ppt/slideMasters/slideMaster\d+\.xml$", name)),
                              key=_slide_no)

        def named(part: str, built: dict) -> str:
            csld = built["root"].find("p:cSld", NS)
            return (csld.attrib.get("name") if csld is not None else None) or part.rsplit("/", 1)[-1]

        # Layouts and masters are addressed by **name**, not by part path.
        # `slideLayout7.xml` is a byte-level fact — soffice renumbers the parts
        # and writes one master per slide — while "Title and Content" is what
        # the slide, the recipe's `layout` op and the application's UI all mean.
        def collect(parts: list[str]) -> tuple[dict, dict]:
            by_name: dict[str, Any] = {}
            of_part: dict[str, str] = {}
            for part in parts:
                built = _part_inventory(z, part)
                label = named(part, built)
                if label in by_name:
                    label = f"{label}#{sum(1 for k in by_name if k.split('#')[0] == label)}"
                of_part[part] = label
                by_name[label] = {"_part": part, "shapes": built["shapes"],
                                  "master": next((rel["resolved"]
                                                  for rel in built["rels"].values()
                                                  if rel["type"] == "slideMaster"), None)}
            return by_name, of_part

        masters, master_name = collect(master_parts)
        layouts, layout_name = collect(layout_parts)
        for entry in layouts.values():
            entry["master"] = master_name.get(entry["master"] or "")
        for entry in masters.values():
            entry.pop("master", None)

        slides = []
        for index, part in enumerate(order):
            built = _part_inventory(z, part)
            root, rels = built["root"], built["rels"]
            layout = next((rel["resolved"] for rel in rels.values()
                           if rel["type"] == "slideLayout"), None)
            slides.append({
                "i": index,
                "_part": part,
                "_layout_part": layout,
                "layout": layout_name.get(layout or ""),
                "master": layouts.get(layout_name.get(layout or ""), {}).get("master"),
                "hidden": root.attrib.get("show") == "0",
                "background": _background_of(root, built["ctx"]["blobs"]),
                "notes": _notes_of(z, rels),
                "transition": _transition_of(root),
                "animation": _animation_of(root),
                "n_shapes": len(built["shapes"]),
                "shapes": built["shapes"],
            })

    return {
        "format": "pptxgym.inventory/1",
        "package": {
            "slide_count": len(order),
            "slide_order": [part.rsplit("/", 1)[-1] for part in order],
            "slide_w": _int(size.attrib.get("cx")) if size is not None else None,
            "slide_h": _int(size.attrib.get("cy")) if size is not None else None,
            "parts": counts,
            # an anti-hacking gate compares these against the broken file: a
            # blob that is not in the input but is in the answer means the
            # original was pasted back in rather than rebuilt
            "media": media,
            "_media_parts": media_parts,
        },
        "slides": slides,
        "layouts": layouts,
        "masters": masters,
    }


# --------------------------------------------------------------------------- #
# comparing
# --------------------------------------------------------------------------- #


def flatten(node: Any, prefix: str = "", out: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dotted path -> scalar, with shapes addressed by identity rather than index.

    Keys beginning with `_` are informational (shape ids, names, part names,
    node GUIDs) and are left out: none of them survives an agent rebuilding a
    shape through the GUI, so comparing them manufactures differences.  `key`
    and `keys` go the same way — they are how a shape is *addressed*, and a
    derived address is not a fact about the file.
    """
    if out is None:
        out = {}
    if isinstance(node, dict):
        for key, value in node.items():
            if key.startswith("_") or key in ("key", "keys"):
                continue
            flatten(value, f"{prefix}.{key}" if prefix else str(key), out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            label = value["key"] if isinstance(value, dict) and "key" in value else index
            flatten(value, f"{prefix}[{label}]", out)
    else:
        out[prefix] = node
    return out


def diff(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Every dotted path where two inventories disagree."""
    a, b = flatten(before), flatten(after)
    return sorted(key for key in set(a) | set(b)
                  if a.get(key, _MISSING) != b.get(key, _MISSING))


_MISSING = object()


def match_shapes(before: list[dict[str, Any]],
                 after: list[dict[str, Any]]) -> list[tuple[dict | None, dict | None]]:
    """Pair up two slides' shapes on the strongest identity both sides share.

    Twins — three cards with the same size and no text — share every key, so
    they are paired by nearest centre rather than by document order, which is
    how a renderer's nudge stops reading as one deletion plus one addition.
    """
    free = list(after)
    pairs: list[tuple[dict | None, dict | None]] = []
    for shape in before:
        chosen = None
        for key in shape["keys"]:
            candidates = [item for item in free if key in item["keys"]]
            if not candidates:
                continue
            box = shape.get("bbox")
            if box and len(candidates) > 1:
                candidates.sort(key=lambda item: (
                    math.hypot((item.get("bbox") or box)["cx"] - box["cx"],
                               (item.get("bbox") or box)["cy"] - box["cy"])))
            chosen = candidates[0]
            break
        if chosen is not None:
            free.remove(chosen)
        pairs.append((shape, chosen))
    pairs.extend((None, item) for item in free)
    return pairs


def main():                                                       # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="semantic inventory of a .pptx")
    ap.add_argument("pptx", nargs="+")
    ap.add_argument("-o", "--out")
    ap.add_argument("--diff", action="store_true",
                    help="two files: print the paths where they differ")
    args = ap.parse_args()
    if args.diff:
        first, second = (inventory_pptx(p) for p in args.pptx[:2])
        for key in diff(first, second):
            print(key)
        return
    data = inventory_pptx(args.pptx[0])
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"{data['package']['slide_count']} slides -> {args.out}")
    else:
        print(text)


if __name__ == "__main__":                                        # pragma: no cover
    main()
