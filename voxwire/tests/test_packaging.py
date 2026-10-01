"""Packaging tests.

pyproject.toml maps the flat `voxwire/` layout by hand, so a new top-level module
or package has to be listed there too, or an installed Voxwire cannot import it.
"""
import re
import tomllib
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "voxwire"
SETUPTOOLS = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]


def test_every_top_level_module_is_packaged():
    listed = set(SETUPTOOLS["py-modules"])
    on_disk = {p.stem for p in APP.glob("*.py")}
    missing = on_disk - listed
    assert not missing, f"add {sorted(missing)} to [tool.setuptools] py-modules"


def test_every_package_is_packaged():
    include = SETUPTOOLS["packages"]["find"]["include"]
    packages = {p.parent.name for p in APP.glob("*/__init__.py")}
    missing = {name for name in packages if not any(fnmatch(name, pat) for pat in include)}
    assert not missing, f"add {sorted(missing)} to [tool.setuptools.packages.find] include"


# scripts/install.sh installs by package name rather than from pyproject.toml, so
# it can drift: a core dependency it skips is a ModuleNotFoundError on a fresh
# install (e.g. httpx / mlx-lm, which the LLM fixup path imports).
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
# Code lines only, so a package named in a comment does not count as installed.
INSTALL_SH = "\n".join(line for line in (ROOT / "scripts" / "install.sh").read_text().splitlines()
                       if not line.lstrip().startswith("#"))


def _name(req: str) -> str:
    return re.split(r"[\[<>=!~; ]", req, maxsplit=1)[0].strip().lower()


def _installed_names() -> set[str]:
    return {_name(tok.strip("\"'")) for tok in re.findall(r"[\w\"'\[\].-]+", INSTALL_SH)}


def test_install_script_covers_core_dependencies():
    missing = {_name(r) for r in PROJECT["dependencies"]} - _installed_names()
    assert not missing, f"scripts/install.sh does not install core deps {sorted(missing)}"


def test_install_script_covers_mlx_extra():
    missing = {_name(r) for r in PROJECT["optional-dependencies"]["mlx"]} - _installed_names()
    assert not missing, f"scripts/install.sh does not install mlx extra {sorted(missing)}"
