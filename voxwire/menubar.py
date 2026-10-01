#!/usr/bin/env python3
"""Voxwire — macOS menubar app.

The single thing you launch on the Mac. It runs the STT/dictation FastAPI server
(server.py) IN-PROCESS and puts native controls in the menubar, so you never
have to configure anything in a browser tab.

It is also the host for the Mac executor (architecture-v2 ADR-8 / Epic 2): the
executor's confirmation dialogs MUST be native and run in a GUI process, which
is exactly this one. v1 ships the confirm primitive + kill switch as seeds; the
shell/fs/ui tool contract lands with Epic 2.

Run:
    ./run.sh app          # or: .venv/bin/python menubar.py

Permissions: because dictation's global hotkey + paste use pynput, the process
that runs THIS needs Accessibility + Input Monitoring. Running via python from a
terminal grants them to that terminal; bundling into Voxwire.app (py2app) so
"Voxwire" owns the grant is a follow-up.
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.request
import webbrowser

import rumps
from AppKit import NSAlert, NSAlertFirstButtonReturn
from PyObjCTools import AppHelper

BASE = "http://127.0.0.1:8123"
PORT = 8123

# ─── tiny HTTP client (stdlib — no extra deps) ─────────────────────────────
def _req(path: str, body: dict | None = None, method: str | None = None, timeout: float = 5.0):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    m = method or ("POST" if body is not None else "GET")
    req = urllib.request.Request(url, data=data, method=m,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except Exception:
        return None


def get(path):  return _req(path)
def post(path, body=None):  return _req(path, body if body is not None else {})


def adopt_device(status: dict, inputs: list[dict], current: int | None) -> int | None:
    """The input device the menu should use: the server's device when a client
    has configured it and it is a real input here, else the current one, else
    the first input. The server's unconfigured default (0) is a placeholder that
    may not even be an input device, so it is never adopted."""
    indexes = [d["index"] for d in inputs]
    dev = status.get("device")
    if status.get("configured") and dev in indexes:
        return dev
    if current in indexes:
        return current
    return indexes[0] if indexes else current


def enable_body(status: dict, inputs: list[dict], current: int | None) -> dict:
    """The /api/dictation body that switches dictation on. `status` must be FRESH
    (read just before the POST): the server owns the config, so only a device the
    server lacks a usable value for is sent — never the menu's cached model or
    device, which may be a poll behind and would revert another client's change."""
    body: dict = {"enabled": True}
    dev = adopt_device(status, inputs, current)
    if dev is not None and dev != status.get("device"):
        body["device_index"] = dev
    return body


def _clear_submenu(item):
    """rumps MenuItem.clear() throws until the submenu's NSMenu exists (it is
    created lazily on first child add). Safe no-op when still empty."""
    try:
        item.clear()
    except AttributeError:
        pass


# ─── native confirmation dialog (executor-host seed; ADR-8 / R1) ────────────
def _show_alert(title: str, message: str, ok="Approve", cancel="Deny") -> bool:
    """Runs on the MAIN thread only."""
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_(ok)
    alert.addButtonWithTitle_(cancel)
    return alert.runModal() == NSAlertFirstButtonReturn


def native_confirm(title: str, message: str, timeout: float = 120.0) -> bool:
    """Block the calling (non-main) thread until the user approves/denies a
    native dialog on the main thread. Defaults to DENY on timeout.

    This is the primitive the Mac executor calls for a gated action. It is
    already cross-thread safe (server runs in a worker thread; the dialog runs
    on rumps' main run loop).

    TODO(Epic 2, R1): reject synthetic events — inspect the approving event's
    source (kCGEventSourceStateHIDSystemState) so the agent cannot click its own
    Approve via CGEvent injection. NSAlert alone does not distinguish these.
    """
    box: dict = {}
    done = threading.Event()

    def run():
        try:
            box["v"] = _show_alert(title, message)
        finally:
            done.set()

    AppHelper.callAfter(run)
    if not done.wait(timeout):
        return False
    return box.get("v", False)


