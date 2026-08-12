"""The package layout is intentional, and old command imports stay usable."""

from __future__ import annotations

import importlib
from pathlib import Path

import pptxgym


def test_implementation_modules_live_in_domain_packages():
    package = Path(pptxgym.__file__).parent
    allowed = {"__init__.py"}
    compatibility = {f"{name}.py" for name in pptxgym._MODULES} | {"cli.py"}
    assert {path.name for path in package.glob("*.py")} == allowed | compatibility
    assert all((package / name).stat().st_size < 1_000 for name in compatibility)


def test_compatibility_imports_are_the_real_modules():
    assert all(target.startswith("pptxgym.") for target in pptxgym._MODULES.values())
    aliases = {**pptxgym._MODULES, "cli": "pptxgym.commands.cli"}
    for old, new in aliases.items():
        assert importlib.import_module(f"pptxgym.{old}") is importlib.import_module(
            new)


def test_lazy_package_exports_resolve_to_domain_modules():
    assert pptxgym.assets.__name__ == "pptxgym.tasks.assets"
    assert pptxgym.render.__name__ == "pptxgym.office.render"
    assert pptxgym.profiles.__name__ == "pptxgym.orchestration.profiles"


def test_agent_operator_skill_is_mirrored_for_both_harnesses():
    root = Path(__file__).resolve().parents[1]
    canonical = root / ".agents/skills/pptxgym-operator/SKILL.md"
    claude = root / ".claude/skills/pptxgym-operator/SKILL.md"
    assert canonical.read_bytes() == claude.read_bytes()
    text = canonical.read_text()
    assert "Never ask for a token in chat" in text
    assert "Do not turn a resume into a new run" in text
