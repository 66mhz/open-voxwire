"""Capture API input checks (BYT-118): an unknown fusion_mode is refused with a
422 before any mic is touched, instead of a 500 later inside fusion.fuse."""
import pytest

server = pytest.importorskip(
    "server", reason="server.py needs the audio stack (sounddevice/PortAudio)")
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def no_audio(monkeypatch):
    """Record would-be mic opens; restore the module state the handlers touch."""
    calls = []
    monkeypatch.setattr(server, "ensure_warm", lambda dev, ch=1: calls.append((dev, ch)) or 16000)
    monkeypatch.setattr(server, "start_dictation", lambda: None)
    monkeypatch.setattr(server, "stop_dictation", lambda: None)
    saved_warm, saved_dict = dict(server._warm), dict(server._dict)
    yield calls
    server._warm.clear()
    server._warm.update(saved_warm)
    server._dict.clear()
    server._dict.update(saved_dict)


@pytest.mark.parametrize("path,body", [
    ("/api/mic/arm", {"device_index": 0, "channels": 2, "fusion_mode": "bogus"}),
    ("/api/record/start", {"device_index": 0, "channels": 2, "fusion_mode": "bogus"}),
    ("/api/dictation", {"enabled": False, "fusion_mode": "bogus"}),
], ids=["arm", "record", "dictation"])
def test_unknown_fusion_mode_is_refused_before_the_mic_opens(path, body, no_audio):
    res = client.post(path, json=body)
    assert res.status_code == 422, res.text
    assert no_audio == []


@pytest.mark.parametrize("mode", ["fusion", "throat", "air"])
def test_known_fusion_modes_are_accepted(mode, no_audio):
    res = client.post("/api/mic/arm", json={"device_index": 0, "channels": 2, "fusion_mode": mode})
    assert res.status_code == 200, res.text
    assert res.json()["fusion_mode"] == mode
