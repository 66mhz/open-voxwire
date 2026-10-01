"""Mic-hotplug resilience tests.

The server must run 24/7 whether the stealth (throat) mic is connected or not.
The process never depends on the mic; these cover the safety net that keeps the
warm-mic subsystem from wedging when a Bluetooth throat mic drops mid-arm:

  * a vanished device is detected as a dead stream (query raises OR reports
    inactive),
  * the idle watcher reaps a dead warm stream (but never mid-capture),
  * re-arming after a drop tears the dead stream down and opens a fresh one.

Importing server.py pulls in the audio stack (sounddevice/PortAudio); on a box
without it (headless CI) the whole module skips. No real device is touched — a
fake InputStream stands in, and the background idle thread is disabled via
VOXWIRE_NO_IDLE_WATCH so it can't race these assertions.
"""
import collections
import os

import pytest

# Disable the module-level idle-watch thread BEFORE importing server, so it can't
# concurrently reap the fake streams these tests install.
os.environ["VOXWIRE_NO_IDLE_WATCH"] = "1"

server = pytest.importorskip(
    "server", reason="audio stack (sounddevice/PortAudio) not importable here")


class _FakeStream:
    """Duck-typed stand-in for sounddevice.InputStream. `active` can be flipped
    dead, or made to raise the way PortAudio does when a device disappears."""

    def __init__(self, *_, **__):
        self.started = False
        self.closed = False
        self._active = False
        self.raise_on_active = False

    def start(self):
        self.started = True
        self._active = True

    def stop(self):
        self._active = False

    def close(self):
        self.closed = True

    @property
    def active(self):
        if self.raise_on_active:
            raise RuntimeError("device vanished")  # emulates PaErrorException
        return self._active


@pytest.fixture(autouse=True)
def _reset_warm():
    """Leave the warm slot released + idle timeout default around each test."""
    yield
    try:
        server.release_warm()
    except Exception:
        pass
    with server._lock:
        server._warm.update(stream=None, device=None, mark=None,
                            blocks=collections.deque(), total=0)


# ── dead-stream detection ────────────────────────────────────────────────────
def test_stream_alive_none_is_dead():
    assert server._stream_alive(None) is False


def test_stream_alive_active():
    s = _FakeStream()
    s.start()
    assert server._stream_alive(s) is True


def test_stream_alive_inactive_is_dead():
    s = _FakeStream()
    s.start()
    s.stop()
    assert server._stream_alive(s) is False


def test_stream_alive_raising_is_dead():
    # A vanished CoreAudio device makes the query raise — that must read as dead,
    # not blow up the caller.
    s = _FakeStream()
    s.start()
    s.raise_on_active = True
    assert server._stream_alive(s) is False


# ── idle watcher reaps a dropped mic ─────────────────────────────────────────
def test_reap_dead_warm_releases_dropped_mic():
    s = _FakeStream()
    s.start()
    s.raise_on_active = True  # throat mic dropped
    with server._lock:
        server._warm.update(stream=s, device=3, mark=None)
    assert server._reap_dead_warm() is True
    assert server._warm["stream"] is None      # slot freed → re-arm can re-init
    assert s.closed is True


def test_reap_leaves_live_stream_alone():
    s = _FakeStream()
    s.start()
    with server._lock:
        server._warm.update(stream=s, device=3, mark=None)
    assert server._reap_dead_warm() is False
    assert server._warm["stream"] is s


def test_reap_never_interrupts_active_capture():
    # Dead stream, but mark set (mid press-to-talk) — don't yank it out from
    # under the in-flight capture; let record/dictation finish first.
    s = _FakeStream()
    s.start()
    s.raise_on_active = True
    with server._lock:
        server._warm.update(stream=s, device=3, mark=1000)
    assert server._reap_dead_warm() is False
    assert server._warm["stream"] is s


# ── re-arm after a drop recreates the stream ─────────────────────────────────
def test_ensure_warm_recreates_dead_stream(monkeypatch):
    created = []

    def _fake_inputstream(*a, **k):
        st = _FakeStream(*a, **k)
        created.append(st)
        return st

    monkeypatch.setattr(server.sd, "InputStream", _fake_inputstream)
    monkeypatch.setattr(server, "_pick_samplerate", lambda _idx: 16000)

    sr = server.ensure_warm(2)                 # first arm → stream #1
    assert sr == 16000
    assert len(created) == 1
    assert server._warm["stream"] is created[0]

    created[0].raise_on_active = True          # its device drops

    server.ensure_warm(2)                       # re-arm same device → new stream
    assert len(created) == 2, "dead stream on the same device must be recreated"
    assert server._warm["stream"] is created[1]
    assert created[0].closed is True           # old one torn down


def test_ensure_warm_reuses_live_stream(monkeypatch):
    created = []

    def _fake_inputstream(*a, **k):
        st = _FakeStream(*a, **k)
        created.append(st)
        return st

    monkeypatch.setattr(server.sd, "InputStream", _fake_inputstream)
    monkeypatch.setattr(server, "_pick_samplerate", lambda _idx: 16000)

    server.ensure_warm(2)
    server.ensure_warm(2)                       # still alive → no new stream
    assert len(created) == 1
