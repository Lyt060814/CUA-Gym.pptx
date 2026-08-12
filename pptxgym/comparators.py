"""Compatibility alias for :mod:`pptxgym.evaluation.comparators`."""
import sys
from .evaluation import comparators as _impl
sys.modules[__name__] = _impl
