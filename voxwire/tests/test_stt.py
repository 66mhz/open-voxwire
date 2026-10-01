"""STT backend-abstraction tests.

Covers the point of the abstraction: the model table is coherent, backend
selection honours per-machine availability, and `transcribe` routes to the right
backend — all WITHOUT importing MLX or faster-whisper (stub backends stand in),
so these run on any OS in CI.
"""
import pytest

import stt
from stt.base import ModelSpec, SttBackend, Transcript


class _StubBackend(SttBackend):
    """A backend with no runtime — records calls, reports a chosen availability."""

    def __init__(self, name: str, available: bool, out: str = "stub text",
                 supports: bool = True):
        self.name = name
        self._available = available
        self._supports = supports
        self._out = out
        self.calls: list = []

    def is_available(self) -> bool:
        return self._available

    def supports(self, spec) -> bool:
        return self._supports

    def transcribe(self, wav_path, spec) -> Transcript:
        self.calls.append((wav_path, spec.key))
        return Transcript(self._out, load_s=0.1, infer_s=0.2)


@pytest.fixture
def clean_stt():
    """Snapshot the model table + backend registry so a test can add throwaway
    stubs/models without leaking into the next test."""
    models = dict(stt.MODELS)
    backends = stt.registered()
    yield
    stt.MODELS.clear()
    stt.MODELS.update(models)
    for b in stt.registered():
        stt.unregister(b.name)
    for b in backends:
        stt.register(b)


# ── the ONE model table is coherent (rule 5) ────────────────────────────────
def test_builtin_backends_self_register():
    names = {b.name for b in stt.registered()}
    assert {"mlx", "faster-whisper"} <= names


def test_every_model_names_a_registered_backend():
    for key, spec in stt.MODELS.items():
        assert spec.key == key, f"{key!r} keyed under the wrong name"
        assert stt.get_backend(spec.backend) is not None, \
            f"model {key!r} names unknown backend {spec.backend!r}"


def test_model_engines_are_known():
    for spec in stt.MODELS.values():
        assert spec.engine in {"whisper", "parakeet"}


def test_both_platforms_have_a_model():
    backends = {s.backend for s in stt.MODELS.values()}
    assert "mlx" in backends            # Apple Silicon
    assert "faster-whisper" in backends  # Windows / Linux / Intel Mac


# ── default selection is a pure, testable rule ──────────────────────────────
def test_pick_default_prefers_first_available():
    assert stt._pick_default(["a", "b"], ("x", "b", "a")) == "b"


def test_pick_default_falls_back_to_any_available():
    assert stt._pick_default(["a", "b"], ("x", "y")) == "a"


def test_pick_default_none_when_nothing_available():
    assert stt._pick_default([], ("x",)) is None


def test_default_model_is_runnable_here_or_none():
    dm = stt.default_model()
    if dm is None:
        assert stt.available_models() == {}
    else:
        assert dm in stt.available_models()
        assert stt.model_available(dm)


# ── availability filtering routes by backend ────────────────────────────────
def test_available_models_filters_by_backend_availability(clean_stt):
    stt.register(_StubBackend("stub-on", True))
    stt.register(_StubBackend("stub-off", False))
    stt.MODELS["m-on"] = ModelSpec("m-on", "stub-on", "whisper", "r/on", "on", 0.1)
    stt.MODELS["m-off"] = ModelSpec("m-off", "stub-off", "whisper", "r/off", "off", 0.1)

    avail = stt.available_models()
    assert "m-on" in avail and "m-off" not in avail
    assert stt.model_available("m-on") is True
    assert stt.model_available("m-off") is False
    assert "stub-on" in stt.available_backends()
    assert "stub-off" not in stt.available_backends()


def test_safe_available_swallows_backend_errors(clean_stt):
    class Boom(SttBackend):
        name = "boom"

        def is_available(self):
            raise RuntimeError("nope")

        def transcribe(self, wav_path, spec):
            raise RuntimeError("nope")

    stt.register(Boom())
    # a misbehaving backend must not break selection for the others
    assert "boom" not in stt.available_backends()


# ── transcribe() routing + error contract ───────────────────────────────────
def test_transcribe_routes_to_the_models_backend(clean_stt, tmp_path):
    stub = _StubBackend("stub-on", True, out="hello world")
    stt.register(stub)
    stt.MODELS["m-on"] = ModelSpec("m-on", "stub-on", "whisper", "r/on", "on", 0.1)

    wav = tmp_path / "clip.wav"
    tr = stt.transcribe(wav, "m-on")
    assert tr.text == "hello world"
    assert tr.infer_s == 0.2
    assert stub.calls == [(wav, "m-on")]


def test_transcribe_unknown_model_raises_keyerror():
    with pytest.raises(KeyError):
        stt.transcribe("clip.wav", "no-such-model")


