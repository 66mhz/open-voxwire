# /// script
# requires-python = ">=3.12"
# dependencies = ["matplotlib", "numpy", "pillow", "scipy", "soundfile"]
# ///
"""Regenerate the README images in docs/images/.

    uv run docs/images/make_images.py                     # everything (macOS)
    uv run docs/images/make_images.py --only spectrogram  # just the spectrogram
    uv run docs/images/make_images.py --only spectrogram --out /tmp/look \
        --wav clip.wav --phrase "what it says" --mark 0.9 1.1   # your own clip, one span boxed

spectrogram  fusion-{light,dark}.png. One phrase three ways, through the repo's own
             voxwire/fusion.py. Without --wav it synthesizes the phrase with macOS
             `say`. The "throat" channel is that voice low-passed at 1 kHz (roughly
             what a throat mic keeps); the "air" channel is the full voice plus faint
             room noise. It is an illustration, not a recording. Only the built-in
             clip gets the boxed /s/ and what each channel did to it: a clip of your
             own gets its words in the footer, and a plain box where you --mark one.
diagrams     pipeline-{light,dark}.png and fusion-diagram-{light,dark}.png, the two
             figures from docs/how-voxwire-works.html rendered with headless Chrome.
ui           web-config.png. The real web UI served by voxwire/server.py, with a
             demo device list so no real device names end up in the repo, in a
             browser-window frame.
art          <name>-{light,dark}.png for every docs/images/src/<name>.html: original
             illustrations (SVG in HTML, styled by src/style.css), one per theme.
             Just one: --only art --art <name>.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
APP = ROOT / "voxwire"
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
PYTHON = APP / ".venv" / "bin" / "python"   # the project's env, which runs the demo server
READY_TIMEOUT_S = 20
PHRASE = "please stop the tests and list the files"
TESTS_S = (0.88, 1.04)          # where the final /s/ of "tests" sits in the synthesized clip
PAD_S = 0.35                    # silence added around the phrase, so the gate has room to show

# Palette: the project's own tokens (docs/how-voxwire-works.html), one set per theme.
THEMES = {
    "light": {"bg": "#ffffff", "ink": "#1f2328", "muted": "#59636e",
              "throat": "#b9740f", "air": "#0d8395", "fused": "#6244e6"},
    "dark": {"bg": "#0d1117", "ink": "#e6edf3", "muted": "#9198a1",
             "throat": "#edac4c", "air": "#49c4d6", "fused": "#9d8dff"},
}


# ── spectrogram: one phrase, three ways ────────────────────────────────────
def _synthesize(path: Path) -> None:
    subprocess.run(["say", "-o", str(path), "--file-format=WAVE",
                    "--data-format=LEI16@16000", PHRASE], check=True)


def _signals(wav: Path):
    import numpy as np
    import scipy.signal as sps
    import soundfile as sf

    sys.path.insert(0, str(APP))
    import fusion

    x, sr = sf.read(wav, dtype="float32")
    x = x if x.ndim == 1 else x[:, 0]
    x = fusion.resample(x, sr, fusion.TARGET_SR)
    sr = fusion.TARGET_SR
    pad = np.zeros(int(PAD_S * sr), dtype=np.float32)
    x = np.concatenate([pad, x, pad])
    b, a = sps.butter(6, 1000 / (sr / 2), btype="low")
    throat = sps.lfilter(b, a, x).astype(np.float32)
    rng = np.random.default_rng(7)
    air = (x + 0.006 * rng.standard_normal(len(x))).astype(np.float32)
    fused = fusion.fuse(throat, air, sr, mode="fusion", do_align=False)
    return sr, fusion.normalize(throat), fusion.normalize(air), fused


def spectrogram(out: Path, wav: Path | None, phrase: str = PHRASE,
                mark: tuple[float, float] | None = TESTS_S) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import scipy.signal as sps
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.patches import FancyBboxPatch

    # Only the built-in clip has been checked: where its /s/ sits, and what each channel did
    # to it. A clip of your own gets no verdicts.
    builtin = wav is None
    source = "synthesized voice" if builtin else wav.name
    with tempfile.TemporaryDirectory() as tmp:
        if builtin:
            wav = Path(tmp) / "phrase.wav"
            _synthesize(wav)
        sr, throat, air, fused = _signals(wav)

    rows = [("throat", throat, "Throat mic", "noise-immune, but the hiss of s, t, f, k never reaches it", "gone"),
            ("air", air, "Air mic", "hears the consonants, and the room noise with them", "there, plus room noise"),
            ("fused", fused, "Voxwire fusion", "voice from the throat, consonants from the air, only while you speak", "kept")]
    specs = []
    for _, sig, *_ in rows:
        f, t, z = sps.stft(sig, fs=sr, nperseg=512, noverlap=448)
        specs.append((f, t, 20 * np.log10(np.abs(z) + 1e-6)))
    top = max(s.max() for _, _, s in specs)
    lo = top - 56

    plt.rcParams["font.family"] = ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"]
    for name, th in THEMES.items():
        fig, axes = plt.subplots(3, 1, figsize=(8.6, 5.9), dpi=180, sharex=True)
        fig.patch.set_alpha(0)
        for ax, (key, _, title, note, verdict), (f, t, s) in zip(axes, rows, specs):
            cmap = LinearSegmentedColormap.from_list(key, [th["bg"], th[key]])
            ax.pcolormesh(t, f / 1000, np.clip(s, lo, top), cmap=cmap, vmin=lo, vmax=top,
                          shading="gouraud", rasterized=True)
            ax.set_ylim(0, 8)
            ax.set_facecolor(th["bg"])
            ax.set_yticks([0, 4, 8])
            ax.tick_params(colors=th["muted"], labelsize=7.5, length=0, pad=4)
            for side in ax.spines.values():
                side.set_visible(False)
            ax.set_ylabel("kHz", color=th["muted"], fontsize=7.5, labelpad=4)
            head = ax.text(0.0, 1.06, title, transform=ax.transAxes, color=th["ink"],
                           fontsize=10.5, fontweight="bold", va="bottom")
            ax.annotate(note, xy=(1, 0), xycoords=head, xytext=(7, 0), textcoords="offset points",
                        color=th["muted"], fontsize=8.5, va="bottom")
            if mark:
                x0, x1 = mark[0] + PAD_S, mark[1] + PAD_S
                ax.add_patch(FancyBboxPatch((x0, 3.6), x1 - x0, 4.1, boxstyle="round,pad=0,rounding_size=0.03",
                                            mutation_aspect=40, fill=False, lw=1.3, ls=(0, (3, 2)),
                                            ec=th["ink"], alpha=0.85))
                if builtin:
                    ax.text(x1 + 0.02, 6.9, f"'s' in tests: {verdict}", color=th["ink"], fontsize=8, va="center")
        axes[-1].set_xlabel("seconds", color=th["muted"], fontsize=7.5, labelpad=3)
        fig.text(0.0, 0.004, f'"{phrase}": {source}, throat channel simulated with a 1 kHz low-pass',
                 color=th["muted"], fontsize=7)
        fig.subplots_adjust(left=0.06, right=0.995, top=0.95, bottom=0.085, hspace=0.42)
        path = out / f"fusion-{name}.png"
        fig.savefig(path, transparent=True)
        plt.close(fig)
        _quantize(path)
        print("wrote", _shown(path))


def _palette(img, flat: bool):
    """The image as 256 colors. The octree suits smooth ramps such as the spectrograms, but it
    shifts the large flat fills of an illustration. So for flat art the palette is the image's
    200 most frequent colors, kept exactly, plus 56 octree colors for the anti-aliasing shades
    left over, and each pixel takes its nearest entry by premultiplied color, so a transparent
    pixel matches on alpha."""
    import numpy as np
    from PIL import Image

    if not flat:
        return img.quantize(colors=256, method=Image.Quantize.FASTOCTREE)
    rgba = np.asarray(img)
    keys = rgba.reshape(-1, 4).copy().view(np.uint32).ravel()
    uniq, inverse, counts = np.unique(keys, return_inverse=True, return_counts=True)
    colors = uniq.view(np.uint8).reshape(-1, 4)
    order = np.argsort(-counts)
    pal, rest = colors[order[:200]], order[200:]
    if len(rest):
        shades = np.repeat(colors[rest], np.minimum(counts[rest], 64), axis=0)
        fit = Image.fromarray(shades[None], "RGBA").quantize(56, method=Image.Quantize.FASTOCTREE)
        pal = np.unique(np.concatenate([pal, np.asarray(fit.convert("RGBA")).reshape(-1, 4)]), axis=0)

    def premultiplied(c):
        c = c.astype(float)
        return np.concatenate([c[:, :3] * c[:, 3:] / 255, c[:, 3:]], axis=1)

    entries, wanted = premultiplied(pal), premultiplied(colors)
    nearest = np.concatenate([((wanted[i:i + 4096, None] - entries[None]) ** 2).sum(axis=2).argmin(axis=1)
                              for i in range(0, len(wanted), 4096)])
    out = Image.fromarray(nearest[inverse].reshape(rgba.shape[:2]).astype(np.uint8), "P")
    out.putpalette(pal[:, :3].ravel().tolist())
    out.info["transparency"] = bytes(pal[:, 3].tolist())
    return out


def _shown(path: Path) -> Path:
    """A path for the log: relative to the repo when it's inside it."""
    return path.relative_to(ROOT) if path.is_relative_to(ROOT) else path


