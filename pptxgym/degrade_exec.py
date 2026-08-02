"""Execute a degradation recipe against a deck: gt.pptx -> input.pptx + delta.

The proposer writes prose ("scatter the six member cards", "delete the chart on
slide 4 and leave its caption").  Turning that into a file is this module's
job, and it is deliberately a *separate* layer: the proposal must never be
shaped by what is convenient to implement.

A recipe is JSON, addressed by the same shape `path` the digest prints:

    {"slides": {"5": [{"op": "delete", "paths": ["12", "13"]},
                      {"op": "scatter", "paths": ["4", "6"], "seed": 7}]}}

Every primitive records what it changed — the element path, the parameters and
the prior value — into a delta entry, so the same recipe that builds the file
also describes exactly what a solver has to undo.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import random
import re
import sys
from pathlib import Path

from lxml import etree
from pptx import Presentation


from . import census
from . import charts
from . import smartart

q = census.q
EMU = 914400
REGISTRY = {}


def op(name):
    def deco(fn):
        REGISTRY[name] = fn
        return fn
    return deco


# --------------------------------------------------------------------------- #
# addressing
# --------------------------------------------------------------------------- #


def index_shapes(slide):
    """path -> element, matching the paths deck_digest prints."""
    out = {}

    def walk(container, prefix):
        for i, el in enumerate(census.shape_children(container)):
            path = f"{prefix}{i}"
            out[path] = el
            if el.tag == q("p:grpSp"):
                walk(el, path + "/")

    walk(slide.shapes._spTree, "")
    return out


def _sp_pr(el):
    for tag in ("p:spPr", "p:grpSpPr"):
        found = el.find(q(tag))
        if found is not None:
            return found
    return None


def _xfrm(el):
    # A graphicFrame — chart, table, SmartArt — has no p:spPr at all; its
    # geometry lives in p:xfrm.  Looking for spPr first and giving up when it
    # is absent meant every deleted chart or diagram entered the delta with no
    # bounding box, and anything downstream that needs to know *where* the
    # damage was (a masked reference render, most obviously) had nothing.
    if el.tag == q("p:graphicFrame"):
        xf = el.find(q("p:xfrm"))
        if xf is not None:
            return xf
    sp = _sp_pr(el)
    if sp is None:
        return None
    return sp.find(q("a:xfrm"))


def _box(el):
    xf = _xfrm(el)
    if xf is None:
        return None
    off, ext = xf.find(q("a:off")), xf.find(q("a:ext"))
    if off is None or ext is None:
        return None
    return (int(off.get("x")), int(off.get("y")),
            int(ext.get("cx")), int(ext.get("cy")))


def _set_box(el, x, y, cx=None, cy=None):
    xf = _xfrm(el)
    off, ext = xf.find(q("a:off")), xf.find(q("a:ext"))
    off.set("x", str(int(x)))
    off.set("y", str(int(y)))
    if cx is not None:
        ext.set("cx", str(max(1, int(cx))))
    if cy is not None:
        ext.set("cy", str(max(1, int(cy))))


def _label(el):
    sid, name = census.shape_ident(el)
    txt = census.element_text(el)[:40]
    return {"shape_id": sid, "name": name, "text": txt,
            "kind": census.classify_kind(el)}


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #


@op("delete")
def _delete(slide, shapes, spec, rng):
    """Remove shapes entirely. Charts/pictures lose their parts on save."""
    out = []
    for path in spec["paths"]:
        el = shapes.get(path)
        if el is None:
            continue
        out.append({"path": path, "op": "delete", **_label(el),
                    "removed_xml": etree.tostring(el).decode()[:4000],
                    "box": _box(el)})
        el.getparent().remove(el)
    return out


@op("delete_slide")
def _delete_slide(prs, idx, spec):
    raise NotImplementedError  # handled at deck level


@op("scatter")
def _scatter(slide, shapes, spec, rng):
    """Push shapes off their aligned positions by a bounded random offset."""
    amp = spec.get("amplitude_in", 0.9)
    out = []
    for path in spec["paths"]:
        el = shapes.get(path)
        box = _box(el) if el is not None else None
        if box is None:
            continue
        dx = rng.uniform(-amp, amp) * EMU
        dy = rng.uniform(-amp * 0.55, amp * 0.55) * EMU
        _set_box(el, box[0] + dx, box[1] + dy)
        out.append({"path": path, "op": "scatter", **_label(el),
                    "was": [box[0], box[1]], "now": [int(box[0] + dx),
                                                     int(box[1] + dy)],
                    "box": box})
    return out


@op("move")
def _move(slide, shapes, spec, rng):
    out = []
    for path in spec["paths"]:
        el = shapes.get(path)
        box = _box(el) if el is not None else None
        if box is None:
            continue
        dx = spec.get("dx_in", 0) * EMU
        dy = spec.get("dy_in", 0) * EMU
        _set_box(el, box[0] + dx, box[1] + dy)
        out.append({"path": path, "op": "move", **_label(el),
                    "was": [box[0], box[1]],
                    "now": [int(box[0] + dx), int(box[1] + dy)], "box": box})
    return out


@op("resize")
def _resize(slide, shapes, spec, rng):
    f = spec.get("factor", 0.7)
    fy = spec.get("factor_y", f)
    out = []
    for path in spec["paths"]:
        el = shapes.get(path)
        box = _box(el) if el is not None else None
        if box is None:
            continue
        x, y, cx, cy = box
        nw, nh = int(cx * f), int(cy * fy)
        if spec.get("keep_center", True):
            _set_box(el, x + (cx - nw) // 2, y + (cy - nh) // 2, nw, nh)
        else:
            _set_box(el, x, y, nw, nh)
        out.append({"path": path, "op": "resize", **_label(el),
                    "was": [cx, cy], "now": [nw, nh], "box": box})
    return out


@op("swap")
def _swap(slide, shapes, spec, rng):
    out = []
    for a, b in spec["pairs"]:
        ea, eb = shapes.get(a), shapes.get(b)
        ba, bb = (_box(ea) if ea is not None else None,
                  _box(eb) if eb is not None else None)
        if not ba or not bb:
            continue
        _set_box(ea, bb[0], bb[1])
        _set_box(eb, ba[0], ba[1])
        out.append({"path": a, "op": "swap", "with": b, **_label(ea),
                    "was": [ba[0], ba[1]], "now": [bb[0], bb[1]], "box": ba})
    return out


@op("clear_text")
def _clear_text(slide, shapes, spec, rng):
    out = []
    for path in spec["paths"]:
        el = shapes.get(path)
        if el is None:
            continue
        before = census.element_text(el)
        if not before:
            continue
        label = _label(el)                     # kind depends on having text
        kept = spec.get("keep_first_paragraph", False)
        body = el.find(q("p:txBody"))
        paras = body.findall(q("a:p")) if body is not None else []
        for i, para in enumerate(paras):
            if kept and i == 0:
                continue
            for t in para.iter(q("a:t")):
                t.text = ""
        out.append({"path": path, "op": "clear_text", **label,
                    "was_text": before[:600], "box": _box(el)})
    return out


@op("set_text")
def _set_text(slide, shapes, spec, rng):
    out = []
    for path in spec["paths"]:
        el = shapes.get(path)
        if el is None:
            continue
        before = census.element_text(el)
        label = _label(el)
        runs = list(el.iter(q("a:t")))
        if not runs:
            continue
        runs[0].text = spec["text"]
        for t in runs[1:]:
            t.text = ""
        out.append({"path": path, "op": "set_text", **label,
                    "was_text": before[:600], "now_text": spec["text"],
                    "box": _box(el)})
    return out


@op("set_font")
def _set_font(slide, shapes, spec, rng):
    """Force a typeface / size / weight onto every run of a shape."""
    out = []
    for path in spec["paths"]:
        el = shapes.get(path)
        if el is None:
            continue
        changed = []
        for rpr in el.iter(q("a:rPr")):
            if "font" in spec:
                for tag in ("a:latin", "a:ea", "a:cs"):
                    old = rpr.find(q(tag))
                    if old is not None:
                        rpr.remove(old)
                latin = etree.SubElement(rpr, q("a:latin"))
                latin.set("typeface", spec["font"])
            if "size_pt" in spec:
                changed.append(rpr.get("sz"))
                rpr.set("sz", str(int(spec["size_pt"] * 100)))
            if "bold" in spec:
                rpr.set("b", "1" if spec["bold"] else "0")
            if "italic" in spec:
                rpr.set("i", "1" if spec["italic"] else "0")
            if "underline" in spec:
                # stripping bold while leaving u="sng" behind marks exactly the
                # runs that were emphasised — it hands over the answer
                if spec["underline"]:
                    rpr.set("u", "sng")
                else:
                    rpr.attrib.pop("u", None)
            if "color" in spec:
                for f in list(rpr):
                    if etree.QName(f).localname.endswith("Fill"):
                        rpr.remove(f)
                sf = etree.SubElement(rpr, q("a:solidFill"))
                etree.SubElement(sf, q("a:srgbClr")).set(
                    "val", spec["color"].lstrip("#").upper())
        out.append({"path": path, "op": "set_font", **_label(el),
                    "params": {k: v for k, v in spec.items() if k != "paths"},
                    "was_sizes": changed[:8], "box": _box(el)})
    return out


@op("strip_effects")
def _strip_effects(slide, shapes, spec, rng):
    """Flatten shadow / glow / reflection / soft edge / 3-D, and gradients."""
    out = []
    for path in spec["paths"]:
        el = shapes.get(path)
        sp = _sp_pr(el) if el is not None else None
        if sp is None:
            continue
        removed = []
        for tag in ("a:effectLst", "a:effectDag", "a:sp3d", "a:scene3d"):
            node = sp.find(q(tag))
            if node is not None and len(node) or (node is not None and
                                                  tag in ("a:sp3d", "a:scene3d")):
                removed.append(etree.QName(node).localname)
                sp.remove(node)
        if spec.get("flatten_gradient", True):
            grad = sp.find(q("a:gradFill"))
            if grad is not None:
                stops = grad.findall(f"{q('a:gsLst')}/{q('a:gs')}")
                hexv = None
                for gs in stops:
                    clr = gs.find(q("a:srgbClr"))
                    if clr is not None:
                        hexv = clr.get("val")
                        break
                sp.remove(grad)
                solid = etree.Element(q("a:solidFill"))
                etree.SubElement(solid, q("a:srgbClr")).set(
                    "val", hexv or "BFBFBF")
                sp.insert(_fill_index(sp), solid)
                removed.append("gradFill")
        if removed:
            out.append({"path": path, "op": "strip_effects", **_label(el),
                        "removed": removed, "box": _box(el)})
    return out


def _fill_index(sp):
    """a:xfrm and geometry come first; fills follow them."""
    i = 0
    for j, child in enumerate(sp):
        if etree.QName(child).localname in ("xfrm", "prstGeom", "custGeom"):
            i = j + 1
    return i


@op("recolor")
def _recolor(slide, shapes, spec, rng):
    out = []
    for path in spec["paths"]:
        el = shapes.get(path)
        sp = _sp_pr(el) if el is not None else None
        if sp is None:
            continue
        before = etree.tostring(sp).decode()[:600]
        for f in list(sp):
            if etree.QName(f).localname.endswith("Fill"):
                sp.remove(f)
        solid = etree.Element(q("a:solidFill"))
        etree.SubElement(solid, q("a:srgbClr")).set(
            "val", spec["to"].lstrip("#").upper())
        sp.insert(_fill_index(sp), solid)
        out.append({"path": path, "op": "recolor", **_label(el),
                    "to": spec["to"], "was_fill_xml": before, "box": _box(el)})
    return out


@op("outline")
def _outline(slide, shapes, spec, rng):
    """Change or remove the shape outline."""
    out = []
    for path in spec["paths"]:
        el = shapes.get(path)
        sp = _sp_pr(el) if el is not None else None
        if sp is None:
            continue
        old = sp.find(q("a:ln"))
        before = etree.tostring(old).decode()[:400] if old is not None else None
        if old is not None:
            sp.remove(old)
        if spec.get("mode") != "remove":
            ln = etree.SubElement(sp, q("a:ln"))
            if "width_pt" in spec:
                ln.set("w", str(int(spec["width_pt"] * 12700)))
            sf = etree.SubElement(ln, q("a:solidFill"))
            etree.SubElement(sf, q("a:srgbClr")).set(
                "val", spec.get("color", "808080").lstrip("#").upper())
        out.append({"path": path, "op": "outline", **_label(el),
                    "mode": spec.get("mode", "set"), "was_ln_xml": before,
                    "box": _box(el)})
    return out


@op("rotate")
def _rotate(slide, shapes, spec, rng):
    out = []
    for path in spec["paths"]:
        el = shapes.get(path)
        xf = _xfrm(el) if el is not None else None
        if xf is None:
            continue
        was = int(xf.get("rot", "0"))
        xf.set("rot", str(int(was + spec.get("deg", 12) * 60000) % 21600000))
        out.append({"path": path, "op": "rotate", **_label(el),
                    "was_deg": was / 60000.0, "by_deg": spec.get("deg", 12),
                    "box": _box(el)})
    return out


@op("zorder")
def _zorder(slide, shapes, spec, rng):
    """Send shapes to the back — a diagram's layering is meaning, not polish."""
    tree = slide.shapes._spTree
    out = []
    for path in spec["paths"]:
        el = shapes.get(path)
        if el is None:
            continue
        parent = el.getparent()
        kids = [c for c in census.shape_children(parent)]
        was = kids.index(el)
        parent.remove(el)
        anchor = None
        for c in parent:
            if c.tag in census.SHAPE_TAGS or c.tag == q("mc:AlternateContent"):
                anchor = c
                break
        if spec.get("to", "back") == "back" and anchor is not None:
            anchor.addprevious(el)
        else:
            parent.append(el)
        out.append({"path": path, "op": "zorder", **_label(el),
                    "was_index": was, "to": spec.get("to", "back"),
                    "box": _box(el)})
    return out


