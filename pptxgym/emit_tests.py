"""Compatibility entry point for :mod:`pptxgym.tasks.emit_tests`."""
if __name__ == "__main__":
    import runpy
    runpy.run_module("pptxgym.tasks.emit_tests", run_name="__main__")
else:
    import sys
    from .tasks import emit_tests as _impl
    sys.modules[__name__] = _impl
