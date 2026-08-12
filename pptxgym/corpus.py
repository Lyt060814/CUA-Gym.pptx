"""Compatibility entry point for :mod:`pptxgym.delivery.corpus`."""
if __name__ == "__main__":
    import runpy
    runpy.run_module("pptxgym.delivery.corpus", run_name="__main__")
else:
    import sys
    from .delivery import corpus as _impl
    sys.modules[__name__] = _impl