@op("ungroup")
def _ungroup(slide, shapes, spec, rng):
    """Dissolve a group, leaving children at their absolute positions."""
    out = []
    for path in spec["paths"]:
        grp = shapes.get(path)
        if grp is None or grp.tag != q("p:grpSp"):
            continue
        gx = grp.find(f"{q('p:grpSpPr')}/{q('a:xfrm')}")
        if gx is None:
            continue
        off, ext = gx.find(q("a:off")), gx.find(q("a:ext"))
        ch_off, ch_ext = gx.find(q("a:chOff")), gx.find(q("a:chExt"))
        ox, oy = int(off.get("x")), int(off.get("y"))
        ew, eh = int(ext.get("cx")), int(ext.get("cy"))
        cox = int(ch_off.get("x")) if ch_off is not None else ox
        coy = int(ch_off.get("y")) if ch_off is not None else oy
        cw = int(ch_ext.get("cx")) if ch_ext is not None else ew
        chh = int(ch_ext.get("cy")) if ch_ext is not None else eh
        sx, sy = (ew / cw if cw else 1), (eh / chh if chh else 1)
        parent = grp.getparent()
        n = 0
        for child in list(census.shape_children(grp)):
            cb = _box(child)
            grp.remove(child)
            grp.addprevious(child)
            if cb:
                _set_box(child, ox + (cb[0] - cox) * sx, oy + (cb[1] - coy) * sy,
                         cb[2] * sx, cb[3] * sy)
            n += 1
        parent.remove(grp)
        out.append({"path": path, "op": "ungroup", "n_children": n,
                    "box": [ox, oy, ew, eh]})
    return out


