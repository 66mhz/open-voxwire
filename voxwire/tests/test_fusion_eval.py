"""Tests for scripts/fusion_eval.py, the fusion measurement.

Its numbers go in the README as the evidence for Voxwire's differentiator, so
the tests cover what would make them wrong: scoring (normalization, word error
rate, the paired bootstrap), which take counts after a redo, the phrase list's
rules, and above all that every mode is scored through the app's own capture
path. Recording needs a microphone and isn't tested here.
"""
import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

import fusion

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fusion_eval.py"
NUMBER_WORDS = {"zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
                "ten", "first", "second", "third", "fourth", "fifth", "sixth", "hundred"}


@pytest.fixture(scope="module")
def fe():
    spec = importlib.util.spec_from_file_location("fusion_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(texts, hyps_by_mode, condition="quiet"):
    return [{"condition": condition, "text": t, "hyp": {m: h[i] for m, h in hyps_by_mode.items()}}
            for i, t in enumerate(texts)]


# ── scoring ────────────────────────────────────────────────────────────────
def test_words_ignore_case_punctuation_and_hyphens(fe):
    assert fe.words("Stash the changes.") == ["stash", "the", "changes"]
    assert fe.words("Re-run the tests!") == ["re", "run", "the", "tests"]
    assert fe.words("It’s done") == fe.words("its done") == ["its", "done"]
    assert fe.words("  ") == []


@pytest.mark.parametrize("ref,hyp,distance", [
    ("stash the changes", "stash the changes", 0),
    ("stash the changes", "stash the chains", 1),        # substitution
    ("stash the changes", "stash changes", 1),           # deletion
    ("stash the changes", "stash all the changes", 1),   # insertion
    ("fix the test", "", 3),
    ("", "noise", 1),
])
def test_edit_distance(fe, ref, hyp, distance):
    assert fe.edit_distance(fe.words(ref), fe.words(hyp)) == distance


def test_summary_is_corpus_wer_with_exact_matches(fe):
    texts = ["stash the changes", "run the tests"]                      # six words
    s = fe.summarize(_rows(texts, {
        "throat": ["stash the chains", "run the"],                       # two errors
        "air": texts,                                                    # none
        "fusion": ["stash the changes", "run the test"],                 # one
    }), b=200)["quiet"]
    assert (s["n"], s["words"]) == (2, 6)
    assert s["modes"]["throat"]["wer"] == pytest.approx(2 / 6)
    assert (s["modes"]["air"]["wer"], s["modes"]["air"]["exact"]) == (0, 2)
    assert (s["modes"]["fusion"]["wer"], s["modes"]["fusion"]["exact"]) == (pytest.approx(1 / 6), 1)
    assert s["fusion_minus"]["throat"]["diff"] == pytest.approx(-1 / 6)
    assert s["fusion_minus"]["air"]["diff"] == pytest.approx(1 / 6)
    for m in fe.MODES:
        lo, hi = s["modes"][m]["ci"]
        assert lo <= s["modes"][m]["wer"] <= hi


def test_the_differences_are_paired(fe):
    """Identical transcripts in every mode differ by exactly zero on every
    resample. An unpaired bootstrap would report a spread here."""
    texts = ["stash the changes", "run the tests", "show the diff"]
    hyp = ["stash the chains", "run tests", "show the diff"]
    s = fe.summarize(_rows(texts, {m: hyp for m in fe.MODES}), b=200)["quiet"]
    assert s["fusion_minus"]["throat"]["ci"] == [0.0, 0.0]


def test_summary_is_deterministic_and_per_condition(fe):
    rows = (_rows(["stash the changes"], {m: ["stash the chains"] for m in fe.MODES}, "quiet")
            + _rows(["run the tests"], {m: ["run the tests"] for m in fe.MODES}, "noisy"))
    a, b = fe.summarize(rows, b=100), fe.summarize(rows, b=100)
    assert a == b
    assert a["quiet"]["modes"]["fusion"]["wer"] == pytest.approx(1 / 3)
    assert a["noisy"]["modes"]["fusion"]["wer"] == 0


def test_report_names_every_mode_and_condition(fe):
    s = fe.summarize(_rows(["stash the changes"], {m: ["stash the changes"] for m in fe.MODES}), b=50)
    md = fe.report(s, model="m", label="M", session="main", recorded="one speaker")
    for label in fe.LABELS.values():
        assert label in md
    assert "quiet (1 phrases, 3 words)" in md and "1 of 1" in md


# ── takes and phrases ──────────────────────────────────────────────────────
def test_a_redo_replaces_the_earlier_take(fe, tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    lines = [{"condition": "quiet", "text": "stop", "file": "quiet/stop.wav", "take": 1},
             {"condition": "noisy", "text": "stop", "file": "noisy/stop.wav", "take": 1},
             {"condition": "quiet", "text": "stop", "file": "quiet/stop.wav", "take": 2}]
    manifest.write_text("".join(json.dumps(x) + "\n" for x in lines))
    takes = {(t["condition"], t["take"]) for t in fe.latest_takes(manifest)}
    assert takes == {("quiet", 2), ("noisy", 1)}


def test_the_phrase_list_follows_its_rules(fe):
    phrases = fe.load_phrases()
    assert len(phrases) >= 40
    assert len({p.lower() for p in phrases}) == len(phrases), "duplicate phrase"
    for p in phrases:
        assert not re.search(r"\d", p), f"{p!r}: no digits (STT may write 6 or six)"
        assert not set(fe.words(p)) & NUMBER_WORDS, f"{p!r}: no number words"
        assert 1 <= len(fe.words(p)) <= 10, f"{p!r}: one to ten words"
        assert fe.words(p) == p.split(), f"{p!r}: lowercase, no punctuation"


# ── the app's path ─────────────────────────────────────────────────────────
def _session(tmp_path, takes, channels=2, sr=48000):
    session = tmp_path / "main"
    (session / "quiet").mkdir(parents=True)
    with (session / "manifest.jsonl").open("w") as f:
        for text in takes:
            rel = f"quiet/{text.replace(' ', '-')}.wav"
            sf.write(session / rel, np.zeros((sr // 2, channels), np.float32), sr, subtype="FLOAT")
            f.write(json.dumps({"condition": "quiet", "text": text, "file": rel, "sr": sr}) + "\n")
    return session


def test_every_take_is_scored_in_every_mode_through_the_capture_path(fe, tmp_path, monkeypatch):
    session = _session(tmp_path, ["stash the changes", "run the tests"])
    calls = []
    real = fusion.render_capture

    def spy(raw, sr, mode="fusion"):
        calls.append((raw.shape, sr, mode))
        return real(raw, sr, mode)
    monkeypatch.setattr(fusion, "render_capture", spy)
    monkeypatch.setattr(fe.stt, "transcribe", lambda wav, model: SimpleNamespace(text=" Run the tests. "))
    rows = fe.score_takes(session, "any-model", progress=lambda _line: None)
    assert sorted(mode for *_, mode in calls) == sorted(fe.MODES * 2)
    assert {(shape, sr) for shape, sr, _ in calls} == {((24000, 2), 48000)}
    assert [r["hyp"]["fusion"] for r in rows] == ["Run the tests."] * 2


def test_a_one_channel_take_is_refused(fe, tmp_path):
    session = _session(tmp_path, ["stop"], channels=1)
    with pytest.raises(SystemExit, match="one channel"):
        fe.score_takes(session, "any-model", progress=lambda _line: None)


def test_the_server_saves_what_the_evaluation_scores(tmp_path, monkeypatch):
    """The claim behind the numbers: a recording the app makes is
    fusion.render_capture's output, so scoring render_capture scores the app."""
    server = pytest.importorskip("server", reason="server.py needs the audio stack")
    monkeypatch.setattr(server, "RECORDINGS_DIR", tmp_path)
    sr = 48000
    t = np.arange(sr // 2) / sr
    raw = np.stack([0.3 * np.sin(2 * np.pi * 220 * t), 0.2 * np.sin(2 * np.pi * 3100 * t)],
                   axis=1).astype(np.float32)
    for mode in fusion.MODES:
        entry = server._finalize_clip(raw, sr, mode)
        saved, saved_sr = sf.read(tmp_path / entry["file"], dtype="float32")
        assert saved_sr == fusion.TARGET_SR
        np.testing.assert_allclose(saved, fusion.render_capture(raw, sr, mode), atol=1e-4)


def test_the_channel_offset_is_measured_and_reported(fe):
    """Fusion assumes one device clock. A throat channel that trails the air mic
    (a Bluetooth link in an Aggregate Device) must show up in the numbers."""
    sr = 48000
    rng = np.random.default_rng(0)
    voicing = np.repeat(rng.random(40) > 0.5, sr // 20).astype(np.float32)    # 2 s of on/off
    air = voicing * np.sin(2 * np.pi * 180 * np.arange(voicing.size) / sr).astype(np.float32)
    lag = int(0.120 * sr)                                                      # 120 ms
    throat = np.concatenate([np.zeros(lag, np.float32), air[:-lag]])
    offset = fe.channel_offset_ms(np.stack([throat, air], axis=1), sr)
    assert offset == pytest.approx(120, abs=5)
    assert abs(fe.channel_offset_ms(np.stack([air, air], axis=1), sr)) < 1

    rows = _rows(["stop"], {m: ["stop"] for m in fe.MODES})
    rows[0]["offset_ms"] = offset
    md = fe.report(fe.summarize(rows, b=50), model="m", label="M", session="main", recorded="x")
    assert "quiet 120 ms" in md and "not sample-aligned hardware" in md
