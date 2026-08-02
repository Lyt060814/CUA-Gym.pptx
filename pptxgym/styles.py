"""Style extraction + theme color resolution (shared pipeline component).

Feeds the census with a per-shape `style` dict and gives the evaluator a way
to compare colors across EQUIVALENT representations: an agent may restore a
color as explicit sRGB while the GT used schemeClr+lumMod (or vice versa) —
both must resolve to (nearly) the same final RGB.

Color spec format (kept raw in the census, resolved lazily):
    {"kind": "srgb"|"scheme"|"sys"|"preset"|"none", "val": "FF0000"|"accent1",
     "mods": {"lumMod": 60000, "lumOff": 20000, "alpha": 50000,
              "shade": ..., "tint": ...}}          # percent-thousandths
"""

from __future__ import annotations

import colorsys
import math
import zipfile

from lxml import etree

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}


def q(tag):
    pfx, local = tag.split(":")
    return f"{{{NS[pfx]}}}{local}"


COLOR_TAGS = ["a:srgbClr", "a:schemeClr", "a:sysClr", "a:prstClr", "a:scrgbClr"]
MOD_TAGS = ["lumMod", "lumOff", "alpha", "shade", "tint", "satMod"]
_SYS_COLOR_FALLBACK = {"windowText": "000000", "window": "FFFFFF"}


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def parse_color(parent):
    """First color child of `parent` -> spec dict, or None."""
    if parent is None:
        return None
    for tag in COLOR_TAGS:
        el = parent.find(q(tag))
        if el is None:
            continue
        kind = {"a:srgbClr": "srgb", "a:schemeClr": "scheme", "a:sysClr": "sys",
                "a:prstClr": "preset", "a:scrgbClr": "scrgb"}[tag]
        if kind == "sys":
            # actual color lives in lastClr; val is a symbolic name
            val = el.get("lastClr") or _SYS_COLOR_FALLBACK.get(
                el.get("val", ""), "000000")
        else:
            val = el.get("val") or el.get("lastClr") or ""
        mods = {}
        for m in MOD_TAGS:
            c = el.find(q(f"a:{m}"))
            if c is not None:
                mods[m] = int(c.get("val", "0"))
        return {"kind": kind, "val": val, "mods": mods}
    return None


def _parse_fill(sp_pr):
    if sp_pr is None:
        return None
    if sp_pr.find(q("a:noFill")) is not None:
        return {"type": "none"}
    solid = sp_pr.find(q("a:solidFill"))
    if solid is not None:
        return {"type": "solid", "color": parse_color(solid)}
    grad = sp_pr.find(q("a:gradFill"))
    if grad is not None:
        stops = []
        for gs in grad.findall(f"{q('a:gsLst')}/{q('a:gs')}"):
            stops.append({"pos": int(gs.get("pos", "0")),
                          "color": parse_color(gs)})
        lin = grad.find(q("a:lin"))
        return {"type": "gradient", "stops": stops,
                "angle": int(lin.get("ang", "0")) if lin is not None else None}
    if sp_pr.find(q("a:blipFill")) is not None:
        return {"type": "blip"}
    if sp_pr.find(q("a:pattFill")) is not None:
        return {"type": "pattern"}
    return None                          # inherited / absent


def _parse_line(sp_pr):
    if sp_pr is None:
        return None
    ln = sp_pr.find(q("a:ln"))
    if ln is None:
        return None
    dash = ln.find(q("a:prstDash"))
    fill = _parse_fill(ln)
    head = ln.find(q("a:headEnd"))
    tail = ln.find(q("a:tailEnd"))
    return {
        "w": int(ln.get("w", "0")),
        "dash": dash.get("val") if dash is not None else "solid",
        "cap": ln.get("cap"),
        "color": (fill or {}).get("color"),
        "head": head.get("type") if head is not None else None,
        "tail": tail.get("type") if tail is not None else None,
        "no_fill": ln.find(q("a:noFill")) is not None,
    }


def _parse_effects(sp_pr):
    if sp_pr is None:
        return {}
    lst = sp_pr.find(q("a:effectLst"))
    if lst is None:
        return {}
    out = {}
    shdw = lst.find(q("a:outerShdw"))
    if shdw is not None:
        out["outerShdw"] = {
            "blurRad": int(shdw.get("blurRad", "0")),
            "dist": int(shdw.get("dist", "0")),
            "dir": int(shdw.get("dir", "0")),
            "color": parse_color(shdw),
        }
    inner = lst.find(q("a:innerShdw"))
    if inner is not None:
        out["innerShdw"] = {"blurRad": int(inner.get("blurRad", "0")),
                            "dist": int(inner.get("dist", "0")),
                            "dir": int(inner.get("dir", "0"))}
    glow = lst.find(q("a:glow"))
    if glow is not None:
        out["glow"] = {"rad": int(glow.get("rad", "0")),
                       "color": parse_color(glow)}
    refl = lst.find(q("a:reflection"))
    if refl is not None:
        out["reflection"] = {"blurRad": int(refl.get("blurRad", "0")),
                             "dist": int(refl.get("dist", "0"))}
    soft = lst.find(q("a:softEdge"))
    if soft is not None:
        out["softEdge"] = {"rad": int(soft.get("rad", "0"))}
    return out


