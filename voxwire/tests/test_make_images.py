"""Tests for docs/images/make_images.py, the script behind the README's images.

It publishes figures for a public README, so the tests cover what could put a
wrong or broken image there. A clip of your own can't inherit the built-in
clip's claims, and a demo server that never came up can't be captured as the
screenshot. A palette PNG is refused when it hazes the transparent background,
and flat fills come through exactly. Rendering needs Chrome and matplotlib and
isn't tested here. The one test that needs Pillow skips without it.
"""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "docs" / "images" / "make_images.py"


@pytest.fixture
def mi():
    spec = importlib.util.spec_from_file_location("make_images", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_main(mi, monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["make_images.py", *argv])
    mi.main()


# ── the spectrogram's claims ────────────────────────────────────────────────
def test_a_clip_of_your_own_needs_its_words(mi, monkeypatch, tmp_path):
    monkeypatch.setattr(mi, "spectrogram", lambda *a: pytest.fail("rendered without --phrase"))
    with pytest.raises(SystemExit) as exc:
        _run_main(mi, monkeypatch, "--only", "spectrogram", "--out", str(tmp_path), "--wav", "clip.wav")
    assert exc.value.code == 2


@pytest.mark.parametrize("extra", [["--phrase", "hello there"], ["--mark", "0.1", "0.2"]])
def test_phrase_and_mark_only_describe_a_wav(mi, monkeypatch, tmp_path, extra):
    monkeypatch.setattr(mi, "spectrogram", lambda *a: pytest.fail("rendered without --wav"))
    with pytest.raises(SystemExit) as exc:
        _run_main(mi, monkeypatch, "--only", "spectrogram", "--out", str(tmp_path), *extra)
    assert exc.value.code == 2


def test_a_clip_of_your_own_gets_its_words_and_mark_not_the_builtin_ones(mi, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(mi, "spectrogram", lambda *a: calls.append(a))
    _run_main(mi, monkeypatch, "--only", "spectrogram", "--out", str(tmp_path),
              "--wav", "clip.wav", "--phrase", "hello there")
    _run_main(mi, monkeypatch, "--only", "spectrogram", "--out", str(tmp_path),
              "--wav", "clip.wav", "--phrase", "hello there", "--mark", "0.5", "0.7")
    assert calls == [(tmp_path, Path("clip.wav"), "hello there", None),
                     (tmp_path, Path("clip.wav"), "hello there", (0.5, 0.7))]


# ── the web UI screenshot ───────────────────────────────────────────────────
def _no_capture(mi, monkeypatch):
    monkeypatch.setattr(mi, "PYTHON", Path(sys.executable))
    monkeypatch.setattr(mi, "_chrome", lambda *a, **k: pytest.fail("captured a server that never answered"))


def test_ui_stops_when_the_demo_server_dies(mi, monkeypatch, tmp_path):
    _no_capture(mi, monkeypatch)
    monkeypatch.setattr(mi, "DEMO_SERVER", "raise SystemExit(3)")
    with pytest.raises(SystemExit, match="exited with code 3"):
        mi.ui(tmp_path)
    assert not (tmp_path / "web-config.png").exists()


def test_ui_stops_when_the_demo_server_never_answers(mi, monkeypatch, tmp_path):
    _no_capture(mi, monkeypatch)
    monkeypatch.setattr(mi, "DEMO_SERVER", "import time; time.sleep(30)")
    monkeypatch.setattr(mi, "READY_TIMEOUT_S", 0.5)
    with pytest.raises(SystemExit, match="didn't answer"):
        mi.ui(tmp_path)
    assert not (tmp_path / "web-config.png").exists()


def test_ui_needs_the_project_env(mi, monkeypatch, tmp_path):
    monkeypatch.setattr(mi, "PYTHON", tmp_path / "no-such-python")
    with pytest.raises(SystemExit, match="install.sh"):
        mi.ui(tmp_path)


# ── palette PNGs ────────────────────────────────────────────────────────────
def test_a_palette_that_hazes_the_background_is_refused(mi):
    before = np.zeros((100, 100), dtype=int)
    before[40:60, 40:60] = 255                       # something opaque on a clear ground
    hazed = np.where(before == 0, 14, before)        # the octree's faint box
    assert not mi._intact(before, hazed)


def test_a_folded_shadow_is_refused_but_stray_edge_pixels_are_not(mi):
    before = np.zeros((200, 200), dtype=int)
    before[:, 100:] = np.linspace(1, 90, 100, dtype=int)   # a soft shadow ramp
    folded = before.copy()
    folded[:, 100:] = np.where(before[:, 100:] < 60, 1, before[:, 100:])
    assert not mi._intact(before, folded)

    stray = before.copy()
    stray[0, 150:153] += 40                          # three edge pixels in 40,000
    assert mi._intact(before, stray)


def test_flat_art_keeps_its_fills_exactly(mi, tmp_path):
    Image = pytest.importorskip("PIL.Image")
    rgba = np.zeros((120, 400, 4), dtype=np.uint8)
    rgba[:, :100] = (255, 255, 255, 255)             # a white card
    rgba[:, 100:200] = (238, 241, 245, 255)          # a panel
    rgba[:, 200:300] = (251, 252, 253, 255)          # a figure's near-white fill
    ramp = np.random.default_rng(1).integers(0, 256, size=(120, 100, 3))   # > 256 colors
    rgba[:, 300:, :3], rgba[:, 300:, 3] = ramp, 255
    path = tmp_path / "flat.png"
    Image.fromarray(rgba, "RGBA").save(path)

    mi._quantize(path, flat=True)

    out = Image.open(path)
    assert out.mode == "P"
    got = np.asarray(out.convert("RGBA"))
    for x, color in ((50, (255, 255, 255, 255)), (150, (238, 241, 245, 255)), (250, (251, 252, 253, 255))):
        assert tuple(got[60, x]) == color
