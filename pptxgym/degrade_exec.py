"""Compatibility entry point for :mod:`pptxgym.office.degrade_exec`."""
if __name__ == "__main__":
    import runpy
    runpy.run_module("pptxgym.office.degrade_exec", run_name="__main__")
else:
    import sys
    from .office import degrade_exec as _impl
    sys.modules[__name__] = _impl