def _intact(before, after) -> bool:
    """Whether a palette version keeps an image's transparency, given both alpha channels:
    fully transparent pixels stay fully transparent, and no more than 1 pixel in 10,000 (a
    stray anti-aliased edge) has its alpha move by more than 32. The octree can fold a soft
    shadow into its background and leave a faint box behind; this catches it."""
    import numpy as np

    return not (after[before == 0] != 0).any() and np.percentile(np.abs(after - before), 99.99) <= 32


def _quantize(path: Path, flat: bool = False) -> None:
    """Rewrite a PNG as a palette PNG (see _palette) when that keeps its transparency intact
    (see _intact) and makes it smaller."""
    import io

    import numpy as np
    from PIL import Image

    img = Image.open(path).convert("RGBA")
    out = _palette(img, flat)
    before = np.asarray(img)[..., 3].astype(int)
    after = np.asarray(out.convert("RGBA"))[..., 3].astype(int)
    if not _intact(before, after):
        return
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    if buf.tell() < path.stat().st_size:
        path.write_bytes(buf.getvalue())


# ── diagrams: the explainer's two figures ──────────────────────────────────
def _chrome(url: str, png: Path, width: int, height: int, scale: float = 2) -> None:
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    f"--force-device-scale-factor={scale}", "--default-background-color=00000000",
                    "--virtual-time-budget=4000", f"--window-size={width},{height}",
                    f"--screenshot={png}", url], check=True, capture_output=True)


