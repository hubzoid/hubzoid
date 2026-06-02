"""Tests for the edge router (issue #1: artifact downloads via the public port).

Unit tests cover the routing predicate + header filtering. The integration
test stands up real loopback servers — a mock bridge, a mock Open WebUI (with
an SSE endpoint and a websocket echo), and the real edge in front — and proves
that /artifacts reaches the bridge, everything else reaches OWUI, SSE streams
through, and websockets relay.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from hubzoid import edge


# ---------------------------------------------------------------------------
# Unit: routing table + header filters
# ---------------------------------------------------------------------------
_ARTIFACTS = (edge.EdgeRoute(prefix="/artifacts", upstream="http://bridge"),)


@pytest.mark.parametrize("path,to_bridge", [
    ("/artifacts", True),
    ("/artifacts/chat-1/report.json", True),
    ("/artifactshack", False),       # prefix must be a full segment
    ("/v1/chat/completions", False),
    ("/uploads/chat-1/x", False),
    ("/healthz", False),
    ("/", False),
    ("/some/owui/page", False),
])
def test_single_hub_only_artifacts_to_bridge(path, to_bridge):
    upstream, fwd = edge._forward_target(path, _ARTIFACTS, "http://owui")
    assert upstream == ("http://bridge" if to_bridge else "http://owui")
    # No strip in the single-hub case: the path passes through unchanged.
    assert fwd == path


def test_gateway_strip_prefix_rewrites_artifact_path():
    routes = (
        edge.EdgeRoute(prefix="/b/irs/artifacts", upstream="http://bridge-irs", strip_prefix="/b/irs"),
        edge.EdgeRoute(prefix="/b/gpms/artifacts", upstream="http://bridge-gpms", strip_prefix="/b/gpms"),
    )
    up, fwd = edge._forward_target("/b/irs/artifacts/chat-9/report.json", routes, "http://owui")
    assert up == "http://bridge-irs"
    assert fwd == "/artifacts/chat-9/report.json"        # bridge sees its native path
    # Other hub's prefix lands on its own bridge.
    up2, fwd2 = edge._forward_target("/b/gpms/artifacts/c/x", routes, "http://owui")
    assert up2 == "http://bridge-gpms"
    assert fwd2 == "/artifacts/c/x"
    # The UI (no artifact prefix) goes to OWUI untouched.
    assert edge._forward_target("/chat", routes, "http://owui") == ("http://owui", "/chat")


def test_response_headers_strip_hop_by_hop_and_length():
    resp = httpx.Response(
        200,
        headers={
            "content-type": "application/json",
            "content-length": "10",
            "transfer-encoding": "chunked",
            "connection": "keep-alive",
            "x-custom": "keep-me",
        },
    )
    out = edge._response_headers(resp)
    assert out.get("content-type") == "application/json"
    assert out.get("x-custom") == "keep-me"
    assert "content-length" not in out
    assert "transfer-encoding" not in out
    assert "connection" not in out


# ---------------------------------------------------------------------------
# Integration: real servers behind the edge
# ---------------------------------------------------------------------------
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Server:
    def __init__(self, app, port):
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self):
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                return
            time.sleep(0.02)
        raise RuntimeError("server did not start in time")

    def stop(self):
        self._server.should_exit = True
        self._thread.join(timeout=5)


def _bridge_app() -> Starlette:
    async def any_path(request):
        return PlainTextResponse(f"BRIDGE:{request.url.path}")
    return Starlette(routes=[Route("/{path:path}", any_path)])


def _owui_app() -> Starlette:
    async def any_path(request):
        return PlainTextResponse(f"OWUI:{request.url.path}")

    async def sse(request):
        async def gen():
            for i in range(3):
                yield f"data: {i}\n\n".encode()
                await asyncio.sleep(0.01)
        return StreamingResponse(gen(), media_type="text/event-stream")

    async def ws_echo(ws):
        await ws.accept()
        try:
            while True:
                msg = await ws.receive_text()
                await ws.send_text("echo:" + msg)
        except WebSocketDisconnect:
            pass

    return Starlette(routes=[
        Route("/sse", sse),
        WebSocketRoute("/ws", ws_echo),
        Route("/{path:path}", any_path),
    ])


@pytest.fixture(scope="module")
def edge_url():
    bport, oport, eport = _free_port(), _free_port(), _free_port()
    bridge = _Server(_bridge_app(), bport)
    owui = _Server(_owui_app(), oport)
    app = edge.build_edge_app(
        default_base=f"http://127.0.0.1:{oport}",
        routes=[edge.EdgeRoute(prefix="/artifacts", upstream=f"http://127.0.0.1:{bport}")],
    )
    front = _Server(app, eport)
    bridge.start()
    owui.start()
    front.start()
    try:
        yield f"http://127.0.0.1:{eport}", f"ws://127.0.0.1:{eport}"
    finally:
        front.stop()
        owui.stop()
        bridge.stop()


def test_artifacts_route_to_bridge(edge_url):
    base, _ = edge_url
    r = httpx.get(base + "/artifacts/chat-1/report.json", timeout=10)
    assert r.status_code == 200
    assert r.text == "BRIDGE:/artifacts/chat-1/report.json"


def test_non_artifacts_route_to_owui(edge_url):
    base, _ = edge_url
    for path in ("/", "/chat", "/v1/models", "/uploads/x"):
        r = httpx.get(base + path, timeout=10)
        assert r.status_code == 200
        assert r.text == f"OWUI:{path}", path


def test_sse_streams_through(edge_url):
    base, _ = edge_url
    with httpx.stream("GET", base + "/sse", timeout=10) as r:
        body = "".join(chunk for chunk in r.iter_text())
    assert body == "data: 0\n\ndata: 1\n\ndata: 2\n\n"


def test_websocket_relays_through(edge_url):
    _, ws_base = edge_url

    async def roundtrip():
        async with websockets.connect(ws_base + "/ws", open_timeout=10) as ws:
            await ws.send("hello")
            return await ws.recv()

    assert asyncio.run(roundtrip()) == "echo:hello"
