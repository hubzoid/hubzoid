"""Edge router — the single public front door for `hubzoid run` / `gateway`.

The default upstream is a bridge (the hub's own, or a gateway's first bridge,
with the others as fallbacks), which serves the Hubzoid web app. Routes send
`/b/<slug>/...` to that hub's bridge with the prefix stripped and
`X-Forwarded-Prefix: /b/<slug>` added. The edge streams (SSE-safe), strips
client-sent `X-Hubzoid-*` / `X-OpenWebUI-*` identity headers, refuses dot
segments, never keeps cookies between visitors and asserts the public scheme.
The bridge's internal API (`/v1`, `/uploads`, `/otel`) stays loopback-only:
the edge answers 404 for it.

An upstream receives exactly the path the edge checked: the decoded path is
encoded again, so `%25` escapes are never decoded twice (`/%256ftel/...`
reaches a bridge as `/%6ftel/...`, not `/otel/...`). With fallback upstreams,
only a small request body is held in memory for a retry on the next upstream;
a larger one streams to the first.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from urllib.parse import quote
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

log = logging.getLogger("hubzoid.edge")

# Hop-by-hop headers must not be forwarded across a proxy (RFC 7230 §6.1).
# `content-length` is dropped on the response because we re-stream the body
# and let the server frame it (chunked), avoiding a length mismatch.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})

# Identity headers are set by trusted callers on loopback (the Slack and
# inbound adapters) when they call a bridge. `X-OpenWebUI-*` is the 1.x name,
# still stripped so an old client can never assert an identity. A client on the public port has
# no business sending them, so the edge drops them before forwarding anything.
_IDENTITY_PREFIXES = ("x-hubzoid-", "x-openwebui-")

# The bridge's own API for Hubzoid processes on loopback. Never public: the
# edge's default upstream is a bridge, so these are refused.
_BRIDGE_INTERNAL = ("/v1", "/uploads", "/otel")
_FORWARDED_PREFIX = "x-forwarded-prefix"

# Client-address headers. The edge decides the client's address itself and
# sends the bridge only that (``X-Forwarded-For``), so a client can't pick the
# address the sign-in limits count.
_CLIENT_ADDRESS = frozenset({"x-forwarded-for", "x-real-ip", "forwarded"})


def _trusted_proxies() -> list:
    """Proxies whose ``X-Forwarded-For`` the edge believes:
    ``HUBZOID_TRUSTED_PROXIES`` (addresses or networks, comma-separated, or
    ``*`` when the public port is reachable only through your proxy), plus
    loopback. Without the setting, private networks are trusted too (a proxy
    on the same LAN); set it to narrow that. A CDN in front (Cloudflare) must
    be listed, or every person counts as one of its few addresses."""
    raw = (os.environ.get("HUBZOID_TRUSTED_PROXIES") or "").strip()
    if raw == "*":
        return ["*"]
    nets = []
    for item in [x.strip() for x in raw.split(",") if x.strip()]:
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            log.warning("edge: HUBZOID_TRUSTED_PROXIES entry %r is not an address or network; ignored", item)
    return nets


def _trusted(addr: str, nets: list) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if nets == ["*"] or ip.is_loopback:
        return True
    if nets:  # an explicit list is the whole list (besides loopback)
        return any(ip in n for n in nets)
    return ip.is_private or ip.is_link_local


def client_address(request: Request) -> str | None:
    """The client's address: the connecting peer, or, when the peer is a
    trusted proxy, the nearest address in ``X-Forwarded-For`` that isn't one."""
    peer = request.client.host if request.client else None
    if not peer:
        return None
    nets = _trusted_proxies()
    if not _trusted(peer, nets):
        return peer
    chain = [a.strip() for v in request.headers.getlist("x-forwarded-for") for a in v.split(",") if a.strip()]
    for addr in reversed(chain):
        if not _trusted(addr, nets):
            return addr
    return chain[0] if chain else peer

# What a path may carry unescaped (RFC 3986 pchar and "/"). Everything else,
# "%" included, is percent-encoded on the way to an upstream.
_PATH_SAFE = "/:@!$&'()*+,;="

# With fallback upstreams (a gateway's other bridges), a body of up to this
# many bytes is held so a refused connection can be retried on the next one. A
# longer body streams to the first upstream only: the edge never buffers a
# request without bound, before any sign-in or endpoint limit has run.
_RETRY_BODY_LIMIT = 64 * 1024


def _upstream_path(path: str) -> str:
    """`path` as the edge checked it (decoded) encoded for the upstream URL, so
    the upstream's one decoding gives back exactly that path. Forwarding the
    decoded path as it was let an escaped escape be decoded twice:
    `/%256ftel/v1/traces` passed the checks as `/%6ftel/v1/traces` and reached
    the bridge as `/otel/v1/traces`."""
    return quote(path, safe=_PATH_SAFE)


