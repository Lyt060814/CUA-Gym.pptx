"""Compatibility alias for :mod:`pptxgym.delivery.vmsmoke`."""
import sys
from .delivery import vmsmoke as _impl
sys.modules[__name__] = _impl
