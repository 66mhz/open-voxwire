"""The ONE LLM model tier table (CLAUDE.md rule 5: model IDs live in one table and
nowhere else).

Code asks for a tier, never for a model. Speech-to-text models have their own
registry in stt/models.py, next to the backends that run them.

  fast   small on-device models for latency-critical work (the transcript fixup)
  heavy  larger local models for agent reasoning (not wired up yet)
  cloud  hosted models behind an OpenAI-compatible endpoint; the model name comes
         from STT_LLM_MODEL or from the endpoint itself, so none is listed here
"""
from __future__ import annotations

from typing import NamedTuple


class Tier(NamedTuple):
    runtime: str                  # what runs it: "mlx" (on-device) | "openai-compatible"
    models: tuple[str, ...]       # preference order, best first


TIERS: dict[str, Tier] = {
    "fast": Tier("mlx", (
        "mlx-community/Qwen2.5-3B-Instruct-4bit",     # best quality/speed on M-series
        "mlx-community/Qwen2.5-1.5B-Instruct-4bit",   # lighter fallback
        "mlx-community/Llama-3.2-3B-Instruct-4bit",
    )),
    "heavy": Tier("mlx", ()),
    "cloud": Tier("openai-compatible", ()),
}


def models(tier: str) -> tuple[str, ...]:
    """The tier's models, best first."""
    return TIERS[tier].models
