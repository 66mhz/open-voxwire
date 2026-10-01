"""Tests for scripts/install.sh on machines that are missing something.

A fresh machine is where an install breaks: a Mac without Homebrew, a Linux box
without PortAudio. The script should stop there with the fix, not leave an
install that fails at first run. Each test runs a copy of the script in a
throwaway tree with stub `uname`, `uv` and (when the test wants one) `brew`
first on PATH. The `uv` stub creates a venv whose python fails
`import sounddevice` when STUB_PORTAUDIO=missing, as it does without PortAudio.
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install.sh"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

UNAME = """#!/usr/bin/env bash
case "$1" in -m) echo "${STUB_ARCH:-arm64}" ;; *) echo "${STUB_OS:-Darwin}" ;; esac
"""

UV = """#!/usr/bin/env bash
echo "uv $*" >> "$STUB_STATE/calls.log"
if [ "$1" = venv ]; then
  venv="${@: -1}"
  mkdir -p "$venv/bin"
  cat > "$venv/bin/python" <<'PY'
#!/usr/bin/env bash
if [ "$1" = -c ] && [ "$2" = "import sounddevice" ] && [ "${STUB_PORTAUDIO:-}" = missing ]; then
  echo "OSError: PortAudio library not found" >&2; exit 1
fi
exit 0
PY
  chmod +x "$venv/bin/python"
fi
"""

BREW = """#!/usr/bin/env bash
echo "brew $*" >> "$STUB_STATE/calls.log"
"""


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def run_install(tmp_path):
    if shutil.which("ffmpeg", path=SYSTEM_PATH):
        pytest.skip("this machine has ffmpeg in a system dir, so 'no ffmpeg' can't be staged")
    repo, bin_, state = tmp_path / "repo", tmp_path / "bin", tmp_path / "state"
    for d in (repo / "scripts", repo / "voxwire", bin_, state):
        d.mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts" / "install.sh")
    _exe(bin_ / "uname", UNAME)
    _exe(bin_ / "uv", UV)
    (state / "calls.log").touch()

    def run(*, brew: bool = False, **env: str):
        if brew:
            _exe(bin_ / "brew", BREW)
        full = {"HOME": str(tmp_path), "PATH": f"{bin_}:{SYSTEM_PATH}",
                "STUB_STATE": str(state), **env}
        proc = subprocess.run(["bash", str(repo / "scripts" / "install.sh")],
                              capture_output=True, text=True, env=full, timeout=30)
        return proc, (state / "calls.log").read_text()
    return run


def test_a_mac_without_homebrew_is_told_how_to_get_ffmpeg(run_install):
    proc, calls = run_install(STUB_OS="Darwin")
    assert proc.returncode == 1
    assert "Homebrew" in proc.stderr and "https://brew.sh" in proc.stderr
    assert "uv venv" not in calls                       # stopped before touching anything


def test_a_mac_with_homebrew_gets_ffmpeg_and_the_mlx_stack(run_install):
    proc, calls = run_install(brew=True, STUB_OS="Darwin", STUB_ARCH="arm64")
    assert proc.returncode == 0, proc.stderr
    assert "brew install ffmpeg" in calls
    assert "parakeet-mlx" in calls and "rumps" in calls
    assert "✓ installed" in proc.stdout


def test_linux_without_portaudio_is_told_which_package_to_install(run_install):
    proc, _ = run_install(STUB_OS="Linux", STUB_ARCH="x86_64", STUB_PORTAUDIO="missing")
    assert proc.returncode == 1
    assert "PortAudio" in proc.stderr and "libportaudio2" in proc.stderr
    assert "✓ installed" not in proc.stdout


def test_linux_with_portaudio_installs_without_the_mac_stack(run_install):
    proc, calls = run_install(STUB_OS="Linux", STUB_ARCH="x86_64")
    assert proc.returncode == 0, proc.stderr
    assert "faster-whisper" in calls
    assert "parakeet-mlx" not in calls and "rumps" not in calls and "brew" not in calls
    assert "✓ installed" in proc.stdout


@pytest.mark.skipif(os.name == "nt", reason="bash script")
def test_the_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
