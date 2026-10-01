#!/usr/bin/env python3
"""Voxwire STT + dictation server — murmur/speak into a throat mic (e.g. IASUS)
or any input, hear exactly what the mic captured, and transcribe on-device.

This is the FastAPI service behind Voxwire's menubar app and web config: STT,
warm-mic capture, and push-to-talk dictation. The transcript → integration
routing lives in `gateway.py`; STT engines are pluggable backends in `stt/`.

Mic model: the input stream can be ARMED (kept warm) so a Bluetooth HFP/SCO
link stays established → press-to-talk is instant with no ~1-2s link-setup
silence. Arming holds the mic active (drains its battery), so it can be released
on demand and auto-releases after an idle timeout. On macOS the live battery %
is read so you can watch the cost.

Run:
    ./run.sh              # sets up venv + launches
    python server.py      # -> http://127.0.0.1:8123
"""
from __future__ import annotations

import asyncio
import collections
import ipaddress
import json
import math
import os
import platform
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from importlib.util import find_spec
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import numpy as np
import scipy.signal as sps
import sounddevice as sd
import soundfile as sf
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import AfterValidator, BaseModel
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketClose

import faithful    # the fixup's faithfulness guard (rule 4): only mis-hearing corrections pass
import fusion      # dual-mic (throat + air) fusion DSP — pure, no I/O
import gateway     # generic transcript → integration router (pure-python, no heavy deps)
import osplatform  # runtime-selected OS layer: clipboard/paste/perms/confirm (mac/win/linux)
import stt         # pluggable STT backends (MLX on Apple Silicon, faster-whisper elsewhere)
import tiers       # the ONE LLM model tier table (fast | heavy | cloud) — rule 5

HERE = Path(__file__).resolve().parent
RECORDINGS_DIR = HERE / "recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)
TARGET_SR = 16000
HF_HUB = Path.home() / ".cache" / "huggingface" / "hub"
BUF_SECONDS = 30
PREROLL_SECONDS = 0.8
IDLE_DEFAULT = 120          # auto-release the warm mic after this many idle seconds (0 = never)

# ─── STT models + backends ─────────────────────────────────────────────────
# Model IDs live in ONE registry (voxwire/stt/models.py — CLAUDE.md rule 5), and
# each names the backend that runs it. `stt` picks MLX on Apple Silicon and
# faster-whisper elsewhere; this server never imports a platform STT runtime.
DEFAULT_MODEL = stt.default_model() or next(iter(stt.MODELS))

# ─── Command-correction (fixup) LLM ──────────────────────────────────────────
# Faithful cleanup ONLY (rule 4): fix mishearings, never invent/expand, skip
# ≤2-word inputs, OFF by default. Zero-config: on Apple Silicon it auto-selects
# the best already-downloaded model of the fast tier (else the first, fetched on
# first use) and warms it in the background when you enable fixup. Model IDs live
# in tiers.py (rule 5). Point STT_LLM_BASE_URL at any OpenAI-compatible endpoint
# to use that instead (llama.cpp / Ollama / a gateway); STT_LLM_LOCAL pins a model.
FIXUP_TIER = "fast"
LLM_BASE_URL = os.environ.get("STT_LLM_BASE_URL")            # e.g. http://host:8000/v1
LLM_API_KEY = os.environ.get("STT_LLM_API_KEY", "none")
LLM_MODEL = os.environ.get("STT_LLM_MODEL")                  # remote model name
_llm_cache: dict[str, object] = {}
_llm_load_lock = threading.Lock()   # one load at a time: a warm-up and a first request share it
_fixup = {"model": None, "state": "cold"}                    # cold|loading|ready|error


class FixupUnavailable(RuntimeError):
    """No fixup backend on this host: no STT_LLM_BASE_URL and no on-device mlx_lm."""


def _apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def _mlx_lm_available() -> bool:
    """On-device fixup needs mlx_lm, which only exists on Apple Silicon."""
    return _apple_silicon() and find_spec("mlx_lm") is not None


def fixup_local_model() -> str:
    """The on-device fixup model: an explicit STT_LLM_LOCAL override, else the
    first fast-tier model already downloaded, else the tier's first (fetched on
    first use)."""
    forced = os.environ.get("STT_LLM_LOCAL")
    if forced:
        return forced
    candidates = tiers.models(FIXUP_TIER)
    for repo in candidates:
        if hf_cached(repo):
            return repo
    return candidates[0]


def fixup_available() -> bool:
    """Can fixup run here at all? Remote endpoint, or on-device MLX LM."""
    return bool(LLM_BASE_URL) or _mlx_lm_available()


def _fixup_hint() -> str:
    """What to do when fixup is unavailable on this host."""
    if _apple_silicon():
        return "re-run ./scripts/install.sh to add mlx-lm, or set STT_LLM_BASE_URL"
    return "set STT_LLM_BASE_URL to an OpenAI-compatible endpoint to enable"


def _local_llm(repo: str):
    """Load the on-device model once and return it. Holding the lock through the
    load makes a concurrent warm-up and first request share one load."""
    with _llm_load_lock:
        if repo not in _llm_cache:
            from mlx_lm import load
            _llm_cache[repo] = load(repo)
        return _llm_cache[repo]


def warm_fixup() -> None:
    """Background-load the on-device fixup model so the first correction isn't a
    cold-start stall. No-op for the remote path or where MLX LM isn't available."""
    if LLM_BASE_URL or not _mlx_lm_available():
        return
    repo = fixup_local_model()
    if _fixup["model"] == repo and _fixup["state"] in ("loading", "ready"):
        return
    _fixup.update(model=repo, state="loading")

    def _load():
        try:
            _local_llm(repo)
            _fixup["state"] = "ready"
        except Exception:
            _fixup["state"] = "error"

    threading.Thread(target=_load, daemon=True).start()


def _llm_headers() -> dict:
    return {"Authorization": f"Bearer {LLM_API_KEY}"}


# Model IDs an endpoint lists that obviously can't chat: embeddings, speech, image,
# rerank, moderation. A pattern over names, not a list of models (rule 5).
_NON_CHAT_MODEL = re.compile(
    r"embed|whisper|tts|speech|transcri|audio|rerank|moderation|dall-e|image"
    r"|(^|[/_:.-])(bge|e5|clip)([/_:.-]|$)", re.I)


