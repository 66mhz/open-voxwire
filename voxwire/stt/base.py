"""Voxwire STT — the pluggable speech-to-text layer.

A *backend* is one speech-to-text runtime (Apple's MLX, CTranslate2's
faster-whisper, …). Voxwire calls one interface — `transcribe(wav, model)` — and
the backend that runs a given model is chosen from the model's `backend` field
and from which backends report `is_available()` on this machine. That is what
lets the same code transcribe on macOS/Apple Silicon (MLX) and on Windows/Linux
(faster-whisper) without the core ever importing a platform-specific stack.

This mirrors `integrations/base.py`: a tiny, dependency-free interface plus a
registry. The flexibility lives here; nothing above names an engine.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ModelSpec:
    """One row of the model registry (see `models.py` — the ONE table)."""
    key: str            # short id used across the API + UI, e.g. "parakeet-v2"
    backend: str        # which backend runs it, e.g. "mlx" / "faster-whisper"
    engine: str         # decoder family: "whisper" | "parakeet"
    repo: str           # Hugging Face repo the weights come from
    label: str          # human label for the UI
    approx_gb: float    # download size, for the UI


@dataclass
class Transcript:
    """What a backend returns for one clip."""
    text: str
    load_s: float = 0.0     # seconds spent loading the model (0 when already warm)
    infer_s: float = 0.0    # seconds of inference


class SttBackend(ABC):
    """A speech-to-text runtime.

    Implement `is_available` + `transcribe`. Keep every heavy import LAZY (inside
    those methods) so importing a backend module never drags a platform-specific
    stack onto a machine that can't use it — a Windows box must be able to import
    the MLX backend module (and simply have it report unavailable).
    """

    #: short unique id, referenced by `ModelSpec.backend`
    name: str = "unnamed"

    @abstractmethod
    def is_available(self) -> bool:
        """True iff this backend can run on this machine right now (platform ok
        AND its runtime importable). Must not raise."""

    @abstractmethod
    def transcribe(self, wav_path: Path, spec: ModelSpec) -> Transcript:
        """Transcribe a 16 kHz mono wav with `spec`."""

    def supports(self, spec: ModelSpec) -> bool:
        """True iff this backend can run THIS model right now, beyond the coarse
        `is_available()` — e.g. the model's specific engine package is present.
        Defaults to yes; a backend serving several engines overrides it so a
        partial install can't advertise a model that fails at runtime. Must not
        raise."""
        return True


# ── backend registry (mirrors integrations.base) ────────────────────────────
_BACKENDS: list[SttBackend] = []


def register(backend: SttBackend) -> SttBackend:
    """Register a backend. Re-registering a name replaces it (no duplicates)."""
    _BACKENDS[:] = [b for b in _BACKENDS if b.name != backend.name]
    _BACKENDS.append(backend)
    return backend


def unregister(name: str) -> None:
    """Remove a registered backend by name (no-op if absent)."""
    _BACKENDS[:] = [b for b in _BACKENDS if b.name != name]


def registered() -> list[SttBackend]:
    """Every registered backend, in registration order."""
    return list(_BACKENDS)


def get_backend(name: str) -> SttBackend | None:
    """The registered backend with this name, or None."""
    return next((b for b in _BACKENDS if b.name == name), None)
