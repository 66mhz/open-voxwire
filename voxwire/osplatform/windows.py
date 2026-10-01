"""Windows platform backend — clip / pyperclip + Control+V.

A global keyboard hook + synthetic paste need no special OS grant on Windows, so
`check_perms()` reports "not applicable". The confirm gate is UNSUPPORTED here
(deny-by-default) — no native, synthetic-input-rejecting dialog is proven on
Windows yet, so the executor stays structurally disabled (CLAUDE.md rule 3 /
DESIGN §3). Native imports stay lazy.
"""
from __future__ import annotations

from .base import PermState, PlatformBackend, register


class WindowsBackend(PlatformBackend):
    name = "windows"
    platforms = ("win32", "cygwin")
    paste_modifier = "ctrl"             # Windows pastes with Control+V

    def clipboard_argv(self) -> list[list[str]]:
        # `clip.exe` ships with Windows and reads text on stdin. `pyperclip` is
        # the cross-fallback in PlatformBackend.copy() if clip is unavailable.
        return [["clip"]]

    def check_perms(self) -> PermState:
        return PermState(
            applicable=False,
            note="Windows global hooks + synthetic paste need no OS permission "
                 "grant.")

    # confirm() and executor_gate_ready() are inherited deny-by-default / False.
    # TODO(Epic 2 / R1 — CLAUDE.md rule 3, DESIGN §3): a native Windows confirm
    # dialog that REJECTS synthetic input must be built + human-reviewed before
    # any executor path may be enabled on Windows. Do not weaken this to ship.


register(WindowsBackend())
