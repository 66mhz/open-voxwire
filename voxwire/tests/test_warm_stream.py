"""Regression tests for warm-mic stream lifecycle (server.ensure_warm).

The bug these guard against was a lock-ordering deadlock that took the whole
server unreachable: ensure_warm() reconfigured the input stream (device or
channel-count change, e.g. toggling dual-mic) by calling the old stream's
stop()/close() *while holding _lock*. PortAudio's close() blocks until the
running audio callback returns, and the callback (_warm_cb) also takes _lock —
so close-under-lock deadlocks, _lock is held forever, every `with _lock`
endpoint hangs, the pollers exhaust the threadpool, and the config UI shows
"server not reachable".

We model PortAudio's semantics faithfully without audio hardware: because
_lock is a non-reentrant threading.Lock, a fake stream whose close() must
acquire _lock reproduces the exact deadlock — it can only complete if close()
runs *outside* the lock. release_warm() has always done that dance; ensure_warm
must too.
"""
import sys
import threading
import time
import types

import pytest

server = pytest.importorskip(
    "server", reason="server.py needs the audio stack (sounddevice/PortAudio)")


class _FakeStream:
    """Stands in for an sd.InputStream. close() takes _lock, exactly like
    PortAudio waiting on the _lock-holding callback — so it can only return if
    ensure_warm closes it outside the lock."""

    def __init__(self):
        self.stopped = False
        self.closed = False

    def stop(self):
        self.stopped = True

    def close(self):
        with server._lock:      # deadlocks iff ensure_warm still holds _lock
            self.closed = True

    @property
    def active(self):           # a running stream is alive → ensure_warm reuses it
        return not (self.stopped or self.closed)


@pytest.fixture
def _isolated_warm(monkeypatch):
    """Save/restore the module-global warm state and stub out real audio I/O so
    ensure_warm exercises only its locking, not hardware."""
    saved_warm = dict(server._warm)
    saved_idle = server._idle_timeout
    server._idle_timeout = 0    # keep the idle-reaper from touching our stream
    server._warm["want_channels"] = 1   # tests seed stream/channels by hand
    monkeypatch.setattr(server, "_pick_samplerate", lambda dev: 16000)
    monkeypatch.setattr(
        server, "_open_input",
        lambda dev, ch, sr: (_FakeStream(), 2 if ch >= 2 else 1),
    )
    try:
        yield
    finally:
        server._warm.clear()
        server._warm.update(saved_warm)
        server._idle_timeout = saved_idle


