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

Size is a running cost, not a one-off one: the proposer re-reads this on every
turn, so bytes here are multiplied by the length of its run.  Two rules follow.
*Aggregate rather than enumerate* whenever a list is long and repetitive — the
twenty-eighth identical tick mark says nothing the first three did not, so it
collapses into a counted cluster that still lists its path.  And *bound the
worst case*: `SIZE_CEILING` walks the per-slide shape cap down until the whole
deck fits, so a 743-shape deck costs a little more than a 90-shape one instead
of eight times as much.  Neither rule is allowed to drop a shape silently —
whatever falls off the end is still counted, by kind.
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

# A deck-level byte budget for the compact digest.  See `digest()`: the
# per-slide shape cap is walked down until the whole thing fits, so the worst
# case is bounded instead of growing linearly with a 743-shape deck.
SIZE_CEILING = 130_000
SHAPE_CAPS = (40, 30, 24, 18, 14, 10)


def _in(v):
    return round(v / EMU, 2)


def _crop(spec):
    """Only the part of a picture fill an agent could act on.

    The census records the whole `blipFill`.  Two of its keys are on every
    picture that was never cropped at all — `fill_mode:"stretch"` is the
    default and `cstate` is a print/screen compression hint — and together
    they were about two thirds of all crop bytes across the first ten decks
    while naming nothing reachable from a GUI: a `crop` op only ever *resets*
    a crop that exists.  `srcRect` itself is kept, but the XML gives it to
    three decimals (a thousandth of a percent of an image edge) and the
    proposal skill already rules out grading a crop boundary the eye cannot
    reach, so it rounds to one.
    """
    if not spec:
        return None
    out = {}
    sr = spec.get("srcRect")
    if sr:
        out["srcRect"] = {k: round(v, 1) for k, v in sr.items()}
    if spec.get("recolor"):
        out["recolor"] = spec["recolor"]
    if spec.get("fill_mode") == "tile":       # stretch is the default; tile is not
        out["fill_mode"] = "tile"
    return out or None


def _equation(eq):
    if not eq:
        return None
    constructs = Counter(x.split(":", 1)[0]
                         for x in eq.get("structure") or []
                         if x.split(":", 1)[0] not in ("oMath", "t"))
    return {"native": True, "text": (eq.get("text") or "")[:160],
            "constructs": constructs.most_common()}


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


def _clusters(shapes, resolver, slide_w, slide_h, member_cap=12):
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
            arrange = "single column"
        elif spread_y < 0.06:
            arrange = "single row"
        else:
            arrange = "scattered (grid or free)"
        c = {"n": len(members), "kind": sig[0],
             "size_in": [sig[2], sig[3]], "arrangement": arrange}
        # empty/None reads the same as absent here, and these were four keys
        # on every cluster of unfilled, unstroked, uneffected shapes
        for key, val in (("geom", sig[1]), ("fill", sig[4]),
                         ("fill_rgb", sig[5]), ("line_rgb", sig[6]),
                         ("effects", list(sig[8]))):
            if val:
                c[key] = val
        # One text for the whole cluster when every member says the same thing
        # — twenty-eight identical axis labels wrote their label out
        # twenty-eight times.
        texts = {(m.get("text") or "")[:44] for m in members}
        shared = texts.pop() if len(texts) == 1 else None
        if shared:
            c["text"] = shared
        c["members"] = [
            {"path": m["path"],
             "at": [round(m["cx"] / slide_w, 2), round(m["cy"] / slide_h, 2)],
             **({"text": m["text"][:44]}
                if m.get("text") and shared is None else {})}
            for m in members[:member_cap]]
        if len(members) > member_cap:
            # the tail is still addressable — only its coordinates are dropped,
            # and `arrangement` plus the render already carry those
            c["more_paths"] = [m["path"] for m in members[member_cap:]]
        out.append(c)
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
    out = {}
    # an empty counter is an empty list on every slide that has no fills, no
    # strokes and no effects — five keys saying nothing
    for key, ctr, n in (("fill_colors", fills, 6), ("line_colors", lines, 4),
                        ("line_widths_pt", widths, 4),
                        ("geometries", radii, 6), ("effects", effects, 5)):
        if ctr:
            out[key] = ctr.most_common(n)
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
        cr = _crop(r.get("crop"))
        if cr:
            item["crop"] = cr
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
        if r.get("equation"):
            item["equation"] = _equation(r["equation"])
        out.append(item)
    return out


