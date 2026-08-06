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

#: How much of the reward the *broken* file is allowed to already satisfy,
#: summed over the plan's components before the floor is subtracted.
#:
#: `noop`'s recorded `= 0.000` is an algebraic identity: `score()` measures the
#: floor from `init_inv` on every call and the `noop` candidate *is* `init_inv`,
#: so `(raw - floor) / (1 - floor)` is zero by construction and the row cannot
#: go red however the comparator is changed.  The number that can go red is the
#: one underneath it — the credit the broken file collects *before* the floor
#: absorbs it.  `comparators.build_plan` tolerates that up to its own
#: `FLOOR_LIMIT` (0.15 per component); this battery is deliberately stricter,
#: because a component the broken file already partly satisfies is exactly a
#: place where doing nothing pays.  Measured 0.000 on 262 components across all
#: ten decks, so 0.0 costs nothing today.  **This is the knob to turn** if a
#: real deck ever trips it — not `noop`'s expectation, which then stops meaning
#: anything again.
ALREADY_RIGHT_LIMIT = 0.0

#: The least a solver may be charged for wrecking a page the task never
#: mentions, measured against `gt`.
#:
#: `damage_untouched_gt` used to declare `NoGain("gt")`, which passes at 1.000:
#: the 0.100 it actually scores comes entirely from
#: `comparators.SCOPE_RATES["untouched_pages_unchanged"]`, a policy constant the
#: battery never asserted was non-zero.  Set the rate to 0 and every deck still
#: passed.  0.05 rather than the 0.10 the policy currently yields, so re-tuning
#: the rate anywhere in 0.05..1.0 is a policy decision and not a battery
#: failure — but a rate of zero is a battery failure, which is the claim.
COLLATERAL_MIN_COST = 0.05


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


def _own_xfrm(shape, entry=None):
    """The shape's transform, **written out** if it only inherits one.

    A placeholder states no `a:xfrm` and takes its position and size from the
    layout. `_get_box` answers `None` for one, so `_perturb_move` and
    `_perturb_resize` returned `False` and `wrong_params` recorded "the branch
    for this operator changed nothing" — a gate it could not fire, on a deck
    whose only fault was using placeholders. deck0003, a one-slide poster, was
    rejected for exactly this after the missing-`text_runs` branch was fixed:
    the next thing under it was a branch that existed and could not act.

    Stating a transform is itself the wrong value here — a solver who moved the
    shape by hand states one too — so writing it is faithful, not a workaround.
    The base comes from the delta's record of where the ground truth had it
    when there is one, so the wrong value stays wrong relative to the right one
    rather than to a constant.
    """
    xfrm = _xfrm_of(shape)
    if xfrm is not None:
        return xfrm
    # `find(...) or find(...)` is wrong on lxml, and it warns why: an element
    # with no children is falsy, so the empty `p:spPr` a placeholder stating
    # nothing carries would fall through to `p:grpSpPr`, find nothing, and give
    # up on precisely the shape this function exists for.
    holder = shape.find("p:spPr", NS)
    if holder is None:
        holder = shape.find("p:grpSpPr", NS)
    if holder is None:
        return None
    box = (entry or {}).get("box") or (0, 0, 914400, 914400)
    xfrm = etree.fromstring(
        f'<a:xfrm xmlns:a="{NS["a"]}">'
        f'<a:off x="{int(box[0])}" y="{int(box[1])}"/>'
        f'<a:ext cx="{max(1, int(box[2]))}" cy="{max(1, int(box[3]))}"/>'
        f'</a:xfrm>'.encode())
    holder.insert(0, xfrm)          # a:xfrm comes first inside spPr
    return xfrm


def _set_box(shape, x=None, y=None, cx=None, cy=None, entry=None) -> bool:
    xfrm = _own_xfrm(shape, entry)
    if xfrm is None:
        return False
    off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
    hit = False
    if off is not None:
        if x is not None:
            off.set("x", str(int(x)))
            hit = True
        if y is not None:
            off.set("y", str(int(y)))
            hit = True
    if ext is not None:
        if cx is not None:
            ext.set("cx", str(max(1, int(cx))))
            hit = True
        if cy is not None:
            ext.set("cy", str(max(1, int(cy))))
            hit = True
    # `True` unconditionally was a second silent no-op: an `a:xfrm` carrying
    # neither `a:off` nor `a:ext` reported a successful perturbation having
    # changed nothing at all.
    return hit


def _get_box(shape, entry=None):
    """Where the shape is, writing out an inherited transform if need be.

    `entry` is passed by the perturbations, which are about to move the shape
    and so need it to have a transform of its own. Read-only callers leave it
    off and still get `None` for a placeholder, which is the honest answer to
    "where does this shape say it is".
    """
    xfrm = _own_xfrm(shape, entry) if entry is not None else _xfrm_of(shape)
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
    #: the plan's scored components, filled in from the plan when one is
    #: available.  `wrong_params` needs it to tell a delta entry that carries
    #: reward from one the plan dropped as unscoreable: failing to perturb the
    #: first pays the attacker, failing to perturb the second costs nothing.
    plan_components: list[dict] = field(default_factory=list)

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

    @staticmethod
    def entry_key(entry: dict) -> tuple:
        """How a delta entry is matched to the plan component that scores it.

        `plan["components"][i]["spec"]` *is* the delta entry, so (slide, op,
        path) is an identity rather than a guess; `deg` is deliberately left
        out because a repair can re-letter the degradations without changing
        which shape carries the reward.
        """
        return (int(entry.get("_slide", entry.get("slide", -1))),
                str(entry.get("op")),
                str(entry.get("path", entry.get("gt_path"))))

    def graded_weight(self) -> dict[tuple, float]:
        """Delta-entry key -> the reward the plan hangs on it.

        Empty when no plan was loaded, which every caller has to read as "I do
        not know", never as "nothing is graded".
        """
        out: dict[tuple, float] = {}
        for component in self.plan_components or ():
            spec = component.get("spec") or {}
            key = self.entry_key({
                "_slide": component.get("slide", spec.get("_slide", -1)),
                "op": component.get("op", spec.get("op")),
                "path": spec.get("path", component.get("gt_path")),
            })
            out[key] = out.get(key, 0.0) + float(component.get("weight") or 0.0)
        return out

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
    """What a row asserts — and every kind here has to be able to go red.

    An expectation that cannot fail is not a test, it is a display with a tick
    beside it.  Two of them shipped: `noop`'s `= 0.000` is an identity (see
    `ALREADY_RIGHT_LIMIT`) and `damage_untouched_gt`'s `NoGain("gt")` would have
    passed at 1.000 (see `COLLATERAL_MIN_COST`).  `Costs` and
    `NothingAlreadyRight` are the falsifiable replacements, and both read the
    scored `result` rather than the single number, because the single number is
    where the identity hides.
    """

    kind: str
    lo: float = 0.0
    hi: float = 0.0
    tol: float = 0.0
    ref: str | None = None
    gap: float = 0.0

    def label(self) -> str:
        if self.kind == "exact":
            return f"= {self.lo:.3f}"
        if self.kind == "at_most":
            return f"<= {self.hi:.3f}"
        if self.kind == "between":
            return f"{self.lo:.2f}..{self.hi:.2f}"
        if self.kind == "costs":
            return f"<= {self.ref} − {self.gap:.2f}"
        if self.kind == "nothing_already_right":
            return f"= 0.000, pre-floor <= {self.hi:.3f}"
        return f"<= {self.ref}"

    def check(self, score: float, others: dict[str, float],
              result: dict | None = None) -> tuple[bool, str]:
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
        if self.kind == "costs":
            base = others.get(self.ref)
            if base is None:
                return False, f"reference attack {self.ref} did not produce a score"
            if base < self.gap:
                # not a pass: there is no room below `ref` to lose `gap` in, so
                # this row cannot be judged at all — and a row that cannot be
                # judged reading as a pass is the whole defect being fixed here
                return False, (
                    f"{self.ref} itself scored {base:.3f}, so nothing can cost "
                    f"{self.gap:.2f} against it — this row cannot be judged "
                    f"until {self.ref} is right, and it is not evidence that "
                    f"collateral damage is punished")
            ok = score <= base - self.gap + 1e-6
            return ok, "" if ok else (
                f"{score:.3f} > {self.ref} {base:.3f} − {self.gap:.2f}: this cost "
                f"{base - score:.3f} of the reward and the battery requires at "
                f"least {self.gap:.2f} — the penalty policy it depends on has "
                f"been turned off or turned down to nothing")
        if self.kind == "nothing_already_right":
            return self._nothing_already_right(score, result)
        raise ValueError(self.kind)

    def _nothing_already_right(self, score: float,
                               result: dict | None) -> tuple[bool, str]:
        """`= 0.000` is free; the pre-floor credit is what has to be earned."""
        if abs(score) > 1e-9:
            return False, (f"{score:.3f} != 0.000 — the broken file scores, "
                           f"which the floor subtraction should have made "
                           f"impossible")
        if result is None or result.get("components") is None:
            return False, ("scored 0.000, which is an identity here (the floor "
                           "is measured from this very candidate) — and no "
                           "per-component result came back, so there is nothing "
                           "underneath it to check")
        already = [c for c in result["components"]
                   if float(c.get("raw") or 0.0) > 0.0]
        earned = sum(float(c.get("weight") or 0.0) * float(c.get("raw") or 0.0)
                     for c in already)
        if earned <= self.hi + 1e-9:
            return True, ""
        worst = sorted(already, key=lambda c: -float(c.get("weight") or 0.0)
                       * float(c.get("raw") or 0.0))[:3]
        return False, (
            f"the broken file already satisfies {earned:.3f} of the reward "
            f"before the floor subtracts it (limit {self.hi:.3f}): "
            + "; ".join(f"{c.get('deg') or c.get('id')}/{c.get('op')} "
                        f"raw {float(c.get('raw') or 0):.2f}"
                        f"×{float(c.get('weight') or 0):.2f} "
                        f"({str(c.get('why'))[:60]})" for c in worst)
            + " — doing nothing pays there, and `noop` scoring 0.000 hides it")


