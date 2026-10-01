#!/usr/bin/env bash
# Launch Voxwire (STT + dictation server). Creates the py3.12 venv on first run.
set -e
cd "$(dirname "$0")"

# venv interpreter: POSIX layout is .venv/bin/python; native Windows (Git Bash /
# MSYS) puts it at .venv/Scripts/python.exe. Resolve it each call so run.sh works
# under Git Bash and WSL as well as macOS/Linux.
py() { [ -x .venv/Scripts/python.exe ] && echo .venv/Scripts/python.exe || echo .venv/bin/python; }

if [ ! -x "$(py)" ]; then
  echo "→ first run: creating py3.12 venv + installing the STT stack (uv)…"
  uv venv --python 3.12 .venv
  # faster-whisper = cross-platform STT (CPU/CUDA); pyperclip = clipboard fallback
  # used by the platform layer (voxwire/osplatform/). MLX = Apple-Silicon-only STT
  # (mlx-whisper/parakeet) + on-device fixup LLM (mlx-lm); install it only there —
  # pip has no MLX wheel elsewhere, which would fail the whole transaction and
  # leave no env. See voxwire/stt/.
  MLX=""
  [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] && MLX="mlx-whisper parakeet-mlx mlx-lm"
  uv pip install --python "$(py)" \
    $MLX faster-whisper \
    sounddevice numpy scipy soundfile fastapi "uvicorn[standard]" pynput pyperclip
fi

# Apple Silicon envs made before on-device fixup existed lack mlx-lm; add it on
# demand so fixup isn't stuck at "unavailable". find_spec keeps the check cheap.
if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
  "$(py)" -c "import importlib.util as u, sys; sys.exit(u.find_spec('mlx_lm') is None)" || \
    uv pip install --python "$(py)" mlx-lm >/dev/null || \
    echo "  note: couldn't install mlx-lm, so on-device fixup stays off (re-run scripts/install.sh)"
fi

if [ "$1" = "app" ]; then
  # Native app that runs the server in-process (no browser tab). The platform
  # layer picks the UI per OS: the rumps menubar on macOS, the pystray system
  # tray on Windows/Linux. GUI deps are installed on demand.
  if [ "$(uname -s)" = "Darwin" ]; then
    "$(py)" -c "import rumps" 2>/dev/null || \
      uv pip install --python "$(py)" rumps >/dev/null
    echo "→ Voxwire menubar app (look for 🎙 in the menubar)"
    exec "$(py)" menubar.py
  else
    "$(py)" -c "import pystray, PIL" 2>/dev/null || \
      uv pip install --python "$(py)" pystray pillow >/dev/null
    echo "→ Voxwire tray app (look for the Voxwire dot in the system tray)"
    exec "$(py)" tray.py
  fi
fi

echo "→ open http://127.0.0.1:8123  (or: ./run.sh app  for the menubar app)"
exec "$(py)" server.py
