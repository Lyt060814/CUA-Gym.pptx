"""Try to cheat every task, so that a task which can be cheated never ships.

Offline calibration proves two points on a curve: the ground truth scores 1.0
and the broken file scores 0.0.  It says nothing about the enormous space in
between, and that space is where an RL agent lives.  A task whose reward can be
collected by pasting a screenshot over the hole teaches the model to paste
screenshots.  The rule this module enforces is the project owner's:

    better to lose a task than to ship one that can be hacked.

So each attack builds a candidate deck that **should not score**, the candidate
is scored by the real comparator, and any attack that beats its threshold
rejects the task.  So does an attack that *applies but cannot be built*: a gate
you never fired is not a gate, and reporting it as "skipped" is how an unproven
gate gets shipped.  The only thing that is not a rejection is an attack that
does not apply at all (`native_to_picture` on a deck whose damage contains no
chart, table or SmartArt), and even that is printed, with the reason.

One entry in the table is not an attack.  `half_restore` restores exactly half
the components and must score about 0.5: RL needs partial work to earn partial
credit, and an earlier batch shipped tasks that scored all-or-nothing across
five components.  A task that fails that is as unusable as one that can be
cheated, for the opposite reason.

Adding an attack
----------------
The list below is what one person thought of; a trained agent searches for what
nobody listed.  So an attack is one function plus a declared expectation:

    @attack("my_cheat", "what it does", AtMost(0.05),
            applies=lambda ctx: None if ctx.deletions else "no deletions")
    def _my_cheat(ctx, out):
        pkg = ctx.open_input()
        ...
        return _built(pkg, out, "what the produced file actually contains")

`applies` returns `None` when the attack is meaningful for this deck, or a
human sentence saying why it is not.  Raising `Unconstructible` from the body
means "this should have been possible and was not" — a rejection, not a skip.
The evidence string is computed by **reading the produced file back**, so an
attack that silently no-ops cannot pass itself off as a clean sweep.

What every attack works from
----------------------------
`work/deck00NN/` gives three things: `source.pptx` (ground truth), `input.pptx`
(what the solver gets) and `delta.json` (every degradation together with the
value it replaced).  Attacks are allowed to read all of it — an attacker with
the answer key that still scores 0 is a strong result; the reverse would be a
task where knowing the answer key is worth points.

Scoring goes through `pptxgym.comparators.score(plan, candidate_inv, gt_inv,
init_inv)`.  That module is written by somebody else and its plan builder is
discovered by name (`Scorer.PLAN_BUILDERS`); until it exists this module builds
candidates and refuses to invent scores for them.

    python3 -m pptxgym.attacks work/deck00*/ -o attack-report.md
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import inspect
import json
import os
import posixpath
import re
import shutil
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from lxml import etree

from . import inventory as inv

EMU_IN = 914400

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "dsp": "http://schemas.microsoft.com/office/drawing/2008/diagram",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}
IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
SLIDE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
SLIDE_CT = ("application/vnd.openxmlformats-officedocument."
            "presentationml.slide+xml")

RENDER_DPI = 150


def q(tag: str) -> str:
    prefix, local = tag.split(":")
    return f"{{{NS[prefix]}}}{local}"


class Unconstructible(RuntimeError):
    """The attack applies to this deck and could not be built.

    This is a rejection, never a skip.  `native_to_picture` that cannot be
    built for a deck whose damage includes a chart leaves that gate unproven,
    and an unproven gate is indistinguishable from a gate that would have
    failed.
    """


class ScorerUnavailable(RuntimeError):
    """`pptxgym.comparators` is absent or does not expose a plan builder."""


# --------------------------------------------------------------------------- #
# package surgery
# --------------------------------------------------------------------------- #


def _resolve(part: str, target: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(part), target))


def _rels_name(part: str) -> str:
    head, _, tail = part.rpartition("/")
    return f"{head}/_rels/{tail}.rels" if head else f"_rels/{tail}.rels"


def _parse(data: bytes):
    return etree.fromstring(data)


def _ser(el) -> bytes:
    return etree.tostring(el, xml_declaration=True, encoding="UTF-8",
                          standalone=True)


class Pkg:
    """An OPC package held in memory, edited part by part.

    Every attack works at this level rather than through python-pptx because a
    load-and-save through any library rewrites *every* part, and then the
    difference between "the attack changed this" and "the library changed this"
    has to be argued rather than read off.  Here, `noop` really is byte-identical
    to `input.pptx`, and a candidate differs from its base exactly where the
    attack touched it.
    """

    def __init__(self, path: str | Path):
        self.src = str(path)
        self._data: dict[str, bytes] = {}
        self._order: list[str] = []
        with zipfile.ZipFile(self.src) as z:
            for info in z.infolist():
                if info.filename.endswith("/"):
                    continue
                self._order.append(info.filename)
                self._data[info.filename] = z.read(info.filename)

    # -- parts ------------------------------------------------------------- #

    def has(self, name: str) -> bool:
        return name in self._data

    def names(self) -> list[str]:
        return list(self._order)

    def read(self, name: str) -> bytes:
        return self._data[name]

    def put(self, name: str, data: bytes) -> None:
        if name not in self._data:
            self._order.append(name)
        self._data[name] = data

    def drop(self, name: str) -> None:
        self._data.pop(name, None)
        if name in self._order:
            self._order.remove(name)

    def xml(self, name: str):
        return _parse(self._data[name])

    def set_xml(self, name: str, el) -> None:
        self.put(name, _ser(el))

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for name in self._order:
                z.writestr(name, self._data[name])
        return path

    # -- relationships ----------------------------------------------------- #

    def rels(self, part: str) -> list[dict[str, str]]:
        name = _rels_name(part)
        if not self.has(name):
            return []
        out = []
        for node in self.xml(name).findall("pr:Relationship", NS):
            out.append({"id": node.get("Id", ""), "type": node.get("Type", ""),
                        "target": node.get("Target", ""),
                        "mode": node.get("TargetMode", "Internal")})
        return out

    def set_rels(self, part: str, rels: list[dict[str, str]]) -> None:
        root = etree.Element(q("pr:Relationships"), nsmap={None: NS["pr"]})
        for rel in rels:
            node = etree.SubElement(root, q("pr:Relationship"))
            node.set("Id", rel["id"])
            node.set("Type", rel["type"])
            node.set("Target", rel["target"])
            if rel.get("mode") == "External":
                node.set("TargetMode", "External")
        self.set_xml(_rels_name(part), root)

    def add_rel(self, part: str, type_url: str, target: str) -> str:
        rels = self.rels(part)
        used = {int(m.group(1)) for rel in rels
                if (m := re.match(r"rId(\d+)$", rel["id"]))}
        rid = f"rId{max(used, default=0) + 1}"
        rels.append({"id": rid, "type": type_url, "target": target,
                     "mode": "Internal"})
        self.set_rels(part, rels)
        return rid

    def targets(self, part: str) -> list[str]:
        return [_resolve(part, rel["target"]) for rel in self.rels(part)
                if rel["mode"] != "External"]

    # -- content types ----------------------------------------------------- #

    def ensure_default(self, ext: str, ctype: str) -> None:
        root = self.xml("[Content_Types].xml")
        for node in root.findall("ct:Default", NS):
            if (node.get("Extension") or "").lower() == ext.lower():
                return
        node = etree.SubElement(root, q("ct:Default"))
        node.set("Extension", ext)
        node.set("ContentType", ctype)
        self.set_xml("[Content_Types].xml", root)

    def ensure_override(self, part: str, ctype: str) -> None:
        root = self.xml("[Content_Types].xml")
        want = "/" + part
        for node in root.findall("ct:Override", NS):
            if node.get("PartName") == want:
                return
        node = etree.SubElement(root, q("ct:Override"))
        node.set("PartName", want)
        node.set("ContentType", ctype)
        self.set_xml("[Content_Types].xml", root)

    def override_of(self, part: str) -> str | None:
        want = "/" + part
        for node in self.xml("[Content_Types].xml").findall("ct:Override", NS):
            if node.get("PartName") == want:
                return node.get("ContentType")
        return None

    def drop_override(self, part: str) -> None:
        root = self.xml("[Content_Types].xml")
        want = "/" + part
        for node in root.findall("ct:Override", NS):
            if node.get("PartName") == want:
                root.remove(node)
        self.set_xml("[Content_Types].xml", root)

    # -- slides ------------------------------------------------------------ #

    def slide_parts(self) -> list[str]:
        """Slide parts in presentation order, which is not part-name order."""
        pres = "ppt/presentation.xml"
        if not self.has(pres):
            return sorted((n for n in self.names()
                           if re.match(r"^ppt/slides/slide\d+\.xml$", n)),
                          key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
        by_id = {rel["id"]: _resolve(pres, rel["target"])
                 for rel in self.rels(pres) if rel["mode"] != "External"}
        out = []
        for node in self.xml(pres).findall("p:sldIdLst/p:sldId", NS):
            target = by_id.get(node.get(q("r:id")) or "")
            if target and self.has(target):
                out.append(target)
        return out

    def slide_size(self) -> tuple[int, int]:
        node = self.xml("ppt/presentation.xml").find("p:sldSz", NS)
        return (int(node.get("cx")), int(node.get("cy")))

    def sp_tree(self, slide_part: str):
        return self.xml(slide_part).find("p:cSld/p:spTree", NS)


def closure(pkg: Pkg, roots) -> set[str]:
    """Every part reachable from `roots` through internal relationships."""
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        part = stack.pop()
        if part in seen or not pkg.has(part):
            continue
        seen.add(part)
        rels = _rels_name(part)
        if pkg.has(rels):
            seen.add(rels)
        stack.extend(pkg.targets(part))
    return seen


def _shape_children(el):
    """Drawable children in document order — `inventory`'s own walk order.

    Reusing it is not a convenience: a path like `6/2` addresses the shape the
    recipe meant only if this module unwraps `mc:AlternateContent` exactly the
    way the inventory does.
    """
    return list(inv._shape_children(el))


def resolve_path(sp_tree, path: str):
    """The shape at a recipe path (`"3"`, `"19/0"`), or None."""
    node = sp_tree
    for step in str(path).split("/"):
        kids = _shape_children(node)
        index = int(step)
        if index >= len(kids):
            return None
        node = kids[index]
    return node


def next_shape_id(sp_tree) -> int:
    ids = [int(n.get("id")) for n in sp_tree.iter(q("p:cNvPr"))
           if (n.get("id") or "").isdigit()]
    return max(ids, default=1) + 1


def _xfrm_of(shape):
    """The shape's own transform element, wherever its type keeps it."""
    if shape.tag == q("p:graphicFrame"):
        node = shape.find("p:xfrm", NS)
        if node is not None:
            return node
    for holder in ("p:spPr", "p:grpSpPr"):
        parent = shape.find(holder, NS)
        if parent is not None:
            node = parent.find("a:xfrm", NS)
            if node is not None:
                return node
    return None