def _connectors(shapes, slide_w, slide_h, id_to_path):
    """Connector topology — the thing a render shows but a render cannot label.

    Returns `(rows, clusters)`.  Topology is never aggregated: every connector
    that is glued to something keeps its own row, because "who connects to
    whom" is the whole point of this block.  What *is* aggregated is the
    opposite case — a *floating* line has no topology to lose, and decks built
    out of drawn rules are full of them (one deck had 301 connectors and not
    one of them was attached: they were tick marks on chart axes).  Identical
    floating lines therefore collapse into a counted cluster that still lists
    every path, the same treatment `decorative` already gets.
    """
    rows, free = [], defaultdict(list)
    for r in shapes:
        if r["kind"] != "connector":
            continue
        item = {"path": r["path"],
                "at": [round(r["cx"] / slide_w, 2), round(r["cy"] / slide_h, 2)],
                "len_in": round(math.hypot(r["w"], r["h"]) / EMU, 2)}
        attached = None
        if r.get("st_cxn") is not None or r.get("end_cxn") is not None:
            attached = [id_to_path.get(r.get("st_cxn")),
                        id_to_path.get(r.get("end_cxn"))]
        item["attached"] = attached          # None ⇒ floating, not glued
        st = r.get("style") or {}
        ln = st.get("line") or {}
        if ln.get("w"):
            item["w_pt"] = round(ln["w"] / 12700, 1)
        if st.get("prstGeom"):
            item["geom"] = st["prstGeom"]
        if attached is None:
            free[(item.get("geom"), item.get("w_pt"),
                  round(item["len_in"], 1))].append(item)
        else:
            rows.append(item)

    clusters = []
    for (geom, w_pt, length), members in free.items():
        if len(members) < 3:                 # too few to be worth summarising
            rows.extend(members)
            continue
        c = {"n": len(members), "attached": None, "len_in": length,
             "paths": [m["path"] for m in members]}
        if geom:
            c["geom"] = geom
        if w_pt:
            c["w_pt"] = w_pt
        clusters.append(c)
    rows.sort(key=lambda i: (i["at"][1], i["at"][0]))
    return rows, sorted(clusters, key=lambda c: -c["n"])


def _align(groups, cap=10):
    """Alignment runs, deduplicated and trimmed.

    Six axes are scanned per slide, so a tidy grid reports the same set of
    shapes once as `top` and again as `cy`; and `equal_spacing` was false on
    three quarters of all groups, where it is the default reading anyway.  The
    long tail is trimmed to the biggest `cap` runs with a count of what was
    dropped, because the twentieth alignment run on a slide tells a proposer
    nothing the first ten did not.
    """
    seen, out = set(), []
    for g in sorted(groups, key=lambda g: -len(g.get("paths") or [])):
        key = tuple(g.get("paths") or [])
        if key in seen:
            continue
        seen.add(key)
        item = {"axis": g["axis"], "paths": list(key)}
        if g.get("equal_spacing"):
            item["equal_spacing"] = True
        out.append(item)
    if len(out) > cap:
        return out[:cap] + [{"n_more_groups": len(out) - cap}]
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
           "z": r["z"]}
    # `geom` and `text` used to be emitted unconditionally: across ten decks
    # that was 381 `"geom":null` and 440 `"text":""`.  Every other optional key
    # on this row already means "absent when missing", so they now do too.
    geom = (r.get("style") or {}).get("prstGeom")
    if geom:
        row["geom"] = geom
    if r.get("text"):
        row["text"] = r["text"][:60]
    if r.get("rot"):
        row["rot"] = r["rot"]
    if r.get("group_path"):
        row["in_group"] = r["group_path"]
    if r.get("placeholder"):
        row["ph"] = r["placeholder"]["type"]
    ts = tsmod.summarize(r.get("text_style"), resolver)
    if ts:
        row["type_style"] = ts
    cr = _crop(r.get("crop"))
    if cr:
        row["crop"] = cr
    if r.get("link"):
        row["link"] = r["link"]
    if r.get("image_sha"):
        row["image"] = r["image_sha"][:8]
    if r.get("hard_target"):
        row["hard_target"] = r["hard_target"]
    if r.get("equation"):
        row["equation"] = _equation(r["equation"])
    return row


