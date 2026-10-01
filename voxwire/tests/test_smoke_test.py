"""Tests for scripts/smoke_test.py, the install check's last step.

Running it needs a speech synthesizer and a model download; the install-check
workflow does that on fresh machines. This covers the rule that keeps its macOS
job honest: on Apple Silicon the default model must run on MLX, because
install.sh also installs faster-whisper there, and a broken MLX stack would
otherwise fall back to it and pass.
"""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "smoke_test.py"


@pytest.fixture(scope="module")
def smoke():
    spec = importlib.util.spec_from_file_location("smoke_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_apple_silicon_falling_back_to_faster_whisper_fails(smoke):
    with pytest.raises(SystemExit, match="should run on MLX"):
        smoke.require_platform_backend("fw-base", "Darwin", "arm64")


@pytest.mark.parametrize("model,system,machine", [
    ("parakeet-v2", "Darwin", "arm64"),     # the Mac stack
    ("fw-base", "Linux", "x86_64"),         # faster-whisper is the right answer here
    ("fw-base", "Darwin", "x86_64"),        # Intel Macs have no MLX
])
def test_the_right_backend_for_the_platform_passes(smoke, model, system, machine):
    smoke.require_platform_backend(model, system, machine)