def _remote_model() -> str | None:
    """STT_LLM_MODEL, else the first chat model the endpoint lists (GET /models),
    else None when the list can't be read, in which case the request leaves
    `model` out and the endpoint picks. Never a made-up name: many endpoints
    reject unknown models. If the endpoint lists only non-chat models (e.g.
    embeddings), fail clearly rather than send a chat request to one."""
    if LLM_MODEL:
        return LLM_MODEL
    if not _fixup.get("remote_model"):
        import httpx
        try:
            r = httpx.get(LLM_BASE_URL.rstrip("/") + "/models", headers=_llm_headers(), timeout=5)
            r.raise_for_status()
            ids = [m.get("id") for m in r.json().get("data", [])
                   if isinstance(m, dict) and m.get("id")]
        except Exception:
            ids = []
        chat = [i for i in ids if not _NON_CHAT_MODEL.search(i)]
        if ids and not chat:
            raise FixupUnavailable(
                f"the fixup endpoint lists no chat model ({', '.join(ids[:5])}); "
                "set STT_LLM_MODEL to one it serves")
        if chat:
            _fixup["remote_model"] = chat[0]   # only a success is cached; failures retry
    return _fixup.get("remote_model")

CORRECT_SYS = (
    "You clean up a raw speech-to-text transcript. Fix ONLY clear phonetic mis-hearings of words "
    "(a throat mic drops consonants like s/f/t/k/p, so some words may be slightly wrong). "
    "HARD RULES: (1) If the transcript is already coherent English, return it EXACTLY unchanged. "
    "(2) NEVER add, expand, rephrase, summarize, complete, or invent words. (3) NEVER turn a short "
    "word or phrase into a longer command or sentence. (4) Preserve the original words, order, and "
    "length. (5) When unsure, return the transcript verbatim. Output ONLY the resulting text.\n"
    "Examples:\n"
    "  continue -> continue\n"
    "  okay how is it going -> okay how is it going\n"
    "  save the current file -> save the current file\n"
    "  lit thee files -> list the files\n"
    "  un thee et again -> run the tests again")


def llm_info() -> dict:
    """Current fixup config + readiness, for the UI. `state` is one of
    ready (loaded in memory) | loading | downloaded (on disk, loads on first use)
    | needs-download | error | unavailable. `cached` is the on-disk half only."""
    if LLM_BASE_URL:
        return {"mode": "remote",
                "model": LLM_MODEL or _fixup.get("remote_model") or "(endpoint default)",
                "endpoint": LLM_BASE_URL, "available": True,
                "state": "ready", "cached": True}
    repo = fixup_local_model()
    if not _mlx_lm_available():
        state = "unavailable"          # non-Apple, or mlx_lm missing: see the hint
    elif repo in _llm_cache:
        state = "ready"                # loaded: the next correction runs immediately
    elif _fixup["model"] == repo and _fixup["state"] in ("loading", "error"):
        state = _fixup["state"]
    else:
        state = "downloaded" if hf_cached(repo) else "needs-download"
    info = {"mode": "local", "model": repo, "endpoint": "on-device (MLX)",
            "available": _mlx_lm_available(), "state": state,
            "cached": hf_cached(repo)}
    if state == "unavailable":
        info["hint"] = _fixup_hint()
    return info


def correct_command(text: str, context: str | None) -> tuple[str, float]:
    words = text.split()
    if len(words) <= 2:
        return text.strip(), 0.0   # short + already clean: trust STT, don't let the LLM invent
    if not fixup_available():
        raise FixupUnavailable(f"fixup is unavailable on this host: {_fixup_hint()}")
    ctx = (context or "").strip()
    user = f'Transcript: "{text}"\n'
    if ctx:
        user += f"(Domain hint, ONLY to disambiguate genuinely mis-heard words: {ctx})\n"
    user += "Cleaned transcript (return unchanged if already correct):"
    t0 = time.time()
    if LLM_BASE_URL:
        import httpx
        body = {"temperature": 0, "max_tokens": 120,
                "messages": [{"role": "system", "content": CORRECT_SYS},
                             {"role": "user", "content": user}]}
        model_name = _remote_model()
        if model_name:
            body["model"] = model_name
        r = httpx.post(LLM_BASE_URL.rstrip("/") + "/chat/completions",
                       headers=_llm_headers(), json=body, timeout=30)
        r.raise_for_status()
        out = r.json()["choices"][0]["message"]["content"].strip()
    else:
        from mlx_lm import generate
        repo = fixup_local_model()
        model, tok = _local_llm(repo)
        _fixup.update(model=repo, state="ready")
        prompt = tok.apply_chat_template(
            [{"role": "system", "content": CORRECT_SYS},
             {"role": "user", "content": user}], add_generation_prompt=True)
        out = generate(model, tok, prompt=prompt, max_tokens=120, verbose=False).strip()
    dt = time.time() - t0
    # anti-hallucination guard: anything but word-for-word mis-hearing fixes (an added,
    # dropped, or swapped-in word) means the model invented — keep the raw transcript
    if not faithful.is_faithful(text, out):
        return text.strip(), dt
    return out, dt


# ─── Warm mic state ─────────────────────────────────────────────────────────
_lock = threading.Lock()
# Serializes warm-stream lifecycle changes (open, reconfigure, release, reap) so
# one can't interleave with another and undo it. The audio callback never takes
# it, so a stream can be stopped while holding it; _lock stays the short-held
# guard for the buffers the callback writes.
_life = threading.Lock()
_warm = {"stream": None, "device": None, "sr": TARGET_SR,
         "blocks": collections.deque(), "total": 0, "mark": None,
         "level_db": -120.0, "last_active": 0.0,
         # dual-mic: channels=2 captures throat(ch0)+air(ch1) from ONE device
         # (a stereo interface / macOS Aggregate Device), fused at finalize.
         "channels": 1, "fusion_mode": "fusion",
         # the channel count last asked of ensure_warm: a stereo request that fell
         # back to mono is still satisfied by the mono stream it got.
         "want_channels": 1}
_idle_timeout = IDLE_DEFAULT
_recordings: list[dict] = []
_rec_counter = 0
_bt_cache = {"ts": 0.0, "data": None}

app = FastAPI(title="Voxwire")


# ─── Device / cache / bluetooth helpers ────────────────────────────────────
def _refresh_devices() -> None:
    """Re-init PortAudio so hotplugged inputs (e.g. a Bluetooth throat mic that
    just connected or dropped) are reflected. PortAudio reads the device list
    only at init, so without this the UI refresh keeps showing stale devices and
    stale indices. Held under the lock and skipped while a stream is open —
    terminating PortAudio would kill an active stream."""
    with _lock:
        if _warm["stream"] is not None:
            return
        try:
            sd._terminate()
            sd._initialize()
        except Exception:
            pass


