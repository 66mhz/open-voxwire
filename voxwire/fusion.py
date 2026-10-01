"""voxwire/fusion.py — dual-mic (throat + air) fusion. Pure DSP, no I/O.

Why this exists (the Voxwire differentiator):
- A throat/contact mic reads the larynx: noise-immune and stealthy, but
  band-limited — it keeps voicing/vowels and drops high-frequency consonants
  (s/f/t/k/sh), which is exactly what STT needs.
- An air mic carries those consonants, but also every bit of room noise.

Fusing them keeps the throat's noise immunity in the low/voicing band and
borrows the air mic's high band for the consonants — but only *while you are
actually voicing* (gated by the throat's own energy), so the air mic's noise
doesn't leak in during silence.

Everything here is pure (arrays in, arrays out) so it is unit-testable without
any audio hardware. Capture/I/O lives in server.py; STT stays a separate plugin.
"""
from __future__ import annotations

import numpy as np
import scipy.signal as sps

TARGET_SR = 16000
MODES = ("fusion", "throat", "air")

NPERSEG, NOVERLAP = 512, 384          # 32 ms STFT window, 8 ms hop at 16 kHz
HOP = NPERSEG - NOVERLAP
# How long the air band stays open around voicing. Unvoiced consonants carry
# almost no throat energy but sit right before voicing (the /s/ of "stop", about
# 100-150 ms) or right after it ("its", "risk", up to about 200 ms). Fusion runs
# on a whole clip, so opening early costs nothing.
GATE_PRE_S = 0.15
GATE_POST_S = 0.20


def to_mono(x: np.ndarray) -> np.ndarray:
    """Collapse a (frames, channels) or (channels, frames) array to mono."""
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 1:
        return x
    # assume the longer axis is time
    return x.mean(axis=1 if x.shape[0] >= x.shape[1] else 0).astype(np.float32)


def normalize(x: np.ndarray, target_rms: float = 0.05) -> np.ndarray:
    """Scale to a target RMS so the two mics mix at comparable levels. No-op on
    silence (avoids amplifying noise / dividing by zero)."""
    x = np.asarray(x, dtype=np.float32)
    rms = float(np.sqrt(np.mean(x ** 2))) if x.size else 0.0
    if rms < 1e-6:
        return x
    return (x * (target_rms / rms)).astype(np.float32)


