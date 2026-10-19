"""Slack routes on the hub's channels router."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import Request

from ...control import authz
from .. import config
from ..errors import ChannelError


def mount(router, hub_dir: Path) -> None:
    @router.get("/channels/{cid}/slack/manifest")
    async def slack_manifest(hub_id: str, cid: str, request: Request):
        """The app manifest for this connection's applied revision."""
        from . import manifest

        await authz.require_request(request, hub_dir, "channels.inspect", config.hub_of(hub_dir))
        try:
            conn = await asyncio.to_thread(config.active, hub_dir, cid)
        except ChannelError as exc:
            raise exc.http() from None
        if conn.provider != "slack":
            raise ChannelError("not_found", "This channel is not a Slack connection.", status=404).http()
        from . import adapter

        return {"manifest": manifest.build(conn), "revision": conn.revision,
                "required_scopes": sorted(set(adapter.BASE_SCOPES) |
                                          ({"files:read"} if conn.settings.files_enabled else set()))}