def list_inputs() -> list[dict]:
    _refresh_devices()
    out = []
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = None
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] < 1:
            continue
        name = d["name"]
        tags = []
        if re.search(r"stealth|iasus", name, re.I):
            tags.append("IASUS")
        if re.search(r"bluetooth|airpod|\bbt\b", name, re.I):
            tags.append("BT")
        out.append({"index": i, "name": name, "samplerate": int(d["default_samplerate"]),
                    "channels": d["max_input_channels"], "tags": tags,
                    "is_default": (i == default_in)})
    return out


def bt_status(name_re: str = r"stealth|iasus") -> dict:
    """Read the connected BT mic's battery % + RSSI from macOS (cached ~45s)."""
    if sys.platform != "darwin":
        return {"available": False}
    now = time.time()
    if _bt_cache["data"] is not None and now - _bt_cache["ts"] < 45:
        return _bt_cache["data"]
    data = {"available": False}
    try:
        out = subprocess.run(["system_profiler", "SPBluetoothDataType", "-detailLevel", "full"],
                             capture_output=True, text=True, timeout=10).stdout
        lines = out.splitlines()
        rx = re.compile(name_re, re.I)
        # walk into the device's indented block
        for idx, ln in enumerate(lines):
            if rx.search(ln) and ln.rstrip().endswith(":"):
                base = len(ln) - len(ln.lstrip())
                fields = {}
                for nxt in lines[idx + 1:]:
                    if nxt.strip() == "":
                        continue
                    ind = len(nxt) - len(nxt.lstrip())
                    if ind <= base:
                        break
                    m = re.match(r"\s+([^:]+):\s*(.+)", nxt)
                    if m:
                        fields[m.group(1).strip()] = m.group(2).strip()
                batt = fields.get("Battery Level")
                if batt:  # only present when connected
                    data = {"available": True, "name": ln.strip().rstrip(":"),
                            "battery": batt, "rssi": fields.get("RSSI"),
                            "services": fields.get("Services", "")}
                break
    except Exception:
        pass
    _bt_cache.update(ts=now, data=data)
    return data


def hf_cached(repo: str) -> bool:
    d = HF_HUB / ("models--" + repo.replace("/", "--"))
    if not d.exists():
        return False
    blobs = d / "blobs"
    if blobs.exists() and list(blobs.glob("*.incomplete")):
        return False
    snaps = d / "snapshots"
    return snaps.exists() and any(snaps.iterdir())


def _pick_samplerate(device_index: int) -> int:
    for sr in (TARGET_SR, 48000, 44100):
        try:
            sd.check_input_settings(device=device_index, samplerate=sr, channels=1)
            return sr
        except Exception:
            continue
    return int(sd.query_devices(device_index)["default_samplerate"])


