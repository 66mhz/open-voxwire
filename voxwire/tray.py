#!/usr/bin/env python3
"""Voxwire — cross-platform system-tray app (Windows / Linux; the non-mac
counterpart to menubar.py).

The single thing you launch when you're not on a Mac. Like the menubar app it runs the
STT/dictation FastAPI server (server.py) IN-PROCESS and puts native controls in
the system tray, so you never have to configure anything in a browser tab. It
drives the SAME running server entirely over its HTTP API (the tiny stdlib
client below — no server internals are imported), exactly like menubar.py does.

Dictation-first: it exposes arm/release, device + model pickers, the global
dictation toggle, a kill switch, the web-config link, and quit. It does NOT host
the executor — the native, synthetic-input-rejecting confirm gate the executor
requires is not yet proven off macOS (osplatform.executor_gate_ready() is False
on Windows/Linux; CLAUDE.md rule 3 / DESIGN §3), so there is deliberately no
confirm/executor control here.

Run:
    ./run.sh app          # on a non-mac; or: .venv/bin/python tray.py

Permissions: dictation's global hotkey + paste use pynput. On Windows and X11
Linux that needs no OS grant; on Wayland global hooks are restricted (see
/api/perms). GUI deps (pystray, pillow) are imported lazily so importing this
module never requires them.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
import webbrowser

BASE = "http://127.0.0.1:8123"
PORT = 8123


# ─── tiny HTTP client (stdlib — no extra deps; copied from menubar.py) ───────
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


# ─── server, launched in-process (mirrors menubar.start_server_in_thread) ────
def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_server_in_thread() -> bool:
    """Start uvicorn(server.app) on a daemon thread. Returns True if we own it,
    False if an external server already holds the port (we just control it)."""
    if _port_in_use(PORT):
        return False
    import uvicorn
    import server  # its module-level side effects (idle-watch thread) are fine here
    cfg = uvicorn.Config(server.app, host="127.0.0.1", port=PORT, log_level="warning")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    return True


# ─── tray icon glyphs (drawn with PIL; lazy import) ──────────────────────────
_STATE_COLORS = {"down": (150, 150, 150), "released": (120, 120, 140),
                 "armed": (60, 200, 110), "recording": (220, 70, 70)}


def _icon_image(state: str):
    """A 64×64 status dot for the tray. PIL is imported lazily here."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = _STATE_COLORS.get(state, _STATE_COLORS["down"])
    d.ellipse((10, 10, 54, 54), fill=color)
    if state == "recording":
        d.ellipse((26, 26, 38, 38), fill=(255, 255, 255))   # inner "rec" dot
    return img


