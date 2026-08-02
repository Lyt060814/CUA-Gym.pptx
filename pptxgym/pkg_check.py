"""Package-integrity gate for degraded decks.

Our ops edit OOXML directly and some of them delete things — `chart_delete`
drops a relationship, a slide-level degradation may remove a picture.  Two
failure modes follow, and neither shows up in a render:

  **Broken references.**  A dangling ``r:id``, a ``.rels`` target that no
  longer exists, or a part with no content-type produces a file that opens
  differently in LibreOffice, WPS and PowerPoint — PowerPoint may offer to
  "repair" it.  A task whose input file behaves differently per application
  has a reward that drifts with the environment, which is worse than a task
  that is merely hard.

  **Orphan parts.**  Dropping the relationship to a chart leaves the chart
  part, its embedded workbook and its cached values sitting in the archive.
  That is an *answer leak*: an agent that unzips the input reads the numbers
  it was supposed to rebuild from the render.

Deliberately not an XSD validation.  Schema conformance mostly catches
attribute domains, which our ops do not violate; reference integrity is where
the real risk is, and it needs no external schema files.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

from lxml import etree

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
SHAPE_LOCAL = {"sp", "pic", "graphicFrame", "cxnSp", "grpSp"}
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# parts every package carries that nothing explicitly points at
ALWAYS_KEEP = {
    "[Content_Types].xml",
    "_rels/.rels",
    "docProps/app.xml",
    "docProps/core.xml",
    "docProps/thumbnail.jpeg",
    "docProps/custom.xml",
}


def _drawn_shapes(root):
    """Shapes on the slide itself, skipping alternate-content stand-ins."""
    tree = root.find(f"{{{P_NS}}}cSld/{{{P_NS}}}spTree")
    if tree is None:
        return []
    out = []

    def walk(container):
        for child in container:
            qn = etree.QName(child)
            if qn.namespace == MC_NS and qn.localname == "AlternateContent":
                branch = child.find(f"{{{MC_NS}}}Choice")
                if branch is None:
                    branch = child.find(f"{{{MC_NS}}}Fallback")
                if branch is not None:
                    walk(branch)
            elif qn.namespace == P_NS and qn.localname in SHAPE_LOCAL:
                out.append(child)
                if qn.localname == "grpSp":
                    walk(child)

    walk(tree)
    return out


def _rels_path(part: str) -> str:
    d, n = posixpath.split(part)
    return posixpath.join(d, "_rels", n + ".rels")


def _resolve(base_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))


def check(pptx_path: str) -> dict:
    problems: list[str] = []
    with zipfile.ZipFile(pptx_path) as z:
        names = set(z.namelist())

        # -- content types --------------------------------------------------- #
        defaults, overrides = {}, {}
        try:
            ct = etree.fromstring(z.read("[Content_Types].xml"))
            for el in ct:
                tag = etree.QName(el).localname
                if tag == "Default":
                    defaults[el.get("Extension", "").lower()] = el.get("ContentType")
                elif tag == "Override":
                    overrides[el.get("PartName", "").lstrip("/")] = el.get("ContentType")
        except (KeyError, etree.XMLSyntaxError) as e:
            problems.append(f"[Content_Types].xml unreadable: {e}")

        for n in sorted(names):
            if n.endswith("/") or n.startswith("_rels/") or "/_rels/" in n:
                continue
            if n == "[Content_Types].xml":
                continue
            ext = n.rsplit(".", 1)[-1].lower() if "." in n else ""
            if n not in overrides and ext not in defaults:
                problems.append(f"no content type for part: {n}")

        # -- relationship graph ---------------------------------------------- #
        referenced: set[str] = set()
        rel_targets: dict[str, dict[str, str]] = {}
        for n in sorted(names):
            if not n.endswith(".rels"):
                continue
            try:
                root = etree.fromstring(z.read(n))
            except etree.XMLSyntaxError as e:
                problems.append(f"{n}: malformed rels ({e})")
                continue
            # "_rels/.rels" owns the package root (""); "a/_rels/b.xml.rels"
            # owns "a/b.xml".  normpath("") is ".", which silently orphans the
            # entire graph, so the root case is handled explicitly.
            if n == "_rels/.rels":
                owner = ""
            else:
                d, base = posixpath.split(n)
                owner = posixpath.normpath(
                    posixpath.join(posixpath.dirname(d), base.removesuffix(".rels")))
            table = {}
            for rel in root.findall(f"{{{RELS_NS}}}Relationship"):
                rid, target = rel.get("Id"), rel.get("Target", "")
                if rel.get("TargetMode") == "External":
                    table[rid] = None
                    continue
                resolved = _resolve(owner or "x", target)
                table[rid] = resolved
                if resolved not in names:
                    problems.append(f"{n}: r:id {rid} -> missing part {resolved}")
                else:
                    referenced.add(resolved)
            rel_targets[owner] = table

        # -- r:id usage inside parts ----------------------------------------- #
        for n in sorted(names):
            if not n.endswith(".xml") or n.endswith(".rels"):
                continue
            try:
                blob = z.read(n)
            except KeyError:
                continue
            used = set(re.findall(rb'r:(?:id|embed|link|dm|lo|qs|cs|pict)="([^"]+)"',
                                  blob))
            if not used:
                continue
            table = rel_targets.get(n)
            if table is None:
                problems.append(f"{n}: uses {len(used)} r:id but has no .rels")
                continue
            for rid in sorted(used):
                if rid.decode() not in table:
                    problems.append(f"{n}: dangling {rid.decode()}")

        # -- reachability from the package root ------------------------------ #
        reachable = {"[Content_Types].xml"}
        frontier = [t for t in (rel_targets.get("", {}) or {}).values() if t]
        while frontier:
            part = frontier.pop()
            if part in reachable:
                continue
            reachable.add(part)
            for t in (rel_targets.get(part, {}) or {}).values():
                if t and t not in reachable:
                    frontier.append(t)

        orphans = []
        for n in sorted(names):
            if n.endswith("/") or n.endswith(".rels") or n in ALWAYS_KEEP:
                continue
            if n not in reachable:
                orphans.append(n)

        # -- unique shape ids per slide -------------------------------------- #
        dup_ids = []
        for n in sorted(names):
            if not re.match(r"ppt/slides/slide\d+\.xml$", n):
                continue
            try:
                root = etree.fromstring(z.read(n))
            except etree.XMLSyntaxError as e:
                problems.append(f"{n}: malformed slide XML ({e})")
                continue
            # Only shapes that actually take part in drawing need unique ids.
            # An OLE object's mc:Fallback carries a stand-in <p:pic> whose
            # cNvPr id is 0 — PowerPoint writes it that way, so counting every
            # cNvPr in the file flags healthy source decks as corrupt.
            seen = defaultdict(int)
            for el in _drawn_shapes(root):
                for c in el.iter():
                    if etree.QName(c).localname == "cNvPr" and c.get("id"):
                        seen[c.get("id")] += 1
                        break
            for sid, cnt in seen.items():
                if cnt > 1:
                    dup_ids.append(f"{n}: shape id {sid} used {cnt}x")

    return {"deck": pptx_path, "problems": problems, "orphans": orphans,
            "duplicate_ids": dup_ids,
            "ok": not problems and not dup_ids}


LEAK_PREFIXES = {"chart": ("ppt/charts/", "ppt/embeddings/"),
                 "picture": ("ppt/media/",),
                 "diagram": ("ppt/diagrams/",),
                 "table": (),
                 "any": ("ppt/charts/", "ppt/embeddings/", "ppt/media/",
                         "ppt/diagrams/")}


def dead_rels(pptx_path: str) -> list[str]:
    """Parts a slide still points at but no longer draws.

    Removing a shape without removing its relationship leaves the part in the
    package, fully reachable — reachability alone will call it healthy.  It is
    the exact residue an object-deletion degradation leaves behind, and it is
    readable with `unzip`.
    """
    out = []
    with zipfile.ZipFile(pptx_path) as z:
        names = set(z.namelist())
        for n in sorted(names):
            m = re.match(r"ppt/slides/(slide\d+)\.xml$", n)
            if not m:
                continue
            rels = _rels_path(n)
            if rels not in names:
                continue
            try:
                body = z.read(n)
                root = etree.fromstring(z.read(rels))
            except (KeyError, etree.XMLSyntaxError):
                continue
            used = {r.decode() for r in
                    re.findall(rb'r:(?:id|embed|link|dm|lo|qs|cs|pict)="([^"]+)"',
                               body)}
            for rel in root.findall(f"{{{RELS_NS}}}Relationship"):
                rid = rel.get("Id")
                rtype = rel.get("Type", "").rsplit("/", 1)[-1]
                # diagramDrawing is SmartArt's pre-rendered fallback: the slide
                # reaches it through the diagram's own extension data, never by
                # an r:id in the slide body, so a body scan always calls it
                # dead — including on slides nothing touched.
                # (an unreferenced *image* rel is the leak we are looking for,
                # so it deliberately stays in scope)
                if rtype in ("slideLayout", "notesSlide", "hyperlink",
                             "diagramDrawing", "tags"):
                    continue
                if rel.get("TargetMode") == "External":
                    continue
                if rid not in used:
                    out.append(f"{m.group(1)}: {rid} -> "
                               f"{_resolve(n, rel.get('Target', ''))} (never drawn)")
    return out


def leak_check(input_pptx: str, delta: dict, gt_pptx: str | None = None) -> dict:
    """Does the degraded package still contain what the agent must rebuild?

    Two independent signals, because either alone misses cases:
      · the part inventory did not shrink relative to the ground truth
      · a slide still relates to a part nothing on it draws
    """
    # Any op that removes a shape can strand its part, not just ones whose
    # name happens to end in "_delete" — matching on the name alone once let a
    # whole run ship with the deleted diagrams' node text still in the archive.
    removed_kinds = set()
    for muts in (delta.get("slides") or {}).values():
        for m in muts:
            op_name = m.get("op", "")
            if op_name.endswith("_delete"):
                removed_kinds.add(op_name.rsplit("_", 1)[0])
            elif op_name in ("delete", "blank_slide"):
                kind = m.get("kind")
                removed_kinds.add({"picture": "picture", "chart": "chart",
                                   "smartart": "diagram",
                                   "table": "table"}.get(kind, "any"))
    if not removed_kinds:
        return {"applicable": False, "leaks": [], "dead_rels": []}

    leaks = []
    if gt_pptx:
        with zipfile.ZipFile(input_pptx) as zi, zipfile.ZipFile(gt_pptx) as zg:
            inn, gtn = zi.namelist(), zg.namelist()
            # Inventory shrink is only meaningful for parts a single shape owns.
            # An image is routinely drawn on several slides, so deleting one
            # instance legitimately leaves the part behind — dead_rels is the
            # trustworthy signal there.
            for kind in removed_kinds & {"chart", "diagram"}:
                for prefix in LEAK_PREFIXES.get(kind, ()):
                    a = sum(1 for n in inn if n.startswith(prefix))
                    b = sum(1 for n in gtn if n.startswith(prefix))
                    if a >= b and b:
                        leaks.append(f"{prefix}: {a} part(s) in input vs {b} in gt "
                                     f"— {kind}_delete removed no data")
    orphan = set(check(input_pptx)["orphans"])
    for kind in removed_kinds:
        for prefix in LEAK_PREFIXES.get(kind, ()):
            leaks += [f"{n} (orphaned but present)" for n in orphan
                      if n.startswith(prefix)]
    dead = [d for d in dead_rels(input_pptx)
            if any(p in d for k in removed_kinds
                   for p in LEAK_PREFIXES.get(k, ()))]
    return {"applicable": True, "leaks": sorted(set(leaks)), "dead_rels": dead}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pptx", nargs="+")
    ap.add_argument("--delta", default=None,
                    help="delta.json — also run the answer-leak check")
    ap.add_argument("--gt", default=None,
                    help="gt.pptx — compare part inventories for the leak check")
    ap.add_argument("--strict-orphans", action="store_true",
                    help="treat any orphan part as a failure")
    args = ap.parse_args()
    bad = 0
    for path in args.pptx:
        r = check(path)
        leak = None
        if args.delta:
            leak = leak_check(path, json.load(open(args.delta)), args.gt)
        ok = r["ok"] and not (args.strict_orphans and r["orphans"]) \
            and not (leak and (leak["leaks"] or leak["dead_rels"]))
        print(f"{'OK  ' if ok else 'FAIL'} {Path(path).name}")
        for p in r["problems"][:12]:
            print(f"   ! {p}")
        for d in r["duplicate_ids"][:6]:
            print(f"   ! {d}")
        if r["orphans"]:
            print(f"   ~ {len(r['orphans'])} orphan part(s): "
                  f"{', '.join(r['orphans'][:4])}")
        if leak and leak["leaks"]:
            print(f"   !! ANSWER LEAK: {'; '.join(leak['leaks'][:4])}")
        if leak and leak["dead_rels"]:
            print(f"   !! DEAD RELS ({len(leak['dead_rels'])}): "
                  f"{'; '.join(leak['dead_rels'][:4])}")
        bad += 0 if ok else 1
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