def _resample_to_target(x: np.ndarray, sr: int) -> np.ndarray:
    if sr == TARGET_SR:
        return x.astype(np.float32)
    g = math.gcd(sr, TARGET_SR)
    return sps.resample_poly(x, TARGET_SR // g, sr // g).astype(np.float32)


def clip_stats(clip16k: np.ndarray, native_sr: int) -> dict:
    eps = 1e-10
    peak = float(np.max(np.abs(clip16k))) if clip16k.size else 0.0
    rms = float(np.sqrt(np.mean(clip16k ** 2)) + eps) if clip16k.size else eps
    win, hop = max(1, int(0.03 * TARGET_SR)), max(1, int(0.01 * TARGET_SR))
    wr = [float(np.sqrt(np.mean(clip16k[i:i + win] ** 2)) + eps)
          for i in range(0, max(0, len(clip16k) - win), hop)]
    wr_arr = np.array(wr) if wr else np.array([rms])
    noise = max(float(np.percentile(wr_arr, 10)), 1e-4)
    signal = float(np.percentile(wr_arr, 90))

    def db(v: float) -> float:
        return round(20 * math.log10(max(v, eps)), 1)

    return {"duration_s": round(len(clip16k) / TARGET_SR, 2), "native_sr": native_sr,
            "rms_db": db(rms), "peak_db": db(peak),
            "snr_db": round(20 * math.log10(signal / noise), 1),
            "low_bandwidth": native_sr < 16000}


# ─── Warm stream management ────────────────────────────────────────────────
def _warm_cb(indata, frames, _t, _s):  # noqa: ANN001 — audio thread
    with _lock:
        # mono: 1-D block; dual-mic: keep both channels (frames, 2) for fusion.
        block = indata[:, 0].copy() if _warm["channels"] == 1 else indata[:, :2].copy()
        _warm["blocks"].append((_warm["total"], block))
        _warm["total"] += frames
        keep_from = _warm["total"] - int(BUF_SECONDS * _warm["sr"])
        blk = _warm["blocks"]
        while blk and (blk[0][0] + len(blk[0][1])) < keep_from:
            blk.popleft()
    rms = float(np.sqrt(np.mean(indata ** 2)) + 1e-10)
    _warm["level_db"] = 20 * math.log10(max(rms, 1e-6))


def _stream_alive(stream) -> bool:
    """True only if `stream` is still capturing. When a warm mic's device
    vanishes — e.g. the Bluetooth throat mic disconnects — PortAudio reports the
    stream inactive or raises when queried; either means dead. Used so a mic
    hotplug never wedges the warm slot (the server process itself is unaffected
    by mic state and keeps running 24/7)."""
    if stream is None:
        return False
    try:
        return bool(stream.active)
    except Exception:
        return False


def _close_stream(stream) -> None:
    """Stop and close a stream already detached from _warm. Never call this while
    holding _lock: PortAudio waits for the running callback, which takes _lock."""
    if stream is None:
        return
    try:
        stream.stop()
    except Exception:
        pass            # e.g. the device vanished; close() must still run
    try:
        stream.close()
    except Exception:
        pass


def _open_input(device_index: int, channels: int, sr: int):
    """Open and start an input stream, falling back to mono if the device can't
    give the requested channel count (so dual-mic never breaks single-mic
    capture). A stream that fails to start is closed before the next attempt."""
    def start(ch: int):
        stream = sd.InputStream(device=device_index, channels=ch,
                                samplerate=sr, dtype="float32", callback=_warm_cb)
        try:
            stream.start()
        except Exception:
            try:
                stream.close()       # never started, so this can't wait on the callback
            except Exception:
                pass
            raise
        return stream

    try:
        return start(channels), channels
    except Exception:
        if channels == 1:
            raise
    return start(1), 1


def ensure_warm(device_index: int, channels: int = 1) -> int:
    channels = 2 if channels >= 2 else 1
    # _life makes the whole detach → close → open sequence one step: a second
    # caller used to open its stream in the gap, then get overwritten, leaving a
    # live stream nothing referenced (and a release in the gap got undone).
    with _life:
        with _lock:
            # Reuse the warm stream only if it matches AND is still alive — a vanished
            # device (dropped throat mic) reads as dead, so we rebuild it.
            if (_warm["stream"] is not None and _warm["device"] == device_index
                    and channels in (_warm["channels"], _warm["want_channels"])
                    and _stream_alive(_warm["stream"])):
                _warm["last_active"] = time.time()
                return _warm["sr"]
            # Detach the old stream under the lock, but tear it down *outside* it:
            # PortAudio's stop()/close() blocks until the running callback returns,
            # and _warm_cb also takes _lock — closing while holding _lock deadlocks
            # (callback waits for the lock, we wait for the callback) and takes the
            # whole server unreachable. release_warm() does the same dance.
            old = _warm["stream"]
            _warm["stream"] = None
        _close_stream(old)
        with _lock:
            sr = _pick_samplerate(device_index)
            _warm.update(device=device_index, sr=sr, blocks=collections.deque(),
                         total=0, mark=None, level_db=-120.0, last_active=time.time())
            stream, got = _open_input(device_index, channels, sr)
            _warm["stream"] = stream
            _warm["channels"] = got
            # Remember what was asked, not what we got: comparing the next request
            # against a mono fallback reopened the mic on every arm/record start,
            # losing the warm stream and its pre-roll.
            _warm["want_channels"] = channels
            return sr


def release_warm() -> None:
    with _life:
        with _lock:
            s = _warm["stream"]
            _warm.update(stream=None, device=None, blocks=collections.deque(),
                         total=0, mark=None, level_db=-120.0)
        _close_stream(s)


def _reap_dead_warm() -> bool:
    """Release the warm slot if its stream has died (the mic's device vanished —
    e.g. the throat mic disconnected) and we are not mid-capture, so PortAudio
    can re-init and the mic can be re-armed cleanly when it comes back. Returns
    True if it reaped. Never touches the server process — that stays up whether
    the stealth mic is connected or not."""
    dead = None
    with _life:
        with _lock:
            stream = _warm["stream"]
            recording = _warm["mark"] is not None
            if stream is not None and not recording and not _stream_alive(stream):
                # Clear the slot atomically, and only for the exact stream we checked,
                # never interrupting a just-started capture. Stop/close the dead
                # stream outside the lock — it can block.
                dead = stream
                _warm.update(stream=None, device=None, blocks=collections.deque(),
                             total=0, mark=None, level_db=-120.0)
        _close_stream(dead)
    return dead is not None


def _idle_watch() -> None:
    while True:
        time.sleep(3)
        try:
            if _reap_dead_warm():
                continue
            with _lock:
                warm = _warm["stream"] is not None
                recording = _warm["mark"] is not None
                idle = time.time() - _warm["last_active"]
                to = _idle_timeout
            if warm and not recording and to > 0 and idle > to:
                release_warm()
        except Exception:
            pass


def _gather(from_frame: int, to_frame: int) -> np.ndarray:
    parts = []
    for start, blk in list(_warm["blocks"]):
        end = start + len(blk)
        if end <= from_frame or start >= to_frame:
            continue
        a = max(0, from_frame - start)
        b = min(len(blk), to_frame - start)
        parts.append(blk[a:b])
    if parts:
        return np.concatenate(parts)          # 1-D (mono) or (frames, 2) (dual)
    return np.zeros((1, 2) if _warm["channels"] == 2 else 1, dtype=np.float32)


def _finalize_clip(raw: np.ndarray, native_sr: int, fusion_mode: str = "fusion") -> dict:
    """Resample + save a captured clip and register it as a recording. A 2-channel
    capture (throat=ch0, air=ch1) is fused to mono before saving (BYT-118)."""
    global _rec_counter
    if raw.ndim == 2 and raw.shape[1] >= 2:
        throat = _resample_to_target(raw[:, 0], native_sr)
        air = _resample_to_target(raw[:, 1], native_sr)
        clip = fusion.fuse(throat, air, TARGET_SR, mode=fusion_mode, do_align=False)
    else:
        clip = _resample_to_target(raw if raw.ndim == 1 else raw[:, 0], native_sr)
    _rec_counter += 1
    rid = _rec_counter
    ts = datetime.now()
    fname = f"rec_{ts:%Y%m%d_%H%M%S}_{rid}.wav"
    sf.write(RECORDINGS_DIR / fname, clip, TARGET_SR)
    entry = {"id": rid, "file": fname, "ts": ts.strftime("%H:%M:%S"),
             "stats": clip_stats(clip, native_sr), "transcripts": {}}
    _recordings.append(entry)
    return entry


# ─── Text insertion + global push-to-talk dictation ────────────────────────
_dict = {"enabled": False, "hotkey": "alt+cmd", "insert_mode": "command",
         "model": DEFAULT_MODEL, "context": "", "fixup": False, "device": 0,
         "recording": False, "last": "", "listener": None, "status": "off",
         "chord": ["alt", "cmd"], "channels": 1, "fusion_mode": "fusion",
         "configured": False}
_pressed: set[str] = set()   # keys currently held (for chord detection)


def insert_text(text: str) -> bool:
    """Put text on the clipboard and paste it into the frontmost app, via the
    platform layer (Command+V on macOS, Control+V on Windows/Linux).

    Pasting needs the OS input-synthesis grant where one applies (on macOS,
    Accessibility — see /api/perms). Best-effort: returns False if the platform
    can't copy or paste here; when only the paste step is unavailable the text is
    still left on the clipboard."""
    return osplatform.paste_text(text)


# Chord support: a hotkey is one or more keys held together ("+"-separated).
_TOKEN_MAP = {"command": "cmd", "cmd": "cmd", "⌘": "cmd", "control": "ctrl", "ctrl": "ctrl",
              "^": "ctrl", "option": "alt", "opt": "alt", "alt": "alt", "⌥": "alt",
              "shift": "shift", "⇧": "shift", "spacebar": "space", "space": "space",
              "return": "enter", "enter": "enter"}
_MOD_GROUPS = {"cmd": {"cmd", "cmd_l", "cmd_r"}, "ctrl": {"ctrl", "ctrl_l", "ctrl_r"},
               "alt": {"alt", "alt_l", "alt_r", "alt_gr"}, "shift": {"shift", "shift_l", "shift_r"}}


def _parse_chord(spec: str) -> list[str]:
    toks = [_TOKEN_MAP.get(t.strip().lower(), t.strip().lower())
            for t in str(spec).split("+") if t.strip()]
    return toks or ["f8"]


def _norm_key(key) -> str | None:
    from pynput import keyboard
    if isinstance(key, keyboard.KeyCode):
        return (key.char or "").lower() or None
    return getattr(key, "name", None)


def _token_ok(token: str, pressed: set) -> bool:
    if token in _MOD_GROUPS:
        return bool(_MOD_GROUPS[token] & pressed)
    return token in pressed


def _chord_held(pressed: set) -> bool:
    ch = _dict.get("chord") or []
    return bool(ch) and all(_token_ok(t, pressed) for t in ch)


def _dictation_worker(frm: int, to: int, sr: int) -> None:
    """Runs off the key-listener thread: transcribe → fixup → paste."""
    try:
        with _lock:
            raw = _gather(frm, to)
        entry = _finalize_clip(raw, sr, _dict["fusion_mode"])
        text = stt.transcribe(RECORDINGS_DIR / entry["file"], _dict["model"]).text
        entry["transcripts"][_dict["model"]] = text
        final = text
        if _dict["fixup"] and text.strip() and _dict["insert_mode"] == "command":
            try:
                final, _ = correct_command(text, _dict["context"])
                entry["corrected"] = final
            except Exception:
                final = text
        if final.strip():
            insert_text(final + " ")
            _dict["last"] = final
    except Exception as e:
        _dict["last"] = f"[error] {type(e).__name__}: {e}"
    finally:
        _dict["recording"] = False


def _on_press(key) -> None:
    if not _dict["enabled"]:
        return
    k = _norm_key(key)
    if k:
        _pressed.add(k)
    if not _dict["recording"] and _chord_held(_pressed):
        try:
            _warm["fusion_mode"] = _dict["fusion_mode"]
            sr = ensure_warm(_dict["device"], _dict["channels"])
            with _lock:
                _warm["mark"] = max(0, _warm["total"] - int(PREROLL_SECONDS * sr))
                _warm["last_active"] = time.time()
            _dict["recording"] = True
        except Exception as e:
            _dict["last"] = f"[mic error] {e}"


def _on_release(key) -> None:
    k = _norm_key(key)
    if k:
        _pressed.discard(k)
    if _dict["recording"] and not _chord_held(_pressed):
        with _lock:
            if _warm["mark"] is None:
                _dict["recording"] = False
                return
            frm, to, sr = _warm["mark"], _warm["total"], _warm["sr"]
            _warm["mark"] = None
            _warm["last_active"] = time.time()
        threading.Thread(target=_dictation_worker, args=(frm, to, sr), daemon=True).start()


def start_dictation() -> None:
    from pynput import keyboard
    stop_dictation()
    _dict["chord"] = _parse_chord(_dict["hotkey"])
    _pressed.clear()
    ensure_warm(_dict["device"], _dict["channels"])   # dual-mic warms stereo up front
    listener = keyboard.Listener(on_press=_on_press, on_release=_on_release)
    listener.start()
    _dict["listener"] = listener
    _dict["status"] = "listening"


def stop_dictation() -> None:
    lis = _dict.get("listener")
    if lis is not None:
        try:
            lis.stop()
        except Exception:
            pass
    _dict["listener"] = None
    _dict["recording"] = False
    _dict["status"] = "off"


# ─── API models ────────────────────────────────────────────────────────────
def _known_fusion_mode(mode: str) -> str:
    if mode not in fusion.MODES:
        raise ValueError(f"fusion_mode must be one of: {', '.join(fusion.MODES)}")
    return mode


# Checked at the door (422), not later inside fusion.fuse (500).
FusionMode = Annotated[str, AfterValidator(_known_fusion_mode)]


class ArmReq(BaseModel):
    device_index: int
    idle_timeout: int | None = None   # seconds; 0 = never auto-release
    channels: int = 1                 # 2 = dual-mic (throat ch0 + air ch1) from one device
    fusion_mode: FusionMode = "fusion"


class DeviceReq(BaseModel):
    device_index: int
    channels: int = 1
    fusion_mode: FusionMode = "fusion"


class TranscribeReq(BaseModel):
    model: str = DEFAULT_MODEL
    recording_id: int | None = None


class CorrectReq(BaseModel):
    text: str | None = None
    recording_id: int | None = None
    context: str | None = None


# ─── Routes ────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (HERE / "index.html").read_text()


@app.get("/api/devices")
def devices() -> dict:
    # Only surface models whose backend actually runs on this machine, so a
    # Windows/Linux box sees the faster-whisper models and a Mac sees MLX.
    avail = stt.available_models()
    models = [{"key": k, "label": s.label, "backend": s.backend,
               "cached": hf_cached(s.repo), "approx_gb": s.approx_gb}
              for k, s in avail.items()]
    # keep default_model inside the offered set — it can fall outside when no STT
    # backend is installed, which would break the UI's default selection.
    default = DEFAULT_MODEL if DEFAULT_MODEL in avail else next(iter(avail), None)
    return {"devices": list_inputs(), "models": models, "default_model": default,
            "backends": stt.available_backends(),
            "warm_device": _warm["device"], "preroll_ms": int(PREROLL_SECONDS * 1000),
            "idle_default": IDLE_DEFAULT, "llm": llm_info()}


@app.get("/api/mic/battery")
def mic_battery() -> dict:
    return bt_status()


@app.post("/api/mic/arm")
def mic_arm(req: ArmReq) -> dict:
    global _idle_timeout
    if req.idle_timeout is not None:
        _idle_timeout = max(0, req.idle_timeout)
    _warm["fusion_mode"] = req.fusion_mode
    try:
        sr = ensure_warm(req.device_index, req.channels)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return {"ok": True, "samplerate": sr, "narrowband": sr < 16000,
            "idle_timeout": _idle_timeout, "channels": _warm["channels"],
            "dual_mic": _warm["channels"] == 2, "fusion_mode": _warm["fusion_mode"]}


@app.post("/api/mic/release")
def mic_release() -> dict:
    release_warm()
    return {"ok": True}


@app.post("/api/mic/idle")
def mic_idle(body: dict) -> dict:
    global _idle_timeout
    _idle_timeout = max(0, int(body.get("idle_timeout", IDLE_DEFAULT)))
    return {"ok": True, "idle_timeout": _idle_timeout}


@app.post("/api/record/start")
def record_start(req: DeviceReq) -> dict:
    _warm["fusion_mode"] = req.fusion_mode
    try:
        sr = ensure_warm(req.device_index, req.channels)  # auto-arms if released
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    with _lock:
        _warm["mark"] = max(0, _warm["total"] - int(PREROLL_SECONDS * sr))
        _warm["last_active"] = time.time()
    return {"ok": True, "samplerate": sr, "dual_mic": _warm["channels"] == 2}


@app.post("/api/record/stop")
def record_stop() -> dict:
    with _lock:
        if _warm["mark"] is None:
            return JSONResponse({"error": "not recording"}, status_code=409)
        frm, to, sr = _warm["mark"], _warm["total"], _warm["sr"]
        raw = _gather(frm, to)
        fmode = _warm["fusion_mode"]
        _warm["mark"] = None
        _warm["last_active"] = time.time()
    entry = _finalize_clip(raw, sr, fmode)
    return {"ok": True, "dual_mic": raw.ndim == 2, **entry}


@app.get("/api/level")
def level() -> dict:
    with _lock:
        warm = _warm["stream"] is not None
        idle_left = None
        if warm and _idle_timeout > 0 and _warm["mark"] is None:
            idle_left = max(0, int(_idle_timeout - (time.time() - _warm["last_active"])))
        return {"warm": warm, "recording": _warm["mark"] is not None,
                "level_db": round(_warm["level_db"], 1), "idle_left": idle_left,
                "channels": _warm["channels"], "dual_mic": _warm["channels"] == 2,
                "dict_enabled": _dict["enabled"], "dict_recording": _dict["recording"],
                "dict_status": _dict["status"], "dict_last": _dict["last"]}


@app.get("/api/recordings")
def recordings() -> dict:
    return {"recordings": list(reversed(_recordings))}


@app.get("/api/recordings/{rid}.wav")
def recording_wav(rid: int):
    entry = next((r for r in _recordings if r["id"] == rid), None)
    if not entry:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(RECORDINGS_DIR / entry["file"], media_type="audio/wav",
                        headers={"Cache-Control": "no-store"})


@app.delete("/api/recordings/{rid}")
def recording_delete(rid: int) -> dict:
    global _recordings
    entry = next((r for r in _recordings if r["id"] == rid), None)
    if entry:
        (RECORDINGS_DIR / entry["file"]).unlink(missing_ok=True)
        _recordings = [r for r in _recordings if r["id"] != rid]
    return {"ok": True}


@app.post("/api/transcribe")
def transcribe(req: TranscribeReq) -> dict:
    if req.model not in stt.MODELS:
        return JSONResponse({"error": f"unknown model {req.model}"}, status_code=400)
    if not stt.model_available(req.model):
        return JSONResponse(
            {"error": f"model {req.model} is not available on this host"},
            status_code=409)
    if req.recording_id is not None:
        entry = next((r for r in _recordings if r["id"] == req.recording_id), None)
    else:
        entry = _recordings[-1] if _recordings else None
    if not entry:
        return JSONResponse({"error": "no recording yet"}, status_code=400)

    wav_path = RECORDINGS_DIR / entry["file"]
    spec = stt.MODELS[req.model]
    t_start = time.time()
    try:
        tr = stt.transcribe(wav_path, req.model)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)

    text = tr.text
    total_s = time.time() - t_start
    dur = entry["stats"]["duration_s"] or 0.001
    entry["transcripts"][req.model] = text
    return {"text": text, "model": req.model, "model_label": spec.label,
            "recording_id": entry["id"], "load_s": round(tr.load_s, 2),
            "infer_s": round(tr.infer_s, 2), "total_s": round(total_s, 2),
            "audio_s": round(dur, 2),
            "rtf": round(tr.infer_s / dur, 3) if dur > 0 else None}