def diagrams(out: Path) -> None:
    src = (ROOT / "docs" / "how-voxwire-works.html").read_text()
    style = re.search(r"<style>.*?</style>", src, re.S).group(0)
    fonts = re.search(r'<link rel="stylesheet" href="https://fonts[^>]+>', src).group(0)
    figures = re.findall(r"<figure>.*?</figure>", src, re.S)
    for base, fig in zip(("pipeline", "fusion-diagram"), figures):
        svg = re.search(r"<svg.*?</svg>", fig, re.S).group(0)
        vb = [float(v) for v in re.search(r'viewBox="([^"]+)"', svg).group(1).split()]
        width = 760 if base == "pipeline" else 880
        height = round(width * vb[3] / vb[2])
        for theme in ("light", "dark"):
            page = (f'<!doctype html><html data-theme="{theme}"><head><meta charset="utf-8">{fonts}{style}'
                    f"<style>html,body{{background:transparent!important;margin:0}}"
                    f"svg{{display:block;width:{width}px;height:{height}px}}</style></head>"
                    f"<body>{svg}</body></html>")
            with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
                fh.write(page)
            try:
                path = out / f"{base}-{theme}.png"
                _chrome(Path(fh.name).as_uri(), path, width, height)
                _quantize(path, flat=True)
                print("wrote", _shown(path))
            finally:
                os.unlink(fh.name)


# ── art: original illustrations in docs/images/src/ ───────────────────────
def art(out: Path, only: str | None = None) -> None:
    src = HERE / "src"
    css = (src / "style.css").read_text()
    for page in sorted(src.glob("*.html")):
        if only and page.stem != only:
            continue
        text = page.read_text().replace('<link rel="stylesheet" href="style.css">', f"<style>{css}</style>")
        m = re.search(r'<svg[^>]*\bwidth="(\d+)"[^>]*\bheight="(\d+)"', text)
        width, height = int(m.group(1)), int(m.group(2))
        for theme in ("light", "dark"):
            themed = re.sub(r'<html data-theme="[^"]*">', f'<html data-theme="{theme}">', text, count=1)
            with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
                fh.write(themed)
            try:
                path = out / f"{page.stem}-{theme}.png"
                _chrome(Path(fh.name).as_uri(), path, width, height)
                _quantize(path, flat=True)
                print("wrote", _shown(path))
            finally:
                os.unlink(fh.name)


# ── ui: the web config with demo devices ───────────────────────────────────
DEMO_SERVER = r'''
import os, sys, tempfile
os.environ["VOXWIRE_NO_IDLE_WATCH"] = "1"
sys.path.insert(0, sys.argv[1])
import uvicorn, server
from pathlib import Path
demo = Path(tempfile.mkdtemp())
server.RECORDINGS_DIR = demo / "recordings"
server.RECORDINGS_DIR.mkdir()
# Serve a copy of the page with Advanced settings open, so the shot shows them.
page = (server.HERE / "index.html").read_text().replace('<details class="adv"', '<details class="adv" open', 1)
(demo / "index.html").write_text(page)
server.HERE = demo
server._warm["device"] = 2          # the demo Aggregate Device: dual-mic available
# A configured server, so the page adopts this config instead of racing to seed its own.
server._dict.update(device=2, channels=2, configured=True)
server.list_inputs = lambda: [
    {"index": 0, "name": "MacBook Microphone", "samplerate": 48000, "channels": 1, "tags": [], "default": True},
    {"index": 1, "name": "Stealth throat mic", "samplerate": 16000, "channels": 1, "tags": ["IASUS", "BT"], "default": False},
    {"index": 2, "name": "Throat + Air (Aggregate Device)", "samplerate": 48000, "channels": 2, "tags": [], "default": False},
]
server.bt_status = lambda: {"available": False}
# The fixup line names its tier, not a model ID (those live only in tiers.py), and neither
# it nor the STT list depends on what this machine has downloaded.
server.llm_info = lambda: {"mode": "local", "model": "fast tier", "available": True,
                           "state": "downloaded", "cached": True}
server.hf_cached = lambda repo: True
server._check_perms = lambda: {"applicable": True, "input_monitoring": True, "accessibility": True}
uvicorn.run(server.app, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
'''


