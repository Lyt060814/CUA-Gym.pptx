"""Compatibility entry point for :mod:`pptxgym.office.roundtrip`."""
if __name__ == "__main__":
    import runpy
    runpy.run_module("pptxgym.office.roundtrip", run_name="__main__")
else:
    import sys
    from .office import roundtrip as _impl
    sys.modules[__name__] = _impl
