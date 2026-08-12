"""Public package surface for CUA-Gym.pptx.

Implementation modules live in domain packages.  The lazy aliases preserve
the established ``from pptxgym import inventory`` style without importing the
entire pipeline when a caller needs one utility.
"""

from importlib import import_module


_MODULES = {
    "pipeline": "pptxgym.core.pipeline",
    "anim_steps": "pptxgym.office.anim_steps",
    "census": "pptxgym.office.census",
    "charts": "pptxgym.office.charts",
    "deck_digest": "pptxgym.office.deck_digest",
    "degrade_exec": "pptxgym.office.degrade_exec",
    "fonts": "pptxgym.office.fonts",
    "pkg_check": "pptxgym.office.pkg_check",
    "render": "pptxgym.office.render",
    "roundtrip": "pptxgym.office.roundtrip",
    "smartart": "pptxgym.office.smartart",
    "styles": "pptxgym.office.styles",
    "text_style": "pptxgym.office.text_style",
    "tools": "pptxgym.office.tools",
    "wps_roundtrip": "pptxgym.office.wps_roundtrip",
    "attacks": "pptxgym.evaluation.attacks",
    "comparators": "pptxgym.evaluation.comparators",
    "consistency": "pptxgym.evaluation.consistency",
    "inventory": "pptxgym.evaluation.inventory",
    "assets": "pptxgym.tasks.assets",
    "emit": "pptxgym.tasks.emit",
    "emit_tests": "pptxgym.tasks.emit_tests",
    "agent": "pptxgym.orchestration.agent",
    "escalate": "pptxgym.orchestration.escalate",
    "foreman": "pptxgym.orchestration.foreman",
    "mailbox": "pptxgym.orchestration.mailbox",
    "observe": "pptxgym.orchestration.observe",
    "profiles": "pptxgym.orchestration.profiles",
    "supervise": "pptxgym.orchestration.supervise",
    "corpus": "pptxgym.delivery.corpus",
    "publish": "pptxgym.delivery.publish",
    "vmsmoke": "pptxgym.delivery.vmsmoke",
    "config": "pptxgym.management.config",
    "control": "pptxgym.management.control",
}

__all__ = sorted(_MODULES)


def __getattr__(name):
    try:
        target = _MODULES[name]
    except KeyError:
        raise AttributeError(name) from None
    module = import_module(target)
    globals()[name] = module
    return module
