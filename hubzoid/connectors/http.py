"""Outbound HTTP for connectors: one client shape, the URL and address rules,
one deadline per operation, bounded JSON and OAuth client authentication.

URL rule (connector URLs and every endpoint discovered from them): HTTPS, or
plain HTTP to a loopback host for local development. Redirects are never
followed: metadata and token endpoints answer directly.

Address rule. An administrator registers the MCP server's URL. Everything else
Hubzoid fetches for a connector (resource and authorization server metadata,
the registration, token and revocation endpoints) is named by that remote
server, so it may not send Hubzoid into the deployment's own network:

  * a public address is always fine;
  * a private, loopback, link-local or other non-public address only on the
    registered server's own host (an internal MCP server answering for its own
    authorization), anywhere on this computer when the server is on this
    computer (development), or on a host an administrator lists in
    ``HUBZOID_CONNECTOR_PRIVATE_HOSTS``: host names or addresses separated by
    commas, for example an internal identity provider on another host;
  * a cloud metadata or credential service (169.254.169.254, fd00:ec2::254,
    100.100.100.200 and the others in ``METADATA_ADDRESSES``), however it is
    written: never, not even when listed.

Every connection a connector client opens resolves the name once, checks every
address it gets (one forbidden address refuses the host), and connects only to
those addresses, so a name that answers differently a moment later changes
nothing. A literal address in a discovered URL is refused when it is read
(``check_url``) with a message an administrator can act on. Through an
outbound proxy from the environment (``HTTPS_PROXY``) the proxy connects: the
addresses the name resolves to here are checked, and a name that does not
resolve here is left to the proxy and its own rules.

Deadline: everything one client does (``DEADLINE`` seconds unless the caller
sets another) is bounded as a whole, so a server answering a byte at a time
cannot hold discovery, an exchange or a refresh lease open past it.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import socket
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpcore
import httpx

from .. import appmode
from . import ConnectorError

TIMEOUT = httpx.Timeout(15.0, connect=5.0)
DEADLINE = 30.0  # seconds for everything one client does (one operation)
MAX_JSON_BYTES = 512 * 1024
MAX_URL_LENGTH = 2048
PRIVATE_HOSTS_ENV = "HUBZOID_CONNECTOR_PRIVATE_HOSTS"

# Cloud metadata and credential services: never a connector endpoint.
METADATA_ADDRESSES = frozenset(ipaddress.ip_address(a) for a in (
    "169.254.169.254",  # AWS, Azure, Google Cloud, Oracle, DigitalOcean, OpenStack, others
    "169.254.170.2",    # AWS ECS task credentials
    "169.254.170.23",   # AWS EKS Pod Identity
    "100.100.100.200",  # Alibaba Cloud
    "168.63.129.16",    # Azure platform (WireServer)
    "192.0.0.192",      # Oracle Cloud (legacy)
    "fd00:ec2::254",    # AWS, IPv6
    "fd00:ec2::23",     # AWS EKS Pod Identity, IPv6
))
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

# Tests may point every connector request at an in-process transport.
_transport: httpx.BaseTransport | None = None

_CONTROL = re.compile(r"[\x00-\x20\x7f]")


def user_agent() -> str:
    try:
        from importlib.metadata import version

        return f"Hubzoid/{version('hubzoid')}"
    except Exception:  # noqa: BLE001 — an uninstalled checkout
        return "Hubzoid"


def client(connector_url: str | None = None, *, timeout: httpx.Timeout | float | None = None,
           deadline: float | None = None) -> httpx.Client:
    """A client for one operation for the connector registered at
    ``connector_url`` (the address rule allows that server's own host).
    ``deadline``: seconds for everything the client does, ``DEADLINE`` by
    default. Use a new client per operation."""
    kwargs = {"timeout": timeout or TIMEOUT, "follow_redirects": False,
              "headers": {"User-Agent": user_agent()}}
    if _transport is not None:
        return httpx.Client(transport=_transport, **kwargs)
    rule = rule_for(connector_url)
    until = time.monotonic() + (DEADLINE if deadline is None else deadline)
    return httpx.Client(transport=_Transport(_Backend(rule, until)), mounts=_proxies(rule),
                        **kwargs)


def is_loopback_url(url: str) -> bool:
    try:
        return appmode.is_loopback_host(urlsplit(url).hostname)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# The address rule
# ---------------------------------------------------------------------------
class AddressRefused(httpcore.ConnectError):
    """A connection the address rule forbids (an httpx ``ConnectError`` to
    callers). ``reason``: ``metadata``, ``private`` or ``unresolved``."""

    def __init__(self, host: str, reason: str):
        super().__init__(f"{host}: {reason} address refused")
        self.host = host
        self.reason = reason


def _host(value: str | None) -> str:
    """A host as connections name it: lower case, no brackets, zone or
    trailing dot, international names in their ASCII form."""
    h = (value or "").strip().strip("[]").rstrip(".").lower()
    if "%" in h:
        h = h.split("%", 1)[0]
    try:
        return str(ipaddress.ip_address(h))
    except ValueError:
        pass
    try:
        return h.encode("idna").decode("ascii")
    except UnicodeError:
        return h


def _forms(value) -> list:
    """An address and any IPv4 address it carries (mapped, 6to4, NAT64)."""
    try:
        ip = ipaddress.ip_address(str(value).strip("[]").split("%", 1)[0])
    except ValueError:
        return []
    out = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour
        if inner is None and ip in _NAT64:
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if inner is not None:
            out.append(inner)
    return out


def _public(ip) -> bool:
    return ip.is_global and not ip.is_multicast


def private_hosts(env=None) -> frozenset[str]:
    """The hosts ``HUBZOID_CONNECTOR_PRIVATE_HOSTS`` lists (names, addresses or
    URLs; commas or spaces between them)."""
    raw = (env if env is not None else os.environ).get(PRIVATE_HOSTS_ENV) or ""
    out = set()
    for item in re.split(r"[\s,]+", raw):
        if "://" in item:
            try:
                item = urlsplit(item).hostname or ""
            except ValueError:
                continue
        if item.strip():
            out.add(_host(item))
    return frozenset(out)


@dataclass(frozen=True)
class Rule:
    """Where requests for one connector may go (see the module docstring)."""

    own_host: str | None    # the registered server's host
    own_loopback: bool      # ... which is this computer
    allowed: frozenset[str]  # HUBZOID_CONNECTOR_PRIVATE_HOSTS

    def check(self, host: str, addresses) -> None:
        """Raise AddressRefused unless ``host``, resolving to ``addresses``,
        may be reached."""
        name = _host(host)
        forms = [_forms(a) for a in addresses]
        if not forms or not all(forms):
            raise AddressRefused(name, "unresolved")
        if any(ip in METADATA_ADDRESSES for f in forms for ip in f):
            raise AddressRefused(name, "metadata")
        if all(_public(ip) for f in forms for ip in f):
            return
        if name == self.own_host or name in self.allowed:
            return
        if self.own_loopback and all(any(ip.is_loopback for ip in f) for f in forms):
            return
        raise AddressRefused(name, "private")


def rule_for(connector_url: str | None, env=None) -> Rule:
    own = None
    if connector_url:
        try:
            own = _host(urlsplit(connector_url).hostname) or None
        except ValueError:
            own = None
    return Rule(own_host=own, own_loopback=bool(own) and appmode.is_loopback_host(own),
                allowed=private_hosts(env))


def refused_host(exc: BaseException | None) -> str | None:
    """The host an httpx error was refused for by the address rule, else None."""
    seen = 0
    while exc is not None and seen < 10:
        if isinstance(exc, AddressRefused):
            return exc.host
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return None


def refusal(what: str, host: str, reason: str = "private") -> str:
    """A sentence for an administrator about a refused address."""
    if reason == "metadata":
        return (f"{what} points at {host}, a cloud metadata address. Hubzoid never sends "
                "requests there.")
    return (f"{what} points at {host}, a private or local network address that the server "
            f"may not send Hubzoid to. If it is your organization's own service, add {host} "
            f"to {PRIVATE_HOSTS_ENV}.")


def _literal_addresses(host: str) -> list[str] | None:
    """The addresses a host stands for without asking DNS: a literal address,
    or this computer for ``localhost`` names. None for any other name."""
    name = _host(host)
    if _forms(name):
        return [name]
    if name == "localhost" or name.endswith(".localhost"):
        return ["127.0.0.1"]
    return None


def _resolve(host: str, port: int | None) -> list[str]:
    """Every address ``host`` resolves to, in the resolver's order."""
    out: list[str] = []
    for family, _type, _proto, _name, sockaddr in socket.getaddrinfo(
            host, port or 443, type=socket.SOCK_STREAM):
        if family in (socket.AF_INET, socket.AF_INET6) and sockaddr[0] not in out:
            out.append(sockaddr[0])
    return out


# ---------------------------------------------------------------------------
# Connections: the rule on resolved addresses, and one deadline
# ---------------------------------------------------------------------------
class _Backend(httpcore.NetworkBackend):
    """Opens connections only to checked addresses, within the deadline."""

    def __init__(self, rule: Rule, until: float):
        self.rule = rule
        self.until = until
        self._inner = httpcore.SyncBackend()

    def bounded(self, timeout: float | None, expired: type[Exception]) -> float:
        left = self.until - time.monotonic()
        if left <= 0:
            raise expired("the operation took too long")
        return left if timeout is None else min(timeout, left)

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.bounded(timeout, httpcore.ConnectTimeout)
        try:
            addresses = _resolve(host, port)
        except OSError as exc:
            raise httpcore.ConnectError(f"{host} could not be resolved") from exc
        self.rule.check(host, addresses)
        error: Exception = httpcore.ConnectError(f"{host} has no address")
        for address in addresses:
            try:
                stream = self._inner.connect_tcp(
                    address, port, timeout=self.bounded(timeout, httpcore.ConnectTimeout),
                    local_address=local_address, socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                error = exc
                continue
            return _Stream(stream, self)
        raise error

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("connectors do not use Unix sockets")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class _Stream(httpcore.NetworkStream):
    def __init__(self, inner, backend: _Backend):
        self._inner = inner
        self._backend = backend

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._inner.read(max_bytes, self._backend.bounded(timeout, httpcore.ReadTimeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._inner.write(buffer, self._backend.bounded(timeout, httpcore.WriteTimeout))

    def close(self) -> None:
        self._inner.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        tls = self._inner.start_tls(ssl_context, server_hostname,
                                    self._backend.bounded(timeout, httpcore.ConnectTimeout))
        return _Stream(tls, self._backend)

    def get_extra_info(self, info: str):
        return self._inner.get_extra_info(info)


class _Transport(httpx.HTTPTransport):
    """httpx's transport, with every connection opened by ``_Backend``."""

    def __init__(self, backend: _Backend):
        ssl_context = httpx.create_ssl_context()
        super().__init__(verify=ssl_context)
        self._pool = httpcore.ConnectionPool(ssl_context=ssl_context, max_connections=10,
                                             max_keepalive_connections=10, keepalive_expiry=5.0,
                                             network_backend=backend)


class _ProxiedTransport(httpx.HTTPTransport):
    """An outbound proxy from the environment: the proxy connects, so the
    target is checked here on the addresses its name resolves to."""

    def __init__(self, rule: Rule, proxy: str):
        super().__init__(proxy=proxy)
        self._rule = rule

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        try:
            addresses = _resolve(host, request.url.port)
        except OSError:
            addresses = _literal_addresses(host)  # the proxy resolves the rest
        if addresses is not None:
            try:
                self._rule.check(host, addresses)
            except AddressRefused as exc:
                raise httpx.ConnectError(str(exc), request=request) from exc
        return super().handle_request(request)


def _proxies(rule: Rule) -> dict:
    """httpx's proxy mounts from the environment, each target checked."""
    from httpx._utils import get_environment_proxies

    return {pattern: (None if proxy is None else _ProxiedTransport(rule, proxy))
            for pattern, proxy in get_environment_proxies().items()}


def check_url(url, *, what: str = "The server URL", base: str | None = None) -> str:
    """``url`` stripped, or ConnectorError ``invalid_url``.

    ``base`` is the connector URL an endpoint was discovered from: loopback HTTP
    is then allowed only when ``base`` is on loopback too, and a literal address
    must pass the address rule (ConnectorError ``private_address``). Names are
    checked when connecting, on the addresses they resolve to.
    """
    if not isinstance(url, str) or not url.strip():
        raise ConnectorError("invalid_url", f"{what} is missing.", 422)
    url = url.strip()
    if len(url) > MAX_URL_LENGTH or _CONTROL.search(url):
        raise ConnectorError("invalid_url", f"{what} is not a valid URL.", 422)
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 — raises ValueError for a malformed port
    except ValueError:
        raise ConnectorError("invalid_url", f"{what} is not a valid URL.", 422) from None
    if parts.scheme not in ("https", "http") or not host:
        raise ConnectorError("invalid_url", f"{what} must start with https://.", 422)
    if parts.username is not None or parts.password is not None:
        raise ConnectorError("invalid_url", f"{what} must not contain a user name or password.", 422)
    if parts.fragment:
        raise ConnectorError("invalid_url", f"{what} must not contain a fragment (#).", 422)
    literal = _literal_addresses(host) if base is not None else None
    if literal is not None:
        try:
            rule_for(base).check(host, literal)
        except AddressRefused as exc:
            raise ConnectorError("private_address", refusal(what, exc.host, exc.reason),
                                 422) from None
    if parts.scheme == "http":
        local = appmode.is_loopback_host(host)
        if not local or (base is not None and not is_loopback_url(base)):
            raise ConnectorError(
                "invalid_url", f"{what} must use https:// (plain http is allowed only on "
                               "this computer, for development).", 422)
    return url


def origin(url: str) -> str:
    return appmode.normalize_origin(url)


def read_json(response: httpx.Response, deadline: float | None = None) -> dict | None:
    """The JSON object in a (streamed) response, at most MAX_JSON_BYTES, else
    None. ``deadline`` (``time.monotonic()``) bounds the whole read: a server
    trickling bytes cannot hold the request open past it."""
    size = 0
    chunks: list[bytes] = []
    for chunk in response.iter_bytes():
        if deadline is not None and time.monotonic() > deadline:
            raise httpx.ReadTimeout("response took too long", request=response.request)
        size += len(chunk)
        if size > MAX_JSON_BYTES:
            return None
        chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks) or b"null")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def get_json(c: httpx.Client, url: str, headers: dict | None = None) -> tuple[int, dict | None]:
    """(status, JSON object or None). Network failures raise httpx errors."""
    with c.stream("GET", url, headers={"Accept": "application/json", **(headers or {})}) as r:
        if r.status_code != 200:
            return r.status_code, None
        return r.status_code, read_json(r)


def post_json(c: httpx.Client, url: str, *, data: dict | None = None, json_body: dict | None = None,
              headers: dict | None = None, deadline: float | None = None) -> tuple[int, dict | None]:
    """POST a form (``data``) or JSON body; (status, JSON object or None)."""
    kwargs: dict = {"headers": {"Accept": "application/json", **(headers or {})}}
    if json_body is not None:
        kwargs["json"] = json_body
    else:
        kwargs["data"] = data or {}
    with c.stream("POST", url, **kwargs) as r:
        if deadline is not None and time.monotonic() > deadline:
            raise httpx.ReadTimeout("response took too long", request=r.request)
        return r.status_code, read_json(r, deadline)


def oauth_error(body: dict | None) -> str:
    """The provider's ``error`` code from an OAuth error body, reduced to a safe
    token for logs and messages (never ``error_description``, which may echo input)."""
    raw = body.get("error") if isinstance(body, dict) else None
    if not isinstance(raw, str):
        return "unknown"
    return re.sub(r"[^a-z0-9_]", "", raw.lower())[:40] or "unknown"


def client_auth(client_info: dict, data: dict) -> tuple[dict, dict]:
    """Add the client's credentials to a token or revocation request, per its
    ``auth_method``: HTTP Basic (RFC 6749 section 2.3.1, form-encoded parts),
    the form body, or none (public client with PKCE). Returns (data, headers)."""
    data = dict(data)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    client_id = client_info.get("client_id") or ""
    secret = client_info.get("client_secret") or ""
    method = client_info.get("auth_method") or ("client_secret_basic" if secret else "none")
    data["client_id"] = client_id
    if method == "client_secret_basic" and secret:
        pair = f"{quote(client_id, safe='')}:{quote(secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    elif method == "client_secret_post" and secret:
        data["client_secret"] = secret
    return data, headers