def Exact(value: float, tol: float = 1e-9) -> Expect:
    return Expect("exact", lo=value, tol=tol)


def AtMost(value: float) -> Expect:
    return Expect("at_most", hi=value)


def Between(lo: float, hi: float) -> Expect:
    return Expect("between", lo=lo, hi=hi)


def NoGain(ref: str) -> Expect:
    return Expect("no_gain", ref=ref)


def Costs(ref: str, gap: float) -> Expect:
    """`score <= others[ref] - gap`: the candidate must *lose* real reward.

    `NoGain(ref)` is the same claim with `gap = 0`, and at `gap = 0` it passes
    at `ref` exactly — which is how a row asserting "collateral damage is
    punished" shipped without asserting that the punishment was non-zero.
    """
    return Expect("costs", ref=ref, gap=gap)


def NothingAlreadyRight(limit: float = ALREADY_RIGHT_LIMIT) -> Expect:
    """Score 0.000 **and** collect nothing before the floor is subtracted."""
    return Expect("nothing_already_right", hi=limit)


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


@attack("noop", "the untouched broken file", NothingAlreadyRight())
def _noop(ctx: Ctx, out: Path) -> Built:
    """The one row whose headline number is guaranteed before it is measured.

    Its expectation is therefore not the headline number.  See
    `ALREADY_RIGHT_LIMIT`: what is checked is the credit the broken file
    collects *before* the floor subtraction that makes `0.000` inevitable.
    """
    shutil.copy2(ctx.input_path, out)
    return Built(out, f"byte-identical to input.pptx "
                      f"({out.stat().st_size} bytes)",
                 {"kind": "copy", "of": "input.pptx",
                  "bytes": out.stat().st_size})


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


#: `op` -> the function that gives that operator's graded value a *wrong*
#: value.  A registry rather than an `elif` chain so that "which operators does
#: this attack actually cover?" is a set difference a test can compute, instead
#: of something a reader has to reconstruct by eye — which is how `recolor` and
#: `table_drop_rows` came to have no branch at all while the row kept passing.
#:
#: Each returns True only when it changed something the comparator reads.  A
#: branch that returns True having changed nothing graded is worse than no
#: branch, because the coverage gate below then believes it: see
#: `_wrong_run_props` and `_wrong_animation` for the two that did exactly that.
PERTURB: dict[str, Callable[..., bool]] = {}


def perturb(*ops: str):
    def wrap(fn):
        for op in ops:
            PERTURB[op] = fn
        return fn
    return wrap


@perturb("delete", "blank_slide")
def _perturb_delete(shape=None, **kw) -> bool:
    # `blank_slide` shares `_cmp_restored_shape` with `delete` — same question
    # ("is the thing back, and is it itself?"), so the same wrong answer.
    # a deleted thing comes back with *every* value wrong and only its identity
    # right: it is the right picture, and it is the wrong size, in the wrong
    # place, in the wrong colour, with the wrong words.  Anything less and the
    # attack overlaps `clone_spam`, which is the opposite case — the wrong
    # object with the right values.
    if shape is None:
        return False
    shift = int(0.75 * EMU_IN)
    box = _get_box(shape, kw.get("entry") or {})
    hit = bool(box) and _set_box(shape, x=box[0] + shift, y=box[1] + shift,
                                 cx=int(box[2] * 1.3), cy=int(box[3] * 1.3),
                                 entry=kw.get("entry"))
    hit = _repaint_runs(shape, "7F007F", 2000) or hit
    hit = _wrong_fill(shape) or hit
    return _retext(shape) or hit


@perturb("move", "scatter", "swap")
def _perturb_move(shape=None, entry=None, **kw) -> bool:
    # `swap` is graded by `_cmp_position`, the same comparator as `move` and
    # `scatter`: whether the thing is where it belongs. So the wrong value is
    # the same wrong value.
    box = _get_box(shape, entry or {}) if shape is not None else None
    shift = int(0.75 * EMU_IN)
    return bool(box) and _set_box(shape, x=box[0] + shift, y=box[1] + shift,
                                  entry=entry)


@perturb("resize")
def _perturb_resize(shape=None, entry=None, **kw) -> bool:
    box = _get_box(shape, entry or {}) if shape is not None else None
    return bool(box) and _set_box(shape, cx=int(box[2] * 1.3),
                                  cy=int(box[3] * 1.3), entry=entry)


@perturb("set_font")
def _perturb_set_font(shape=None, entry=None, **kw) -> bool:
    return (shape is not None
            and _wrong_run_props(shape, (entry or {}).get("params") or {}))


@perturb("outline")
def _perturb_outline(shape=None, **kw) -> bool:
    return shape is not None and _wrong_outline(shape)


@perturb("recolor")
def _perturb_recolor(shape=None, entry=None, **kw) -> bool:
    """`_cmp_recolor` compares the fill and nothing else.

    So the wrong value is any fill the ground truth's is not — including the
    common case where the ground truth states no fill at all and inherits one
    (all three of deck0004's `recolor` components), where *stating* one is
    already the wrong answer.
    """
    return shape is not None and _wrong_fill(shape, avoid=(entry or {}).get("to"))


@perturb("clear_table_cells")
def _perturb_clear_cells(shape=None, entry=None, **kw) -> bool:
    return (shape is not None
            and _wrong_cells(shape, (entry or {}).get("cleared") or []))


@perturb("table_drop_rows", "table_drop_cols")
def _perturb_table_lines(shape=None, entry=None, **kw) -> bool:
    """`_cmp_drop_rows`/`_cmp_drop_cols` ask whether the dropped lines are back.

    The wrong value is the line present but not saying what it said, which is
    what a solver who retyped the row from memory produces — and the case
    deck0009 shipped without ever testing, because the row was left correct and
    paid its component's full 0.1475.
    """
    axis = "row" if (entry or {}).get("op") == "table_drop_rows" else "col"
    return (shape is not None
            and _wrong_table_lines(shape, (entry or {}).get("removed") or [],
                                   axis))


@perturb("strip_animation", "anim_drop_steps")
def _perturb_animation(root=None, **kw) -> bool:
    # `_cmp_anim_steps` compares `_anim_signature`, which is exactly
    # (class, preset, subtype) per effect — the tuple `_wrong_animation`
    # already moves. One registry entry, not a second implementation.
    return root is not None and _wrong_animation(root)


@perturb("clear_text", "set_text")
def _perturb_text(shape=None, **kw) -> bool:
    # `_cmp_text` is `_facet_text`: the words, and only the words.
    return shape is not None and _retext(shape)


@perturb("text_runs")
def _perturb_text_runs(shape=None, entry=None, **kw) -> bool:
    """`_cmp_text_runs` grades two different things and the spec says which.

    A paragraph recorded `deleted` or `emptied` is scored on its *presence* —
    "the paragraph itself is what went missing, and there is no style term to
    weigh it against" — so the wrong value there is the wrong words. A
    paragraph that was restyled is scored on the run properties the step named
    and on nothing else, because a restyle never touches the text.

    Perturbing the text of a restyle would still fail the comparator, by making
    the paragraph unfindable — but that is `delete`'s wrong answer wearing this
    attack's name, and `wrong_params` means right identity, wrong value. So
    each case gets the wrong value of its own kind.
    """
    if shape is None:
        return False
    spec = entry or {}
    touched = spec.get("touched") or []
    params = spec.get("params") or {}
    gone = any(item.get("action") in ("deleted", "emptied") for item in touched)
    hit = False
    if gone or not params:
        hit = _retext(shape) or hit
    if params:
        hit = _wrong_run_props(shape, params) or hit
    return hit


@perturb("rotate")
def _perturb_rotate(shape=None, **kw) -> bool:
    # `_cmp_rotate` compares `xfrm/@rot` within `ROT_TOL`; a quarter turn is
    # past any tolerance worth having.
    return shape is not None and _wrong_rotation(shape)


@perturb("zorder")
def _perturb_zorder(shape=None, **kw) -> bool:
    # `_cmp_zorder` asks which shapes this one is in front of, so the wrong
    # value is being in front of the other ones.
    return shape is not None and _wrong_z(shape)


@perturb("crop")
def _perturb_crop(shape=None, **kw) -> bool:
    # `_facet_crop` compares `(crop, mode)` off the picture fill.
    return shape is not None and _wrong_crop(shape)


@perturb("ungroup")
def _perturb_ungroup(shape=None, **kw) -> bool:
    # `_cmp_ungroup` weighs the group's existence 2 and its membership 3, so
    # a group that is back with the wrong things in it is the wrong value —
    # and leaving the group itself in place is what keeps this distinct from
    # `delete`.
    return shape is not None and _wrong_membership(shape)


@perturb("detach_connector")
def _perturb_detach(shape=None, **kw) -> bool:
    # `_cmp_detach` weighs which shape each end holds 3, and where the
    # connector sits 1.
    return shape is not None and _wrong_connector(shape)


@perturb("strip_effects")
def _perturb_effects(shape=None, entry=None, **kw) -> bool:
    # `_cmp_strip_effects` splits on what was removed: a `gradFill` is graded
    # as a fill, anything else as `_effect_facts` — the effect list and the
    # style's `effectRef`.
    if shape is None:
        return False
    removed = (entry or {}).get("removed") or []
    hit = False
    if any(name != "gradFill" for name in removed) or not removed:
        hit = _wrong_effects(shape) or hit
    if "gradFill" in removed:
        hit = _wrong_fill(shape) or hit
    return hit


