"""Compatibility alias for :mod:`pptxgym.orchestration.escalate`."""
import sys
from .orchestration import escalate as _impl
sys.modules[__name__] = _impl
