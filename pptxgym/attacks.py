"""Compatibility entry point for :mod:`pptxgym.evaluation.attacks`."""
if __name__ == "__main__":
    import runpy
    runpy.run_module("pptxgym.evaluation.attacks", run_name="__main__")
else:
    import sys
    from .evaluation import attacks as _impl
    sys.modules[__name__] = _impl