def _set_box(shape, x=None, y=None, cx=None, cy=None) -> bool:
    xfrm = _xfrm_of(shape)
    if xfrm is None:
        return False
    off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
    if off is not None:
        if x is not None:
            off.set("x", str(int(x)))
        if y is not None:
            off.set("y", str(int(y)))
    if ext is not None:
        if cx is not None:
            ext.set("cx", str(max(1, int(cx))))
        if cy is not None:
            ext.set("cy", str(max(1, int(cy))))
    return True


def _get_box(shape):
    xfrm = _xfrm_of(shape)
    if xfrm is None:
        return None
    off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
    if off is None or ext is None:
        return None
    return (int(off.get("x", 0)), int(off.get("y", 0)),
            int(ext.get("cx", 0)), int(ext.get("cy", 0)))


def add_picture(pkg: Pkg, slide_part: str, png: bytes, box, name: str) -> None:
    """Drop a PNG onto a slide, on top of everything already there."""
    digest = hashlib.sha256(png).hexdigest()[:12]
    media = f"ppt/media/attack{digest}.png"
    if not pkg.has(media):
        pkg.put(media, png)
    pkg.ensure_default("png", "image/png")
    rid = pkg.add_rel(slide_part, IMAGE_REL,
                      posixpath.relpath(media, posixpath.dirname(slide_part)))
    root = pkg.xml(slide_part)
    tree = root.find("p:cSld/p:spTree", NS)
    x, y, cx, cy = box
    pic = etree.fromstring(f"""<p:pic xmlns:p="{NS['p']}" xmlns:a="{NS['a']}"
        xmlns:r="{NS['r']}">
      <p:nvPicPr><p:cNvPr id="{next_shape_id(tree)}" name="{name}"/>
        <p:cNvPicPr/><p:nvPr/></p:nvPicPr>
      <p:blipFill><a:blip r:embed="{rid}"/>
        <a:stretch><a:fillRect/></a:stretch></p:blipFill>
      <p:spPr><a:xfrm><a:off x="{int(x)}" y="{int(y)}"/>
        <a:ext cx="{int(cx)}" cy="{int(cy)}"/></a:xfrm>
        <a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
    </p:pic>""".encode())
    tree.append(pic)
    pkg.set_xml(slide_part, root)


def copy_slides(dst: Pkg, src: Pkg, indices) -> list[str]:
    """Replace whole slides in `dst` with `src`'s versions of the same slides.

    Slide-level, not shape-level, because a shape-level restore has to
    re-create dropped relationships, media parts, diagram data and chart
    workbooks by hand, and a half-restore that is only half a restore proves
    nothing about monotonicity.

    A part reachable from a slide that is *not* being restored is left alone:
    otherwise restoring one component would quietly restore a second one that
    happens to share a media part.
    """
    parts = dst.slide_parts()
    chosen = [parts[i] for i in indices if i < len(parts)]
    protected = closure(dst, [p for p in parts if p not in chosen])
    protected |= closure(dst, [t for t in dst.targets("ppt/presentation.xml")
                               if t not in parts])
    for part in sorted(closure(src, chosen)):
        if not src.has(part):
            continue
        if dst.has(part) and part in protected:
            continue
        dst.put(part, src.read(part))
        ctype = src.override_of(part)
        if ctype:
            dst.ensure_override(part, ctype)
    for ext_node in src.xml("[Content_Types].xml").findall("ct:Default", NS):
        dst.ensure_default(ext_node.get("Extension"), ext_node.get("ContentType"))
    return chosen


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


_RENDER_LOCK = threading.Lock()


def render_pages(pptx: Path, cache_dir: Path, dpi: int = RENDER_DPI) -> list[Path]:
    """Every page of a deck as a PNG, 0-indexed, cached on disk."""
    from . import render as rd

    cache_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(cache_dir.glob("page-*.png"))
    if existing:
        return existing
    with _RENDER_LOCK:
        existing = sorted(cache_dir.glob("page-*.png"))
        if existing:
            return existing
        rd.render_pptx(str(pptx), str(cache_dir), prefix="page", dpi=dpi)
    out = sorted(cache_dir.glob("page-*.png"))
    if not out:
        raise Unconstructible(f"soffice rendered no pages for {pptx}")
    return out


def crop_png(png: Path, box, slide_size) -> bytes:
    """The part of a rendered page covered by an EMU box, as PNG bytes."""
    from io import BytesIO

    from PIL import Image

    image = Image.open(png).convert("RGB")
    sw, sh = slide_size
    x, y, cx, cy = box
    left = max(0, int(round(x / sw * image.width)))
    top = max(0, int(round(y / sh * image.height)))
    right = min(image.width, int(round((x + cx) / sw * image.width)))
    bottom = min(image.height, int(round((y + cy) / sh * image.height)))
    if right - left < 2 or bottom - top < 2:
        raise Unconstructible(f"box {box} crops to nothing on {png.name}")
    buf = BytesIO()
    image.crop((left, top, right, bottom)).save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# the deck under attack
# --------------------------------------------------------------------------- #


