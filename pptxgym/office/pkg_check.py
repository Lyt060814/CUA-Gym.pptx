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
#
# `docProps/thumbnail.jpeg` used to be on this list and is deliberately not.
# A healthy package reaches it from `_rels/.rels` like any other part, so the
# exemption never suppressed an orphan on a deck that was intact — measured
# across all 44 packages under `work/`, the orphan list is byte-identical with
# and without it.  The only file it ever changed the verdict on is one where
# the part is present and its relationship is already gone: a half-finished
# thumbnail strip, which is exactly the state the gate should be reporting.
ALWAYS_KEEP = {
    "[Content_Types].xml",
    "_rels/.rels",
    "docProps/app.xml",
    "docProps/core.xml",
    "docProps/custom.xml",
}

THUMB_PART = re.compile(r"^docProps/thumbnail\.[^/]+$")


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

    problems += _diagram_caches(pptx_path)
    return {"deck": pptx_path, "problems": problems, "orphans": orphans,
            "duplicate_ids": dup_ids,
            "ok": not problems and not dup_ids}


def _diagram_caches(pptx_path: str) -> list[str]:
    """A live SmartArt whose pre-rendered drawing has gone missing.

    `ppt/diagrams/dataN.xml` names its cache by rId in `<dsp:dataModelExt>`,
    and the slide never mentions it, so a dead-rel sweep reads it as garbage.
    Losing it does not corrupt the file and nothing downstream notices — but
    renderers that trust the cache (LibreOffice does) re-lay the diagram out
    from scratch, so an untouched SmartArt quietly changes shape between the
    ground truth and the input.  That difference is not in the instruction and
    no GUI action restores a cached part, so it cannot be graded.

    The executor guards against this and a fresh run reproduces nothing; one
    shipped `input.pptx` lost a cache anyway and I could not explain how.  A
    condition worth this much depends on nothing being able to produce it
    quietly, which is a job for the gate, not for the guard being right.
    """
    out = []
    with zipfile.ZipFile(pptx_path) as z:
        names = set(z.namelist())
        for n in sorted(names):
            m = re.match(r"ppt/slides/(slide\d+)\.xml$", n)
            if not m or _rels_path(n) not in names:
                continue
            try:
                root = etree.fromstring(z.read(_rels_path(n)))
            except etree.XMLSyntaxError:
                continue
            rids = {r.get("Id"): r.get("Type", "").rsplit("/", 1)[-1]
                    for r in root.findall(f"{{{RELS_NS}}}Relationship")}
            for rel in root.findall(f"{{{RELS_NS}}}Relationship"):
                if rel.get("Type", "").rsplit("/", 1)[-1] != "diagramData":
                    continue
                data = _resolve(n, rel.get("Target", ""))
                if data not in names:
                    continue
                want = re.findall(rb'dataModelExt[^>]*relId="([^"]+)"',
                                  z.read(data))
                for rid in {r.decode() for r in want}:
                    if rids.get(rid) != "diagramDrawing":
                        out.append(
                            f"{m.group(1)}: SmartArt in {data.split('/')[-1]} "
                            f"points at its drawing cache ({rid}) but the "
                            f"relationship is gone — the diagram will be "
                            f"re-laid out on open")
    return out


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


def thumbnail_leak(input_pptx: str, delta: dict) -> list[str]:
    """A rendered picture of slide 1, before the damage, inside the input.

    Office writes `docProps/thumbnail.*` into the package and refreshes it on
    every save.  Copy a deck, damage it, save it with python-pptx and the old
    picture rides along — so where slide 1 is what the agent must rebuild, the
    file it is handed contains a photograph of the answer.  `degrade_exec`
    strips the part; this is the gate that notices when it did not.

    **Fires on any surviving thumbnail, not only when the delta touches page
    0.**  The narrower rule is the tempting one — the leak is only an *answer*
    leak on a damaged slide 1 — and it is the wrong one here:

      * The strip is unconditional, so a thumbnail that survived means the
        strip did not run.  Whatever produced that file bypassed the one place
        the guarantee lives, and the next thing it produces will not be safe
        either.  A gate that stays quiet because this particular deck happens
        not to be harmed reports the harm and misses the fault.
      * Page indices in the delta name slides in the *ground truth's* order.
        `reorder_slides` makes a different slide the first one, at which point
        "page 0 is in the delta" and "the thumbnail depicts a damaged slide"
        are two different questions.  Conditioning would mean re-deriving that
        correspondence here and keeping it right as deck-level operators land
        — the same fragile rule `degrade_exec` declined to write, and no
        better for being written twice.
      * A false alarm costs a re-run of one stage.  A miss ships a task whose
        answer is in the input.

    The delta still decides the *wording*: a damaged slide 1 is an answer leak
    and says so, anything else is a broken guarantee.  Both fail the gate.
    """
    with zipfile.ZipFile(input_pptx) as z:
        found = sorted(n for n in z.namelist() if THUMB_PART.match(n))
        sizes = {n: z.getinfo(n).file_size for n in found}
    if not found:
        return []

    # json round-trips these keys to strings; an in-memory delta may not have
    pages = {str(k) for k in (delta.get("slides") or {})}
    out = []
    for n in found:
        if "0" in pages:
            why = ("slide 1 is damaged, so this is a picture of the answer "
                   "the agent is being asked to produce")
        else:
            why = ("slide 1 is undamaged, so this leaks no answer — but the "
                   "strip is unconditional, so its survival means the strip "
                   "did not run on this file")
        out.append(f"{n} ({sizes[n]} B) survived the degrade — {why}")
    # a delta that claims the strip happened while the part is still there is
    # worse than either: the record of the build disagrees with the build
    claimed = [p for p in (delta.get("stripped_parts") or []) if p in found]
    for p in claimed:
        out.append(f"{p}: delta lists it in stripped_parts but it is still "
                   f"in the package")
    return out


def leak_check(input_pptx: str, delta: dict, gt_pptx: str | None = None) -> dict:
    """Does the degraded package still contain what the agent must rebuild?

    Three independent signals, because any one alone misses cases:
      · a rendering of slide 1 from before the damage is still in the package
      · the part inventory did not shrink relative to the ground truth
      · a slide still relates to a part nothing on it draws

    The thumbnail runs before the `removed_kinds` gate below, and outside it.
    That gate exists because the other two signals ask "did a removal strand
    something", and with no removal there is nothing to strand.  The thumbnail
    needs no removal at all — a shape merely *nudged* on slide 1 leaks it in
    full — so a recipe of pure moves and recolours, which disarms everything
    downstream, is precisely a recipe this must still answer for.  For the
    same reason it is not an entry in `LEAK_PREFIXES`: that map is keyed by
    what the recipe removed, and is only ever read under `for kind in
    removed_kinds`.
    """
    thumb = thumbnail_leak(input_pptx, delta)
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
        # `applicable` tracks whether the check had anything to say, so a
        # finding makes it True even with no deletion to hang it off — a
        # False alongside a non-empty `leaks` would be a lie to any future
        # reader.  Neither current consumer reads it; both concatenate
        # `leaks` + `dead_rels`, and that contract is unchanged.
        return {"applicable": bool(thumb), "leaks": thumb, "dead_rels": []}

    leaks = list(thumb)
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
