"""Self-contained OOXML inventory + degradation evaluator, embedded into every
rollout task file verbatim.

Stdlib only — zipfile and ElementTree.  A task class that runs inside the
harness must not depend on python-pptx being installed in the evaluator
process, and it must be readable as one file.

The inventory is deliberately *semantic*, not byte-level.  A solver who
rebuilds a shape by hand will not reproduce the original XML, and should not
have to: what is compared is where a shape sits, what it says, which image it
carries, how its text is styled, and whether a composite object still is one.
"""

RUNTIME = r'''
NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}
EMU_IN = 914400
SHAPE_TAGS = {"sp", "pic", "graphicFrame", "cxnSp", "grpSp"}


def _ln(tag):
    return tag.rsplit("}", 1)[-1]


def _rels_name(part):
    d, _, n = part.rpartition("/")
    return f"{d}/_rels/{n}.rels" if d else f"_rels/{n}.rels"


def _resolve(base, target):
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), target))


def _rels(z, part):
    name = _rels_name(part)
    if name not in z.namelist():
        return {}
    out = {}
    for node in ET.fromstring(z.read(name)):
        if _ln(node.tag) != "Relationship":
            continue
        rid = node.attrib.get("Id")
        if rid:
            out[rid] = {
                "type": node.attrib.get("Type", "").rsplit("/", 1)[-1],
                "target": node.attrib.get("Target", ""),
                "resolved": _resolve(part, node.attrib.get("Target", "")),
                "external": node.attrib.get("TargetMode") == "External",
            }
    return out


def _shape_children(node):
    """Drawable children, descending through mc:AlternateContent."""
    for child in node:
        if _ln(child.tag) == "AlternateContent":
            branch = None
            for b in child:
                if _ln(b.tag) == "Choice":
                    branch = b
                    break
            if branch is None:
                for b in child:
                    if _ln(b.tag) == "Fallback":
                        branch = b
                        break
            if branch is not None:
                yield from _shape_children(branch)
        elif _ln(child.tag) in SHAPE_TAGS:
            yield child


def _text_of(node):
    parts = [(t.text or "") for t in node.iter() if _ln(t.tag) == "t"]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _find(node, *path):
    cur = node
    for want in path:
        nxt = None
        for child in cur:
            if _ln(child.tag) == want:
                nxt = child
                break
        if nxt is None:
            return None
        cur = nxt
    return cur


def _xfrm_of(el):
    if _ln(el.tag) == "graphicFrame":
        xf = _find(el, "xfrm")
        if xf is not None:
            return xf
    sp = _find(el, "spPr") or _find(el, "grpSpPr")
    return _find(sp, "xfrm") if sp is not None else None


def _box(el):
    xf = _xfrm_of(el)
    if xf is None:
        return None
    off = _find(xf, "off")
    ext = _find(xf, "ext")
    if off is None or ext is None:
        return None
    try:
        return [int(off.attrib["x"]), int(off.attrib["y"]),
                int(ext.attrib["cx"]), int(ext.attrib["cy"])]
    except (KeyError, ValueError):
        return None


def _kind(el):
    tag = _ln(el.tag)
    if tag == "pic":
        return "picture"
    if tag == "cxnSp":
        return "connector"
    if tag == "grpSp":
        return "group"
    if tag == "graphicFrame":
        gd = _find(el, "graphic", "graphicData")
        uri = gd.attrib.get("uri", "") if gd is not None else ""
        if uri.endswith("/table"):
            return "table"
        if uri.endswith("/chart"):
            return "chart"
        if uri.endswith("/diagram"):
            return "smartart"
        return "graphicframe"
    for node in el.iter():
        if _ln(node.tag) == "ph":
            return "placeholder"
    return "textbox" if _text_of(el) else "autoshape"


def _color_of(node):
    if node is None:
        return None
    for child in node.iter():
        n = _ln(child.tag)
        if n == "srgbClr":
            return "#" + (child.attrib.get("val") or "").upper()
        if n == "schemeClr":
            return "scheme:" + (child.attrib.get("val") or "")
    return None


def _line_of(el):
    sp = _find(el, "spPr")
    ln = _find(sp, "ln") if sp is not None else None
    if ln is None:
        return None
    no_fill = any(_ln(c.tag) == "noFill" for c in ln)
    return {
        "w_emu": int(ln.attrib["w"]) if ln.attrib.get("w", "").isdigit() else None,
        "color": None if no_fill else _color_of(_find(ln, "solidFill")),
        "none": no_fill,
    }


def _fill_of(el):
    sp = _find(el, "spPr")
    if sp is None:
        return None
    for child in sp:
        n = _ln(child.tag)
        if n == "solidFill":
            return {"type": "solid", "color": _color_of(child)}
        if n == "noFill":
            return {"type": "none"}
        if n == "gradFill":
            return {"type": "gradient", "color": _color_of(child)}
        if n == "blipFill":
            return {"type": "picture"}
    return None


def _effects_of(el):
    sp = _find(el, "spPr")
    if sp is None:
        return []
    out = []
    for child in sp:
        n = _ln(child.tag)
        if n in ("effectLst", "effectDag"):
            out += sorted({_ln(g.tag) for g in child})
        elif n in ("sp3d", "scene3d"):
            out.append(n)
    return sorted(set(out))


def _runs_of(el):
    """Explicit run properties, per paragraph. Inheritance is not resolved —
    a solver restyling text writes explicit properties, and that is what the
    original carries too."""
    paras = []
    for body in el.iter():
        if _ln(body.tag) != "txBody":
            continue
        for para in body:
            if _ln(para.tag) != "p":
                continue
            runs = []
            for node in para:
                if _ln(node.tag) not in ("r", "fld"):
                    continue
                rpr = _find(node, "rPr")
                t = _find(node, "t")
                txt = (t.text or "") if t is not None else ""
                if not txt.strip():
                    continue
                item = {"t": txt.strip()[:60]}
                if rpr is not None:
                    if rpr.attrib.get("sz", "").isdigit():
                        item["sz"] = int(rpr.attrib["sz"]) / 100.0
                    if rpr.attrib.get("b") is not None:
                        item["b"] = rpr.attrib["b"] in ("1", "true")
                    if rpr.attrib.get("i") is not None:
                        item["i"] = rpr.attrib["i"] in ("1", "true")
                    u = rpr.attrib.get("u")
                    if u and u != "none":
                        item["u"] = u
                    latin = _find(rpr, "latin")
                    if latin is not None and latin.attrib.get("typeface"):
                        item["font"] = latin.attrib["typeface"]
                    c = _color_of(_find(rpr, "solidFill"))
                    if c:
                        item["color"] = c
                runs.append(item)
            if runs:
                paras.append(runs)
    return paras


def _table_of(el):
    tbl = _find(el, "graphic", "graphicData", "tbl")
    if tbl is None:
        return None
    rows = []
    for tr in tbl:
        if _ln(tr.tag) != "tr":
            continue
        rows.append([_text_of(tc)[:80] for tc in tr if _ln(tc.tag) == "tc"])
    return {"rows": len(rows), "cols": max((len(r) for r in rows), default=0),
            "cells": rows}


def _chart_of(z, el, part):
    gd = _find(el, "graphic", "graphicData")
    if gd is None:
        return None
    ref = None
    for child in gd:
        if _ln(child.tag) == "chart":
            ref = child
            break
    if ref is None:
        return None
    rid = ref.attrib.get(f"{{{NS['r']}}}id")
    rel = _rels(z, part).get(rid or "")
    if not rel or rel["resolved"] not in z.namelist():
        return None
    root = ET.fromstring(z.read(rel["resolved"]))
    plot = None
    for node in root.iter():
        n = _ln(node.tag)
        if n.endswith("Chart") and n != "chartSpace":
            plot = n
            break
    series = []
    for ser in root.iter():
        if _ln(ser.tag) != "ser":
            continue
        name = None
        tx = _find(ser, "tx")
        if tx is not None:
            vals = [v.text for v in tx.iter() if _ln(v.tag) == "v"]
            name = vals[0] if vals else None
        vals = []
        val = _find(ser, "val")
        if val is not None:
            vals = [v.text for v in val.iter() if _ln(v.tag) == "v"]
        series.append({"name": name, "n": len(vals), "vals": vals[:24]})
    return {"plot": plot, "n_series": len(series), "series": series,
            "has_legend": any(_ln(n.tag) == "legend" for n in root.iter()),
            "title": _text_of(_find(root, "chart", "title"))
            if _find(root, "chart", "title") is not None else None}


def _diagram_of(z, el, part):
    gd = _find(el, "graphic", "graphicData")
    if gd is None:
        return None
    rel_node = None
    for child in gd:
        if _ln(child.tag) == "relIds":
            rel_node = child
            break
    if rel_node is None:
        return None
    dm = rel_node.attrib.get(f"{{{NS['r']}}}dm")
    rel = _rels(z, part).get(dm or "")
    if not rel or rel["resolved"] not in z.namelist():
        return None
    root = ET.fromstring(z.read(rel["resolved"]))
    nodes = []
    for pt in root.iter():
        if _ln(pt.tag) != "pt":
            continue
        if (pt.attrib.get("type") or "node") != "node":
            continue
        txt = _text_of(pt)
        if txt:
            nodes.append(txt[:80])
    return {"n_nodes": len(nodes), "nodes": nodes}


def _image_sha(z, el, part):
    blip = None
    for node in el.iter():
        if _ln(node.tag) == "blip":
            blip = node
            break
    if blip is None:
        return None
    rid = blip.attrib.get(f"{{{NS['r']}}}embed")
    rel = _rels(z, part).get(rid or "")
    if not rel or rel["resolved"] not in z.namelist():
        return None
    return hashlib.sha256(z.read(rel["resolved"])).hexdigest()[:16]


def inventory_pptx(path):
    """Semantic inventory of every drawable shape, slide by slide."""
    out = {"slides": [], "n_slides": 0}
    with zipfile.ZipFile(path) as z:
        pres = "ppt/presentation.xml"
        order = []
        if pres in z.namelist():
            rels = _rels(z, pres)
            root = ET.fromstring(z.read(pres))
            for lst in root:
                if _ln(lst.tag) != "sldIdLst":
                    continue
                for sid in lst:
                    rid = sid.attrib.get(f"{{{NS['r']}}}id")
                    rel = rels.get(rid or "")
                    if rel:
                        order.append(rel["resolved"])
        if not order:
            order = sorted(n for n in z.namelist()
                           if re.match(r"ppt/slides/slide\d+\.xml$", n))
        out["n_slides"] = len(order)

        for idx, part in enumerate(order, start=1):
            root = ET.fromstring(z.read(part))
            tree = _find(root, "cSld", "spTree")
            shapes = []

            def walk(container, prefix):
                for i, el in enumerate(_shape_children(container)):
                    p = f"{prefix}{i}"
                    kind = _kind(el)
                    rec = {"path": p, "kind": kind, "box": _box(el)}
                    txt = _text_of(el)
                    if txt:
                        rec["text"] = txt[:200]
                        rec["text_sha"] = hashlib.sha256(
                            txt.encode("utf-8")).hexdigest()[:16]
                    sha = _image_sha(z, el, part)
                    if sha:
                        rec["image"] = sha
                    ln = _line_of(el)
                    if ln:
                        rec["line"] = ln
                    fill = _fill_of(el)
                    if fill:
                        rec["fill"] = fill
                    fx = _effects_of(el)
                    if fx:
                        rec["effects"] = fx
                    runs = _runs_of(el)
                    if runs:
                        rec["runs"] = runs
                    if kind == "table":
                        rec["table"] = _table_of(el)
                    elif kind == "chart":
                        rec["chart"] = _chart_of(z, el, part)
                    elif kind == "smartart":
                        rec["diagram"] = _diagram_of(z, el, part)
                    xf = _xfrm_of(el)
                    if xf is not None and xf.attrib.get("rot"):
                        try:
                            rec["rot"] = int(xf.attrib["rot"]) / 60000.0
                        except ValueError:
                            pass
                    shapes.append(rec)
                    if kind == "group":
                        walk(el, p + "/")

            if tree is not None:
                walk(tree, "")
            out["slides"].append({"page": idx, "part": part, "shapes": shapes})
    return out
'''