@dataclass
class Ctx:
    """One task: ground truth, the file the solver gets, and what was done to it."""

    deck: Path
    scratch: Path
    delta: dict = field(default_factory=dict)
    recipe: dict = field(default_factory=dict)
    #: degradation id -> its share of the reward, filled in from the plan when
    #: one is available.  `half_restore` needs it; nothing else does.
    weights: dict[str, float] = field(default_factory=dict)

    @classmethod
    def load(cls, deck: str | Path, scratch: str | Path) -> "Ctx":
        deck = Path(deck)
        ctx = cls(deck=deck, scratch=Path(scratch))
        ctx.delta = json.loads((deck / "delta.json").read_text())
        recipe = deck / "recipe.json"
        ctx.recipe = json.loads(recipe.read_text()) if recipe.exists() else {}
        ctx.scratch.mkdir(parents=True, exist_ok=True)
        return ctx

    # -- inputs ------------------------------------------------------------ #

    @property
    def name(self) -> str:
        return self.deck.name

    @property
    def gt_path(self) -> Path:
        return self.deck / "source.pptx"

    @property
    def input_path(self) -> Path:
        return self.deck / "input.pptx"

    def open_gt(self) -> Pkg:
        return Pkg(self.gt_path)

    def open_input(self) -> Pkg:
        return Pkg(self.input_path)

    # -- what was damaged -------------------------------------------------- #

    def entries(self) -> list[dict]:
        """Every delta entry, tagged with its 0-based slide index and component.

        `deg` is present on nine of the ten decks; on the tenth the recipe step
        that produced the entry still carries it, and the join is exact on
        (slide, op, path).  Guessing the grouping would silently change what
        "half the components" means.
        """
        out = []
        for key, items in self.delta.get("slides", {}).items():
            for item in items:
                record = dict(item)
                record["_slide"] = int(key)
                record.setdefault("deg", None)
                out.append(record)
        unknown = [e for e in out if not e["deg"]]
        if unknown:
            self._join_recipe(unknown)
        for index, entry in enumerate(out):
            if not entry["deg"]:
                entry["deg"] = f"?{index}"
        out.sort(key=lambda e: (e["_slide"], str(e.get("path"))))
        return out

    def _join_recipe(self, entries) -> None:
        by_key: dict[tuple, str] = {}
        for key, steps in self.recipe.get("slides", {}).items():
            slide = int(key) - 1                 # recipe is 1-based, delta 0-based
            for step in steps:
                for path in step.get("paths", []) or ["-"]:
                    by_key[(slide, step["op"], str(path))] = step.get("deg")
        for section in ("smartart", "charts", "tables", "animation"):
            for step in self.recipe.get(section, []) or []:
                slide = int(step.get("slide", 0)) - 1
                by_key[(slide, section, "-")] = step.get("deg")
        for entry in entries:
            entry["deg"] = by_key.get(
                (entry["_slide"], entry["op"], str(entry.get("path"))))

    def components(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for entry in self.entries():
            out.setdefault(entry["deg"], []).append(entry)
        return dict(sorted(out.items()))

    def component_slides(self) -> dict[str, set[int]]:
        return {deg: {e["_slide"] for e in items}
                for deg, items in self.components().items()}

    def damaged_slides(self) -> list[int]:
        return sorted({e["_slide"] for e in self.entries()})

    def deletions(self) -> list[dict]:
        return [e for e in self.entries() if e["op"] == "delete" and e.get("box")]

    def composite_entries(self) -> list[dict]:
        """Damage that involves a chart, a table or a SmartArt graphic."""
        native_ops = {"clear_table_cells", "table_drop_rows", "table_drop_cols",
                      "chart_edit", "smartart_drop_nodes", "smartart"}
        out = []
        for entry in self.entries():
            if entry.get("kind") in ("chart", "table", "smartart"):
                out.append(entry)
            elif entry["op"] in native_ops:
                out.append(entry)
        return out

    def untouched_slide(self, pkg: Pkg) -> int | None:
        damaged = set(self.damaged_slides())
        parts = pkg.slide_parts()
        best, best_n = None, 0
        for index, part in enumerate(parts):
            if index in damaged:
                continue
            tree = pkg.sp_tree(part)
            n = len(_shape_children(tree)) if tree is not None else 0
            if n > best_n:
                best, best_n = index, n
        return best

    # -- renders ----------------------------------------------------------- #

    def gt_pages(self) -> list[Path]:
        return render_pages(self.gt_path, self.scratch / "render-gt")


# --------------------------------------------------------------------------- #
# expectations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Expect:
    kind: str
    lo: float = 0.0
    hi: float = 0.0
    tol: float = 0.0
    ref: str | None = None

    def label(self) -> str:
        if self.kind == "exact":
            return f"= {self.lo:.3f}"
        if self.kind == "at_most":
            return f"<= {self.hi:.3f}"
        if self.kind == "between":
            return f"{self.lo:.2f}..{self.hi:.2f}"
        return f"<= {self.ref}"

    def check(self, score: float, others: dict[str, float]) -> tuple[bool, str]:
        if self.kind == "exact":
            ok = abs(score - self.lo) <= self.tol
            return ok, "" if ok else f"{score:.3f} != {self.lo:.3f}"
        if self.kind == "at_most":
            ok = score <= self.hi + 1e-9
            return ok, "" if ok else f"{score:.3f} > {self.hi:.3f}"
        if self.kind == "between":
            ok = self.lo - 1e-9 <= score <= self.hi + 1e-9
            return ok, "" if ok else f"{score:.3f} outside {self.lo:.2f}..{self.hi:.2f}"
        if self.kind == "no_gain":
            base = others.get(self.ref)
            if base is None:
                return False, f"reference attack {self.ref} did not produce a score"
            ok = score <= base + 1e-6
            return ok, "" if ok else f"{score:.3f} > {self.ref} {base:.3f}"
        raise ValueError(self.kind)


def Exact(value: float, tol: float = 1e-9) -> Expect:
    return Expect("exact", lo=value, tol=tol)


def AtMost(value: float) -> Expect:
    return Expect("at_most", hi=value)


def Between(lo: float, hi: float) -> Expect:
    return Expect("between", lo=lo, hi=hi)


def NoGain(ref: str) -> Expect:
    return Expect("no_gain", ref=ref)


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #


@dataclass
class Attack:
    name: str
    what: str
    expect: Expect
    build: Callable[[Ctx, Path], "Built"]
    applies: Callable[[Ctx], str | None]
    order: int = 0


@dataclass
class Built:
    path: Path
    evidence: str
    #: machine-readable post-conditions, so a test can assert on what the
    #: attack did rather than on the prose it printed
    facts: dict = field(default_factory=dict)


ATTACKS: dict[str, Attack] = {}
_ORDER = [0]


def attack(name: str, what: str, expect: Expect, applies=lambda ctx: None):
    def wrap(fn):
        _ORDER[0] += 1
        ATTACKS[name] = Attack(name, what, expect, fn, applies, _ORDER[0])
        return fn
    return wrap


def _built(pkg: Pkg, out: Path, evidence: str) -> Built:
    pkg.save(out)
    return Built(out, evidence)


# --------------------------------------------------------------------------- #
# the endpoints
# --------------------------------------------------------------------------- #


@attack("noop", "the untouched broken file", Exact(0.0))
def _noop(ctx: Ctx, out: Path) -> Built:
    shutil.copy2(ctx.input_path, out)
    return Built(out, f"byte-identical to input.pptx "
                      f"({out.stat().st_size} bytes)")


@attack("gt", "the ground truth", Exact(1.0))
def _gt(ctx: Ctx, out: Path) -> Built:
    shutil.copy2(ctx.gt_path, out)
    return Built(out, f"byte-identical to source.pptx "
                      f"({out.stat().st_size} bytes)")


@attack("gt_roundtrip", "the ground truth after a real WPS open-and-save",
        Exact(1.0))
def _gt_roundtrip(ctx: Ctx, out: Path) -> Built:
    """Filled in by `wps_pass`; building it here would serialise the displays."""
    raise Unconstructible("gt_roundtrip is built by the batched WPS pass")


# --------------------------------------------------------------------------- #
# the cheats
# --------------------------------------------------------------------------- #


@attack("screenshot_paste",
        "render the original page and lay it over the damaged page",
        AtMost(0.05))
def _screenshot_paste(ctx: Ctx, out: Path) -> Built:
    pkg = ctx.open_input()
    pages = ctx.gt_pages()
    size = pkg.slide_size()
    parts = pkg.slide_parts()
    covered = []
    for index in ctx.damaged_slides():
        if index >= len(parts) or index >= len(pages):
            continue
        add_picture(pkg, parts[index], pages[index].read_bytes(),
                    (0, 0, size[0], size[1]), f"Slide{index + 1} overlay")
        covered.append(index + 1)
    if not covered:
        raise Unconstructible("no damaged page could be covered")
    pkg.save(out)
    return Built(out, _evidence_cover(out, covered, size),
                 {"pages": covered})


def _evidence_cover(path: Path, pages, size) -> str:
    """Read the produced deck back and confirm the pages really are covered."""
    got = Pkg(path)
    parts = got.slide_parts()
    facts = []
    for page in pages:
        tree = got.sp_tree(parts[page - 1])
        kids = _shape_children(tree)
        top = kids[-1]
        box = _get_box(top)
        full = box and box[0] <= 0 and box[1] <= 0 and \
            box[2] >= size[0] and box[3] >= size[1]
        facts.append(f"p{page}:{'full-bleed' if full else 'NOT-FULL'}"
                     f"@z{len(kids) - 1}")
    return f"{len(pages)} pages covered — " + " ".join(facts[:6])


@attack("clone_spam", "duplicate surviving shapes to fill the holes",
        AtMost(0.05),
        applies=lambda ctx: None if ctx.deletions() else "nothing was deleted")
def _clone_spam(ctx: Ctx, out: Path) -> Built:
    pkg = ctx.open_input()
    parts = pkg.slide_parts()
    filled = 0
    by_slide: dict[int, list[dict]] = {}
    for entry in ctx.deletions():
        by_slide.setdefault(entry["_slide"], []).append(entry)
    for index, holes in by_slide.items():
        if index >= len(parts):
            continue
        root = pkg.xml(parts[index])
        tree = root.find("p:cSld/p:spTree", NS)
        donors = [s for s in _shape_children(tree) if _get_box(s)]
        if not donors:
            continue
        for entry in holes:
            donor = max(donors, key=lambda s: _get_box(s)[2] * _get_box(s)[3])
            clone = copy.deepcopy(donor)
            for node in clone.iter(q("p:cNvPr")):
                node.set("id", str(next_shape_id(tree)))
                node.set("name", f"Clone {next_shape_id(tree)}")
            x, y, cx, cy = entry["box"]
            _set_box(clone, x=x, y=y, cx=cx, cy=cy)
            tree.append(clone)
            filled += 1
        pkg.set_xml(parts[index], root)
    if not filled:
        raise Unconstructible("no hole had a surviving shape to clone")
    got_before = sum(len(_shape_children(ctx.open_input().sp_tree(p)))
                     for p in parts)
    pkg.save(out)
    got_after = sum(len(_shape_children(Pkg(out).sp_tree(p))) for p in parts)
    return Built(out, f"{filled} holes filled with clones; slide shapes "
                      f"{got_before} -> {got_after}",
                 {"filled": filled, "shapes_before": got_before,
                  "shapes_after": got_after})


@attack("native_to_picture",
        "replace a chart / table / SmartArt with a rendered image of it",
        AtMost(0.05),
        applies=lambda ctx: None if ctx.composite_entries()
        else "no chart, table or SmartArt in the damage")
def _native_to_picture(ctx: Ctx, out: Path) -> Built:
    """The cheat is per-component: paste a picture of the composite instead of
    rebuilding it.  Everything else in the deck stays broken, so a comparator
    that insists on a native object has to score this at the floor."""
    pkg = ctx.open_input()
    gt = ctx.open_gt()
    pages = ctx.gt_pages()
    size = pkg.slide_size()
    parts, gt_parts = pkg.slide_parts(), gt.slide_parts()
    done = []
    for entry in ctx.composite_entries():
        index = entry["_slide"]
        if index >= len(parts) or index >= len(pages):
            continue
        box = entry.get("box")
        if not box and entry.get("path") not in (None, "-"):
            shape = resolve_path(gt.sp_tree(gt_parts[index]), entry["path"])
            box = _get_box(shape) if shape is not None else None
        if not box:
            box = _gt_composite_box(gt, gt_parts[index])
        if not box:
            continue
        root = pkg.xml(parts[index])
        tree = root.find("p:cSld/p:spTree", NS)
        removed = None
        if entry.get("path") not in (None, "-"):
            shape = resolve_path(tree, entry["path"])
            if shape is not None and shape.tag == q("p:graphicFrame"):
                removed = shape.get("name") or "graphicFrame"
                shape.getparent().remove(shape)
                pkg.set_xml(parts[index], root)
        else:
            for shape in list(_shape_children(tree)):
                if shape.tag == q("p:graphicFrame") and _get_box(shape) == tuple(box):
                    removed = "graphicFrame"
                    shape.getparent().remove(shape)
            pkg.set_xml(parts[index], root)
        add_picture(pkg, parts[index], crop_png(pages[index], box, size),
                    (box[0], box[1], box[2], box[3]),
                    f"Flattened {entry.get('kind') or entry['op']}")
        done.append(f"p{index + 1}:{entry.get('kind') or entry['op']}"
                    f"{'(frame removed)' if removed else ''}")
    if not done:
        raise Unconstructible(
            "the damaged composites gave no box to render a picture into")
    pkg.save(out)
    got = Pkg(out)
    pics = sum(1 for p in got.slide_parts()
               for s in _shape_children(got.sp_tree(p))
               if s.tag == q("p:pic") and (s.find(".//p:cNvPr", NS) is not None
                                           and "Flattened" in (s.find(".//p:cNvPr", NS).get("name") or "")))
    return Built(out, f"{pics} native objects replaced by pictures — "
                      + " ".join(done[:6]))


def _gt_composite_box(gt: Pkg, part: str):
    for shape in _shape_children(gt.sp_tree(part)):
        if shape.tag == q("p:graphicFrame"):
            return _get_box(shape)
    return None


@attack("page_delete", "delete the damaged page outright", AtMost(0.05))
def _page_delete(ctx: Ctx, out: Path) -> Built:
    pkg = ctx.open_input()
    parts = pkg.slide_parts()
    doomed = {parts[i] for i in ctx.damaged_slides() if i < len(parts)}
    if not doomed:
        raise Unconstructible("no damaged page to delete")
    pres = "ppt/presentation.xml"
    rels = pkg.rels(pres)
    by_id = {rel["id"]: _resolve(pres, rel["target"]) for rel in rels}
    root = pkg.xml(pres)
    lst = root.find("p:sldIdLst", NS)
    for node in list(lst):
        if by_id.get(node.get(q("r:id")) or "") in doomed:
            lst.remove(node)
    pkg.set_xml(pres, root)
    pkg.set_rels(pres, [r for r in rels if by_id.get(r["id"]) not in doomed])
    for part in doomed:
        pkg.drop(part)
        pkg.drop(_rels_name(part))
        pkg.drop_override(part)
    pkg.save(out)
    got = Pkg(out)
    return Built(out, f"slides {len(parts)} -> {len(got.slide_parts())}, "
                      f"removed pages "
                      f"{[i + 1 for i in ctx.damaged_slides()][:8]}",
                 {"before": len(parts), "after": len(got.slide_parts())})


@attack("rename_only",
        "rename shapes to the ground truth's names, change nothing else",
        AtMost(0.05),
        applies=lambda ctx: None
        if any(e.get("name") for e in ctx.entries()) else "no named damage")
def _rename_only(ctx: Ctx, out: Path) -> Built:
    pkg = ctx.open_input()
    gt = ctx.open_gt()
    parts, gt_parts = pkg.slide_parts(), gt.slide_parts()
    renamed = []
    for index in ctx.damaged_slides():
        if index >= len(parts):
            continue
        wanted = [e["name"] for e in ctx.entries()
                  if e["_slide"] == index and e.get("name")]
        if index < len(gt_parts):
            wanted += [n.get("name") for n in gt.sp_tree(gt_parts[index]).iter(q("p:cNvPr"))
                       if n.get("name")]
        root = pkg.xml(parts[index])
        tree = root.find("p:cSld/p:spTree", NS)
        for shape, name in zip(_shape_children(tree), wanted):
            node = shape.find(".//p:cNvPr", NS)
            if node is not None and node.get("name") != name:
                renamed.append((index + 1, node.get("name"), name))
                node.set("name", name)
        pkg.set_xml(parts[index], root)
    if not renamed:
        raise Unconstructible("no shape could be renamed")
    pkg.save(out)
    sample = ", ".join(f"p{p}: {a!r}->{b!r}" for p, a, b in renamed[:3])
    return Built(out, f"{len(renamed)} shapes renamed, geometry untouched — {sample}",
                 {"renamed": len(renamed)})


def _withheld_media(ctx: Ctx) -> list[str]:
    """Media parts the ground truth has and the broken file does not."""
    gt, broken = Pkg(ctx.gt_path), Pkg(ctx.input_path)
    return [n for n in gt.names() if "/media/" in n and not broken.has(n)]


@attack("orphan_media",
        "copy the withheld media blobs into the package without placing them",
        AtMost(0.05),
        applies=lambda ctx: None if _withheld_media(ctx)
        else "the broken deck already holds every media part — "
             "there is nothing to smuggle back")
def _orphan_media(ctx: Ctx, out: Path) -> Built:
    """`package.media` is what an anti-hacking gate compares against the input.
    Adding the blobs back without drawing anything is the cheapest way to move
    that list, so it has to be worth nothing."""
    pkg = ctx.open_input()
    gt = ctx.open_gt()
    added = []
    for name in gt.names():
        if "/media/" in name and not pkg.has(name):
            pkg.put(name, gt.read(name))
            added.append(name.rsplit("/", 1)[-1])
    for node in gt.xml("[Content_Types].xml").findall("ct:Default", NS):
        pkg.ensure_default(node.get("Extension"), node.get("ContentType"))
    if not added:
        raise Unconstructible("the broken deck already holds every media part")
    pkg.save(out)
    got = Pkg(out)
    return Built(out, f"{len(added)} media parts restored as orphans "
                      f"({', '.join(added[:4])}); slide shapes unchanged "
                      f"({sum(len(_shape_children(got.sp_tree(p))) for p in got.slide_parts())})")


@attack("duplicate_gt_slide",
        "append an untouched copy of the original page, leave the broken one",
        AtMost(0.05))
def _duplicate_gt_slide(ctx: Ctx, out: Path) -> Built:
    """If pages are matched by content rather than by position, the whole task
    is solved with copy-paste and the damaged page never has to be touched."""
    pkg = ctx.open_input()
    gt = ctx.open_gt()
    gt_parts = gt.slide_parts()
    used = {int(m.group(1)) for n in pkg.names()
            if (m := re.match(r"ppt/slides/slide(\d+)\.xml$", n))}
    pres = "ppt/presentation.xml"
    root = pkg.xml(pres)
    lst = root.find("p:sldIdLst", NS)
    ids = [int(n.get("id")) for n in lst.findall("p:sldId", NS)]
    appended = []
    for index in ctx.damaged_slides():
        if index >= len(gt_parts):
            continue
        number = max(used, default=0) + 1
        used.add(number)
        new_part = f"ppt/slides/slide{number}.xml"
        pkg.put(new_part, gt.read(gt_parts[index]))
        pkg.ensure_override(new_part, gt.override_of(gt_parts[index]) or SLIDE_CT)
        src_rels = gt.rels(gt_parts[index])
        pkg.set_rels(new_part, src_rels)
        for rel in src_rels:
            if rel["mode"] == "External":
                continue
            target = _resolve(gt_parts[index], rel["target"])
            for part in sorted(closure(gt, [target])):
                if not pkg.has(part):
                    pkg.put(part, gt.read(part))
                    ctype = gt.override_of(part)
                    if ctype:
                        pkg.ensure_override(part, ctype)
        rid = pkg.add_rel(pres, SLIDE_REL, f"slides/slide{number}.xml")
        node = etree.SubElement(lst, q("p:sldId"))
        ids.append(max(ids, default=255) + 1)
        node.set("id", str(ids[-1]))
        node.set(q("r:id"), rid)
        appended.append(index + 1)
    if not appended:
        raise Unconstructible("no damaged page to duplicate")
    pkg.set_xml(pres, root)
    for ext_node in gt.xml("[Content_Types].xml").findall("ct:Default", NS):
        pkg.ensure_default(ext_node.get("Extension"), ext_node.get("ContentType"))
    pkg.save(out)
    got = Pkg(out)
    return Built(out, f"{len(got.slide_parts())} pages "
                      f"(was {len(pkg.slide_parts()) - len(appended)}); "
                      f"originals of {appended[:6]} appended, broken pages kept")


@attack("wrong_params",
        "restore each damaged thing with a wrong value", AtMost(0.30))
def _wrong_params(ctx: Ctx, out: Path) -> Built:
    """Built off the ground truth, then every graded thing is nudged off its
    correct value: shifted, resized, mis-coloured, re-worded.  Nothing is
    missing, and nothing is right."""
    pkg = ctx.open_gt()
    parts = pkg.slide_parts()
    touched: dict[str, int] = {}
    notes = []
    shift = int(0.75 * EMU_IN)
    for entry in ctx.entries():
        index = entry["_slide"]
        if index >= len(parts):
            continue
        root = pkg.xml(parts[index])
        tree = root.find("p:cSld/p:spTree", NS)
        shape = (resolve_path(tree, entry["path"])
                 if entry.get("path") not in (None, "-") else None)
        hit = False
        op = entry["op"]
        if op == "delete" and shape is not None:
            # a deleted thing comes back with *every* value wrong and only its
            # identity right: it is the right picture, and it is the wrong size,
            # in the wrong place, in the wrong colour, with the wrong words.
            # Anything less and the attack overlaps `clone_spam`, which is the
            # opposite case — the wrong object with the right values.
            box = _get_box(shape)
            if box:
                hit = _set_box(shape, x=box[0] + shift, y=box[1] + shift,
                               cx=int(box[2] * 1.3), cy=int(box[3] * 1.3))
            hit = _repaint_runs(shape, "7F007F", 2000) or hit
            hit = _wrong_fill(shape) or hit
            hit = _retext(shape) or hit
        elif op in ("move", "scatter") and shape is not None:
            box = _get_box(shape)
            if box:
                hit = _set_box(shape, x=box[0] + shift, y=box[1] + shift)
        elif op == "resize" and shape is not None:
            box = _get_box(shape)
            if box:
                hit = _set_box(shape, cx=int(box[2] * 1.3), cy=int(box[3] * 1.3))
        elif op == "set_font" and shape is not None:
            hit = _wrong_run_props(shape, entry.get("params") or {})
        elif op == "outline" and shape is not None:
            hit = _wrong_outline(shape)
        elif op == "clear_table_cells" and shape is not None:
            hit = _wrong_cells(shape, entry.get("cleared") or [])
        elif op == "strip_animation":
            hit = _wrong_animation(root)
        elif op == "smartart_drop_nodes":
            hit = _wrong_diagram(pkg, parts[index],
                                 entry.get("removed_nodes") or [])
        if hit:
            pkg.set_xml(parts[index], root)
            touched[entry["deg"]] = touched.get(entry["deg"], 0) + 1
        else:
            notes.append(f"{entry['deg']}/{op}")
    if not touched:
        raise Unconstructible("no damaged thing could be given a wrong value")
    total = len(ctx.components())
    pkg.save(out)
    miss = f"; not perturbed: {sorted(set(notes))[:4]}" if notes else ""
    return Built(out, f"{sum(touched.values())} values wrong across "
                      f"{len(touched)}/{total} components{miss}")


#: run property -> the attribute `set_font` writes it into.
_RUN_ATTR_OF_PARAM = {"bold": "b", "italic": "i", "underline": "u",
                      "size_pt": "sz"}


def _wrong_run_props(shape, params: dict) -> bool:
    """Give every property the step changed a value **different from this one**.

    `_repaint_runs` recolours and resizes, and that is not the same thing: the
    comparator only looks at the properties the operator named, so recolouring
    a step that set `bold` leaves the graded value untouched and the component
    scores 1.00 inside an attack whose evidence line claims the value is
    wrong.  Two decks paid out that way — deck0009's `b+u` component scored a
    full 1.00 under `wrong_params` — which is an attack reporting a gate it
    never fired.
    """
    wanted = [p for p in params if p in _RUN_ATTR_OF_PARAM or p in ("color", "font")]
    if not wanted:
        wanted = ["color", "size_pt", "bold"]
    hit = False
    for rpr in list(shape.iter(q("a:rPr"))) + list(shape.iter(q("a:endParaRPr"))):
        for param in wanted:
            if param == "color":
                for child in list(rpr):
                    if child.tag.endswith("Fill"):
                        rpr.remove(child)
                fill = etree.Element(q("a:solidFill"))
                clr = etree.SubElement(fill, q("a:srgbClr"))
                clr.set("val", "7F007F")
                rpr.insert(0, fill)
            elif param == "font":
                for latin in rpr.findall("a:latin", NS):
                    rpr.remove(latin)
                latin = etree.SubElement(rpr, q("a:latin"))
                latin.set("typeface", "Wingdings")
            elif param == "size_pt":
                now = rpr.get("sz")
                rpr.set("sz", "4400" if now in (None, "4400") else "1000")
            else:
                attr = _RUN_ATTR_OF_PARAM[param]
                now = (rpr.get(attr) or "").lower()
                if attr == "u":
                    rpr.set("u", "none" if now not in ("", "none") else "sng")
                else:
                    rpr.set(attr, "0" if now in ("1", "true") else "1")
            hit = True
    return hit


def _repaint_runs(shape, rgb: str, size: int) -> bool:
    hit = False
    for rpr in list(shape.iter(q("a:rPr"))) + list(shape.iter(q("a:endParaRPr"))):
        for child in list(rpr):
            if child.tag.endswith("Fill"):
                rpr.remove(child)
        fill = etree.SubElement(rpr, q("a:solidFill"))
        clr = etree.SubElement(fill, q("a:srgbClr"))
        clr.set("val", rgb)
        rpr.insert(0, fill)
        rpr.set("sz", str(size))
        hit = True
    return hit


def _wrong_fill(shape) -> bool:
    holder = shape.find("p:spPr", NS)
    if holder is None:
        return False
    for child in list(holder):
        if child.tag.endswith("Fill"):
            holder.remove(child)
    fill = etree.fromstring(
        f'<a:solidFill xmlns:a="{NS["a"]}"><a:srgbClr val="7F007F"/>'
        f'</a:solidFill>'.encode())
    geom = holder.find("a:prstGeom", NS)
    holder.insert(list(holder).index(geom) + 1 if geom is not None else len(holder),
                  fill)
    return True


def _retext(shape) -> bool:
    hit = False
    for node in shape.iter(q("a:t")):
        if (node.text or "").strip():
            node.text = "WRONG"
            hit = True
    return hit


def _wrong_outline(shape) -> bool:
    holder = shape.find("p:spPr", NS)
    if holder is None:
        return False
    for old in holder.findall("a:ln", NS):
        holder.remove(old)
    line = etree.fromstring(
        f'<a:ln xmlns:a="{NS["a"]}" w="76200"><a:solidFill>'
        f'<a:srgbClr val="FF00FF"/></a:solidFill>'
        f'<a:prstDash val="sysDash"/></a:ln>'.encode())
    geom = holder.find("a:prstGeom", NS)
    holder.insert(list(holder).index(geom) + 1 if geom is not None else len(holder),
                  line)
    return True


def _wrong_cells(shape, cleared) -> bool:
    tbl = shape.find("a:graphic/a:graphicData/a:tbl", NS)
    if tbl is None:
        return False
    rows = tbl.findall("a:tr", NS)
    hit = False
    for cell in cleared:
        r, c = cell.get("at", [0, 0])
        if r >= len(rows):
            continue
        cells = rows[r].findall("a:tc", NS)
        if c >= len(cells):
            continue
        for node in cells[c].iter(q("a:t")):
            node.text = "?"
            hit = True
    return hit


def _wrong_animation(root) -> bool:
    """Every effect fires as something else than it fires as now.

    `presetID="1"` was hard-coded here, and preset 1 (`appear`) is what a deck
    built in PowerPoint's default entrance already uses: on deck0002 every one
    of the 18 effects was already `('entr', '1', '0')`, so the "wrong" value
    was the right one and three `strip_animation` components scored 1.00 in an
    attack that reported them as perturbed.  A wrong value has to be wrong
    *relative to what is there*.
    """
    hit = False
    for node in root.iter():
        if node.get("presetID") is None:
            continue
        node.set("presetID", "22" if node.get("presetID") != "22" else "1")
        node.set("presetSubtype",
                 "16" if node.get("presetSubtype") != "16" else "0")
        hit = True
    return hit


def _wrong_diagram(pkg: Pkg, slide_part: str, nodes) -> bool:
    ids = {n.get("modelId") for n in nodes}
    hit = False
    for target in pkg.targets(slide_part):
        if not re.match(r"ppt/diagrams/data\d+\.xml$", target) or not pkg.has(target):
            continue
        root = pkg.xml(target)
        for pt in root.iter(q("dgm:pt")):
            if pt.get("modelId") in ids:
                for node in pt.iter(q("a:t")):
                    node.text = "WRONG"
                    hit = True
        if hit:
            pkg.set_xml(target, root)
    return hit


@attack("damage_untouched", "break a page nobody was asked to touch",
        NoGain("noop"))
def _damage_untouched(ctx: Ctx, out: Path) -> Built:
    return _wreck_a_bystander(ctx, out, ctx.open_input(), "input.pptx")


@attack("damage_untouched_gt",
        "the ground truth with an unrelated page broken", NoGain("gt"))
def _damage_untouched_gt(ctx: Ctx, out: Path) -> Built:
    return _wreck_a_bystander(ctx, out, ctx.open_gt(), "source.pptx")


def _wreck_a_bystander(ctx: Ctx, out: Path, pkg: Pkg, base: str) -> Built:
    index = ctx.untouched_slide(pkg)
    if index is None:
        raise Unconstructible("every page of this deck is part of the task")
    part = pkg.slide_parts()[index]
    root = pkg.xml(part)
    tree = root.find("p:cSld/p:spTree", NS)
    kids = _shape_children(tree)
    before = len(kids)
    for shape in kids[len(kids) // 2:]:
        shape.getparent().remove(shape)
    for shape in _shape_children(tree):
        box = _get_box(shape)
        if box:
            _set_box(shape, x=box[0] + EMU_IN, y=box[1] + EMU_IN)
    pkg.set_xml(part, root)
    pkg.save(out)
    got = Pkg(out)
    after = len(_shape_children(got.sp_tree(got.slide_parts()[index])))
    return Built(out, f"{base}: page {index + 1} (never in the task) "
                      f"{before} -> {after} shapes, survivors moved 1in",
                 {"page": index, "before": before, "after": after})


@attack("half_restore", "restore half the reward mass", Between(0.35, 0.65))
def _half_restore(ctx: Ctx, out: Path) -> Built:
    """Not an attack — the monotonicity check.

    "Half the components" means half the *weight*, not half the count: the plan
    weights a degradation by its estimated steps, so three cheap components and
    one expensive one are not two halves.  Where the plan is not available the
    count is the fallback.

    Components that share a page are restored or withheld together, because a
    page-level restore cannot separate them; the subset is then chosen to land
    as close to half as that constraint allows.
    """
    comps = ctx.component_slides()
    units = _merge_by_slide(comps)
    if len(units) < 2:
        raise Unconstructible(
            f"{len(comps)} components collapse into {len(units)} page-disjoint "
            f"unit(s): there is no half to restore")
    weights = ctx.weights if sum(ctx.weights.values()) > 0 else {}
    if not weights:
        # a plan that weights everything at zero (or no plan at all) is not a
        # reason to restore nothing — that would silently turn the
        # monotonicity check into a second `noop`.
        weights = {deg: 1.0 / len(comps) for deg in comps}
    best = None
    for mask in range(1, (1 << len(units)) - 1):
        picked = [units[i] for i in range(len(units)) if mask >> i & 1]
        mass = sum(weights.get(d, 0.0) for u in picked for d in u["degs"])
        cost = abs(mass - 0.5)
        if best is None or (cost, len(picked)) < (best[0], len(best[1])):
            best = (cost, picked, mass)
    _, picked, mass = best
    restored = sorted(d for u in picked for d in u["degs"])
    slides = sorted({s for u in picked for s in u["slides"]})
    pkg = ctx.open_input()
    gt = ctx.open_gt()
    copy_slides(pkg, gt, slides)
    pkg.save(out)
    same = _pages_equal(out, ctx.gt_path, slides)
    return Built(out, f"restored {restored} = {mass:.2f} of the reward mass "
                      f"on pages {[s + 1 for s in slides]}; "
                      f"{same}/{len(slides)} pages now byte-equal to the gt",
                 {"restored": restored, "mass": mass,
                  "pages": [s + 1 for s in slides], "exact": same})


def _merge_by_slide(comps: dict[str, set[int]]) -> list[dict]:
    units: list[dict] = []
    for deg, slides in comps.items():
        joined = [u for u in units if u["slides"] & slides]
        merged = {"degs": {deg}, "slides": set(slides)}
        for unit in joined:
            merged["degs"] |= unit["degs"]
            merged["slides"] |= unit["slides"]
            units.remove(unit)
        units.append(merged)
    return sorted(units, key=lambda u: sorted(u["degs"]))


def _pages_equal(candidate: Path, gt: Path, indices) -> int:
    a, b = Pkg(candidate), Pkg(gt)
    pa, pb = a.slide_parts(), b.slide_parts()
    return sum(1 for i in indices
               if i < len(pa) and i < len(pb) and a.read(pa[i]) == b.read(pb[i]))


# --------------------------------------------------------------------------- #
# WPS pass
# --------------------------------------------------------------------------- #


def wps_pass(jobs: list[tuple[Ctx, Path]], workers: int = 3) -> dict[str, Built]:
    """Round-trip every deck's ground truth through a real WPS open-and-save.

    `wps_roundtrip.batch` runs the same shape but yields a comparison report and
    throws the saved file away, and the saved file is exactly what has to be
    scored here — so this claims displays from the same `POOL`, which is what
    makes the concurrency safe.
    """
    from . import wps_roundtrip as wps

    missing = wps.preflight()
    if missing:
        return {ctx.name: Built(out, "WPS unavailable: " + "; ".join(missing))
                for ctx, out in jobs}

    results: dict[str, Built] = {}
    errors: dict[str, str] = {}

    def one(job):
        ctx, out = job
        try:
            saved = wps.roundtrip_wps(str(ctx.gt_path), wait=3600)
            shutil.copy2(saved, out)
            shutil.rmtree(Path(saved).parent, ignore_errors=True)
            before = ctx.gt_path.stat().st_size
            results[ctx.name] = Built(
                out, f"WPS re-serialised the package "
                     f"({before} -> {out.stat().st_size} bytes, "
                     f"{_parts_differing(ctx.gt_path, out)} parts differ)")
        except Exception as error:                              # noqa: BLE001
            errors[ctx.name] = f"{type(error).__name__}: {error}"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, jobs))
    for name, message in errors.items():
        results[name] = Built(Path("/dev/null"), "FAILED " + message)
    return results