@op("clear_table_cells")
def _clear_cells(slide, shapes, spec, rng):
    out = []
    for path in spec["paths"]:
        el = shapes.get(path)
        if el is None:
            continue
        tbl = el.find(f"{q('a:graphic')}/{q('a:graphicData')}/{q('a:tbl')}")
        if tbl is None:
            continue
        rows = tbl.findall(q("a:tr"))
        want_rows = spec.get("rows")
        want_cols = spec.get("cols")
        cleared = []
        for ri, tr in enumerate(rows):
            if want_rows is not None and ri not in want_rows:
                continue
            for ci, tc in enumerate(tr.findall(q("a:tc"))):
                if want_cols is not None and ci not in want_cols:
                    continue
                txt = census.element_text(tc)
                if not txt:
                    continue
                for t in tc.iter(q("a:t")):
                    t.text = ""
                cleared.append({"at": [ri, ci], "was": txt[:80]})
        if cleared:
            out.append({"path": path, "op": "clear_table_cells", **_label(el),
                        "cleared": cleared, "box": _box(el)})
    return out


@op("table_drop_rows")
def _tbl_drop_rows(slide, shapes, spec, rng):
    """Delete whole rows. `clear_table_cells` empties them; this removes them,
    which is what "one entry is missing from the list" actually looks like."""
    out = []
    for path in spec["paths"]:
        el = shapes.get(path)
        tbl = (el.find(f"{q('a:graphic')}/{q('a:graphicData')}/{q('a:tbl')}")
               if el is not None else None)
        if tbl is None:
            continue
        rows = tbl.findall(q("a:tr"))
        gone = []
        for ri in sorted(spec.get("rows", []), reverse=True):
            if 0 <= ri < len(rows):
                gone.append({"row": ri,
                             "cells": [census.element_text(c)[:60]
                                       for c in rows[ri].findall(q("a:tc"))]})
                rows[ri].getparent().remove(rows[ri])
        if gone:
            out.append({"path": path, "op": "table_drop_rows", **_label(el),
                        "removed": list(reversed(gone)), "box": _box(el)})
    return out


