"""Voxwire platform layer — one OS desktop-integration interface, selected at
runtime from `sys.platform`.

The rest of the app calls `paste_text(text)`, `copy_text(text)`, `check_perms()`,
`request_perms()`, and `confirm(...)`, and never imports an OS-native module.
Which implementation runs is decided here from `sys.platform` — macOS, Windows,
or Linux (X11 first). Each backend keeps its native imports LAZY, so importing
this package (which imports all three backends so they self-register) is safe on
any OS.

    import osplatform
    osplatform.paste_text("hello ")     # clipboard + the OS paste chord
    osplatform.check_perms()            # {'applicable': ..., 'input_monitoring': ...}

This package is NOT named `platform`: `voxwire/` is a sys.path root, so a
top-level `platform` package would shadow the stdlib `platform` module that
`stt/mlx_backend.py` imports. See `base.py` / `docs/DESIGN.md`.
"""
from __future__ import annotations

import sys

from .base import (ConfirmRequest, PermState, PlatformBackend, _serves,
                   get_backend, get_confirm_provider, register, registered,
                   set_confirm_provider, unregister)

# Import the built-in backends so they self-register. Each keeps its native
# imports lazy, so importing all three on any OS is harmless.
from . import linux as _linux      # noqa: E402,F401
from . import macos as _macos      # noqa: E402,F401
from . import windows as _windows  # noqa: E402,F401

__all__ = [
    "PlatformBackend", "PermState", "ConfirmRequest",
    "register", "unregister", "registered", "get_backend",
    "set_confirm_provider", "get_confirm_provider",
    "select", "current", "name", "paste_modifier",
    "copy_text", "paste_text",
    "check_perms", "request_perms", "confirm", "executor_gate_ready",
]


def select(platform: str) -> PlatformBackend | None:
    """The first registered backend that serves `platform` (a `sys.platform`
    string), else None. Pure over the registry — unit-testable with a fake
    platform string (mirrors `stt._pick_default`)."""
    return next((b for b in registered() if _serves(b.platforms, platform)), None)


# The chosen backend is cached — the OS never changes for the life of a process.
_current: PlatformBackend | None = None


def current() -> PlatformBackend:
    """The platform backend for the OS we're running on.

    Raises RuntimeError on an unsupported OS rather than guessing — the public
    helpers below catch that and degrade (leave text on the clipboard, deny the
    confirm), never emit a wrong keystroke."""
    global _current
    if _current is None:
        _current = select(sys.platform)
    if _current is None:
        raise RuntimeError(
            f"no Voxwire platform backend for {sys.platform!r}; "
            f"registered: {[b.name for b in registered()]}")
    return _current


def name() -> str | None:
    """Name of the backend serving this OS ("macos"/"windows"/"linux"), or None."""
    b = select(sys.platform)
    return b.name if b else None


def paste_modifier() -> str | None:
    """The paste-chord modifier on this OS ("cmd" on macOS, "ctrl" elsewhere)."""
    b = select(sys.platform)
    return b.paste_modifier if b else None


def copy_text(text: str) -> bool:
    """Put `text` on the clipboard. False (degrades) if unsupported/unavailable."""
    try:
        return current().copy(text)
    except Exception:
        return False


def paste_text(text: str) -> bool:
    """Copy `text` and paste it into the frontmost app. False if it couldn't."""
    try:
        return current().paste(text)
    except Exception:
        return False


def check_perms() -> dict:
    """The dictation permission state on this OS, as a flat dict for the API."""
    try:
        return current().check_perms().as_dict()
    except Exception:
        return PermState(applicable=False,
                         note=f"unsupported OS {sys.platform!r}").as_dict()


def request_perms() -> dict:
    """Trigger any OS permission prompts. `{'applicable': False}` where none."""
    try:
        return current().request_perms()
    except Exception:
        return {"applicable": False}


def confirm(title: str, message: str, timeout: float = 120.0) -> bool:
    """Put a destructive action to the human at the native gate. Deny-by-default:
    False on any error or on an OS without a proven gate (CLAUDE.md rule 3)."""
    try:
        return current().confirm(ConfirmRequest(title, message, timeout))
    except Exception:
        return False


def executor_gate_ready() -> bool:
    """Whether this OS's confirm gate is hardened enough to enable the executor.
    False everywhere until the human-reviewed per-OS gate lands (rule 3)."""
    try:
        return current().executor_gate_ready()
    except Exception:
        return False