def test_transcribe_unavailable_backend_raises_runtimeerror(clean_stt):
    stt.register(_StubBackend("stub-off", False))
    stt.MODELS["m-off"] = ModelSpec("m-off", "stub-off", "whisper", "r/off", "off", 0.1)
    with pytest.raises(RuntimeError):
        stt.transcribe("clip.wav", "m-off")


# ── the MLX backend refuses to claim availability off Apple Silicon ─────────
def test_mlx_backend_unavailable_off_apple(monkeypatch):
    from stt.mlx_backend import MlxBackend
    monkeypatch.setattr("stt.mlx_backend.sys.platform", "win32")
    assert MlxBackend().is_available() is False


def test_mlx_backend_unavailable_on_non_arm_mac(monkeypatch):
    from stt.mlx_backend import MlxBackend
    monkeypatch.setattr("stt.mlx_backend.sys.platform", "darwin")
    monkeypatch.setattr("stt.mlx_backend.platform.machine", lambda: "x86_64")
    assert MlxBackend().is_available() is False


def test_faster_whisper_availability_is_boolean_and_never_raises():
    from stt.faster_whisper_backend import FasterWhisperBackend
    assert isinstance(FasterWhisperBackend().is_available(), bool)


# ── per-model runnability: an available backend can still not run a model ────
def test_available_models_respects_supports(clean_stt):
    stt.register(_StubBackend("stub-sup", True, supports=True))
    stt.register(_StubBackend("stub-nosup", True, supports=False))
    stt.MODELS["m-sup"] = ModelSpec("m-sup", "stub-sup", "whisper", "r", "s", 0.1)
    stt.MODELS["m-nosup"] = ModelSpec("m-nosup", "stub-nosup", "whisper", "r", "n", 0.1)

    avail = stt.available_models()
    assert "m-sup" in avail
    assert "m-nosup" not in avail   # backend available, but can't run THIS model
    assert stt.model_available("m-sup") is True
    assert stt.model_available("m-nosup") is False


def test_transcribe_raises_when_engine_unsupported(clean_stt):
    stt.register(_StubBackend("stub-nosup", True, supports=False))
    stt.MODELS["m-nosup"] = ModelSpec("m-nosup", "stub-nosup", "whisper", "r", "n", 0.1)
    with pytest.raises(RuntimeError):
        stt.transcribe("clip.wav", "m-nosup")


def test_mlx_supports_checks_the_engine_module(monkeypatch):
    # A Mac with mlx.core but only one engine installed must not advertise the
    # other. supports() uses find_spec (no import); fake it per module.
    from stt.mlx_backend import MlxBackend
    whisper = ModelSpec("w", "mlx", "whisper", "r", "l", 1.0)
    parakeet = ModelSpec("p", "mlx", "parakeet", "r", "l", 1.0)
    monkeypatch.setattr("stt.mlx_backend.find_spec",
                        lambda m: object() if m == "mlx_whisper" else None)
    b = MlxBackend()
    assert b.supports(whisper) is True
    assert b.supports(parakeet) is False


# ── backends are discovered, not hand-imported (matches integrations) ───────
def test_backend_discovery_registers_builtins():
    stt._discover_backends()   # idempotent
    names = {b.name for b in stt.registered()}
    assert {"mlx", "faster-whisper"} <= names


# ── faster-whisper gets samples, never a file to decode ─────────────────────
def test_faster_whisper_is_handed_16k_mono_samples(tmp_path):
    """Given a path, faster-whisper decodes through PyAV, and PyAV 19 broke that
    (an argument faster-whisper 1.2.1 still passes is gone). The backend reads the
    WAV itself and passes float32 samples at 16 kHz, whatever the file holds."""
    import numpy as np
    import soundfile as sf
    from stt.faster_whisper_backend import FasterWhisperBackend

    seen = []

    class _Model:
        def transcribe(self, audio, beam_size):
            seen.append(audio)
            return iter([type("Seg", (), {"text": " heard"})()]), None

    backend = FasterWhisperBackend()
    spec = stt.MODELS["fw-base"]
    backend._cache[spec.repo] = _Model()
    mono16 = tmp_path / "mono16.wav"
    sf.write(mono16, np.zeros(16000, np.float32), 16000)
    stereo48 = tmp_path / "stereo48.wav"
    sf.write(stereo48, np.zeros((48000, 2), np.float32), 48000)

    for wav in (mono16, stereo48):
        assert backend.transcribe(wav, spec).text == "heard"
    for audio in seen:
        assert isinstance(audio, np.ndarray) and audio.dtype == np.float32
        assert audio.ndim == 1 and audio.shape[0] == 16000    # one second at 16 kHz
