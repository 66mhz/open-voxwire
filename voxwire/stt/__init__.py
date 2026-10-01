"""Voxwire STT — pluggable speech-to-text behind one interface.

The rest of the app calls `transcribe(wav, model_key)`, `available_models()`,
and `default_model()`, and never imports a platform STT runtime. Which engine
runs a model is decided by the model's `backend` (see `models.py`, the ONE
model table) and by which backends report `is_available()` here — MLX on Apple
Silicon, faster-whisper on Windows/Linux/Intel Macs.

    import stt
    tr = stt.transcribe(wav_path, stt.default_model())
    print(tr.text)
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from .base import (ModelSpec, SttBackend, Transcript, get_backend, register,
                   registered, unregister)
from .models import DEFAULT_PREFERENCE, MODELS


def _discover_backends() -> None:
    """Import every backend module in this package so its `register(...)` runs.
    Mirrors `gateway.load_integrations`: drop a `*_backend.py` in here and it is
    live — no core edit needed. Idempotent (imports cache; `register` dedups)."""
    pkg_dir = Path(__file__).resolve().parent
    for info in pkgutil.iter_modules([str(pkg_dir)]):
        name = info.name
        if name in ("base", "models") or name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{name}")


# Register the built-ins (mlx_backend, faster_whisper_backend) and any
# third-party `*_backend.py` dropped beside them. Each keeps its heavy runtime
# import lazy, so discovery is safe on any OS.
_discover_backends()

__all__ = [
    "MODELS", "ModelSpec", "Transcript", "SttBackend",
    "register", "unregister", "registered", "get_backend",
    "available_backends", "available_models", "backend_for",
    "model_available", "default_model", "transcribe",
]


def _safe_available(backend: SttBackend) -> bool:
    """`is_available()` must not raise, but a third-party backend might; treat any
    exception as 'not available' so one bad backend can't break selection."""
    try:
        return backend.is_available()
    except Exception:
        return False


def _safe_supports(backend: SttBackend, spec: ModelSpec) -> bool:
    """`supports()` should not raise either; treat any error as unsupported so a
    flaky backend never advertises a model that fails at transcribe time."""
    try:
        return backend.supports(spec)
    except Exception:
        return False


def available_backends() -> list[str]:
    """Names of backends that can run on this machine right now."""
    return [b.name for b in registered() if _safe_available(b)]


def backend_for(model_key: str) -> SttBackend | None:
    """The backend that runs `model_key` (whether or not it's available), or None
    if the model or its backend is unknown."""
    spec = MODELS.get(model_key)
    return get_backend(spec.backend) if spec else None


def model_available(model_key: str) -> bool:
    """True iff `model_key` exists and its backend can run it here."""
    spec = MODELS.get(model_key)
    b = backend_for(model_key)
    return bool(spec and b and _safe_available(b) and _safe_supports(b, spec))


def available_models() -> dict[str, ModelSpec]:
    """The subset of `MODELS` whose backend is available AND can run it here."""
    out: dict[str, ModelSpec] = {}
    for key, spec in MODELS.items():
        b = get_backend(spec.backend)
        if b and _safe_available(b) and _safe_supports(b, spec):
            out[key] = spec
    return out


def _pick_default(available: list[str], preference: tuple[str, ...]) -> str | None:
    """First preferred key that's available, else any available key, else None.
    Pure + tiny so the selection rule is unit-testable without a real backend."""
    for key in preference:
        if key in available:
            return key
    return available[0] if available else None


def default_model() -> str | None:
    """The model to use out of the box on this machine (None if none can run)."""
    return _pick_default(list(available_models()), DEFAULT_PREFERENCE)


def transcribe(wav_path, model_key: str) -> Transcript:
    """Transcribe a 16 kHz mono wav with `model_key`, routing to its backend.

    Raises KeyError for an unknown model and RuntimeError if the model's backend
    can't run here — with a message naming what *is* available.
    """
    spec = MODELS.get(model_key)
    if spec is None:
        raise KeyError(f"unknown model {model_key!r}")
    backend = get_backend(spec.backend)
    if backend is None:
        raise RuntimeError(f"no backend {spec.backend!r} for model {model_key!r}")
    if not _safe_available(backend):
        raise RuntimeError(
            f"backend {spec.backend!r} (model {model_key!r}) is not available on "
            f"this machine; available backends: {available_backends() or 'none'}")
    if not _safe_supports(backend, spec):
        raise RuntimeError(
            f"model {model_key!r} needs the {spec.engine!r} runtime for backend "
            f"{spec.backend!r}, which isn't installed here")
    return backend.transcribe(Path(wav_path), spec)
