#!/usr/bin/env python3
"""Measure what dual-mic fusion buys you: word error rate for fusion,
throat only and air only, on your own recordings.

You read each phrase once per condition through a 2-channel device (throat on
ch0, air on ch1). The raw stereo is kept, so all three modes are scored on the
same take, through the app's own path: fusion.render_capture, then
stt.transcribe.

    voxwire/.venv/bin/python scripts/fusion_eval.py devices
    voxwire/.venv/bin/python scripts/fusion_eval.py check  --device "Throat + Air"
    voxwire/.venv/bin/python scripts/fusion_eval.py record --device "Throat + Air" --condition quiet
    voxwire/.venv/bin/python scripts/fusion_eval.py record --device "Throat + Air" --condition noisy
    voxwire/.venv/bin/python scripts/fusion_eval.py score  [--model KEY] [--markdown FILE]

The recordings are your voice. They stay in recordings/eval/ (git-ignored) and
never leave this machine; only the scores are meant to be shared. The protocol
is in docs/fusion-eval.md.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import random
import re
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "voxwire"))

import fusion  # noqa: E402  the app's own DSP: what the app fuses is what gets scored
import stt     # noqa: E402  the app's own speech-to-text layer

PHRASES = ROOT / "scripts" / "fusion_eval_phrases.txt"
DATA = ROOT / "recordings" / "eval"
MODES = ("throat", "air", "fusion")
LABELS = {"throat": "Throat only", "air": "Air only", "fusion": "Fusion"}
PRE_ROLL_S, POST_ROLL_S = 0.3, 0.4   # a take starts a beat before Enter and ends a beat after
BOOTSTRAP = 2000
CONDITION = re.compile(r"[a-z0-9][a-z0-9-]*")
SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
OFFSET_WARN_MS = 10.0                # beyond this, the channels aren't on one clock


# ── text and scores (pure) ─────────────────────────────────────────────────
def words(text: str) -> list[str]:
    """Lowercase words with punctuation dropped and hyphens split, so
    "Stash the changes." and "stash the changes" score the same."""
    text = text.lower().replace("’", "'")
    text = re.sub(r"[-–—/]", " ", text)
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return text.replace("'", "").split()


def edit_distance(ref: list[str], hyp: list[str]) -> int:
    """Word-level Levenshtein distance: substitutions + deletions + insertions."""
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1]


def load_phrases(path: Path = PHRASES) -> list[str]:
    lines = (line.strip() for line in Path(path).read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def latest_takes(manifest: Path) -> list[dict]:
    """One take per (condition, phrase): the last one recorded, so a redo wins."""
    takes: dict[tuple[str, str], dict] = {}
    if manifest.exists():
        for line in manifest.read_text().splitlines():
            if line.strip():
                take = json.loads(line)
                takes[(take["condition"], take["text"])] = take
    return list(takes.values())


def summarize(rows: list[dict], *, b: int = BOOTSTRAP, seed: int = 151) -> dict:
    """Corpus WER per condition and mode with a 95% bootstrap interval, phrases
    transcribed exactly right, and fusion's paired difference against each
    single mic.

    `rows` holds one dict per scored take: {"condition", "text", "hyp": {mode: text}}.
    The bootstrap resamples phrases with replacement and scores every mode on the
    same resample, so the differences are paired. Deterministic for a seed."""
    out = {}
    for cond in sorted({r["condition"] for r in rows}):
        rs = [r for r in rows if r["condition"] == cond]
        modes = [m for m in MODES if all(m in r["hyp"] for r in rs)]
        refs = [words(r["text"]) for r in rs]
        lens = np.array([len(ref) for ref in refs], dtype=float)
        edits = {m: np.array([edit_distance(ref, words(r["hyp"][m])) for ref, r in zip(refs, rs)],
                             dtype=float) for m in modes}
        idx = np.random.default_rng(seed).integers(0, len(rs), size=(b, len(rs)))
        total = lens[idx].sum(axis=1)
        boot = {m: edits[m][idx].sum(axis=1) / total for m in modes}

        def interval(x: np.ndarray) -> list[float]:
            lo, hi = np.percentile(x, [2.5, 97.5])
            return [float(lo), float(hi)]

        res = {"n": len(rs), "words": int(lens.sum()), "modes": {
            m: {"wer": float(edits[m].sum() / lens.sum()), "ci": interval(boot[m]),
                "exact": int((edits[m] == 0).sum())} for m in modes}}
        if all("offset_ms" in r for r in rs):
            res["offset_ms"] = float(np.median([abs(r["offset_ms"]) for r in rs]))
        if "fusion" in modes:
            res["fusion_minus"] = {
                m: {"diff": res["modes"]["fusion"]["wer"] - res["modes"][m]["wer"],
                    "ci": interval(boot["fusion"] - boot[m])}
                for m in modes if m != "fusion"}
        out[cond] = res
    return out


def report(summary: dict, *, model: str, label: str, session: str, recorded: str) -> str:
    """The summary as Markdown, ready for docs/fusion-eval.md."""
    def pct(x: float) -> str:
        return f"{100 * x:.1f}%"

    def pts(x: float) -> str:
        return f"{100 * x:+.1f}"

    head = "| Condition | " + " | ".join(LABELS[m] for m in MODES) + " |"
    rule = "|---|" + "---|" * len(MODES)
    wer = [head, rule]
    exact = [head, rule]
    for cond, s in summary.items():
        name = f"{cond} ({s['n']} phrases, {s['words']} words)"
        wer.append(f"| {name} | " + " | ".join(
            f"{pct(s['modes'][m]['wer'])} ({pct(s['modes'][m]['ci'][0])}–{pct(s['modes'][m]['ci'][1])})"
            if m in s["modes"] else "–" for m in MODES) + " |")
        exact.append(f"| {cond} | " + " | ".join(
            f"{s['modes'][m]['exact']} of {s['n']}" if m in s["modes"] else "–" for m in MODES) + " |")
    diffs = []
    for cond, s in summary.items():
        for m, d in s.get("fusion_minus", {}).items():
            diffs.append(f"- {cond}: fusion vs {LABELS[m].lower()}, {pts(d['diff'])} points "
                         f"(95% interval {pts(d['ci'][0])} to {pts(d['ci'][1])})")
    offsets = {c: s["offset_ms"] for c, s in summary.items() if "offset_ms" in s}
    if offsets:
        line = ("Median offset between the channels: "
                + ", ".join(f"{c} {ms:.0f} ms" for c, ms in offsets.items()) + ".")
        if max(offsets.values()) > OFFSET_WARN_MS:
            line += (" Fusion assumes one device clock, so these fusion scores describe this "
                     "setup, not sample-aligned hardware.")
        diffs += ["", line]
    return "\n".join([
        f"Model `{model}` ({label}). Session `{session}`, {recorded}. "
        f"Scored {datetime.now():%Y-%m-%d}.",
        "",
        "**Word error rate** (95% interval)",
        "",
        *wer,
        "",
        "**Phrases transcribed exactly right**",
        "",
        *exact,
        "",
        "**Fusion's difference in word error rate** (negative is better)",
        "",
        *diffs,
        "",
        "Word error rate is (substitutions + deletions + insertions) / reference words, after "
        "lowercasing and dropping punctuation. The intervals come from "
        f"{BOOTSTRAP:,} bootstrap resamples of the phrases; the differences are paired, "
        "because every mode is scored on the same takes.",
        "",
    ])


# ── scoring (reads the recordings, runs the app's path) ────────────────────
def score_takes(session: Path, model: str, *, progress=print) -> list[dict]:
    """Render every take in every mode exactly as the app would, and transcribe it."""
    takes = sorted(latest_takes(session / "manifest.jsonl"),
                   key=lambda t: (t["condition"], t["text"]))
    if not takes:
        raise SystemExit(f"no takes in {session}; record some first (see --help)")
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "clip.wav"
        for i, take in enumerate(takes, 1):
            audio, sr = sf.read(session / take["file"], dtype="float32", always_2d=True)
            if audio.shape[1] < 2:
                raise SystemExit(f"{take['file']} has one channel; fusion needs throat (ch0) + air (ch1)")
            hyp = {}
            for mode in MODES:
                sf.write(wav, fusion.render_capture(audio, sr, mode), fusion.TARGET_SR)
                hyp[mode] = stt.transcribe(wav, model).text.strip()
            rows.append({"condition": take["condition"], "text": take["text"],
                         "file": take["file"], "hyp": hyp,
                         "offset_ms": channel_offset_ms(audio, sr)})
            ref = words(take["text"])
            marks = " ".join(f"{m}:{'ok' if words(hyp[m]) == ref else 'x'}" for m in MODES)
            progress(f"  [{i}/{len(takes)}] {take['condition']:>7} · {take['text']:<34} {marks}")
    return rows


# ── audio I/O ──────────────────────────────────────────────────────────────
def _sounddevice():
    try:
        import sounddevice as sd
    except OSError as e:          # PortAudio missing
        raise SystemExit(f"can't open audio: {e}")
    return sd


def _input_devices(sd) -> list[tuple[int, dict]]:
    return [(i, d) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] >= 2]


def _pick_device(sd, spec: str) -> int:
    devices = _input_devices(sd)
    if spec.isdigit():
        if any(i == int(spec) for i, _ in devices):
            return int(spec)
        raise SystemExit(f"device {spec} isn't an input with 2 or more channels; see `devices`")
    found = [(i, d) for i, d in devices if spec.lower() in d["name"].lower()]
    if len(found) == 1:
        return found[0][0]
    names = ", ".join(f"[{i}] {d['name']}" for i, d in (found or devices)) or "none"
    raise SystemExit(f"{'several' if found else 'no'} 2-channel inputs match {spec!r}: {names}")


class Recorder:
    """An always-open 2-channel input with a short history, so a take can start
    a beat before you press Enter, like the app's warm mic."""

    def __init__(self, sd, device: int, sr: int, keep_s: float = 120.0):
        self.sr, self.keep = sr, int(keep_s * sr)
        self.blocks: collections.deque = collections.deque()
        self.gaps: collections.deque = collections.deque()   # frames where input was dropped
        self.total = 0
        self.lock = threading.Lock()
        self.stream = sd.InputStream(device=device, channels=2, samplerate=sr,
                                     dtype="float32", callback=self._callback)

    def _callback(self, indata, frames, _time, status) -> None:
        with self.lock:
            if status.input_overflow:            # samples were lost just before this block
                self.gaps.append(self.total)
            self.blocks.append((self.total, indata[:, :2].copy()))
            self.total += frames
            while self.blocks and self.blocks[0][0] + len(self.blocks[0][1]) < self.total - self.keep:
                self.blocks.popleft()
            while self.gaps and self.gaps[0] < self.total - self.keep:
                self.gaps.popleft()

    def __enter__(self) -> Recorder:
        self.stream.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stream.stop()
        self.stream.close()

    def now(self) -> int:
        with self.lock:
            return self.total

    def dropped(self, start: int, end: int) -> bool:
        """True if PortAudio lost input inside [start, end): such a take has a hole."""
        with self.lock:
            return any(start < gap < end for gap in self.gaps)

    def take(self, start: int, end: int) -> np.ndarray:
        with self.lock:
            blocks = list(self.blocks)
        parts = [blk[max(0, start - s):min(len(blk), end - s)]
                 for s, blk in blocks if s + len(blk) > start and s < end]
        return np.concatenate(parts) if parts else np.zeros((0, 2), dtype=np.float32)


