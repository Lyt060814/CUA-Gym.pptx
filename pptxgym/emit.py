"""Compatibility entry point for :mod:`pptxgym.tasks.emit`."""
if __name__ == "__main__":
    import runpy
    runpy.run_module("pptxgym.tasks.emit", run_name="__main__")
else:
    import sys
    from .tasks import emit as _impl
    sys.modules[__name__] = _impl
