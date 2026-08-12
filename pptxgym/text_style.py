"""Compatibility alias for :mod:`pptxgym.office.text_style`."""
import sys
from .office import text_style as _impl
sys.modules[__name__] = _impl
