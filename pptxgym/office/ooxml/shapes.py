"""Shape-tree traversal shared by inventory, damage, and attack code."""

from __future__ import annotations

from collections.abc import Iterable


NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}


def q(tag: str) -> str:
    prefix, local = tag.split(":")
    return f"{{{NS[prefix]}}}{local}"


SHAPE_TAGS = {q("p:sp"), q("p:pic"), q("p:graphicFrame"), q("p:cxnSp"),
              q("p:grpSp")}


def shape_children(element) -> Iterable:
    """Drawable children in order, unwrapping AlternateContent blocks."""
    for child in element:
        if child.tag == q("mc:AlternateContent"):
            branch = child.find("mc:Choice", NS)
            if branch is None:
                branch = child.find("mc:Fallback", NS)
            if branch is not None:
                yield from shape_children(branch)
        elif child.tag in SHAPE_TAGS:
            yield child


def resolve_path(shape_tree, path: str):
    """Return the shape at a recipe path such as ``3`` or ``19/0``."""
    node = shape_tree
    for step in str(path).split("/"):
        children = list(shape_children(node))
        index = int(step)
        if index >= len(children):
            return None
        node = children[index]
    return node


def next_shape_id(shape_tree) -> int:
    ids = [int(node.get("id")) for node in shape_tree.iter(q("p:cNvPr"))
           if (node.get("id") or "").isdigit()]
    return max(ids, default=1) + 1
