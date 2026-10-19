"""`github__<operation>` tools for agents and stations of a hub with a GitHub
channel connection. An agent lists the ones it needs in its frontmatter
(`tools: [github__getIssueContext, ...]`). See hubzoid.channels.github.operations
for the rules every call follows."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def make(ctx) -> list:
    try:
        from ..channels import config
    except ImportError:
        return []
    hub_dir = getattr(ctx, "hub_dir", None) or getattr(ctx, "root", None)
    if hub_dir is None:
        return []
    try:
        conns = [c for c in config.active_all(hub_dir) if c.provider == "github" and c.enabled]
    except Exception:  # noqa: BLE001 - no channel store yet
        return []
    if not conns:
        return []
    if len(conns) > 1:
        log.warning("several GitHub connections; the tools use %s", conns[0].id)
    from ..channels.github import operations

    return operations.tools(conns[0])