def levels(audio: np.ndarray) -> list[tuple[float, float]]:
    """(RMS, peak) in dBFS for each channel."""
    def db(v: float) -> float:
        return 20 * np.log10(max(v, 1e-9))
    return [(db(float(np.sqrt(np.mean(c ** 2)))), db(float(np.max(np.abs(c)))))
            for c in audio.T] if audio.size else []


def level_warnings(audio: np.ndarray) -> list[str]:
    out = []
    for ch, (rms, peak) in zip(("throat (ch0)", "air (ch1)"), levels(audio)):
        if peak > -0.5:
            out.append(f"{ch} clipped: turn its input gain down and redo")
        elif rms < -60:
            out.append(f"{ch} is silent: is it connected and on this device?")
    return out


def channel_offset_ms(audio: np.ndarray, sr: int) -> float:
    """How far the throat channel lags the air channel, in ms (negative: it leads),
    from their shared voicing envelope. The app fuses a 2-channel capture without
    aligning it, which is right for one device clock and wrong for, say, a
    Bluetooth throat mic in an Aggregate Device, whose link adds latency."""
    throat = fusion.resample(audio[:, 0], sr)
    air = fusion.resample(audio[:, 1], sr)
    return 1000.0 * fusion.estimate_lag(throat, air, fusion.TARGET_SR, max_lag_ms=400) / fusion.TARGET_SR


