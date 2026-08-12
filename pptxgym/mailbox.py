"""Compatibility alias for :mod:`pptxgym.orchestration.mailbox`."""
import sys
from .orchestration import mailbox as _impl
sys.modules[__name__] = _impl
