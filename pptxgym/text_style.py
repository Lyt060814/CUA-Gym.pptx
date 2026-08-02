"""Paragraph- and run-level text formatting for the census.

Text formatting is the single most common thing in a real deck — 96% of slides
carry explicit run properties — and it is exactly what a proposer cannot read
off a render with any confidence ("is that bold, or a heavier font?").

The hard part is that almost nothing is stated where it applies.  A run with no
``a:rPr`` still has a font, a size and a colour; they come from an inheritance
chain that OOXML never spells out in the slide part:

    run rPr
      -> paragraph pPr/defRPr
      -> shape txBody/lstStyle/lvlNpPr/defRPr
      -> layout placeholder lstStyle for the same family + level
      -> master placeholder lstStyle, then master txStyles
         (titleStyle / bodyStyle / otherStyle) for that level
      -> theme fontScheme for the +mj-lt / +mn-lt tokens

`TextResolver` walks that chain once per deck and caches it, so every run comes
back with an *effective* font/size/weight rather than a mostly-empty dict.
"""

from __future__ import annotations

import re
from lxml import etree

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def q(tag):
    pfx, local = tag.split(":")
    return f"{{{NS[pfx]}}}{local}"


_FAMILY_STYLE = {"title": "p:titleStyle", "body": "p:bodyStyle"}


# --------------------------------------------------------------------------- #
# leaf readers
# --------------------------------------------------------------------------- #


def _read_rpr(rpr, parse_color):
    """Explicitly-stated run properties only — inheritance happens later."""
    if rpr is None:
        return {}
    out = {}
    if rpr.get("sz"):
        out["sz"] = int(rpr.get("sz")) / 100.0
    for attr, key in (("b", "b"), ("i", "i")):
        if rpr.get(attr) is not None:
            out[key] = rpr.get(attr) in ("1", "true")
    if rpr.get("u") and rpr.get("u") != "none":
        out["u"] = rpr.get("u")
    if rpr.get("strike") and rpr.get("strike") != "noStrike":
        out["strike"] = rpr.get("strike")
    if rpr.get("spc"):
        out["spc"] = int(rpr.get("spc")) / 100.0
    if rpr.get("baseline") and rpr.get("baseline") != "0":
        out["baseline"] = int(rpr.get("baseline")) / 1000.0
    if rpr.get("cap") and rpr.get("cap") != "none":
        out["cap"] = rpr.get("cap")
    latin = rpr.find(q("a:latin"))
    if latin is not None and latin.get("typeface"):
        out["font"] = latin.get("typeface")
    ea = rpr.find(q("a:ea"))
    if ea is not None and ea.get("typeface"):
        out["font_ea"] = ea.get("typeface")
    fill = rpr.find(q("a:solidFill"))
    if fill is not None:
        spec = parse_color(fill)
        if spec:
            out["color"] = spec
    elif rpr.find(q("a:noFill")) is not None:
        out["color"] = {"kind": "none", "val": None, "mods": {}}
    if rpr.find(q("a:hlinkClick")) is not None:
        out["link"] = True
    ln = rpr.find(q("a:ln"))
    if ln is not None and ln.find(q("a:noFill")) is None:
        out["outline"] = True
    return out


