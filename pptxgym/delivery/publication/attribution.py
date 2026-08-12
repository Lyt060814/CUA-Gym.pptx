"""Source provenance and licence attribution for published tasks."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

ZENODO_API = "https://zenodo.org/api/records/"

ATTRIBUTION = """\
# Source and licence

This task is a **modified copy** of a third-party presentation. The deck was
deliberately damaged; the original is unaltered at the source below.

- **Title:** {title}
- **Creators:** {creators}
- **DOI:** [{doi}](https://doi.org/{doi_bare})
- **Original licence:** {license}

The original licence governs this derivative. Attribution is to the creators
above, not to this dataset.

**Note on embedded media.** The licence stated by the depositor covers the
presentation. Photographs, figures and logos embedded inside it may belong to
third parties and may carry their own terms; that is not resolvable from the
source metadata, and it is unchanged by our modifications.
"""


def provenance_of(deck) -> dict | None:
    """Return a deck's source record when it exists and is valid JSON."""
    path = deck.root / "provenance.json"
    if not path.exists():
        return None
    try:
        provenance = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    return provenance if isinstance(provenance, dict) else None


def zenodo_creators(doi: str, cache: Path) -> list[str]:
    """Fetch creator names once per DOI and retain them in a local cache."""
    store = {}
    if cache.exists():
        try:
            store = json.loads(cache.read_text())
        except json.JSONDecodeError:
            store = {}
    if doi in store:
        return store[doi]

    match = re.search(r"zenodo\.(\d+)", doi or "")
    if not match:
        return []
    try:
        with urllib.request.urlopen(ZENODO_API + match.group(1), timeout=30) as response:
            record = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return []
    names = [creator.get("name", "") for creator in
             (record.get("metadata") or {}).get("creators") or []]
    names = [name for name in names if name]
    store[doi] = names
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(store, ensure_ascii=False, indent=1))
    return names


def attribution_md(provenance: dict, creators: list[str]) -> str:
    doi = (provenance.get("doi") or "").replace("https://doi.org/", "")
    return ATTRIBUTION.format(
        title=provenance.get("title") or "(untitled)",
        creators="; ".join(creators) or "(not recorded)",
        doi=provenance.get("doi") or "(none)",
        doi_bare=doi,
        license=provenance.get("license") or "(unstated)",
    )
