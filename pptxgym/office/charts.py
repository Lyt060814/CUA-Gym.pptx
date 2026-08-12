"""Partial edits to a native chart: drop a series, strip its furniture.

Same shape of problem as SmartArt.  A chart is its own package part, so the
shape-level ops can only delete the whole graphicFrame — which makes "one of
the three series is missing, add it back" impossible to express, even though
it is one of the better chart tasks: the surviving series pin down the
formatting, and the axis pins down the categories.

And the same shape of trap, in two places:

  **The cache.**  `c:numCache` / `c:strCache` inside the chart part is what
  actually renders; the embedded workbook is only the editing source.  Drop a
  series from the chart XML and the picture is right immediately.

  **The workbook.**  `ppt/embeddings/*.xlsx` still holds every number,
  including the series just removed — an answer leak straight out of `unzip`.
  Rewriting the sheet is possible but brittle; severing the link is not, and
  costs nothing that matters here (the chart keeps rendering from its cache,
  and an agent rebuilding the series types the values in anyway).  So the
  workbook relationship goes with the series.
"""

from __future__ import annotations

import argparse
import json
import re
import zipfile

from lxml import etree

C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

STRIPPABLE = {
    "legend": ("c:chart/c:legend",),
    "title": ("c:chart/c:title",),
    "data_labels": (".//c:dLbls",),
    "gridlines": (".//c:majorGridlines", ".//c:minorGridlines"),
    "axis_titles": (".//c:valAx/c:title", ".//c:catAx/c:title"),
}


def _q(path):
    """Qualify a `c:`-prefixed ElementPath.

    `".//c:dLbls".split("/")` is `['.', '', 'c:dLbls']`, and the empty middle
    segment fell into the qualifying branch, where `"".split(":")[1]` raised
    IndexError.  Every descendant-axis entry in STRIPPABLE goes through here,
    so three of the five things this module can strip — data labels, gridlines
    and axis titles — crashed the moment a recipe asked for one.
    """
    out = []
    for p in path.split("/"):
        out.append(p if (not p or p.startswith(".")) else f"{{{C}}}{p.split(':')[-1]}")
    return "/".join(out)


def series_of(chart_xml: bytes) -> list[dict]:
    root = etree.fromstring(chart_xml)
    out = []
    for i, ser in enumerate(root.iter(f"{{{C}}}ser")):
        name_el = ser.find(f"{{{C}}}tx//{{{C}}}v")
        vals = [v.text for v in ser.findall(
            f"{{{C}}}val//{{{C}}}pt/{{{C}}}v")]
        out.append({"index": i,
                    "idx": (ser.find(f"{{{C}}}idx").get("val")
                            if ser.find(f"{{{C}}}idx") is not None else None),
                    "name": name_el.text if name_el is not None else None,
                    "n_points": len(vals),
                    "sample": vals[:6]})
    return out


def edit_chart(chart_xml: bytes, drop_names=None, drop_index=None, strip=None):
    root = etree.fromstring(chart_xml)
    report = {"removed_series": [], "stripped": []}
    wanted = [w.lower() for w in (drop_names or [])]
    keep = []
    for i, ser in enumerate(list(root.iter(f"{{{C}}}ser"))):
        name_el = ser.find(f"{{{C}}}tx//{{{C}}}v")
        name = name_el.text if name_el is not None else ""
        hit = (i in (drop_index or [])) or any(
            w in (name or "").lower() for w in wanted)
        if hit:
            report["removed_series"].append({"index": i, "name": name})
            ser.getparent().remove(ser)
        else:
            keep.append(ser)

    # idx and order are positional; leaving holes confuses renderers
    for new_i, ser in enumerate(keep):
        for tag in ("idx", "order"):
            el = ser.find(f"{{{C}}}{tag}")
            if el is not None:
                el.set("val", str(new_i))

    for name in (strip or []):
        if name not in STRIPPABLE:
            raise SystemExit(f"chart strip {name!r} is not one of "
                             f"{sorted(STRIPPABLE)}")
        for path in STRIPPABLE[name]:
            for el in root.findall(_q(path)) or root.findall(
                    path.replace("c:", f"{{{C}}}")):
                # a stripped title has to be retyped, so the reward needs to
                # know what it said; the record used to be the bare word
                # "title" and nothing else
                text = "".join(t.text or "" for t in el.iter(f"{{{A}}}t"))
                report["stripped"].append(name)
                report.setdefault("stripped_detail", []).append(
                    {"what": name, "text": text[:200] or None,
                     "xml": etree.tostring(el).decode()[:1200]})
                el.getparent().remove(el)
        if name == "title":
            chart = root.find(f"{{{C}}}chart")
            if chart is not None and chart.find(f"{{{C}}}autoTitleDeleted") is None:
                etree.SubElement(chart, f"{{{C}}}autoTitleDeleted").set("val", "1")

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                          standalone=True), report


