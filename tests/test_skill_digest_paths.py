"""The skills quote digest fields by path; the digest changes underneath them.

`renderer_drift` was flat (`renderer_drift.drift_in.p90_in`) until it was split
per renderer.  The recipe skill kept quoting the old path for a while, which
reads as a real rule and silently resolves to nothing — the agent then invents
an amplitude floor instead of computing one.  Nothing fails when that happens:
a skill file is prose, not code.  This test is the thing that fails instead.

    python3 -m pytest tests/ -q
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / ".claude" / "skills"
DIGEST = ROOT / "work" / "deck0001" / "digest.json"

# `renderer_drift` plus the dotted segments that follow it.  Stops at anything
# that is not an identifier, so prose like "`renderer_drift.libreoffice.*` has
# exactly one use" contributes the path `renderer_drift.libreoffice` and
# nothing more.
PATH_RE = re.compile(r"renderer_drift((?:\.[A-Za-z_][A-Za-z0-9_]*)+)")


def _cited_paths():
    """Every `renderer_drift.…` path quoted in a skill file, with its site."""
    for md in sorted(SKILLS.glob("**/*.md")):
        for lineno, line in enumerate(md.read_text().splitlines(), 1):
            for m in PATH_RE.finditer(line):
                yield md, lineno, ["renderer_drift"] + m.group(1).split(".")[1:]


def _missing_segment(root: dict, segments):
    """The first segment that a real digest does not have, or None.

    An empty container is not a missing field: WPS measures clean on every
    deck so far, so `renderer_drift.wps.drift_in` is legitimately `{}` and
    `…drift_in.p90_in` has nothing to resolve against.  That is the case the
    skill is required to handle in prose, not a stale path.
    """
    node = root
    for i, seg in enumerate(segments):
        if not isinstance(node, dict):
            return ".".join(segments[:i + 1])
        if not node:                      # measured, nothing to report
            return None
        if seg not in node:
            return ".".join(segments[:i + 1])
        node = node[seg]
    return None


@pytest.mark.corpus
def test_skills_only_cite_digest_fields_that_exist():
    if not DIGEST.exists():
        pytest.skip(f"no reference digest at {DIGEST} — run `inspect` first")
    summary = json.loads(DIGEST.read_text())["deck_summary"]
    root = {"renderer_drift": summary["renderer_drift"]}

    broken = []
    for md, lineno, segments in _cited_paths():
        missing = _missing_segment(root, segments)
        if missing:
            broken.append(f"{md.relative_to(ROOT)}:{lineno} cites "
                          f"`{'.'.join(segments)}` — `{missing}` is not in "
                          f"{DIGEST.relative_to(ROOT)}")
    assert not broken, (
        "skill files quote digest paths that no longer resolve:\n  "
        + "\n  ".join(broken)
        + "\n(the digest schema moved; rekey the prose onto the new field)")