@perturb("strip_transition")
def _perturb_transition(root=None, **kw) -> bool:
    # `_cmp_transition` compares `(type, detail)` on the slide.
    return root is not None and _wrong_transition(root)


@perturb("clear_notes")
def _perturb_notes(pkg=None, part=None, **kw) -> bool:
    # `_cmp_notes` compares the slide's notes text, normalised. The notes live
    # in their own part, so this is the one branch that has to follow a
    # relationship to find what it is grading.
    return _wrong_notes(pkg, part)


@perturb("chart_edit")
def _perturb_chart(pkg=None, part=None, entry=None, **kw) -> bool:
    # `_cmp_chart` matches series **by name**, so renaming them is the wrong
    # value that keeps the chart a chart: the right object, the wrong data.
    return _wrong_chart(pkg, part)


@perturb("smartart_drop_nodes")
def _perturb_smartart(pkg=None, part=None, entry=None, **kw) -> bool:
    return _wrong_diagram(pkg, part, (entry or {}).get("removed_nodes") or [])


@attack("wrong_params",
        "restore each damaged thing with a wrong value", AtMost(0.30))
def _wrong_params(ctx: Ctx, out: Path) -> Built:
    """Built off the ground truth, then every graded thing is nudged off its
    correct value: shifted, resized, mis-coloured, re-worded.  Nothing is
    missing, and nothing is right.

    **An entry this attack cannot perturb is left at its ground-truth value, so
    it scores 1.0 and pays the attacker its full weight.**  That used to be a
    line of prose (`not perturbed: ['d5/recolor']`) that nothing read: deck0004
    scored 0.105 and deck0009 0.142 against a 0.300 bar, which is 12.5% and
    14.8% of those decks' reward never tested by the attack that exists to test
    it, and both shipped.  An attack may not be credited for ground it did not
    touch, so any *graded* entry left correct now raises `Unconstructible` —
    which is a rejection, the same verdict as any other unfired gate.

    Entries the plan dropped as unscoreable carry no weight and are reported
    but do not reject.  With no plan loaded there is no way to tell the two
    apart, so every entry is treated as graded — the conservative direction.
    """
    pkg = ctx.open_gt()
    parts = pkg.slide_parts()
    touched: dict[str, int] = {}
    graded = ctx.graded_weight()
    missed: list[dict] = []
    # Resolve every path before perturbing any of them.
    #
    # Paths are positional, and some branches move shapes: `_wrong_membership`
    # reparents a child out of its group, `_wrong_z` moves an element to the
    # other end of its parent. Resolving inside the loop means every later
    # entry is addressed against a tree an earlier one has already rearranged.
    #
    # deck0003 escalated the exact instance. `Ctx.entries()` sorts by
    # `(slide, str(path))`, so the `ungroup` on path `24` always precedes the
    # five `move` entries on `24/0..24/4`; `_wrong_membership` reparents
    # `members[0]`, `24/4` stops existing, `_perturb_move` returns `False`, and
    # the deck is refused for an unproven gate that our own attack broke.
    #
    # `degrade_exec` learned this and says so — "Index ONCE, before anything
    # runs. Paths are positional, so a delete renumbers every shape after it".
    # The element references stay valid across the rearrangement; only the
    # paths do not, which is precisely why they are resolved first.
    roots: dict[int, object] = {}
    resolved: list[tuple[dict, object]] = []
    for entry in ctx.entries():
        index = entry["_slide"]
        if index >= len(parts):
            continue
        if index not in roots:
            roots[index] = pkg.xml(parts[index])
        tree = roots[index].find("p:cSld/p:spTree", NS)
        resolved.append((entry, resolve_path(tree, entry["path"])
                         if entry.get("path") not in (None, "-") else None))

    for entry, shape in resolved:
        index = entry["_slide"]
        root = roots[index]
        op = entry["op"]
        branch = PERTURB.get(op)
        hit = bool(branch) and branch(shape=shape, entry=entry, root=root,
                                      pkg=pkg, part=parts[index])
        if hit:
            pkg.set_xml(parts[index], root)
            touched[entry["deg"]] = touched.get(entry["deg"], 0) + 1
        else:
            key = ctx.entry_key(entry)
            missed.append({
                "deg": entry["deg"], "op": op, "slide": index,
                "path": str(entry.get("path")),
                "why": "no perturbation branch for this operator" if not branch
                       else "the branch for this operator changed nothing",
                # no plan -> `graded` is empty -> treat every entry as graded
                "weight": graded.get(key, 0.0) if graded else None,
            })
    if not touched:
        raise Unconstructible("no damaged thing could be given a wrong value")
    total = len(ctx.components())
    pkg.save(out)

    untested = [m for m in missed if m["weight"] is None or m["weight"] > 0.0]
    paid = sum(m["weight"] or 0.0 for m in untested)
    facts = {"kind": "wrong_params", "perturbed": sum(touched.values()),
             "components": len(touched), "component_total": total,
             "unperturbed": missed, "untested_weight": round(paid, 6),
             "plan_known": bool(graded),
             "ops_without_a_branch": sorted(
                 {m["op"] for m in missed if "no perturbation branch" in m["why"]})}
    if untested:
        raise Unconstructible(
            "this attack was credited for ground it did not touch: "
            + "; ".join(f"{m['deg']}/{m['op']} on slide {m['slide'] + 1} "
                        f"({m['why']}"
                        + (f", worth {m['weight']:.4f}"
                           if m["weight"] is not None else ", weight unknown")
                        + ")" for m in untested[:4])
            + (f" — {paid:.4f} of the reward is left at its correct value and "
               f"pays out inside a row that reports the task as unhackable"
               if graded else
               " — every one of those components keeps its correct value and "
               "scores 1.0 inside a row that reports the task as unhackable"))
    return Built(out, f"{sum(touched.values())} values wrong across "
                      f"{len(touched)}/{total} components; "
                      f"every graded entry perturbed"
                      + (f" ({len(missed)} unscoreable entr"
                         f"{'y' if len(missed) == 1 else 'ies'} skipped)"
                         if missed else ""),
                 facts)


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
    """Recolour and resize every run to something it is not already.

    `rgb` and `size` are what to move *towards*, not what to force: text
    already wearing them would be repainted to its own correct value and score
    1.00 inside an attack whose evidence says the value is wrong. That is the
    bug `_wrong_fill` and `_wrong_animation` each carry a paragraph about,
    and it was still live here.
    """
    hit = False
    for rpr in list(shape.iter(q("a:rPr"))) + list(shape.iter(q("a:endParaRPr"))):
        worn = {(node.get("val") or "").lstrip("#").upper()
                for node in rpr.iter(q("a:srgbClr"))}
        colour = rgb if rgb.upper() not in worn else next(
            (c for c in _WRONG_FILLS if c not in worn), "123456")
        for child in list(rpr):
            if child.tag.endswith("Fill"):
                rpr.remove(child)
        fill = etree.SubElement(rpr, q("a:solidFill"))
        clr = etree.SubElement(fill, q("a:srgbClr"))
        clr.set("val", colour)
        rpr.insert(0, fill)
        rpr.set("sz", str(size if str(size) != rpr.get("sz") else size * 2))
        hit = True
    return hit


#: two colours far enough apart that whichever one the shape already wears, the
#: other is visibly not it.
_WRONG_FILLS = ("7F007F", "00FF7F")