def high_band_share(x: np.ndarray, sr: int, split_hz: float = 2000.0) -> float:
    """Share of a signal's energy above `split_hz`. A throat mic keeps little."""
    power = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    freqs = np.fft.rfftfreq(len(x), 1 / sr)
    total = float(power.sum())
    return float(power[freqs >= split_hz].sum()) / total if total > 0 else 0.0


# ── commands ───────────────────────────────────────────────────────────────
def cmd_devices(_args) -> None:
    sd = _sounddevice()
    devices = _input_devices(sd)
    if not devices:
        raise SystemExit("no input with 2 or more channels. Fusion needs throat + air on one "
                         "device: a 2-channel audio interface, or a macOS Aggregate Device")
    for i, d in devices:
        print(f"[{i}] {d['name']}  ({d['max_input_channels']} channels, "
              f"{int(d['default_samplerate'])} Hz)")


def cmd_check(args) -> None:
    sd = _sounddevice()
    dev = _pick_device(sd, args.device)
    sr = int(sd.query_devices(dev)["default_samplerate"])
    input("Speak normally for four seconds after Enter (any words) … ")
    audio = sd.rec(int(4 * sr), samplerate=sr, channels=2, device=dev, dtype="float32")
    sd.wait()
    shares = [high_band_share(audio[:, c], sr) for c in (0, 1)]
    for name, (rms, peak), share in zip(("ch0 (throat)", "ch1 (air)"), levels(audio), shares):
        print(f"  {name}: level {rms:.0f} dBFS, peak {peak:.1f} dBFS, "
              f"{100 * share:.0f}% of its energy above 2 kHz")
    for w in level_warnings(audio):
        print(f"  ! {w}")
    offset = channel_offset_ms(audio, sr)
    print(f"  the throat channel {'lags' if offset >= 0 else 'leads'} the air channel by "
          f"{abs(offset):.0f} ms")
    if shares[0] > shares[1]:
        print("  ! ch0 carries more high frequencies than ch1. Voxwire expects the throat mic on "
              "ch0 and the air mic on ch1: are they swapped?")
    elif abs(offset) > OFFSET_WARN_MS:
        print(f"  ! the channels are {abs(offset):.0f} ms apart. Fusion assumes one device clock "
              "(a 2-channel interface); a Bluetooth mic in an Aggregate Device adds its link "
              "latency. Scores from this setup describe this setup, not aligned hardware.")
    else:
        print("  ch0 looks like the throat mic and ch1 like the air mic, in step. Good to record.")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]


