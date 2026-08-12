"""Compatibility alias for :mod:`pptxgym.management.control`."""
import sys
from .management import control as _impl
sys.modules[__name__] = _impl