# ---------------------------------------------------------------------------
# Renderer drift — two renderers, two entirely different jobs.
#
#   **WPS** is the application these tasks are solved and graded in.  It is
#   therefore the *only* measurement that bounds how far a degradation may move
#   something: drift the grader's own application produces is indistinguishable
#   from drift the agent produced.  Measured across the first ten decks it
#   changed 0.0% of shapes on every one of them.
#
#   **LibreOffice** is the proxy we can drive headlessly at ingest time.  Its
#   number is NOT a tolerance for WPS — it ran 7.6%–61.5% on those same ten
#   decks, essentially all of it textbox and table reflow that WPS does not
#   reproduce.  It is kept for a different job: a deck the proxy mangles is
#   often built on fragile constructs, which is a corpus-quality signal worth
#   having before six more stages are spent on it.
#
# WPS has no headless converter on Linux — measuring it drives a GUI at 60–90 s
# a deck — so it is never run from `inspect`.  It is measured out of band
# (`python3 -m pptxgym.office.wps_roundtrip <deck>/source.pptx`) and read here from
# `roundtrip-wps.json` if that file exists.  When it does not, the digest says
# so explicitly: the LibreOffice figure must never silently stand in for it.

WPS_ROLE = ("the application the task is solved and graded in — this is the "
            "measurement that bounds position-based work")
LO_ROLE = ("headless proxy, run at ingest as a corpus-fragility signal only — "
           "NOT a tolerance for WPS, and not a reason to avoid position work")


def _drift_side(rep: dict, renderer: str, role: str) -> dict:
    if not rep or rep.get("verdict") in (None, "unmeasured"):
        out = {"renderer": renderer, "role": role, "measured": False}
        if rep and rep.get("error"):
            out["error"] = rep["error"]
        return out
    by_kind = rep.get("by_kind") or {}
    return {
        "renderer": renderer, "role": role, "measured": True,
        "verdict": rep.get("verdict"),
        "changed_frac": rep.get("changed_frac"),
        "drift_in": rep.get("drift") or {},
        "kinds_that_move": sorted(set(by_kind.get("moved", {}))
                                  | set(by_kind.get("resized", {}))),
    }


def renderer_drift(lo: dict | None = None, wps: dict | None = None) -> dict:
    """Both round trips, labelled by renderer, with which one governs."""
    w = _drift_side(wps, "wps", WPS_ROLE)
    proxy = _drift_side(lo, "libreoffice", LO_ROLE)
    if w["measured"]:
        governs = "wps"
        if w["changed_frac"]:
            reading = (
                f"WPS moved/changed {w['changed_frac']:.1%} of the shapes on "
                f"this deck ({', '.join(w['kinds_that_move']) or 'no kind'} "
                f"drifts). Position-based degradations must stay well above "
                f"that amplitude, and should avoid those kinds as scored "
                f"targets.")
        else:
            reading = ("WPS changed nothing on this deck: open-and-save is "
                       "lossless here, so position-based degradations carry no "
                       "renderer noise and need no special amplitude margin.")
    else:
        governs = None
        reading = ("This deck has NOT been round-tripped through WPS, so the "
                   "grading application's drift is unknown. The LibreOffice "
                   "figure below is a proxy and does NOT substitute for it: "
                   "treat position-based degradations as unverified — keep "
                   "displacements large and obvious, and prefer pictures, "
                   "cards and diagrams over text boxes and tables as scored "
                   "position targets, until WPS is measured "
                   "(`python3 -m pptxgym.office.wps_roundtrip <deck>/source.pptx`).")
    return {"governs": governs, "reading": reading, "wps": w,
            "libreoffice": proxy}


