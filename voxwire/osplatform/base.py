"""Voxwire platform layer — the OS desktop-integration interface + registry.

A *platform backend* is one operating system's implementation of the handful of
native things Voxwire's dictation loop needs: put text on the clipboard, paste it
into the frontmost app, report/request the OS permissions that requires, and put
a destructive action to the human at a native gate. Voxwire calls one interface;
the backend that serves the current OS is chosen at runtime from `sys.platform`.

This mirrors `voxwire/stt/base.py`: a tiny, dependency-free interface plus a
registry. Every OS-native import (pynput, pyobjc/Quartz, a clipboard subprocess)
stays LAZY — inside the methods — so importing ANY backend module on the "wrong"
OS is harmless and never raises (a Windows box can import the macOS backend; it
simply reports `is_current()` False and never touches pyobjc). The flexibility
lives here; nothing above names an OS.

Why this package is `osplatform`, not `platform`: `voxwire/` is a sys.path root,
so a top-level package literally named `platform` would SHADOW the stdlib
`platform` module — which `stt/mlx_backend.py` imports for `platform.machine()`.
That shadow silently breaks MLX availability detection on Apple Silicon. See
`docs/DESIGN.md` (Platform layer).
"""
from __future__ import annotations

import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable


@dataclass
class PermState:
    """Whether the dictation hotkey + synthetic paste have the OS grants they
    need on this machine.

    `applicable` is False on OSes where a global keyboard hook + synthetic paste
    need no explicit user grant (Windows, and X11 Linux); the two grant booleans
    are then None and `note` explains. On macOS the booleans mirror the Input
    Monitoring + Accessibility grants (None if they can't be read).
    """
    applicable: bool
    input_monitoring: bool | None = None
    accessibility: bool | None = None
    note: str = ""

    def as_dict(self) -> dict:
        """Flat dict for the JSON API. Keeps `input_monitoring`/`accessibility`
        as top-level keys so existing clients (the menubar) keep working."""
        return {"applicable": self.applicable,
                "input_monitoring": self.input_monitoring,
                "accessibility": self.accessibility,
                "note": self.note}


@dataclass
class ConfirmRequest:
    """One destructive action put to the human at the native confirm gate.

    This is the primitive the FUTURE executor calls before touching a machine.
    `title`/`message` are shown verbatim; nothing on this object can approve
    itself — approval comes only from a human at a native dialog (see
    `PlatformBackend.confirm`, CLAUDE.md rule 3, DESIGN §3).
    """
    title: str
    message: str
    timeout: float = 120.0