def extract_style(el) -> dict:
    """Style dict for a p:sp / p:pic / p:cxnSp element."""
    sp_pr = el.find(q("p:spPr"))
    if sp_pr is None:
        return {}
    style = {}
    fill = _parse_fill(sp_pr)
    if fill:
        style["fill"] = fill
    line = _parse_line(sp_pr)
    if line:
        style["line"] = line
    fx = _parse_effects(sp_pr)
    if fx:
        style["effects"] = fx
    if sp_pr.find(q("a:sp3d")) is not None:
        style["sp3d"] = True
    if sp_pr.find(q("a:scene3d")) is not None:
        style["scene3d"] = True
    geom = sp_pr.find(q("a:prstGeom"))
    if geom is not None:
        style["prstGeom"] = geom.get("prst")
    return style


def count_style_features(style: dict) -> int:
    n = 0
    if style.get("fill", {}).get("type") == "gradient":
        n += 2
    n += len(style.get("effects", {}))
    if style.get("sp3d") or style.get("scene3d"):
        n += 2
    ln = style.get("line") or {}
    if ln and (ln.get("dash", "solid") != "solid" or ln.get("w", 0) > 19050):
        n += 1
    if style.get("prstGeom") not in (None, "rect"):
        n += 1
    return n


# --------------------------------------------------------------------------- #
# theme resolution
# --------------------------------------------------------------------------- #

_SCHEME_ALIASES = {"bg1": "lt1", "tx1": "dk1", "bg2": "lt2", "tx2": "dk2"}


class ThemeResolver:
    """Resolve a color spec to final (r, g, b) using the deck's theme.

    Reads clrScheme from ppt/theme/theme1.xml and the master's clrMap.
    lumMod/lumOff/shade/tint applied in HSL luminance space — the standard
    close-enough approximation; the evaluator compares with a delta-E band.
    """

    def __init__(self, pptx_path: str):
        self.scheme: dict[str, str] = {}
        self.clr_map: dict[str, str] = {}
        try:
            with zipfile.ZipFile(pptx_path) as z:
                theme_names = sorted(n for n in z.namelist()
                                     if n.startswith("ppt/theme/theme"))
                if theme_names:
                    root = etree.fromstring(z.read(theme_names[0]))
                    scheme = root.find(f".//{q('a:clrScheme')}")
                    if scheme is not None:
                        for child in scheme:
                            name = etree.QName(child).localname
                            spec = parse_color(child)
                            if spec:
                                self.scheme[name] = spec["val"]
                masters = sorted(n for n in z.namelist()
                                 if n.startswith("ppt/slideMasters/slideMaster"))
                if masters:
                    root = etree.fromstring(z.read(masters[0]))
                    cm = root.find(q("p:clrMap"))
                    if cm is not None:
                        self.clr_map = dict(cm.attrib)
        except Exception:                               # noqa: BLE001
            pass

    def resolve(self, spec: dict | None):
        if not spec or spec.get("kind") == "none":
            return None
        if spec["kind"] == "srgb":
            rgb = _hex_rgb(spec["val"])
        elif spec["kind"] == "scheme":
            name = spec["val"]
            name = self.clr_map.get(name, _SCHEME_ALIASES.get(name, name))
            name = _SCHEME_ALIASES.get(name, name)
            hexv = self.scheme.get(name)
            if hexv is None:
                return None
            rgb = _hex_rgb(hexv)
        elif spec["kind"] == "sys":
            rgb = _hex_rgb(spec["val"] or "000000")
        else:
            return None
        return _apply_mods(rgb, spec.get("mods", {}))


def _hex_rgb(hexv: str):
    hexv = hexv.strip().lstrip("#")
    if hexv in _SYS_COLOR_FALLBACK:
        hexv = _SYS_COLOR_FALLBACK[hexv]
    if len(hexv) != 6 or any(c not in "0123456789abcdefABCDEF" for c in hexv):
        return (0, 0, 0)
    return tuple(int(hexv[i:i + 2], 16) for i in (0, 2, 4))


def _apply_mods(rgb, mods):
    if not mods:
        return rgb
    r, g, b = (c / 255.0 for c in rgb)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if "lumMod" in mods:
        l = l * mods["lumMod"] / 100000.0
    if "lumOff" in mods:
        l = min(1.0, l + mods["lumOff"] / 100000.0)
    if "shade" in mods:
        l = l * mods["shade"] / 100000.0
    if "tint" in mods:
        t = mods["tint"] / 100000.0
        l = l * t + (1 - t)
    l = max(0.0, min(1.0, l))
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


# --------------------------------------------------------------------------- #
# color distance (CIE76 delta-E on Lab — adequate with band thresholds)
# --------------------------------------------------------------------------- #


def _srgb_to_lab(rgb):
    def f(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (f(c) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def g2(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = g2(x), g2(y), g2(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(rgb1, rgb2) -> float:
    if rgb1 is None or rgb2 is None:
        return 100.0
    l1, a1, b1 = _srgb_to_lab(rgb1)
    l2, a2, b2 = _srgb_to_lab(rgb2)
    return math.hypot(l1 - l2, math.hypot(a1 - a2, b1 - b2))
