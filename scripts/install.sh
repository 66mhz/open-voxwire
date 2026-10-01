#!/usr/bin/env bash
# Voxwire — single-click install. Creates the env and installs dependencies.
# Requires: Python 3.12 (via uv). macOS adds the MLX + menubar stack (+ Homebrew
# ffmpeg); Linux/Windows use the faster-whisper backend.
set -e
here="$(cd "$(dirname "$0")/.." && pwd)"
app="$here/voxwire"

echo "→ Voxwire install"

# 1. uv (fast Python env manager)
if ! command -v uv >/dev/null 2>&1; then
  echo "  installing uv…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# 2. ffmpeg — only the MLX/parakeet path (macOS) needs it; faster-whisper decodes
# via bundled PyAV, so it's skipped off macOS (where brew wouldn't exist anyway).
if [ "$(uname -s)" = "Darwin" ] && ! command -v ffmpeg >/dev/null 2>&1; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "✗ Voxwire needs ffmpeg on macOS, and installs it with Homebrew, which isn't here." >&2
    echo "  Install Homebrew (https://brew.sh) or ffmpeg, then run this again." >&2
    exit 1
  fi
  echo "  installing ffmpeg (brew)…"
  brew install ffmpeg
fi

# 3. python env beside the app (so ./run.sh finds .venv)
echo "  creating py3.12 env…"
uv venv --python 3.12 "$app/.venv"
# venv interpreter: POSIX = bin/python; native Windows (Git Bash/MSYS) = Scripts/python.exe.
PY="$app/.venv/bin/python"; [ -x "$app/.venv/Scripts/python.exe" ] && PY="$app/.venv/Scripts/python.exe"
# faster-whisper = cross-platform STT (CPU/CUDA), the default off Apple Silicon.
# pyperclip + pystray + pillow = the cross-platform clipboard fallback + tray UI
# (voxwire/osplatform/, voxwire/tray.py). MLX = Apple-Silicon-only STT; rumps =
# macOS-only menubar UI — install the mac-only wheels only on macOS so a
# Linux/Windows setup doesn't fail the install. httpx (remote LLM fixup) and
# mlx-lm (on-device fixup) are what server.correct_command imports; websockets is
# what scripts/stream_pcm.py imports. This list
# must cover pyproject.toml's core deps + the mlx extra (tests/test_packaging.py).
mac_pkgs=""
[ "$(uname -s)" = "Darwin" ] && mac_pkgs="rumps"
[ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] && mac_pkgs="mlx-whisper parakeet-mlx mlx-lm $mac_pkgs"
uv pip install --python "$PY" \
  $mac_pkgs faster-whisper pyperclip pystray pillow \
  sounddevice numpy scipy soundfile \
  fastapi "uvicorn[standard]" pynput httpx websockets

# 4. A first run records through PortAudio. The macOS and Windows sounddevice
# wheels bundle it; Linux needs the system library, so say how to get it here
# rather than fail later at startup.
if ! "$PY" -c "import sounddevice" >/dev/null 2>&1; then
  echo "✗ PortAudio, the audio library Voxwire records through, is missing." >&2
  echo "  Debian/Ubuntu: sudo apt-get install libportaudio2" >&2
  echo "  Fedora:        sudo dnf install portaudio" >&2
  echo "  Arch:          sudo pacman -S portaudio" >&2
  echo "  Then run this again." >&2
  exit 1
fi

echo
echo "✓ installed. Next:"
echo "    cd voxwire && ./run.sh app     # native menubar app (recommended)"
echo "    cd voxwire && ./run.sh         # headless + web config on :8123"
echo
echo "  You'll be asked to grant macOS Accessibility + Input Monitoring the"
echo "  first time you enable dictation — that's what lets the hotkey + paste work."