def take_name(text: str) -> str:
    """A file name for a phrase's take: readable, and unique to the exact text,
    so two phrases never share a file however alike they are."""
    return f"{_slug(text)[:40] or 'phrase'}-{hashlib.sha256(text.encode()).hexdigest()[:8]}"


def session_dir(name: str) -> Path:
    """recordings/eval/<name>. A plain name only, so a session can't land
    outside the git-ignored folder."""
    if not SESSION.fullmatch(name):
        raise SystemExit(f"--session takes a plain name (letters, digits, - and _), not {name!r}")
    return DATA / name


def run_takes(rec, todo: list[str], *, sr: int, commit, ask=input, say=print,
              first: int = 1, total: int | None = None) -> int:
    """Record each phrase in `todo` until a take of it is kept; return how many
    are left. A take is committed only once you keep it: Enter keeps it and goes
    on, q keeps it and stops, r throws it away. A take with dropped samples, or
    one too short to hold a phrase, is thrown away and asked for again.
    Interrupting at any prompt leaves that phrase unrecorded."""
    total = total or len(todo)
    i = 0
    while i < len(todo):
        text = todo[i]
        say(f"\n[{first + i}/{total}]  {text}")
        ask("  Enter, then read it … ")
        start = rec.now() - int(PRE_ROLL_S * sr)
        ask("  ● recording · Enter when done ")
        time.sleep(POST_ROLL_S)
        end = rec.now()
        audio = rec.take(start, end)
        if rec.dropped(start, end):
            say("  ! the input dropped samples during that take (the computer was busy); once more")
            continue
        if len(audio) < (PRE_ROLL_S + POST_ROLL_S + 0.2) * sr:
            say("  ! that was too short to hold a phrase; once more")
            continue
        for w in level_warnings(audio):
            say(f"  ! {w}")
        answer = ask("  Enter = keep · r = redo · q = keep and quit ").strip().lower()
        if answer == "r":
            continue
        commit(text, audio)
        i += 1
        if answer == "q":
            break
    return len(todo) - i


