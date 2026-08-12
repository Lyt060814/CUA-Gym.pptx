"""Remove nodes from inside a SmartArt graphic.

Four proposals in run 2 asked for a *partial* edit of a diagram — drop one
column of three, leave two of six boxes — and every one had to be approximated
by deleting the whole graphic, which silently changed the task from "complete
the pattern" to "rebuild it from nothing".

That was a missing tool, not an OOXML limitation.  A SmartArt is four parts
plus a cache, all of them plain XML:

    slide  ->  dgm:relIds  r:dm data.xml   the data model: the actual content
                           r:lo layout.xml the layout algorithm
                           r:qs/r:cs       style and colours
           ->  (extension) drawing.xml     a *cached* render, as dsp:sp shapes

The data model holds `dgm:pt` nodes (with the text), `parTrans`/`sibTrans`
companions, `pres` points produced by the layout pass, and a `dgm:cxnLst`
wiring them together.  Each `dsp:sp` in the cache carries the `modelId` of the
pres point it draws, so node -> presOf connection -> pres point -> cached
shape is a complete chain in both directions.

**The cache is why the naive edit fails.**  Strip a node from the data model
alone and PowerPoint re-lays-out on open — but LibreOffice prefers the cache
and renders the old picture, so the file says one thing and our render says
another.  Deleting the cache instead makes LibreOffice fall back to its own
partial layout engine, which draws a different diagram.  So both sides are
edited together: the node leaves the data model (no answer left in the zip)
and its shape leaves the cache (every renderer agrees).  The survivors keep
their original geometry, so the gap reads as a gap rather than a re-flow —
which is exactly what "two of six are missing" should look like.
"""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from lxml import etree

DGM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
DSP = "http://schemas.microsoft.com/office/drawing/2008/diagram"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _text(el):
    return " ".join(t.text or "" for t in el.iter(f"{{{A}}}t")).strip()


def read_nodes(data_xml: bytes) -> list[dict]:
    """The content-bearing nodes of a data model, in document order."""
    root = etree.fromstring(data_xml)
    out = []
    for pt in root.iter(f"{{{DGM}}}pt"):
        if (pt.get("type") or "node") != "node":
            continue
        out.append({"modelId": pt.get("modelId"), "text": _text(pt)})
    return out


def _connections(root):
    return list(root.iter(f"{{{DGM}}}cxn"))


def strip_nodes(data_xml: bytes, drawing_xml: bytes | None, drop_ids: set[str]):
    """Return (new_data, new_drawing, report) with drop_ids removed."""
    data = etree.fromstring(data_xml)
    report = {"removed_nodes": [], "removed_pres": [], "removed_cxn": 0,
              "removed_shapes": 0}

    pts = {pt.get("modelId"): pt for pt in data.iter(f"{{{DGM}}}pt")}
    doomed = set()
    for nid in drop_ids:
        pt = pts.get(nid)
        if pt is None:
            continue
        doomed.add(nid)
        report["removed_nodes"].append({"modelId": nid, "text": _text(pt)})

    # a node drags along its transition companions and its presentation points
    for cxn in _connections(data):
        src, dst = cxn.get("srcId"), cxn.get("destId")
        kind = cxn.get("type") or "parOf"
        if src in doomed or dst in doomed:
            for key in ("parTransId", "sibTransId"):
                tid = cxn.get(key)
                if tid and tid in pts:
                    doomed.add(tid)
            if kind == "presOf" and dst not in doomed and src in doomed:
                doomed.add(dst)
                report["removed_pres"].append(dst)
            if kind == "presParOf" and dst in doomed:
                pass

    # presOf may be recorded in either direction depending on the writer
    for cxn in _connections(data):
        if (cxn.get("type") == "presOf" and cxn.get("srcId") in doomed
                and cxn.get("destId") not in doomed):
            doomed.add(cxn.get("destId"))
            report["removed_pres"].append(cxn.get("destId"))

    # A layout does not give a node one shape, it gives it a *container*: the
    # heading, the body text and usually a decorative icon all hang off one
    # pres point by presParOf, and only the heading carries the presOf link
    # back to the node.  Follow presOf alone and a deleted column keeps its
    # icon.  So climb from the node's pres point to its container, and take
    # the whole container — but only once nothing inside it belongs to a node
    # that is staying, which is what keeps sibling columns safe.
    par = {}
    kids = {}
    for cxn in _connections(data):
        if cxn.get("type") == "presParOf":
            par[cxn.get("destId")] = cxn.get("srcId")
            kids.setdefault(cxn.get("srcId"), []).append(cxn.get("destId"))
    owner = {}
    for cxn in _connections(data):
        if cxn.get("type") == "presOf":
            owner[cxn.get("destId")] = cxn.get("srcId")

    def subtree(root):
        out, stack = set(), [root]
        while stack:
            n = stack.pop()
            for k in kids.get(n, []):
                if k not in out:
                    out.add(k)
                    stack.append(k)
        return out

    for pres_id in [p for p in list(doomed) if p in par]:
        container = par[pres_id]
        if container is None or container in doomed:
            continue
        inside = subtree(container) | {container}
        if any(owner.get(x) and owner[x] not in doomed for x in inside):
            continue                       # a surviving node lives in here
        for x in inside:
            if x not in doomed:
                doomed.add(x)
                report["removed_pres"].append(x)

    for mid in list(doomed):
        pt = pts.get(mid)
        if pt is not None and pt.getparent() is not None:
            pt.getparent().remove(pt)

    for cxn in _connections(data):
        ids = {cxn.get("srcId"), cxn.get("destId"),
               cxn.get("parTransId"), cxn.get("sibTransId")}
        if ids & doomed:
            cxn.getparent().remove(cxn)
            report["removed_cxn"] += 1

    new_drawing = None
    if drawing_xml:
        draw = etree.fromstring(drawing_xml)
        for sp in list(draw.iter(f"{{{DSP}}}sp")):
            if sp.get("modelId") in doomed:
                sp.getparent().remove(sp)
                report["removed_shapes"] += 1
        new_drawing = etree.tostring(draw, xml_declaration=True,
                                     encoding="UTF-8", standalone=True)

    return (etree.tostring(data, xml_declaration=True, encoding="UTF-8",
                           standalone=True), new_drawing, report)


