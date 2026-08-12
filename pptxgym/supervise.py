"""Compatibility entry point for :mod:`pptxgym.orchestration.supervise`."""
if __name__ == "__main__":
    import runpy
    runpy.run_module("pptxgym.orchestration.supervise", run_name="__main__")
else:
    import sys
    from .orchestration import supervise as _impl
    sys.modules[__name__] = _impl
