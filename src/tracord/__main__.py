"""Module entry point for ``python -m tracord``."""

from .cli import console_main


if __name__ == "__main__":
    raise SystemExit(console_main())
