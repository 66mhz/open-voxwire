"""faster-whisper STT backend — cross-platform (CTranslate2; CPU or CUDA).

The portable path: Windows, Linux, and Intel Macs. Uses CUDA when present, else
CPU with int8. Decodes audio via PyAV bundled with faster-whisper, so there is
no ffmpeg-binary requirement. Heavy imports stay lazy.

Tuning via env:
  VOXWIRE_FW_DEVICE   auto | cpu | cuda        (default: auto)
  VOXWIRE_FW_COMPUTE  int8 | int8_float16 | float16 | float32
                      (default: int8 on cpu/auto, float16 on cuda)
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .base import ModelSpec, SttBackend, Transcript, register


class FasterWhisperBackend(SttBackend):
    name = "faster-whisper"

    def __init__(self) -> None:
        self._cache: dict[str, object] = {}
        self._load_lock = threading.Lock()

    def is_available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except Exception:
            return False

    def _load(self, repo: str):
        from faster_whisper import WhisperModel
        device = os.environ.get("VOXWIRE_FW_DEVICE", "auto")
        default_compute = "float16" if device == "cuda" else "int8"
        compute = os.environ.get("VOXWIRE_FW_COMPUTE", default_compute)
        return WhisperModel(repo, device=device, compute_type=compute)

    def transcribe(self, wav_path: Path, spec: ModelSpec) -> Transcript:
        # Double-checked lock so overlapping first-time transcribes (FastAPI runs
        # sync endpoints in a threadpool; dictation adds its own thread) don't
        # each download/load the same multi-GB model.
        load_s = 0.0
        if spec.repo not in self._cache:
            with self._load_lock:
                if spec.repo not in self._cache:
                    t0 = time.time()
                    self._cache[spec.repo] = self._load(spec.repo)
                    load_s = time.time() - t0
        model = self._cache[spec.repo]
        t0 = time.time()
        # transcribe() returns a lazy generator — materialize it to run inference.
        segments, _info = model.transcribe(str(wav_path), beam_size=5)
        text = "".join(seg.text for seg in segments).strip()
        infer_s = time.time() - t0
        return Transcript(text, load_s, infer_s)


register(FasterWhisperBackend())
