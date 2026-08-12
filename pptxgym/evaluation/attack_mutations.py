"""Deterministic OOXML mutations used by the adversarial battery.

Each helper changes one scored facet to a value known to be wrong. The attack
registry and policy stay in :mod:`pptxgym.evaluation.attacks`.
"""

from __future__ import annotations

import re

from lxml import etree

from ..office.ooxml.package import Package as Pkg
from ..office.ooxml.package import resolve_target as _resolve

EMU_IN = 914400
NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
}


def q(tag: str) -> str:
    prefix, local = tag.split(":")
    return f"{{{NS[prefix]}}}{local}"


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
