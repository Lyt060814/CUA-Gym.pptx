"""Compatibility alias for :mod:`pptxgym.orchestration.agent`."""
import sys
from .orchestration import agent as _impl
sys.modules[__name__] = _impl