def _wrong_fill(shape, avoid: str | None = None) -> bool:
    """Repaint the shape a colour it is not wearing.

    `7F007F` was hard-coded, which is the `_wrong_animation` bug waiting to
    happen: a shape whose ground-truth fill is already that colour would be
    "perturbed" to its own correct value and score 1.00 inside an attack whose
    evidence says the value is wrong.  `avoid` additionally takes the colour the
    *degradation* painted it, so the attack cannot accidentally reproduce the
    broken file either.
    """
    holder = shape.find("p:spPr", NS)
    if holder is None:
        return False
    worn = {(node.get("val") or "").lstrip("#").upper()
            for node in holder.iter(q("a:srgbClr"))}
    if avoid:
        worn.add(str(avoid).lstrip("#").upper())
    colour = next((c for c in _WRONG_FILLS if c not in worn), None)
    if colour is None:                       # wearing both: any third will do
        colour = "123456"
    for child in list(holder):
        if child.tag.endswith("Fill"):
            holder.remove(child)
    fill = etree.fromstring(
        f'<a:solidFill xmlns:a="{NS["a"]}"><a:srgbClr val="{colour}"/>'
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


def _wrong_rotation(shape) -> bool:
    """A quarter turn away from wherever it is now.

    Relative, not absolute, for the reason `_wrong_animation` records: a shape
    whose ground truth is already at the hard-coded angle would be "perturbed"
    to its own correct value and score 1.00 inside an attack reporting it as
    wrong.
    """
    xfrm = shape.find("p:spPr/a:xfrm", NS)
    if xfrm is None:
        holder = shape.find("p:spPr", NS)
        if holder is None:
            return False
        xfrm = etree.SubElement(holder, q("a:xfrm"))
    now = int(xfrm.get("rot") or 0)
    xfrm.set("rot", str((now + 5400000) % 21600000))     # +90°, in 1/60000ths
    return True


def _wrong_z(shape) -> bool:
    """Send it to the bottom of the page — or to the top if it is the bottom.

    `_cmp_zorder` scores *which shapes this one is in front of*, restricted to
    the peers the step actually passed. Moving the shape to the far end is not
    automatically wrong: a step recorded `to: "back"` grades the peers that sit
    **below** it in the ground truth, and bringing it to the very front leaves
    every one of those pairs exactly as the ground truth has them, so the
    component would score 1.00 in an attack claiming it was perturbed. The
    front is a wrong value for one direction and the right value for the other.

    Going to the bottom inverts every pair in which this shape is above
    something, and going to the top inverts every pair in which it is below —
    so one of the two inverts the graded set whichever direction the step took,
    and "the end it is not already at" picks the one that moves.
    """
    parent = shape.getparent()
    if parent is None:
        return False
    body = [k for k in parent if not k.tag.endswith("}nvGrpSpPr")
            and not k.tag.endswith("}grpSpPr")]
    if len(body) < 2 or shape not in body:
        return False                       # nothing to be in front of
    first = list(parent).index(body[0])
    parent.remove(shape)
    if body[0] is shape:                   # already at the bottom -> the top
        parent.append(shape)
    else:                                  # -> the bottom
        parent.insert(first, shape)
    return True


def _wrong_crop(shape) -> bool:
    """`_facet_crop` compares `(crop, mode)`, so a different crop is enough.

    The offsets are chosen against what is already there for the usual reason;
    a picture cropped exactly this much would otherwise be handed its own
    answer.
    """
    fill = shape.find("p:blipFill", NS)
    if fill is None:
        return False
    src = fill.find("a:srcRect", NS)
    if src is None:
        blip = fill.find("a:blip", NS)
        src = etree.Element(q("a:srcRect"))
        fill.insert(list(fill).index(blip) + 1 if blip is not None else 0, src)
    now = {side: int(src.get(side) or 0) for side in ("l", "t", "r", "b")}
    for side in ("l", "t"):
        src.set(side, str(20000 if now[side] != 20000 else 5000))
    return True


def _wrong_membership(shape) -> bool:
    """Take one member out of the group and leave it loose on the page.

    `_cmp_ungroup` weighs the group's existence 2 and how many of its members
    belong to it 3. Reparenting one child keeps the group — so this stays the
    wrong *value*, not a deletion — while making the membership wrong. It also
    leaves the shape on the slide, so no scope penalty stands in for the
    component score and confuses what was measured.
    """
    parent = shape.getparent()
    if parent is None:
        return False
    members = [k for k in shape
               if k.tag.endswith("}sp") or k.tag.endswith("}pic")
               or k.tag.endswith("}grpSp") or k.tag.endswith("}graphicFrame")
               or k.tag.endswith("}cxnSp")]
    if not members:
        return False
    shape.remove(members[0])
    parent.append(members[0])
    return True


def _wrong_connector(shape) -> bool:
    """Unhook the ends and move it, which is what a hand-drawn one looks like.

    `_cmp_detach` weighs which shape each end holds 3 and where the connector
    sits 1, so both are moved: an attack that only shifted the box would leave
    three quarters of the component at its correct value.
    """
    hit = False
    props = shape.find("p:nvCxnSpPr/p:cNvCxnSpPr", NS)
    if props is not None:
        for tag in ("a:stCxn", "a:endCxn"):
            for node in props.findall(tag, NS):
                props.remove(node)
                hit = True
    box = _get_box(shape, {})
    if box:
        shift = int(0.75 * EMU_IN)
        hit = _set_box(shape, x=box[0] + shift, y=box[1] + shift) or hit
    return hit


#: effect kind -> its XML, so one that is not already worn can always be
#: chosen.  Keyed by name rather than parsed back out of the string: deriving
#: it with `lstrip("<a:")` reads correctly and strips a *set* of characters, so
#: any effect whose name began with `a` would silently lose it.
_WRONG_EFFECTS = {
    "glow": '<a:glow rad="127000"><a:srgbClr val="7F007F"/></a:glow>',
    "reflection": '<a:reflection blurRad="63500" stA="50000" endPos="50000"/>',
    "softEdge": '<a:softEdge rad="63500"/>',
}


def _wrong_effects(shape) -> bool:
    """Wear an effect the ground truth is not wearing.

    The inventory records effects as a **sorted list of tag names** and nothing
    else — `["outerShdw"]`, not the shadow's blur or colour — so replacing a
    shadow with a differently-parameterised shadow is not a different value.
    The first version of this did exactly that and `test_the_branch_makes_its
    _own_comparator_say_no[strip_effects]` measured it at 1.00: the entry
    reported as perturbed, the component scored full marks, the gate certified
    untested. The same class as `_wrong_fill`'s hard-coded purple and
    `_wrong_animation`'s `presetID="1"`, found this time by measurement rather
    than by a deck failing in production.

    `effectRef` moves too: a shape carrying no explicit `effectLst` is graded
    on the theme reference alone, so changing only the list would leave it
    exactly as it was.
    """
    holder = shape.find("p:spPr", NS)
    if holder is None:
        return False
    worn = set()
    for old in holder.findall("a:effectLst", NS):
        worn |= {child.tag.split("}")[-1] for child in old}
        holder.remove(old)
    xml = next((x for name, x in _WRONG_EFFECTS.items() if name not in worn),
               _WRONG_EFFECTS["glow"])
    holder.append(etree.fromstring(
        f'<a:effectLst xmlns:a="{NS["a"]}">{xml}</a:effectLst>'.encode()))
    ref = shape.find("p:style/a:effectRef", NS)
    if ref is not None:
        now = ref.get("idx") or "0"
        ref.set("idx", "3" if now != "3" else "1")
    return True


#: transitions far enough apart that whichever one the slide has, the other is
#: not it.  Same rule as `_WRONG_FILLS`.
_WRONG_TRANSITIONS = ("wipe", "blinds")


def _wrong_transition(root) -> bool:
    """`_cmp_transition` compares `(type, detail)`, so give it another one.

    Chosen against what the slide already has: a deck built with `wipe`
    throughout would otherwise be "perturbed" to its own transition, and three
    `strip_animation` components once scored 1.00 in an attack that reported
    them as perturbed for exactly that reason.
    """
    for old in root.findall("p:transition", NS):
        worn = {child.tag.split("}")[-1] for child in old}
        root.remove(old)
        kind = next((k for k in _WRONG_TRANSITIONS if k not in worn), "fade")
        break
    else:
        kind = _WRONG_TRANSITIONS[0]
    node = etree.fromstring(
        f'<p:transition xmlns:p="{NS["p"]}" spd="slow">'
        f'<p:{kind}/></p:transition>'.encode())
    # after `p:cSld` and `p:clrMapOvr`, which is where the schema puts it
    after = -1
    for i, child in enumerate(root):
        if child.tag.endswith("}cSld") or child.tag.endswith("}clrMapOvr"):
            after = i
    root.insert(after + 1, node)
    return True


NOTES_REL = ("http://schemas.openxmlformats.org/officeDocument/2006/"
             "relationships/notesSlide")
CHART_REL = ("http://schemas.openxmlformats.org/officeDocument/2006/"
             "relationships/chart")


def _related(pkg, part: str, kind: str) -> list[str]:
    """Every part `part` points at through a relationship of type `kind`."""
    if pkg is None or part is None:
        return []
    out = []
    for rel in pkg.rels(part):
        if rel.get("type") == kind and rel.get("mode") != "External":
            target = _resolve(part, rel["target"])
            if pkg.has(target):
                out.append(target)
    return out


def _wrong_notes(pkg, part) -> bool:
    """Give the speaker notes different words.

    `_cmp_notes` compares the normalised notes text, so this is the same wrong
    value `_retext` gives a shape — applied to the notes part, which is where
    the graded text actually lives.
    """
    hit = False
    for name in _related(pkg, part, NOTES_REL):
        root = pkg.xml(name)
        changed = False
        for node in root.iter(q("a:t")):
            if (node.text or "").strip():
                node.text = "WRONG"
                changed = True
        if changed:
            pkg.set_xml(name, root)
            hit = True
    return hit


def _wrong_chart(pkg, part) -> bool:
    """Rename every series and move every number.

    `_cmp_chart` pairs series **by name** and then compares their points, so a
    renamed series is one the answer cannot find and a moved point is one it
    finds wrong. Both, because a chart step records either `removed_series` or
    edited values and the branch does not get to know which.
    """
    hit = False
    for name in _related(pkg, part, CHART_REL):
        root = pkg.xml(name)
        changed = False
        for node in root.iter(q("c:tx")):
            for text in node.iter(q("c:v")):
                if (text.text or "").strip():
                    text.text = f"WRONG {text.text}"
                    changed = True
        for node in root.iter(q("c:val")):
            for text in node.iter(q("c:v")):
                try:
                    text.text = str(float(text.text) + 137.0)
                except (TypeError, ValueError):
                    continue
                changed = True
        if changed:
            pkg.set_xml(name, root)
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


def _wrong_table_lines(shape, removed, axis: str) -> bool:
    """Retype the dropped rows/columns as something the table never said.

    The comparator matches the dropped lines by their text, so a line that is
    present and says the wrong thing is the wrong value — and it is the
    realistic wrong answer too: a solver who retyped the missing row from
    memory produces exactly this.  Only a line that actually said something is
    counted as perturbed; a blank one is `Unscorable` to the comparator as
    well, so claiming it here would be the same lie in the other direction.
    """
    tbl = shape.find("a:graphic/a:graphicData/a:tbl", NS)
    if tbl is None:
        return False
    rows = tbl.findall("a:tr", NS)
    hit = False
    for item in removed:
        index = item.get(axis)
        if not isinstance(index, int):
            continue
        cells = []
        if axis == "row":
            if 0 <= index < len(rows):
                cells = rows[index].findall("a:tc", NS)
        else:
            for row in rows:
                got = row.findall("a:tc", NS)
                if 0 <= index < len(got):
                    cells.append(got[index])
        for cell in cells:
            for node in cell.iter(q("a:t")):
                if (node.text or "").strip():
                    node.text = "WRONG"
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
        "the ground truth with an unrelated page broken",
        Costs("gt", COLLATERAL_MIN_COST))
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
# the other direction: work that deserves credit
# --------------------------------------------------------------------------- #
#
# Everything above asks *can this be cheated?* and nothing above asks the
# opposite.  That is the failure that actually happened: a model was run on the
# previous batch of these tasks and **three of four recorded 0.0 while having
# done 43%, 53% and 63% of the work** — a gate firing on a legitimate solution,
# a rubric demanding an XML state no user interface can produce, and components
# comparing a group-child's coordinates against slide-level EMU.  Fourteen
# attacks, all passing, said nothing about any of it.
#
# This is a **separate class, not more rows in the same table**, because the two
# have opposite defaults:
#
#   an attack        prefers to lose a task rather than ship a hackable one
#   a variant        prefers to lose a hack rather than ship a task that
#                    punishes correct work
#
# Mixing them dilutes both — one shared threshold would have to be lenient
# enough for the attacks and strict enough for the variants at the same time.
#
# A variant is the answer **reached by another route**: the same slide, made
# the way an application makes it rather than the way the recipe unmade it.  It
# must score within `VARIANT_TOL` of `gt` and **no hard gate may fire on it**.
# A gate that fires here rejects the task exactly as a successful attack does;
# a score that drops is a defect in a comparator, not a threshold to widen.
# When an attack and a variant cannot be separated, that is a finding about the
# comparator — never a number to split the difference with.

