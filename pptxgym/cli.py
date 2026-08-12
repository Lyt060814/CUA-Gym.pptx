"""Compatibility entry point for :mod:`pptxgym.commands.cli`."""
if __name__ == "__main__":
    from .commands.cli import main
    main()
else:
    import sys
    from .commands import cli as _impl
    sys.modules[__name__] = _impl