# ─── server, launched in-process ────────────────────────────────────────────
def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_server_in_thread():
    """Start uvicorn(server.app) on a daemon thread. Returns True if we own it,
    False if an external server already holds the port (we just control it)."""
    if _port_in_use(PORT):
        return False
    import uvicorn
    import server  # imported here; its module-level idle-watch thread is fine
    cfg = uvicorn.Config(server.app, **{**server.UVICORN, "port": PORT})
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    return True


# ─── the menubar app ────────────────────────────────────────────────────────
ICON_ARMED = "🎙"
ICON_IDLE = "🌙"
ICON_REC = "🔴"
ICON_DOWN = "⚠️"


class VoxwireApp(rumps.App):
    def __init__(self):
        super().__init__("Voxwire", title=ICON_DOWN, quit_button=None)
        self.owns_server = False
        self._halt = False
        self._devices = []
        self._models = {}
        self._cur_device = None
        self._cur_model = None
        self._tick_count = 0

        # menu skeleton (submenus filled on first poll)
        self.m_status = rumps.MenuItem("starting…")
        self.m_arm = rumps.MenuItem("Arm mic", callback=self.toggle_arm)
        self.m_device = rumps.MenuItem("Device")
        self.m_model = rumps.MenuItem("Model")
        self.m_dictation = rumps.MenuItem("Global dictation", callback=self.toggle_dictation)
        self.m_perms = rumps.MenuItem("Permissions: …", callback=self.request_perms)
        self.m_battery = rumps.MenuItem("Throat mic: —")
        self.m_testconfirm = rumps.MenuItem("Test native confirmation…", callback=self.test_confirm)
        self.m_web = rumps.MenuItem("Open web config…", callback=self.open_web)
        self.m_kill = rumps.MenuItem("■ Kill switch", callback=self.kill)
        self.menu = [
            self.m_status, None,
            self.m_arm, self.m_device, self.m_model, self.m_dictation, None,
            self.m_perms, self.m_battery, None,
            self.m_testconfirm, self.m_web, None,
            self.m_kill,
            rumps.MenuItem("Quit Voxwire", callback=self.quit_app),
        ]

    # ── lifecycle ──
    def start(self):
        self.owns_server = start_server_in_thread()
        if not self.owns_server and not _port_in_use(PORT):
            rumps.alert("Voxwire", "Could not start the server on :8123.")
        rumps.Timer(self.tick, 2).start()
        self.run()

    # ── polling ──
    def tick(self, _):
        lvl = get("/api/level")
        if lvl is None:
            self.title = ICON_DOWN
            self.m_status.title = "server unreachable"
            return
        armed = lvl.get("warm")
        rec = lvl.get("recording")
        self.title = ICON_REC if rec else (ICON_ARMED if armed else ICON_IDLE)
        db = lvl.get("level_db")
        state = "recording" if rec else ("armed" if armed else "released")
        idle = lvl.get("idle_left")
        self.m_status.title = (f"{state}"
                               + (f" · {db:.0f} dB" if db is not None and armed else "")
                               + (f" · sleeps in {idle}s" if idle else ""))
        self.m_arm.title = "Release mic" if armed else "Arm mic"
        self.m_arm.state = 1 if armed else 0

        d = get("/api/dictation/status") or {}
        self.m_dictation.title = (f"Global dictation: on ({d.get('hotkey','?')})"
                                  if d.get("enabled") else "Global dictation: off")
        self.m_dictation.state = 1 if d.get("enabled") else 0

        # Reflect the server's live config so the menubar tracks the web (and any
        # other client). The server owns the config; the menu is just a view of it.
        nm = d.get("model")
        nd = adopt_device(d, self._devices, self._cur_device)
        if (nm is not None and nm != self._cur_model) or nd != self._cur_device:
            if nm is not None:
                self._cur_model = nm
            self._cur_device = nd
            self.refresh_devices_models()

        # heavier refreshes every ~15 ticks (~30s)
        self._tick_count += 1
        if self._tick_count % 15 == 1:
            self.refresh_devices_models()
            self.refresh_perms()
            self.refresh_battery()

    def refresh_devices_models(self):
        d = get("/api/devices")
        if not d:
            return
        self._devices = d.get("devices", [])
        self._models = {m["key"]: m for m in d.get("models", [])}
        _clear_submenu(self.m_device)
        for dev in self._devices:
            tag = " ★IASUS" if "IASUS" in dev.get("tags", []) else ""
            it = rumps.MenuItem(f'{dev["name"]}{tag}', callback=self.pick_device)
            it._device_index = dev["index"]
            it.state = 1 if dev["index"] == self._cur_device else 0
            self.m_device[it.title] = it
        _clear_submenu(self.m_model)
        for key, m in self._models.items():
            label = m["label"] + ("  ✓" if m.get("cached") else f"  ⬇{m.get('approx_gb','?')}GB")
            it = rumps.MenuItem(label, callback=self.pick_model)
            it._model_key = key
            it.state = 1 if key == self._cur_model else 0
            self.m_model[it.title] = it

    def refresh_perms(self):
        p = get("/api/perms") or {}
        def mark(v): return "✓" if v is True else ("✗" if v is False else "?")
        self.m_perms.title = (f"Permissions — Input Mon {mark(p.get('input_monitoring'))} · "
                              f"Accessibility {mark(p.get('accessibility'))}")

    def refresh_battery(self):
        b = get("/api/mic/battery") or {}
        self.m_battery.title = (f"Throat mic: {b.get('battery','—')}"
                                + (f" · {b.get('rssi')} dBm" if b.get("rssi") else "")) \
            if b.get("available") else "Throat mic: not connected"

    # ── actions ──
    def toggle_arm(self, sender):
        if sender.state:  # currently armed
            post("/api/mic/release", {})
        else:
            dev = adopt_device({}, self._devices, self._cur_device)
            post("/api/mic/arm", {"device_index": 0 if dev is None else dev})

    def pick_device(self, sender):
        self._cur_device = sender._device_index
        post("/api/dictation", {"device_index": sender._device_index})  # shared config
        post("/api/mic/arm", {"device_index": sender._device_index})
        self.refresh_devices_models()

    def pick_model(self, sender):
        self._cur_model = sender._model_key
        # Write the shared config (enabled unchanged); the web reflects it on poll.
        post("/api/dictation", {"model": sender._model_key})
        self.refresh_devices_models()

    def toggle_dictation(self, sender):
        if sender.state:
            post("/api/dictation", {"enabled": False})
        else:
            fresh = get("/api/dictation/status") or {}
            r = post("/api/dictation", enable_body(fresh, self._devices, self._cur_device))
            if r and r.get("error"):
                rumps.alert("Voxwire — dictation", str(r["error"]))

    def request_perms(self, _):
        post("/api/perms/request", {})
        rumps.notification("Voxwire", "Permissions requested",
                           "Approve the macOS prompts, enable in System Settings, then relaunch.")

    def test_confirm(self, _):
        # Prove the cross-thread native confirm primitive from a WORKER thread,
        # the way the executor will call it.
        def worker():
            ok = native_confirm("Voxwire — confirm action",
                                "This is the native gate the Mac executor will use.\n"
                                "shell.run: git status")
            AppHelper.callAfter(rumps.notification, "Voxwire",
                                "Native confirm", "Approved ✓" if ok else "Denied ✗")
        threading.Thread(target=worker, daemon=True).start()

    def open_web(self, _):
        webbrowser.open(BASE)

    def kill(self, _):
        post("/api/mic/release", {})
        post("/api/dictation", {"enabled": False})
        self._halt = True
        self.title = ICON_IDLE
        rumps.notification("Voxwire", "Kill switch",
                           "Mic released, dictation disabled. Re-arm from the menu to resume.")

    def quit_app(self, _):
        try:
            post("/api/mic/release", {})
            post("/api/dictation", {"enabled": False})
        finally:
            rumps.quit_application()


if __name__ == "__main__":
    VoxwireApp().start()