def _path_and_query(request: Request) -> tuple[str, str]:
    """The decoded path and the `?query` suffix as the server split them.
    `request.url.path` is not that path: Starlette 1.9 re-escapes `?`, `#` and
    `\\` in it, so `/a%3Fb` was encoded again and forwarded as `/a%253Fb`, and
    earlier releases cut it at a decoded `?`."""
    query = request.scope.get("query_string", b"").decode()
    return request.scope["path"], "?" + query if query else ""


async def _retry_body(request: Request):
    """(content, retryable) for a request that may be retried on a fallback.

    A body of at most `_RETRY_BODY_LIMIT` bytes is read whole (a refused
    connection delivered nothing, so it can be sent again). A longer one,
    declared or found while reading a chunked body, is forwarded as a stream of
    what was read followed by the rest, to one upstream only."""
    declared = request.headers.get("content-length", "").strip()
    stream = request.stream()
    if declared.isdigit() and int(declared) > _RETRY_BODY_LIMIT:
        return stream, False
    held: list[bytes] = []
    size = 0
    async for chunk in stream:
        held.append(chunk)
        size += len(chunk)
        if size > _RETRY_BODY_LIMIT:
            return _chain(held, stream), False
    return b"".join(held), True


async def _chain(held: list[bytes], rest):
    for chunk in held:
        yield chunk
    async for chunk in rest:
        yield chunk


def _clean_path(path: str) -> str:
    """Collapse repeated slashes and drop a trailing one, for matching only."""
    path = re.sub(r"/{2,}", "/", path)
    return path.rstrip("/") or "/"


def _is_bridge_internal(path: str) -> bool:
    path = _clean_path(path)
    return any(path == p or path.startswith(p + "/") for p in _BRIDGE_INTERNAL)


# The OpenAI-compatible chat API is public only for a person's own API key
# (``Authorization: Bearer hzp_...``): the bridge checks the key and runs the
# call as its owner. The bridge's own keys stay internal.
_PERSONAL_KEY_PATHS = ("/v1/chat/completions", "/v1/models")


def _personal_key(request) -> bool:
    auth = (request.headers.get("authorization") or "").strip()
    return auth[:7].lower() == "bearer " and auth[7:].strip().startswith("hzp_")


def _personal_key_call(request, path: str | None = None) -> bool:
    """A person's API key on the chat API: bare ``/v1/...`` (the first agent)
    or, behind a gateway, ``/b/<agent>/v1/...`` after its prefix is removed."""
    return (_clean_path(request.url.path if path is None else path) in _PERSONAL_KEY_PATHS
            and _personal_key(request))


def _has_dot_segment(path: str) -> bool:
    """`.` or `..` path segments. The prefix checks below run on the path as
    sent, while the upstream client collapses dot segments, so
    `/b/x/artifacts/../v1/chat/completions` would pass the artifact route and
    arrive as `/v1/chat/completions`. Browsers never send them."""
    return any(seg in (".", "..") for seg in path.split("/"))


# The artifact path on a single bridge. The single-hub topology routes exactly
# this prefix to the bridge; the gateway prepends a per-hub prefix.
DEFAULT_ARTIFACT_PREFIX = "/artifacts"


