"""Compatibility alias for :mod:`pptxgym.orchestration.profiles`."""
import sys
from .orchestration import profiles as _impl
sys.modules[__name__] = _impl
