"""The package layout keeps implementation code inside domain packages."""

from __future__ import annotations

from pathlib import Path

import pptxgym


def test_implementation_modules_live_in_domain_packages():
    package = Path(pptxgym.__file__).parent
    assert {path.name for path in package.glob("*.py")} == {"__init__.py"}
    assert {path.name for path in package.iterdir() if path.is_dir() and
            not path.name.startswith("__pycache__")} == {
        "commands", "core", "delivery", "evaluation", "management",
        "office", "orchestration", "resources", "tasks",
    }


def test_agent_operator_skill_is_mirrored_for_both_harnesses():
    root = Path(__file__).resolve().parents[1]
    canonical = root / ".agents/skills/pptxgym-operator/SKILL.md"
    claude = root / ".claude/skills/pptxgym-operator/SKILL.md"
    assert canonical.read_bytes() == claude.read_bytes()
    text = canonical.read_text()
    assert "Never ask for a token in chat" in text
    assert "Do not turn a resume into a new run" in text


def test_installed_package_carries_agent_quick_start_resources():
    package = Path(pptxgym.__file__).parent
    agent = package / "resources/guides/AGENTS.md"
    skill = package / "resources/skills/pptxgym-operator/SKILL.md"
    manifest = package / "resources/skills/pptxgym-operator/agents/openai.yaml"
    assert "Resume an interrupted run" in agent.read_text()
    assert "Never ask for a token in chat" in skill.read_text()
    assert 'display_name: "CUA-Gym.pptx Operator"' in manifest.read_text()