@op("table_drop_cols")
def _tbl_drop_cols(slide, shapes, spec, rng):
    out = []
    for path in spec["paths"]:
        el = shapes.get(path)
        tbl = (el.find(f"{q('a:graphic')}/{q('a:graphicData')}/{q('a:tbl')}")
               if el is not None else None)
        if tbl is None:
            continue
        grid = tbl.find(q("a:tblGrid"))
        cols = grid.findall(q("a:gridCol")) if grid is not None else []
        gone = []
        for ci in sorted(spec.get("cols", []), reverse=True):
            if not (0 <= ci < len(cols)):
                continue
            texts = []
            for tr in tbl.findall(q("a:tr")):
                cells = tr.findall(q("a:tc"))
                if ci < len(cells):
                    texts.append(census.element_text(cells[ci])[:40])
                    cells[ci].getparent().remove(cells[ci])
            cols[ci].getparent().remove(cols[ci])
            gone.append({"col": ci, "cells": texts})
        if gone:
            out.append({"path": path, "op": "table_drop_cols", **_label(el),
                        "removed": list(reversed(gone)), "box": _box(el)})
    return out


@op("text_runs")
def _text_runs(slide, shapes, spec, rng):
    """Edit *some* runs of a shape, not all of them.

    `set_font` is whole-shape, which cannot express "the emphasis on two rows
    was wiped" without also flattening the header — and a whole-shape bold
    strip leaves any underline behind, marking the very runs it meant to hide.
    Selects by paragraph index, or by substring, and can delete the paragraph
    outright.
    """
    out = []
    want_idx = set(spec.get("paragraphs", []) or [])
    match = [m.lower() for m in (spec.get("match") or [])]
    for path in spec["paths"]:
        el = shapes.get(path)
        if el is None:
            continue
        label = _label(el)
        touched = []
        for body in el.iter(q("p:txBody")):
            paras = body.findall(q("a:p"))
            for pi, para in enumerate(paras):
                text = census.element_text(para)
                hit = (pi in want_idx) or (match and any(
                    m in text.lower() for m in match))
                if not hit:
                    continue
                if spec.get("delete"):
                    para.getparent().remove(para)
                    touched.append({"paragraph": pi, "was": text[:80],
                                    "action": "deleted"})
                    continue
                for rpr in para.iter(q("a:rPr")):
                    _apply_run_props(rpr, spec)
                touched.append({"paragraph": pi, "was": text[:80],
                                "action": "restyled"})
        if touched:
            out.append({"path": path, "op": "text_runs", **label,
                        "touched": touched,
                        "params": {k: v for k, v in spec.items()
                                   if k not in ("paths", "paragraphs", "match")},
                        "box": _box(el)})
    return out