#: how far below `gt` a variant may land.  Not a tolerance the comparator is
#: allowed to spend: it exists because a variant is built by rewriting XML and
#: a deck of 98 components can round differently in the last place.
VARIANT_TOL = 0.02


@dataclass
class LegitimateVariant:
    name: str
    what: str
    build: Callable[["Ctx", Path], "Built"]
    applies: Callable[["Ctx"], str | None]
    order: int = 0


LEGITIMATE_VARIANTS: dict[str, LegitimateVariant] = {}
_VORDER = [0]


def legitimate_variant(name: str, what: str, applies=lambda ctx: None):
    def wrap(fn):
        _VORDER[0] += 1
        LEGITIMATE_VARIANTS[name] = LegitimateVariant(name, what, fn, applies,
                                                      _VORDER[0])
        return fn
    return wrap


def _damaged_paths(ctx: "Ctx") -> dict[int, list[str]]:
    """slide index -> the shape paths the recipe damaged, deepest last."""
    out: dict[int, list[str]] = {}
    for entry in ctx.entries():
        path = entry.get("path")
        if path in (None, "-"):
            continue
        out.setdefault(entry["_slide"], [])
        if path not in out[entry["_slide"]]:
            out[entry["_slide"]].append(str(path))
    return out


def _top_level(paths) -> list[str]:
    return [p for p in paths if "/" not in p]


def _rename(shape, sid: int, label: str) -> None:
    """Give a rebuilt shape the identity an application would give it.

    A shape drawn again through the GUI is a *new* shape: new id, stock name.
    Anything that pairs it with the original through its name is not measuring
    the work, and `rename_only` is the attack that proves a name is free.
    """
    node = shape.find(".//p:cNvPr", NS)
    if node is not None:
        node.set("id", str(sid))
        node.set("name", f"{label} {sid}")


def _union(boxes) -> tuple[int, int, int, int]:
    xs = [b[0] for b in boxes]
    ys = [b[1] for b in boxes]
    x2 = [b[0] + b[2] for b in boxes]
    y2 = [b[1] + b[3] for b in boxes]
    return (min(xs), min(ys), max(x2) - min(xs), max(y2) - min(ys))


@legitimate_variant("rebuilt_shapes",
         "the damaged shapes deleted and drawn again — new ids, stock names, "
         "last in the z-order",
         applies=lambda ctx: None if any(_top_level(p) for p in
                                         _damaged_paths(ctx).values())
         else "no top-level shape was damaged")
def _v_rebuilt(ctx: "Ctx", out: Path) -> Built:
    """What a solver who *redraws* rather than *edits* actually produces.

    Same geometry, same words, same picture — and a different shape id, a
    different name and a different position in the shape tree, because the
    application appends what you draw.  Every one of those is something a
    matcher can mistake for a different shape.
    """
    pkg = ctx.open_gt()
    parts = pkg.slide_parts()
    moved = 0
    pages = []
    for index, paths in sorted(_damaged_paths(ctx).items()):
        if index >= len(parts):
            continue
        root = pkg.xml(parts[index])
        tree = root.find("p:cSld/p:spTree", NS)
        chosen = [resolve_path(tree, p) for p in _top_level(paths)]
        chosen = [s for s in chosen if s is not None]
        if not chosen:
            continue
        for shape in chosen:
            sid = next_shape_id(tree)
            shape.getparent().remove(shape)
            _rename(shape, sid, "Rectangle")
            tree.append(shape)
            moved += 1
        pkg.set_xml(parts[index], root)
        pages.append(index + 1)
    if not moved:
        raise Unconstructible("no damaged shape could be redrawn")
    pkg.save(out)
    got = Pkg(out)
    tail = []
    for page in pages[:4]:
        kids = _shape_children(got.sp_tree(got.slide_parts()[page - 1]))
        node = kids[-1].find(".//p:cNvPr", NS)
        tail.append(f"p{page}:{(node.get('name') if node is not None else '?')}")
    return Built(out, f"{moved} shapes redrawn on pages {pages[:6]} — "
                      f"now last in the tree: {' '.join(tail)}",
                 {"rebuilt": moved, "pages": pages})


@legitimate_variant("regrouped",
         "the damaged shapes wrapped in one group, drawn identically",
         applies=lambda ctx: None
         if any(len(_top_level(p)) > 1 for p in _damaged_paths(ctx).values())
         else "no page has two top-level damaged shapes to group")
def _v_regrouped(ctx: "Ctx", out: Path) -> Built:
    """A group is a container, not a picture: the shapes inside it are where
    they were.  This is the shape of the third production failure — a component
    comparing a group child's *local* coordinates against slide-level EMU — and
    the child offsets here are deliberately left untouched (`chOff`/`chExt`
    equal `off`/`ext`), so a comparator that reads them raw sees the same
    numbers and one that mishandles the matrix does not."""
    pkg = ctx.open_gt()
    parts = pkg.slide_parts()
    made = []
    for index, paths in sorted(_damaged_paths(ctx).items()):
        top = _top_level(paths)
        if index >= len(parts) or len(top) < 2:
            continue
        root = pkg.xml(parts[index])
        tree = root.find("p:cSld/p:spTree", NS)
        chosen = [resolve_path(tree, p) for p in top]
        chosen = [s for s in chosen if s is not None and _get_box(s)]
        if len(chosen) < 2:
            continue
        x, y, cx, cy = _union([_get_box(s) for s in chosen])
        sid = next_shape_id(tree)
        group = etree.fromstring(f"""<p:grpSp xmlns:p="{NS['p']}"
            xmlns:a="{NS['a']}">
          <p:nvGrpSpPr><p:cNvPr id="{sid}" name="Group {sid}"/>
            <p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
          <p:grpSpPr><a:xfrm><a:off x="{x}" y="{y}"/>
            <a:ext cx="{cx}" cy="{cy}"/>
            <a:chOff x="{x}" y="{y}"/><a:chExt cx="{cx}" cy="{cy}"/>
          </a:xfrm></p:grpSpPr></p:grpSp>""".encode())
        for shape in chosen:
            shape.getparent().remove(shape)
            group.append(shape)
        tree.append(group)
        pkg.set_xml(parts[index], root)
        made.append(f"p{index + 1}:{len(chosen)}")
    if not made:
        raise Unconstructible("no page had two groupable damaged shapes")
    pkg.save(out)
    got = Pkg(out)
    groups = sum(1 for p in got.slide_parts()
                 for s in _shape_children(got.sp_tree(p))
                 if s.tag == q("p:grpSp"))
    return Built(out, f"{len(made)} new group(s) ({' '.join(made[:6])}); "
                      f"{groups} groups in the deck, child offsets unchanged",
                 {"groups": made})


@legitimate_variant("ungrouped", "a group holding damaged shapes dissolved into loose ones",
         applies=lambda ctx: None
         if any("/" in p for paths in _damaged_paths(ctx).values()
                for p in paths) else "no damage inside a group")
