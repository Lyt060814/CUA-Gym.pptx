"""Produce the material a task promises its solver.

A proposal declares what the agent gets besides the broken file — a render of
the original page, a masked render, the picture it has to re-insert, the
numbers behind a deleted chart.  Until those files exist the task cannot be
attempted: the instruction says "the reference image shows how the page should
look" and there is no image.

Almost all of it is derivable rather than judged, which was not obvious at
first.  The interesting case is the masked render: "cover the damaged region,
leave the surrounding context" sounds like a judgement call, but `delta.json`
already records the original bounding box of everything that was broken, so
the mask is exactly the union of those boxes.  The rest follows the same
pattern — the deleted picture's bytes are still in the source package, the
deleted chart's numbers are still in its cache.

Everything is derived from `source.pptx`, never from `input.pptx`: the source
is the ground truth, and the whole point of an asset is to carry a piece of it
across to the solver.
"""

from __future__ import annotations

import csv
import json
import re
import tempfile
import zipfile
from pathlib import Path

EMU = 914400
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


class AssetError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def render_page(pptx: Path, page: int, out: Path, dpi: int = 130) -> Path:
    from pptx import Presentation
    from . import render

    prs = Presentation(str(pptx))
    xs = prs.slides._sldIdLst
    for i, s in enumerate(list(xs)):
        if i != page - 1:
            prs.part.drop_rel(s.rId)
            xs.remove(s)
    with tempfile.TemporaryDirectory() as td:
        one = Path(td) / "one.pptx"
        prs.save(str(one))
        pngs = render.render_pptx(str(one), td, "x", dpi=dpi)
        if not pngs:
            raise AssetError(f"could not render page {page} of {pptx.name}")
        out.parent.mkdir(parents=True, exist_ok=True)
        Path(pngs[0]).replace(out)
    return out


def _boxes_for(delta: dict, page: int) -> list[tuple[int, int, int, int]]:
    """Original bounding boxes of everything broken on a page."""
    out = []
    for entry in (delta.get("slides") or {}).get(str(page - 1), []):
        box = entry.get("box")
        if box and len(box) == 4 and box[2] > 0 and box[3] > 0:
            out.append(tuple(box))
    return out


def mask_regions(png: Path, boxes, slide_w: int, slide_h: int, out: Path,
                 pad_in: float = 0.06) -> dict:
    """Cover the damaged regions, keep everything around them.

    The point of a masked render is that the agent gets *environmental
    evidence* rather than the answer: alignment, spacing and the surviving
    neighbours stay visible, the thing it has to rebuild does not.
    """
    from PIL import Image, ImageDraw

    im = Image.open(png).convert("RGB")
    W, H = im.size
    dr = ImageDraw.Draw(im)
    sx, sy = W / slide_w, H / slide_h
    pad = pad_in * EMU
    covered = []
    for x, y, cx, cy in boxes:
        r = [max(0, (x - pad) * sx), max(0, (y - pad) * sy),
             min(W, (x + cx + pad) * sx), min(H, (y + cy + pad) * sy)]
        w, h = int(r[2] - r[0]), int(r[3] - r[1])
        if w < 2 or h < 2:
            continue
        # hatch on its own tile and paste it: drawing the diagonals straight
        # onto the page lets them run past the rectangle and streak the parts
        # that are supposed to stay readable
        tile = Image.new("RGB", (w, h), (232, 234, 236))
        td = ImageDraw.Draw(tile)
        for k in range(-h, w, 18):
            td.line([k, h, k + h, 0], fill=(208, 212, 216), width=2)
        td.rectangle([0, 0, w - 1, h - 1], outline=(150, 156, 162), width=2)
        im.paste(tile, (int(r[0]), int(r[1])))
        covered.append([round(v) for v in r])
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    return {"masked_px": covered}


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #


def extract_deleted_images(source: Path, delta: dict, out_dir: Path) -> list[dict]:
    """Pull the bytes of every picture the recipe removed out of the source.

    A task that says "put the logo back" is unanswerable unless the logo is
    handed over; it cannot be drawn and it is no longer in the file.
    """
    wanted = {}
    for page_key, entries in (delta.get("slides") or {}).items():
        slide_no = int(page_key) + 1
        for e in entries:
            if e.get("op") not in ("delete", "blank_slide"):
                continue
            xml = e.get("removed_xml") or ""
            for rid in re.findall(r'r:embed="([^"]+)"', xml):
                wanted.setdefault((slide_no, rid), e)

    made = []
    if not wanted:
        return made
    out_dir.mkdir(parents=True, exist_ok=True)
    from lxml import etree
    with zipfile.ZipFile(source) as z:
        for (slide_no, rid), entry in sorted(wanted.items()):
            rels = f"ppt/slides/_rels/slide{slide_no}.xml.rels"
            try:
                root = etree.fromstring(z.read(rels))
            except KeyError:
                continue
            target = None
            for rel in root.findall(f"{{{PKG_REL}}}Relationship"):
                if rel.get("Id") == rid:
                    target = rel.get("Target", "").replace("../", "ppt/")
            if not target or target not in z.namelist():
                continue
            ext = target.rsplit(".", 1)[-1]
            name = f"p{slide_no:02d}-{entry.get('name') or 'picture'}"
            name = re.sub(r"[^A-Za-z0-9._-]+", "-", name)[:48] + f".{ext}"
            (out_dir / name).write_bytes(z.read(target))
            made.append({"file": name, "slide": slide_no,
                         "from": target, "shape": entry.get("name")})
    return made


