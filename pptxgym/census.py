"""Compatibility entry point for :mod:`pptxgym.office.census`."""
if __name__ == "__main__":
    import runpy
    runpy.run_module("pptxgym.office.census", run_name="__main__")
else:
    import sys
    from .office import census as _impl
    sys.modules[__name__] = _impl
