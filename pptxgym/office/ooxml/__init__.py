"""Shared low-level OOXML helpers used by producers and evaluators."""

from .package import Package, closure, relationship_part, resolve_target

__all__ = ["Package", "closure", "relationship_part", "resolve_target"]
