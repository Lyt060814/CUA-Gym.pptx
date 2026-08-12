"""Slide and shape correspondence for the PPTX reward engine."""

from __future__ import annotations

import hashlib
import math


POS_TOL = 9144
_WEAK_KEY_PREFIXES = ("name:", "geo:", "kind:")
_ORDER_MARGIN = 0.20
_ORDER_EVIDENCE = 2


class Unscorable(Exception):
    """The evaluator cannot establish what the answer is."""


def norm(text: str) -> str:
    return " ".join((text or "").split())


def bbox(shape: dict | None) -> dict | None:
    return (shape or {}).get("bbox")


def centre_ok(a: dict, b: dict) -> bool:
    return abs(a["cx"] - b["cx"]) <= POS_TOL \
        and abs(a["cy"] - b["cy"]) <= POS_TOL


def extent_ok(a: dict, b: dict, dims: tuple[str, ...]) -> bool:
    return all(abs(a[dim] - b[dim]) <= POS_TOL for dim in dims)


def is_strong(key: str | None) -> bool:
    return bool(key) and not key.startswith(_WEAK_KEY_PREFIXES)


def boxes_meet(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return True
    for centre, dimension in (("cx", "w"), ("cy", "h")):
        a0 = a[centre] - a[dimension] / 2.0
        a1 = a[centre] + a[dimension] / 2.0
        b0 = b[centre] - b[dimension] / 2.0
        b1 = b[centre] + b[dimension] / 2.0
        if a1 + POS_TOL < b0 or b1 + POS_TOL < a0:
            return False
    return True


def pair_slide_detail(gt_shapes: list[dict], other: list[dict]) \
        -> dict[str, tuple[dict | None, str | None]]:
    """Pair strong identities before weak labels or geometry classes."""
    proposals = []
    for gt_index, gt_shape in enumerate(gt_shapes):
        for other_index, other_shape in enumerate(other):
            other_keys = set(other_shape["keys"])
            for rank, key in enumerate(gt_shape["keys"]):
                if key not in other_keys:
                    continue
                gt_box, other_box = bbox(gt_shape), bbox(other_shape)
                if not is_strong(key) and not boxes_meet(gt_box, other_box):
                    continue
                distance = (math.hypot(gt_box["cx"] - other_box["cx"],
                                       gt_box["cy"] - other_box["cy"])
                            if gt_box and other_box else 0.0)
                proposals.append((rank, distance, gt_shape["_path"],
                                  gt_index, other_index, key))
                break
    proposals.sort(key=lambda proposal: (proposal[0], proposal[1], proposal[2]))
    taken_gt: set[int] = set()
    taken_other: set[int] = set()
    out = {shape["_path"]: (None, None) for shape in gt_shapes}
    for _rank, _distance, path, gt_index, other_index, key in proposals:
        if gt_index in taken_gt or other_index in taken_other:
            continue
        taken_gt.add(gt_index)
        taken_other.add(other_index)
        out[path] = (other[other_index], key)
    return out


def pair_slide(gt_shapes: list[dict], other: list[dict]) \
        -> dict[str, dict | None]:
    return {path: shape for path, (shape, _key)
            in pair_slide_detail(gt_shapes, other).items()}


def _page_signature(slide: dict) -> set:
    return {hashlib.sha256(norm(shape.get("_plain", "")).encode())
            .hexdigest()[:8]
            for shape in slide.get("shapes", [])
            if norm(shape.get("_plain", ""))}


def _overlap(left: set, right: set) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / float(len(left | right))


def page_is_itself(gt_slides: list[dict], mine: dict, index: int) -> bool:
    wanted = _page_signature(gt_slides[index])
    if len(wanted) < _ORDER_EVIDENCE:
        return True
    have = _page_signature(mine)
    here = _overlap(wanted, have)
    best_score, best = here, index
    for other, page in enumerate(gt_slides):
        value = _overlap(_page_signature(page), have)
        if value > best_score:
            best_score, best = value, other
    return best == index or best_score <= here + _ORDER_MARGIN


class Scene:
    """One ground-truth/other-file pairing, built once and reused."""

    def __init__(self, gt_inv: dict, other_inv: dict,
                 slide_of: list | None = None):
        self.gt = gt_inv
        self.other = other_inv
        count = len(gt_inv["slides"])
        self.slide_of = list(slide_of) if slide_of else list(range(count))
        self._pairs: dict[int, dict[str, tuple[dict | None, str | None]]] = {}
        self._flat: dict[int, dict[str, dict | None]] = {}

    def gt_slide(self, index: int) -> dict:
        try:
            return self.gt["slides"][index]
        except IndexError as error:
            raise Unscorable(f"gt has no slide {index}") from error

    def slide(self, index: int) -> dict | None:
        if index >= len(self.slide_of):
            return None
        target = self.slide_of[index]
        if target is None or target >= len(self.other["slides"]):
            return None
        return self.other["slides"][target]

    def detail(self, index: int) -> dict[str, tuple[dict | None, str | None]]:
        if index not in self._pairs:
            other = self.slide(index)
            self._pairs[index] = pair_slide_detail(
                self.gt_slide(index)["shapes"],
                other["shapes"] if other else [])
        return self._pairs[index]

    def pairs(self, index: int) -> dict[str, dict | None]:
        if index not in self._flat:
            self._flat[index] = {
                path: shape for path, (shape, _key) in self.detail(index).items()}
        return self._flat[index]

    def key_for(self, index: int, path: str) -> str | None:
        return self.detail(index).get(path, (None, None))[1]


class Target:
    """One comparator's slide, shape, and ground-truth originals."""

    def __init__(self, scene: Scene, component: dict):
        self.scene = scene
        self.component = component
        self.index = component["slide"]
        self.spec = component["spec"]

    @property
    def gt_slide(self) -> dict:
        return self.scene.gt_slide(self.index)

    @property
    def slide(self) -> dict | None:
        return self.scene.slide(self.index)

    @property
    def gt_shape(self) -> dict:
        path = self.component.get("gt_path")
        if path is None:
            raise Unscorable("component has no shape path")
        for shape in self.gt_slide["shapes"]:
            if shape["_path"] == path:
                return shape
        raise Unscorable(
            f"no shape at gt path {path!r} on slide {self.index + 1}")

    @property
    def shape(self) -> dict | None:
        return self.scene.pairs(self.index).get(self.component.get("gt_path"))

    def counterpart(self, gt_shape: dict) -> dict | None:
        return self.scene.pairs(self.index).get(gt_shape["_path"])

    def gt_siblings(self, gt_shape: dict) -> list[dict]:
        prefix = gt_shape["_path"].rsplit("/", 1)[0] + "/" \
            if "/" in gt_shape["_path"] else ""
        return [shape for shape in self.gt_slide["shapes"]
                if shape is not gt_shape
                and shape["_path"].startswith(prefix)
                and "/" not in shape["_path"][len(prefix):]]
