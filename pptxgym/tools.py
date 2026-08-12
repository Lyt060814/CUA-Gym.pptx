"""Compatibility entry point for :mod:`pptxgym.office.tools`."""
if __name__ == "__main__":
    import runpy
    runpy.run_module("pptxgym.office.tools", run_name="__main__")
else:
    import sys
    from .office import tools as _impl
    sys.modules[__name__] = _impl
