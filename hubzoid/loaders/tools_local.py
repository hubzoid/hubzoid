"""Auto-discover @function_tool callables in <hub>/tools_local/*.py.

We import every `.py` file under the hub's `tools_local/` (case/plural-flexible
via _fs.resolve_bucket) and collect any module-level attributes that are
`FunctionTool` instances. Naming convention: the attribute name is the tool
name unless `@function_tool(name_override="...")` was used.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

from agents.tool import FunctionTool

from .._fs import resolve_bucket

log = logging.getLogger(__name__)


def load_all(hub_dir: Path) -> dict[str, FunctionTool]:
    tools_dir = resolve_bucket(hub_dir, "tools_local")
    if tools_dir is None:
        return {}

    out: dict[str, FunctionTool] = {}
    for path in sorted(tools_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue  # convention: underscore = private/example
        mod = _load_module(path)
        for attr_name in dir(mod):
            if attr_name.startswith("_"):
                continue
            obj = getattr(mod, attr_name)
            if isinstance(obj, FunctionTool):
                name = obj.name
                if name in out:
                    log.warning(
                        "duplicate tool name %r — keeping the first definition (%s shadows existing)",
                        name, path,
                    )
                    continue
                out[name] = obj
    return out


def _load_module(path: Path):
    """Import a .py file as a uniquely-named module."""
    mod_name = f"hubzoid_tools_local_{path.stem}_{abs(hash(path.as_posix())) % 10_000_000}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module
