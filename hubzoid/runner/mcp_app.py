"""The hub's MCP server as an AgentCore MCP runtime: ``hubzoid runner mcp``.

The same server the hub service mounts at ``/mcp`` (``mcp_server.build_server``:
knowledge, skills, hub tools, files, approvals), served on ``0.0.0.0:8000/mcp``
in a microVM per MCP session. Sign-in is checked twice: AgentCore's JWT
authorizer first (``discoveryUrl`` at the hub's
``/mcp/oauth/.well-known/openid-configuration``, audience the runtime's own
address), then Hubzoid here, against its own keys and the person's live grant.
Tokens are issued only by the hub service; nothing here issues or refreshes.

AgentCore checks an MCP runtime's health with an unauthenticated JSON-RPC
``ping`` to ``POST /mcp``. That one call is answered without a token; every
other request needs one. Outside callers never reach this unsigned: AgentCore's
authorizer refuses them first.

Platform V2 snapshots this process once it answers health checks, and every
session starts from that snapshot. So nothing secret, no database connection
and no hub tool code is loaded before the first signed request: the MCP server
itself is built then, after configuration and secrets are read.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from . import role


RUNTIME_NAME_ENV = "HUBZOID_MCP_RUNTIME_NAME"


def own_url(settings) -> str:
    """This runtime's own address: ``HUBZOID_MCP_AGENTCORE_URL``, or looked up
    by the runtime's name (``HUBZOID_MCP_RUNTIME_NAME``). A CloudFormation
    stack cannot hand a runtime its own ARN, which AgentCore picks on creation."""
    if settings.mcp_agentcore_url:
        return settings.mcp_agentcore_url
    name = (os.environ.get(RUNTIME_NAME_ENV) or "").strip()
    if not name:
        return ""
    import boto3

    from .protocol import agentcore_url

    region = settings.agentcore_region or os.environ.get("AWS_REGION") or None
    ctl = boto3.client("bedrock-agentcore-control", region_name=region)
    for page in ctl.get_paginator("list_agent_runtimes").paginate():
        for runtime in page.get("agentRuntimes", []):
            if runtime.get("agentRuntimeName") == name:
                return agentcore_url(runtime["agentRuntimeArn"], region, settings.agentcore_qualifier)
    raise RuntimeError(f"No AgentCore runtime named {name!r} in this account and region.")


def _verifier(hub_dir: Path, settings):
    from fastmcp.server.auth import TokenVerifier

    from ..mcp_oauth import HubOAuth

    address = own_url(settings)
    if not settings.mcp_public_url or not address:
        raise RuntimeError("hubzoid runner mcp needs MCP_PUBLIC_URL (the hub service's /mcp address, "
                           "which issues tokens) and this runtime's address (HUBZOID_MCP_AGENTCORE_URL, "
                           f"or {RUNTIME_NAME_ENV} to look it up).")
    provider = HubOAuth(hub_dir, settings.mcp_public_url, extra_resources=(address,))
    provider.accept = (provider.resources[-1],)

    class AgentCoreVerifier(TokenVerifier):
        async def verify_token(self, token: str):
            return await provider.load_access_token(token)

    return AgentCoreVerifier()


def _build(hub_dir: Path):
    from .. import settings as settingslib
    from ..mcp_server import build_server

    settings = settingslib.load(hub_dir)
    mcp, _count = build_server(hub_dir, settings=settings, auth=_verifier(hub_dir, settings))
    return mcp.http_app(path="/mcp", stateless_http=settings.mcp_stateless)


async def _json(send, status: int, obj: dict) -> None:
    out = json.dumps(obj).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(out)).encode())]})
    await send({"type": "http.response.body", "body": out})


async def _body(receive) -> bytes:
    body, more = b"", True
    while more:
        message = await receive()
        body += message.get("body", b"")
        more = message.get("more_body", False)
    return body


class McpRuntime:
    """The ASGI app AgentCore talks to."""

    def __init__(self, hub_dir: Path):
        self.hub_dir = Path(hub_dir).resolve()
        self.app = None
        self._lifespan = None
        self._lock = asyncio.Lock()

    async def _inner(self):
        async with self._lock:
            if self.app is None:
                app = await asyncio.to_thread(_build, self.hub_dir)
                self._lifespan = app.router.lifespan_context(app)
                await self._lifespan.__aenter__()
                self.app = app
        return self.app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    if self._lifespan is not None:
                        await self._lifespan.__aexit__(None, None, None)
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return await (await self._inner())(scope, receive, send)
        path, method = scope["path"].rstrip("/") or "/", scope["method"]
        if path == "/ping" and method == "GET":
            return await _json(send, 200, {"status": "Healthy"})
        if path == "/warmup" and method == "POST":
            from . import server as runner_server

            await asyncio.to_thread(runner_server._warm_memory, self.hub_dir)
            await asyncio.to_thread(runner_server._warm_disk, self.hub_dir)
            return await _json(send, 200, {"ok": True})
        signed = any(k == b"authorization" for k, _ in scope.get("headers", []))
        if path == "/mcp" and method == "POST" and not signed:
            body = await _body(receive)
            try:
                request = json.loads(body or b"{}")
            except ValueError:
                request = None
            if isinstance(request, dict) and request.get("method") == "ping":
                return await _json(send, 200, {"jsonrpc": "2.0", "id": request.get("id"), "result": {}})
            return await _json(send, 401, {"error": "invalid_token", "error_description": "Sign in first"})
        if not signed:
            return await _json(send, 401, {"error": "invalid_token", "error_description": "Sign in first"})
        return await (await self._inner())(scope, receive, send)


def create_app(hub_dir: Path):
    return McpRuntime(hub_dir)


def serve(hub_dir: Path, *, port: int = 8000) -> None:
    """``hubzoid runner mcp``: serve the hub's MCP server on 0.0.0.0."""
    import uvicorn

    os.environ.setdefault(role.ENV, role.MCP)
    if role.current() != role.MCP:
        raise SystemExit(f"hubzoid runner mcp needs {role.ENV}=mcp (it is {role.current()!r}).")
    uvicorn.run(create_app(hub_dir), host="0.0.0.0", port=port, log_level="info", proxy_headers=False)
