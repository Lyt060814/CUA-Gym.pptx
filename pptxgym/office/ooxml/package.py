"""Minimal in-memory OPC package editing without unrelated rewrites."""

from __future__ import annotations

import posixpath
import re
import zipfile
from pathlib import Path

from lxml import etree


NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
}


def q(tag: str) -> str:
    prefix, local = tag.split(":")
    return f"{{{NS[prefix]}}}{local}"


def resolve_target(part: str, target: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(part), target))


def relationship_part(part: str) -> str:
    head, _, tail = part.rpartition("/")
    return f"{head}/_rels/{tail}.rels" if head else f"_rels/{tail}.rels"


def parse_xml(data: bytes):
    return etree.fromstring(data)


def serialize_xml(element) -> bytes:
    return etree.tostring(element, xml_declaration=True, encoding="UTF-8",
                          standalone=True)


class Package:
    """An OPC package held in memory and edited part by part."""

    def __init__(self, path: str | Path):
        self.src = str(path)
        self._data: dict[str, bytes] = {}
        self._order: list[str] = []
        with zipfile.ZipFile(self.src) as archive:
            for info in archive.infolist():
                if info.filename.endswith("/"):
                    continue
                self._order.append(info.filename)
                self._data[info.filename] = archive.read(info.filename)

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
        return parse_xml(self._data[name])

    def set_xml(self, name: str, element) -> None:
        self.put(name, serialize_xml(element))

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in self._order:
                archive.writestr(name, self._data[name])
        return path

    def rels(self, part: str) -> list[dict[str, str]]:
        name = relationship_part(part)
        if not self.has(name):
            return []
        out = []
        for node in self.xml(name).findall("pr:Relationship", NS):
            out.append({"id": node.get("Id", ""),
                        "type": node.get("Type", ""),
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
        self.set_xml(relationship_part(part), root)

    def add_rel(self, part: str, type_url: str, target: str) -> str:
        rels = self.rels(part)
        used = {int(match.group(1)) for rel in rels
                if (match := re.match(r"rId(\d+)$", rel["id"]))}
        rid = f"rId{max(used, default=0) + 1}"
        rels.append({"id": rid, "type": type_url, "target": target,
                     "mode": "Internal"})
        self.set_rels(part, rels)
        return rid

    def targets(self, part: str) -> list[str]:
        return [resolve_target(part, rel["target"]) for rel in self.rels(part)
                if rel["mode"] != "External"]

    def ensure_default(self, ext: str, content_type: str) -> None:
        root = self.xml("[Content_Types].xml")
        for node in root.findall("ct:Default", NS):
            if (node.get("Extension") or "").lower() == ext.lower():
                return
        node = etree.SubElement(root, q("ct:Default"))
        node.set("Extension", ext)
        node.set("ContentType", content_type)
        self.set_xml("[Content_Types].xml", root)

    def ensure_override(self, part: str, content_type: str) -> None:
        root = self.xml("[Content_Types].xml")
        wanted = "/" + part
        for node in root.findall("ct:Override", NS):
            if node.get("PartName") == wanted:
                return
        node = etree.SubElement(root, q("ct:Override"))
        node.set("PartName", wanted)
        node.set("ContentType", content_type)
        self.set_xml("[Content_Types].xml", root)

    def override_of(self, part: str) -> str | None:
        wanted = "/" + part
        for node in self.xml("[Content_Types].xml").findall("ct:Override", NS):
            if node.get("PartName") == wanted:
                return node.get("ContentType")
        return None

    def drop_override(self, part: str) -> None:
        root = self.xml("[Content_Types].xml")
        wanted = "/" + part
        for node in root.findall("ct:Override", NS):
            if node.get("PartName") == wanted:
                root.remove(node)
        self.set_xml("[Content_Types].xml", root)

    def slide_parts(self) -> list[str]:
        presentation = "ppt/presentation.xml"
        if not self.has(presentation):
            return sorted((name for name in self.names()
                           if re.match(r"^ppt/slides/slide\d+\.xml$", name)),
                          key=lambda name: int(
                              re.search(r"(\d+)\.xml$", name).group(1)))
        by_id = {rel["id"]: resolve_target(presentation, rel["target"])
                 for rel in self.rels(presentation)
                 if rel["mode"] != "External"}
        out = []
        for node in self.xml(presentation).findall("p:sldIdLst/p:sldId", NS):
            target = by_id.get(node.get(q("r:id")) or "")
            if target and self.has(target):
                out.append(target)
        return out

    def slide_size(self) -> tuple[int, int]:
        node = self.xml("ppt/presentation.xml").find("p:sldSz", NS)
        return int(node.get("cx")), int(node.get("cy"))

    def sp_tree(self, slide_part: str):
        return self.xml(slide_part).find("p:cSld/p:spTree", NS)


def closure(package: Package, roots) -> set[str]:
    """Every part reachable from roots through internal relationships."""
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        part = stack.pop()
        if part in seen or not package.has(part):
            continue
        seen.add(part)
        rels = relationship_part(part)
        if package.has(rels):
            seen.add(rels)
        stack.extend(package.targets(part))
    return seen
