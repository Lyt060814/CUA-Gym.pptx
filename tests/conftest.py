"""Two kinds of test, kept apart.

The suite used to read `work/` — the live pipeline directory — as though it
were a fixture.  "The tests pass" then meant *the code is correct **and** no
deck moved since yesterday*, and those are not the same claim.  On the day
this file was written the baseline was seven failures, and not one of them was
a regression: five were one task-authoring defect in `deck0008` and two were
properties whose only specimen a repair had since mended.  A failure that
cannot be told apart from a repair is not evidence.

So:

* **Default** — everything runs against `tests/fixtures/minidecks.py`, which
  builds its decks from nothing.  A green run here is a claim about the code
  and about nothing else, and re-running any pipeline stage cannot change it.

* **`--corpus`** — additionally runs the tests marked `@pytest.mark.corpus`,
  which read `work/` on purpose: whether *these ten decks* are shippable,
  what the repairer has and has not fixed, which historical defect is still
  present.  Real questions, worth asking, and answered by a command that says
  it is asking them.  They are `skip`ped rather than deselected by default so
  the count is visible instead of silently absent.

`pytest tests/ -q`            the code
`pytest tests/ -q --corpus`   the code and the ten decks currently in `work/`
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))


def pytest_addoption(parser):
    parser.addoption(
        "--corpus", action="store_true", default=False,
        help="also run the tests that read the live `work/` directory")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "corpus: reads the live `work/` tree; skipped unless --corpus is given")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--corpus"):
        return
    skip = pytest.mark.skip(reason="corpus test: reads live work/, needs --corpus")
    for item in items:
        if "corpus" in item.keywords:
            item.add_marker(skip)


# --------------------------------------------------------------------------- #
# the frozen decks
# --------------------------------------------------------------------------- #


class MiniCorpus:
    """The miniature decks, parsed once.

    Same shape as the `_deck` helper the corpus tests use — a plan and the two
    inventories — so a fixture-based test and its corpus twin read the same
    way.  The plan is deep-copied on the way out because several tests mutate
    it.
    """

    def __init__(self, roots: dict[str, Path]):
        self.roots = roots
        self._cache: dict[str, tuple] = {}

    def root(self, name: str) -> Path:
        return self.roots[name]

    def _parse(self, name: str):
        if name not in self._cache:
            from pptxgym import comparators as C
            from pptxgym.inventory import inventory_pptx
            root = self.roots[name]
            self._cache[name] = (C.build_plan(root, write=False),
                                 inventory_pptx(root / "source.pptx"),
                                 inventory_pptx(root / "input.pptx"))
        return self._cache[name]

    def __call__(self, name: str):
        plan, gt, init = self._parse(name)
        return copy.deepcopy(plan), gt, init

    def accepted(self, name: str):
        """A plan with the `plan_accepted` gate stood down.

        For the decks whose refusal is the point of the deck (`mini_excused`,
        `mini_high_floor`) but whose *machinery* a test needs anyway — the
        same stand-down the corpus tests do through `_specimen`.
        """
        plan, gt, init = self(name)
        return {**plan, "rejected": []}, gt, init


@pytest.fixture(scope="session")
def mini(tmp_path_factory) -> MiniCorpus:
    import minidecks
    base = tmp_path_factory.mktemp("minidecks")
    return MiniCorpus(minidecks.build_all(base))


@pytest.fixture(scope="session")
def mini_work(mini) -> Path:
    """A `work/`-shaped tree of frozen decks, planned and bundled.

    For the stages downstream of scoring — `emit`, `emit_tests`, `pkg_check` —
    whose fixtures used to reach into `work/` and *choose* a deck: the
    cheapest one, the one with the richest operator mix, the first one whose
    plan was not rejected.  A repair that rejects a deck therefore changed
    which deck those sixty-odd tests were about, silently, and the emitter
    tests' subject drifted with the corpus.

    Here the choice is fixed: `deck9001` (`mini_plain`) is the run-level mix
    and `deck9005` (`mini_inherited`) is the one without, and neither moves.
    """
    import minidecks
    return minidecks.promote(next(iter(mini.roots.values())).parent)
