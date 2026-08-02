"""Inspection helpers an agent needs while writing a recipe.

Deliberately terse output: an agent pays for every line it reads, so these
print the few facts that decide a path — where a shape is, how big, what it
says, which image it carries — and nothing else.

    python -m pptxgym.tools shapes   work/deck0001 7 12 19
    python -m pptxgym.tools smartart work/deck0001 --slide 19
    python -m pptxgym.tools chart    work/deck0001 --slide 5
    python -m pptxgym.tools pair     work/deck0001 7 12
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import zipfile
from pathlib import Path


def _digest(deck_dir: Path) -> dict:
    f = deck_dir / "digest.json"
    if not f.exists():
        raise SystemExit(f"{f} not found — run `pptxgym inspect` first")
    return json.loads(f.read_text())


def cmd_shapes(args):
    deck = Path(args.deck)
    dig = _digest(deck)
    pages = set(args.pages or [])
    for s in dig["slides"]:
        if pages and s["page"] not in pages:
            continue
        print(f"\n=== p{s['page']}  {s['layout']}  {s['shape_census']}")
        rows = list(s["largest_shapes"])
        for d in (s.get("decorative") or {}).get("sample", []):
            d = dict(d)
            d["_d"] = 1
            rows.append(d)
        for r in sorted(rows, key=lambda r: (r["at"][1], r["at"][0])):
            x, y = r["at"]
            w, h = r["size_in"]
            ht = r.get("hard_target") or {}
            flag = (f" [OLE {ht['ole'].get('progId')}]" if ht.get("ole")
                    else " [custGeom]" if ht.get("custom_geometry") else "")
            ts = r.get("type_style") or {}
            sty = ""
            if ts:
                sty = (f" {(ts.get('fonts') or [''])[0][:12]}"
                       f"/{(ts.get('sizes_pt') or [''])[0]}")
                if ts.get("colors"):
                    sty += f"/{ts['colors'][0]}"
            img = f" img:{r['image']}" if r.get("image") else ""
            mark = "·" if r.get("_d") else " "
            txt = (r.get("text") or "").replace("\n", " ")[:44]
            print(f" {mark}{r['path']:<7}{r['kind']:<12} @({x:.2f},{y:.2f}) "
                  f"{w:>5.1f}x{h:<5.1f} z{r.get('z','?'):<4}{sty}{img}{flag}  {txt}")
        for o in s.get("whole_objects", []):
            bits = []
            if o.get("plot"):
                bits.append(f"chart:{o['plot']} series={o.get('n_series')}")
            if o.get("cells"):
                bits.append(f"table {o['n_rows']}x{o['n_cols']}")
            if o.get("nodes"):
                bits.append(f"smartart:{o.get('diagram_layout')} n={o.get('n_nodes')}")
            if o.get("n_children"):
                bits.append("group n=%d kids=%s" % (
                    o["n_children"],
                    ",".join(c["path"] for c in o.get("children", [])[:10])))
            if bits:
                print(f"  >> {o['path']:<7}{o['kind']:<12} {'; '.join(bits)}")
        for c in s.get("connectors", [])[:6]:
            print(f"  ~~ {c['path']:<7}connector    len={c['len_in']} "
                  f"attached={c['attached']}")
        for rep in (s.get("repetition") or [])[:4]:
            print(f"  ## x{rep['n']} {rep['kind']} {rep['size_in']} "
                  f"{rep['arrangement']} paths="
                  + ",".join(m["path"] for m in rep["members"][:10]))


def cmd_smartart(args):
    from . import smartart
    src = Path(args.deck) / "source.pptx"
    with zipfile.ZipFile(src) as z:
        dp, dr = smartart.diagram_parts_for(z, args.slide, args.graphic)
        for n in smartart.read_nodes(z.read(dp)):
            print(f"  {n['modelId']}  {n['text'][:72]}")
        print(f"  -- data={dp} drawing={dr}")


def cmd_chart(args):
    from . import charts
    src = Path(args.deck) / "source.pptx"
    with zipfile.ZipFile(src) as z:
        part = charts.chart_parts_for(z, args.slide, args.chart)
        for s in charts.series_of(z.read(part)):
            print(f"  [{s['index']}] {s['name']!r}  {s['n_points']} pts  "
                  f"{s['sample']}")
        print("  --", part)


def cmd_pair(args):
    """Render the same page from source and from input, side by side on disk."""
    from . import render
    from pptx import Presentation

    deck = Path(args.deck)
    out = deck / "compare"
    out.mkdir(exist_ok=True)
    made = []
    for label, src in (("gt", deck / "source.pptx"),
                       ("in", deck / "input.pptx")):
        if not src.exists():
            print(f"  (no {src.name} yet)")
            continue
        for page in args.pages:
            prs = Presentation(str(src))
            xs = prs.slides._sldIdLst
            for i, s in enumerate(list(xs)):
                if i != page - 1:
                    prs.part.drop_rel(s.rId)
                    xs.remove(s)
            with tempfile.TemporaryDirectory() as td:
                one = Path(td) / "one.pptx"
                prs.save(str(one))
                pngs = render.render_pptx(str(one), td, "x", dpi=args.dpi)
                if not pngs:
                    continue
                dest = out / f"{label}-{page:02d}.png"
                Path(pngs[0]).replace(dest)
                made.append(dest)
    for m in made:
        print(" ", m)
    print("open the gt/in pair for each page and check the damage matches "
          "what the proposal describes")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pptxgym.tools", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("shapes", help="compact shape table for picking paths")
    p.add_argument("deck")
    p.add_argument("pages", nargs="*", type=int)
    p.set_defaults(func=cmd_shapes)

    p = sub.add_parser("smartart", help="nodes inside a SmartArt")
    p.add_argument("deck")
    p.add_argument("--slide", type=int, required=True)
    p.add_argument("--graphic", type=int, default=0)
    p.set_defaults(func=cmd_smartart)

    p = sub.add_parser("chart", help="series inside a chart")
    p.add_argument("deck")
    p.add_argument("--slide", type=int, required=True)
    p.add_argument("--chart", type=int, default=0)
    p.set_defaults(func=cmd_chart)

    p = sub.add_parser("pair", help="render source vs input for given pages")
    p.add_argument("deck")
    p.add_argument("pages", nargs="+", type=int)
    p.add_argument("--dpi", type=int, default=110)
    p.set_defaults(func=cmd_pair)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
