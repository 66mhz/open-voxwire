"""Fixup LLM tests: availability, model choice, the load lifecycle,
and the faithful-only guards (rule 4).

MLX never runs here. A fake `mlx_lm` module stands in for the on-device path and
a patched `httpx` for the remote one, so these run on any OS and never download
a model.
"""
import os
import sys
import threading
import time
import types

import pytest

os.environ["VOXWIRE_NO_IDLE_WATCH"] = "1"   # no background thread racing the tests
server = pytest.importorskip(
    "server", reason="server.py needs the audio stack (sounddevice/PortAudio)")
httpx = pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app, base_url="http://127.0.0.1:8123", client=("127.0.0.1", 50000))   # this machine (see LocalOnly)
MISHEARD = "lit thee files please"
FIXED = "list the files please"


def _wait(cond, timeout: float = 5.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached in time")


class _Tok:
    def apply_chat_template(self, messages, add_generation_prompt=True):
        return "\n".join(m["content"] for m in messages)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(server, "_llm_cache", {})
    monkeypatch.setattr(server, "_fixup", {"model": None, "state": "cold"})
    monkeypatch.setattr(server, "hf_cached", lambda repo: False)
    monkeypatch.delenv("STT_LLM_LOCAL", raising=False)


@pytest.fixture
def fake_mlx(monkeypatch):
    """On-device path with a fake mlx_lm whose load() parks on `gate`."""
    calls = {"load": 0, "reply": FIXED}
    gate = threading.Event()
    mod = types.ModuleType("mlx_lm")

    def load(repo):
        calls["load"] += 1
        gate.wait(5)
        return (f"model:{repo}", _Tok())

    def generate(model, tok, prompt, max_tokens, verbose):
        return calls["reply"]

    mod.load, mod.generate = load, generate
    monkeypatch.setitem(sys.modules, "mlx_lm", mod)
    monkeypatch.setattr(server, "_mlx_lm_available", lambda: True)
    monkeypatch.setattr(server, "LLM_BASE_URL", None)
    return calls, gate


@pytest.fixture
def no_backend(monkeypatch):
    """Neither an endpoint nor mlx_lm: importing mlx_lm must fail, as off Apple Silicon."""
    monkeypatch.setitem(sys.modules, "mlx_lm", None)
    monkeypatch.setattr(server, "_mlx_lm_available", lambda: False)
    monkeypatch.setattr(server, "LLM_BASE_URL", None)


@pytest.fixture
def remote(monkeypatch):
    """Remote path; records the chat request it would have sent."""
    sent = {}

    def post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, json=json)
        return _Resp({"choices": [{"message": {"content": FIXED}}]})

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setitem(sys.modules, "mlx_lm", None)
    monkeypatch.setattr(server, "LLM_BASE_URL", "http://llm.test/v1")
    return sent


# ── availability ───────────────────────────────────────────────────────────
def test_without_a_backend_correct_is_a_clear_409_not_a_500(no_backend):
    res = client.post("/api/correct", json={"text": MISHEARD})
    assert res.status_code == 409
    assert "STT_LLM_BASE_URL" in res.json()["error"]


def test_without_a_backend_the_status_says_what_to_do(no_backend):
    info = client.get("/api/fixup").json()
    assert info["state"] == "unavailable"
    assert info["hint"]


def test_on_apple_silicon_without_mlx_lm_the_hint_says_install_it(no_backend, monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server.platform, "machine", lambda: "arm64")
    assert "mlx-lm" in server.llm_info()["hint"]


def test_correct_command_signals_unavailable_so_callers_keep_the_raw_text(no_backend):
    # the dictation worker catches this and pastes the raw transcript
    with pytest.raises(server.FixupUnavailable):
        server.correct_command(MISHEARD, None)


# ── on-device lifecycle ────────────────────────────────────────────────────
def test_fixup_uses_the_fast_tier(fake_mlx):
    import tiers
    assert server.fixup_local_model() == tiers.models("fast")[0]


def test_readiness_goes_needs_download_then_loading_then_ready(fake_mlx):
    calls, gate = fake_mlx
    assert server.llm_info()["state"] == "needs-download"
    server.warm_fixup()
    _wait(lambda: server.llm_info()["state"] == "loading")
    gate.set()
    _wait(lambda: server.llm_info()["state"] == "ready")
    assert client.get("/api/fixup").json()["state"] == "ready"


def test_a_downloaded_model_is_not_ready_until_it_is_loaded(fake_mlx, monkeypatch):
    calls, gate = fake_mlx
    monkeypatch.setattr(server, "hf_cached", lambda repo: True)
    info = server.llm_info()
    assert info["state"] == "downloaded" and info["cached"]
    assert calls["load"] == 0
    gate.set()
    server.warm_fixup()
    _wait(lambda: server.llm_info()["state"] == "ready")
    assert calls["load"] == 1


