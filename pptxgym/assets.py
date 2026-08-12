"""Compatibility alias for :mod:`pptxgym.tasks.assets`."""
import sys
from .tasks import assets as _impl
sys.modules[__name__] = _impl