# ─── the tray app ────────────────────────────────────────────────────────────
class VoxwireTray:
    def __init__(self):
        self.owns_server = False
        self._icon = None
        self._halt = False
        self._devices: list[dict] = []
        self._models: dict[str, dict] = {}
        self._cur_device: int | None = None
        self._cur_model: str | None = None
        self._state = "down"
        self._status = "starting…"
        self._dict_on = False
        self._dict_hotkey = "?"
        self._perms = ""
        self._battery = "Throat mic: —"
        self._tick = 0

    # ── lifecycle ──
    def run(self):
        import pystray
        self.owns_server = start_server_in_thread()
        self._icon = pystray.Icon("voxwire", _icon_image("down"), "Voxwire",
                                  menu=self._build_menu())
        # pystray calls `setup` on its own thread once the icon is live — start
        # polling there so menu/tooltip/icon track the server.
        self._icon.run(setup=self._on_ready)

    def _on_ready(self, icon):
        icon.visible = True
        threading.Thread(target=self._poll_loop, daemon=True).start()

    # ── polling ──
    def _poll_loop(self):
        while not self._halt:
            try:
                self._refresh_fast()
                self._tick += 1
                if self._tick % 15 == 1:      # heavier refreshes ~every 30s
                    self._refresh_slow()
                if self._icon is not None:
                    self._icon.icon = _icon_image(self._state)
                    self._icon.title = f"Voxwire — {self._status}"
                    self._icon.update_menu()
            except Exception:
                import traceback
                traceback.print_exc()   # surface poll failures instead of hiding them
            time.sleep(2)

    def _refresh_fast(self):
        lvl = get("/api/level")
        if lvl is None:
            self._state, self._status = "down", "server unreachable"
            return
        armed, rec = lvl.get("warm"), lvl.get("recording")
        self._state = "recording" if rec else ("armed" if armed else "released")
        db, idle = lvl.get("level_db"), lvl.get("idle_left")
        self._status = (self._state
                        + (f" · {db:.0f} dB" if db is not None and armed else "")
                        + (f" · sleeps in {idle}s" if idle else ""))
        d = get("/api/dictation/status") or {}
        self._dict_on = bool(d.get("enabled"))
        self._dict_hotkey = d.get("hotkey", "?")

    def _refresh_slow(self):
        d = get("/api/devices")
        if d:
            self._devices = d.get("devices", [])
            self._models = {m["key"]: m for m in d.get("models", [])}
            if self._cur_device is None and self._devices:
                # prefer the warm device, then the OS-default input, then the first
                warm = d.get("warm_device")
                dflt = next((x["index"] for x in self._devices if x.get("is_default")), None)
                self._cur_device = warm if warm is not None else (
                    dflt if dflt is not None else self._devices[0]["index"])
        p = get("/api/perms") or {}
        if p.get("applicable"):
            def mark(v): return "✓" if v is True else ("✗" if v is False else "?")
            self._perms = (f"Permissions — Input Mon {mark(p.get('input_monitoring'))} · "
                           f"Accessibility {mark(p.get('accessibility'))}")
        else:
            self._perms = "Permissions: not required on this OS"
        b = get("/api/mic/battery") or {}
        self._battery = (f"Throat mic: {b.get('battery','—')}"
                         + (f" · {b.get('rssi')} dBm" if b.get("rssi") else "")) \
            if b.get("available") else "Throat mic: not connected"

    # ── menu ──
    def _build_menu(self):
        import pystray
        MI, Menu = pystray.MenuItem, pystray.Menu
        return Menu(
            MI(lambda item: self._status, None, enabled=False),
            Menu.SEPARATOR,
            MI(lambda item: "Release mic" if self._state in ("armed", "recording")
               else "Arm mic", self._toggle_arm,
               checked=lambda item: self._state in ("armed", "recording")),
            MI("Device", Menu(self._device_items)),
            MI("Model", Menu(self._model_items)),
            MI(lambda item: (f"Global dictation: on ({self._dict_hotkey})"
                             if self._dict_on else "Global dictation: off"),
               self._toggle_dictation, checked=lambda item: self._dict_on),
            Menu.SEPARATOR,
            MI(lambda item: self._perms or "Permissions: …", None, enabled=False),
            MI(lambda item: self._battery, None, enabled=False),
            Menu.SEPARATOR,
            MI("Open web config…", self._open_web),
            MI("■ Kill switch", self._kill),
            MI("Quit Voxwire", self._quit),
        )

    def _device_items(self):
        """Generator of radio MenuItems for the input devices (re-read each open)."""
        import pystray
        for dev in self._devices:
            idx = dev["index"]
            tag = " ★IASUS" if "IASUS" in dev.get("tags", []) else ""
            yield pystray.MenuItem(
                f'{dev["name"]}{tag}',
                lambda icon, item, i=idx: self._pick_device(i),
                radio=True, checked=lambda item, i=idx: self._cur_device == i)

    def _model_items(self):
        import pystray
        for key, m in self._models.items():
            label = m["label"] + ("  ✓" if m.get("cached") else f"  ⬇{m.get('approx_gb','?')}GB")
            yield pystray.MenuItem(
                label, lambda icon, item, k=key: self._pick_model(k),
                radio=True, checked=lambda item, k=key: self._cur_model == k)

    # ── actions ──
    def _toggle_arm(self, icon, item):
        if self._state in ("armed", "recording"):
            post("/api/mic/release", {})
        else:
            dev = self._cur_device if self._cur_device is not None else (
                self._devices[0]["index"] if self._devices else 0)
            post("/api/mic/arm", {"device_index": dev})

    def _pick_device(self, index: int):
        self._cur_device = index
        post("/api/mic/arm", {"device_index": index})
        if self._dict_on:   # else the next hotkey press reverts to the old mic
            post("/api/dictation", {"enabled": True, "device_index": index})

    def _pick_model(self, key: str):
        self._cur_model = key
        d = get("/api/dictation/status") or {}
        if d.get("enabled"):
            post("/api/dictation", {"enabled": True, "model": key})

    def _notify(self, title: str, message: str):
        """Surface a message via a native notification if the tray backend
        supports it, and always reflect it in the tray status/tooltip."""
        try:
            if self._icon is not None:
                self._icon.notify(message, title)
        except Exception:
            pass
        self._status = f"{title}: {message}"

    def _toggle_dictation(self, icon, item):
        if self._dict_on:
            post("/api/dictation", {"enabled": False})
        else:
            body = {"enabled": True, "device_index": self._cur_device or 0}
            if self._cur_model:
                body["model"] = self._cur_model
            r = post("/api/dictation", body)
            if r is None or r.get("error"):   # e.g. pynput can't hook under Wayland
                self._notify("Dictation failed",
                             (r or {}).get("error") or "server unreachable")

    def _open_web(self, icon, item):
        webbrowser.open(BASE)

    def _kill(self, icon, item):
        post("/api/mic/release", {})
        post("/api/dictation", {"enabled": False})
        self._state, self._status = "released", "killed — re-arm to resume"

    def _quit(self, icon, item):
        self._halt = True
        try:
            post("/api/mic/release", {})
            post("/api/dictation", {"enabled": False})
        finally:
            icon.stop()


def main():
    VoxwireTray().run()


if __name__ == "__main__":
    main()