# The live page goes into a browser window, drawn dark like the UI itself, with an edge that
# shows on a light page and on a dark one.
FRAME = """<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; background: transparent; }}
.win {{ margin: 18px 30px 46px; border: 1px solid #454c55; border-radius: 12px; overflow: hidden;
        background: #0d1013; box-shadow: 0 22px 44px rgba(0, 0, 0, .28), 0 4px 10px rgba(0, 0, 0, .12); }}
.bar {{ position: relative; display: flex; align-items: center; gap: 8px; height: 38px; padding: 0 14px;
        background: #1c2128; border-bottom: 1px solid #2d333b; }}
.bar i {{ display: block; width: 12px; height: 12px; border-radius: 50%; }}
.url {{ position: absolute; left: 50%; transform: translateX(-50%); width: 280px; line-height: 24px;
        border-radius: 7px; background: #0d1117; color: #9198a1; text-align: center;
        font: 13px -apple-system, system-ui, sans-serif; }}
iframe {{ display: block; width: {width}px; height: {height}px; border: 0; }}
</style></head><body><div class="win"><div class="bar"><i style="background:#ff5f57"></i>
<i style="background:#febc2e"></i><i style="background:#28c840"></i><div class="url">127.0.0.1:8123</div></div>
<iframe src="{src}"></iframe></div></body></html>"""


def ui(out: Path) -> None:
    if not PYTHON.exists():
        sys.exit(f"{_shown(PYTHON)} not found: run ./scripts/install.sh first")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([str(PYTHON), "-c", DEMO_SERVER, str(APP), str(port)])
    width, height = 980, 1236
    try:
        # Never capture a server that isn't up: Chrome would save its error page as the shot.
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            if proc.poll() is not None:
                sys.exit(f"the demo server exited with code {proc.returncode} before it answered")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/fixup", timeout=1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    sys.exit(f"the demo server didn't answer within {READY_TIMEOUT_S} s")
                time.sleep(0.2)
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
            fh.write(FRAME.format(width=width, height=height, src=f"http://127.0.0.1:{port}/"))
        try:
            path = out / "web-config.png"
            # Shown at 640 px in the README; this scale gives it about twice that.
            _chrome(Path(fh.name).as_uri(), path, width + 62, height + 105, scale=1.35)
            _quantize(path, flat=True)
            print("wrote", _shown(path))
        finally:
            os.unlink(fh.name)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", choices=("spectrogram", "diagrams", "ui", "art"))
    p.add_argument("--art", help="with --only art: render just this src/<name>.html")
    p.add_argument("--wav", type=Path, help="speech clip for the spectrogram (default: macOS say)")
    p.add_argument("--phrase", help="with --wav: what the clip says, for the figure's footer")
    p.add_argument("--mark", nargs=2, type=float, metavar=("START", "END"),
                   help="with --wav: seconds into the clip to box, such as a consonant")
    p.add_argument("--out", type=Path, default=HERE, help="where to write the images (default: docs/images/)")
    args = p.parse_args()
    if args.wav is not None and not args.phrase:
        p.error("--wav needs --phrase: what the clip says, for the figure's footer")
    if args.wav is None and (args.phrase or args.mark):
        p.error("--phrase and --mark describe a --wav clip")
    if args.only in (None, "spectrogram") and args.wav is None and not shutil.which("say"):
        sys.exit("no --wav given and macOS `say` is not available")
    args.out.mkdir(parents=True, exist_ok=True)
    if args.only in (None, "spectrogram"):
        if args.wav is None:
            spectrogram(args.out, None)
        else:
            spectrogram(args.out, args.wav, args.phrase, tuple(args.mark) if args.mark else None)
    if args.only in (None, "diagrams"):
        diagrams(args.out)
    if args.only in (None, "ui"):
        ui(args.out)
    if args.only in (None, "art"):
        art(args.out, args.art)


if __name__ == "__main__":
    main()
