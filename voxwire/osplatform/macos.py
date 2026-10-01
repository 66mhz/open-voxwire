"""macOS platform backend — pbcopy + Command+V, Quartz/ApplicationServices
permission checks, and the seam for the native confirm gate.

Every native import (Quartz, ApplicationServices, pynput) stays INSIDE the
methods, so importing this module on Windows/Linux is harmless — `is_current()`
is False there and nothing pyobjc-shaped ever loads.
"""
from __future__ import annotations

from .base import (ConfirmRequest, PermState, PlatformBackend,
                   get_confirm_provider, register)


class MacOSBackend(PlatformBackend):
    name = "macos"
    platforms = ("darwin",)
    paste_modifier = "cmd"              # macOS pastes with Command+V

    def clipboard_argv(self) -> list[list[str]]:
        return [["pbcopy"]]

    def check_perms(self) -> PermState:
        # Quartz / ApplicationServices report the Input Monitoring + Accessibility
        # grants that pynput's global hotkey + synthetic paste require. Imported
        # lazily so a non-mac import never touches pyobjc.
        input_monitoring: bool | None = None
        accessibility: bool | None = None
        try:
            from Quartz import CGPreflightListenEventAccess
            input_monitoring = bool(CGPreflightListenEventAccess())
        except Exception:
            pass
        try:
            from ApplicationServices import AXIsProcessTrusted
            accessibility = bool(AXIsProcessTrusted())
        except Exception:
            try:
                from Quartz import CGPreflightPostEventAccess
                accessibility = bool(CGPreflightPostEventAccess())
            except Exception:
                pass
        return PermState(
            applicable=True, input_monitoring=input_monitoring,
            accessibility=accessibility,
            note="Grant Input Monitoring + Accessibility to the app running "
                 "Voxwire (System Settings › Privacy & Security).")

    def request_perms(self) -> dict:
        """Trigger the macOS permission prompts so this app lands in the lists."""
        out: dict = {"applicable": True}
        try:
            from Quartz import CGRequestListenEventAccess
            out["input_monitoring"] = bool(CGRequestListenEventAccess())
        except Exception as e:
            out["input_monitoring"] = f"err: {e}"
        try:
            from ApplicationServices import (AXIsProcessTrustedWithOptions,
                                             kAXTrustedCheckOptionPrompt)
            out["accessibility"] = bool(AXIsProcessTrustedWithOptions(
                {kAXTrustedCheckOptionPrompt: True}))
        except Exception as e:
            out["accessibility"] = f"err: {e}"
        return out

    # ── confirm gate ─────────────────────────────────────────────────────────
    def confirm(self, req: ConfirmRequest) -> bool:
        """Route to the native gate the GUI host registered (the menubar's
        NSAlert, kept as-is), or DENY when unwired — which is the default today.

        SECURITY (CLAUDE.md rule 3 / DESIGN §3): a registered provider is only
        the dialog; whether the executor may run at all is gated separately by
        `executor_gate_ready()`, which stays False until the dialog REJECTS
        synthetic input (kCGEventSourceStateHIDSystemState) so the agent cannot
        click its own Approve via CGEvent injection. That hardening + the wiring
        of a provider here are a separate, human-reviewed task (Epic 2 / R1) —
        deliberately NOT done in this change. Any error denies.
        """
        provider = get_confirm_provider()
        if provider is None:
            return False
        try:
            return bool(provider(req))
        except Exception:
            return False

    # executor_gate_ready() stays False (inherited): macOS's NSAlert does not yet
    # reject synthetic input (see menubar.native_confirm TODO). Until R1 lands the
    # executor stays disabled even on macOS — the more restrictive choice.


register(MacOSBackend())
