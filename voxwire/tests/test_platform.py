"""Platform-layer tests (BYT-132 — the platform layer).

Covers the point of the abstraction: backends self-register, OS selection is a
pure/testable rule, the clipboard command + paste modifier are constructed
correctly per OS, and — SECURITY-CRITICAL — the confirm gate is deny-by-default
everywhere and the executor stays structurally disabled. All WITHOUT a GUI, a
real tray, or a real clipboard (fakes + monkeypatched sys.platform), so these run
on any OS in CI.
"""
import contextlib
import sys
import types

import pytest

import osplatform
from osplatform import base
from osplatform.base import ConfirmRequest, _serves


@pytest.fixture
def clean_provider():
    """Snapshot + restore the native confirm provider so a test can register a
    throwaway one without leaking it into the next test."""
    prev = osplatform.get_confirm_provider()
    yield
    osplatform.set_confirm_provider(prev)


# ── backends register + declare coherent data ───────────────────────────────
def test_builtin_backends_self_register():
    names = {b.name for b in osplatform.registered()}
    assert {"macos", "windows", "linux"} <= names


def test_every_backend_declares_platforms_and_a_sane_modifier():
    for b in osplatform.registered():
        assert b.platforms, f"{b.name!r} serves no platforms"
        assert b.paste_modifier in {"cmd", "ctrl"}, \
            f"{b.name!r} has an odd paste modifier {b.paste_modifier!r}"


# ── OS selection is a pure, testable rule ───────────────────────────────────
def test_serves_matches_exact_and_prefix():
    assert _serves(("linux",), "linux2") is True         # prefix (py<3.3 style)
    assert _serves(("linux",), "linux") is True          # exact
    assert _serves(("win32", "cygwin"), "cygwin") is True
    assert _serves(("darwin",), "win32") is False
    assert _serves((), "anything") is False


@pytest.mark.parametrize("platform,expect", [
    ("darwin", "macos"),
    ("win32", "windows"),
    ("cygwin", "windows"),
    ("linux", "linux"),
    ("linux2", "linux"),
])
def test_select_picks_backend_by_platform(platform, expect):
    b = osplatform.select(platform)
    assert b is not None and b.name == expect


def test_select_unknown_os_is_none():
    assert osplatform.select("sunos5") is None


def test_is_current_tracks_sys_platform(monkeypatch):
    monkeypatch.setattr(base.sys, "platform", "darwin")
    assert osplatform.get_backend("macos").is_current() is True
    assert osplatform.get_backend("windows").is_current() is False
    monkeypatch.setattr(base.sys, "platform", "win32")
    assert osplatform.get_backend("macos").is_current() is False
    assert osplatform.get_backend("windows").is_current() is True


# ── clipboard command + paste modifier construction (per OS) ────────────────
def test_macos_clipboard_and_modifier():
    b = osplatform.get_backend("macos")
    assert b.paste_modifier == "cmd"                 # macOS pastes with Command+V
    assert b.clipboard_argv() == [["pbcopy"]]


def test_windows_clipboard_and_modifier():
    b = osplatform.get_backend("windows")
    assert b.paste_modifier == "ctrl"                # Windows pastes with Control+V
    assert b.clipboard_argv() == [["clip"]]


def test_linux_clipboard_and_modifier():
    b = osplatform.get_backend("linux")
    assert b.paste_modifier == "ctrl"                # Linux pastes with Control+V
    argv = b.clipboard_argv()
    assert argv[0] == ["wl-copy"]                    # Wayland first
    assert ["xclip", "-selection", "clipboard"] in argv


def test_copy_uses_first_working_command(monkeypatch):
    calls = []

    def fake_run(argv, input=None, check=False):
        calls.append(argv)
        if argv == ["wl-copy"]:
            raise FileNotFoundError()                # wl-copy not installed → skip
        return None                                  # xclip "works"

    monkeypatch.setattr(base.subprocess, "run", fake_run)
    assert osplatform.get_backend("linux").copy("hi") is True
    assert calls == [["wl-copy"], ["xclip", "-selection", "clipboard"]]


def test_copy_falls_back_to_pyperclip(monkeypatch):
    def fake_run(*a, **k):
        raise FileNotFoundError()                    # no CLI clipboard tool present

    monkeypatch.setattr(base.subprocess, "run", fake_run)
    captured = {}
    fake = types.ModuleType("pyperclip")
    fake.copy = lambda t: captured.__setitem__("t", t)
    monkeypatch.setitem(sys.modules, "pyperclip", fake)
    assert osplatform.get_backend("windows").copy("clipme") is True
    assert captured["t"] == "clipme"


