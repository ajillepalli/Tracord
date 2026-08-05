"""Packaged JSON Schema resources for Tracord contracts."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def schema_resource(name: str):
    """Return the traversable resource for a packaged schema name."""
    return files(__package__).joinpath(name)


def schema_path(name: str) -> Path:
    """Return a filesystem path when package resources are unpacked."""
    return Path(str(schema_resource(name)))