def chart_and_table_data(source: Path, pages: list[int], out_dir: Path) -> list[dict]:
    """CSV for every chart and table on the given pages, read from the source.

    A rebuild task has to state its numbers somewhere.  The chart's own cache
    is the honest place to take them from — it is what the original actually
    rendered.
    """
    from pptx import Presentation
    from . import census

    made = []
    prs = Presentation(str(source))
    for page in pages:
        if not 1 <= page <= len(prs.slides):
            continue
        slide = prs.slides[page - 1]
        recs = census.SlideCensus(prs, slide, page - 1).walk()
        for rec in recs:
            if rec.chart:
                rows = []
                cats = (rec.chart["series"][0].get("cats") or []) if rec.chart["series"] else []
                header = [""] + [s.get("name") or f"series{i}"
                                 for i, s in enumerate(rec.chart["series"])]
                rows.append(header)
                for i, c in enumerate(cats):
                    rows.append([c] + [(s.get("vals") or [None] * len(cats))[i]
                                       if i < len(s.get("vals") or []) else None
                                       for s in rec.chart["series"]])
                name = f"p{page:02d}-chart-{rec.chart.get('plot') or 'data'}.csv"
                out_dir.mkdir(parents=True, exist_ok=True)
                with open(out_dir / name, "w", newline="") as f:
                    csv.writer(f).writerows(rows)
                made.append({"file": name, "slide": page, "kind": "chart",
                             "title": rec.chart.get("title"),
                             "series": rec.chart.get("n_series"),
                             "rows": len(rows) - 1})
            elif rec.table:
                name = f"p{page:02d}-table.csv"
                out_dir.mkdir(parents=True, exist_ok=True)
                with open(out_dir / name, "w", newline="") as f:
                    csv.writer(f).writerows(r["cells"] for r in rec.table["rows"])
                made.append({"file": name, "slide": page, "kind": "table",
                             "size": [rec.table["n_rows"], rec.table["n_cols"]]})
    return made


def keyframes(source: Path, page: int, out_dir: Path) -> dict:
    from . import anim_steps
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = anim_steps.render_keyframes(str(source), page, str(out_dir))
    return {"slide": page, "steps": meta.get("n_steps", 0),
            "frames": [Path(f).name for f in meta.get("frames", [])]}


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


def materialise(deck) -> dict:
    """Produce every asset the proposal declares. Deterministic throughout."""
    from pptx import Presentation

    proposal = json.loads(deck.proposal.read_text())
    tasks = proposal.get("tasks") or []
    if not tasks:
        return {"assets": [], "note": "deck yields no task"}
    task = tasks[0]
    delta = json.loads(deck.delta.read_text())
    out_dir = deck.root / "assets"
    out_dir.mkdir(exist_ok=True)

    prs = Presentation(str(deck.source))
    sw, sh = prs.slide_width, prs.slide_height
    task_pages = sorted({p for g in task["degradations"] for p in g.get("slides", [])})

    produced, unmet = [], []
    for a in task.get("assets", []):
        kind = a.get("kind")
        pages = a.get("slides") or []
        try:
            if kind in ("reference_image", "reference_image_masked"):
                masked = bool(a.get("masked")) or kind.endswith("_masked")
                if not pages:
                    raise AssetError("reference_image without `slides`")
                for page in pages:
                    raw = out_dir / f"reference-p{page:02d}.png"
                    render_page(deck.source, page, raw)
                    item = {"kind": "reference_image", "slide": page,
                            "file": raw.name, "masked": masked}
                    if masked:
                        boxes = _boxes_for(delta, page)
                        if not boxes:
                            raise AssetError(
                                f"masked render asked for slide {page}, but the "
                                f"delta records no bounding box there — nothing "
                                f"to mask, so the render would be the answer")
                        m = out_dir / f"reference-p{page:02d}-masked.png"
                        info = mask_regions(raw, boxes, sw, sh, m)
                        raw.unlink()          # never ship the unmasked one
                        item.update(file=m.name, regions=len(boxes), **info)
                    produced.append(item)

            elif kind in ("image", "picture", "asset_image"):
                made = extract_deleted_images(deck.source, delta, out_dir)
                if not made:
                    raise AssetError("no deleted picture found in the delta to "
                                     "hand over")
                produced += [{"kind": "image", **m} for m in made]

            elif kind in ("data", "csv"):
                made = chart_and_table_data(deck.source, pages or task_pages,
                                            out_dir)
                if not made:
                    raise AssetError(
                        f"no chart or table found on slides "
                        f"{pages or task_pages} to take numbers from")
                produced += [{"kind": "data", **m} for m in made]

            elif kind in ("reference_keyframes", "keyframes"):
                if not pages:
                    raise AssetError("keyframes without `slides`")
                for page in pages:
                    produced.append({"kind": "reference_keyframes",
                                     **keyframes(deck.source,
                                                 page, out_dir / f"build-p{page:02d}")})

            else:
                raise AssetError(f"no producer for asset kind {kind!r}")

        except AssetError as e:
            unmet.append({"kind": kind, "slides": pages, "why": str(e)})

    manifest = {"task": task["name"], "produced": produced, "unmet": unmet}
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1))
    return manifest