def _parts_differing(a: Path, b: Path) -> int:
    pa, pb = Pkg(a), Pkg(b)
    names = set(pa.names()) | set(pb.names())
    return sum(1 for n in names
               if (pa.read(n) if pa.has(n) else None)
               != (pb.read(n) if pb.has(n) else None))


# --------------------------------------------------------------------------- #
# the comparator bridge
# --------------------------------------------------------------------------- #


class Scorer:
    """Whatever `pptxgym.comparators` turns out to call its plan builder.

    The comparator is written by somebody else against an agreed `score(plan,
    candidate_inv, gt_inv, init_inv)`; the function that produces `plan` was
    never named in that agreement.  Guessing it here is cheaper than blocking,
    and naming every candidate makes the failure legible when none of them fit.
    """

    PLAN_BUILDERS = ("build_plan", "make_plan", "plan_from_delta", "plan_for",
                     "plan_for_deck", "load_plan", "plan")

    def __init__(self, module=None):
        if module is None:
            try:
                from . import comparators as module      # type: ignore
            except ImportError as error:
                raise ScorerUnavailable(
                    f"pptxgym.comparators is not importable: {error}")
        self.module = module
        # hashed at import, not at report time: the comparator is being edited
        # while this runs, and a report that names the version on disk when it
        # finished is naming a version it did not judge.
        source = getattr(module, "__file__", None)
        self.sha = (hashlib.sha256(Path(source).read_bytes()).hexdigest()[:12]
                    if source and Path(source).exists() else "?")
        if not hasattr(module, "score"):
            raise ScorerUnavailable("pptxgym.comparators has no score()")
        name = os.environ.get("PPTXGYM_PLAN_BUILDER")
        names = (name,) if name else self.PLAN_BUILDERS
        self.builder = next((getattr(module, n) for n in names
                             if callable(getattr(module, n, None))), None)
        if self.builder is None:
            raise ScorerUnavailable(
                "pptxgym.comparators exposes none of "
                f"{list(names)} — set PPTXGYM_PLAN_BUILDER to the right name")

    def plan(self, ctx: Ctx):
        """Call the plan builder however it wants to be called.

        `write=False` where the builder accepts it: the battery reads `work/`
        and must not leave anything in it.  A plan written into the deck by a
        red-teaming run is indistinguishable, afterwards, from the plan the
        pipeline produced.
        """
        kwargs = {}
        try:
            if "write" in inspect.signature(self.builder).parameters:
                kwargs["write"] = False
        except (TypeError, ValueError):                          # builtins
            pass
        attempts = (
            (ctx.deck,), (str(ctx.deck),), (ctx.delta,),
            (ctx.delta, ctx.recipe), (str(ctx.gt_path), str(ctx.input_path)),
        )
        last = None
        for args in attempts:
            try:
                return self.builder(*args, **kwargs)
            except TypeError as error:
                if "argument" not in str(error) and "positional" not in str(error):
                    raise
                last = error
        raise ScorerUnavailable(
            f"could not call {self.builder.__name__}: {last}")

    def score(self, plan, candidate, gt, init) -> dict:
        return self.module.score(plan, candidate, gt, init)

    def signature(self) -> str:
        """Which comparator this was, byte for byte.

        `comparators.py` is being written while this runs; a battery report
        that does not say which version it judged is a claim nobody can check.
        """
        return (f"{self.module.__name__}.score"
                f"{inspect.signature(self.module.score)} / plan via "
                f"{self.builder.__name__} / sha256:{self.sha}")