def _read_ppr(ppr):
    """Explicitly-stated paragraph properties."""
    if ppr is None:
        return {}
    out = {}
    if ppr.get("lvl"):
        out["lvl"] = int(ppr.get("lvl"))
    if ppr.get("algn"):
        out["algn"] = ppr.get("algn")
    if ppr.get("marL"):
        out["marL"] = int(ppr.get("marL"))
    if ppr.get("indent"):
        out["indent"] = int(ppr.get("indent"))
    if ppr.get("rtl") == "1":
        out["rtl"] = True
    ln = ppr.find(q("a:lnSpc"))
    if ln is not None:
        pct = ln.find(q("a:spcPct"))
        pts = ln.find(q("a:spcPts"))
        if pct is not None:
            out["line_pct"] = int(pct.get("val")) / 1000.0
        elif pts is not None:
            out["line_pts"] = int(pts.get("val")) / 100.0
    for tag, key in ((q("a:spcBef"), "before_pts"), (q("a:spcAft"), "after_pts")):
        el = ppr.find(tag)
        if el is not None:
            pts = el.find(q("a:spcPts"))
            pct = el.find(q("a:spcPct"))
            if pts is not None:
                out[key] = int(pts.get("val")) / 100.0
            elif pct is not None:
                out[key.replace("_pts", "_pct")] = int(pct.get("val")) / 1000.0
    if ppr.find(q("a:buNone")) is not None:
        out["bullet"] = "none"
    else:
        ch = ppr.find(q("a:buChar"))
        num = ppr.find(q("a:buAutoNum"))
        pic = ppr.find(q("a:buBlip"))
        if ch is not None:
            out["bullet"] = f"char:{ch.get('char')}"
        elif num is not None:
            out["bullet"] = f"num:{num.get('type', 'arabicPeriod')}"
        elif pic is not None:
            out["bullet"] = "image"
    return out


# --------------------------------------------------------------------------- #
# inheritance
# --------------------------------------------------------------------------- #