def _apply_run_props(rpr, spec):
    if "font" in spec:
        for tag in ("a:latin", "a:ea", "a:cs"):
            old = rpr.find(q(tag))
            if old is not None:
                rpr.remove(old)
        etree.SubElement(rpr, q("a:latin")).set("typeface", spec["font"])
    if "size_pt" in spec:
        rpr.set("sz", str(int(spec["size_pt"] * 100)))
    for key, attr in (("bold", "b"), ("italic", "i")):
        if key in spec:
            rpr.set(attr, "1" if spec[key] else "0")
    if "underline" in spec:
        if spec["underline"]:
            rpr.set("u", "sng")
        else:
            rpr.attrib.pop("u", None)
    if "color" in spec:
        for f in list(rpr):
            if etree.QName(f).localname.endswith("Fill"):
                rpr.remove(f)
        sf = etree.SubElement(rpr, q("a:solidFill"))
        etree.SubElement(sf, q("a:srgbClr")).set(
            "val", spec["color"].lstrip("#").upper())


@op("detach_connector")
def _detach(slide, shapes, spec, rng):
    out = []
    for path in spec["paths"]:
        el = shapes.get(path)
        if el is None or el.tag != q("p:cxnSp"):
            continue
        nv = el.find(f"{q('p:nvCxnSpPr')}/{q('p:cNvCxnSpPr')}")
        was = []
        if nv is not None:
            for tag in ("a:stCxn", "a:endCxn"):
                node = nv.find(q(tag))
                if node is not None:
                    was.append({tag: dict(node.attrib)})
                    nv.remove(node)
        box = _box(el)
        if box and spec.get("nudge_in", 0.4):
            _set_box(el, box[0] + spec.get("nudge_in", 0.4) * EMU,
                     box[1] + spec.get("nudge_in", 0.4) * 0.5 * EMU)
        out.append({"path": path, "op": "detach_connector", **_label(el),
                    "was_attachments": was, "box": box})
    return out


