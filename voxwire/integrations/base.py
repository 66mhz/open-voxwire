"""Voxwire integrations — the flexibility layer.

An *integration* connects Voxwire to one of your systems or services. It is a
drop-in plugin: subclass `Integration`, declare how it's triggered and what it
does, and register it. Nothing is hardcoded into the gateway.

This is deliberately tiny and dependency-free so it's easy to read and extend.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Command:
    """A recognized voice command handed to an integration."""
    text: str                       # the (cleaned) transcript
    source: str = "unknown"         # which input produced it (e.g. "throat", "air")
    confidence: float = 1.0         # STT/decode confidence, 0..1
    meta: dict = field(default_factory=dict)


@dataclass
class Result:
    """What an integration did, spoken/shown back to the user."""
    ok: bool
    reply: str                      # short natural-language result
    requires_confirmation: bool = False   # destructive → gate before executing
    preview: str | None = None      # exact action shown at the confirmation gate


class Integration:
    """Base class for a Voxwire integration.

    Subclass and implement `matches` + `handle`. Keep `matches` cheap; it runs on
    every command. Anything destructive must return Result(requires_confirmation=
    True, preview=...) rather than acting immediately — the executor owns the gate.
    """

    #: short unique id, e.g. "slack", "home", "shell"
    name: str = "unnamed"

    #: optional wake words that route to this integration ("slack", "post to")
    wake_words: tuple[str, ...] = ()

    def matches(self, command: Command) -> bool:
        """Return True if this integration should handle the command."""
        t = command.text.lower()
        return any(w in t for w in self.wake_words)

    def handle(self, command: Command) -> Result:
        """Do the work. Override this."""
        raise NotImplementedError


# ── registry ────────────────────────────────────────────────────────────────
_REGISTRY: list[Integration] = []


def register(integration: Integration) -> Integration:
    """Register an integration instance so the gateway can route to it.

    Registering a `name` that is already present replaces the old instance, so
    reloading a plugin never leaves duplicates. Later registration wins ties.
    """
    _REGISTRY[:] = [i for i in _REGISTRY if i.name != integration.name]
    _REGISTRY.append(integration)
    return integration


def unregister(name: str) -> None:
    """Remove a registered integration by name (no-op if it isn't registered)."""
    _REGISTRY[:] = [i for i in _REGISTRY if i.name != name]


def registered() -> list[Integration]:
    """The integrations currently registered, in registration order."""
    return list(_REGISTRY)


def route(command: Command) -> Integration | None:
    """Find the integration that should handle a command (last match wins)."""
    for integ in reversed(_REGISTRY):
        try:
            if integ.matches(command):
                return integ
        except Exception:
            continue
    return None


# The reference integration lives in `echo.py` as a drop-in plugin — nothing is
# baked into this interface. Copy that file to build your own.
