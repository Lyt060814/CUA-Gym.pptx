"""Compatibility alias for :mod:`pptxgym.office.anim_steps`."""
import sys
from .office import anim_steps as _impl
sys.modules[__name__] = _impl