# --------------------------------------------------------------------------- #
# the battery
# --------------------------------------------------------------------------- #


@dataclass
class Row:
    attack: str
    what: str
    expect: str
    status: str            # scored | n/a | unconstructible | error
    score: float | None = None
    ok: bool | None = None
    note: str = ""
    evidence: str = ""
    #: the components that earned the most, for a row that should have earned
    #: nothing — "which term paid out" is the only actionable half of a failure
    detail: list = field(default_factory=list)


@dataclass
class Report:
    deck: str
    components: list[str]
    rows: list[Row]
    plan_rejected: list[str] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return bool(self.reasons)

    @property
    def reasons(self) -> list[str]:
        out = [f"the comparator rejects the plan: {why}"
               for why in self.plan_rejected]
        for row in self.rows:
            if row.status == "unconstructible":
                out.append(f"{row.attack}: unproven gate — {row.note}")
            elif row.status == "error":
                out.append(f"{row.attack}: {row.note}")
            elif row.status == "scored" and row.ok is False:
                out.append(f"{row.attack}: {row.note}")
        return out


def build_all(ctx: Ctx, outdir: Path, names=None) -> dict[str, Row | Built]:
    """Construct every applicable candidate deck for one task."""
    outdir.mkdir(parents=True, exist_ok=True)
    built: dict[str, Any] = {}
    for name in (names or list(ATTACKS)):
        atk = ATTACKS[name]
        if name == "gt_roundtrip":
            continue
        reason = atk.applies(ctx)
        if reason:
            built[name] = Row(name, atk.what, atk.expect.label(), "n/a",
                              note=reason)
            continue
        target = outdir / f"{name}.pptx"
        try:
            built[name] = atk.build(ctx, target)
        except Unconstructible as error:
            built[name] = Row(name, atk.what, atk.expect.label(),
                              "unconstructible", note=str(error))
        except Exception as error:                              # noqa: BLE001
            built[name] = Row(name, atk.what, atk.expect.label(), "error",
                              note=f"{type(error).__name__}: {error}")
    return built


