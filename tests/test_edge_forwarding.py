"""The edge forwards exactly what it checked, and never buffers an unbounded body.

Review findings fixed here:

* Double percent-encoding: `POST /%256ftel/v1/traces` passed the edge's checks
  as `/%6ftel/v1/traces`, was forwarded with `%6f` still an escape, and the
  bridge decoded it again into `/otel/v1/traces`, a loopback-only route. The
  edge now encodes the path it checked, so the upstream decodes it back to
  exactly that path.
* Unbounded buffering: with fallback upstreams (a gateway with two or more
  bridges) the edge read the whole request body into memory before contacting
  any bridge. Now at most `_RETRY_BODY_LIMIT` bytes are held for a retry; a
  longer body streams to the first upstream.
"""
from __future__ import annotations

import asyncio
import http.client
from urllib.parse import unquote, urlsplit

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from hubzoid import edge
from tests.test_edge import _free_port, _Server


# ---------------------------------------------------------------------------
# the path the upstream sees
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [
    "/%6ftel/v1/traces", "/%76%31/chat/completions", "/api/files/100%.pdf", "/api/files/50%25off.pdf",
    "/api/files/café report.pdf", "/a?b#c/d", "/a[b]{c}|d^e`f\\g\"h<i>", "/a'(b)*+,;=:@!$&~-._",
    "//double//slashes/", "/�",
])
def test_the_upstream_decodes_the_path_the_edge_checked(path):
    forwarded = edge._upstream_path(path)
    assert forwarded.isascii() and unquote(forwarded) == path
    # httpx keeps the encoding as it is, so the upstream decodes this path once.
    assert httpx.Request("GET", "http://bridge" + forwarded).url.path == path


def _bridge_app() -> Starlette:
    async def internal(request: Request):
        return PlainTextResponse("INTERNAL:" + request.url.path)

    async def any_path(request: Request):
        return PlainTextResponse("BRIDGE:" + request.scope["path"], status_code=404)

    return Starlette(routes=[
        Route("/otel/v1/traces", internal, methods=["POST"]),
        Route("/v1/chat/completions", internal, methods=["POST"]),
        Route("/uploads/{rest:path}", internal),
        Route("/{path:path}", any_path, methods=["GET", "POST"]),
    ])


@pytest.fixture(scope="module")
def web_app_edge():
    bport, eport = _free_port(), _free_port()
    bridge = _Server(_bridge_app(), bport)
    app = edge.build_edge_app(
        default_base=f"http://127.0.0.1:{bport}",
        routes=[edge.EdgeRoute("/b/sales/api", f"http://127.0.0.1:{bport}", "/b/sales")],
        web_app=True)
    front = _Server(app, eport)
    bridge.start()
    front.start()
    try:
        yield f"http://127.0.0.1:{eport}"
    finally:
        front.stop()
        bridge.stop()


def _raw(base: str, method: str, path: str) -> tuple[int, str]:
    """A request sent exactly as written (no client-side normalization)."""
    u = urlsplit(base)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    try:
        conn.request(method, path, body=b"{}" if method == "POST" else None)
        r = conn.getresponse()
        return r.status, r.read().decode()
    finally:
        conn.close()


@pytest.mark.parametrize("method,path,seen", [
    ("POST", "/%256ftel/v1/traces", "/%6ftel/v1/traces"),
    ("POST", "/%2576%2531/chat/completions", "/%76%31/chat/completions"),
    ("GET", "/%2575ploads/c1/x.txt", "/%75ploads/c1/x.txt"),
    ("POST", "/%25%36%66tel/v1/traces", "/%6ftel/v1/traces"),
])
def test_double_encoding_cannot_reach_the_bridges_internal_api(web_app_edge, method, path, seen):
    assert _raw(web_app_edge, method, unquote(unquote(path)))[0] == 404   # refused by the edge
    status, body = _raw(web_app_edge, method, path)
    assert "INTERNAL" not in body
    assert (status, body) == (404, "BRIDGE:" + seen)


@pytest.mark.parametrize("path,seen", [
    ("/b/sales/api/%2561gents", "/api/%61gents"),               # stripped, still not decoded twice
    ("/b/sales/api/files/100%25.pdf", "/api/files/100%.pdf"),   # a literal % in a file name
    ("/b/sales/api/files/50%2525off.pdf", "/api/files/50%25off.pdf"),
    ("/b/sales/api/files/caf%C3%A9%20menu.pdf", "/api/files/café menu.pdf"),
    ("/b/sales/api/a%3Fb", "/api/a"),                           # the edge checked /api/a too
])
def test_routes_forward_the_path_they_matched(web_app_edge, path, seen):
    assert _raw(web_app_edge, "GET", path) == (404, "BRIDGE:" + seen)


