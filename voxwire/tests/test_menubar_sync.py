"""Menubar ← server config. menubar.py imports rumps and PyObjC, which
exist only on macOS, so those are stubbed and only the pure choice of which
input device the menu uses is exercised."""
import sys
import types

import pytest

INPUTS = [{"index": 2, "name": "MacBook Pro Microphone"},
          {"index": 5, "name": "Stealth throat mic"}]


@pytest.fixture
def menubar(monkeypatch):
    rumps = types.ModuleType("rumps")

    class _App:
        def __init__(self, *a, **k):
            pass
    rumps.App, rumps.MenuItem, rumps.Timer = _App, object, object
    rumps.alert = rumps.notification = rumps.quit_application = lambda *a, **k: None
    appkit = types.ModuleType("AppKit")
    appkit.NSAlert, appkit.NSAlertFirstButtonReturn = object, 1000
    pyobjc = types.ModuleType("PyObjCTools")
    pyobjc.AppHelper = types.SimpleNamespace(callAfter=lambda *a, **k: None)
    for name, mod in (("rumps", rumps), ("AppKit", appkit), ("PyObjCTools", pyobjc)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delitem(sys.modules, "menubar", raising=False)
    import menubar
    return menubar


def test_adopts_the_servers_configured_input(menubar):
    assert menubar.adopt_device({"device": 5, "configured": True}, INPUTS, current=2) == 5


def test_keeps_its_device_when_the_servers_is_not_an_input(menubar):
    assert menubar.adopt_device({"device": 0, "configured": True}, INPUTS, current=2) == 2


def test_ignores_the_servers_placeholder_until_a_client_configures_it(menubar):
    assert menubar.adopt_device({"device": 5, "configured": False}, INPUTS, current=2) == 2


def test_falls_back_to_the_first_input_rather_than_device_zero(menubar):
    assert menubar.adopt_device({"device": 0, "configured": True}, INPUTS, current=None) == 2
    assert menubar.adopt_device({}, INPUTS, current=None) == 2


def test_with_no_inputs_it_keeps_what_it_has(menubar):
    assert menubar.adopt_device({"device": 0, "configured": True}, [], current=None) is None


# ── PR #11 review: enabling never writes back a stale cached config ─────────
def test_enable_sends_neither_cached_model_nor_device_the_server_has(menubar):
    fresh = {"configured": True, "device": 5, "model": "set-by-the-web"}
    assert menubar.enable_body(fresh, INPUTS, current=2) == {"enabled": True}


def test_enable_supplies_a_device_only_when_the_servers_is_unusable(menubar):
    assert menubar.enable_body({"configured": False, "device": 0}, INPUTS, current=5) \
        == {"enabled": True, "device_index": 5}
    assert menubar.enable_body({"configured": True, "device": 9}, INPUTS, current=None) \
        == {"enabled": True, "device_index": 2}
