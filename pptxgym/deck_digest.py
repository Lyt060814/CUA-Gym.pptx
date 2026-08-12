"""Compatibility entry point for :mod:`pptxgym.office.deck_digest`."""
if __name__ == "__main__":
    import runpy
    runpy.run_module("pptxgym.office.deck_digest", run_name="__main__")
else:
    import sys
    from .office import deck_digest as _impl
    sys.modules[__name__] = _impl
