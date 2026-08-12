"""Compatibility alias for :mod:`pptxgym.office.render`."""
import sys
from .office import render as _impl
sys.modules[__name__] = _impl
