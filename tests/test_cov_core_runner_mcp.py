"""MCP runtime lazy startup, malformed unsigned requests and lifecycle cleanup."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient

from hubzoid.runner import mcp_app, role, server
from tests.test_agentcore_runner import hub  # noqa: F401


@pytest.mark.parametrize("body", [b"{bad", b"[]", b"null", b""])
def test_unsigned_invalid_requests_are_refused_without_build(hub, monkeypatch, body):
    build = Mock(side_effect=AssertionError("unsigned request must not load tools"))
    monkeypatch.setattr(mcp_app, "_build", build)
    with TestClient(mcp_app.create_app(hub)) as client:
        result = client.post("/mcp", content=body)
    assert result.status_code == 401
    assert result.json() == {"error": "invalid_token", "error_description": "Sign in first"}
    build.assert_not_called()


def test_signed_requests_build_once_and_close_lifespan(hub, monkeypatch):
    lifecycle = []

    @asynccontextmanager
    async def lifespan(app):
        lifecycle.append("opened")
        yield
        lifecycle.append("closed")

    async def endpoint(request):
        return JSONResponse({"path": request.url.path})

    inner = Starlette(routes=[Route("/mcp", endpoint, methods=["GET", "POST"])], lifespan=lifespan)
    build = Mock(return_value=inner)
    monkeypatch.setattr(mcp_app, "_build", build)
    runtime = mcp_app.create_app(hub)
    with TestClient(runtime) as client:
        assert lifecycle == [] and runtime.app is None
        for method in (client.get, client.post):
            result = method("/mcp", headers={"authorization": "Bearer fake"})
            assert result.status_code == 200 and result.json() == {"path": "/mcp"}
        assert lifecycle == ["opened"]
    assert lifecycle == ["opened", "closed"]
    build.assert_called_once_with(hub.resolve())


def test_websocket_scope_is_delegated_and_warmup_does_not_build_tools(hub, monkeypatch):
    async def websocket(socket):
        await socket.accept()
        await socket.send_json({"delegated": True})
        await socket.close()

    monkeypatch.setattr(mcp_app, "_build", lambda path: Starlette(routes=[WebSocketRoute("/ws", websocket)]))
    memory, disk = Mock(), Mock()
    monkeypatch.setattr(server, "_warm_memory", memory)
    monkeypatch.setattr(server, "_warm_disk", disk)
    runtime = mcp_app.create_app(hub)
    with TestClient(runtime) as client:
        result = client.post("/warmup")
        assert result.status_code == 200 and result.json() == {"ok": True}
        assert runtime.app is None
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json() == {"delegated": True}
    memory.assert_called_once_with(hub.resolve())
    disk.assert_called_once_with(hub.resolve())


@pytest.mark.asyncio
async def test_request_body_joins_multiple_asgi_chunks():
    chunks = iter([{"body": b'{"method":', "more_body": True}, {"body": b'"ping"}'}])

    async def receive():
        return next(chunks)

    assert json.loads(await mcp_app._body(receive)) == {"method": "ping"}


def test_missing_runtime_address_refuses_verifier(hub, monkeypatch):
    monkeypatch.delenv(mcp_app.RUNTIME_NAME_ENV, raising=False)
    settings = SimpleNamespace(mcp_agentcore_url="", mcp_public_url="https://hub.example/mcp")
    assert mcp_app.own_url(settings) == ""
    with pytest.raises(RuntimeError, match="needs MCP_PUBLIC_URL"):
        mcp_app._verifier(hub, settings)


def test_serve_requires_mcp_role_and_sets_safe_uvicorn_options(hub, monkeypatch):
    import uvicorn

    monkeypatch.setenv(role.ENV, "runner")
    with pytest.raises(SystemExit, match="needs HUBZOID_ROLE=mcp"):
        mcp_app.serve(hub)
    monkeypatch.delenv(role.ENV)
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    mcp_app.serve(hub, port=8999)
    assert role.current() == role.MCP
    assert run.call_args.kwargs == {"host": "0.0.0.0", "port": 8999, "log_level": "info", "proxy_headers": False}
    assert isinstance(run.call_args.args[0], mcp_app.McpRuntime)


@pytest.mark.asyncio
async def test_lazy_build_serializes_simultaneous_first_requests(hub, monkeypatch):
    inner = Starlette()
    build = Mock(return_value=inner)
    monkeypatch.setattr(mcp_app, "_build", build)
    runtime = mcp_app.create_app(hub)
    first, second = await asyncio.gather(runtime._inner(), runtime._inner())
    assert first is inner and second is inner
    build.assert_called_once()
    await runtime._lifespan.__aexit__(None, None, None)