@app.post("/api/correct")
def correct(req: CorrectReq) -> dict:
    text = req.text
    entry = None
    if req.recording_id is not None:
        entry = next((r for r in _recordings if r["id"] == req.recording_id), None)
        if text is None and entry and entry["transcripts"]:
            text = list(entry["transcripts"].values())[-1]
    if not text or not text.strip():
        return JSONResponse({"error": "no text to correct"}, status_code=400)
    try:
        out, dt = correct_command(text, req.context)
    except FixupUnavailable as e:
        return JSONResponse({"error": str(e), **llm_info()}, status_code=409)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    if entry is not None:
        entry["corrected"] = out
    return {"corrected": out, "raw": text, "latency_s": round(dt, 2), **llm_info()}


@app.get("/api/fixup")
def fixup_status() -> dict:
    """Fixup readiness, for the UI to follow while a model warms up."""
    return llm_info()


class ClipboardReq(BaseModel):
    text: str


@app.post("/api/clipboard")
def clipboard(req: ClipboardReq) -> dict:
    return {"ok": osplatform.copy_text(req.text or "")}


# ─── Command gateway (transcript → integration plugins) ─────────────────────
class CommandReq(BaseModel):
    transcript: str
    source: str = "unknown"           # which input produced it, e.g. "throat", "air"
    confidence: float = 1.0
    context: str | None = None        # optional domain hint, passed through to the plugin


