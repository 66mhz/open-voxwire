"""Settings sync — /api/dictation/status is the single source of truth.

The menubar and the web config used to drift because the server never exposed the
live model/device/etc, and every /api/dictation call forced dictation on/off. Now
both UIs read the full config back from status and push config-only changes
(enabled omitted). These cover the server side of that contract.
"""
import sys
import threading
import time
import types

import pytest

server = pytest.importorskip(   # conftest sets VOXWIRE_NO_IDLE_WATCH before this import
    "server", reason="audio stack (sounddevice/PortAudio) not importable here")
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app, base_url="http://127.0.0.1:8123", client=("127.0.0.1", 50000))   # this machine (see LocalOnly)


@pytest.fixture(autouse=True)
def _reset():
    """Start each test from a known config and put the whole shared _dict back
    afterwards, so nothing a test writes leaks into another test module."""
    saved = dict(server._dict)
    server._dict.update(enabled=False, channels=1, fusion_mode="fusion",
                        configured=False, hotkey="alt+cmd", insert_mode="command")
    yield
    server._dict.clear()
    server._dict.update(saved)


def test_status_exposes_the_full_config():
    s = client.get("/api/dictation/status").json()
    for k in ("model", "device", "channels", "fusion_mode", "fixup", "context",
              "hotkey", "insert_mode", "enabled", "configured"):
        assert k in s, f"/api/dictation/status is missing {k!r} — UIs can't reflect it"


def test_config_only_update_does_not_toggle_dictation():
    assert client.get("/api/dictation/status").json()["enabled"] is False
    r = client.post("/api/dictation", json={"channels": 2, "fusion_mode": "throat"}).json()
    assert r["enabled"] is False        # a config-only POST must NOT switch dictation on
    assert r["channels"] == 2 and r["fusion_mode"] == "throat"
    assert r["configured"] is True      # marks the config as set → later UIs adopt it


def test_config_change_is_visible_via_status():
    # simulates: change it in one UI → the other reads it back from status
    client.post("/api/dictation", json={"hotkey": "ctrl+space", "insert_mode": "type"})
    s = client.get("/api/dictation/status").json()
    assert s["hotkey"] == "ctrl+space" and s["insert_mode"] == "type"


def test_config_only_post_never_starts_the_listener(monkeypatch):
    started = []
    monkeypatch.setattr(server, "start_dictation", lambda: started.append(1))
    client.post("/api/dictation", json={"fusion_mode": "air"})   # enabled omitted, dictation off
    assert started == []


def test_toggle_still_works_with_explicit_enabled(monkeypatch):
    monkeypatch.setattr(server, "start_dictation", lambda: None)
    monkeypatch.setattr(server, "stop_dictation", lambda: None)
    assert client.post("/api/dictation", json={"enabled": True}).json()["enabled"] is True
    assert client.post("/api/dictation", json={"enabled": False}).json()["enabled"] is False


# ── review follow-ups ──────────────────────────────────────────────────────
def test_values_that_are_ignored_do_not_mark_the_config_as_set():
    r = client.post("/api/dictation", json={"hotkey": "", "insert_mode": ""}).json()
    assert r["configured"] is False
    assert r["hotkey"] == "alt+cmd" and r["insert_mode"] == "command"


def test_a_rejected_update_changes_nothing():
    before = client.get("/api/dictation/status").json()
    res = client.post("/api/dictation", json={"hotkey": "f8", "model": "no-such-model"})
    assert res.status_code == 400
    after = client.get("/api/dictation/status").json()
    assert after["hotkey"] == before["hotkey"] and after["configured"] == before["configured"]


def test_a_config_only_change_while_dictating_keeps_dual_mic(monkeypatch):
    warmed = []
    monkeypatch.setattr(server, "ensure_warm", lambda dev, ch=1: warmed.append(ch) or 16000)
    listener = types.SimpleNamespace(start=lambda: None, stop=lambda: None)
    fake = types.ModuleType("pynput")
    fake.keyboard = types.SimpleNamespace(Listener=lambda **kw: listener)
    monkeypatch.setitem(sys.modules, "pynput", fake)
    assert client.post("/api/dictation", json={"enabled": True, "channels": 2}).json()["enabled"]
    client.post("/api/dictation", json={"context": "short git commands"})   # config-only
    assert warmed and all(ch == 2 for ch in warmed), warmed