def _run_with_timeout(fn, timeout=5.0):
    """Run fn() in a daemon thread; return its result or fail if it deadlocks."""
    box = {}

    def _target():
        try:
            box["ok"] = fn()
        except BaseException as exc:      # noqa: BLE001 — surface in the test
            box["err"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise AssertionError(
            "ensure_warm deadlocked (close() under _lock) — server would be "
            "unreachable"
        )
    if "err" in box:
        raise box["err"]
    return box["ok"]


def test_reconfigure_does_not_deadlock_and_closes_old_stream(_isolated_warm):
    old = _FakeStream()
    server._warm.update(stream=old, device=3, channels=1)

    # device change forces the reconfigure path (stop/close old, open new).
    sr = _run_with_timeout(lambda: server.ensure_warm(device_index=7, channels=1))

    assert sr == 16000
    assert old.stopped and old.closed          # old stream torn down
    assert server._warm["stream"] is not old   # swapped for the new one
    assert server._warm["device"] == 7


def test_channel_switch_reconfigures_without_deadlock(_isolated_warm):
    """Toggling dual-mic (channels 1 -> 2) is the exact path the new dual-mic
    UI drives, and the one most likely to hit the old deadlock."""
    old = _FakeStream()
    server._warm.update(stream=old, device=3, channels=1)

    _run_with_timeout(lambda: server.ensure_warm(device_index=3, channels=2))

    assert old.closed
    assert server._warm["channels"] == 2


def test_same_config_is_a_noop_and_keeps_stream(_isolated_warm):
    keep = _FakeStream()
    server._warm.update(stream=keep, device=5, channels=1, sr=16000)

    sr = _run_with_timeout(lambda: server.ensure_warm(device_index=5, channels=1))

    assert sr == 16000
    assert server._warm["stream"] is keep      # unchanged
    assert not keep.closed                      # not torn down


# ── concurrent lifecycle transitions (review) ──────────────────────
def _parked_stream(gate):
    """An old stream whose stop() parks the caller in the unlocked close gap."""
    class _SlowStop(_FakeStream):
        def stop(self):
            gate.wait(5)
            super().stop()
    return _SlowStop()


@pytest.fixture
def _opened(_isolated_warm, monkeypatch):
    opened = []

    def open_input(dev, ch, sr):
        s = _FakeStream()
        opened.append(s)
        return s, 2 if ch >= 2 else 1
    monkeypatch.setattr(server, "_open_input", open_input)
    return opened


def test_concurrent_reconfigures_leave_exactly_one_live_stream(_opened):
    """The second caller used to open its stream while the first was closing the
    old one outside _lock, and then get overwritten: a live, unreferenced
    stream whose callback kept writing into the warm buffer."""
    gate = threading.Event()
    server._warm.update(stream=_parked_stream(gate), device=3, channels=1)
    first = threading.Thread(target=server.ensure_warm, args=(7, 1), daemon=True)
    first.start()
    time.sleep(0.1)
    second = threading.Thread(target=server.ensure_warm, args=(9, 1), daemon=True)
    second.start()
    time.sleep(0.1)                     # room for an unserialised second open
    gate.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert [s for s in _opened if not s.closed] == [server._warm["stream"]]
    assert server._warm["device"] == 9


def test_a_release_during_a_reconfigure_is_not_undone(_opened):
    gate = threading.Event()
    server._warm.update(stream=_parked_stream(gate), device=3, channels=1)
    arm = threading.Thread(target=server.ensure_warm, args=(7, 1), daemon=True)
    arm.start()
    time.sleep(0.1)
    release = threading.Thread(target=server.release_warm, daemon=True)
    release.start()
    time.sleep(0.1)
    gate.set()
    arm.join(5)
    release.join(5)
    assert server._warm["stream"] is None
    assert all(s.closed for s in _opened)


def test_a_stereo_stream_that_fails_to_start_is_closed_before_the_mono_fallback(monkeypatch):
    made = []

    class _InputStream:
        def __init__(self, device, channels, samplerate, dtype, callback):
            self.channels, self.closed = channels, False
            made.append(self)

        def start(self):
            if self.channels == 2:
                raise RuntimeError("Invalid number of channels")

        def stop(self):
            pass

        def close(self):
            self.closed = True

    monkeypatch.setattr(server.sd, "InputStream", _InputStream)
    stream, got = server._open_input(0, 2, 16000)
    assert got == 1 and stream is made[1]
    assert made[0].closed and not made[1].closed


def test_dictation_warms_the_configured_channel_count(monkeypatch):
    """Dual-mic dictation used to warm a mono stream, so the first hotkey press
    tore it down and reopened in stereo, losing the pre-roll."""
    calls = []
    monkeypatch.setattr(server, "ensure_warm", lambda dev, ch=1: calls.append((dev, ch)) or 16000)
    listener = types.SimpleNamespace(start=lambda: None, stop=lambda: None)
    fake = types.ModuleType("pynput")
    fake.keyboard = types.SimpleNamespace(Listener=lambda **kw: listener)
    monkeypatch.setitem(sys.modules, "pynput", fake)
    monkeypatch.setitem(server._dict, "channels", 2)
    monkeypatch.setitem(server._dict, "device", 4)
    server.start_dictation()
    try:
        assert calls == [(4, 2)]
    finally:
        server.stop_dictation()


# ── mono fallback + teardown on a vanished device (PR #9 review) ────────────
def test_a_mono_fallback_satisfies_the_next_stereo_request(_isolated_warm, monkeypatch):
    """A device that can't do stereo falls back to mono. The next arm/record
    start asks for stereo again and used to compare that with the 1 channel it
    got, so it closed and reopened the mic every time, losing the pre-roll."""
    opened = []

    def open_input(dev, ch, sr):
        s = _FakeStream()
        opened.append(s)
        return s, 1                      # this device only does mono
    monkeypatch.setattr(server, "_open_input", open_input)
    server._warm.update(stream=None, device=None)

    _run_with_timeout(lambda: server.ensure_warm(device_index=4, channels=2))
    first = server._warm["stream"]
    _run_with_timeout(lambda: server.ensure_warm(device_index=4, channels=2))
    _run_with_timeout(lambda: server.ensure_warm(device_index=4, channels=1))

    assert opened == [first] and not first.closed
    assert server._warm["channels"] == 1


def test_close_stream_still_closes_when_stop_raises():
    class _Vanished(_FakeStream):
        def stop(self):
            raise RuntimeError("device unavailable")

        def close(self):                 # no _lock here: called directly
            self.closed = True
    s = _Vanished()
    server._close_stream(s)
    assert s.closed