class PlatformBackend(ABC):
    """One operating system's desktop integration.

    Subclasses declare `name`, the `platforms` they serve, and the `paste_modifier`
    for their paste chord, then implement `clipboard_argv` + `check_perms`. The
    clipboard/paste plumbing is shared here (only the command + modifier differ).
    Keep every native import LAZY.
    """

    #: short unique id, referenced by selection + the JSON API
    name: str = "unnamed"

    #: `sys.platform` values this backend serves — matched exactly OR by prefix
    #: (so "linux2"/"linux" both hit the Linux backend). e.g. ("linux",)
    platforms: tuple[str, ...] = ()

    #: the modifier held during the paste chord. macOS pastes with Command+V;
    #: Windows and Linux paste with Control+V. Named as a `pynput.keyboard.Key`
    #: attribute ("cmd" / "ctrl").
    paste_modifier: str = "ctrl"

    # ── OS selection ─────────────────────────────────────────────────────────
    def is_current(self) -> bool:
        """True iff this backend serves the OS we're running on. Never raises."""
        return _serves(self.platforms, sys.platform)

    # ── clipboard ────────────────────────────────────────────────────────────
    @abstractmethod
    def clipboard_argv(self) -> list[list[str]]:
        """Candidate CLI commands (argv lists) that read UTF-8 text on stdin and
        put it on the system clipboard, in priority order (the first one actually
        present + working wins). PURE — no I/O — so each OS's command construction
        is unit-testable without a real clipboard."""

    def copy(self, text: str) -> bool:
        """Put `text` on the clipboard. Tries each `clipboard_argv()` command in
        order, then a `pyperclip` fallback. Returns False (degrades gracefully,
        exactly like the old macOS-only code) if none of the tooling is present."""
        data = (text or "").encode()
        for argv in self.clipboard_argv():
            try:
                subprocess.run(argv, input=data, check=True)
                return True
            except FileNotFoundError:
                continue        # this clipboard tool isn't installed — try the next
            except Exception:
                continue        # it failed (e.g. wl-copy on an X11 session) — next
        try:
            import pyperclip     # optional cross-platform fallback, imported lazily
            pyperclip.copy(text or "")
            return True
        except Exception:
            return False

    # ── paste ────────────────────────────────────────────────────────────────
    def paste(self, text: str) -> bool:
        """Copy `text`, then synthesize the paste chord (modifier+V) into the
        frontmost app via pynput. Returns False if copying failed or pynput is
        unavailable — the caller then just leaves the text on the clipboard.
        Needs the OS input-synthesis grant where one applies (see `check_perms`)."""
        if not self.copy(text):
            return False
        try:
            from pynput.keyboard import Controller, Key
            modifier = getattr(Key, self.paste_modifier)
            time.sleep(0.05)
            kbd = Controller()
            with kbd.pressed(modifier):
                kbd.press("v")
                kbd.release("v")
            return True
        except Exception:
            return False

    # ── permissions ──────────────────────────────────────────────────────────
    @abstractmethod
    def check_perms(self) -> PermState:
        """Report whether the hotkey + paste have the grants they need here.
        Must not raise (unreadable grants → None, not an exception)."""

    def request_perms(self) -> dict:
        """Trigger any OS permission prompts. Default: nothing to request."""
        return {"applicable": False}

    # ── confirm gate — SECURITY-CRITICAL (CLAUDE.md rule 3 / DESIGN §3) ──────
    def executor_gate_ready(self) -> bool:
        """Whether THIS OS has a native confirm gate hardened enough — one that
        REJECTS synthetic input — to let the executor be enabled.

        Deny-by-default: False everywhere until a per-OS hardened, human-reviewed
        gate lands (Epic 2 / R1). While this is False the executor stays
        STRUCTURALLY disabled on this OS — the security model never depends on a
        prompt. Do not flip this to make a feature work (rule 3)."""
        return False

    def confirm(self, req: ConfirmRequest) -> bool:
        """Put a destructive action to the human and return their decision.

        Deny-by-default: returns False unless an OS with a proven native gate
        overrides this. The agent can NEVER reach a path that returns True on its
        own behalf — approval is a human at a native dialog that the agent's
        synthetic input cannot drive. Any error denies."""
        return False


# ── selection helper (pure, shared) ─────────────────────────────────────────
def _serves(platforms: tuple[str, ...], platform: str) -> bool:
    """True iff `platform` (a `sys.platform` string) is served by `platforms`,
    matched exactly or by prefix. Pure + tiny so selection is unit-testable."""
    return any(platform == p or platform.startswith(p) for p in platforms)


# ── backend registry (mirrors stt.base / integrations.base) ──────────────────
_BACKENDS: list[PlatformBackend] = []


def register(backend: PlatformBackend) -> PlatformBackend:
    """Register a backend. Re-registering a name replaces it (no duplicates)."""
    _BACKENDS[:] = [b for b in _BACKENDS if b.name != backend.name]
    _BACKENDS.append(backend)
    return backend


def unregister(name: str) -> None:
    """Remove a registered backend by name (no-op if absent)."""
    _BACKENDS[:] = [b for b in _BACKENDS if b.name != name]


def registered() -> list[PlatformBackend]:
    """Every registered backend, in registration order."""
    return list(_BACKENDS)


def get_backend(name: str) -> PlatformBackend | None:
    """The registered backend with this name, or None."""
    return next((b for b in _BACKENDS if b.name == name), None)


# ── native confirm provider seam (deferred wiring, deny-by-default) ──────────
# The GUI host that owns a hardened native dialog (the menubar on macOS) may
# register it here so `confirm()` can route to it. It is UNWIRED by default, so
# every backend's confirm() denies out of the box. Wiring a provider is part of
# the separate, human-reviewed executor task (Epic 2 / R1) and MUST only supply a
# dialog that rejects synthetic input. Registering a non-native, agent-drivable
# provider would be a rule-3 violation — never do that.
_confirm_provider: Callable[[ConfirmRequest], bool] | None = None


def set_confirm_provider(fn: Callable[[ConfirmRequest], bool] | None) -> None:
    """Register (or clear, with None) the native confirm provider. See above."""
    global _confirm_provider
    _confirm_provider = fn


def get_confirm_provider() -> Callable[[ConfirmRequest], bool] | None:
    """The registered native confirm provider, or None (the default → deny)."""
    return _confirm_provider
