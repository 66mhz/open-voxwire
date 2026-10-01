"""Tests for scripts/service-macos.sh.

The script drives launchd, so each test runs a copy of it inside a throwaway
tree with stub `launchctl`, `plutil`, `uname`, `curl`, `sleep` and `uv` first
on PATH. The launchctl stub keeps the loaded jobs in a state dir and logs every
call, so the tests can assert what the script asked launchd to do. It can also
misbehave on request: STUB_STUCK lists labels whose bootout fails and leaves the
job loaded, STUB_BOOTOUT_NOISY makes bootout exit non-zero after unloading, and
STUB_BOOTSTRAP=fail|fail-once makes bootstrap fail; STUB_PLUTIL=fail rejects
every plist. Runs on macOS and in Linux
CI alike (plutil validates for real when it exists).
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "service-macos.sh"
UID = os.getuid()

STUBS = {
    "launchctl": r"""#!/usr/bin/env bash
echo "launchctl $*" >> "$STUB_STATE/calls.log"
job() { echo "$STUB_STATE/loaded.${1##*/}"; }
case "$1" in
  print)     [ -f "$(job "$2")" ] || exit 113
             printf '\tstate = running\n\tpid = 4242\n' ;;
  bootstrap) label=$(sed -n 's:.*<key>Label</key><string>\([^<]*\)</string>.*:\1:p' "$3")
             [ -n "$label" ] || exit 5
             [ ! -f "$STUB_STATE/loaded.$label" ] || exit 5     # already loaded
             case "${STUB_BOOTSTRAP:-works}" in
               fail) exit 5 ;;
               fail-once) [ -f "$STUB_STATE/bootstrap-failed" ] \
                            || { touch "$STUB_STATE/bootstrap-failed"; exit 5; } ;;
             esac
             touch "$STUB_STATE/loaded.$label" ;;
  bootout)   [ -f "$(job "$2")" ] || exit 3
             case " ${STUB_STUCK:-} " in *" ${2##*/} "*) exit 5 ;; esac
             rm -f "$(job "$2")"
             [ -z "${STUB_BOOTOUT_NOISY:-}" ] || exit 5 ;;
  kickstart) target="${@: -1}"; [ -f "$(job "$target")" ] || exit 113 ;;
esac
""",
    "plutil": """#!/usr/bin/env bash
[ "${STUB_PLUTIL:-}" = fail ] && exit 1
[ -x /usr/bin/plutil ] && exec /usr/bin/plutil "$@"
exit 0
""",
    "sleep": """#!/usr/bin/env bash
exit 0
""",
    "uname": """#!/usr/bin/env bash
echo "${STUB_UNAME:-Darwin}"
""",
    "curl": r"""#!/usr/bin/env bash
echo "curl $*" >> "$STUB_STATE/calls.log"
url="${@: -1}"
case "${STUB_HTTP:-down}" in
  voxwire) case "$url" in
             */api/dictation/status) echo '{"enabled": false, "hotkey": "alt+cmd"}' ;;
             *) echo '<html></html>' ;;
           esac ;;
  other)   case "$url" in
             */api/dictation/status) exit 22 ;;
             *) echo '<html>someone else</html>' ;;
           esac ;;
  *)       exit 7 ;;