def resample(x: np.ndarray, in_sr: int, out_sr: int = TARGET_SR) -> np.ndarray:
    """Resample mono float32 to out_sr (polyphase). No-op when the rates match."""
    x = np.asarray(x, dtype=np.float32)
    if in_sr == out_sr or x.size == 0:
        return x
    g = int(np.gcd(int(in_sr), int(out_sr)))
    return sps.resample_poly(x, out_sr // g, in_sr // g).astype(np.float32)


def decode_pcm(pcm: bytes, channels: int, in_sr: int, mode: str = "fusion") -> np.ndarray:
    """Decode interleaved little-endian int16 PCM (`channels` samples per frame)
    to a 16 kHz mono float32 clip, fusing when there are two or more channels
    (ch0 = throat, ch1 = air; any further channels are ignored). This is the
    network-input path: a capture node streams PCM, the host decodes → fuses →
    transcribes."""
    if channels < 1:
        raise ValueError(f"channels must be at least 1, got {channels}")
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    a = a[: (a.size // channels) * channels].reshape(-1, channels)
    if channels >= 2:
        throat = resample(a[:, 0], in_sr, TARGET_SR)
        air = resample(a[:, 1], in_sr, TARGET_SR)
        return fuse(throat, air, TARGET_SR, mode=mode, do_align=False)
    return resample(a[:, 0], in_sr, TARGET_SR)


def _envelope(x: np.ndarray, sr: int, win_ms: float = 10.0) -> np.ndarray:
    """Smoothed amplitude envelope — robust basis for time-alignment across two
    mics with very different spectra (they still share the voicing envelope)."""
    w = max(1, int(sr * win_ms / 1000.0))
    return np.convolve(np.abs(x), np.ones(w, dtype=np.float32) / w, mode="same")


def estimate_lag(a: np.ndarray, b: np.ndarray, sr: int,
                 max_lag_ms: float = 60.0) -> int:
    """Integer-sample lag of `a` relative to `b` (positive → a is delayed),
    found by cross-correlating their envelopes, bounded to ±max_lag_ms."""
    ea, eb = _envelope(a, sr), _envelope(b, sr)
    n = min(len(ea), len(eb))
    if n < 2:
        return 0
    ea = ea[:n] - ea[:n].mean()
    eb = eb[:n] - eb[:n].mean()
    corr = sps.correlate(ea, eb, mode="full")
    lags = sps.correlation_lags(len(ea), len(eb), mode="full")
    max_lag = max(1, int(sr * max_lag_ms / 1000.0))
    keep = np.abs(lags) <= max_lag
    if not keep.any():
        return 0
    return int(lags[keep][int(np.argmax(corr[keep]))])


def align(a: np.ndarray, b: np.ndarray, sr: int,
          max_lag_ms: float = 60.0) -> tuple[np.ndarray, np.ndarray]:
    """Time-align two mono signals and trim to a common length. Returns (a, b)
    of equal length, shifted so their voicing envelopes line up."""
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    lag = estimate_lag(a, b, sr, max_lag_ms)
    if lag > 0:            # a is delayed → delay b to match
        b = np.concatenate([np.zeros(lag, dtype=np.float32), b])
    elif lag < 0:          # b is delayed → delay a to match
        a = np.concatenate([np.zeros(-lag, dtype=np.float32), a])
    n = min(len(a), len(b))
    return a[:n], b[:n]


def _stft(x: np.ndarray, sr: int):
    return sps.stft(x, fs=sr, window="hann", nperseg=NPERSEG,
                    noverlap=NOVERLAP, boundary="zeros", padded=True)


def _crossover_weights(freqs: np.ndarray, crossover_hz: float,
                       width_hz: float = 400.0) -> np.ndarray:
    """Raised-cosine throat weight per frequency bin: 1 well below the crossover,
    0 well above, smooth in between. Air weight is 1 - this."""
    lo = crossover_hz - width_hz / 2.0
    hi = crossover_hz + width_hz / 2.0
    w = np.ones_like(freqs, dtype=np.float32)
    band = (freqs >= lo) & (freqs <= hi)
    w[freqs > hi] = 0.0
    if band.any():
        w[band] = 0.5 * (1.0 + np.cos(np.pi * (freqs[band] - lo) / (hi - lo)))
    return w


def _hold(g: np.ndarray, pre: int, post: int) -> np.ndarray:
    """Open every frame as wide as the strongest frame within `pre` frames after
    it or `post` frames before it (so the gate opens `pre` frames ahead of
    voicing and holds `post` frames after), then soften the edges so the air
    band fades in and out instead of clicking."""
    if g.size == 0:
        return g
    padded = np.pad(g, (post, pre))           # frame i sees g[i-post .. i+pre]
    held = np.lib.stride_tricks.sliding_window_view(padded, post + pre + 1).max(axis=1)
    ramp = np.ones(5, dtype=np.float32) / 5   # ~40 ms
    return np.clip(np.convolve(held, ramp, mode="same"), 0.0, 1.0).astype(np.float32)


def _voicing_gate(Zt: np.ndarray, freqs: np.ndarray, crossover_hz: float,
                  sr: int) -> np.ndarray:
    """Per-frame gate in [0,1] from the throat's low-band energy: ~1 while you
    voice and just around it, ~0 in silence. Gates the air mic's high band so its
    room noise only passes when you're actually speaking (the noise-immunity
    trick), while the hold around voicing keeps the unvoiced consonants."""
    low = freqs <= crossover_hz
    energy = np.abs(Zt[low, :]).sum(axis=0) if low.any() else np.abs(Zt).sum(axis=0)
    peak = float(energy.max()) if energy.size else 0.0
    if peak < 1e-9:
        return np.zeros_like(energy)
    # The loud reference comes from the active frames only (those within 20 dB
    # of the peak), not the whole clip: a 90th percentile over a clip that is
    # mostly silence (half a second of speech in a 12 s clip) lands on the
    # silence, and the gate then never opens.
    active = energy[energy >= 0.1 * peak]
    ref = float(np.percentile(active, 90))
    # soft threshold: below ~10% of the loud reference → closed, above ~40% → open
    g = np.clip((energy / ref - 0.10) / 0.30, 0.0, 1.0)
    return _hold(g, pre=round(GATE_PRE_S * sr / HOP), post=round(GATE_POST_S * sr / HOP))


def fuse_stereo(stereo: np.ndarray, sr: int = TARGET_SR, throat_ch: int = 0,
                air_ch: int = 1, mode: str = "fusion") -> np.ndarray:
    """Fuse a 2-channel capture where the channels are already sample-aligned
    (one device / a macOS Aggregate Device: throat on `throat_ch`, air on
    `air_ch`). Alignment is skipped since a shared clock guarantees it. A mono
    input has nothing to fuse and comes back normalized."""
    x = np.asarray(stereo, dtype=np.float32)
    if x.ndim == 1 or x.shape[1] < 2:
        return normalize(to_mono(x))
    return fuse(x[:, throat_ch], x[:, air_ch], sr, mode=mode, do_align=False)


def fuse(throat: np.ndarray, air: np.ndarray, sr: int = TARGET_SR,
         crossover_hz: float = 900.0, mode: str = "fusion",
         do_align: bool = True) -> np.ndarray:
    """Fuse a time-aligned throat + air pair into one mono signal for STT.

    mode="throat" or "air" returns that source alone (normalized); "fusion"
    (default) uses the throat below `crossover_hz` and the voicing-gated air
    above it. Inputs are mono, same sample rate; unequal lengths are aligned +
    trimmed. Returns float32.
    """
    throat = normalize(to_mono(throat))
    air = normalize(to_mono(air))
    if mode == "throat":
        return throat
    if mode == "air":
        return air
    if mode != "fusion":
        raise ValueError(f"unknown fusion mode {mode!r} (want one of {MODES})")

    if do_align:
        throat, air = align(throat, air, sr)
    else:
        n = min(len(throat), len(air))
        throat, air = throat[:n], air[:n]
    n = len(throat)
    if n == 0:
        return throat
    if n < NPERSEG:                           # shorter than one STFT window: pad, then trim
        throat = np.pad(throat, (0, NPERSEG - n))
        air = np.pad(air, (0, NPERSEG - n))

    freqs, _, Zt = _stft(throat, sr)
    _, _, Za = _stft(air, sr)
    tframes = min(Zt.shape[1], Za.shape[1])
    Zt, Za = Zt[:, :tframes], Za[:, :tframes]

    wt = _crossover_weights(freqs, crossover_hz)[:, None]       # throat weight (freq)
    wa = 1.0 - wt                                               # air weight (freq)
    gate = _voicing_gate(Zt, freqs, crossover_hz, sr)[None, :]  # per-frame (air only)

    Zf = wt * Zt + wa * gate * Za
    _, fused = sps.istft(Zf, fs=sr, window="hann", nperseg=NPERSEG, noverlap=NOVERLAP)
    fused = np.asarray(fused, dtype=np.float32)
    return normalize(fused[:n])
