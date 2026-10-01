"""/api/audio-stream tests (BYT-118): who may connect, and bad input.

Only peers on this machine may connect: a network client is unauthenticated and
could otherwise have its transcript pasted at the cursor.
Any web page can open a WebSocket to 127.0.0.1 (WebSockets skip CORS), and this
endpoint can paste transcripts at the cursor, so a browser origin other than the
server's own loopback page is refused. Non-browser clients (the capture node,
scripts/stream_pcm.py) send no Origin header and are allowed.
"""
import numpy as np
import pytest

server = pytest.importorskip(
    "server", reason="server.py needs the audio stack (sounddevice/PortAudio)")
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

client = TestClient(server.app, base_url="http://127.0.0.1:8123", client=("127.0.0.1", 50000))   # this machine (see LocalOnly)
# Absolute: TestClient joins a relative WebSocket path onto ws://testserver,
# whose Host is not this machine, so LocalOnly would refuse it.
URL = "ws://127.0.0.1:8123/api/audio-stream"


@pytest.fixture
def fake_stt(monkeypatch, tmp_path):
    seen = {}

    class _Result:
        text = "hello there"

    def transcribe(path, model):
        seen["model"] = model
        return _Result()
    monkeypatch.setattr(server.stt, "transcribe", transcribe)
    monkeypatch.setattr(server, "RECORDINGS_DIR", tmp_path)
    return seen


def _no_audio_roundtrip(ws):
    ws.send_json({"event": "start", "channels": 1, "sr": 16000})
    ws.send_json({"event": "stop"})
    return ws.receive_json()


def _pcm(frames=1600, channels=1):
    return np.zeros((frames, channels), dtype="<i2").tobytes()


# ── who may connect ────────────────────────────────────────────────────────
@pytest.mark.parametrize("headers", [
    {"origin": "https://evil.example"},
    {"origin": "http://evil.example:8123", "host": "evil.example:8123"},
    {"origin": "http://127.0.0.1:9999", "host": "127.0.0.1:8123"},
    {"origin": "null"},
], ids=["cross-site", "dns-rebinding", "other-local-origin", "opaque-origin"])
def test_refuses_browser_origins_other_than_its_own(headers):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(URL, headers=headers):
            pass
    assert exc.value.code == 1008


@pytest.mark.parametrize("peer", ["203.0.113.7", "198.51.100.4", "2001:db8::1", "testclient"])
def test_refuses_peers_that_are_not_on_this_machine(peer, fake_stt, monkeypatch):
    pasted = []
    monkeypatch.setattr(server, "insert_text", pasted.append)
    remote = TestClient(server.app, base_url="http://127.0.0.1:8123", client=(peer, 50000))
    with pytest.raises(WebSocketDisconnect) as exc:
        with remote.websocket_connect(URL) as ws:        # no Origin, like a node
            ws.send_json({"event": "start", "channels": 1, "sr": 16000, "paste": True})
            ws.send_bytes(_pcm())
            ws.send_json({"event": "stop"})
            ws.receive_json()
    assert exc.value.code == 1008
    assert pasted == [] and "model" not in fake_stt      # nothing transcribed or pasted


@pytest.mark.parametrize("peer", ["127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"])
def test_accepts_loopback_peers(peer):
    with TestClient(server.app, base_url="http://127.0.0.1:8123",
                    client=(peer, 50000)).websocket_connect(URL) as ws:
        assert _no_audio_roundtrip(ws) == {"error": "no audio received"}


def test_an_unknown_peer_is_not_loopback():
    assert server._loopback_peer(None) is False


def test_accepts_clients_that_send_no_origin():
    with client.websocket_connect(URL) as ws:
        assert _no_audio_roundtrip(ws) == {"error": "no audio received"}


@pytest.mark.parametrize("host", ["127.0.0.1:8123", "localhost:8123", "[::1]:8123"])
def test_accepts_its_own_loopback_page(host):
    with client.websocket_connect(URL, headers={"host": host, "origin": f"http://{host}"}) as ws:
        assert _no_audio_roundtrip(ws) == {"error": "no audio received"}


# ── bad input ──────────────────────────────────────────────────────────────
def test_invalid_json_gets_an_error_and_the_connection_survives():
    with client.websocket_connect(URL) as ws:
        ws.send_text("{not json")
        assert "error" in ws.receive_json()
        assert _no_audio_roundtrip(ws) == {"error": "no audio received"}


@pytest.mark.parametrize("bad", [
    {"channels": 3}, {"channels": 0}, {"channels": "2"}, {"channels": True},
    {"sr": 0}, {"sr": -16000}, {"sr": "16000"},
    {"mode": "bogus"}, {"model": "no-such-model"},
], ids=lambda b: "-".join(f"{k}={v}" for k, v in b.items()))
def test_a_bad_start_is_refused_and_the_last_good_config_stays(bad, fake_stt):
    with client.websocket_connect(URL) as ws:
        ws.send_json({"event": "start", "channels": 1, "sr": 16000})
        ws.send_bytes(_pcm())
        ws.send_json({"event": "start", **bad})
        assert "error" in ws.receive_json()
        ws.send_json({"event": "stop"})
        out = ws.receive_json()
        assert out.get("ok") is True, out
        assert out["dual_mic"] is False and out["mode"] == "fusion"


def test_audio_beyond_the_cap_is_refused(monkeypatch):
    monkeypatch.setattr(server, "MAX_STREAM_BYTES", 4000)
    with client.websocket_connect(URL) as ws:
        ws.send_json({"event": "start", "channels": 1, "sr": 16000})
        ws.send_bytes(b"\x00" * 3000)
        ws.send_bytes(b"\x00" * 3000)
        assert "too much audio" in ws.receive_json()["error"]
        ws.send_json({"event": "stop"})
        assert ws.receive_json() == {"error": "no audio received"}


def test_a_good_utterance_is_transcribed(fake_stt):
    with client.websocket_connect(URL) as ws:
        ws.send_json({"event": "start", "channels": 2, "sr": 16000, "mode": "throat"})
        ws.send_bytes(_pcm(channels=2))
        ws.send_json({"event": "stop"})
        out = ws.receive_json()
    assert out["ok"] is True and out["transcript"] == "hello there"
    assert out["dual_mic"] is True and out["mode"] == "throat"
