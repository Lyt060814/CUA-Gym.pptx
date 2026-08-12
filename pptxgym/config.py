"""Compatibility alias for :mod:`pptxgym.management.config`."""
import sys
from .management import config as _impl
sys.modules[__name__] = _impl
