"""Compatibility alias for :mod:`pptxgym.core.pipeline`."""
import sys
from .core import pipeline as _impl
sys.modules[__name__] = _impl
