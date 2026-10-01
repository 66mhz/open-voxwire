"""Dual-mic fusion DSP tests (BYT-118) — pure/synthetic, no audio hardware.

Verifies the three properties the fusion has to have:
  1. time-alignment recovers a known lag,
  2. fusion recovers high-frequency (consonant) energy the throat mic lacks,
  3. the throat-voicing gate suppresses the air mic's noise during silence.
"""
import numpy as np
import pytest
import scipy.signal as sps

import fusion

SR = 16000


def tone(freq, dur, sr=SR, amp=1.0):
    t = np.arange(int(dur * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def high_rms(x, sr=SR, cutoff=2000.0):
    """RMS of the >cutoff band — our proxy for 'consonant' energy."""
    b, a = sps.butter(4, cutoff / (sr / 2), btype="high")
    y = sps.filtfilt(b, a, x)
    return float(np.sqrt(np.mean(y ** 2)))


# ── alignment ────────────────────────────────────────────────────────────────
def test_estimate_lag_recovers_known_delay():
    base = np.concatenate([np.zeros(2000, np.float32), tone(300, 0.25),
                           np.zeros(2000, np.float32)])
    delay = 320                              # 20 ms
    a = np.concatenate([np.zeros(delay, np.float32), base])   # a lags base
    lag = fusion.estimate_lag(a, base, SR)
    assert abs(lag - delay) <= 32            # within ~2 ms


def test_align_lines_up_and_equal_length():
    base = np.concatenate([np.zeros(1500, np.float32), tone(250, 0.3),
                           np.zeros(1500, np.float32)])
    a = np.concatenate([np.zeros(256, np.float32), base])
    a2, b2 = fusion.align(a, base, SR)
    assert len(a2) == len(b2)
    # correlation at zero lag is near-maximal after alignment
    resid = fusion.estimate_lag(a2, b2, SR)
    assert abs(resid) <= 32


# ── mode passthrough ─────────────────────────────────────────────────────────
def test_modes_passthrough():
    throat, air = tone(200, 0.5), tone(200, 0.5) + tone(4000, 0.5, amp=0.5)
    only_t = fusion.fuse(throat, air, SR, mode="throat")
    only_a = fusion.fuse(throat, air, SR, mode="air")
    # throat mode has ~no high band; air mode does
    assert high_rms(only_t) < 1e-3
    assert high_rms(only_a) > 1e-2


def test_unknown_mode_raises():
    try:
        fusion.fuse(tone(200, 0.2), tone(200, 0.2), SR, mode="bogus")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown mode")


# ── the whole point: recover consonants, reject noise ────────────────────────
def test_fusion_recovers_high_frequency_from_air():
    # throat: voicing only (200 Hz). air: same voicing + a 4 kHz 'consonant'.
    throat = tone(200, 1.0)
    air = tone(200, 1.0) + tone(4000, 1.0, amp=0.6)
    fused = fusion.fuse(throat, air, SR, mode="fusion", do_align=False)
    assert not np.isnan(fused).any()
    assert fused.dtype == np.float32
    # fused has clearly more high-band energy than the throat alone
    assert high_rms(fused) > 10 * high_rms(throat) + 1e-3


def test_voicing_gate_suppresses_air_noise_in_silence():
    # 2 s: first half throat-silent, second half voiced. Air has a 4 kHz tone
    # the WHOLE time (stand-in for room noise present during silence).
    sil = np.zeros(SR, np.float32)
    throat = np.concatenate([sil, tone(200, 1.0)])
    air = tone(4000, 2.0, amp=0.5)
    fused = fusion.fuse(throat, air, SR, mode="fusion", do_align=False)

    half = len(fused) // 2
    lead = fused[: int(0.4 * SR)]            # throat-silent region
    tail = fused[half + int(0.1 * SR): half + int(0.5 * SR)]   # voiced region
    e_silent, e_voiced = high_rms(lead), high_rms(tail)
    # high band passes when voicing, is largely gated out during silence
    assert e_voiced > 3 * e_silent + 1e-4
    # and the gated-out level is well below the raw air in that region
    assert e_silent < 0.5 * high_rms(air[: int(0.4 * SR)])


def test_fuse_stereo_splits_channels_and_recovers_highs():
    throat = tone(200, 1.0)                       # ch0: voicing only
    air = tone(200, 1.0) + tone(4000, 1.0, amp=0.6)   # ch1: + consonant
    stereo = np.stack([throat, air], axis=1)      # (frames, 2), sample-aligned
    fused = fusion.fuse_stereo(stereo, SR, mode="fusion")
    assert fused.ndim == 1
    assert high_rms(fused) > 10 * high_rms(throat) + 1e-3
    # a mono input has nothing to fuse: it comes back normalized, still mono
    mono = fusion.fuse_stereo(throat, SR)
    assert mono.ndim == 1
    assert np.allclose(mono, fusion.normalize(throat))


def test_decode_pcm_stereo_and_mono():
    throat = tone(200, 1.0)
    air = tone(200, 1.0) + tone(4000, 1.0, amp=0.6)
    stereo_i16 = (np.stack([throat, air], axis=1) * 20000).astype("<i2")
    fused = fusion.decode_pcm(stereo_i16.tobytes(), channels=2, in_sr=SR, mode="fusion")
    assert fused.ndim == 1 and fused.dtype == np.float32
    assert high_rms(fused) > 10 * high_rms(throat) + 1e-3
    # mono PCM decodes straight through (resampled to 16 kHz)
    mono_i16 = (throat * 20000).astype("<i2")
    m = fusion.decode_pcm(mono_i16.tobytes(), channels=1, in_sr=SR)
    assert m.ndim == 1 and abs(len(m) - len(throat)) <= 1


def test_resample_roundtrip_length():
    x = tone(300, 0.5, sr=48000)
    y = fusion.resample(x, 48000, 16000)
    assert abs(len(y) - 16000 * 0.5) <= 2
    assert np.array_equal(fusion.resample(x, 16000, 16000), x)  # no-op when equal


# ── basics ───────────────────────────────────────────────────────────────────
def test_to_mono_and_normalize():
    stereo = np.stack([tone(300, 0.2), tone(300, 0.2)], axis=1)   # (frames, 2)
    assert fusion.to_mono(stereo).ndim == 1
    loud = tone(300, 0.2, amp=3.0)
    n = fusion.normalize(loud, target_rms=0.05)
    assert abs(float(np.sqrt(np.mean(n ** 2))) - 0.05) < 1e-3
    assert np.array_equal(fusion.normalize(np.zeros(100, np.float32)),
                          np.zeros(100, np.float32))   # silence unchanged


# ── unvoiced consonants next to voicing (BYT-118 review) ─────────────────────
def fricative(dur, sr=SR, rms=0.3, seed=0):
    """Band-limited noise at 4-7 kHz: an /s/-like unvoiced consonant."""
    rng = np.random.default_rng(seed)
    b, a = sps.butter(4, [4000 / (sr / 2), 7000 / (sr / 2)], btype="band")
    x = sps.lfilter(b, a, rng.standard_normal(int(dur * sr)))
    return (rms * x / np.sqrt(np.mean(x ** 2))).astype(np.float32)


def band_rms(x, t0, t1, sr=SR, lo=4000.0, hi=7000.0):
    b, a = sps.butter(4, [lo / (sr / 2), hi / (sr / 2)], btype="band")
    y = sps.filtfilt(b, a, x)[int(t0 * sr):int(t1 * sr)]
    return float(np.sqrt(np.mean(y ** 2)))


def _word_with_s_at_both_ends():
    """'sus'-like: /s/ 330-450 ms, vowel 450-850 ms, /s/ 850-1000 ms, silence
    to 1.4 s. The throat only hears the vowel; the air hears everything plus
    faint room noise."""
    n = int(1.4 * SR)
    throat = np.zeros(n, np.float32)
    v0, v1 = int(0.45 * SR), int(0.85 * SR)
    throat[v0:v1] = tone(200, 0.40)
    air = np.random.default_rng(1).standard_normal(n).astype(np.float32) * 0.01
    air[v0:v1] += tone(200, 0.40)
    air[int(0.33 * SR):v0] += fricative(0.12, seed=2)
    air[v1:int(1.0 * SR)] += fricative(0.15, seed=3)
    return throat, air


def test_fusion_keeps_unvoiced_consonants_around_voicing():
    # /s/ /f/ /t/ /k/ carry almost no throat energy, so a gate that opens only
    # on throat energy drops the very sounds fusion exists to recover.
    throat, air = _word_with_s_at_both_ends()
    fused = fusion.fuse(throat, air, SR, mode="fusion", do_align=False)
    air_n = fusion.normalize(air)
    for t0, t1 in ((0.35, 0.44), (0.86, 0.99)):          # the two /s/ windows
        kept = band_rms(fused, t0, t1) / band_rms(air_n, t0, t1)
        assert kept > 0.5, f"/s/ at {t0}-{t1}s kept only {kept:.0%} of its energy"


def test_fusion_still_gates_room_noise_away_from_speech():
    throat, air = _word_with_s_at_both_ends()
    fused = fusion.fuse(throat, air, SR, mode="fusion", do_align=False)
    air_n = fusion.normalize(air)
    for t0, t1 in ((0.02, 0.15), (1.25, 1.38)):          # far from any speech
        assert band_rms(fused, t0, t1) < 0.1 * band_rms(air_n, t0, t1)


def test_fusion_handles_clips_shorter_than_one_stft_window():
    rng = np.random.default_rng(4)
    for n in (8, 100, 384, 511):
        out = fusion.fuse(rng.standard_normal(n), rng.standard_normal(n), SR,
                          mode="fusion", do_align=False)
        assert len(out) == n and out.dtype == np.float32 and np.isfinite(out).all()


def test_decode_pcm_uses_the_declared_channel_stride():
    throat = tone(200, 0.5)
    air = tone(200, 0.5) + tone(4000, 0.5, amp=0.6)
    junk = tone(1000, 0.5, amp=0.9)                     # a third channel to ignore
    frames = (np.stack([throat, air, junk], axis=1) * 20000).astype("<i2")
    got = fusion.decode_pcm(frames.tobytes(), channels=3, in_sr=SR, mode="fusion")
    want = fusion.fuse(frames[:, 0] / 32768.0, frames[:, 1] / 32768.0, SR,
                       mode="fusion", do_align=False)
    assert np.allclose(got, want, atol=1e-5)


def test_decode_pcm_rejects_a_channel_count_below_one():
    try:
        fusion.decode_pcm(b"\x00\x00" * 16, channels=0, in_sr=SR)
    except ValueError:
        return
    raise AssertionError("expected ValueError for channels=0")


# ── brief speech in a long clip (PR #9 review) ───────────────────────────────
def _brief_word_in_long_silence(throat_floor=0.002):
    """0.5 s of voicing + a 4 kHz 'consonant' at 6.0-6.5 s of a 12 s clip. The
    air mic carries room noise; the throat a faint floor (or none)."""
    n = 12 * SR
    rng = np.random.default_rng(5)
    throat = rng.standard_normal(n).astype(np.float32) * throat_floor
    air = rng.standard_normal(n).astype(np.float32) * 0.01
    v0, v1 = 6 * SR, int(6.5 * SR)
    throat[v0:v1] += tone(200, 0.5)
    air[v0:v1] += tone(200, 0.5) + tone(4000, 0.5, amp=0.6)
    return throat, air


@pytest.mark.parametrize("throat_floor", [0.0, 0.002])
def test_fusion_opens_the_air_band_for_brief_speech_in_a_long_clip(throat_floor):
    # A whole-clip 90th percentile lands on the silence when speech is under
    # 10% of the clip: with a silent throat the gate never opened (every
    # consonant dropped); with a throat noise floor it opened on the noise.
    throat, air = _brief_word_in_long_silence(throat_floor)
    fused = fusion.fuse(throat, air, SR, mode="fusion", do_align=False)
    air_n = fusion.normalize(air)
    kept = band_rms(fused, 6.1, 6.4, lo=3500, hi=4500) / band_rms(air_n, 6.1, 6.4, lo=3500, hi=4500)
    assert kept > 0.5, f"the brief word kept only {kept:.0%} of its high band"


@pytest.mark.parametrize("throat_floor", [0.0, 0.002])
def test_fusion_gates_room_noise_around_brief_speech(throat_floor):
    throat, air = _brief_word_in_long_silence(throat_floor)
    fused = fusion.fuse(throat, air, SR, mode="fusion", do_align=False)
    air_n = fusion.normalize(air)
    for t0, t1 in ((1.0, 3.0), (9.0, 11.0)):            # far from the word
        assert band_rms(fused, t0, t1) < 0.1 * band_rms(air_n, t0, t1)


# ── render_capture: what the app saves and the evaluation scores ─────────────
def test_render_capture_resamples_each_channel_then_fuses():
    sr = 48000
    raw = np.stack([tone(200, 1.0, sr), tone(200, 1.0, sr) + tone(4000, 1.0, sr, amp=0.6)], axis=1)
    for mode in fusion.MODES:
        expected = fusion.fuse(fusion.resample(raw[:, 0], sr), fusion.resample(raw[:, 1], sr),
                               SR, mode=mode, do_align=False)
        np.testing.assert_array_equal(fusion.render_capture(raw, sr, mode), expected)


def test_render_capture_keeps_throat_on_ch0_and_air_on_ch1():
    """A swapped channel would silently swap every throat-only and air-only score."""
    raw = np.stack([tone(200, 1.0), tone(4000, 1.0, amp=0.6)], axis=1)
    throat = fusion.render_capture(raw, SR, "throat")       # every mode is normalized,
    air = fusion.render_capture(raw, SR, "air")             # so compare shares, not levels

    def rms(x):
        return float(np.sqrt(np.mean(x ** 2)))
    assert high_rms(throat) < 0.05 * rms(throat)
    assert high_rms(air) > 0.9 * rms(air)


def test_render_capture_passes_mono_through_resampled():
    mono = tone(200, 1.0, 48000)
    np.testing.assert_array_equal(fusion.render_capture(mono, 48000), fusion.resample(mono, 48000))
    np.testing.assert_array_equal(fusion.render_capture(mono[:, None], 48000),
                                  fusion.resample(mono, 48000))
