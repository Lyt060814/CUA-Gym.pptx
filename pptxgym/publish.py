"""Compatibility entry point for :mod:`pptxgym.delivery.publish`."""
if __name__ == "__main__":
    import runpy
    runpy.run_module("pptxgym.delivery.publish", run_name="__main__")
else:
    import sys
    from .delivery import publish as _impl
    sys.modules[__name__] = _impl