def test_a_disable_during_a_slow_config_restart_wins(monkeypatch):
    """A config-only request restarts a running listener, which can be slow (a
    Bluetooth mic re-opening). A disable arriving meanwhile used to finish first
    and then be overwritten when the restart set enabled=True again."""
    started, gate = threading.Event(), threading.Event()

    def slow_start():
        started.set()
        gate.wait(5)
    monkeypatch.setattr(server, "start_dictation", slow_start)
    monkeypatch.setattr(server, "stop_dictation", lambda: None)
    server._dict["enabled"] = True
    restart = threading.Thread(
        target=server.dictation, args=(server.DictationReq(context="x"),), daemon=True)
    restart.start()
    assert started.wait(5)
    disable = threading.Thread(
        target=server.dictation, args=(server.DictationReq(enabled=False),), daemon=True)
    disable.start()
    time.sleep(0.2)                     # an unserialised disable finishes here
    gate.set()
    restart.join(5)
    disable.join(5)
    assert server._dict["enabled"] is False


# ── PR #11 review: a config change never restarts a running listener ──────
@pytest.fixture
def running(monkeypatch):
    """Dictation on with a live listener; any restart is recorded as a failure."""
    restarts = []
    monkeypatch.setattr(server, "start_dictation", lambda: restarts.append("start"))
    monkeypatch.setattr(server, "stop_dictation", lambda: restarts.append("stop"))
    warmed = []
    monkeypatch.setattr(server, "ensure_warm",
                        lambda dev, ch=1: warmed.append((dev, ch)) or 16000)
    server._dict.update(enabled=True, listener=object(), recording=False,
                        chord=["alt", "cmd"], device=0)
    return types.SimpleNamespace(restarts=restarts, warmed=warmed)


def test_a_config_update_during_a_recording_keeps_the_utterance(running):
    server._dict["recording"] = True             # the user is holding the hotkey
    r = client.post("/api/dictation", json={"context": "git", "model": server._dict["model"],
                                            "device_index": 3}).json()
    assert r["enabled"] is True and r["context"] == "git" and r["device"] == 3
    assert running.restarts == []                # no stop_dictation → recording not cleared
    assert server._dict["recording"] is True
    assert running.warmed == []                  # no re-warm mid-capture (it drops the buffer)


def test_a_device_change_rewarms_live_when_idle(running):
    client.post("/api/dictation", json={"device_index": 2, "channels": 2})
    assert running.restarts == [] and running.warmed == [(2, 2)]


def test_a_hotkey_change_takes_effect_without_a_restart(running):
    client.post("/api/dictation", json={"hotkey": "ctrl+space"})
    assert running.restarts == [] and server._dict["chord"] == ["ctrl", "space"]


def test_an_unchanged_field_does_not_rewarm(running):
    client.post("/api/dictation", json={"device_index": 0, "context": "x"})
    assert running.restarts == [] and running.warmed == []


def test_enable_while_already_running_does_not_restart(running):
    assert client.post("/api/dictation", json={"enabled": True}).json()["enabled"] is True
    assert running.restarts == []


# ── PR #11 review: two clients editing different fields between polls ───────
def test_two_clients_editing_different_fields_both_stick():
    # Both clients last polled the same status; A changes the hotkey, then B —
    # still showing the old hotkey — changes the insert mode, sending only that
    # field (partial update), so A's edit survives.
    client.post("/api/dictation", json={"hotkey": "ctrl+space"})     # client A
    client.post("/api/dictation", json={"insert_mode": "type"})      # client B (stale)
    s = client.get("/api/dictation/status").json()
    assert s["hotkey"] == "ctrl+space" and s["insert_mode"] == "type"


def test_the_web_ui_posts_only_the_changed_field():
    """The web used to POST the whole form on every control change, writing its
    stale view of every other field back over another client's edit. The full
    form (dictConfigBody) may only seed a fresh, unconfigured server."""
    import pathlib
    html = (pathlib.Path(server.__file__).parent / "index.html").read_text()
    uses = [ln for ln in html.splitlines() if "dictConfigBody()" in ln
            and "function dictConfigBody" not in ln]
    assert len(uses) == 1 and "seed" in uses[0], uses
    assert "api('/api/dictation', dictConfigBody())" not in html
    assert "dictBody(" not in html