@app.post("/api/command")
def command(req: CommandReq) -> dict:
    """Route a (cleaned) transcript through the registered integrations.

    A gated action comes back with requires_confirmation=True + a preview; the
    gateway does NOT execute it — the executor host owns that gate.
    """
    meta = {"context": req.context} if req.context else {}
    return gateway.dispatch(req.transcript, source=req.source,
                            confidence=req.confidence, meta=meta)


@app.get("/api/integrations")
def integrations() -> dict:
    # Only plugin name + exception type: a failed overlay's message can hold a
    # token or an internal host name. The full detail is logged locally.
    return {"integrations": gateway.integrations(), "errors": gateway.load_error_summary()}


def _transcribe_pcm(pcm: bytes, cfg: dict) -> dict:
    """Decode a streamed PCM utterance → fuse (if stereo) → transcribe. Blocking;
    runs off the event loop."""
    try:
        clip = fusion.decode_pcm(pcm, int(cfg["channels"]), int(cfg["sr"]), cfg["mode"])
        entry = _finalize_clip(clip, TARGET_SR)          # already mono 16 kHz
        text = stt.transcribe(RECORDINGS_DIR / entry["file"], cfg["model"]).text
        entry["transcripts"][cfg["model"]] = text
        out = {"ok": True, "recording_id": entry["id"], "transcript": text,
               "dual_mic": int(cfg["channels"]) >= 2, "mode": cfg["mode"]}
        if cfg.get("paste") and text.strip():
            insert_text(text + " ")
        return out
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