@op("crop")
def _crop(slide, shapes, spec, rng):
    """Change or clear a picture's crop rectangle."""
    out = []
    for path in spec["paths"]:
        el = shapes.get(path)
        if el is None:
            continue
        bf = el.find(q("p:blipFill"))
        if bf is None:
            sp = _sp_pr(el)
            bf = sp.find(q("a:blipFill")) if sp is not None else None
        if bf is None:
            continue
        old = bf.find(q("a:srcRect"))
        was = dict(old.attrib) if old is not None else {}
        if old is not None:
            bf.remove(old)
        if spec.get("mode") != "reset":
            sr = etree.Element(q("a:srcRect"))
            for k in ("l", "t", "r", "b"):
                if k in spec:
                    sr.set(k, str(int(spec[k] * 1000)))
            bf.insert(0, sr)
        out.append({"path": path, "op": "crop", **_label(el),
                    "was_srcRect": was, "mode": spec.get("mode", "set"),
                    "box": _box(el)})
    return out


@op("anim_drop_steps")
def _anim_drop_steps(slide, shapes, spec, rng):
    """Remove individual click steps from the build sequence.

    `strip_animation` is all-or-nothing, which cannot express "three of the
    eight builds were lost" — and that is the natural degradation now that a
    keyframe sequence can serve as the reference.  Steps are 1-based, matching
    what anim_steps.py prints.
    """
    from . import anim_steps
    timing = anim_steps.timing_of(slide)
    if timing is None:
        return []
    seq = None
    for node in timing.iter(q("p:seq")):
        ctn = node.find(q("p:cTn"))
        if ctn is not None and ctn.get("nodeType") == "mainSeq":
            seq = node
            break
    if seq is None:
        return []
    kids = seq.find(q("p:cTn")).find(q("p:childTnLst"))
    if kids is None:
        return []
    steps = [c for c in kids if etree.QName(c).localname == "par"]
    gone = []
    for i in sorted(spec.get("steps", []), reverse=True):
        if not (1 <= i <= len(steps)):
            continue
        node = steps[i - 1]
        targets = sorted({t.get("spid") for t in node.iter(q("p:spTgt"))
                          if t.get("spid")})
        gone.append({"step": i, "targets": targets})
        node.getparent().remove(node)
    return ([{"path": "-", "op": "anim_drop_steps",
              "removed": list(reversed(gone)),
              "n_steps_before": len(steps)}] if gone else [])


@op("strip_animation")
def _strip_anim(slide, shapes, spec, rng):
    removed = 0
    for node in list(slide.element.iter()):
        if etree.QName(node).localname == "timing":
            node.getparent().remove(node)
            removed += 1
    return ([{"path": "-", "op": "strip_animation", "removed": removed}]
            if removed else [])


