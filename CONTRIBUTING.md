# Contributing to Voxwire

Thanks for your interest in Voxwire — a throat-mic-native, local-first voice agent.
This guide covers how to set up, the conventions that keep the project safe and
flexible, and how to get a change merged.

By participating you agree to keep interactions respectful and constructive.

## Development setup

Requires Python **3.12** and [`uv`](https://docs.astral.sh/uv/). macOS + Apple
Silicon gets the MLX stack; Windows/Linux/Intel Macs use the faster-whisper
backend.

```bash
git clone <your-fork> voxwire && cd voxwire
./scripts/install.sh                                   # runtime env + deps
uv pip install --python voxwire/.venv/bin/python -e ".[dev]"   # pytest + ruff
```

The `voxwire/.venv/bin/` paths in this guide are for macOS/Linux. On native
Windows (Git Bash), the venv interpreter and tools live under
`voxwire/.venv/Scripts/` instead — e.g. `voxwire/.venv/Scripts/python.exe` and
`voxwire/.venv/Scripts/ruff.exe`.

Run the app:

```bash
cd voxwire && ./run.sh app    # native menubar app (macOS)
cd voxwire && ./run.sh        # headless server + web config at http://127.0.0.1:8123
```

## Tests and lint (must pass)

```bash
voxwire/.venv/bin/python -m pytest      # from the repo root
voxwire/.venv/bin/ruff check .
./scripts/leakcheck.sh                  # no private/secret-shaped content
```

CI runs the same three (plus a gitleaks secret scan) on every PR — see
`.github/workflows/ci.yml`. Add real tests for new behavior; report actual
results and never claim green if red.

## Project conventions

A few rules are load-bearing. PRs that break them won't merge:

- **No private or secret content.** Voxwire is a public project. Never commit
  secrets, keys, tokens, private hostnames, or IP ranges. Run
  `./scripts/leakcheck.sh` before every commit and keep it clean.
- **Integrations are plugins — never hardcoded.** Nothing service-specific belongs
  in `gateway.py`. To add one, subclass `Integration` in a new module under
  `voxwire/integrations/`, declare its `wake_words` + `handle()`, and call
  `register(...)`. Copy `voxwire/integrations/echo.py` to start. A plugin can also live
  outside the repo: declare it in your package's `voxwire.integrations` entry points, or
  put it in a folder named by `VOXWIRE_PLUGIN_PATH`. Anything
  destructive must return `Result(requires_confirmation=True, preview=...)` and
  let the executor own the gate — an integration must never act on a gated
  command itself.
- **STT engines are plugins too.** A speech-to-text backend implements
  `is_available()` + `transcribe()` in a `*_backend.py` under `voxwire/stt/` and
  registers itself. Keep the heavy runtime import **lazy** so importing the module
  on the "wrong" OS is harmless.
- **Model IDs live in one table.** STT model IDs belong in `voxwire/stt/models.py`;
  no engine or model name should appear elsewhere in the code.
- **The security gate is never weakened to ship a feature.** Confirmation is owned
  by the executor, deny-by-default (see `SECURITY.md` and `docs/DESIGN.md §3`).
- **LLM fixup stays faithful.** It fixes mishearings only — it must never invent,
  expand, or "reconstruct" a command, skips very short inputs, and stays off by
  default.

Match the surrounding style; update `docs/DESIGN.md` if you change the
architecture.

## Docs images

The README's PNGs are generated; don't edit them by hand. The illustrations are
SVG in `docs/images/src/*.html`, styled by `docs/images/src/style.css`, and
`docs/images/make_images.py` renders each one in a light and a dark variant. It
also renders the spectrogram through the real `voxwire/fusion.py` and captures the
web UI with demo devices. It drives headless Google Chrome (set `CHROME` if yours
isn't in `/Applications`), and it voices the spectrogram's phrase with macOS `say`
unless you pass `--wav`:

```bash
uv run docs/images/make_images.py                        # everything
uv run docs/images/make_images.py --only art --art hero  # one illustration
uv run docs/images/make_images.py --out /tmp/look        # somewhere else, to compare first
```

The feature icons in `docs/images/icons/` are plain SVG files. Edit them directly.

Photos and screen recordings come in through `docs/images/add_media.py`. It turns a
photo upright and keeps only its pixels: a phone photo carries the GPS position it
was taken at, the phone's model and the time. A test fails on any image in `docs/`
that still has EXIF, XMP or IPTC metadata.

```bash
uv run docs/images/add_media.py photo ~/Downloads/IMG_1234.HEIC throat-mic-photo
uv run docs/images/add_media.py gif ~/Desktop/demo.mov demo --start 1.5 --duration 9
```

## Pull requests

1. Branch off `main`.
2. Keep the change focused; write a clear description of what and why.
3. Make sure pytest, ruff, and the leak check are green locally.
4. Open the PR — CI must pass before review.

Small, well-tested PRs get reviewed fastest. If you're planning something large,
open an issue first so we can align on the approach.