class TextResolver:
    """Effective text formatting, resolved through the OOXML inheritance chain."""

    def __init__(self, prs, parse_color):
        self.prs = prs
        self.parse_color = parse_color
        self._lvl_cache: dict = {}
        self._theme_fonts = {"major": None, "minor": None}
        self._load_theme_fonts()
        # A shape that is not a placeholder does NOT inherit the master's
        # bodyStyle — it falls back to presentation.xml's defaultTextStyle.
        self._default_style = prs.element.find(q("p:defaultTextStyle"))

    def _load_theme_fonts(self):
        try:
            master = self.prs.slide_masters[0]
            theme_part = None
            for rel in master.part.rels.values():
                if "theme" in rel.reltype:
                    theme_part = rel.target_part
                    break
            if theme_part is None:
                return
            root = etree.fromstring(theme_part.blob)
            for which, tag in (("major", "a:majorFont"), ("minor", "a:minorFont")):
                fs = root.find(f".//{q(tag)}")
                if fs is not None:
                    latin = fs.find(q("a:latin"))
                    if latin is not None:
                        self._theme_fonts[which] = latin.get("typeface")
        except Exception:                                        # noqa: BLE001
            pass

    def _resolve_font_token(self, name):
        if not name:
            return name
        if name.startswith("+mj"):
            return self._theme_fonts["major"] or name
        if name.startswith("+mn"):
            return self._theme_fonts["minor"] or name
        return name

    @staticmethod
    def _lvl_defaults(lst_style, lvl):
        """defRPr/pPr of a:lstStyle for a given 0-based level."""
        if lst_style is None:
            return {}, {}
        el = lst_style.find(q(f"a:lvl{lvl + 1}pPr"))
        if el is None:
            el = lst_style.find(q("a:lvl1pPr"))
        if el is None:
            return {}, {}
        return el, el.find(q("a:defRPr"))

    def _inherited_chain(self, slide, ph, lvl):
        """(pPr-dicts, rPr-dicts) from outermost default inward, for a level."""
        key = (id(slide.slide_layout), (ph or {}).get("idx"),
               (ph or {}).get("type"), lvl)
        if key in self._lvl_cache:
            return self._lvl_cache[key]

        p_chain, r_chain = [], []
        layout = slide.slide_layout
        master = layout.slide_master

        if ph is None:
            # free-floating textbox / autoshape: presentation-level defaults
            ppr_el, rpr_el = self._lvl_defaults(self._default_style, lvl)
            if ppr_el is not None and ppr_el != {}:
                p_chain.append(_read_ppr(ppr_el))
                r_chain.append(_read_rpr(rpr_el, self.parse_color))
            self._lvl_cache[key] = (p_chain, r_chain)
            return p_chain, r_chain

        # master txStyles for this placeholder family (outermost)
        family = _ph_family(ph.get("type"))
        style_tag = _FAMILY_STYLE.get(family, "p:otherStyle")
        tx = master.element.find(f"{q('p:txStyles')}/{q(style_tag)}")
        if tx is not None:
            ppr_el, rpr_el = self._lvl_defaults(tx, lvl)
            if ppr_el is not None and ppr_el != {}:
                p_chain.append(_read_ppr(ppr_el))
                r_chain.append(_read_rpr(rpr_el, self.parse_color))

        # master then layout placeholder lstStyle
        for host in (master, layout):
            el = _find_ph_element(host, ph)
            if el is None:
                continue
            lst = el.find(f"{q('p:txBody')}/{q('a:lstStyle')}")
            ppr_el, rpr_el = self._lvl_defaults(lst, lvl)
            if ppr_el is not None and ppr_el != {}:
                p_chain.append(_read_ppr(ppr_el))
                r_chain.append(_read_rpr(rpr_el, self.parse_color))

        self._lvl_cache[key] = (p_chain, r_chain)
        return p_chain, r_chain

    # -- public ------------------------------------------------------------- #

    def shape_text(self, el, slide, ph, max_paras=12, max_runs=8):
        """Full paragraph/run structure of a p:sp, with effective formatting."""
        body = el.find(q("p:txBody"))
        if body is None:
            body = el.find(f"{q('p:graphicFrame')}/{q('p:txBody')}")
        if body is None:
            return None

        shape_lst = body.find(q("a:lstStyle"))
        body_pr = body.find(q("a:bodyPr"))
        paras = []
        for p in body.findall(q("a:p")):
            ppr = p.find(q("a:pPr"))
            own_p = _read_ppr(ppr)
            lvl = own_p.get("lvl", 0)

            p_chain, r_chain = self._inherited_chain(slide, ph, lvl)
            sp_ppr_el, sp_rpr_el = self._lvl_defaults(shape_lst, lvl)
            sp_p = _read_ppr(sp_ppr_el) if sp_ppr_el is not None and sp_ppr_el != {} else {}
            sp_r = (_read_rpr(sp_rpr_el, self.parse_color)
                    if sp_ppr_el is not None and sp_ppr_el != {} else {})

            eff_p = {}
            for layer in (*p_chain, sp_p, own_p):
                eff_p.update(layer)

            para_def = _read_rpr(ppr.find(q("a:defRPr")) if ppr is not None else None,
                                 self.parse_color)
            base_r = {}
            for layer in (*r_chain, sp_r, para_def):
                base_r.update(layer)

            runs = []
            for node in p:
                local = etree.QName(node).localname
                if local == "br":
                    if runs:
                        runs[-1]["t"] += "\n"
                    continue
                if local not in ("r", "fld"):
                    continue
                t = node.find(q("a:t"))
                text = t.text if t is not None and t.text else ""
                if local == "fld":
                    text = text or f"<{node.get('type', 'field')}>"
                eff_r = dict(base_r)
                eff_r.update(_read_rpr(node.find(q("a:rPr")), self.parse_color))
                eff_r["font"] = self._resolve_font_token(eff_r.get("font"))
                eff_r["t"] = text
                if local == "fld":
                    eff_r["field"] = node.get("type", "")
                runs.append(eff_r)

            if not runs:
                endp = p.find(q("a:endParaRPr"))
                if endp is not None:
                    eff_r = dict(base_r)
                    eff_r.update(_read_rpr(endp, self.parse_color))
                    eff_r["font"] = self._resolve_font_token(eff_r.get("font"))
                    eff_r["t"] = ""
                    runs.append(eff_r)

            paras.append({"lvl": lvl, "props": eff_p,
                          "runs": _merge_runs(runs)[:max_runs],
                          "n_runs": len(runs)})
            if len(paras) >= max_paras:
                break

        out = {"paragraphs": paras, "n_paragraphs": len(body.findall(q("a:p")))}
        if body_pr is not None:
            if body_pr.get("anchor"):
                out["anchor"] = body_pr.get("anchor")
            if body_pr.get("rot"):
                out["text_rot"] = int(body_pr.get("rot")) / 60000.0
            if body_pr.get("vert") and body_pr.get("vert") != "horz":
                out["vert"] = body_pr.get("vert")
            if body_pr.find(q("a:normAutofit")) is not None:
                out["autofit"] = "shrink"
            elif body_pr.find(q("a:spAutoFit")) is not None:
                out["autofit"] = "resize_shape"
            if body_pr.get("wrap") == "none":
                out["wrap"] = "none"
        return out