def score_all(ctx: Ctx, built: dict, scorer: Scorer, plan=None) -> Report:
    """Score every candidate against one plan.

    A plan the comparator has already rejected fails `plan_accepted` first and
    every candidate then scores 0.0 — including the ground truth.  That is the
    right verdict for the task and the wrong reading of the battery: it would
    report thirteen attacks as "passed" when not one of them was exercised.  So
    the rejection is recorded as the deck's first rejection reason, and the
    table is then produced with that one gate stood down, so the attacks say
    something about the comparator underneath it.
    """
    plan = scorer.plan(ctx) if plan is None else plan
    refused = list(plan.get("rejected") or ())
    if refused:
        plan = {**plan, "rejected": []}
    gt_inv = inv.inventory_pptx(ctx.gt_path)
    init_inv = inv.inventory_pptx(ctx.input_path)
    scores: dict[str, float] = {}
    rows: list[Row] = []
    order = sorted(ATTACKS.values(), key=lambda a: a.order)
    # two passes: `no_gain` expectations reference another attack's score
    results: dict[str, Row] = {}
    for atk in order:
        item = built.get(atk.name)
        if item is None:
            continue
        if isinstance(item, Row):
            results[atk.name] = item
            continue
        row = Row(atk.name, atk.what, atk.expect.label(), "scored",
                  evidence=item.evidence)
        if item.evidence.startswith(("FAILED", "WPS unavailable")):
            row.status = "unconstructible"
            row.note = item.evidence
            results[atk.name] = row
            continue
        try:
            cand = inv.inventory_pptx(item.path)
            out = scorer.score(plan, cand, gt_inv, init_inv)
            row.score = float(out["score"])
            scores[atk.name] = row.score
            row.detail = sorted(out.get("components") or [],
                                key=lambda c: -c["weight"] * c["score"])[:2]
            gate = out.get("failed_gate")
            if gate:
                # the gate's *reason* is the actionable half: "slide 5 is not
                # the page it was" sends somebody to a page, "slide_count_and_
                # order" sends them to a function.
                why = (out.get("gate_reasons") or {}).get(gate, "")
                row.evidence += f" | gate:{gate}" + (f" — {why}" if why else "")
        except Exception as error:                              # noqa: BLE001
            row.status = "error"
            row.note = f"{type(error).__name__}: {error}"
        results[atk.name] = row
    for atk in order:
        row = results.get(atk.name)
        if row is not None and row.status == "scored":
            row.ok, row.note = atk.expect.check(row.score, scores)
            if row.ok is False and row.detail:
                row.evidence += " | paid out: " + "; ".join(
                    f"{c.get('deg') or c['id']}/{c['op']} {c['score']:.2f}"
                    f"×{c['weight']:.2f} ({c['why'][:70]})"
                    for c in row.detail)
    return Report(ctx.name, list(ctx.components()),
                  [results[a.name] for a in order if a.name in results],
                  plan_rejected=refused)


