"""Edge router — the single public front door for `hubzoid run` / `gateway`.

The reverse proxy / load balancer in front of a hub points at ONE port
(Open WebUI's `PORT`, default 3080). But artifact download links are served
by the FastAPI bridge on a different, loopback-only port (`BRIDGE_PORT`,
default 8000), and Open WebUI has no `/artifacts` route — so a browser
clicking a download link would hit Open WebUI and 404. That is the report-
download bug.

The fix, with zero operator setup: hubzoid binds the public port itself with
a tiny path-router and moves Open WebUI to a loopback port. A small routing
table sends specific path prefixes to the bridge; everything else (the UI,
including websockets) goes to Open WebUI.

  * single hub (`hubzoid run`):   `/artifacts/*` -> the bridge.
  * gateway (`hubzoid gateway`):  `/b/<hub>/artifacts/*` -> that hub's bridge
                                  (one shared OWUI fronts many bridges, so
                                  each hub gets its own artifact prefix).

**Only artifact paths are exposed off a bridge.** `/v1`, `/uploads` and
`/healthz` stay loopback — Open WebUI reaches the bridge directly over
127.0.0.1, and exposing `/v1` would hand the model to anyone with the key,
bypassing Open WebUI's auth/RBAC.

The router streams responses (SSE-safe) and transparently relays websockets
(Open WebUI uses socket.io for live updates). Pure-Python (Starlette + httpx
+ websockets, all already hubzoid deps).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import websockets
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

log = logging.getLogger("hubzoid.edge")

# Hop-by-hop headers must not be forwarded across a proxy (RFC 7230 §6.1).
# `content-length` is dropped on the response because we re-stream the body
# and let the server frame it (chunked), avoiding a length mismatch.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})

# The artifact path on a single bridge. The single-hub topology routes exactly
# this prefix to the bridge; the gateway prepends a per-hub prefix.
DEFAULT_ARTIFACT_PREFIX = "/artifacts"


@dataclass(frozen=True)
class EdgeRoute:
    """One prefix-routing rule: paths under `prefix` go to `upstream`.

    `strip_prefix` is removed from the path before forwarding — used by the
    gateway so `/b/<hub>/artifacts/...` reaches the bridge as `/artifacts/...`.
    """
    prefix: str
    upstream: str
    strip_prefix: str = ""


def _match(path: str, routes: tuple[EdgeRoute, ...]) -> EdgeRoute | None:
    for r in routes:
        if path == r.prefix or path.startswith(r.prefix + "/"):
            return r
    return None


def _forward_target(
    path: str, routes: tuple[EdgeRoute, ...], default_base: str
) -> tuple[str, str]:
    """Resolve (upstream_base, forward_path) for an incoming path.

    A matched route sends the path to its bridge (with `strip_prefix`
    removed); an unmatched path goes to Open WebUI unchanged.
    """
    route = _match(path, routes)
    if route is None:
        return default_base, path
    fwd = path
    if route.strip_prefix and path.startswith(route.strip_prefix):
        fwd = path[len(route.strip_prefix):] or "/"
    return route.upstream, fwd


def _request_headers(
    request: Request, public_scheme: str = ""
) -> list[tuple[bytes, bytes]]:
    """Forward the client's headers upstream, minus hop-by-hop.

    The inbound `Host` is PRESERVED (standard reverse-proxy behaviour). The
    edge is the public front door, so Open WebUI must build absolute URLs -
    notably the OAuth `redirect_uri` - from the real public host, not the
    loopback upstream it is dialled on. httpx keeps an explicit `Host` header
    instead of deriving one from the connection target, so passing it through
    is enough; dropping it made OWUI see `127.0.0.1:<port>` and produce a
    `redirect_uri` the IdP rejects.

    `public_scheme` (e.g. "https", derived from the operator's declared public
    URL) is asserted as `X-Forwarded-Proto` only when the request carries none.
    An inbound `X-Forwarded-Proto` from a fronting TLS proxy always wins, and
    with no declared scheme nothing is added, so plain-http localhost is
    untouched. OWUI's uvicorn honours `X-Forwarded-Proto` from loopback.
    """
    headers = [
        (k, v)
        for k, v in request.headers.raw
        if k.decode("latin-1").lower() not in _HOP_BY_HOP
    ]
    if public_scheme and not any(
        k.decode("latin-1").lower() == "x-forwarded-proto" for k, _ in headers
    ):
        headers.append((b"x-forwarded-proto", public_scheme.encode("latin-1")))
    return headers


def _response_headers(resp: httpx.Response) -> dict[str, str]:
    """Response headers as a dict, EXCLUDING `set-cookie`.

    A dict collapses duplicate keys, and httpx's `.items()` comma-folds them -
    which is illegal for `Set-Cookie` (RFC 6265 §5.2) and silently loses every
    cookie but the first. The OAuth login callback sets several at once
    (`token`, `oauth_session_id`, ...), so `set-cookie` is excluded here and
    re-appended individually by the handler via `multi_items()`.
    """
    return {
        k: v
        for k, v in resp.headers.items()
        if k.lower() not in _HOP_BY_HOP
        and k.lower() != "content-length"
        and k.lower() != "set-cookie"
    }


def build_edge_app(
    *,
    default_base: str,
    routes: tuple[EdgeRoute, ...] | list[EdgeRoute] = (),
    public_scheme: str = "",
) -> Starlette:
    """A Starlette reverse proxy: `routes` go to their bridge, the rest to OWUI.

    Args:
        default_base: Open WebUI base, e.g. "http://127.0.0.1:43080". Receives
            every path not matched by a route, plus all websockets.
        routes: prefix rules sending artifact paths to the right bridge.
        public_scheme: the operator's public scheme ("https"), asserted as
            `X-Forwarded-Proto` when the inbound request carries none. Empty
            (the default) leaves the scheme untouched - correct for localhost.
    """
    default_base = default_base.rstrip("/")
    norm_routes = tuple(
        EdgeRoute(r.prefix, r.upstream.rstrip("/"), r.strip_prefix) for r in routes
    )
    owui_ws_base = "ws://" + default_base.split("://", 1)[-1]

    @asynccontextmanager
    async def lifespan(app: Starlette):
        # No read timeout: SSE / long LLM streams must not be cut off.
        app.state.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
            follow_redirects=False,
        )
        try:
            yield
        finally:
            await app.state.client.aclose()

    async def http_handler(request: Request) -> Response:
        upstream, fwd_path = _forward_target(request.url.path, norm_routes, default_base)
        url = upstream + fwd_path
        if request.url.query:
            url += "?" + request.url.query

        client: httpx.AsyncClient = request.app.state.client
        upstream_req = client.build_request(
            request.method,
            url,
            headers=_request_headers(request, public_scheme),
            content=request.stream(),
        )
        try:
            resp = await client.send(upstream_req, stream=True)
        except httpx.ConnectError:
            return Response("upstream unavailable", status_code=502)

        sr = StreamingResponse(
            resp.aiter_raw(),
            status_code=resp.status_code,
            headers=_response_headers(resp),
            background=BackgroundTask(resp.aclose),
        )
        # Re-emit every Set-Cookie individually: a dict (used above) can hold
        # only one, but the OAuth login callback sets several at once.
        for k, v in resp.headers.multi_items():
            if k.lower() == "set-cookie":
                sr.raw_headers.append((b"set-cookie", v.encode("latin-1")))
        return sr

    async def ws_handler(websocket: WebSocket) -> None:
        # Only Open WebUI uses websockets (socket.io); bridges don't. Relay
        # every websocket to OWUI verbatim.
        target = owui_ws_base + websocket.url.path
        if websocket.url.query:
            target += "?" + websocket.url.query
        await websocket.accept()
        try:
            upstream = await websockets.connect(
                target, open_timeout=10, ping_interval=None, max_size=None
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("edge ws connect failed: %s", exc)
            await websocket.close()
            return
        try:
            await _relay_ws(websocket, upstream)
        finally:
            await upstream.close()

    app = Starlette(
        lifespan=lifespan,
        routes=[
            WebSocketRoute("/{path:path}", ws_handler),
            Route("/{path:path}", http_handler, methods=[
                "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
            ]),
        ],
    )
    return app


def _factory() -> Starlette:
    """uvicorn factory: ``uvicorn hubzoid.edge:_factory --factory``.

    Reads the routing table from the environment so `hubzoid run` / `gateway`
    launch it the same way they launch the bridge:

      HUBZOID_EDGE_DEFAULT        Open WebUI base URL (catch-all + websockets).
      HUBZOID_EDGE_ROUTES         JSON: [{"prefix","upstream","strip_prefix"}, ...].
      HUBZOID_EDGE_PUBLIC_SCHEME  Public scheme ("https") asserted upstream as
                                  X-Forwarded-Proto when the request has none.
    """
    default_base = os.environ.get("HUBZOID_EDGE_DEFAULT")
    if not default_base:
        raise RuntimeError("edge factory needs HUBZOID_EDGE_DEFAULT in the environment.")
    raw = os.environ.get("HUBZOID_EDGE_ROUTES", "[]")
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"HUBZOID_EDGE_ROUTES is not valid JSON: {exc}") from exc
    routes = [
        EdgeRoute(
            prefix=r["prefix"],
            upstream=r["upstream"],
            strip_prefix=r.get("strip_prefix", ""),
        )
        for r in spec
    ]
    public_scheme = os.environ.get("HUBZOID_EDGE_PUBLIC_SCHEME", "").strip().lower()
    return build_edge_app(
        default_base=default_base, routes=routes, public_scheme=public_scheme
    )


async def _relay_ws(client_ws: WebSocket, upstream) -> None:
    """Pump messages both directions until either side closes."""

    async def client_to_upstream() -> None:
        try:
            while True:
                msg = await client_ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
        except (WebSocketDisconnect, websockets.ConnectionClosed):
            return

    async def upstream_to_client() -> None:
        try:
            async for message in upstream:
                if isinstance(message, (bytes, bytearray)):
                    await client_ws.send_bytes(bytes(message))
                else:
                    await client_ws.send_text(message)
        except (websockets.ConnectionClosed, RuntimeError):
            return

    t1 = asyncio.ensure_future(client_to_upstream())
    t2 = asyncio.ensure_future(upstream_to_client())
    _, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    try:
        await client_ws.close()
    except RuntimeError:
        pass
