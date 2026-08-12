"""Compatibility entry point for :mod:`pptxgym.office.wps_roundtrip`."""
if __name__ == "__main__":
    import runpy
    runpy.run_module("pptxgym.office.wps_roundtrip", run_name="__main__")
else:
    import sys
    from .office import wps_roundtrip as _impl
    sys.modules[__name__] = _impl
