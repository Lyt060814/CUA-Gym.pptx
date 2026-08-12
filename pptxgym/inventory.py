"""Compatibility entry point for :mod:`pptxgym.evaluation.inventory`."""
if __name__ == "__main__":
    import runpy
    runpy.run_module("pptxgym.evaluation.inventory", run_name="__main__")
else:
    import sys
    from .evaluation import inventory as _impl
    sys.modules[__name__] = _impl