def cmd_record(args) -> None:
    session = session_dir(args.session)
    if not CONDITION.fullmatch(args.condition):
        raise SystemExit("--condition takes lowercase letters, digits and hyphens, e.g. quiet or noisy")
    sd = _sounddevice()
    dev = _pick_device(sd, args.device)
    info = sd.query_devices(dev)
    sr = int(info["default_samplerate"])
    (session / args.condition).mkdir(parents=True, exist_ok=True)
    manifest = session / "manifest.jsonl"

    phrases = load_phrases(Path(args.phrases))
    random.Random(f"{args.seed}:{args.condition}").shuffle(phrases)   # same order on resume
    done = {t["text"] for t in latest_takes(manifest) if t["condition"] == args.condition}
    todo = [p for p in phrases if p not in done]
    print(f"{info['name']} at {sr} Hz · condition '{args.condition}' · "
          f"{len(done)} of {len(phrases)} recorded, {len(todo)} to go.")
    print("Read each phrase as you'd dictate it. Redo only if you misread it or got "
          "interrupted, never because you think the mic misheard: that would bias the test.")

    def commit(text: str, audio: np.ndarray) -> None:
        rel = f"{args.condition}/{take_name(text)}.wav"
        sf.write(session / rel, audio, sr, subtype="FLOAT")
        with manifest.open("a") as f:
            f.write(json.dumps({"condition": args.condition, "text": text, "file": rel,
                                "sr": sr, "device": info["name"],
                                "recorded_at": datetime.now().isoformat(timespec="seconds")}) + "\n")

    with Recorder(sd, dev, sr) as rec:
        left = run_takes(rec, todo, sr=sr, commit=commit, first=len(done) + 1, total=len(phrases))
    print(f"\n{'Done' if not left else f'{left} left'}. Run the same command to "
          f"{'record another condition' if not left else 'resume'}; `score` when all are in.")


def cmd_score(args) -> None:
    session = session_dir(args.session)
    model = args.model or stt.default_model()
    if not model or not stt.model_available(model):
        raise SystemExit(f"speech-to-text model {model!r} can't run here; "
                         f"available: {', '.join(stt.available_models()) or 'none'}")
    print(f"Scoring session '{args.session}' with {model} …")
    rows = score_takes(session, model)
    summary = summarize(rows)
    takes = latest_takes(session / "manifest.jsonl")
    devices = sorted({t.get("device", "?") for t in takes})
    rates = sorted({t["sr"] for t in takes})
    recorded = (f"one speaker, recorded through {', '.join(devices)} "
                f"at {', '.join(f'{r} Hz' for r in rates)}")
    md = report(summary, model=model, label=stt.MODELS[model].label,
                session=args.session, recorded=recorded)
    (session / f"scores-{model}.json").write_text(json.dumps(
        {"model": model, "summary": summary, "rows": rows}, indent=2))
    print("\n" + md)
    print(f"Per-phrase transcripts: {session / f'scores-{model}.json'} (stays local)")
    if args.markdown:
        Path(args.markdown).write_text(md)
        print(f"Summary written to {args.markdown}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--session", default="main",
                   help="name for this set of recordings (default: main)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices", help="list inputs with 2 or more channels").set_defaults(fn=cmd_devices)
    c = sub.add_parser("check", help="four seconds of speech: levels, and is ch0 the throat?")
    c.add_argument("--device", required=True, help="index or part of the name")
    c.set_defaults(fn=cmd_check)
    r = sub.add_parser("record", help="read the phrases aloud, one take each (resumable)")
    r.add_argument("--device", required=True, help="index or part of the name")
    r.add_argument("--condition", required=True, help="e.g. quiet or noisy")
    r.add_argument("--phrases", default=str(PHRASES))
    r.add_argument("--seed", default="151", help="shuffles the phrase order per condition")
    r.set_defaults(fn=cmd_record)
    s = sub.add_parser("score", help="transcribe every take in every mode and report WER")
    s.add_argument("--model", help="speech-to-text model key (default: this machine's default)")
    s.add_argument("--markdown", help="also write the summary to this file")
    s.set_defaults(fn=cmd_score)
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        print("\nstopped. Run the same command to pick up where you left off.")


if __name__ == "__main__":
    main()
