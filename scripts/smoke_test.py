#!/usr/bin/env python3
"""After ./scripts/install.sh: prove a fresh install works (BYT-121).

Starts the server and checks that it answers, then transcribes a sentence of
synthesized speech with this machine's default model, the one a new user gets.
The install-check workflow runs it on clean GitHub-hosted macOS and Linux
machines; run it yourself after installing somewhere new.

    voxwire/.venv/bin/python scripts/smoke_test.py

The speech comes from `say` on macOS and `espeak-ng` on Linux. The first run
downloads the default model. The server gets a spare port, so a Voxwire that's
already running on 8123 is left alone.
"""
from __future__ import annotations

import json
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "voxwire"
SENTENCE = "the quick brown fox jumps over the lazy dog"


def fail(msg: str) -> None:
    raise SystemExit(f"✗ {msg}")


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:
        body = r.read().decode()
    return json.loads(body) if r.headers.get_content_type() == "application/json" else body


def check_server() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    code = ("import uvicorn, server; "
            f"uvicorn.run(server.app, host='127.0.0.1', port={port}, log_level='warning')")
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=APP)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):                  # a cold first import can take a while
            if proc.poll() is not None:
                fail(f"the server exited with code {proc.returncode} before it answered")
            try:
                status = _get(base + "/api/dictation/status")
                break
            except OSError:
                time.sleep(0.5)
        else:
            fail("the server didn't answer within 60 s")
        if "enabled" not in status:
            fail(f"/api/dictation/status answered {status!r}")
        if "Voxwire" not in _get(base + "/"):
            fail("the web UI didn't load")
        names = [i["name"] for i in _get(base + "/api/integrations")["integrations"]]
        if "echo" not in names:
            fail(f"the built-in echo plugin didn't load (got {names})")
        print(f"✓ the server answers: web UI, dictation status, plugins ({', '.join(names)})")
    finally:
        proc.terminate()
        proc.wait(timeout=15)


def synthesize(out: Path) -> None:
    """Speak SENTENCE into the WAV file `out`."""
    if sys.platform == "darwin":
        cmd = ["say", "-o", str(out), "--data-format=LEI16@16000", SENTENCE]
    elif shutil.which("espeak-ng"):
        cmd = ["espeak-ng", "-w", str(out), SENTENCE]
    else:
        fail("no speech synthesizer here; install espeak-ng")
    subprocess.run(cmd, check=True)


def check_stt() -> None:
    sys.path.insert(0, str(APP))
    import fusion
    import soundfile as sf
    import stt

    model = stt.default_model()
    if model is None:
        fail(f"no speech-to-text model can run here (backends: {stt.available_backends() or 'none'})")
    with tempfile.TemporaryDirectory() as tmp:
        spoken, clip = Path(tmp) / "spoken.wav", Path(tmp) / "clip.wav"
        synthesize(spoken)
        audio, sr = sf.read(spoken, dtype="float32", always_2d=True)
        sf.write(clip, fusion.resample(audio[:, 0], sr), fusion.TARGET_SR)    # what stt expects
        tr = stt.transcribe(clip, model)
    heard = set(tr.text.lower().replace(".", " ").replace(",", " ").split())
    hits = sum(w in heard for w in SENTENCE.split())
    if hits < 0.7 * len(SENTENCE.split()):
        fail(f"{model} heard {tr.text.strip()!r}, expected about {SENTENCE!r}")
    print(f"✓ speech-to-text with {model}: heard {tr.text.strip()!r} "
          f"(model load {tr.load_s:.1f} s, transcription {tr.infer_s:.1f} s)")


def main() -> None:
    print(f"Voxwire smoke test · {platform.system()} {platform.machine()} · "
          f"Python {platform.python_version()}")
    check_server()
    check_stt()
    print("✓ this install works")


if __name__ == "__main__":
    main()