def test_copy_degrades_to_false_without_tooling(monkeypatch):
    def fake_run(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(base.subprocess, "run", fake_run)
    fake = types.ModuleType("pyperclip")

    def _boom(_):
        raise RuntimeError("no clipboard here")

    fake.copy = _boom
    monkeypatch.setitem(sys.modules, "pyperclip", fake)
    assert osplatform.get_backend("linux").copy("hi") is False


def _install_fake_pynput(monkeypatch) -> dict:
    """Install a fake pynput.keyboard that records the modifier + keys pressed,
    so `paste()`'s chord construction is testable without a real keyboard."""
    rec: dict = {}

    class Key:
        cmd = "MOD.cmd"
        ctrl = "MOD.ctrl"

    class Controller:
        @contextlib.contextmanager
        def pressed(self, mod):
            rec["mod"] = mod
            yield

        def press(self, k):
            rec.setdefault("pressed", []).append(k)

        def release(self, k):
            rec.setdefault("released", []).append(k)

    mod = types.ModuleType("pynput.keyboard")
    mod.Controller = Controller
    mod.Key = Key
    monkeypatch.setitem(sys.modules, "pynput.keyboard",  mod)
    monkeypatch.setitem(sys.modules, "pynput",
                        sys.modules.get("pynput") or types.ModuleType("pynput"))
    monkeypatch.setattr(base.time, "sleep", lambda *_: None)   # no 50ms wait
    return rec


def test_paste_uses_command_modifier_on_macos(monkeypatch):
    monkeypatch.setattr(base.PlatformBackend, "copy", lambda self, t: True)
    rec = _install_fake_pynput(monkeypatch)
    assert osplatform.get_backend("macos").paste("hey") is True
    assert rec["mod"] == "MOD.cmd"
    assert rec["pressed"] == ["v"] and rec["released"] == ["v"]


@pytest.mark.parametrize("os_name", ["windows", "linux"])
def test_paste_uses_control_modifier_off_macos(monkeypatch, os_name):
    monkeypatch.setattr(base.PlatformBackend, "copy", lambda self, t: True)
    rec = _install_fake_pynput(monkeypatch)
    assert osplatform.get_backend(os_name).paste("hey") is True
    assert rec["mod"] == "MOD.ctrl"


def test_paste_degrades_when_copy_fails(monkeypatch):
    monkeypatch.setattr(base.PlatformBackend, "copy", lambda self, t: False)
    # copy failed → paste must not synthesize a keystroke, and returns False.
    assert osplatform.get_backend("linux").paste("x") is False


# ── permissions ─────────────────────────────────────────────────────────────
def test_macos_perms_shape():
    p = osplatform.get_backend("macos").check_perms()
    assert p.applicable is True
    assert p.input_monitoring in (True, False, None)
    assert p.accessibility in (True, False, None)
    assert set(p.as_dict()) >= {"applicable", "input_monitoring", "accessibility", "note"}


@pytest.mark.parametrize("os_name", ["windows", "linux"])
def test_non_mac_perms_not_applicable(os_name):
    p = osplatform.get_backend(os_name).check_perms()
    assert p.applicable is False
    assert p.input_monitoring is None and p.accessibility is None
    assert p.note                                    # a human-readable explanation


def test_check_perms_dict_always_has_the_api_keys():
    d = osplatform.check_perms()
    assert {"applicable", "input_monitoring", "accessibility"} <= set(d)


# ── confirm gate — SECURITY-CRITICAL (CLAUDE.md rule 3 / DESIGN §3) ─────────
def test_confirm_denies_by_default_on_every_backend():
    req = ConfirmRequest("Voxwire — confirm action", "shell.run: git status")
    for b in osplatform.registered():
        assert b.confirm(req) is False, f"{b.name!r} approved without a human gate!"


def test_executor_gate_ready_is_false_everywhere():
    # The executor stays structurally disabled until a hardened, human-reviewed
    # native gate lands (Epic 2 / R1) — on EVERY OS, macOS included.
    for b in osplatform.registered():
        assert b.executor_gate_ready() is False
    assert osplatform.executor_gate_ready() is False


def test_module_confirm_denies_by_default():
    assert osplatform.confirm("t", "m") is False


def test_macos_confirm_consults_registered_provider(clean_provider):
    seen = []
    osplatform.set_confirm_provider(lambda req: (seen.append(req), True)[1])
    assert osplatform.get_backend("macos").confirm(ConfirmRequest("t", "m")) is True
    assert len(seen) == 1


def test_non_mac_never_approves_even_with_a_rogue_provider(clean_provider):
    # A provider that always approves must NOT be reachable on Windows/Linux —
    # those OSes have no proven native gate, so they deny unconditionally.
    osplatform.set_confirm_provider(lambda req: True)
    for os_name in ("windows", "linux"):
        assert osplatform.get_backend(os_name).confirm(ConfirmRequest("t", "m")) is False


def test_confirm_denies_when_provider_raises(clean_provider):
    def boom(req):
        raise RuntimeError("provider blew up")

    osplatform.set_confirm_provider(boom)
    assert osplatform.get_backend("macos").confirm(ConfirmRequest("t", "m")) is False