def test_a_failed_load_reports_error(fake_mlx, monkeypatch):
    def broken(repo):
        raise OSError("disk full")
    monkeypatch.setattr(sys.modules["mlx_lm"], "load", broken)
    server.warm_fixup()
    _wait(lambda: server.llm_info()["state"] == "error")


def test_warm_up_and_first_request_share_one_model_load(fake_mlx):
    calls, gate = fake_mlx
    server.warm_fixup()                       # background load, parked on the gate
    _wait(lambda: calls["load"] == 1)
    out = {}
    worker = threading.Thread(
        target=lambda: out.update(result=server.correct_command(MISHEARD, None)))
    worker.start()
    time.sleep(0.2)                           # room for an unsynchronised second load
    gate.set()
    worker.join(5)
    assert calls["load"] == 1
    assert out["result"][0] == FIXED


# ── faithful-only guards (rule 4) ──────────────────────────────────────────
@pytest.mark.parametrize("text", ["continue", "lit files", "  run   tests  "])
def test_inputs_of_two_words_or_fewer_never_reach_the_llm(fake_mlx, text):
    calls, gate = fake_mlx
    gate.set()
    calls["reply"] = "okay start execution"   # what the prototype turned "continue" into
    assert server.correct_command(text, None) == (text.strip(), 0.0)
    assert calls["load"] == 0


def test_three_words_is_the_first_length_the_llm_sees(fake_mlx):
    calls, gate = fake_mlx
    gate.set()
    calls["reply"] = "list the files"
    assert server.correct_command("lit thee files", None)[0] == "list the files"
    assert calls["load"] == 1


def test_a_reply_that_expands_the_text_is_discarded(fake_mlx):
    calls, gate = fake_mlx
    gate.set()
    calls["reply"] = "okay start execution of the entire test suite right now please"
    assert server.correct_command("run the tests", None)[0] == "run the tests"


@pytest.mark.parametrize("reply", [
    "delete all files now",       # barely longer: slipped past a length-only guard
    "delete the tests",           # same length, one word swapped in
    "run the tests and push",     # modest expansion
])
def test_a_reply_that_substitutes_or_adds_words_is_discarded(fake_mlx, reply):
    calls, gate = fake_mlx
    gate.set()
    calls["reply"] = reply
    assert server.correct_command("run the tests", None)[0] == "run the tests"


def test_a_genuine_throat_mic_correction_is_kept(fake_mlx):
    calls, gate = fake_mlx
    gate.set()
    calls["reply"] = "run the tests again"
    assert server.correct_command("un thee et again", None)[0] == "run the tests again"


# ── remote endpoint ────────────────────────────────────────────────────────
def test_remote_sends_the_configured_model(remote, monkeypatch):
    monkeypatch.setattr(server, "LLM_MODEL", "llama3.2")
    assert server.correct_command(MISHEARD, None)[0] == FIXED
    assert remote["json"]["model"] == "llama3.2"


def test_remote_asks_the_endpoint_for_a_model_when_none_is_set(remote, monkeypatch):
    monkeypatch.setattr(server, "LLM_MODEL", None)
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _Resp({"data": [{"id": "qwen2.5:3b"}]}))
    server.correct_command(MISHEARD, None)
    assert remote["json"]["model"] == "qwen2.5:3b"


def test_remote_never_sends_a_made_up_model_name(remote, monkeypatch):
    monkeypatch.setattr(server, "LLM_MODEL", None)

    def down(url, **kw):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "get", down)
    server.correct_command(MISHEARD, None)
    assert "model" not in remote["json"]


def test_remote_skips_non_chat_models_the_endpoint_lists_first(remote, monkeypatch):
    monkeypatch.setattr(server, "LLM_MODEL", None)
    listed = [{"id": "text-embedding-3-small"}, {"id": "whisper-1"}, {"id": "BAAI/bge-m3"},
              {"id": "intfloat/multilingual-e5-large"}, {"id": "qwen2.5:3b"}]
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _Resp({"data": listed}))
    server.correct_command(MISHEARD, None)
    assert remote["json"]["model"] == "qwen2.5:3b"


def test_remote_with_only_non_chat_models_fails_clearly(remote, monkeypatch):
    monkeypatch.setattr(server, "LLM_MODEL", None)
    listed = [{"id": "nomic-embed-text"}, {"id": "tts-1"}]
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _Resp({"data": listed}))
    res = client.post("/api/correct", json={"text": MISHEARD})
    assert res.status_code == 409
    assert "STT_LLM_MODEL" in res.json()["error"]
    assert "url" not in remote                  # no chat request went to an embedding model