def _v_ungrouped(ctx: "Ctx", out: Path) -> Built:
    """The reverse of `regrouped`, and the same claim: dissolving a group moves
    nothing.  Children are rewritten into slide coordinates through the group's
    own matrix, which is what the application does when you ungroup."""
    pkg = ctx.open_gt()
    parts = pkg.slide_parts()
    done = []
    for index, paths in sorted(_damaged_paths(ctx).items()):
        nested = sorted({p.split("/")[0] for p in paths if "/" in p})
        if index >= len(parts) or not nested:
            continue
        root = pkg.xml(parts[index])
        tree = root.find("p:cSld/p:spTree", NS)
        groups = [resolve_path(tree, p) for p in nested]
        for group in groups:
            if group is None or group.tag != q("p:grpSp"):
                continue
            xfrm = group.find("p:grpSpPr/a:xfrm", NS)
            if xfrm is None or xfrm.get("rot") or xfrm.get("flipH") \
                    or xfrm.get("flipV"):
                # a rotated or mirrored group does not dissolve into the same
                # picture, and pretending it does would make this a false
                # positive rather than a variant.
                continue
            off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
            ch_off, ch_ext = xfrm.find("a:chOff", NS), xfrm.find("a:chExt", NS)
            if off is None or ext is None:
                continue
            ox, oy = int(off.get("x")), int(off.get("y"))
            ex, ey = int(ext.get("cx")), int(ext.get("cy"))
            cox = int(ch_off.get("x")) if ch_off is not None else ox
            coy = int(ch_off.get("y")) if ch_off is not None else oy
            cex = int(ch_ext.get("cx")) if ch_ext is not None else ex
            cey = int(ch_ext.get("cy")) if ch_ext is not None else ey
            sx = ex / float(cex or 1)
            sy = ey / float(cey or 1)
            at = list(tree).index(group)
            for child in list(_shape_children(group)):
                box = _get_box(child)
                if box:
                    _set_box(child,
                             x=ox + (box[0] - cox) * sx,
                             y=oy + (box[1] - coy) * sy,
                             cx=box[2] * sx, cy=box[3] * sy)
                group.remove(child)
                tree.insert(at, child)
                at += 1
            group.getparent().remove(group)
            done.append(f"p{index + 1}")
        pkg.set_xml(parts[index], root)
    if not done:
        raise Unconstructible(
            "the groups holding damaged shapes are rotated or mirrored: "
            "dissolving them is not the same picture")
    pkg.save(out)
    got = Pkg(out)
    left = sum(1 for p in got.slide_parts()
               for s in _shape_children(got.sp_tree(p)) if s.tag == q("p:grpSp"))
    return Built(out, f"{len(done)} group(s) dissolved ({' '.join(done[:6])}); "
                      f"{left} top-level groups left, children in slide EMU",
                 {"dissolved": done})


@legitimate_variant("text_retyped",
         "the text of every damaged shape re-entered — same words, different "
         "run boundaries")
def _v_text_retyped(ctx: "Ctx", out: Path) -> Built:
    """Run boundaries are a fiction of whoever wrote the file.  Typing the same
    sentence again splits it differently — a spell-check tag, an autocorrect,
    one character typed twice — and every split carries the same properties the
    original run had, so nothing about the formatting has changed."""
    pkg = ctx.open_gt()
    parts = pkg.slide_parts()
    split = 0
    for index, paths in sorted(_damaged_paths(ctx).items()):
        if index >= len(parts):
            continue
        root = pkg.xml(parts[index])
        tree = root.find("p:cSld/p:spTree", NS)
        for path in paths:
            shape = resolve_path(tree, path)
            if shape is None:
                continue
            for para in shape.iter(q("a:p")):
                for run in list(para.findall(q("a:r"))):
                    node = run.find(q("a:t"))
                    text = (node.text or "") if node is not None else ""
                    if len(text) < 4:
                        continue
                    half = len(text) // 2
                    twin = copy.deepcopy(run)
                    node.text = text[:half]
                    twin.find(q("a:t")).text = text[half:]
                    run.addnext(twin)
                    split += 1
        pkg.set_xml(parts[index], root)
    if not split:
        raise Unconstructible("no damaged shape holds text long enough to split")
    pkg.save(out)
    got = Pkg(out)
    runs = sum(1 for p in got.slide_parts()
               for _ in got.xml(p).iter(q("a:r")))
    return Built(out, f"{split} runs split in two (same a:rPr on both halves); "
                      f"{runs} runs in the deck", {"split": split})


@legitimate_variant("picture_reinserted",
         "the damaged pictures inserted again from the supplied asset instead "
         "of restored in place",
         applies=lambda ctx: None if _damaged_pictures(ctx)
         else "no picture in the damage")
def _v_picture_reinserted(ctx: "Ctx", out: Path) -> Built:
    """"Insert picture" is the instruction; the file it produces is not the
    file that was there.  The bytes are the same — they come from `assets/` —
    but the part is new, the relationship id is new and the shape is new, so
    anything that identifies a picture by its part name or its shape id sees a
    stranger where the answer is."""
    pkg = ctx.open_gt()
    parts = pkg.slide_parts()
    done = []
    for index, shapes in sorted(_damaged_pictures(ctx).items()):
        if index >= len(parts):
            continue
        part = parts[index]
        root = pkg.xml(part)
        tree = root.find("p:cSld/p:spTree", NS)
        by_rid = {rel["id"]: _resolve(part, rel["target"])
                  for rel in pkg.rels(part) if rel["mode"] != "External"}
        for path in shapes:
            pic = resolve_path(tree, path)
            if pic is None or pic.tag != q("p:pic"):
                continue
            blip = pic.find(".//a:blip", NS)
            if blip is None:
                continue
            target = by_rid.get(blip.get(q("r:embed")) or "")
            if not target or not pkg.has(target):
                continue
            ext = target.rsplit(".", 1)[-1]
            fresh = f"ppt/media/inserted{len(done)}_{Path(target).stem}.{ext}"
            pkg.put(fresh, pkg.read(target))
            pkg.ensure_default(ext, f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}")
            rid = pkg.add_rel(part, IMAGE_REL,
                              posixpath.relpath(fresh, posixpath.dirname(part)))
            blip.set(q("r:embed"), rid)
            pic.getparent().remove(pic)
            _rename(pic, next_shape_id(tree), "Picture")
            tree.append(pic)
            done.append(f"p{index + 1}:{fresh.rsplit('/', 1)[-1]}")
        pkg.set_xml(part, root)
    if not done:
        raise Unconstructible("no damaged picture could be re-inserted")
    pkg.save(out)
    got = Pkg(out)
    media = sum(1 for n in got.names() if "/media/" in n)
    return Built(out, f"{len(done)} pictures re-inserted as new parts "
                      f"({' '.join(done[:4])}); {media} media parts in the "
                      f"package", {"reinserted": done})


def _damaged_pictures(ctx: "Ctx") -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for entry in ctx.entries():
        path = entry.get("path")
        if path in (None, "-") or entry.get("kind") != "picture":
            continue
        out.setdefault(entry["_slide"], []).append(str(path))
    return out


@legitimate_variant("colour_written_out",
         "theme colours on the damaged shapes written as the sRGB they resolve "
         "to",
         applies=lambda ctx: None if _theme_coloured(ctx)
         else "no unmodified theme colour on any damaged shape")
def _v_colour_written_out(ctx: "Ctx", out: Path) -> Built:
    """The equivalence REWARD.md §1 names first: *the theme colour resolved to
    an explicit sRGB*.  A colour picker that reports `#4472C4` and writes it
    back has changed nothing anybody can see.

    Only `a:schemeClr` with **no child modifiers** is converted: `lumMod` and
    friends are arithmetic the renderer does, and a value guessed for them
    would make this a false alarm rather than a variant."""
    pkg = ctx.open_gt()
    parts = pkg.slide_parts()
    scheme = _theme_colours(pkg)
    changed = []
    for index, paths in sorted(_damaged_paths(ctx).items()):
        if index >= len(parts):
            continue
        root = pkg.xml(parts[index])
        tree = root.find("p:cSld/p:spTree", NS)
        for path in paths:
            shape = resolve_path(tree, path)
            if shape is None:
                continue
            for node in list(shape.iter(q("a:schemeClr"))):
                rgb = scheme.get((node.get("val") or "").lower())
                if not rgb or len(node):
                    continue
                srgb = etree.Element(q("a:srgbClr"))
                srgb.set("val", rgb)
                node.getparent().replace(node, srgb)
                changed.append(f"{node.get('val')}->{rgb}")
        pkg.set_xml(parts[index], root)
    if not changed:
        raise Unconstructible("no unmodified theme colour on a damaged shape")
    pkg.save(out)
    return Built(out, f"{len(changed)} theme colours resolved to sRGB "
                      f"({', '.join(sorted(set(changed))[:4])})",
                 {"resolved": len(changed)})


#: `a:clrMap` renames four of the twelve slots; the rest are their own names.
_CLR_MAP_DEFAULT = {"tx1": "dk1", "bg1": "lt1", "tx2": "dk2", "bg2": "lt2"}


def _theme_colours(pkg: Pkg) -> dict[str, str]:
    """scheme colour name -> the sRGB the theme gives it, where it gives one."""
    theme = next((n for n in pkg.names()
                  if re.match(r"ppt/theme/theme\d+\.xml$", n)), None)
    if theme is None:
        return {}
    out: dict[str, str] = {}
    root = pkg.xml(theme)
    for slot in root.findall(".//a:themeElements/a:clrScheme/*", NS):
        name = _local(slot.tag)
        srgb = slot.find("a:srgbClr", NS)
        sys_clr = slot.find("a:sysClr", NS)
        value = (srgb.get("val") if srgb is not None
                 else (sys_clr.get("lastClr") if sys_clr is not None else None))
        if value:
            out[name] = value.upper()
    for alias, slot in _CLR_MAP_DEFAULT.items():
        if slot in out:
            out.setdefault(alias, out[slot])
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _theme_coloured(ctx: "Ctx") -> bool:
    try:
        pkg = ctx.open_gt()
    except Exception:                                           # noqa: BLE001
        return False
    scheme = _theme_colours(pkg)
    if not scheme:
        return False
    parts = pkg.slide_parts()
    for index, paths in _damaged_paths(ctx).items():
        if index >= len(parts):
            continue
        tree = pkg.sp_tree(parts[index])
        for path in paths:
            shape = resolve_path(tree, path)
            if shape is None:
                continue
            for node in shape.iter(q("a:schemeClr")):
                if not len(node) and (node.get("val") or "").lower() in scheme:
                    return True
    return False


