"""Outbound HTTP for connectors: one client shape, URL rules, bounded JSON and
OAuth client authentication.

URL rule (connector URLs and every endpoint discovered from them): HTTPS, or
plain HTTP to a loopback host for local development. An endpoint discovered
from a remote server (its metadata, its 401 challenge) may not point at this
machine or at link-local, unspecified, multicast or reserved addresses (cloud
metadata services live there), unless the connector itself is on loopback.
Names are not resolved: a server registered by an administrator is trusted to
name its own authorization server, including one on a private network.
Redirects are never followed: metadata and token endpoints answer directly.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import re
import time
from urllib.parse import quote, urlsplit

import httpx

from .. import appmode
from . import ConnectorError

TIMEOUT = httpx.Timeout(15.0, connect=5.0)
MAX_JSON_BYTES = 512 * 1024
MAX_URL_LENGTH = 2048

# Tests may point every connector request at an in-process transport.
_transport: httpx.BaseTransport | None = None

_CONTROL = re.compile(r"[\x00-\x20\x7f]")


def user_agent() -> str:
    try:
        from importlib.metadata import version

        return f"Hubzoid/{version('hubzoid')}"
    except Exception:  # noqa: BLE001 — an uninstalled checkout
        return "Hubzoid"


def client(timeout: httpx.Timeout | float | None = None) -> httpx.Client:
    return httpx.Client(timeout=timeout or TIMEOUT, follow_redirects=False,
                        transport=_transport, headers={"User-Agent": user_agent()})


def is_loopback_url(url: str) -> bool:
    try:
        return appmode.is_loopback_host(urlsplit(url).hostname)
    except ValueError:
        return False


def _local_or_reserved(host: str) -> bool:
    """This machine by name, or a literal address no remote endpoint has."""
    name = host.strip("[]").lower().rstrip(".")
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast
            or ip.is_reserved)


def check_url(url, *, what: str = "The server URL", base: str | None = None) -> str:
    """``url`` stripped, or ConnectorError ``invalid_url``.

    ``base`` is the connector URL an endpoint was discovered from: loopback HTTP
    is then allowed only when ``base`` is on loopback too.
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
    if base is not None and not is_loopback_url(base) and _local_or_reserved(host):
        raise ConnectorError("invalid_url", f"{what} points at a local or reserved address, "
                                            "which a remote server may not send Hubzoid to.", 422)
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
