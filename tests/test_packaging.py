"""What `pip install hubzoid` ships and installs.

1.0.1 shipped a wheel without two packages because the package list was kept by
hand. These checks read the build configuration itself, so a new directory of
Python modules that would not ship fails here, before a release.
"""
from __future__ import annotations

import glob
import re
import tomllib
from pathlib import Path

import pytest
from setuptools import find_packages

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "hubzoid"
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


def _packages() -> set[str]:
    find = PYPROJECT["tool"]["setuptools"]["packages"]["find"]
    assert find.get("namespaces") is False, "only directories with __init__.py are packages"
    return set(find_packages(where=str(ROOT), include=find["include"], exclude=find.get("exclude", ())))


def _package_data() -> set[Path]:
    """Every file under hubzoid/ that the package-data globs ship."""
    files: set[Path] = set()
    for pattern in PYPROJECT["tool"]["setuptools"]["package-data"]["hubzoid"]:
        for match in glob.glob(pattern, root_dir=PKG, recursive=True, include_hidden=True):
            if (PKG / match).is_file():
                files.add((PKG / match).resolve())
    return files


def _python_dirs() -> list[Path]:
    return sorted({p.parent for p in PKG.rglob("*.py") if "__pycache__" not in p.parts})


def test_every_directory_of_python_modules_ships():
    packages, data = _packages(), _package_data()
    missing = []
    for folder in _python_dirs():
        dotted = ".".join(folder.relative_to(ROOT).parts)
        if dotted in packages:
            continue
        modules = [p.resolve() for p in folder.glob("*.py")]
        if not all(m in data for m in modules):
            missing.append(dotted)
    assert not missing, (
        f"{missing} would not ship: add an __init__.py (every package under hubzoid/ "
        "ships) or cover the files in [tool.setuptools.package-data]")


def test_every_package_directory_is_found():
    found = _packages()
    on_disk = {".".join(p.parent.relative_to(ROOT).parts) for p in PKG.rglob("__init__.py")
               if "__pycache__" not in p.parts}
    assert on_disk == found
    for name in ("hubzoid.auth", "hubzoid.chat", "hubzoid.connectors", "hubzoid.migrations.operational"):
        assert name in found, name


def test_package_data_covers_templates_migrations_and_the_web_app():
    data = _package_data()
    for rel in ("templates/minimal/AGENTS.md", "templates/minimal/connectors/.mcp.json",
                "migrations/operational/versions/0009_accounts.py",
                "migrations/operational/script.py.mako"):
        assert (PKG / rel).resolve() in data, rel
    for name in ("minimal", "demo", "watchtower"):
        tpl = PKG / "templates" / name
        assert all(p.resolve() in data for p in tpl.rglob("*") if p.is_file()
                   and "__pycache__" not in p.parts and p.suffix != ".pyc"), name
    index = PKG / "portal_dist" / "index.html"
    if index.is_file():
        assert index.resolve() in data


def _requirement_names(reqs: list[str]) -> dict[str, str]:
    out = {}
    for req in reqs:
        name = re.split(r"[\s\[<>=!~;]", req, maxsplit=1)[0].lower().replace("_", "-")
        out[name] = req
    return out


def test_open_webui_is_an_optional_extra():
    core = _requirement_names(PYPROJECT["project"]["dependencies"])
    assert "open-webui" not in core
    assert PYPROJECT["project"]["optional-dependencies"]["openwebui"] == ["open-webui==0.11.4"]


def test_web_app_dependencies_are_declared_with_bounds():
    core = _requirement_names(PYPROJECT["project"]["dependencies"])
    assert core["pwdlib"].startswith("pwdlib[argon2,bcrypt]")
    for name in ("pwdlib", "authlib", "itsdangerous", "python-multipart"):
        assert ">=" in core[name] and "<" in core[name], core[name]


def test_requirements_txt_mirrors_the_dependencies():
    lines = [ln.strip() for ln in (ROOT / "requirements.txt").read_text().splitlines()
             if ln.strip() and not ln.startswith("#")]
    assert lines == PYPROJECT["project"]["dependencies"]


def _pins(name: str) -> dict[str, str]:
    pins = {}
    for line in (ROOT / name).read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;]+)", line)
        if m:
            pins[m.group(1).lower()] = m.group(2)
    return pins


def test_core_lock_is_slim_and_legacy_lock_keeps_open_webui():
    core, legacy = _pins("requirements.lock"), _pins("requirements-openwebui.lock")
    for heavy in ("open-webui", "torch", "av", "transformers", "sentence-transformers", "chromadb"):
        assert heavy not in core, heavy
    for name in ("pwdlib", "authlib", "itsdangerous", "python-multipart", "fastapi", "psycopg"):
        assert name in core, name
    assert legacy["open-webui"] == "0.11.4"
    # One reviewed set: every package both locks pin has the same version.
    assert {k: v for k, v in core.items() if k in legacy and legacy[k] != v} == {}
    assert len(core) < len(legacy) / 1.5


@pytest.mark.parametrize("lock", ["requirements.lock", "requirements-openwebui.lock"])
def test_locks_pin_this_release_line(lock):
    """Each lock satisfies the declared bounds of the web app dependencies."""
    pins = _pins(lock)
    assert pins["pwdlib"].startswith("0.3.")
    assert pins["python-multipart"].startswith("0.0.")