def diagram_parts_for(z: zipfile.ZipFile, slide_no: int, graphic_index=0):
    """(data_part, drawing_part) behind the Nth SmartArt on a slide."""
    slide = f"ppt/slides/slide{slide_no}.xml"
    rels = f"ppt/slides/_rels/slide{slide_no}.xml.rels"
    body = z.read(slide)
    rel_root = etree.fromstring(z.read(rels))
    table = {}
    for rel in rel_root.findall(f"{{{PKG_REL}}}Relationship"):
        target = rel.get("Target", "").replace("../", "ppt/")
        table[rel.get("Id")] = (rel.get("Type", "").rsplit("/", 1)[-1], target)

    root = etree.fromstring(body)
    frames = [n for n in root.iter() if etree.QName(n).localname == "relIds"]
    if graphic_index >= len(frames):
        raise SystemExit(f"slide {slide_no} has {len(frames)} SmartArt graphic(s)")
    rel = frames[graphic_index]
    dm = rel.get(f"{{{R}}}dm")
    data_part = table.get(dm, (None, None))[1]
    drawing_part = None
    for kind, tgt in table.values():
        if kind == "diagramDrawing" and data_part:
            n = re.search(r"(\d+)\.xml$", data_part)
            if n and tgt.endswith(f"drawing{n.group(1)}.xml"):
                drawing_part = tgt
    return data_part, drawing_part


def rewrite(pptx_in, pptx_out, slide_no, drop_texts=None, drop_ids=None,
            graphic_index=0):
    with zipfile.ZipFile(pptx_in) as z:
        data_part, drawing_part = diagram_parts_for(z, slide_no, graphic_index)
        if not data_part:
            raise SystemExit("no diagram data part found")
        data_xml = z.read(data_part)
        drawing_xml = z.read(drawing_part) if drawing_part else None
        names = z.namelist()
        blobs = {n: z.read(n) for n in names}

    ids = set(drop_ids or [])
    if drop_texts:
        wanted = [t.lower() for t in drop_texts]
        for n in read_nodes(data_xml):
            low = (n["text"] or "").lower()
            if any(w in low for w in wanted):
                ids.add(n["modelId"])

    new_data, new_drawing, report = strip_nodes(data_xml, drawing_xml, ids)
    blobs[data_part] = new_data
    if drawing_part and new_drawing:
        blobs[drawing_part] = new_drawing

    with zipfile.ZipFile(pptx_out, "w", zipfile.ZIP_DEFLATED) as zo:
        for n in names:
            zo.writestr(n, blobs[n])
    report["data_part"] = data_part
    report["drawing_part"] = drawing_part
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pptx")
    ap.add_argument("--slide", type=int, required=True)
    ap.add_argument("--list", action="store_true", help="print the nodes and exit")
    ap.add_argument("--drop-text", nargs="*", default=None,
                    help="substring match against node text")
    ap.add_argument("--drop-id", nargs="*", default=None)
    ap.add_argument("--graphic", type=int, default=0)
    ap.add_argument("-o", "--out")
    args = ap.parse_args()

    if args.list:
        with zipfile.ZipFile(args.pptx) as z:
            dp, dr = diagram_parts_for(z, args.slide, args.graphic)
            for n in read_nodes(z.read(dp)):
                print(f"  {n['modelId']}  {n['text'][:70]}")
            print(f"  -- data={dp} drawing={dr}")
        return
    if not args.out:
        raise SystemExit("-o is required")
    rep = rewrite(args.pptx, args.out, args.slide, args.drop_text, args.drop_id,
                  args.graphic)
    print(json.dumps(rep, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