def chart_parts_for(z: zipfile.ZipFile, slide_no: int, chart_index=0):
    rels = f"ppt/slides/_rels/slide{slide_no}.xml.rels"
    root = etree.fromstring(z.read(rels))
    charts = []
    for rel in root.findall(f"{{{PKG_REL}}}Relationship"):
        if rel.get("Type", "").endswith("/chart"):
            charts.append(rel.get("Target", "").replace("../", "ppt/"))
    if chart_index >= len(charts):
        raise SystemExit(f"slide {slide_no} has {len(charts)} chart(s)")
    return charts[chart_index]


def _workbook_of(z, chart_part):
    rels = re.sub(r"([^/]+)$", r"_rels/\1.rels", chart_part)
    try:
        root = etree.fromstring(z.read(rels))
    except KeyError:
        return None, None
    for rel in root.findall(f"{{{PKG_REL}}}Relationship"):
        if "package" in rel.get("Type", "") or rel.get("Target", "").endswith(
                (".xlsx", ".xls")):
            return rels, rel.get("Id")
    return rels, None


def rewrite(pptx_in, pptx_out, slide_no, drop_names=None, drop_index=None,
            strip=None, chart_index=0, sever_workbook=True):
    with zipfile.ZipFile(pptx_in) as z:
        part = chart_parts_for(z, slide_no, chart_index)
        chart_xml = z.read(part)
        names = z.namelist()
        blobs = {n: z.read(n) for n in names}
        rels_part, wb_rid = _workbook_of(z, part)

    new_xml, report = edit_chart(chart_xml, drop_names, drop_index, strip)
    blobs[part] = new_xml
    report["chart_part"] = part

    dropped_wb = None
    if sever_workbook and report["removed_series"] and rels_part and wb_rid:
        root = etree.fromstring(blobs[rels_part])
        for rel in root.findall(f"{{{PKG_REL}}}Relationship"):
            if rel.get("Id") == wb_rid:
                target = rel.get("Target", "").replace("../", "ppt/")
                root.remove(rel)
                dropped_wb = target
        blobs[rels_part] = etree.tostring(root, xml_declaration=True,
                                          encoding="UTF-8", standalone=True)
        if dropped_wb:
            # the chart body still points at the workbook through
            # <c:externalData r:id=...>; leaving it behind is a dangling ref
            chart_root = etree.fromstring(blobs[part])
            for ext in chart_root.findall(f"{{{C}}}externalData"):
                chart_root.remove(ext)
            blobs[part] = etree.tostring(chart_root, xml_declaration=True,
                                         encoding="UTF-8", standalone=True)
            names = [n for n in names if n != dropped_wb]
            blobs.pop(dropped_wb, None)
            ct = etree.fromstring(blobs["[Content_Types].xml"])
            for ov in list(ct):
                if ov.get("PartName", "").lstrip("/") == dropped_wb:
                    ct.remove(ov)
            blobs["[Content_Types].xml"] = etree.tostring(
                ct, xml_declaration=True, encoding="UTF-8", standalone=True)
    report["dropped_workbook"] = dropped_wb

    with zipfile.ZipFile(pptx_out, "w", zipfile.ZIP_DEFLATED) as zo:
        for n in names:
            zo.writestr(n, blobs[n])
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pptx")
    ap.add_argument("--slide", type=int, required=True)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--drop-name", nargs="*", default=None)
    ap.add_argument("--drop-index", nargs="*", type=int, default=None)
    ap.add_argument("--strip", nargs="*", default=None,
                    choices=sorted(STRIPPABLE))
    ap.add_argument("--chart", type=int, default=0)
    ap.add_argument("-o", "--out")
    args = ap.parse_args()
    if args.list:
        with zipfile.ZipFile(args.pptx) as z:
            part = chart_parts_for(z, args.slide, args.chart)
            for s in series_of(z.read(part)):
                print(f"  [{s['index']}] {s['name']!r}  {s['n_points']} pts  "
                      f"{s['sample']}")
            print("  --", part)
        return
    if not args.out:
        raise SystemExit("-o required")
    print(json.dumps(rewrite(args.pptx, args.out, args.slide, args.drop_name,
                             args.drop_index, args.strip, args.chart),
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
