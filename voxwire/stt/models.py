"""The ONE STT model registry (CLAUDE.md rule 5: model IDs live in one table and
nowhere else).

Every model names the backend that runs it. Add a model → add a row here. Add a
runtime → add an `SttBackend`. Never name a model or a repo anywhere else.
"""
from __future__ import annotations

from .base import ModelSpec

MODELS: dict[str, ModelSpec] = {
    # ── Apple Silicon (MLX) — the fast local path on a Mac ──────────────────
    "parakeet-v2": ModelSpec(
        "parakeet-v2", "mlx", "parakeet",
        "mlx-community/parakeet-tdt-0.6b-v2",
        "Parakeet TDT 0.6B v2 (MLX) — English, fastest", 2.3),
    "parakeet-v3": ModelSpec(
        "parakeet-v3", "mlx", "parakeet",
        "mlx-community/parakeet-tdt-0.6b-v3",
        "Parakeet TDT 0.6B v3 (MLX) — multilingual", 2.4),
    "whisper-turbo": ModelSpec(
        "whisper-turbo", "mlx", "whisper",
        "mlx-community/whisper-large-v3-turbo",
        "Whisper large-v3-turbo (MLX) — robust to degraded audio", 1.6),
    "whisper-large-v3": ModelSpec(
        "whisper-large-v3", "mlx", "whisper",
        "mlx-community/whisper-large-v3-mlx",
        "Whisper large-v3 (MLX) — most accurate, slower", 3.1),
    "whisper-base": ModelSpec(
        "whisper-base", "mlx", "whisper",
        "mlx-community/whisper-base-mlx",
        "Whisper base (MLX) — tiny, lowest latency", 0.15),

    # ── Cross-platform (faster-whisper / CTranslate2) — CPU or CUDA ─────────
    # Windows, Linux, and Intel Macs. Repos are the canonical Systran CT2
    # conversions; faster-whisper caches them under the same HF hub path.
    "fw-base": ModelSpec(
        "fw-base", "faster-whisper", "whisper",
        "Systran/faster-whisper-base",
        "Whisper base (faster-whisper) — tiny, CPU-friendly", 0.15),
    "fw-small": ModelSpec(
        "fw-small", "faster-whisper", "whisper",
        "Systran/faster-whisper-small",
        "Whisper small (faster-whisper) — CPU, better accuracy", 0.5),
    "fw-large-v3": ModelSpec(
        "fw-large-v3", "faster-whisper", "whisper",
        "Systran/faster-whisper-large-v3",
        "Whisper large-v3 (faster-whisper) — most accurate", 3.1),
}

# Preferred default model, best first. `default_model()` returns the first whose
# backend is available here — so a Mac lands on Parakeet, everyone else on the
# tiny faster-whisper base (fast first run; larger models are one click away).
DEFAULT_PREFERENCE: tuple[str, ...] = ("parakeet-v2", "fw-base", "fw-large-v3")
