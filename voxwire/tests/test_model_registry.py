"""Rule 5: model IDs live in the registries and nowhere else.

LLM model IDs belong in tiers.py (fast | heavy | cloud); speech-to-text model
IDs in stt/models.py. Everything else asks for a tier or an STT model key.
"""
import os
import re
from pathlib import Path

import stt
import tiers

APP = Path(__file__).resolve().parents[1]
REGISTRIES = {APP / "tiers.py", APP / "stt" / "models.py"}
SKIP_DIRS = {".venv", "__pycache__", "recordings", "tests", "node_modules"}
# A quoted Hugging Face-style repo id that names a model: org/…3B…, …Instruct…, …whisper…
HF_MODEL_ID = re.compile(
    r"""["'][\w.-]+/[\w.-]*?(?:\d+(?:\.\d+)?[bB]|[Ii]nstruct|whisper|parakeet)[\w.-]*["']""")


def _sources():
    for dirpath, dirnames, filenames in os.walk(APP):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix in {".py", ".html", ".sh"} and path not in REGISTRIES:
                yield path


def test_tier_table_names_the_three_tiers():
    assert set(tiers.TIERS) == {"fast", "heavy", "cloud"}
    assert tiers.models("fast"), "fixup needs at least one fast-tier model"


def test_no_registered_model_id_appears_outside_the_registries():
    ids = {m for tier in tiers.TIERS.values() for m in tier.models}
    ids |= {spec.repo for spec in stt.MODELS.values()}
    for path in _sources():
        text = path.read_text(errors="ignore")
        leaked = sorted(i for i in ids if i in text)
        assert not leaked, f"{path.relative_to(APP)} names {leaked}"


def test_no_unregistered_model_id_is_hardcoded():
    for path in _sources():
        found = [m.group(0) for m in HF_MODEL_ID.finditer(path.read_text(errors="ignore"))]
        assert not found, f"{path.relative_to(APP)} hardcodes {found}; add it to tiers.py"
