# Hubzoid Enterprise · access management. Production use requires a license
# with the "access" entitlement; free to run for development. See LICENSING.md.
"""Load restricted tools from `<hub>/restricted/*.py` and tag each with the
permission its file name implies.

The convention: the file stem is the permission, normalized. So every
`@function_tool` defined in `restricted/sales.py` shares the permission
`sales`, and a non-`.py` file in the folder (for example `restricted/.env`,
where secrets live) is ignored. This mirrors `loaders/tools_local.py` but keys
each tool to the door it belongs to, and is kept separate so the access feature
stays self-contained.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

from agents.tool import FunctionTool

from .._fs import resolve_bucket
from .identity import normalize

log = logging.getLogger("hubzoid.access")


def load_restricted(hub_dir: Path) -> list[tuple[FunctionTool, str]]:
    """Return [(tool, permission)] for every FunctionTool under restricted/.

    Empty list when the hub has no `restricted/` folder, so existing hubs are
    completely unaffected.
    """
    rdir = resolve_bucket(hub_dir, "restricted")
    if rdir is None:
        return []

    out: list[tuple[FunctionTool, str]] = []
    seen: set[str] = set()
    for path in sorted(rdir.glob("*.py")):
        if path.name.startswith("_"):
            continue  # convention: underscore = private/example
        permission = normalize(path.stem)
        if not permission:
            continue
        mod = _load_module(path)
        for attr_name in dir(mod):
            if attr_name.startswith("_"):
                continue
            obj = getattr(mod, attr_name)
            if isinstance(obj, FunctionTool):
                if obj.name in seen:
                    log.warning(
                        "duplicate restricted tool name %r — keeping first (%s)",
                        obj.name, path,
                    )
                    continue
                seen.add(obj.name)
                out.append((obj, permission))
    return out


def _load_module(path: Path):
    """Import a .py file as a uniquely-named module (mirrors tools_local)."""
    mod_name = f"hubzoid_restricted_{path.stem}_{abs(hash(path.as_posix())) % 10_000_000}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module
