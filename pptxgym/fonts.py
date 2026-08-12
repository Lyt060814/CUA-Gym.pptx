"""Compatibility entry point for :mod:`pptxgym.office.fonts`."""
if __name__ == "__main__":
    import runpy
    runpy.run_module("pptxgym.office.fonts", run_name="__main__")
else:
    import sys
    from .office import fonts as _impl
    sys.modules[__name__] = _impl
