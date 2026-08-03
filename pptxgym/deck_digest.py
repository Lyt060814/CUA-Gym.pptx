"""Compact structural digest for the task proposer.

A raw census is far too big to hand a proposing agent, and most of it is
noise.  This surfaces what the proposal criteria actually need:

  · the visual system — colours, geometry and *typography* conventions
  · **repetition** — clusters of look-alike shapes, because a redundant set is
    the single most valuable thing to degrade (a survivor becomes the anchor)
  · whole objects worth deleting, with enough of their content (chart values,
    table cells, SmartArt nodes) to judge whether a rebuild is well-posed
  · the structure a render cannot show: grouping, z-order, connector topology,
    layout/master identity, notes, animation
  · everything else the deck contains, at least as a count — nothing is
    silently dropped, because "not mentioned" reads as "not there"

The one thing deliberately withheld is which of our operators apply: the
proposer must judge what SHOULD be degraded, never what is convenient to
implement.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path


from . import anim_steps
from . import census
from . import styles
from . import text_style as tsmod

EMU = 914400


def _in(v):
    return round(v / EMU, 2)


def _sig(rec, resolver):
    """Look-alike signature: same signature ⇒ visually interchangeable."""
    st = rec.get("style") or {}
    fill = st.get("fill") or {}
    ln = st.get("line") or {}
    fc = resolver.resolve(fill.get("color")) if fill.get("color") else None
    lc = resolver.resolve(ln.get("color")) if ln.get("color") else None
    return (rec["kind"], st.get("prstGeom"),
            round(rec["w"] / EMU, 1), round(rec["h"] / EMU, 1),
            fill.get("type"), fc, lc, round(ln.get("w", 0) / 6350),
            tuple(sorted((st.get("effects") or {}))))


def _clusters(shapes, resolver, slide_w, slide_h):
    """Groups of >=2 look-alike shapes, described by how they are arranged."""
    buckets = defaultdict(list)
    for r in shapes:
        if r["kind"] == "group" or r["semantic"] == "background":
            continue
        buckets[_sig(r, resolver)].append(r)
    out = []
    for sig, members in buckets.items():
        if len(members) < 2:
            continue
        xs = [m["cx"] for m in members]
        ys = [m["cy"] for m in members]
        spread_x = (max(xs) - min(xs)) / slide_w
        spread_y = (max(ys) - min(ys)) / slide_h
        if spread_x < 0.06:
            arrange = "竖排一列"
        elif spread_y < 0.06:
            arrange = "横排一行"
        else:
            arrange = "散布(网格或自由)"
        out.append({
            "n": len(members), "kind": sig[0], "geom": sig[1],
            "size_in": [sig[2], sig[3]], "arrangement": arrange,
            "fill": sig[4], "fill_rgb": sig[5], "line_rgb": sig[6],
            "effects": list(sig[8]),
            "members": [{"path": m["path"],
                         "at": [round(m["cx"] / slide_w, 2),
                                round(m["cy"] / slide_h, 2)],
                         "text": (m.get("text") or "")[:44]} for m in members],
        })
    return sorted(out, key=lambda c: -c["n"])


def _visual_system(shapes, resolver):
    """Shape-level *and* type-level conventions of a slide."""
    fills, lines, radii, effects, widths = (Counter(), Counter(), Counter(),
                                            Counter(), Counter())
    fonts, sizes, tcolors, aligns, bullets = (Counter(), Counter(), Counter(),
                                              Counter(), Counter())
    bold = 0
    for r in shapes:
        st = r.get("style") or {}
        f = (st.get("fill") or {})
        if f.get("color"):
            rgb = resolver.resolve(f["color"])
            if rgb:
                fills["#%02X%02X%02X" % rgb] += 1
        ln = st.get("line") or {}
        if ln.get("color"):
            rgb = resolver.resolve(ln["color"])
            if rgb:
                lines["#%02X%02X%02X" % rgb] += 1
        if ln.get("w"):
            widths[round(ln["w"] / 12700, 1)] += 1
        if st.get("prstGeom"):
            radii[st["prstGeom"]] += 1
        for e in (st.get("effects") or {}):
            effects[e] += 1

        ts = r.get("text_style")
        if not ts:
            continue
        for para in ts["paragraphs"]:
            pr = para.get("props") or {}
            if pr.get("algn"):
                aligns[pr["algn"]] += 1
            if pr.get("bullet") and pr["bullet"] != "none":
                bullets[pr["bullet"]] += 1
            for run in para["runs"]:
                if not (run.get("t") or "").strip():
                    continue
                if run.get("font"):
                    fonts[run["font"]] += 1
                if run.get("sz"):
                    sizes[round(run["sz"], 1)] += 1
                if run.get("b"):
                    bold += 1
                if run.get("color"):
                    rgb = resolver.resolve(run["color"])
                    if rgb:
                        tcolors["#%02X%02X%02X" % rgb] += 1
    out = {"fill_colors": fills.most_common(6), "line_colors": lines.most_common(4),
           "line_widths_pt": widths.most_common(4),
           "geometries": radii.most_common(6), "effects": effects.most_common(5)}
    typo = {}
    if fonts:
        typo["fonts"] = fonts.most_common(5)
    if sizes:
        typo["sizes_pt"] = sizes.most_common(8)
    if tcolors:
        typo["text_colors"] = tcolors.most_common(5)
    if aligns:
        typo["align"] = aligns.most_common(4)
    if bullets:
        typo["bullets"] = bullets.most_common(4)
    if bold:
        typo["bold_runs"] = bold
    if typo:
        out["typography"] = typo
    return out


def _objects(shapes, slide_w, slide_h):
    """Whole objects worth deleting, with the content a rebuild would need."""
    out = []
    for r in shapes:
        if r["kind"] not in ("chart", "table", "picture", "smartart", "group",
                             "graphicframe"):
            continue
        item = {"path": r["path"], "kind": r["kind"],
                "at": [round(r["cx"] / slide_w, 2), round(r["cy"] / slide_h, 2)],
                "size_in": [round(r["w"] / EMU, 1), round(r["h"] / EMU, 1)]}
        c = r.get("chart")
        if c:
            item.update({
                "plot": c.get("plot"), "title": c.get("title"),
                "n_series": c.get("n_series"),
                # values matter: whether a chart rebuild is well-posed depends
                # on whether the numbers exist anywhere the agent can reach
                "series": [{"name": s.get("name"),
                            "cats": (s.get("cats") or [])[:12],
                            "vals": (s.get("vals") or [])[:12]}
                           for s in (c.get("series") or [])[:6]],
            })
        t = r.get("table")
        if t:
            item.update({"n_rows": t["n_rows"], "n_cols": t["n_cols"],
                         "header": t["first_row_header"],
                         "merges": t["merges"][:8],
                         "cells": [row["cells"] for row in t["rows"][:10]]})
        d = r.get("diagram")
        if d:
            item.update({"diagram_layout": d.get("layout"),
                         "n_nodes": d.get("n_nodes"),
                         "nodes": (d.get("nodes") or [])[:20]})
        if r.get("crop"):
            item["crop"] = r["crop"]
        if r["kind"] == "group":
            kids = [x for x in shapes if x.get("group_path") == r["path"]]
            item["n_children"] = len(kids)
            item["children"] = [{"path": k["path"], "kind": k["kind"],
                                 "text": (k.get("text") or "")[:30]}
                                for k in kids[:14]]
        if r.get("text"):
            item["text"] = r["text"][:60]
        if r.get("link"):
            item["link"] = r["link"]
        if r.get("hard_target"):
            item["hard_target"] = r["hard_target"]
        out.append(item)
    return out


def _connectors(shapes, slide_w, slide_h, id_to_path):
    """Connector topology — the thing a render shows but a render cannot label."""
    out = []
    for r in shapes:
        if r["kind"] != "connector":
            continue
        item = {"path": r["path"],
                "at": [round(r["cx"] / slide_w, 2), round(r["cy"] / slide_h, 2)],
                "len_in": round(math.hypot(r["w"], r["h"]) / EMU, 2)}
        if r.get("st_cxn") is not None or r.get("end_cxn") is not None:
            item["attached"] = [id_to_path.get(r.get("st_cxn")),
                                id_to_path.get(r.get("end_cxn"))]
        else:
            item["attached"] = None          # floating line, not glued to shapes
        st = r.get("style") or {}
        ln = st.get("line") or {}
        if ln.get("w"):
            item["w_pt"] = round(ln["w"] / 12700, 1)
        if st.get("prstGeom"):
            item["geom"] = st["prstGeom"]
        out.append(item)
    return out


def _overlaps(shapes, slide_w, slide_h):
    """Pairs whose boxes overlap materially — layering the render flattens."""
    boxes = [(r, r["cx"] - r["w"] / 2, r["cy"] - r["h"] / 2,
              r["cx"] + r["w"] / 2, r["cy"] + r["h"] / 2)
             for r in shapes if r["w"] > 0 and r["h"] > 0]
    out = []
    for i in range(len(boxes)):
        ra, ax0, ay0, ax1, ay1 = boxes[i]
        for j in range(i + 1, len(boxes)):
            rb, bx0, by0, bx1, by1 = boxes[j]
            ox = min(ax1, bx1) - max(ax0, bx0)
            oy = min(ay1, by1) - max(ay0, by0)
            if ox <= 0 or oy <= 0:
                continue
            inter = ox * oy
            smaller = min((ax1 - ax0) * (ay1 - ay0), (bx1 - bx0) * (by1 - by0))
            if smaller and inter / smaller >= 0.35:
                lo, hi = (ra, rb) if ra["z"] < rb["z"] else (rb, ra)
                out.append({"under": lo["path"], "over": hi["path"],
                            "frac": round(inter / smaller, 2)})
    return sorted(out, key=lambda o: -o["frac"])[:10]


def _shape_row(r, sw, sh, resolver):
    row = {"path": r["path"], "kind": r["kind"],
           "at": [round(r["cx"] / sw, 2), round(r["cy"] / sh, 2)],
           "size_in": [round(r["w"] / EMU, 1), round(r["h"] / EMU, 1)],
           "z": r["z"],
           "geom": (r.get("style") or {}).get("prstGeom"),
           "text": (r.get("text") or "")[:60]}
    if r.get("rot"):
        row["rot"] = r["rot"]
    if r.get("group_path"):
        row["in_group"] = r["group_path"]
    if r.get("placeholder"):
        row["ph"] = r["placeholder"]["type"]
    ts = tsmod.summarize(r.get("text_style"), resolver)
    if ts:
        row["type_style"] = ts
    if r.get("crop"):
        row["crop"] = r["crop"]
    if r.get("link"):
        row["link"] = r["link"]
    if r.get("image_sha"):
        row["image"] = r["image_sha"][:8]
    if r.get("hard_target"):
        row["hard_target"] = r["hard_target"]
    return row


def digest(pptx_path: str, max_shapes_listed=40, drift: dict | None = None) -> dict:
    cen = census.census_deck(pptx_path)
    resolver = styles.ThemeResolver(pptx_path)
    try:
        anim = anim_steps.deck_animation(pptx_path)
    except Exception:                                        # noqa: BLE001
        anim = {"slides": [], "transition_kinds": [], "n_animated": 0}
    anim_by_page = {a["page"]: a for a in anim["slides"]}
    sw, sh = cen["slide_w"], cen["slide_h"]
    slides = []
    deck_fonts, deck_sizes, deck_layouts = Counter(), Counter(), Counter()
    hidden, with_notes, with_anim, with_trans = [], [], [], []
    hard = Counter()

    for s in cen["slides"]:
        real = [r for r in s["shapes"] if r["kind"] != "group"]
        content = [r for r in real if r["semantic"] == "content"]
        decor = [r for r in real if r["semantic"] == "decorative"]
        bg = [r for r in real if r["semantic"] == "background"]
        id_to_path = {r["shape_id"]: r["path"] for r in s["shapes"]
                      if r.get("shape_id") is not None}
        big = sorted(content, key=lambda r: -(r["w"] * r["h"]))[:max_shapes_listed]
        meta = s.get("meta") or {}
        page = s["idx"] + 1

        if meta.get("layout"):
            deck_layouts[meta["layout"]] += 1
        if meta.get("hidden"):
            hidden.append(page)
        if meta.get("notes"):
            with_notes.append(page)
        if meta.get("animation"):
            with_anim.append(page)
        if meta.get("transition"):
            with_trans.append(page)

        for r in real:
            ht = r.get("hard_target") or {}
            for k in ("custom_geometry", "ole", "media"):
                if ht.get(k):
                    hard[k] += 1

        vs = _visual_system(content, resolver)
        for f, n in (vs.get("typography", {}).get("fonts") or []):
            deck_fonts[f] += n
        for z, n in (vs.get("typography", {}).get("sizes_pt") or []):
            deck_sizes[z] += n

        entry = {
            "index": s["idx"], "page": page,
            "layout": meta.get("layout"), "master": meta.get("master"),
            "n_content_shapes": len(content),
            "shape_census": {"content": len(content), "decorative": len(decor),
                             "background": len(bg),
                             "groups": sum(1 for r in s["shapes"]
                                           if r["kind"] == "group")},
            "visual_system": vs,
            "repetition": _clusters(s["shapes"], resolver, sw, sh),
            "alignment_groups": s.get("alignment_groups") or [],
            "whole_objects": _objects(s["shapes"], sw, sh),
            # NOTE: deliberately NOT listing which of our operators apply.
            # The proposer must judge what SHOULD be degraded, never what is
            # convenient to implement — anchoring it on today's tooling caps
            # the diversity of everything downstream.
            "largest_shapes": [_shape_row(r, sw, sh, resolver) for r in big],
            "n_shapes_not_listed": max(0, len(content) - len(big)),
        }
        conns = _connectors(s["shapes"], sw, sh, id_to_path)
        if conns:
            entry["connectors"] = conns
        ov = _overlaps(content, sw, sh)
        if ov:
            entry["overlaps"] = ov
        if decor:
            # never silently dropped: a diagram's arrows and icons all land here
            entry["decorative"] = {
                "by_kind": Counter(r["kind"] for r in decor).most_common(),
                "sample": [_shape_row(r, sw, sh, resolver) for r in
                           sorted(decor, key=lambda r: -(r["w"] * r["h"]))[:8]],
            }
        if bg:
            entry["background_shapes"] = [_shape_row(r, sw, sh, resolver)
                                          for r in bg[:3]]
        for k in ("hidden", "notes", "background"):
            if meta.get(k):
                entry[k] = meta[k]
        a = anim_by_page.get(page)
        if a:
            for k in ("transition", "motion_paths", "interactive_triggers"):
                if a.get(k):
                    entry[k] = a[k]
        if meta.get("animation"):
            sl_anim = meta["animation"]
            cls = Counter(e.get("cls") for e in sl_anim["effects"] if e.get("cls"))
            tgt = []
            for e in sl_anim["effects"]:
                p = id_to_path.get(int(e["target"])) if (e.get("target") or "").isdigit() else None
                if p and p not in tgt:
                    tgt.append(p)
            entry["animation"] = {"n_effects": sl_anim["n_effects"],
                                  "n_targets": sl_anim["n_targets"],
                                  "classes": cls.most_common(),
                                  "target_paths": tgt[:16]}
        slides.append(entry)

    return {
        "deck": pptx_path, "name": Path(pptx_path).name,
        "slide_size_in": [round(sw / EMU, 1), round(sh / EMU, 1)],
        "is_poster": max(sw, sh) / EMU > 20,
        "n_slides": len(cen["slides"]),
        "deck_summary": {
            "layouts_used": deck_layouts.most_common(),
            "fonts": deck_fonts.most_common(6),
            "font_sizes_pt": deck_sizes.most_common(10),
            "hidden_slides": hidden,
            "slides_with_notes": with_notes,
            "slides_with_animation": with_anim,
            "slides_with_transition": with_trans,
            "transition_kinds": anim["transition_kinds"],
            "slides_with_motion_path": [a["page"] for a in anim["slides"]
                                        if a.get("motion_paths")],
            "slides_with_click_trigger": [a["page"] for a in anim["slides"]
                                          if a.get("interactive_triggers")],
            # shapes no GUI action can recreate — context, never targets
            # what the renderer moves on its own, just by opening and saving.
            # Only text boxes and tables reflow — every other kind measured
            # across ten decks stayed put — so this bounds how small a
            # position-based degradation is allowed to be, and on which shapes.
            "renderer_drift": drift or {},
            "hard_targets": dict(hard),
            # parts that never appear as a shape; empty means genuinely absent,
            # not unexamined
            "package_parts": cen.get("package") or {},
        },
        "slides": slides,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pptx")
    ap.add_argument("--slide", type=int, default=None, help="1-based; omit for all")
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()
    d = digest(args.pptx)
    if args.slide:
        d["slides"] = [s for s in d["slides"] if s["page"] == args.slide]
    text = json.dumps(d, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text)
        print(f"{args.out}  {len(text)//1024}KB")
    else:
        print(text)


if __name__ == "__main__":
    main()