MAX_STREAM_BYTES = 32 * 1024 * 1024    # ~8 min of 16 kHz stereo int16 per utterance
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _loopback_peer(client) -> bool:
    """True only for a peer on this machine. The server binds 127.0.0.1, but the
    endpoint must not rely on that: if it is ever reached from the network (a
    0.0.0.0 bind, a port forward) an unauthenticated LAN client could stream
    audio and have the transcript pasted at your cursor. Deny by default; an
    unknown peer is not loopback. A network capture node needs an authenticated
    listener, which does not exist yet (docs/capture-node.md)."""
    host = getattr(client, "host", None)
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _loopback_host(host: str | None) -> bool:
    """True only when a Host header names this machine (127.0.0.1, localhost or
    ::1, on any port). A web page that reaches the server through DNS rebinding
    talks to it under the page's own name, and its Host says so."""
    if not host:
        return False
    h = urlsplit(f"//{host}")
    return h.hostname in _LOOPBACK_HOSTS and h.username is None and h.password is None


def _trusted_origin(origin: str | None, host: str | None) -> bool:
    """Browsers send an Origin on cross-site requests, on every POST, and on every
    WebSocket. Allow clients that send none (the menubar, the capture node,
    scripts, curl) and this server's own page on a loopback host. Without this,
    any web page could drive the API (CSRF) or stream audio in and have the
    transcript pasted at your cursor: WebSockets skip CORS entirely, and a
    simple POST needs no preflight. Checking the Host too defeats DNS rebinding."""
    if origin is None:
        return True
    o = urlsplit(origin)
    h = urlsplit(f"//{host}") if host else None
    return (o.scheme in ("http", "https") and h is not None
            and o.hostname in _LOOPBACK_HOSTS and h.hostname in _LOOPBACK_HOSTS
            and o.netloc == h.netloc)


def _local_request(conn: HTTPConnection) -> bool:
    """The one gate in front of every route: a peer on this machine, addressed to
    a loopback name, and, if it is a browser, on Voxwire's own page."""
    host = conn.headers.get("host")
    return (_loopback_peer(conn.client) and _loopback_host(host)
            and _trusted_origin(conn.headers.get("origin"), host))


class LocalOnly:
    """ASGI middleware that refuses any HTTP request or WebSocket failing
    `_local_request`, before a route runs. Deny by default: a route added later
    is covered without doing anything. Voxwire has no authentication, so this is
    what keeps other web pages and other machines from arming the mic, reading
    recordings, writing the clipboard or routing commands."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket") and not _local_request(HTTPConnection(scope)):
            refuse = (PlainTextResponse("Voxwire answers only its own page on this machine.",
                                        status_code=403)
                      if scope["type"] == "http" else WebSocketClose(code=1008))
            await refuse(scope, receive, send)
            return
        await self.app(scope, receive, send)


app.add_middleware(LocalOnly)


def _stream_start(cfg: dict, evt: dict) -> tuple[dict | None, str | None]:
    """Apply a start frame to a copy of cfg. Returns (new_cfg, None), or
    (None, error) leaving the caller's cfg untouched."""
    new = {**cfg, **{k: evt[k] for k in ("sr", "channels", "mode", "model", "paste") if k in evt}}

    def whole(v) -> bool:
        return isinstance(v, int) and not isinstance(v, bool)
    if not whole(new["channels"]) or new["channels"] not in (1, 2):
        return None, "channels must be 1 (mono) or 2 (throat on ch0, air on ch1)"
    if not whole(new["sr"]) or not 8000 <= new["sr"] <= 192000:
        return None, "sr must be a sample rate in Hz between 8000 and 192000"
    if new["mode"] not in fusion.MODES:
        return None, f"unknown mode {new['mode']!r} (one of: {', '.join(fusion.MODES)})"
    if new["model"] not in stt.MODELS:
        return None, f"unknown model {new['model']!r}"
    new["paste"] = bool(new["paste"])
    return new, None


@app.websocket("/api/audio-stream")
async def audio_stream(ws: WebSocket) -> None:
    """Network PCM input (BYT-118 capture node) — a client streams interleaved
    little-endian int16 PCM, mono or stereo (throat=ch0, air=ch1), bracketed by
    JSON control frames; the host fuses + transcribes. See docs/capture-node.md.

        →  {"event":"start","sr":16000,"channels":2,"mode":"fusion","model":"...","paste":false}
        →  <binary PCM frames…>
        →  {"event":"stop"}
        ←  {"ok":true,"transcript":"…","recording_id":N,"dual_mic":true}

    Multiple utterances may be sent on one connection; "cancel" drops the buffer.
    A start frame with bad values is refused with an error and changes nothing.
    Only peers on this machine may connect (see _loopback_peer), and of those,
    browser pages other than this server's own are refused (see _trusted_origin).
    LocalOnly already enforces both for every route; the endpoint checks again
    because it is the one that can paste at your cursor.
    """
    if not (_loopback_peer(ws.client)
            and _trusted_origin(ws.headers.get("origin"), ws.headers.get("host"))):
        await ws.close(code=1008)           # policy violation, before any audio flows
        return
    await ws.accept()
    cfg = {"sr": TARGET_SR, "channels": 2, "mode": "fusion",
           "model": DEFAULT_MODEL, "paste": False}
    buf = bytearray()
    overflow = False                         # this utterance passed MAX_STREAM_BYTES
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                if overflow:
                    continue
                if len(buf) + len(msg["bytes"]) > MAX_STREAM_BYTES:
                    buf.clear()
                    overflow = True
                    await ws.send_json({"error": "too much audio in one utterance; "
                                        f"the limit is {MAX_STREAM_BYTES} bytes"})
                    continue
                buf.extend(msg["bytes"])
                continue
            if msg.get("text") is None:
                continue
            try:
                evt = json.loads(msg["text"])
            except ValueError:
                evt = None
            if not isinstance(evt, dict):
                await ws.send_json({"error": "control frames must be JSON objects"})
                continue
            event = evt.get("event")
            if event == "start":
                new, err = _stream_start(cfg, evt)
                if err:
                    await ws.send_json({"error": err})
                else:
                    cfg = new
                    buf.clear()
                    overflow = False
            elif event == "cancel":
                buf.clear()
                overflow = False
            elif event == "stop":
                data = bytes(buf)
                buf.clear()
                overflow = False
                if not data:
                    await ws.send_json({"error": "no audio received"})
                else:
                    await ws.send_json(await asyncio.to_thread(_transcribe_pcm, data, dict(cfg)))
            elif event == "close":
                break
    except WebSocketDisconnect:
        return


