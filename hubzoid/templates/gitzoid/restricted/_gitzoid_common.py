"""Shared by GitZoid's restricted tool files. The leading underscore keeps
Hubzoid from loading it as a tool file."""
from __future__ import annotations

import functools
import hashlib
import importlib.util
import sys
from pathlib import Path

HUB_DIR = Path(__file__).resolve().parents[1]


def _lib():
    root = HUB_DIR / "workflows" / "gitzoid" / "lib"
    name = "gitzoid_" + hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:10]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, root / "package.py", submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


gz = _lib()


def ctx():
    import hubzoid
    user = hubzoid.current_user()
    if user is None:
        raise PermissionError("GitZoid's tools answer signed-in people only.")
    settings, _ = gz.settings.read_file(HUB_DIR)
    return user, settings, gz.store.Store(hubzoid.data_dir())


def gh(settings, store):
    return gz.runtime.make_github(settings, store, HUB_DIR)


def repo_name(settings, repo: str) -> str:
    names = settings.repo_names()
    if repo in names:
        return repo
    short = [n for n in names if n.split("/", 1)[1] == repo]
    if len(short) == 1:
        return short[0]
    raise ValueError(f"{repo!r} is not one of GitZoid's repos here: {', '.join(names)}")


def safe(fn):
    """Errors come back as a sentence the agent can relay, not a stack."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, PermissionError, FileNotFoundError, gz.github.GitHubError, gz.settings.SettingsError) as exc:
            return f"Could not answer: {exc}"
    return run