def build_variants(ctx: "Ctx", outdir: Path, names=None) -> dict:
    """Construct every `legitimate_variant` this deck offers material for."""
    outdir.mkdir(parents=True, exist_ok=True)
    built: dict[str, Any] = {}
    for name in (names or list(LEGITIMATE_VARIANTS)):
        if name not in LEGITIMATE_VARIANTS:
            continue
        item = LEGITIMATE_VARIANTS[name]
        reason = item.applies(ctx)
        if reason:
            built[name] = Row(name, item.what, f"~ gt ±{VARIANT_TOL}", "n/a",
                              note=reason)
            continue
        try:
            built[name] = item.build(ctx, outdir / f"variant-{name}.pptx")
        except Unconstructible as error:
            built[name] = Row(name, item.what, f"~ gt ±{VARIANT_TOL}",
                              "unconstructible", note=str(error))
        except Exception as error:                              # noqa: BLE001
            built[name] = Row(name, item.what, f"~ gt ±{VARIANT_TOL}", "error",
                              note=f"{type(error).__name__}: {error}")
    return built


def score_variants(built: dict, scorer: "Scorer", plan, gt_inv, init_inv,
                   gt_score: float) -> list[Row]:
    """Score every variant against the same plan the attacks were scored with.

    Two verdicts, and both are the task's, not the variant's: a score that
    falls away from `gt` is a comparator that does not recognise correct work,
    and **a hard gate that fires is the 1100001 failure reproducing itself** —
    a gate zeroing a solution while a component in the same evaluator is paying
    for it.  An `unconstructible` variant is not a rejection here, unlike an
    attack: it means the deck offers no material for that route, and a route
    nobody can take proves nothing either way.  It is printed with its reason.
    """
    rows: list[Row] = []
    for item in sorted(LEGITIMATE_VARIANTS.values(), key=lambda v: v.order):
        got = built.get(item.name)
        if got is None:
            continue
        if isinstance(got, Row):
            rows.append(got)
            continue
        row = Row(item.name, item.what, f"~ gt ±{VARIANT_TOL}", "scored",
                  evidence=got.evidence, facts=dict(got.facts or {}))
        try:
            result = scorer.score(plan, inv.inventory_pptx(got.path), gt_inv,
                                  init_inv)
            row.score = float(result["score"])
            gate = result.get("failed_gate")
            fell = row.score < gt_score - VARIANT_TOL
            row.ok = not gate and not fell
            notes = []
            if gate:
                why = (result.get("gate_reasons") or {}).get(gate, "")
                notes.append(f"GATE {gate} fires on correct work: {why}")
            if fell:
                worst = sorted((result.get("components") or []),
                               key=lambda c: c["weight"] * (1.0 - c["score"]),
                               reverse=True)[:2]
                notes.append(
                    f"{row.score:.3f} < gt {gt_score:.3f} − {VARIANT_TOL}; lost: "
                    + "; ".join(f"{c.get('deg') or c['id']}/{c['op']} "
                                f"{c['score']:.2f}×{c['weight']:.2f} "
                                f"({c['why'][:60]})" for c in worst))
            if result.get("penalty"):
                notes.append(f"scope penalty {result['penalty']:.2f}: "
                             f"{sorted(result.get('scope_violations') or {})}")
            row.note = " | ".join(notes)
        except Exception as error:                              # noqa: BLE001
            row.status = "error"
            row.note = f"{type(error).__name__}: {error}"
        rows.append(row)
    return rows


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
            facts = roundtrip_facts(ctx.gt_path, out)
            results[ctx.name] = Built(
                out, f"WPS re-serialised the package "
                     f"({facts['before_bytes']} -> {facts['after_bytes']} bytes, "
                     f"{facts['parts_differing']} parts differ)", facts)
        except Exception as error:                              # noqa: BLE001
            errors[ctx.name] = f"{type(error).__name__}: {error}"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, jobs))
    for name, message in errors.items():
        results[name] = Built(Path("/dev/null"), "FAILED " + message,
                              {"kind": "wps_roundtrip", "failed": message})
    return results


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def roundtrip_facts(source: Path, saved: Path) -> dict:
    """The round trip as numbers, computed by reading both files back.

    `attacks.json` records `"wps": true`, which is `bool(wps)` off the command
    line — the flag, not the fact — and the candidate decks are deleted after
    scoring, so a free-text sentence was the only surviving proof that a WPS
    window ever opened.  An audit could only confirm the eight round trips by
    matching byte sizes *inside that prose*, and a future sentence reading
    `(N -> N bytes, 0 parts differ)` would have read exactly as clean.  These
    are the same facts in a shape a later reader can check without parsing
    English, and `roundtrip_problems` is what checks them.
    """
    return {
        "kind": "wps_roundtrip",
        "editor": "wps",
        "source": str(source),
        "before_bytes": Path(source).stat().st_size,
        "after_bytes": Path(saved).stat().st_size,
        "before_sha256": _sha(source),
        "after_sha256": _sha(saved),
        "parts_differing": _parts_differing(source, saved),
        "parts_before": len(Pkg(source).names()),
        "parts_after": len(Pkg(saved).names()),
    }


def roundtrip_problems(facts: dict | None, source: Path | None = None) -> list[str]:
    """Why this evidence does not show that a real application opened the file.

    Every clause is something the run of 2026-08-05 satisfied comfortably and
    that a stub, a copy or a silent fallback would not.
    """
    if not facts:
        return ["no structured evidence: the only record that WPS ran would be "
                "a sentence, and a sentence cannot be checked after the "
                "candidate decks are deleted"]
    if facts.get("failed"):
        return [f"the round trip failed: {facts['failed']}"]
    bad = []
    if facts.get("kind") != "wps_roundtrip":
        bad.append(f"evidence is not a WPS round trip (kind="
                   f"{facts.get('kind')!r})")
    for key in ("before_bytes", "after_bytes", "before_sha256", "after_sha256",
                "parts_differing"):
        if facts.get(key) in (None, ""):
            bad.append(f"evidence records no {key}")
    if bad:
        return bad
    if source is not None:
        want = Path(source)
        if facts["before_bytes"] != want.stat().st_size:
            bad.append(f"the file that went in was {facts['before_bytes']} "
                       f"bytes, the ground truth is {want.stat().st_size} — "
                       f"something other than the ground truth was round-tripped")
        elif facts.get("before_sha256") != _sha(want):
            bad.append("the file that went in does not hash to the ground truth")
    if facts["after_sha256"] == facts["before_sha256"]:
        bad.append("the saved file is byte-identical to the input: nothing "
                   "re-serialised it, so this is a copy and not a round trip")
    if int(facts["parts_differing"]) <= 0:
        bad.append("0 parts differ: no package part was rewritten, so no "
                   "application opened and saved this file")
    return bad


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
    #: scored | n/a | unconstructible | error | not_run.  The last is not a
    #: shade of `unconstructible`: that one says the attack was attempted and
    #: no candidate could be made, this says nobody asked it.  Both reject the
    #: deck — an unproven gate is an unproven gate — but the table has to say
    #: which, or the fix ("this deck has no chart to flatten" against "run
    #: this where WPS exists") is guesswork.
    status: str
    score: float | None = None
    ok: bool | None = None
    note: str = ""
    evidence: str = ""
    #: the components that earned the most, for a row that should have earned
    #: nothing — "which term paid out" is the only actionable half of a failure
    detail: list = field(default_factory=list)
    #: what the attack did, as numbers rather than as prose.  `evidence` is
    #: written for a person and is the only thing that survives the candidate
    #: decks being deleted; this is the same claim in a shape a later reader
    #: can check.  Carried through to `attacks.json` by `dataclasses.asdict`.
    facts: dict = field(default_factory=dict)


@dataclass
class Report:
    deck: str
    components: list[str]
    rows: list[Row]
    plan_rejected: list[str] = field(default_factory=list)
    #: the other direction — correct work, reached another way.  Kept in its
    #: own list because its verdicts are read the other way round: an attack
    #: that cannot be built is a rejection, a variant that cannot be built is
    #: a route this deck does not offer.
    variants: list[Row] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return bool(self.reasons)

    #: every status a row can hold, in the order a reader wants them counted
    STATUSES = ("scored", "n/a", "unconstructible", "error", "not_run", "built")

    def coverage(self) -> dict:
        """How many attacks and variants **ran**, not how many rows exist.

        `state.json` recorded `attacks: 14, variants: 6` for every deck —
        `len(rows)` and `len(variants)`.  The run those numbers describe
        actually executed 107 of 112 attack cells and 39 of 48 variant cells,
        and one deck's protection against being punished for legitimate work
        rested on 2 of its 6 variants.  A reader of the summary alone could not
        tell a battery that ran everything from one that found no material for
        a third of it, and the summary is what the pipeline and any dashboard
        read.  So the summary counts executions.
        """
        def count(rows: list[Row]) -> dict:
            out = {s: 0 for s in self.STATUSES}
            for row in rows:
                out[row.status] = out.get(row.status, 0) + 1
            out["total"] = len(rows)
            return out

        attacks, variants = count(self.rows), count(self.variants)
        return {
            "attacks_total": attacks["total"],
            "attacks_scored": attacks["scored"],
            "attacks_na": attacks["n/a"],
            "attacks_unproven": attacks["unconstructible"] + attacks["error"]
            + attacks["not_run"],
            "attacks_not_scored": [
                f"{r.attack} ({r.status})" for r in self.rows
                if r.status != "scored"],
            "variants_total": variants["total"],
            "variants_scored": variants["scored"],
            "variants_na": variants["n/a"] + variants["unconstructible"],
            "variants_error": variants["error"],
            "variants_not_scored": [
                f"{r.attack} ({r.status})" for r in self.variants
                if r.status != "scored"],
        }

    def coverage_line(self) -> str:
        c = self.coverage()
        return (f"{c['attacks_scored']}/{c['attacks_total']} attacks and "
                f"{c['variants_scored']}/{c['variants_total']} legitimate "
                f"variants were actually scored"
                + (f"; not scored: "
                   + ", ".join(c["attacks_not_scored"]
                               + c["variants_not_scored"])
                   if c["attacks_not_scored"] or c["variants_not_scored"]
                   else ""))

    @property
    def reasons(self) -> list[str]:
        out = [f"the comparator rejects the plan: {why}"
               for why in self.plan_rejected]
        for row in self.rows:
            if row.status == "unconstructible":
                out.append(f"{row.attack}: unproven gate — {row.note}")
            elif row.status == "not_run":
                out.append(f"{row.attack}: never fired — {row.note}")
            elif row.status == "error":
                out.append(f"{row.attack}: {row.note}")
            elif row.status == "scored" and row.ok is False:
                out.append(f"{row.attack}: {row.note}")
        for row in self.variants:
            if row.status == "error":
                out.append(f"variant {row.attack}: {row.note}")
            elif row.status == "scored" and row.ok is False:
                out.append(f"variant {row.attack}: {row.note}")
        return out


