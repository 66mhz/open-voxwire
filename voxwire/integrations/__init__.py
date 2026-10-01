"""Voxwire integrations — the flexibility layer (drop-in plugins).

Each integration is a small module in this package that subclasses `Integration`
and calls `register(...)`. The gateway discovers and loads them; none is wired
into the core. See `base.py` for the interface and `echo.py` for a template.
"""
from .base import (  # noqa: F401  (re-exported for convenience)
    Command,
    Integration,
    Result,
    register,
    registered,
    route,
    unregister,
)