def _build(pptx_path, cen, resolver, anim, drift, max_shapes_listed):
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
            # `n_content_shapes` used to sit here as well; it was
            # `shape_census.content` under a second name.
            "shape_census": {"content": len(content), "decorative": len(decor),
                             "background": len(bg),
                             "groups": sum(1 for r in s["shapes"]
                                           if r["kind"] == "group")},
            "visual_system": vs,
            "repetition": _clusters(s["shapes"], resolver, sw, sh),
            "alignment_groups": _align(s.get("alignment_groups") or []),
            "whole_objects": _objects(s["shapes"], sw, sh),
            # NOTE: deliberately NOT listing which of our operators apply.
            # The proposer must judge what SHOULD be degraded, never what is
            # convenient to implement — anchoring it on today's tooling caps
            # the diversity of everything downstream.
            "largest_shapes": [_shape_row(r, sw, sh, resolver) for r in big],
        }
        listed = {id(r) for r in big}
        rest = [r for r in content if id(r) not in listed]
        if rest:
            # nothing is silently dropped: the shapes the cap left out are
            # still counted, by kind, so "not mentioned" cannot read as
            # "not there"
            entry["n_shapes_not_listed"] = len(rest)
            entry["not_listed_by_kind"] = Counter(
                r["kind"] for r in rest).most_common()
        conns, cclusters = _connectors(s["shapes"], sw, sh, id_to_path)
        if conns:
            entry["connectors"] = conns
        if cclusters:
            entry["connector_clusters"] = cclusters
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
        equations = [r for r in real if r.get("equation")]
        if equations:
            # Formulae are often small and have no a:t text, so semantic
            # classification puts them among decorative shapes and the normal
            # shape cap can hide every path. Focused runs need an exhaustive,
            # compact address list rather than a larger general shape dump.
            entry["equations"] = [
                {"path": r["path"], "at": [round(r["cx"] / sw, 2),
                                             round(r["cy"] / sh, 2)],
                 "size_in": [round(r["w"] / EMU, 2),
                             round(r["h"] / EMU, 2)],
                 **_equation(r["equation"])}
                for r in equations]
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
            "native_equations": sum(1 for slide in cen["slides"]
                                    for shape in slide["shapes"]
                                    if shape.get("equation")),
            # What each renderer changes on its own, just by opening and
            # saving.  Both are reported and both are labelled, because they
            # answer different questions: WPS (`governs`) bounds position work
            # because it is what the task is graded in; LibreOffice is only a
            # corpus-fragility signal.  Absent a WPS measurement this says so
            # rather than passing the proxy off as authoritative.
            "renderer_drift": drift if drift is not None else renderer_drift(),
            # shapes no GUI action can recreate — context, never targets
            "hard_targets": dict(hard),
            # parts that never appear as a shape; empty means genuinely absent,
            # not unexamined
            "package_parts": cen.get("package") or {},
            "shapes_listed_per_slide": max_shapes_listed,
        },
        "slides": slides,
    }


def _size(d: dict) -> int:
    """Bytes of the compact form — what an agent actually pays to read."""
    return len(json.dumps(d, ensure_ascii=False,
                          separators=(",", ":")).encode())


def digest(pptx_path: str, max_shapes_listed=40, drift: dict | None = None,
           ceiling: int = SIZE_CEILING) -> dict:
    """Structural digest, with a bounded worst case.

    The per-slide cap alone does not bound the deck: fifteen slides of fifty
    shapes each produced a 215 KB digest (~80k tokens) that the proposing
    agent re-read on every turn.  So the cap is walked down until the compact
    form fits under `ceiling`, and the cap that was used is recorded in
    `deck_summary.shapes_listed_per_slide` while every slide that lost rows
    keeps `n_shapes_not_listed` and `not_listed_by_kind`.  Degrading means
    listing fewer of the *smallest* shapes on the *fullest* slides — the ones
    a render shows best and a proposer targets least.
    """
    cen = census.census_deck(pptx_path)
    resolver = styles.ThemeResolver(pptx_path)
    try:
        anim = anim_steps.deck_animation(pptx_path)
    except Exception:                                        # noqa: BLE001
        anim = {"slides": [], "transition_kinds": [], "n_animated": 0}

    caps = [c for c in SHAPE_CAPS if c < max_shapes_listed]
    out = _build(pptx_path, cen, resolver, anim, drift, max_shapes_listed)
    for cap in caps:
        if _size(out) <= ceiling:
            break
        out = _build(pptx_path, cen, resolver, anim, drift, cap)
    return out


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
