"""Compatibility entry point for :mod:`pptxgym.office.pkg_check`."""
if __name__ == "__main__":
    import runpy
    runpy.run_module("pptxgym.office.pkg_check", run_name="__main__")
else:
    import sys
    from .office import pkg_check as _impl
    sys.modules[__name__] = _impl
