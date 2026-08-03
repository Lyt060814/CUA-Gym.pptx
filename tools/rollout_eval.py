"""Scoring half of the embedded runtime: per-degradation checks + hard gates.

One check per degradation, each answering "has this particular damage been
undone", and a set of gates that answer "was it undone by doing the work".
The gates are the part that matters at scale: a restoration score alone is
satisfied by pasting a screenshot of the original over the page.
"""

RUNTIME = r'''
IN = 914400.0


def _centre(box):
    return (box[0] + box[2] / 2.0, box[1] + box[3] / 2.0) if box else None


def _slide(inv, page):
    for s in inv["slides"]:
        if s["page"] == page:
            return s
    return {"page": page, "shapes": []}


def _content_key(rec):
    """What identifies a shape across a rebuild: what it shows, not its id."""
    if rec.get("image"):
        return ("img", rec["image"])
    if rec.get("text_sha"):
        return ("txt", rec["text_sha"])
    return ("geo", rec.get("kind"),
            round((rec.get("box") or [0, 0, 0, 0])[2] / IN, 1),
            round((rec.get("box") or [0, 0, 0, 0])[3] / IN, 1))


def _near(box_a, box_b, tol_in):
    ca, cb = _centre(box_a), _centre(box_b)
    if not ca or not cb:
        return False
    return (abs(ca[0] - cb[0]) <= tol_in * IN
            and abs(ca[1] - cb[1]) <= tol_in * IN)


def _size_near(box_a, box_b, frac):
    """Both extents agree. A straight connector legitimately has one extent of
    zero, so a plain relative comparison can never credit one that was
    restored perfectly."""
    if not box_a or not box_b:
        return False
    for a, b in ((box_a[2], box_b[2]), (box_a[3], box_b[3])):
        tol = max(frac * abs(b), 0.02 * IN)
        if abs(a - b) > tol:
            return False
    return True


def _norm_runs(runs):
    out = []
    for para in runs or []:
        for r in para:
            out.append({k: v for k, v in r.items() if k != "t"})
    return out


# --------------------------------------------------------------------------- #
# per-degradation checks
# --------------------------------------------------------------------------- #


def check_restored_shape(cand, gt, spec):
    """A shape that was deleted, moved, resized or rotated is back where the
    ground truth has it, showing what the ground truth shows."""
    page = spec["page"]
    tol = spec.get("tol_in", 0.35)
    gt_rec = _lookup(gt, page, spec["gt_path"])
    if gt_rec is None:
        return 0.0, ["ground-truth shape missing from inventory"]
    want = _content_key(gt_rec)
    hits = [r for r in _slide(cand, page)["shapes"] if _content_key(r) == want]
    if not hits:
        return 0.0, [f"nothing on slide {page} matches the missing "
                     f"{gt_rec.get('kind')} ({want[0]})"]
    best, notes = 0.0, []
    for rec in hits:
        score, why = 0.0, []
        if _near(rec.get("box"), gt_rec.get("box"), tol):
            score += 0.6
        else:
            why.append("position off")
        if _size_near(rec.get("box"), gt_rec.get("box"), spec.get("size_frac", 0.25)):
            score += 0.4
        else:
            why.append("size off")
        if score > best:
            best, notes = score, why
    return best, notes


def check_text_style(cand, gt, spec):
    """Run-level styling on a shape matches the ground truth again."""
    page = spec["page"]
    gt_rec = _lookup(gt, page, spec["gt_path"])
    if gt_rec is None:
        return 0.0, ["ground-truth shape missing"]
    want = _content_key(gt_rec)
    hits = [r for r in _slide(cand, page)["shapes"] if _content_key(r) == want]
    if not hits:
        return 0.0, [f"the styled text is gone from slide {page}"]
    gt_runs = _norm_runs(gt_rec.get("runs"))
    if not gt_runs:
        return 0.0, ["ground truth records no run properties"]
    keys = spec.get("keys") or ["font", "sz", "b", "i", "u", "color"]
    best = 0.0
    for rec in hits:
        got = _norm_runs(rec.get("runs"))
        if not got:
            continue
        n = min(len(got), len(gt_runs))
        agree = 0
        for i in range(n):
            same = all(got[i].get(k) == gt_runs[i].get(k) for k in keys)
            agree += 1 if same else 0
        best = max(best, agree / max(len(gt_runs), 1))
    return best, ([] if best >= 0.999 else ["run styling still differs"])


def check_outline(cand, gt, spec):
    page = spec["page"]
    gt_rec = _lookup(gt, page, spec["gt_path"])
    if gt_rec is None:
        return 0.0, ["ground-truth shape missing"]
    want = _content_key(gt_rec)
    hits = [r for r in _slide(cand, page)["shapes"] if _content_key(r) == want]
    if not hits:
        return 0.0, [f"shape gone from slide {page}"]
    gt_ln = gt_rec.get("line") or {}
    for rec in hits:
        ln = rec.get("line") or {}
        if ln.get("none") == gt_ln.get("none") and ln.get("color") == gt_ln.get("color"):
            return 1.0, []
    return 0.0, ["outline colour or presence still differs"]


def check_effects(cand, gt, spec):
    page = spec["page"]
    gt_rec = _lookup(gt, page, spec["gt_path"])
    if gt_rec is None:
        return 0.0, ["ground-truth shape missing"]
    want = _content_key(gt_rec)
    for rec in _slide(cand, page)["shapes"]:
        if _content_key(rec) != want:
            continue
        if set(rec.get("effects") or []) >= set(gt_rec.get("effects") or []):
            return 1.0, []
    return 0.0, ["the shape effects are still flattened"]


def check_table_cells(cand, gt, spec):
    page = spec["page"]
    gt_rec = _lookup(gt, page, spec["gt_path"])
    gt_tbl = (gt_rec or {}).get("table")
    if not gt_tbl:
        return 0.0, ["ground-truth table missing"]
    tables = [r for r in _slide(cand, page)["shapes"] if r.get("table")]
    if not tables:
        return 0.0, [f"no table left on slide {page}"]
    want_cells = spec.get("cells") or []
    best = 0.0
    for rec in tables:
        got = rec["table"]["cells"]
        hit = 0
        for item in want_cells:
            ri, ci, text = item["at"][0], item["at"][1], (item["was"] or "").strip()
            if ri < len(got) and ci < len(got[ri]):
                if got[ri][ci].strip()[:60] == text[:60]:
                    hit += 1
        best = max(best, hit / max(len(want_cells), 1))
    return best, ([] if best >= 0.999 else ["some cleared cells are still empty "
                                            "or hold the wrong value"])


def check_diagram_nodes(cand, gt, spec):
    """A SmartArt lost some of its nodes; they are back, and it is still a
    SmartArt rather than a picture of one."""
    page = spec["page"]
    want = [t.strip().lower() for t in spec.get("nodes") or []]
    diagrams = [r for r in _slide(cand, page)["shapes"] if r.get("diagram")]
    texts = " | ".join(
        n.lower() for r in diagrams for n in (r["diagram"]["nodes"] or []))
    if not diagrams:
        # allow a hand-built equivalent, but say so
        loose = " | ".join((r.get("text") or "").lower()
                           for r in _slide(cand, page)["shapes"])
        hit = sum(1 for w in want if w and w[:40] in loose)
        return (hit / max(len(want), 1)) * 0.7, [
            "rebuilt without a diagram object — partial credit only"]
    hit = sum(1 for w in want if w and w[:40] in texts)
    return hit / max(len(want), 1), ([] if hit == len(want)
                                     else ["some nodes are still missing"])


def check_chart_series(cand, gt, spec):
    page = spec["page"]
    charts = [r for r in _slide(cand, page)["shapes"] if r.get("chart")]
    if not charts:
        return 0.0, [f"no native chart on slide {page} — a picture of one "
                     f"does not count"]
    want = [(s or "").strip().lower() for s in spec.get("series") or []]
    best = 0.0
    for rec in charts:
        names = [(s.get("name") or "").strip().lower()
                 for s in rec["chart"]["series"]]
        hit = sum(1 for w in want if w in names)
        best = max(best, hit / max(len(want), 1))
    return best, ([] if best >= 0.999 else ["series still missing"])


def _lookup(inv, page, path):
    for rec in _slide(inv, page)["shapes"]:
        if rec["path"] == path:
            return rec
    return None


CHECKERS = {
    "restored_shape": check_restored_shape,
    "text_style": check_text_style,
    "outline": check_outline,
    "effects": check_effects,
    "table_cells": check_table_cells,
    "diagram_nodes": check_diagram_nodes,
    "chart_series": check_chart_series,
}


# --------------------------------------------------------------------------- #
# hard gates
# --------------------------------------------------------------------------- #


def gate_untouched_slides(cand, gt, init, plan):
    """Slides the task never damaged must come back unchanged.

    Catches the blunt cheat of rebuilding the deck, and the subtler one of
    'tidying' pages nobody asked about.
    """
    touched = set(plan["affected_pages"])
    bad = []
    for s in gt["slides"]:
        if s["page"] in touched:
            continue
        want = sorted(str(_content_key(r)) for r in s["shapes"])
        got = sorted(str(_content_key(r))
                     for r in _slide(cand, s["page"])["shapes"])
        if want != got:
            bad.append(s["page"])
    return (not bad), {"changed_untouched_slides": bad[:8]}


def gate_no_paste_over(cand, gt, init, plan):
    """No new full-bleed picture on a damaged slide.

    Dropping a screenshot of the original page on top restores every pixel and
    none of the structure; it is the single most likely way to farm this task.
    """
    bad = []
    for page in plan["affected_pages"]:
        gt_imgs = {r.get("image") for r in _slide(gt, page)["shapes"]
                   if r.get("image")}
        page_area = plan["slide_w"] * plan["slide_h"]
        for rec in _slide(cand, page)["shapes"]:
            if rec.get("kind") != "picture" or not rec.get("box"):
                continue
            area = rec["box"][2] * rec["box"][3]
            if area >= 0.55 * page_area and rec.get("image") not in gt_imgs:
                bad.append({"page": page, "area_frac": round(area / page_area, 2)})
    return (not bad), {"pasted_full_page_images": bad[:6]}


def gate_no_stray_additions(cand, gt, init, plan):
    """A damaged slide must not end up with materially more shapes than the
    original.  Cloning a neighbour and leaving both scores well on geometry
    while looking nothing like the answer."""
    bad = []
    for page in plan["affected_pages"]:
        want = len(_slide(gt, page)["shapes"])
        got = len(_slide(cand, page)["shapes"])
        if got > want + max(2, int(0.25 * want)):
            bad.append({"page": page, "gt": want, "candidate": got})
    return (not bad), {"slides_with_stray_shapes": bad[:6]}


def gate_deck_shape(cand, gt, init, plan):
    """Same number of slides, in the same order."""
    ok = cand["n_slides"] == gt["n_slides"]
    return ok, {"n_slides": cand["n_slides"], "expected": gt["n_slides"]}


def gate_native_objects(cand, gt, init, plan):
    """Where the ground truth has a chart, table or SmartArt on a damaged
    slide, the candidate must have one too — not an image of one."""
    bad = []
    for page in plan["affected_pages"]:
        for kind in ("chart", "table", "smartart"):
            want = sum(1 for r in _slide(gt, page)["shapes"] if r["kind"] == kind)
            got = sum(1 for r in _slide(cand, page)["shapes"] if r["kind"] == kind)
            if want and got < want:
                bad.append({"page": page, "kind": kind, "gt": want, "candidate": got})
    return (not bad), {"missing_native_objects": bad[:6]}


GATES = {
    "untouched_slides_intact": gate_untouched_slides,
    "no_full_page_paste": gate_no_paste_over,
    "no_stray_additions": gate_no_stray_additions,
    "deck_shape_preserved": gate_deck_shape,
    "native_objects_present": gate_native_objects,
}


def evaluate_candidate(cand, gt, init, plan):
    """Score = mean component progress, but only once every gate holds."""
    gates, gate_detail = {}, {}
    for name, fn in GATES.items():
        ok, detail = fn(cand, gt, init, plan)
        gates[name] = bool(ok)
        if detail:
            gate_detail[name] = detail

    components, violations = [], []
    for spec in plan["components"]:
        fn = CHECKERS.get(spec["check"])
        if fn is None:
            components.append({"id": spec["id"], "check": spec["check"],
                               "progress": 0.0, "notes": ["unknown check"]})
            continue
        try:
            raw, notes = fn(cand, gt, spec)
        except Exception as error:                                # noqa: BLE001
            raw, notes = 0.0, [f"{type(error).__name__}: {error}"]
        # Floor normalisation. A shape that was moved is still on the slide, so
        # a size check credits the untouched degraded file for work nobody did.
        # `floor` is what this component scores on the broken file; subtracting
        # it is what makes doing nothing worth zero.
        floor = float(spec.get("floor", 0.0))
        progress = 0.0 if raw <= floor else (raw - floor) / max(1e-9, 1.0 - floor)
        progress = max(0.0, min(1.0, progress))
        components.append({"id": spec["id"], "check": spec["check"],
                           "page": spec.get("page"),
                           "progress": round(float(progress), 4),
                           "raw": round(float(raw), 4), "floor": round(floor, 4),
                           "notes": notes})
        if progress < 0.999:
            violations.append(f"{spec['id']}: " + ("; ".join(notes) or "incomplete"))

    mean = (sum(c["progress"] for c in components) / len(components)
            if components else 0.0)
    lowest = min((c["progress"] for c in components), default=0.0)
    passed = all(gates.values())
    return {
        "evaluator": plan["evaluator"],
        "score": round(mean, 4) if passed else 0.0,
        "mean_component_progress": round(mean, 4),
        "minimum_component_progress": round(lowest, 4),
        "hard_gates": gates,
        "hard_gate_detail": gate_detail,
        "components": components,
        "violations": {"components": violations[:12]} if violations else {},
    }
'''