@dataclass(frozen=True)
class EdgeRoute:
    """One prefix-routing rule: paths under `prefix` go to `upstream`.

    `strip_prefix` is removed from the path before forwarding — used by the
    gateway so `/b/<hub>/artifacts/...` reaches the bridge as `/artifacts/...`.
    `fallbacks` are tried in order when `upstream` cannot be reached. The
    gateway lists every bridge for `/portal`, since they share one database.
    """
    prefix: str
    upstream: str
    strip_prefix: str = ""
    fallbacks: tuple[str, ...] = ()

    def upstreams(self) -> tuple[str, ...]:
        return (self.upstream, *self.fallbacks)


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
    removed); an unmatched path goes to the default upstream unchanged.
    """
    route = _match(path, routes)
    if route is None:
        return default_base, path
    fwd = path
    if route.strip_prefix and path.startswith(route.strip_prefix):
        fwd = path[len(route.strip_prefix):] or "/"
    return route.upstream, fwd


def _request_headers(
    request: Request, public_scheme: str = "", *, forwarded_prefix: str = "",
) -> list[tuple[bytes, bytes]]:
    """Forward the client's headers upstream, minus hop-by-hop and identity headers.

    The inbound `Host` is PRESERVED (standard reverse-proxy behaviour). The
    edge is the public front door, so a bridge must build absolute URLs -
    notably the OAuth `redirect_uri` - from the real public host, not the
    loopback upstream it is dialled on. httpx keeps an explicit `Host` header
    instead of deriving one from the connection target, so passing it through
    is enough.

    `public_scheme` (e.g. "https", derived from the operator's declared public
    URL) is asserted as `X-Forwarded-Proto` only when the request carries none.
    An inbound `X-Forwarded-Proto` from a fronting TLS proxy always wins, and
    with no declared scheme nothing is added, so plain-http localhost is
    untouched.

    Client-address headers are replaced by one `X-Forwarded-For`: the address
    the edge believes (`client_address`).

    `forwarded_prefix` drops a client-sent `X-Forwarded-Prefix` and, when
    non-empty, sends the prefix the edge stripped (`/b/<slug>`), so a bridge
    knows the call is hub-scoped.
    """
    headers = [
        (k, v)
        for k, v in request.headers.raw
        if k.decode("latin-1").lower() not in _HOP_BY_HOP
        and not k.decode("latin-1").lower().startswith(_IDENTITY_PREFIXES)
    ]
    headers = [(k, v) for k, v in headers
               if k.decode("latin-1").lower() != _FORWARDED_PREFIX
               and k.decode("latin-1").lower() not in _CLIENT_ADDRESS]
    client = client_address(request)
    if client:
        headers.append((b"x-forwarded-for", client.encode("latin-1")))
    if forwarded_prefix:
        headers.append((_FORWARDED_PREFIX.encode("latin-1"),
                        forwarded_prefix.encode("latin-1")))
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
    default_fallbacks: tuple[str, ...] | list[str] = (),
    workflow_hubs: tuple[str, ...] | list[str] = (),
) -> Starlette:
    """A Starlette reverse proxy: `routes` go to their bridge, the rest to the
    default upstream (a bridge).

    Args:
        default_base: the default upstream, e.g. "http://127.0.0.1:43080".
            Receives every path not matched by a route.
        routes: prefix rules sending hub paths to the right bridge.
        public_scheme: the operator's public scheme ("https"), asserted as
            `X-Forwarded-Proto` when the inbound request carries none. Empty
            (the default) leaves the scheme untouched - correct for localhost.
        default_fallbacks: upstreams tried in order when the default one can't
            be reached (a gateway's other bridges, which share its database).
    """
    default_base = default_base.rstrip("/")
    norm_routes = tuple(
        EdgeRoute(r.prefix, r.upstream.rstrip("/"), r.strip_prefix,
                  tuple(f.rstrip("/") for f in r.fallbacks)) for r in routes
    )
    default_bases = (default_base, *(f.rstrip("/") for f in default_fallbacks))

    @asynccontextmanager
    async def lifespan(app: Starlette):
        # No read timeout: SSE / long LLM streams must not be cut off.
        # One client serves every visitor, so it must never keep cookies: httpx
        # would store an upstream Set-Cookie (a sign-in token) and send it with
        # the next request that has no Cookie of its own. A raw CookieJar whose
        # policy allows no domain stores nothing (httpx.Cookies would copy it
        # into a default jar and lose the policy).
        app.state.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
            follow_redirects=False,
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
        )
        monitor_task = None
        app.state.workflow_health = {str(h): False for h in workflow_hubs}
        if workflow_hubs:
            from .workflows.monitor import poll
            monitor_task = asyncio.create_task(poll(workflow_hubs, app.state.workflow_health))
        try:
            yield
        finally:
            if monitor_task is not None:
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass
            await app.state.client.aclose()

    async def http_handler(request: Request) -> Response:
        path, query = _path_and_query(request)
        if path == '/healthz/workflows' and request.method in ('GET', 'HEAD'):
            states = getattr(request.app.state, 'workflow_health', {})
            healthy = bool(states) and all(states.values())
            return JSONResponse({'ok': healthy, 'checked': len(states)}, status_code=200 if healthy else 503)
        if _has_dot_segment(path):
            return Response("Bad request", status_code=400)
        upstream, fwd_path = _forward_target(path, norm_routes, default_base)
        # The bridge's internal routes stay internal however they're spelled:
        # as sent, and after a /b/<agent> prefix is removed.
        for checked in {path, fwd_path}:
            if _is_bridge_internal(checked) and not _personal_key_call(request, checked):
                if _clean_path(checked) in _PERSONAL_KEY_PATHS:
                    return JSONResponse({"error": {"message": "Send your personal API key: "
                                                   "Authorization: Bearer hzp_...", "type": "auth"}},
                                        status_code=401)
                return Response("Not found", status_code=404)
        matched = _match(path, norm_routes)
        bases = matched.upstreams() if matched is not None else default_bases
        # Tell a bridge which /b/<slug> prefix was stripped (and never pass a
        # client's own claim on).
        prefix = matched.strip_prefix if matched is not None and matched.strip_prefix else ""

        client: httpx.AsyncClient = request.app.state.client
        # With fallbacks, hold a small body so a refused connection can be
        # retried on the next bridge. A larger body goes to the first only.
        # A read that breaks before the answer starts (a busy bridge dropping
        # the connection) is retried once for GET and HEAD, which change
        # nothing; anything else answers 502 instead of an unhandled error.
        # Either way the body is read once, here, and kept for a resend.
        idempotent = request.method in ("GET", "HEAD")
        if len(bases) > 1:
            content, retryable = await _retry_body(request)
            if not retryable:
                bases = bases[:1]
                idempotent = False  # a large body streams once, never twice
        elif idempotent:
            content = await request.body()
        else:
            content = request.stream()
        resp = None
        for i, base in enumerate(bases):
            for attempt in (0, 1):
                upstream_req = client.build_request(
                    request.method,
                    base + _upstream_path(fwd_path) + query,
                    headers=_request_headers(request, public_scheme, forwarded_prefix=prefix),
                    content=content,
                )
                try:
                    resp = await client.send(upstream_req, stream=True)
                    break
                except httpx.ConnectError:
                    break  # try the next bridge, if any
                except httpx.TransportError as exc:
                    if idempotent and attempt == 0:
                        log.info("edge: retrying %s %s after %s", request.method, path, type(exc).__name__)
                        continue
                    log.warning("edge: upstream failed for %s %s (%s)", request.method, path, type(exc).__name__)
                    return Response("upstream error", status_code=502)
            if resp is not None:
                break
        if resp is None:
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

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/{path:path}", http_handler, methods=[
                "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
            ]),
        ],
    )
    return app


def _redact_oauth_callback_logs() -> None:
    """The edge's access log sees the same addresses as the bridge: one-time
    sign-in links (``/auth/set-password?token=``, ``/api/auth/link/<token>``),
    sign-in callbacks and connection callbacks all carry single-use secrets.
    Install the same redaction the bridge uses; never fail to start over it."""
    try:
        from .auth import logredact

        logredact.install()
    except Exception:  # noqa: BLE001 - logging hygiene must not stop the front door
        log.warning("edge: could not install sign-in log redaction", exc_info=True)
    try:
        from .connectors import routes as connector_routes
    except ImportError:
        return
    redact = getattr(connector_routes, "redact_oauth_callback_logs", None)
    if redact is None:
        return
    try:
        redact()
    except Exception:  # noqa: BLE001 — logging hygiene must not stop the front door
        log.warning("edge: could not install OAuth callback log redaction", exc_info=True)


def _factory() -> Starlette:
    """uvicorn factory: ``uvicorn hubzoid.edge:_factory --factory``.

    Reads the routing table from the environment so `hubzoid run` / `gateway`
    launch it the same way they launch the bridge:

      HUBZOID_EDGE_DEFAULT        The default upstream (catch-all): a bridge.
      HUBZOID_EDGE_DEFAULT_FALLBACKS  JSON list of upstreams tried when the
                                  default can't be reached (optional).
      HUBZOID_EDGE_ROUTES         JSON: [{"prefix","upstream","strip_prefix","fallbacks"}, ...].
      HUBZOID_EDGE_PUBLIC_SCHEME  Public scheme ("https") asserted upstream as
                                  X-Forwarded-Proto when the request has none.
    """
    default_base = os.environ.get("HUBZOID_EDGE_DEFAULT")
    if not default_base:
        raise RuntimeError("edge factory needs HUBZOID_EDGE_DEFAULT in the environment.")
    _redact_oauth_callback_logs()
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
            fallbacks=tuple(r.get("fallbacks", ())),
        )
        for r in spec
    ]
    public_scheme = os.environ.get("HUBZOID_EDGE_PUBLIC_SCHEME", "").strip().lower()
    kwargs = {"workflow_hubs": json.loads(os.environ.get("HUBZOID_EDGE_WORKFLOW_HUBS", "[]"))}
    raw_fallbacks = os.environ.get("HUBZOID_EDGE_DEFAULT_FALLBACKS", "").strip()
    if raw_fallbacks:
        try:
            fallbacks = json.loads(raw_fallbacks)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"HUBZOID_EDGE_DEFAULT_FALLBACKS is not valid JSON: {exc}") from exc
        kwargs["default_fallbacks"] = tuple(str(f) for f in fallbacks)
    return build_edge_app(
        default_base=default_base, routes=routes, public_scheme=public_scheme, **kwargs
    )