def test_websockets_relay_the_path_they_checked():
    """Websockets (Open WebUI's socket.io) take the same path encoding."""
    import websockets
    from starlette.routing import WebSocketRoute

    async def named(ws):
        await ws.accept()
        await ws.send_text("WS:" + ws.scope["path"])
        await ws.close()

    oport, eport = _free_port(), _free_port()
    owui = _Server(Starlette(routes=[WebSocketRoute("/ws", named), WebSocketRoute("/{p:path}", named)]), oport)
    front = _Server(edge.build_edge_app(default_base=f"http://127.0.0.1:{oport}", web_app=False), eport)
    owui.start()
    front.start()

    async def ask(path):
        async with websockets.connect(f"ws://127.0.0.1:{eport}{path}", open_timeout=10) as ws:
            return await ws.recv()

    try:
        assert asyncio.run(ask("/ws")) == "WS:/ws"
        assert asyncio.run(ask("/%2577s")) == "WS:/%77s"
    finally:
        front.stop()
        owui.stop()


# ---------------------------------------------------------------------------
# request bodies with fallback upstreams
# ---------------------------------------------------------------------------
class _Upstreams(httpx.AsyncBaseTransport):
    """Upstream bridges. Records, per request, how many body bytes the edge had
    read from the client when it contacted the bridge."""

    def __init__(self, pulled, refuse=()):
        self.pulled, self.refuse, self.seen = pulled, set(refuse), []

    async def handle_async_request(self, request):
        at_dispatch = self.pulled()
        if request.url.host in self.refuse:
            raise httpx.ConnectError("refused")
        body = b"".join([chunk async for chunk in request.stream])
        self.seen.append((request.url.host, at_dispatch, body))
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"))


def _post(chunks: list[bytes], *, refuse=(), declared: int | None = None, fallbacks=("http://bridge2",)):
    """POST /api/auth/login through an edge with fallbacks, the body arriving
    in `chunks` (chunked, or with a declared Content-Length)."""
    pulled = {"bytes": 0}
    upstreams = _Upstreams(lambda: pulled["bytes"], refuse)
    app = edge.build_edge_app(default_base="http://bridge1", default_fallbacks=fallbacks, web_app=True)
    headers = [(b"host", b"hub.example.org")]
    if declared is not None:
        headers.append((b"content-length", str(declared).encode()))
    queue = list(chunks)
    sent: list[dict] = []

    async def receive():
        if not queue:
            await asyncio.Event().wait()   # the client stays connected
        chunk = queue.pop(0)
        pulled["bytes"] += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": bool(queue)}

    async def send(message):
        sent.append(message)

    async def go():
        app.state.client = httpx.AsyncClient(transport=upstreams)
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/api/auth/login",
                 "raw_path": b"/api/auth/login", "root_path": "", "query_string": b"", "headers": headers,
                 "client": ("203.0.113.9", 4171), "server": ("127.0.0.1", 4170)}
        try:
            await app(scope, receive, send)
        finally:
            await app.state.client.aclose()

    asyncio.run(go())
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, upstreams.seen


MIB = 1024 * 1024


@pytest.mark.parametrize("declared", [None, 8 * MIB])
def test_a_large_body_is_not_buffered_before_a_bridge_is_contacted(declared):
    chunks = [b"x" * 65536] * 128                      # 8 MiB, unauthenticated
    status, seen = _post(chunks, declared=declared)
    assert status == 200
    (host, at_dispatch, body), = seen
    assert host == "bridge1" and len(body) == 8 * MIB  # all of it reaches the bridge
    assert at_dispatch <= edge._RETRY_BODY_LIMIT + 65536


def test_a_large_body_goes_to_the_first_bridge_only():
    chunks = [b"x" * 65536] * 4
    status, seen = _post(chunks, refuse={"bridge1"})
    assert status == 502 and seen == []


@pytest.mark.parametrize("declared", [None, 25])
def test_a_small_body_still_moves_to_the_next_bridge(declared):
    status, seen = _post([b'{"email":', b' "a@example.org"}'], refuse={"bridge1"}, declared=declared)
    assert status == 200
    assert [(h, b) for h, _, b in seen] == [("bridge2", b'{"email": "a@example.org"}')]


def test_without_fallbacks_the_body_streams_as_before():
    chunks = [b"y" * 65536] * 32
    status, seen = _post(chunks, fallbacks=())
    (host, at_dispatch, body), = seen
    assert status == 200 and host == "bridge1" and len(body) == 32 * 65536
    assert at_dispatch == 0