def build_all(ctx: Ctx, outdir: Path, names=None) -> dict[str, Row | Built]:
    """Construct every applicable candidate deck for one task."""
    outdir.mkdir(parents=True, exist_ok=True)
    built: dict[str, Any] = {}
    for name in (names or list(ATTACKS)):
        if name not in ATTACKS:
            continue                       # `--only` may name a variant
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
    outcomes: dict[str, dict] = {}
    for atk in order:
        item = built.get(atk.name)
        if item is None:
            continue
        if isinstance(item, Row):
            results[atk.name] = item
            continue
        row = Row(atk.name, atk.what, atk.expect.label(), "scored",
                  evidence=item.evidence, facts=dict(item.facts or {}))
        if item.evidence.startswith(("FAILED", "WPS unavailable")):
            row.status = "unconstructible"
            row.note = item.evidence
            results[atk.name] = row
            continue
        if atk.name == "gt_roundtrip" or row.facts.get("kind") == "wps_roundtrip":
            # the evidence has to be checkable, not merely present: with the
            # candidate deleted afterwards this row's facts are the entire
            # record that the application these tasks are graded in ever opened
            # the file, and an unverified record is the flag again.
            bad = roundtrip_problems(row.facts, ctx.gt_path)
            if bad:
                row.status = "unconstructible"
                row.note = "the round-trip evidence does not stand up: " \
                           + "; ".join(bad)
                results[atk.name] = row
                continue
        try:
            cand = inv.inventory_pptx(item.path)
            out = scorer.score(plan, cand, gt_inv, init_inv)
            outcomes[atk.name] = out
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
            row.ok, row.note = atk.expect.check(row.score, scores,
                                                outcomes.get(atk.name))
            if row.ok is False and row.detail:
                row.evidence += " | paid out: " + "; ".join(
                    f"{c.get('deg') or c['id']}/{c['op']} {c['score']:.2f}"
                    f"×{c['weight']:.2f} ({c['why'][:70]})"
                    for c in row.detail)
    variants = score_variants(built.get("_legitimate_variants") or {}, scorer,
                              plan,
                              gt_inv, init_inv, scores.get("gt", 1.0))
    return Report(ctx.name, list(ctx.components()),
                  [results[a.name] for a in order if a.name in results],
                  plan_rejected=refused, variants=variants)


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
            # `wrong_params` rejects the deck when it leaves a *graded* entry at
            # its correct value, and only the plan knows which entries those
            # are: the components it dropped as unscoreable carry no weight and
            # failing to perturb one of those costs nobody anything.
            ctx.plan_components = list(plans[ctx.name].get("components") or [])

    def one(ctx: Ctx):
        built[ctx.name] = build_all(ctx, outdir / ctx.name, names)
        # the second class, built in the same pass and kept apart under a key
        # no attack can have: mixing the two tables is what dilutes both.
        built[ctx.name]["_legitimate_variants"] = build_variants(
            ctx, outdir / ctx.name, names)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, ctxs))

    asked_for_roundtrip = names is None or "gt_roundtrip" in names
    if wps and asked_for_roundtrip:
        jobs = [(c, outdir / c.name / "gt_roundtrip.pptx") for c in ctxs]
        for name, item in wps_pass(jobs, workers=wps_workers).items():
            built[name]["gt_roundtrip"] = item
        for ctx in ctxs:
            built[ctx.name].setdefault("gt_roundtrip", Row(
                "gt_roundtrip", ATTACKS["gt_roundtrip"].what,
                ATTACKS["gt_roundtrip"].expect.label(), "unconstructible",
                note="WPS produced no saved file"))
    elif asked_for_roundtrip:
        # `--no-wps` used to leave no row at all, and a battery with no row
        # has nothing to fail: every table came back clean without once
        # asking whether the application these tasks are actually graded in
        # returns the ground truth unchanged.  An attack nobody ran is not an
        # attack that passed, so it says so in the one place a reader looks.
        for ctx in ctxs:
            built[ctx.name]["gt_roundtrip"] = Row(
                "gt_roundtrip", ATTACKS["gt_roundtrip"].what,
                ATTACKS["gt_roundtrip"].expect.label(), "not_run",
                note="WPS was switched off (--no-wps), so the one attack that "
                     "puts the ground truth through the application the task "
                     "is graded in was never run")

    if scorer is None:
        return [Report(c.name, list(c.components()),
                       [item if isinstance(item, Row) else
                        Row(k, ATTACKS[k].what, ATTACKS[k].expect.label(),
                            "built", evidence=item.evidence,
                            facts=dict(item.facts or {}))
                        for k, item in sorted(
                            ((k, v) for k, v in built[c.name].items()
                             if k in ATTACKS),
                            key=lambda kv: ATTACKS[kv[0]].order)],
                       variants=[item if isinstance(item, Row) else
                                 Row(k, LEGITIMATE_VARIANTS[k].what, "~ gt", "built",
                                     evidence=item.evidence,
                                     facts=dict(item.facts or {}))
                                 for k, item in sorted(
                                     (built[c.name].get("_legitimate_variants")
                                      or {}).items(),
                                     key=lambda kv: LEGITIMATE_VARIANTS[kv[0]].order)])
                for c in ctxs]
    return [score_all(c, built[c.name], scorer, plans.get(c.name)) for c in ctxs]


# --------------------------------------------------------------------------- #
# the table a person reads
# --------------------------------------------------------------------------- #


def _mark(row: Row) -> str:
    if row.status == "n/a":
        return "n/a"
    if row.status == "not_run":
        return "REJECT (never run)"
    if row.status in ("unconstructible", "error"):
        return "REJECT (unproven)"
    if row.status == "built":
        return "built"
    return "pass" if row.ok else "**FAIL**"


def _vmark(row: Row) -> str:
    """A variant reads the other way round: not being buildable is not a
    rejection, it is a route this deck does not offer."""
    if row.status == "n/a":
        return "n/a"
    if row.status == "unconstructible":
        return "no material"
    if row.status == "error":
        return "**ERROR**"
    if row.status == "built":
        return "built"
    return "pass" if row.ok else "**FAIL (rejects the task)**"


def variant_table(report: Report) -> list[str]:
    if not report.variants:
        return []
    lines = ["", "**legitimate variants** — the same answer reached another "
             "way.  Each must score within "
             f"{VARIANT_TOL} of `gt` **and trip no hard gate**; a gate that "
             "fires here rejects the task exactly as a successful attack does.",
             "",
             "| variant | what it does | score | verdict | evidence |",
             "|---|---|---|---|---|"]
    for row in report.variants:
        score = "—" if row.score is None else f"{row.score:.3f}"
        detail = row.evidence or row.note
        if row.note and row.evidence:
            detail = f"{row.note} — {row.evidence}"
        lines.append(f"| `{row.attack}` | {row.what} | {score} | "
                     f"{_vmark(row)} | {detail} |")
    return lines


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
    lines += variant_table(report)
    lines.append("")
    # rows are what was asked; this is what happened.  Printed under the two
    # tables because a table of fourteen rows five of which were `n/a` reads,
    # at a glance, as fourteen attacks that passed.
    lines.append(f"coverage: {report.coverage_line()}")
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
            if (row.status in ("unconstructible", "error", "not_run")
                    or row.ok is False):
                counts[row.attack] = counts.get(row.attack, 0) + 1
    vcounts: dict[str, int] = {}
    for report in reports:
        for row in report.variants:
            if row.status == "error" or row.ok is False:
                vcounts[row.attack] = vcounts.get(row.attack, 0) + 1
    cov = [r.coverage() for r in reports]
    lines = [f"{len(kept)}/{len(reports)} decks survive the battery.",
             "",
             f"{sum(c['attacks_scored'] for c in cov)}/"
             f"{sum(c['attacks_total'] for c in cov)} attack cells and "
             f"{sum(c['variants_scored'] for c in cov)}/"
             f"{sum(c['variants_total'] for c in cov)} variant cells were "
             f"actually scored — the rest found no material on their deck and "
             f"say so per row.",
             "",
             "| attack | decks it rejects |", "|---|---|"]
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{name}` | {count} |")
    if not counts:
        lines.append("| — | 0 |")
    lines += ["", "| legitimate variant | decks where correct work is not "
              "credited |", "|---|---|"]
    for name, count in sorted(vcounts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{name}` | {count} |")
    if not vcounts:
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
