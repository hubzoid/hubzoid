"""Where a remote MCP server's authorization lives.

The MCP authorization spec, as a client sees it:

  1. Call the server without a token. A protected server answers 401 with
     ``WWW-Authenticate: Bearer resource_metadata="..."`` (and maybe ``scope``).
  2. Protected Resource Metadata (RFC 9728): the URL from that header, else the
     path-aware and root ``/.well-known/oauth-protected-resource`` URLs. Its
     ``resource`` must cover the server URL; it names the authorization servers.
  3. Authorization Server Metadata (RFC 8414), or OpenID configuration, for
     the first authorization server that publishes it. Its ``issuer`` must be
     the identifier the metadata URL was built from.

The ``mcp`` SDK supplies the URL builders, header parsing and metadata models.
This module adds what a multi-user server needs on top: every URL checked
against the connector URL and address rules (``http.py``: a remote server may
not send Hubzoid into the deployment's private network), bounded responses and
one deadline for the whole discovery, no redirects, and one result object that
the flow, the Console test and the refresh path share. Nothing here writes
state.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from . import ConnectorError
from . import http as net

log = logging.getLogger("hubzoid.connectors")


def _shown(url: str) -> str:
    """A URL for a log line: no query or fragment, which may carry a key."""
    try:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}{parts.path}"
    except ValueError:
        return "<invalid URL>"


@dataclass
class Probe:
    """What the MCP endpoint said to a call without a token."""

    status: int | None
    resource_metadata: str | None = None
    scope: str | None = None

    @property
    def requires_auth(self) -> bool | None:
        if self.status is None:
            return None
        if self.status == 401:
            return True
        if 200 <= self.status < 300:
            return False
        return None


@dataclass
class Discovery:
    server_url: str
    probe: Probe
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    revocation_endpoint: str | None
    # RFC 8707 resource indicator to send, or None when the server published no
    # protected resource metadata (a pre-MCP authorization server).
    resource: str | None
    resource_metadata_url: str | None
    iss_supported: bool
    pkce: str  # "S256" (advertised) | "assumed" (not advertised; S256 is still used)
    scopes_supported: list[str] | None
    token_auth_methods: list[str] | None
    prm_scopes: list[str] | None = None
    notes: list[str] = field(default_factory=list)

    def default_scope(self) -> str | None:
        """The MCP scope-selection rule: the 401's ``scope``, else every scope
        the resource metadata lists, else none (the server's default)."""
        if self.probe.scope:
            return self.probe.scope
        if self.prm_scopes:
            return " ".join(self.prm_scopes)
        return None

    def summary(self) -> dict:
        """Secret-free facts for the Console's discovery test."""
        return {
            "requires_auth": self.probe.requires_auth,
            "resource": self.resource,
            "resource_metadata_url": self.resource_metadata_url,
            "issuer": self.issuer,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "registration_endpoint": self.registration_endpoint,
            "revocation_endpoint": self.revocation_endpoint,
            "iss_parameter_supported": self.iss_supported,
            "pkce": self.pkce,
            "scopes_supported": self.scopes_supported,
            "default_scope": self.default_scope(),
            "token_endpoint_auth_methods": self.token_auth_methods,
            "notes": list(self.notes),
        }


def _protocol_version() -> str:
    try:
        from mcp.types import LATEST_PROTOCOL_VERSION

        return LATEST_PROTOCOL_VERSION
    except Exception:  # noqa: BLE001
        return "2025-06-18"


def probe(c: httpx.Client, url: str) -> Probe:
    """POST an ``initialize`` without credentials and read the challenge.
    Raises ConnectorError ``unreachable`` when nothing answers."""
    from mcp.client.auth.utils import (
        extract_resource_metadata_from_www_auth,
        extract_scope_from_www_auth,
    )

    version = _protocol_version()
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {},
                       "clientInfo": {"name": "Hubzoid", "version": "1"}}}
    try:
        with c.stream("POST", url, json=body, headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": version}) as r:
            # Never read a successful (possibly streaming) body: only the status
            # and the challenge matter here.
            result = Probe(status=r.status_code)
            if r.status_code == 401:
                result.resource_metadata = extract_resource_metadata_from_www_auth(r)
                result.scope = extract_scope_from_www_auth(r)
            session = r.headers.get("mcp-session-id")
    except httpx.HTTPError as exc:
        log.info("connectors: %s did not answer (%s)", _shown(url), type(exc).__name__)
        raise ConnectorError("unreachable", "The server could not be reached. Check the URL "
                                            "and that the server is running.", 502) from None
    if session:
        # A server that needs no sign-in opened a session for our probe: close
        # it. The answer is never read: only that the DELETE was sent matters.
        try:
            with c.stream("DELETE", url, headers={"mcp-session-id": session,
                                                  "MCP-Protocol-Version": version},
                          timeout=5.0):
                pass
        except httpx.HTTPError:
            pass
    return result


def _refused(refused: list[str], what: str, url: str, exc: BaseException) -> None:
    """Record a request the address rule refused, for the final error."""
    while exc is not None and not isinstance(exc, net.AddressRefused):
        exc = exc.__cause__ or exc.__context__
    if exc is not None:
        log.info("connectors: not fetching %s (address rule)", _shown(url))
        refused.append(net.refusal(what, exc.host, exc.reason))


def _resource_metadata(c: httpx.Client, url: str, pr: Probe, refused: list[str]):
    """(raw PRM dict, parsed model, the URL it came from) or (None, None, None)."""
    from mcp.client.auth.utils import build_protected_resource_metadata_discovery_urls
    from mcp.shared.auth import ProtectedResourceMetadata
    from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url

    hinted = pr.resource_metadata
    what = "The resource metadata URL"
    for candidate in build_protected_resource_metadata_discovery_urls(hinted, url):
        try:
            candidate = net.check_url(candidate, what=what, base=url)
        except ConnectorError as err:
            log.info("connectors: ignoring resource metadata URL %s (URL rule)", _shown(candidate))
            if err.code == "private_address":
                refused.append(err.message)
            continue
        try:
            status, raw = net.get_json(c, candidate)
        except httpx.HTTPError as exc:
            _refused(refused, what, candidate, exc)
            continue
        if status != 200 or raw is None:
            continue
        try:
            prm = ProtectedResourceMetadata.model_validate(raw)
        except ValidationError:
            log.info("connectors: invalid resource metadata at %s", _shown(candidate))
            continue
        published = raw.get("resource") if isinstance(raw.get("resource"), str) else str(prm.resource)
        if not check_resource_allowed(requested_resource=resource_url_from_server_url(url),
                                      configured_resource=published):
            raise ConnectorError(
                "resource_mismatch",
                "The server's metadata describes a different resource than this URL. "
                "Check the URL, including its path.", 502)
        return raw, prm, candidate
    return None, None, None


def _authorization_metadata(c: httpx.Client, url: str, as_url: str | None, refused: list[str]):
    """(raw dict, parsed model) for ``as_url`` (or the server origin, the
    pre-2025-06 location), or (None, None)."""
    from mcp.client.auth.utils import build_oauth_authorization_server_metadata_discovery_urls
    from mcp.shared.auth import OAuthMetadata

    what = "The authorization server"
    for candidate in build_oauth_authorization_server_metadata_discovery_urls(as_url, url):
        try:
            candidate = net.check_url(candidate, what=what, base=url)
        except ConnectorError as err:
            if err.code == "private_address":
                refused.append(err.message)
            continue
        try:
            status, raw = net.get_json(c, candidate)
        except httpx.HTTPError as exc:
            _refused(refused, what, candidate, exc)
            continue
        if status >= 500:
            break
        if status != 200 or raw is None:
            continue
        try:
            meta = OAuthMetadata.model_validate(raw)
        except ValidationError:
            log.info("connectors: invalid authorization server metadata at %s", _shown(candidate))
            continue
        issuer = raw.get("issuer") if isinstance(raw.get("issuer"), str) else str(meta.issuer)
        # RFC 8414 section 3.3: the issuer must be the identifier the metadata
        # URL was built from. Without resource metadata, the server origin.
        if as_url is not None:
            if issuer.rstrip("/") != as_url.rstrip("/"):
                raise ConnectorError("issuer_mismatch", "The authorization server's metadata "
                                     "names a different issuer than it was found under.", 502)
        elif issuer.rstrip("/") != net.origin(url):
            # Found at the server's own origin, so the issuer is that origin.
            raise ConnectorError("issuer_mismatch", "The authorization server's metadata "
                                 "names a different issuer than this server.", 502)
        return raw, meta
    return None, None


def discover(url: str, *, c: httpx.Client | None = None) -> Discovery:
    """Everything needed to authorize a person with the server at ``url``.
    Raises ConnectorError with a message an administrator can act on."""
    url = net.check_url(url)
    own = c is None
    c = c or net.client(url)
    refused: list[str] = []
    try:
        pr = probe(c, url)
        prm_raw, prm, prm_url = _resource_metadata(c, url, pr, refused)
        notes: list[str] = []
        servers = [str(s) for s in (prm.authorization_servers if prm else [])]
        if prm_raw is not None and isinstance(prm_raw.get("authorization_servers"), list):
            servers = [s for s in prm_raw["authorization_servers"] if isinstance(s, str)] or servers
        raw = meta = None
        issuer = None
        for as_url in servers or [None]:
            if as_url is not None:
                try:
                    as_url = net.check_url(as_url, what="The authorization server URL", base=url)
                except ConnectorError as err:
                    if err.code == "private_address":
                        refused.append(err.message)
                    continue
            raw, meta = _authorization_metadata(c, url, as_url, refused)
            if meta is not None:
                issuer = raw.get("issuer") if isinstance(raw.get("issuer"), str) else str(meta.issuer)
                break
        if meta is None:
            if refused:
                # The likely cause, and what an administrator can do about it.
                raise ConnectorError("private_address", refused[0], 502)
            if pr.requires_auth is False and prm is None:
                raise ConnectorError("no_authorization", "This server does not ask for sign-in. "
                                     "Register it with authentication set to none.", 409)
            raise ConnectorError("no_authorization_metadata", "The server does not publish "
                                 "OAuth authorization server metadata, so Hubzoid cannot "
                                 "connect people to it.", 502)
        endpoints = {}
        for name in ("authorization_endpoint", "token_endpoint", "registration_endpoint",
                     "revocation_endpoint"):
            value = raw.get(name) if isinstance(raw.get(name), str) else getattr(meta, name, None)
            endpoints[name] = (net.check_url(str(value), what=f"The {name.replace('_', ' ')}",
                                             base=url) if value else None)
        methods = meta.code_challenge_methods_supported
        if methods and "S256" not in methods:
            raise ConnectorError("pkce_unsupported", "The authorization server does not "
                                 "support PKCE with S256, which Hubzoid requires.", 502)
        if not methods:
            notes.append("The authorization server does not list its PKCE methods; Hubzoid "
                         "uses S256 anyway.")
        resource = None
        if prm is not None:
            resource = (prm_raw.get("resource") if isinstance(prm_raw.get("resource"), str)
                        else str(prm.resource))
        else:
            notes.append("No protected resource metadata: the resource parameter is not sent.")
        return Discovery(
            server_url=url, probe=pr, issuer=issuer,
            authorization_endpoint=endpoints["authorization_endpoint"],
            token_endpoint=endpoints["token_endpoint"],
            registration_endpoint=endpoints["registration_endpoint"],
            revocation_endpoint=endpoints["revocation_endpoint"],
            resource=resource, resource_metadata_url=prm_url,
            iss_supported=raw.get("authorization_response_iss_parameter_supported") is True,
            pkce="S256" if methods else "assumed",
            scopes_supported=meta.scopes_supported,
            token_auth_methods=meta.token_endpoint_auth_methods_supported,
            prm_scopes=(prm.scopes_supported if prm else None),
            notes=notes,
        )
    finally:
        if own:
            c.close()
