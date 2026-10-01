"""Leak-check tests.

scripts/leakcheck.sh is the rule-1 guard: it must flag private addresses and
secret-shaped strings anywhere in the tree, and stay quiet on ordinary text.
Each case copies the script into a throwaway tree holding one sample file and
runs it there, so neither the real repo nor the maintainer denylist affects the
result. The suite runs under BSD grep (macOS) and GNU grep (CI), so both agree.

Samples are assembled at runtime so this file does not trip the check itself.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "leakcheck.sh"


def _ip(*octets: int) -> str:
    return ".".join(str(o) for o in octets)


def _run(tmp_path: Path, text: str, *, rel: str = "notes.md",
         denylist: str | None = None) -> subprocess.CompletedProcess:
    (tmp_path / "scripts").mkdir(exist_ok=True)
    shutil.copy(SCRIPT, tmp_path / "scripts" / "leakcheck.sh")
    if denylist is not None:
        (tmp_path / "scripts" / "private-denylist.txt").write_text(denylist)
    sample = tmp_path / rel
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text(text + "\n")
    return subprocess.run(["bash", str(tmp_path / "scripts" / "leakcheck.sh")],
                          capture_output=True, text=True, timeout=30)


FLAGGED = [
    pytest.param(f"host {_ip(10, 42, 0, 8)}", id="10/8"),
    pytest.param(f"gw {_ip(10, 0, 0, 1)}", id="10.0.0.x"),
    pytest.param(f"db={_ip(172, 16, 4, 2)}", id="172.16/12-low"),
    pytest.param(f"db={_ip(172, 31, 255, 1)}", id="172.16/12-high"),
    pytest.param(f"nas {_ip(192, 168, 1, 9)}.", id="192.168/16"),
    pytest.param(f"tailnet {_ip(100, 64, 0, 1)}", id="cgnat-low"),
    pytest.param(f"tailnet {_ip(100, 127, 255, 254)}", id="cgnat-high"),
    pytest.param(f"({_ip(10, 1, 2, 3)})", id="in-parens"),
    pytest.param(_ip(192, 168, 0, 1), id="whole-line"),
    pytest.param("key " + "sk-" + "a1B2" * 5, id="api-key"),
    pytest.param("AKIA" + "ABCDEFGHIJKLMNOP", id="aws-access-key"),
    pytest.param("-----BEGIN " + "RSA PRIVATE KEY-----", id="pem-private-key"),
]

CLEAN = [
    pytest.param(f"serve on {_ip(127, 0, 0, 1)}:8123", id="loopback"),
    pytest.param(f"bind {_ip(0, 0, 0, 0)}", id="any-address"),
    pytest.param(f"dns {_ip(8, 8, 8, 8)}", id="public"),
    pytest.param(f"{_ip(172, 15, 0, 1)} and {_ip(172, 32, 0, 1)}", id="just-outside-172.16/12"),
    pytest.param(f"{_ip(100, 63, 255, 255)} and {_ip(100, 128, 0, 1)}", id="just-outside-cgnat"),
    pytest.param(f"version {_ip(100, 2, 3)}", id="100.x.y-version"),
    pytest.param(_ip(192, 169, 1, 1), id="not-192.168"),
    pytest.param(f"{_ip(110, 0, 0, 1)} and {_ip(210, 10, 0, 1)}", id="10-inside-a-larger-octet"),
    pytest.param(f"versions {_ip(1, 10, 0, 3)} and {_ip(2, 10, 0)}", id="version-strings"),
    pytest.param("ranges: 10/8, 172.16/12, 192.168/16, 100.64/10", id="cidr-shorthand"),
]


@pytest.mark.parametrize("text", FLAGGED)
def test_flags_private_addresses_and_secrets(tmp_path, text):
    res = _run(tmp_path, text)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "notes.md" in res.stdout


@pytest.mark.parametrize("text", CLEAN)
def test_stays_quiet_on_ordinary_text(tmp_path, text):
    res = _run(tmp_path, text)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "leak check clean" in res.stdout


def test_denylist_terms_are_flagged(tmp_path):
    term = "acme" + "-internal"
    res = _run(tmp_path, f"deploy to {term}-01",
               denylist=f"# maintainer-local terms\n\n{term}\n")
    assert res.returncode == 1, res.stdout + res.stderr


def test_denylist_of_only_comments_changes_nothing(tmp_path):
    res = _run(tmp_path, "nothing private here", denylist="# comment\n\n   \n")
    assert res.returncode == 0, res.stdout + res.stderr


def test_skips_excluded_paths(tmp_path):
    res = _run(tmp_path, f"host {_ip(10, 42, 0, 8)}", rel=".venv/lib/site.txt")
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.parametrize("rel", ["recordings/take1.txt", "build/lib/x.txt", "dist/x.txt",
                                 "sub/node_modules/pkg/x.txt"])
def test_skips_excluded_dirs(tmp_path, rel):
    res = _run(tmp_path, f"host {_ip(10, 42, 0, 8)}", rel=rel)
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.parametrize("line", [
    "saved under recordings/ on {ip}",
    "artifacts in build/ and dist/ from {ip}",
    "clip.wav was captured at {ip}",
    "see scripts/leakcheck.sh, then ssh {ip}",
])
def test_exclusions_are_by_path_not_line_content(tmp_path, line):
    # A tracked file whose matching line merely MENTIONS an excluded path is
    # still a leak and must be reported.
    res = _run(tmp_path, line.format(ip=_ip(10, 42, 0, 8)))
    assert res.returncode == 1, res.stdout + res.stderr
    assert "notes.md" in res.stdout


def test_own_tooling_files_are_not_flagged(tmp_path):
    # The denylist necessarily contains the terms it guards; it must not self-flag.
    term = "acme" + "-internal"
    res = _run(tmp_path, "nothing private here", denylist=f"{term}\n")
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.parametrize("bad", ["(unclosed", "[abc"])
def test_malformed_denylist_regex_fails_closed(tmp_path, bad):
    res = _run(tmp_path, "nothing private here", denylist=f"{bad}\n")
    assert res.returncode == 2, res.stdout + res.stderr
    assert "leak check clean" not in res.stdout
    assert "NOT verified clean" in res.stderr


# --history: the pre-public-flip gate. A leak removed from the tree still ships
# in the commits that carried it, so the tree scan alone is not enough.

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "commit.gpgsign=false", *args],
                   cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path, *, denylist: str | None = None) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "leakcheck.sh")
    if denylist is not None:
        (tmp_path / "scripts" / "private-denylist.txt").write_text(denylist)
    _git(tmp_path, "init", "-q")
    return tmp_path


def _check(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(repo / "scripts" / "leakcheck.sh"), *args],
                          capture_output=True, text=True, timeout=30)


def test_history_flags_a_leak_removed_from_the_tree(tmp_path):
    repo = _repo(tmp_path)
    (repo / "notes.md").write_text(f"host {_ip(10, 42, 0, 8)}\n")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-qm", "add notes")
    (repo / "notes.md").write_text("host redacted\n")
    _git(repo, "commit", "-qam", "scrub notes")

    assert _check(repo).returncode == 0          # the tree is clean now…
    res = _check(repo, "--history")              # …but history is not
    assert res.returncode == 1, res.stdout + res.stderr
    assert ":notes.md:1:" in res.stdout


def test_history_flags_a_private_term_in_a_commit_message(tmp_path):
    term = "acme" + "-internal"
    repo = _repo(tmp_path, denylist=f"{term}\n")
    (repo / "a.txt").write_text("fine\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", f"deploy fix\n\nfrom {term}-01")
    res = _check(repo, "--history")
    assert res.returncode == 1, res.stdout + res.stderr
    assert ":message:deploy fix" in res.stdout


def test_history_clean_and_skips_own_tooling_files(tmp_path):
    term = "acme" + "-internal"
    repo = _repo(tmp_path, denylist=f"{term}\n")
    _git(repo, "add", "-f", "scripts")           # even a committed denylist self-excludes
    _git(repo, "commit", "-qm", "tooling")
    res = _check(repo, "--history")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "leak check clean" in res.stdout


def test_history_fails_closed_on_malformed_regex(tmp_path):
    repo = _repo(tmp_path, denylist="(unclosed\n")
    (repo / "a.txt").write_text("fine\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "one")
    res = _check(repo, "--history")
    assert res.returncode == 2, res.stdout + res.stderr
    assert "leak check clean" not in res.stdout


def test_rejects_unknown_arguments(tmp_path):
    res = _check(_repo(tmp_path), "--histroy")
    assert res.returncode == 2
    assert "usage" in res.stderr