def run(decks, outdir: Path, scorer: Scorer | None = None, names=None,
        workers: int = 4, wps_workers: int = 3, wps: bool = True) -> list[Report]:
    """Build every candidate for every deck, then score them all."""
    outdir = Path(outdir)
    ctxs = [Ctx.load(d, outdir / Path(d).name / "scratch") for d in decks]
    built: dict[str, dict] = {}
    plans: dict[str, Any] = {}

    # the plan is built before the candidates, not after: `half_restore` has to
    # know what half the reward is worth before it decides what to restore.
    if scorer is not None:
        for ctx in ctxs:
            plans[ctx.name] = scorer.plan(ctx)
            ctx.weights = {d["id"]: float(d["weight"])
                           for d in plans[ctx.name].get("degradations") or []
                           if d.get("id")}

    def one(ctx: Ctx):
        built[ctx.name] = build_all(ctx, outdir / ctx.name, names)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, ctxs))

    if wps and (names is None or "gt_roundtrip" in names):
        jobs = [(c, outdir / c.name / "gt_roundtrip.pptx") for c in ctxs]
        for name, item in wps_pass(jobs, workers=wps_workers).items():
            built[name]["gt_roundtrip"] = item
        for ctx in ctxs:
            built[ctx.name].setdefault("gt_roundtrip", Row(
                "gt_roundtrip", ATTACKS["gt_roundtrip"].what,
                ATTACKS["gt_roundtrip"].expect.label(), "unconstructible",
                note="WPS produced no saved file"))

    if scorer is None:
        return [Report(c.name, list(c.components()),
                       [item if isinstance(item, Row) else
                        Row(k, ATTACKS[k].what, ATTACKS[k].expect.label(),
                            "built", evidence=item.evidence)
                        for k, item in sorted(
                            built[c.name].items(),
                            key=lambda kv: ATTACKS[kv[0]].order)])
                for c in ctxs]
    return [score_all(c, built[c.name], scorer, plans.get(c.name)) for c in ctxs]