def _merge_runs(runs):
    """Collapse adjacent runs that differ only in text — decks split constantly."""
    out = []
    for r in runs:
        if out:
            a = {k: v for k, v in out[-1].items() if k != "t"}
            b = {k: v for k, v in r.items() if k != "t"}
            if a == b:
                out[-1]["t"] += r["t"]
                continue
        out.append(dict(r))
    for r in out:
        r["t"] = re.sub(r"[ \t]+", " ", r["t"])
    return out


def _find_ph_element(host, ph):
    if not ph:
        return None
    want = _ph_family(ph.get("type"))
    fallback = None
    for cand in host.placeholders:
        el = cand._element
        info = el.find(f".//{q('p:nvPr')}/{q('p:ph')}")
        if info is None:
            continue
        if int(info.get("idx", "0")) == ph.get("idx"):
            return el
        if _ph_family(info.get("type", "body")) == want and fallback is None:
            fallback = el
    return fallback


_PH_FAMILY = {
    "ctrTitle": "title", "title": "title",
    "subTitle": "body", "body": "body", "obj": "body", "tbl": "body",
    "chart": "body", "clipArt": "body", "dgm": "body", "media": "body",
    "pic": "body", "sldImg": "body",
    "dt": "other", "ftr": "other", "sldNum": "other", "hdr": "other",
}


def _ph_family(t):
    return _PH_FAMILY.get(t or "body", "body")


# --------------------------------------------------------------------------- #
# summarisation — what the digest actually shows
# --------------------------------------------------------------------------- #


def summarize(text_style, resolver=None):
    """Compact one shape's formatting into something a proposer can scan."""
    if not text_style:
        return None
    fonts, sizes, colors = set(), set(), set()
    bold = italic = links = 0
    n = 0
    for para in text_style["paragraphs"]:
        for r in para["runs"]:
            if not r.get("t", "").strip():
                continue
            n += 1
            if r.get("font"):
                fonts.add(r["font"])
            if r.get("sz"):
                sizes.add(round(r["sz"], 1))
            if r.get("b"):
                bold += 1
            if r.get("i"):
                italic += 1
            if r.get("link"):
                links += 1
            if resolver is not None and r.get("color"):
                rgb = resolver.resolve(r["color"])
                if rgb:
                    colors.add("#%02X%02X%02X" % rgb)
    if not n:
        return None
    out = {"fonts": sorted(fonts), "sizes_pt": sorted(sizes),
           "colors": sorted(colors), "n_runs": n}
    if bold:
        out["bold_runs"] = bold
    if italic:
        out["italic_runs"] = italic
    if links:
        out["link_runs"] = links
    aligns = {p["props"].get("algn") for p in text_style["paragraphs"]}
    aligns.discard(None)
    if aligns:
        out["align"] = sorted(aligns)
    bullets = {p["props"].get("bullet") for p in text_style["paragraphs"]}
    bullets.discard(None)
    if bullets - {"none"}:
        out["bullets"] = sorted(b for b in bullets if b != "none")
    lvls = {p["lvl"] for p in text_style["paragraphs"]}
    if lvls - {0}:
        out["levels"] = sorted(lvls)
    for k in ("anchor", "autofit", "text_rot", "vert"):
        if text_style.get(k):
            out[k] = text_style[k]
    return out