class DictationReq(BaseModel):
    enabled: bool | None = None            # None = leave enabled unchanged (config-only update)
    hotkey: str | None = None
    insert_mode: str | None = None
    model: str | None = None
    context: str | None = None
    fixup: bool | None = None
    device_index: int | None = None
    channels: int | None = None            # 2 = dual-mic (throat + air)
    fusion_mode: FusionMode | None = None  # fusion | throat | air


def _dictation_status() -> dict:
    # The full live config — the SINGLE source of truth both UIs (menubar + web)
    # read back, so a change in one is reflected in the other.
    return {"enabled": _dict["enabled"], "hotkey": _dict["hotkey"],
            "insert_mode": _dict["insert_mode"], "model": _dict["model"],
            "device": _dict["device"], "channels": _dict["channels"],
            "fusion_mode": _dict["fusion_mode"], "fixup": _dict["fixup"],
            "context": _dict["context"], "configured": _dict["configured"],
            "recording": _dict["recording"], "status": _dict["status"],
            "last": _dict["last"]}


def _apply_live(changed: set[str]) -> None:
    """Apply config changes to the running listener without restarting it. The
    listener reads model/context/fixup/insert_mode/fusion_mode live at each press;
    only the parsed chord and the warm stream need touching. A device/channel
    re-warm is skipped mid-recording (re-opening the stream would drop the held
    utterance) — the next press warms the new device anyway."""
    if "hotkey" in changed:
        _dict["chord"] = _parse_chord(_dict["hotkey"])
    if changed & {"device", "channels"} and not _dict["recording"]:
        ensure_warm(_dict["device"], _dict["channels"])


# One /api/dictation change at a time. A config-only request that restarts a
# running listener can be slow (a Bluetooth mic re-opening); without this, a
# disable arriving meanwhile finished first and was then undone.
_dict_lock = threading.Lock()


@app.post("/api/dictation")
def dictation(req: DictationReq) -> dict:
    with _dict_lock:
        # Validate first so a rejected request changes nothing.
        if req.model is not None:
            if req.model not in stt.MODELS:
                return JSONResponse({"error": f"unknown model {req.model}"}, status_code=400)
            if not stt.model_available(req.model):
                return JSONResponse(
                    {"error": f"model {req.model} is not available on this host"},
                    status_code=409)
        changes = {}
        if req.hotkey:                      # empty strings are ignored, not written
            changes["hotkey"] = req.hotkey
        if req.insert_mode:
            changes["insert_mode"] = req.insert_mode
        if req.model is not None:
            changes["model"] = req.model
        if req.context is not None:
            changes["context"] = req.context
        if req.fixup is not None:
            changes["fixup"] = req.fixup
        if req.device_index is not None:
            changes["device"] = req.device_index
        if req.channels is not None:
            changes["channels"] = 2 if req.channels >= 2 else 1
        if req.fusion_mode is not None:
            changes["fusion_mode"] = req.fusion_mode
        changed = {k for k, v in changes.items() if _dict[k] != v}
        if changes:
            _dict.update(changes)
            _dict["configured"] = True     # a real config write → later-loading UIs adopt it
        if changes.get("fixup"):
            warm_fixup()          # background-load so the first correction is fast
        # enabled=None → config-only update: keep the current enabled state. A running
        # listener is updated in place, never restarted — a restart clears an
        # in-progress recording and the held utterance is lost.
        want = _dict["enabled"] if req.enabled is None else req.enabled
        try:
            if want and _dict["listener"] is not None:
                _apply_live(changed)
                _dict["enabled"] = True
            elif want:
                start_dictation()
                _dict["enabled"] = True
            else:
                stop_dictation()
                _dict["enabled"] = False
        except Exception as e:
            _dict["enabled"] = False
            _dict["status"] = "off"
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
        return _dictation_status()


@app.get("/api/dictation/status")
def dictation_status() -> dict:
    return _dictation_status()


@app.post("/api/dictation/test")
def dictation_test() -> dict:
    threading.Thread(target=lambda: (time.sleep(2.5), insert_text("STT dictation test ✓ ")),
                     daemon=True).start()
    return {"ok": True,
            "note": "Focus VSCode within ~2.5s — 'STT dictation test ✓' should type at your cursor. "
                    "If nothing appears, grant Accessibility + Input Monitoring to your terminal app."}


def _check_perms() -> dict:
    # The OS-specific grant logic lives behind the platform layer now: macOS
    # reports the Input Monitoring + Accessibility grants; Windows/Linux report
    # "not applicable" (global hooks need no such grant). Keeps input_monitoring
    # + accessibility as top-level keys so the menubar keeps working.
    return osplatform.check_perms()


@app.get("/api/perms")
def perms() -> dict:
    return _check_perms()


@app.post("/api/perms/request")
def perms_request() -> dict:
    """Trigger any OS permission prompts so this app is added to the lists
    (macOS only; a no-op that reports 'not applicable' on Windows/Linux)."""
    return osplatform.request_perms()


# Background watcher: auto-releases the warm mic after idle (saves battery) and
# reaps a warm stream whose device vanished (throat mic disconnected) so it can
# be re-armed. Skipped when VOXWIRE_NO_IDLE_WATCH is set, so tests can import
# this module without the background thread racing their assertions.
if not os.environ.get("VOXWIRE_NO_IDLE_WATCH"):
    threading.Thread(target=_idle_watch, daemon=True).start()


if __name__ == "__main__":
    import uvicorn
    print("\n  Voxwire  →  http://127.0.0.1:8123\n")
    uvicorn.run(app, host="127.0.0.1", port=8123, log_level="warning")