# --------------------------------------------------------------------------- #
# the table a person reads
# --------------------------------------------------------------------------- #


def _mark(row: Row) -> str:
    if row.status == "n/a":
        return "n/a"
    if row.status in ("unconstructible", "error"):
        return "REJECT (unproven)"
    if row.status == "built":
        return "built"
    return "pass" if row.ok else "**FAIL**"


def table(report: Report) -> str:
    lines = [f"### {report.deck} — {len(report.components)} degradations "
             f"({', '.join(report.components)})", ""]
    if report.plan_rejected:
        lines += ["**The comparator rejects this task's plan outright** — "
                  "every candidate below, the ground truth included, scores "
                  "0.0 through the `plan_accepted` gate.  The table is "
                  "therefore produced with that one gate stood down, so the "
                  "attacks are actually exercised; the deck is rejected either "
                  "way.", ""]
        lines += [f"- {why}" for why in report.plan_rejected] + [""]
    lines += ["| attack | what it does | expect | score | verdict | evidence |",
              "|---|---|---|---|---|---|"]
    for row in report.rows:
        score = "—" if row.score is None else f"{row.score:.3f}"
        detail = row.evidence or row.note
        if row.note and row.evidence and row.status == "scored" and row.ok is False:
            detail = f"{row.note} — {row.evidence}"
        lines.append(f"| `{row.attack}` | {row.what} | {row.expect} | {score} "
                     f"| {_mark(row)} | {detail} |")
    lines.append("")
    if report.rejected:
        lines.append(f"**verdict: REJECT** — " + "; ".join(report.reasons))
    else:
        lines.append("**verdict: survives the battery**")
    lines.append("")
    return "\n".join(lines)


def summary(reports: list[Report]) -> str:
    kept = [r for r in reports if not r.rejected]
    counts: dict[str, int] = {}
    for report in reports:
        for row in report.rows:
            if row.status in ("unconstructible", "error") or row.ok is False:
                counts[row.attack] = counts.get(row.attack, 0) + 1
    lines = [f"{len(kept)}/{len(reports)} decks survive the battery.", "",
             "| attack | decks it rejects |", "|---|---|"]
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{name}` | {count} |")
    if not counts:
        lines.append("| — | 0 |")
    return "\n".join(lines) + "\n"


def main(argv=None):                                            # pragma: no cover
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("decks", nargs="+")
    ap.add_argument("-o", "--out", help="write the markdown report here")
    ap.add_argument("--outdir", help="where candidate decks go (a temp dir)")
    ap.add_argument("--only", help="comma-separated attack names")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--wps-workers", type=int, default=3)
    ap.add_argument("--no-wps", action="store_true")
    ap.add_argument("--no-score", action="store_true",
                    help="build the candidates and stop")
    args = ap.parse_args(argv)

    outdir = Path(args.outdir or tempfile.mkdtemp(prefix="pptxgym-attacks-"))
    names = args.only.split(",") if args.only else None
    scorer = None
    if not args.no_score:
        scorer = Scorer()
    started = time.time()
    reports = run(args.decks, outdir, scorer, names,
                  workers=args.workers, wps_workers=args.wps_workers,
                  wps=not args.no_wps)
    body = "\n".join([summary(reports), ""] + [table(r) for r in reports])
    header = (f"# attack battery\n\n"
              f"candidates in `{outdir}`; {len(reports)} decks in "
              f"{time.time() - started:.0f}s"
              + (f"; comparator: {scorer.signature()}" if scorer else
                 "; not scored")
              + "\n\n")
    if args.out:
        Path(args.out).write_text(header + body)
        print(f"-> {args.out}")
    print(header + body)
    return 1 if any(r.rejected for r in reports) else 0


if __name__ == "__main__":                                      # pragma: no cover
    raise SystemExit(main())