@op("strip_transition")
def _strip_trans(slide, shapes, spec, rng):
    removed = []
    for node in list(slide.element.iter()):
        if etree.QName(node).localname == "transition":
            kids = [etree.QName(c).localname for c in node]
            removed.append(kids[0] if kids else "?")
            node.getparent().remove(node)
    return ([{"path": "-", "op": "strip_transition", "was": removed}]
            if removed else [])


@op("blank_slide")
def _blank(slide, shapes, spec, rng):
    """Strip a slide back to its layout, keeping the placeholders empty."""
    tree = slide.shapes._spTree
    keep = set(spec.get("keep_paths", []))
    out = []
    for path, el in sorted(shapes.items(), key=lambda kv: -len(kv[0])):
        if "/" in path or path in keep:
            continue
        if el.getparent() is None:
            continue
        out.append({"path": path, "op": "blank_slide", **_label(el),
                    "removed_xml": etree.tostring(el).decode()[:1500],
                    "box": _box(el)})
        el.getparent().remove(el)
    return out


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


def run(gt_path: str, recipe: dict, out_path: str) -> dict:
    prs = Presentation(gt_path)
    rng = random.Random(recipe.get("seed", 11))
    delta = {"gt": gt_path, "recipe": recipe.get("name"), "slides": {}}

    for page_str, steps in (recipe.get("slides") or {}).items():
        idx = int(page_str) - 1
        if idx < 0 or idx >= len(prs.slides):
            raise SystemExit(f"slide {page_str} out of range")
        slide = prs.slides[idx]
        entries = []
        # Index ONCE, before anything runs.  Paths are positional, so a delete
        # renumbers every shape after it — re-indexing between steps would
        # silently point later steps at the wrong shapes, and the recipe was
        # written against the digest's numbering, which is the pristine one.
        shapes = index_shapes(slide)
        for step in steps:
            fn = REGISTRY.get(step["op"])
            if fn is None:
                raise SystemExit(f"unknown op {step['op']!r}; "
                                 f"known: {sorted(REGISTRY)}")
            entries += fn(slide, shapes, step, rng)
        dropped = _drop_dead_rels(slide)
        if dropped:
            delta.setdefault("dropped_rels", {})[str(idx)] = dropped
        if entries:
            delta["slides"][str(idx)] = entries

    for page in (recipe.get("delete_slides") or []):
        _drop_slide(prs, page - 1)
        delta.setdefault("deleted_slides", []).append(page)

    if recipe.get("reorder_slides"):
        delta["reorder_slides"] = _reorder(prs, recipe["reorder_slides"])

    for spec in (recipe.get("clear_notes") or []):
        got = _clear_notes(prs, spec)
        if got:
            delta.setdefault("cleared_notes", []).extend(got)

    for spec in (recipe.get("layout") or []):
        got = _layout_edit(prs, spec)
        if got:
            delta.setdefault("layout_edits", []).append(got)

    prs.save(out_path)

    # SmartArt internals live in separate package parts, so partial edits run
    # after the save, straight on the zip.  Without this a "drop one column of
    # three" degradation can only be approximated by deleting the whole
    # graphic, which turns completing a pattern into rebuilding from nothing.
    for spec in (recipe.get("smartart") or []):
        rep = smartart.rewrite(out_path, out_path + ".tmp", spec["slide"],
                               drop_texts=spec.get("drop_text"),
                               drop_ids=spec.get("drop_id"),
                               graphic_index=spec.get("graphic", 0))
        os.replace(out_path + ".tmp", out_path)
        entry = {"path": "-", "op": "smartart_drop_nodes",
                 "slide": spec["slide"], **rep}
        delta["slides"].setdefault(str(spec["slide"] - 1), []).append(entry)

    for spec in (recipe.get("chart") or []):
        rep = charts.rewrite(out_path, out_path + ".tmp", spec["slide"],
                             drop_names=spec.get("drop_name"),
                             drop_index=spec.get("drop_index"),
                             strip=spec.get("strip"),
                             chart_index=spec.get("chart", 0))
        os.replace(out_path + ".tmp", out_path)
        entry = {"path": "-", "op": "chart_edit", "slide": spec["slide"], **rep}
        delta["slides"].setdefault(str(spec["slide"] - 1), []).append(entry)

    delta["input"] = out_path
    return delta


