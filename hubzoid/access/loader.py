# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
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


#: The surfaces a restricted capability's own ``surfaces:`` may name: where a
#: request comes from (``access.policy``), not the Settings display groups.
KNOWN_SURFACES = frozenset({"owui", "web", "api", "mcp", "workflow", "app", "slack-dm", "whatsapp",
                            "telegram", "webhook", "email", "channel", "cli"})
#: Never allowed: a shared Slack thread answered under one person's identity.
NEVER_SURFACES = frozenset({"slack-channel"})


def restricted_surfaces(hub_dir: Path) -> dict[str, frozenset[str]]:
    """``{capability: surfaces}`` for the restricted capabilities whose entry in
    the optional ``identity/permissions.yaml`` sets ``surfaces:``. Each list
    replaces ``HUBZOID_RESTRICTED_SURFACES`` for that one capability, so one
    capability can be opened to WhatsApp without opening every other one. An
    empty list means no surface. Raises ValueError naming the file for a
    surface that is unknown or never allowed (``slack-channel``)."""
    from ..control import yamlfile

    idir = resolve_bucket(hub_dir, "identity")
    path = idir / "permissions.yaml" if idir else None
    if path is None or not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if "surfaces" not in text:
        return {}  # labels only: the Settings reader (capabilities._metadata) handles those
    try:
        # Strict: a duplicate key (a second `reset:` without surfaces) or a
        # document that isn't a mapping must not fall back to the hub-wide list.
        data = yamlfile.loads(text, path=str(path)).data
    except yamlfile.YamlError as exc:
        where = f" (line {exc.line})" if exc.line else ""
        raise ValueError(f"{path}{where}: {exc.message} It sets surfaces:, so it must be valid.") from None
    out: dict[str, frozenset[str]] = {}
    for name, meta in data.items():
        if not isinstance(meta, dict):
            if meta is not None:
                raise ValueError(f"{path}: {name}: each capability is a mapping, for example "
                                 f"{name}: {{surfaces: [web]}}")
            continue
        if "surfaces" not in meta:
            continue
        raw = meta["surfaces"]
        if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
            raise ValueError(f"{path}: {name}: surfaces is a list of names, for example [web, whatsapp]")
        surfaces = frozenset(v.strip().lower() for v in raw if v.strip())
        never = surfaces & NEVER_SURFACES
        if never:
            raise ValueError(f"{path}: {name}: {sorted(never)[0]} is never allowed for restricted tools (a "
                             "shared thread answers under one person's identity). Use slack-dm.")
        from .guard import allowed_surfaces

        unknown = surfaces - KNOWN_SURFACES - allowed_surfaces()   # a deployment's own surface names count
        if unknown:
            raise ValueError(f"{path}: {name}: unknown surface {sorted(unknown)[0]!r}. "
                             f"Use any of: {', '.join(sorted(KNOWN_SURFACES))}")
        out[normalize(str(name))] = surfaces
    return out
