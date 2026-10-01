"""MLX STT backend — Apple Silicon only (mlx-whisper + parakeet-mlx).

The fast local path on a Mac. Every heavy import stays inside the methods, so
importing this module on Windows/Linux is harmless — `is_available()` simply
returns False there.
"""
from __future__ import annotations

import platform
import sys
import threading
import time
from importlib.util import find_spec
from pathlib import Path

from .base import ModelSpec, SttBackend, Transcript, register


class MlxBackend(SttBackend):
    name = "mlx"

    def __init__(self) -> None:
        self._parakeet_cache: dict[str, object] = {}
        self._load_lock = threading.Lock()

    def is_available(self) -> bool:
        # MLX is Apple-Silicon only; check platform BEFORE importing so a
        # non-Apple machine never pays the import.
        if sys.platform != "darwin" or platform.machine() != "arm64":
            return False
        try:
            import mlx.core  # noqa: F401
            return True
        except Exception:
            return False

    def supports(self, spec: ModelSpec) -> bool:
        # is_available() only proves mlx.core is present; each engine needs its
        # own package. Check that WITHOUT importing it (find_spec is cheap and
        # side-effect free), so a box with mlx.core but no parakeet_mlx does not
        # advertise Parakeet models that then fail at transcribe time.
        module = "mlx_whisper" if spec.engine == "whisper" else "parakeet_mlx"
        try:
            return find_spec(module) is not None
        except Exception:
            return False

    def transcribe(self, wav_path: Path, spec: ModelSpec) -> Transcript:
        if spec.engine == "whisper":
            import mlx_whisper
            import soundfile as sf
            clip, _ = sf.read(str(wav_path), dtype="float32")
            t0 = time.time()
            result = mlx_whisper.transcribe(clip, path_or_hf_repo=spec.repo, fp16=True)
            infer_s = time.time() - t0
            text = (result.get("text") if isinstance(result, dict) else str(result)) or ""
            return Transcript(text.strip(), 0.0, infer_s)

        # parakeet — cache the loaded model per repo (loading is the slow part).
        # Double-checked lock so two concurrent first-time transcribes (an API
        # request + the dictation worker) don't each load a multi-GB model.
        from parakeet_mlx import from_pretrained
        load_s = 0.0
        if spec.repo not in self._parakeet_cache:
            with self._load_lock:
                if spec.repo not in self._parakeet_cache:
                    t0 = time.time()
                    self._parakeet_cache[spec.repo] = from_pretrained(spec.repo)
                    load_s = time.time() - t0
        t0 = time.time()
        result = self._parakeet_cache[spec.repo].transcribe(str(wav_path))
        infer_s = time.time() - t0
        # NB: an empty AlignedResult has text == "" (falsy), so test for the
        # attribute — a plain `getattr(...) or ...` would fall through to the
        # object's repr on silence.
        if hasattr(result, "text"):
            text = result.text or ""
        elif isinstance(result, dict):
            text = result.get("text", "")
        else:
            text = str(result)
        return Transcript(text.strip(), load_s, infer_s)


register(MlxBackend())