KEEP_RELTYPES = ("slideLayout", "notesSlide", "hyperlink", "tags")


def _drop_dead_rels(slide) -> list[str]:
    """Drop relationships nothing on the slide points at any more.

    Removing a shape from the spTree leaves its relationship — and therefore
    its part — alive and reachable.  For a deleted picture or SmartArt that is
    an **answer leak**: the image, or the diagram data with all its node text,
    is still sitting in the archive for anyone who unzips the input file.
    Shared relationships are safe: a rid is only dropped once no element on
    the slide references it.
    """
    body = etree.tostring(slide.element).decode()
    used = set(re.findall(r'r:(?:id|embed|link|dm|lo|qs|cs|pict)="([^"]+)"', body))
    dropped = []
    for rid, rel in list(slide.part.rels.items()):
        if rid in used or rel.is_external:
            continue
        if any(k in rel.reltype for k in KEEP_RELTYPES):
            continue
        slide.part.drop_rel(rid)
        dropped.append(f"{rid} -> {rel.reltype.rsplit('/', 1)[-1]}")
    return dropped


def _reorder(prs, spec):
    """Shuffle slide order — the anchor is the deck's own narrative.

    `swap` moves shapes; nothing moved slides, which is why the skill's
    "restore the page order" task shape never once appeared in a run.
    """
    lst = prs.slides._sldIdLst
    entries = list(lst)
    if isinstance(spec, dict) and spec.get("swap"):
        pairs = [(a - 1, b - 1) for a, b in spec["swap"]]
    else:
        pairs = [(a - 1, b - 1) for a, b in spec]
    for a, b in pairs:
        entries[a], entries[b] = entries[b], entries[a]
    for e in list(lst):
        lst.remove(e)
    for e in entries:
        lst.append(e)
    return {"swapped": [[a + 1, b + 1] for a, b in pairs]}


def _clear_notes(prs, spec):
    out = []
    for page in spec.get("slides", []):
        slide = prs.slides[page - 1]
        if not slide.has_notes_slide:
            continue
        tf = slide.notes_slide.notes_text_frame
        was = tf.text.strip()
        if not was:
            continue
        tf.text = ""
        out.append({"slide": page, "was": was[:400]})
    return out


def _layout_edit(prs, spec):
    """Break something on a slide layout, so every slide using it breaks.

    This is the only way to pose "is the right fix one edit to the layout, or
    N edits to the slides?" — and telling those apart is a real skill in the
    application, not a trick.
    """
    name = spec.get("layout")
    target = None
    for master in prs.slide_masters:
        for lay in master.slide_layouts:
            if lay.name == name:
                target = lay
                break
    if target is None:
        raise SystemExit(f"layout {name!r} not found")
    shapes = {}
    for i, el in enumerate(census.shape_children(target.shapes._spTree)):
        shapes[str(i)] = el
    removed = []
    for path in spec.get("delete_paths", []):
        el = shapes.get(path)
        if el is None:
            continue
        removed.append({"path": path, **_label(el)})
        el.getparent().remove(el)
    used_by = [i + 1 for i, s in enumerate(prs.slides)
               if s.slide_layout.name == name]
    return {"layout": name, "removed": removed, "affects_slides": used_by}


def _drop_slide(prs, idx):
    xml_slides = prs.slides._sldIdLst
    entries = list(xml_slides)
    prs.part.drop_rel(entries[idx].rId)
    xml_slides.remove(entries[idx])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gt")
    ap.add_argument("recipe")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--delta", default=None)
    args = ap.parse_args()
    recipe = json.loads(Path(args.recipe).read_text())
    delta = run(args.gt, recipe, args.out)
    if args.delta:
        Path(args.delta).write_text(json.dumps(delta, ensure_ascii=False, indent=1))
    n = sum(len(v) for v in delta["slides"].values())
    print(f"{args.out}  {n} change(s) on {len(delta['slides'])} slide(s)")
    for k, v in delta["slides"].items():
        ops = {}
        for e in v:
            ops[e["op"]] = ops.get(e["op"], 0) + 1
        print(f"  p{int(k)+1:<3} {ops}")


if __name__ == "__main__":
    main()