esac
""",
}

UV_STUB = """#!/usr/bin/env bash
echo "uv $*" >> "$STUB_STATE/calls.log"
[ "${STUB_UV:-works}" = works ] || exit 1
touch "$STUB_STATE/rumps"
"""

# The venv python only needs to answer `-c "import rumps"`.
PY_STUB = """#!/usr/bin/env bash
[ "$1" = -c ] && [ "$2" = "import rumps" ] && { [ -f "$STUB_STATE/rumps" ]; exit $?; }
exit 0
"""


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Env:
    def __init__(self, root: Path, *, uv: bool, rumps: bool):
        self.root = root
        self.home = root / "home"
        self.state = root / "state"
        self.bin = root / "bin"
        self.repo = root / "repo & co"   # a path needing XML escaping in the plist
        for d in (self.home, self.state, self.bin, self.repo / "scripts",
                  self.repo / "voxwire" / ".venv" / "bin"):
            d.mkdir(parents=True, exist_ok=True)
        shutil.copy(SCRIPT, self.repo / "scripts" / "service-macos.sh")
        _exe(self.repo / "voxwire" / ".venv" / "bin" / "python", PY_STUB)
        for name, body in STUBS.items():
            _exe(self.bin / name, body)
        if uv:
            _exe(self.bin / "uv", UV_STUB)
        if rumps:
            (self.state / "rumps").touch()
        (self.state / "calls.log").touch()
        self.path = f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin"

    @property
    def plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / "io.voxwire.plist"

    @property
    def legacy_plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / "io.voxwire.agent.plist"

    def loaded(self, label: str) -> bool:
        return (self.state / f"loaded.{label}").exists()

    def calls(self) -> str:
        return (self.state / "calls.log").read_text()

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        full = {"HOME": str(self.home), "PATH": self.path, "STUB_STATE": str(self.state), **env}
        return subprocess.run(["bash", str(self.repo / "scripts" / "service-macos.sh"), *args],
                              capture_output=True, text=True, env=full, timeout=30)

    def leftovers(self) -> list[str]:
        """Staging files install must never leave in LaunchAgents."""
        return [p.name for p in self.plist.parent.glob(".io.voxwire.*")]

    def add_legacy_agent(self) -> None:
        self.legacy_plist.parent.mkdir(parents=True, exist_ok=True)
        self.legacy_plist.write_text(
            "<plist><dict><key>Label</key><string>io.voxwire.agent</string></dict></plist>\n")
        (self.state / "loaded.io.voxwire.agent").touch()


@pytest.fixture
def make_env(tmp_path):
    def make(*, uv: bool = True, rumps: bool = True) -> Env:
        env = Env(tmp_path, uv=uv, rumps=rumps)
        if not uv and shutil.which("uv", path=env.path):
            pytest.skip("a real uv is on the system PATH")
        return env
    return make


def test_install_writes_a_valid_plist_and_loads_it(make_env):
    env = make_env()
    res = env.run("install")
    assert res.returncode == 0, res.stderr
    text = env.plist.read_text()
    assert "<string>io.voxwire</string>" in text
    assert "menubar.py" in text and "repo &amp; co" in text
    assert env.loaded("io.voxwire")
    assert f"bootstrap gui/{UID} " in env.calls()
    # RunAtLoad already started it; a kickstart -k would kill the fresh process.
    assert "kickstart" not in env.calls()


def _log(env: Env) -> Path:
    return env.home / "Library" / "Logs" / "voxwire.log"


def test_install_precreates_an_owner_only_log(make_env):
    env = make_env()
    res = env.run("install")
    assert res.returncode == 0, res.stderr
    assert stat.S_IMODE(_log(env).stat().st_mode) == 0o600


def test_install_tightens_a_world_readable_log_from_an_older_install(make_env):
    env = make_env()
    _log(env).parent.mkdir(parents=True)
    _log(env).write_text("old line\n")
    _log(env).chmod(0o644)
    assert env.run("install").returncode == 0
    assert stat.S_IMODE(_log(env).stat().st_mode) == 0o600
    assert _log(env).read_text() == "old line\n"          # small logs are kept as-is


def test_restart_rotates_an_oversized_log_keeping_one_old_copy(make_env):
    env = make_env()
    assert env.run("install").returncode == 0
    rotated = _log(env).with_name("voxwire.log.1")
    rotated.write_text("older rotation\n")
    with open(_log(env), "r+b") as f:                       # sparse, just over 10 MB
        f.truncate(10 * 1024 * 1024 + 1)
    res = env.run("restart")
    assert res.returncode == 0, res.stderr
    assert "rotated" in res.stdout
    assert rotated.stat().st_size == 10 * 1024 * 1024 + 1   # replaced the older one
    assert _log(env).stat().st_size == 0
    assert stat.S_IMODE(_log(env).stat().st_mode) == 0o600
    assert stat.S_IMODE(rotated.stat().st_mode) == 0o600


def test_install_retires_the_legacy_agent(make_env):
    env = make_env()
    env.add_legacy_agent()
    res = env.run("install")
    assert res.returncode == 0, res.stderr
    assert f"bootout gui/{UID}/io.voxwire.agent" in env.calls()
    assert not env.loaded("io.voxwire.agent")
    assert not env.legacy_plist.exists()
    assert env.loaded("io.voxwire")


def test_install_changes_nothing_when_the_new_plist_fails_validation(make_env):
    env = make_env()
    assert env.run("install").returncode == 0
    before = env.plist.read_text()
    env.add_legacy_agent()
    res = env.run("install", "--headless", STUB_PLUTIL="fail")
    assert res.returncode != 0
    assert "validation" in res.stderr
    assert env.plist.read_text() == before and env.loaded("io.voxwire")
    assert env.legacy_plist.exists() and env.loaded("io.voxwire.agent")
    assert "bootout" not in env.calls().split("--headless")[-1]
    assert env.leftovers() == []


def test_reinstall_restores_the_previous_service_when_bootstrap_fails(make_env):
    env = make_env()
    assert env.run("install").returncode == 0                  # menubar
    before = env.plist.read_text()
    res = env.run("install", "--headless", STUB_BOOTSTRAP="fail-once")
    assert res.returncode != 0
    assert "restored the previous service" in res.stderr
    assert env.plist.read_text() == before and env.loaded("io.voxwire")
    assert env.leftovers() == []


def test_install_falls_back_to_the_legacy_agent_when_bootstrap_fails(make_env):
    env = make_env()
    env.add_legacy_agent()
    res = env.run("install", STUB_BOOTSTRAP="fail-once")
    assert res.returncode != 0
    assert "restored the previous service" in res.stderr
    assert env.legacy_plist.exists() and env.loaded("io.voxwire.agent")
    assert not env.plist.exists() and not env.loaded("io.voxwire")


def test_install_says_so_when_nothing_could_be_restored(make_env):
    env = make_env()
    res = env.run("install", STUB_BOOTSTRAP="fail")
    assert res.returncode != 0
    assert "not running" in res.stderr
    assert "installed →" not in res.stdout
    assert not env.plist.exists() and env.leftovers() == []


def test_install_stops_before_touching_anything_if_the_current_job_will_not_stop(make_env):
    env = make_env()
    assert env.run("install").returncode == 0
    env.add_legacy_agent()
    res = env.run("install", "--headless", STUB_STUCK="io.voxwire")
    assert res.returncode != 0
    assert "menubar.py" in env.plist.read_text()
    assert env.loaded("io.voxwire.agent") and env.legacy_plist.exists()
    assert env.leftovers() == []


def test_install_without_rumps_or_uv_stops_before_writing_a_crash_loop(make_env):
    env = make_env(uv=False, rumps=False)
    res = env.run("install")
    assert res.returncode != 0
    assert "rumps" in res.stderr
    assert not env.plist.exists()
    assert "bootstrap" not in env.calls()


def test_install_without_rumps_fails_if_uv_cannot_install_it(make_env):
    env = make_env(rumps=False)
    res = env.run("install", STUB_UV="broken")
    assert res.returncode != 0
    assert "rumps" in res.stderr
    assert not env.plist.exists()


def test_install_uses_uv_to_add_rumps(make_env):
    env = make_env(rumps=False)
    res = env.run("install")
    assert res.returncode == 0, res.stderr
    assert "uv pip install" in env.calls() and "rumps" in env.calls()
    assert env.loaded("io.voxwire")


def test_headless_install_needs_no_rumps(make_env):
    env = make_env(uv=False, rumps=False)
    res = env.run("install", "--headless")
    assert res.returncode == 0, res.stderr
    text = env.plist.read_text()
    assert "server.py" in text and "<string>Background</string>" in text


def test_restart_loads_the_job_when_it_was_booted_out(make_env):
    env = make_env()
    assert env.run("install").returncode == 0
    (env.state / "loaded.io.voxwire").unlink()          # a manual bootout
    res = env.run("restart")
    assert res.returncode == 0, res.stderr
    assert env.loaded("io.voxwire")
    assert env.calls().count("bootstrap") == 2
    assert "kickstart" not in env.calls()


def test_restart_kickstarts_a_loaded_job(make_env):
    env = make_env()
    assert env.run("install").returncode == 0
    res = env.run("restart")
    assert res.returncode == 0, res.stderr
    assert env.calls().count("bootstrap") == 1
    assert env.calls().strip().splitlines()[-1] == f"launchctl kickstart -k gui/{UID}/io.voxwire"


def test_restart_when_not_installed_says_so(make_env):
    env = make_env()
    res = env.run("restart")
    assert res.returncode != 0
    assert "not installed" in res.stderr


def test_status_recognises_voxwire_with_a_bounded_probe(make_env):
    env = make_env()
    res = env.run("status", STUB_HTTP="voxwire")
    assert "Voxwire responding" in res.stdout
    probes = [c for c in env.calls().splitlines() if c.startswith("curl")]
    assert probes and all("--max-time" in c for c in probes)
    assert "/api/dictation/status" in probes[0]


def test_status_does_not_mistake_another_server_for_voxwire(make_env):
    env = make_env()
    res = env.run("status", STUB_HTTP="other")
    assert "something else is serving" in res.stdout
    assert "Voxwire responding" not in res.stdout


def test_status_when_nothing_listens(make_env):
    env = make_env()
    res = env.run("status", STUB_HTTP="down")
    assert "not responding" in res.stdout


def test_status_warns_when_the_legacy_job_is_loaded(make_env):
    env = make_env()
    env.add_legacy_agent()
    res = env.run("status", STUB_HTTP="voxwire")
    assert "io.voxwire.agent" in res.stdout


def test_uninstall_removes_the_job_and_any_legacy_agent(make_env):
    env = make_env()
    assert env.run("install").returncode == 0
    env.add_legacy_agent()
    res = env.run("uninstall")
    assert res.returncode == 0, res.stderr
    assert not env.plist.exists() and not env.legacy_plist.exists()
    assert not env.loaded("io.voxwire") and not env.loaded("io.voxwire.agent")


def test_uninstall_keeps_the_plist_and_fails_when_bootout_leaves_the_job_running(make_env):
    env = make_env()
    assert env.run("install").returncode == 0
    res = env.run("uninstall", STUB_STUCK="io.voxwire")
    assert res.returncode != 0
    assert "NOT uninstalled" in res.stderr
    assert "uninstalled (" not in res.stdout
    assert env.plist.exists() and env.loaded("io.voxwire")


def test_uninstall_keeps_the_legacy_plist_when_its_job_will_not_stop(make_env):
    env = make_env()
    env.add_legacy_agent()
    res = env.run("uninstall", STUB_STUCK="io.voxwire.agent")
    assert res.returncode != 0
    assert "io.voxwire.agent" in res.stderr
    assert env.legacy_plist.exists() and env.loaded("io.voxwire.agent")


def test_uninstall_trusts_the_job_state_not_bootouts_exit_code(make_env):
    env = make_env()
    assert env.run("install").returncode == 0
    res = env.run("uninstall", STUB_BOOTOUT_NOISY="1")
    assert res.returncode == 0, res.stderr
    assert not env.plist.exists() and not env.loaded("io.voxwire")


def test_off_macos_it_points_at_run_sh_instead_of_a_missing_compose_file(make_env):
    env = make_env()
    res = env.run("install", STUB_UNAME="Linux")
    assert res.returncode != 0
    assert "run.sh" in res.stderr
    assert "docker-compose" not in res.stderr


def test_a_stuck_legacy_job_fails_install_before_the_current_one_is_stopped(make_env):
    """Both labels loaded and the old job won't stop. install used to boot out
    io.voxwire first, then die saying nothing was changed, leaving Voxwire
    stopped (PR 12 review). It must fail while the current job still runs."""
    env = make_env()
    assert env.run("install").returncode == 0
    env.add_legacy_agent()
    res = env.run("install", "--headless", STUB_STUCK="io.voxwire.agent")
    assert res.returncode != 0
    assert "io.voxwire.agent" in res.stderr and "restarted io.voxwire unchanged" in res.stderr
    assert env.loaded("io.voxwire"), "the current service was left stopped"
    assert "menubar.py" in env.plist.read_text()      # the staged plist was never swapped in
    assert env.loaded("io.voxwire.agent") and env.legacy_plist.exists()
    assert env.leftovers() == []
