"""The rubric's list of scored properties against the comparator's facets.

Two implementations of "what gets scored" drift, and drift silently. That is
the lesson `tools/` taught by dying with none of a session's fixes in it, and
the solvability probe was the surviving instance: it refused a deck because a
rebuilt table's **column widths** were not pinned, when `_facet_table_all`
compares row and column counts and cell text and has never looked at a column
width.

Fixing that by editing prose leaves the same hazard one commit later. These
tests fail when a facet is added, removed or renamed without the rubric being
told — which is the only moment at which the two can be brought back together
cheaply.
"""
from pathlib import Path
import re

import pptxgym.comparators as C

SKILL = (Path(__file__).resolve().parents[1]
         / ".claude" / "skills" / "ppt-task-solvability" / "SKILL.md")

#: facet function -> the row of the rubric's table that speaks for it.
#: A facet with no entry here is a property the probe has never been told
#: about, which is exactly the condition these tests exist to catch.
SPOKEN_FOR = {
    "_facet_centre": "position",
    "_facet_extent": "size",
    "_facet_text": "text",
    "_facet_run_props": "text",
    "_facet_fill": "fill, line, effects",
    "_facet_line": "fill, line, effects",
    "_facet_effects": "fill, line, effects",
    "_facet_picture": "picture",
    "_facet_crop": "crop",
    "_facet_table_all": "table",
    "_facet_chart_all": "chart",
    "_facet_diagram": "diagram / SmartArt",
    "_facet_diagram_all": "diagram / SmartArt",
    "_facet_connector": "connector",
    "_facet_equation": "equation",
    "_facet_geom": "identity",
    "_facet_members": "identity",
    "_facet_rebuilt_composite": "identity",
}


def _facets() -> set[str]:
    return {n for n in dir(C) if n.startswith("_facet_")}


def test_every_facet_the_comparator_has_is_named_in_the_rubric():
    missing = sorted(_facets() - set(SPOKEN_FOR))
    assert not missing, (
        f"the comparator scores {missing} and the solvability rubric has "
        f"never been told — a probe cannot judge determinacy against a "
        f"property it does not know is measured")


def test_the_rubric_names_no_facet_the_comparator_dropped():
    stale = sorted(set(SPOKEN_FOR) - _facets())
    assert not stale, (
        f"the rubric still speaks for {stale}, which the comparator no longer "
        f"has — the probe would refuse tasks over properties nobody scores")


def test_each_row_the_rubric_promises_is_actually_in_it():
    text = SKILL.read_text(encoding="utf-8")
    for row in sorted(set(SPOKEN_FOR.values())):
        assert row in text, f"the rubric no longer has a row for {row!r}"


def test_the_rubric_says_column_widths_are_not_a_gap():
    """The specific false refusal that motivated all of this."""
    text = SKILL.read_text(encoding="utf-8")
    assert "column widths" in text
    assert re.search(r"row and column \*\*counts\*\*", text), (
        "the table row must say counts, because that is what "
        "_facet_table_all actually compares")


def test_the_table_facet_still_ignores_column_widths():
    """If this ever stops being true, the rubric above becomes the wrong
    advice and a real gap starts being waved through."""
    import inspect
    src = inspect.getsource(C._facet_table_all)
    assert "n_rows" in src and "n_cols" in src
    assert "width" not in src.lower(), (
        "_facet_table_all now looks at a width — the rubric tells probes it "
        "does not, and that is now a lie in the direction that loses marks")
