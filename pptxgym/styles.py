"""Compatibility alias for :mod:`pptxgym.office.styles`."""
import sys
from .office import styles as _impl
sys.modules[__name__] = _impl
