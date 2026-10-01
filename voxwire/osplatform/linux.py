"""Linux platform backend — wl-copy / xclip / xsel + Control+V (X11 first).

An X11 global hook + synthetic paste generally need no OS grant, so `check_perms`
reports "not applicable" (with a Wayland caveat). Wayland deliberately restricts
global hooks + synthetic input, so the hotkey/paste may be a no-op under a strict
compositor — noted, not worked around here. The confirm gate is UNSUPPORTED
(deny-by-default) — no native, synthetic-input-rejecting dialog is proven on
Linux yet, so the executor stays structurally disabled (CLAUDE.md rule 3 /
DESIGN §3). Native imports stay lazy.
"""
from __future__ import annotations

import os

from .base import PermState, PlatformBackend, register


class LinuxBackend(PlatformBackend):
    name = "linux"
    platforms = ("linux",)              # matches "linux" and "linux2" by prefix
    paste_modifier = "ctrl"             # Linux pastes with Control+V

    def clipboard_argv(self) -> list[list[str]]:
        # Priority order; PlatformBackend.copy() uses the first one that is
        # present AND works. wl-copy first for Wayland; it fails fast on X11
        # (no WAYLAND_DISPLAY), falling through to xclip / xsel. pyperclip is the
        # final fallback in copy(). Static + pure so it stays unit-testable.
        return [["wl-copy"],
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"]]

    def check_perms(self) -> PermState:
        wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        note = ("Wayland restricts global hotkeys + synthetic paste; run under "
                "X11 (or wire a desktop portal) if the hotkey/paste do nothing."
                if wayland else
                "X11 global hooks + synthetic paste need no OS permission grant.")
        return PermState(applicable=False, note=note)

    # confirm() and executor_gate_ready() are inherited deny-by-default / False.
    # TODO(Epic 2 / R1 — CLAUDE.md rule 3, DESIGN §3): a native Linux confirm
    # dialog that REJECTS synthetic input (harder under X11's permissive input
    # model) must be built + human-reviewed before any executor path may be
    # enabled on Linux. Do not weaken this to ship.


register(LinuxBackend())
