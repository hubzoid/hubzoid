"""The connector registry: remote MCP servers an administrator offers
(``hz_connectors``, in the shared operational store).

A connector is a slug id (also the app of its ``connector_<id>`` capability), a
display name, the server URL, how it signs in (``oauth``: each person with their
own account, ``shared``: one company key sent as a header, ``none``), an
optional client registered with the provider in advance (its secret encrypted
with the deployment key), scopes to request, and an optional tool allow-list.
A client Hubzoid registers dynamically (RFC 7591) is cached here too, encrypted,
one per redirect URI, so every person reuses it.

Secrets never leave this module in a listing: ``Connector.public()`` says only
whether a secret exists. Every change writes one row to the access audit log
(``hz_access_audit``) in the same transaction, like other Console changes.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text

from .. import secretbox
from ..access.identity import normalize
from . import ConnectorError, capability, engine
from . import http as net

log = logging.getLogger("hubzoid.connectors")

AUTH_TYPES = ("oauth", "none", "shared")
DEFAULT_SHARED_HEADER = "Authorization"
_HEADER_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
_SHARED_KEY_RE = re.compile(r"^[\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?$")  # a header value
# Headers a shared key may never set: they would change the request itself
# (routing, framing, cookies, proxies) or the MCP protocol's own headers.
_FORBIDDEN_HEADERS = frozenset({
    "host", "cookie", "set-cookie", "content-length", "content-type", "transfer-encoding",
    "connection", "keep-alive", "proxy-authorization", "proxy-authenticate",
    "proxy-connection", "te", "trailer", "upgrade", "accept", "accept-encoding",
    "mcp-session-id", "mcp-protocol-version", "last-event-id", "forwarded",
    "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip",
})
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
_TOOL_RE = re.compile(r"^[A-Za-z0-9_.\-/]{1,128}$")
_SCOPE_TOKEN_RE = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]{1,200}$")
_NAME_MAX = 80
_CLIENT_ID_MAX = 512
_SECRET_MAX = 2048
_MAX_TOOLS = 200
_MAX_SCOPES = 50
_FIELDS = {"id", "name", "url", "auth_type", "client_id", "client_secret", "scopes",
           "tool_allowlist", "enabled", "shared_header", "shared_secret"}
_COLUMNS = ("id", "name", "url", "auth_type", "client_id", "client_secret_enc",
            "client_info_enc", "scopes", "tool_allowlist", "enabled", "created_by",
            "created_at", "updated_at", "shared_header", "shared_secret_enc")
_SELECT = "SELECT " + ", ".join(_COLUMNS) + " FROM hz_connectors"

ORG = "*"  # the access audit's organization-wide scope (access.store.ORG)


@dataclass(frozen=True)
class Connector:
    id: str
    name: str
    url: str
    auth_type: str
    client_id: str | None
    has_client_secret: bool
    scopes: str | None
    tool_allowlist: tuple[str, ...] | None
    enabled: bool
    created_by: str | None
    created_at: float
    updated_at: float
    registered: bool  # a dynamically registered client is cached
    shared_header: str | None = None  # Shared key: the header it is sent in
    has_shared_secret: bool = False

    @property
    def capability(self) -> str:
        return capability(self.id)

    def public(self) -> dict:
        """The registry entry for the Console. Never a secret."""
        return {
            "id": self.id, "name": self.name, "url": self.url, "auth_type": self.auth_type,
            "client_id": self.client_id, "has_client_secret": self.has_client_secret,
            "scopes": self.scopes,
            "tool_allowlist": list(self.tool_allowlist) if self.tool_allowlist else None,
            "enabled": self.enabled, "capability": self.capability,
            "dynamic_client": self.registered, "created_by": self.created_by,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "shared_header": self.shared_header, "has_shared_secret": self.has_shared_secret,
        }


def _row(r) -> dict:
    return dict(zip(_COLUMNS, r))


def _connector(row: dict) -> Connector:
    allow = None
    if row.get("tool_allowlist"):
        try:
            parsed = json.loads(row["tool_allowlist"])
            allow = tuple(str(t) for t in parsed) if isinstance(parsed, list) and parsed else None
        except ValueError:
            # A damaged allow-list must not silently widen to every tool.
            allow = ("",)
    return Connector(
        id=row["id"], name=row["name"], url=row["url"], auth_type=row["auth_type"] or "oauth",
        client_id=row["client_id"] or None, has_client_secret=bool(row["client_secret_enc"]),
        scopes=row["scopes"] or None, tool_allowlist=allow, enabled=bool(row["enabled"]),
        created_by=row["created_by"], created_at=float(row["created_at"] or 0),
        updated_at=float(row["updated_at"] or 0), registered=bool(row["client_info_enc"]),
        shared_header=row.get("shared_header") or None,
        has_shared_secret=bool(row.get("shared_secret_enc")),
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    if slug and not slug[0].isalpha():
        slug = "c_" + slug
    return slug[:40].rstrip("_")


def check_id(value) -> str:
    cid = value.strip().lower() if isinstance(value, str) else ""
    if not ID_RE.match(cid):
        raise ConnectorError("invalid_id", "Use 2 to 40 lowercase letters, digits or _ for the "
                             "ID, starting with a letter (for example gmail).", 422)
    return cid


def _name(value) -> str:
    name = " ".join(value.split()) if isinstance(value, str) else ""
    if not name or len(name) > _NAME_MAX:
        raise ConnectorError("invalid_name", f"Enter a name of at most {_NAME_MAX} characters.", 422)
    return name


def _auth_type(value) -> str:
    auth = value.strip().lower() if isinstance(value, str) else ""
    if auth not in AUTH_TYPES:
        raise ConnectorError("invalid_auth_type", "Sign-in must be oauth, shared or none.", 422)
    return auth


def _check_shared_key(key: str) -> None:
    if not _SHARED_KEY_RE.match(key):
        raise ConnectorError("invalid_shared_secret", "A key uses printable ASCII characters "
                             "only.", 422)


def _shared_header(value) -> str:
    """A header name a shared key may be sent in (default ``Authorization``)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_SHARED_HEADER
    name = value.strip() if isinstance(value, str) else ""
    if not _HEADER_RE.match(name) or name.lower() in _FORBIDDEN_HEADERS:
        raise ConnectorError("invalid_shared_header", "Use a header such as Authorization or "
                             "X-API-Key. This one can't carry a key.", 422)
    return name


def _optional_text(value, *, code: str, what: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConnectorError(code, f"{what} must be text.", 422)
    value = value.strip()
    if not value:
        return None
    if len(value) > limit or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ConnectorError(code, f"{what} is not valid.", 422)
    return value


def _scopes(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = [s for s in re.split(r"[\s,]+", value) if s]
    elif isinstance(value, (list, tuple)) and all(isinstance(s, str) for s in value):
        items = [s.strip() for s in value if s.strip()]
    else:
        raise ConnectorError("invalid_scopes", "Scopes must be text separated by spaces.", 422)
    if len(items) > _MAX_SCOPES or any(not _SCOPE_TOKEN_RE.match(s) for s in items):
        raise ConnectorError("invalid_scopes", "A scope may not contain spaces, quotes or "
                             "backslashes.", 422)
    seen: list[str] = []
    for s in items:
        if s not in seen:
            seen.append(s)
    return " ".join(seen) or None


def _allowlist(value) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = [s for s in re.split(r"[\s,]+", value) if s]
    elif isinstance(value, (list, tuple)) and all(isinstance(s, str) for s in value):
        items = [s.strip() for s in value if s.strip()]
    else:
        raise ConnectorError("invalid_tool_allowlist", "The tool allow-list must be a list of "
                             "tool names.", 422)
    if len(items) > _MAX_TOOLS or any(not _TOOL_RE.match(s) for s in items):
        raise ConnectorError("invalid_tool_allowlist", "Tool names use letters, digits and "
                             "_ . - / only.", 422)
    out: list[str] = []
    for s in items:
        if s not in out:
            out.append(s)
    return out or None


def _enabled(value) -> bool:
    if not isinstance(value, bool):
        raise ConnectorError("invalid_enabled", "enabled must be true or false.", 422)
    return value


def _body(data) -> dict:
    if not isinstance(data, dict):
        raise ConnectorError("invalid_request", "Send the connector as a JSON object.", 422)
    unknown = sorted(set(data) - _FIELDS)
    if unknown:
        raise ConnectorError("invalid_request", "Unknown field: " + ", ".join(unknown[:5]) + ".", 422)
    return data


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def _get_row(conn, cid: str) -> dict | None:
    r = conn.execute(text(_SELECT + " WHERE id = :id"), {"id": cid}).fetchone()
    return _row(r) if r else None


def get(hub_dir, cid: str) -> Connector | None:
    if not isinstance(cid, str) or not ID_RE.match(cid):
        return None
    with engine(hub_dir).connect() as conn:
        row = _get_row(conn, cid)
    return _connector(row) if row else None


def list_all(hub_dir) -> list[Connector]:
    with engine(hub_dir).connect() as conn:
        rows = [_row(r) for r in conn.execute(text(_SELECT + " ORDER BY name, id")).fetchall()]
    return [_connector(r) for r in rows]


def shared_headers(hub_dir, cid: str) -> dict | None:
    """The request header of a Shared key connector, decrypted, or None. A key
    sent as ``Authorization`` without a scheme is sent as a Bearer token."""
    with engine(hub_dir).connect() as conn:
        r = conn.execute(text("SELECT auth_type, shared_header, shared_secret_enc FROM "
                              "hz_connectors WHERE id = :id"), {"id": cid}).fetchone()
    if not r or r[0] != "shared" or not r[2]:
        return None
    try:
        key = secretbox.decrypt_text(Path(hub_dir), r[2])
    except secretbox.SecretKeyError:
        log.error("connectors: the shared key of %s cannot be decrypted with the deployment key",
                  cid)
        return None
    header = r[1] or DEFAULT_SHARED_HEADER
    if header.lower() == "authorization" and " " not in key.strip():
        key = f"Bearer {key.strip()}"
    return {header: key}


def client_secret(hub_dir, cid: str) -> str | None:
    """The pre-registered client secret, decrypted, or None."""
    with engine(hub_dir).connect() as conn:
        r = conn.execute(text("SELECT client_secret_enc FROM hz_connectors WHERE id = :id"),
                         {"id": cid}).fetchone()
    if not r or not r[0]:
        return None
    try:
        return secretbox.decrypt_text(Path(hub_dir), r[0])
    except secretbox.SecretKeyError:
        log.error("connectors: the client secret of %s cannot be decrypted with the deployment "
                  "key", cid)
        raise ConnectorError("secret_unreadable", "This connector's client secret cannot be read "
                             "with this deployment's key. An administrator can enter it again.",
                             500) from None


# ---------------------------------------------------------------------------
# Dynamically registered clients (RFC 7591), one per redirect URI
# ---------------------------------------------------------------------------
def _registrations(hub_dir, raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = secretbox.decrypt_json(Path(hub_dir), raw)
    except (secretbox.SecretKeyError, ValueError):
        log.warning("connectors: a registered client cannot be decrypted; registering again")
        return {}
    clients = value.get("clients") if isinstance(value, dict) else None
    return clients if isinstance(clients, dict) else {}


def registration(hub_dir, cid: str, redirect_uri: str) -> dict | None:
    """The cached dynamic client for ``redirect_uri``, or None."""
    with engine(hub_dir).connect() as conn:
        r = conn.execute(text("SELECT client_info_enc FROM hz_connectors WHERE id = :id"),
                         {"id": cid}).fetchone()
    if not r:
        return None
    found = _registrations(hub_dir, r[0]).get(redirect_uri)
    return found if isinstance(found, dict) else None


def save_registration(hub_dir, cid: str, redirect_uri: str, client: dict, *,
                      attempts: int = 5) -> dict:
    """Cache ``client`` for ``redirect_uri`` unless another process cached one
    first (compare-and-set on the stored value). Returns the client to use."""
    eng = engine(hub_dir)
    for _ in range(attempts):
        with eng.begin() as conn:
            r = conn.execute(text("SELECT client_info_enc FROM hz_connectors WHERE id = :id"),
                             {"id": cid}).fetchone()
            if not r:
                raise ConnectorError("not_found", "This connector no longer exists.", 404)
            current = r[0]
            clients = _registrations(hub_dir, current)
            existing = clients.get(redirect_uri)
            if isinstance(existing, dict) and existing.get("issuer") == client.get("issuer") \
                    and existing.get("scope") == client.get("scope") and not _expired(existing):
                return existing
            clients[redirect_uri] = client
            new = secretbox.encrypt_json(Path(hub_dir), {"v": 1, "clients": clients})
            if current is None:
                res = conn.execute(text(
                    "UPDATE hz_connectors SET client_info_enc = :new "
                    "WHERE id = :id AND client_info_enc IS NULL"), {"new": new, "id": cid})
            else:
                res = conn.execute(text(
                    "UPDATE hz_connectors SET client_info_enc = :new "
                    "WHERE id = :id AND client_info_enc = :old"),
                    {"new": new, "id": cid, "old": current})
            if res.rowcount == 1:
                return client
    raise ConnectorError("busy", "The connector is being changed. Try again.", 409)


def _expired(client: dict) -> bool:
    try:
        exp = int(client.get("client_secret_expires_at") or 0)
    except (TypeError, ValueError):
        return True
    return bool(exp) and exp < time.time() + 300


def forget_registration(hub_dir, cid: str, redirect_uri: str) -> None:
    """Drop a cached client the provider no longer accepts."""
    eng = engine(hub_dir)
    with eng.begin() as conn:
        r = conn.execute(text("SELECT client_info_enc FROM hz_connectors WHERE id = :id"),
                         {"id": cid}).fetchone()
        if not r or not r[0]:
            return
        clients = _registrations(hub_dir, r[0])
        if clients.pop(redirect_uri, None) is None:
            return
        new = secretbox.encrypt_json(Path(hub_dir), {"v": 1, "clients": clients}) if clients else None
        conn.execute(text("UPDATE hz_connectors SET client_info_enc = :new "
                          "WHERE id = :id AND client_info_enc = :old"),
                     {"new": new, "id": cid, "old": r[0]})


# ---------------------------------------------------------------------------
# Writes (administrators; the route checks who)
# ---------------------------------------------------------------------------
def _audit(conn, hub_dir, actor: str, action: str, cid: str) -> None:
    from ..access import store_for

    store_for(Path(hub_dir)).write_audit(conn, normalize(actor) or "unknown", action, hub=ORG,
                                         permission=capability(cid), surface="console")


def create(hub_dir, data, *, actor: str) -> Connector:
    data = _body(data)
    name = _name(data.get("name"))
    url = net.check_url(data.get("url"))
    auth = _auth_type(data.get("auth_type", "oauth"))
    client_id = _optional_text(data.get("client_id"), code="invalid_client_id",
                               what="The client ID", limit=_CLIENT_ID_MAX)
    secret = _optional_text(data.get("client_secret"), code="invalid_client_secret",
                            what="The client secret", limit=_SECRET_MAX)
    shared_header = shared_enc = None
    if auth in ("none", "shared"):
        client_id = secret = None
    if auth == "shared":
        shared_header = _shared_header(data.get("shared_header"))
        key = _optional_text(data.get("shared_secret"), code="invalid_shared_secret",
                             what="The key", limit=_SECRET_MAX)
        if not key:
            raise ConnectorError("invalid_shared_secret", "Enter the key.", 422)
        _check_shared_key(key)
        shared_enc = secretbox.encrypt(Path(hub_dir), key)
    if secret and not client_id:
        raise ConnectorError("invalid_client_secret", "Enter the client ID that goes with the "
                             "client secret.", 422)
    scopes = _scopes(data.get("scopes")) if auth == "oauth" else None
    allow = _allowlist(data.get("tool_allowlist"))
    enabled = _enabled(data.get("enabled", True))
    explicit = data.get("id")
    now = time.time()
    with engine(hub_dir).begin() as conn:
        if explicit not in (None, ""):
            cid = check_id(explicit)
            if _get_row(conn, cid):
                raise ConnectorError("exists", f"A connector with the ID {cid} already exists.", 409)
        else:
            base = slugify(name)
            if not ID_RE.match(base or ""):
                raise ConnectorError("invalid_id", "Choose an ID for this connector: lowercase "
                                     "letters, digits or _.", 422)
            cid, n = base, 2
            while _get_row(conn, cid):
                cid = f"{base[:36]}_{n}"
                n += 1
        conn.execute(text(
            "INSERT INTO hz_connectors (" + ", ".join(_COLUMNS) + ") VALUES ("
            + ", ".join(f":{k}" for k in _COLUMNS) + ")"),
            {"id": cid, "name": name, "url": url, "auth_type": auth, "client_id": client_id,
             "client_secret_enc": secretbox.encrypt(Path(hub_dir), secret) if secret else None,
             "client_info_enc": None, "scopes": scopes,
             "tool_allowlist": json.dumps(allow) if allow else None,
             "enabled": 1 if enabled else 0, "created_by": normalize(actor) or None,
             "created_at": now, "updated_at": now, "shared_header": shared_header,
             "shared_secret_enc": shared_enc})
        _audit(conn, hub_dir, actor, "connector_create", cid)
    log.info("connectors: %s added %s (%s)", normalize(actor), cid, auth)
    return get(hub_dir, cid)


def update(hub_dir, cid: str, data, *, actor: str) -> tuple[Connector, bool]:
    """Apply the fields present in ``data``. Returns (connector, reset) where
    ``reset`` is True when people's existing connections no longer applied (the
    URL or authentication changed) and were removed: a token is never sent to
    a server other than the one that issued it."""
    data = _body(data)
    if "id" in data and data["id"] not in (None, cid):
        raise ConnectorError("invalid_id", "A connector's ID cannot be changed. Add a new one "
                             "instead.", 422)
    with engine(hub_dir).begin() as conn:
        row = _get_row(conn, cid) if ID_RE.match(cid or "") else None
        if row is None:
            raise ConnectorError("not_found", "No connector has this ID.", 404)
        changes: dict = {}
        if "name" in data:
            changes["name"] = _name(data["name"])
        if "url" in data:
            changes["url"] = net.check_url(data["url"])
        if "auth_type" in data:
            changes["auth_type"] = _auth_type(data["auth_type"])
        if "client_id" in data:
            changes["client_id"] = _optional_text(data["client_id"], code="invalid_client_id",
                                                  what="The client ID", limit=_CLIENT_ID_MAX)
            if changes["client_id"] != row["client_id"] and "client_secret" not in data:
                # A secret belongs to its client ID.
                changes["client_secret_enc"] = None
        if "client_secret" in data:
            secret = _optional_text(data["client_secret"], code="invalid_client_secret",
                                    what="The client secret", limit=_SECRET_MAX)
            changes["client_secret_enc"] = (secretbox.encrypt(Path(hub_dir), secret)
                                            if secret else None)
        if "scopes" in data:
            changes["scopes"] = _scopes(data["scopes"])
        if "tool_allowlist" in data:
            allow = _allowlist(data["tool_allowlist"])
            changes["tool_allowlist"] = json.dumps(allow) if allow else None
        if "enabled" in data:
            changes["enabled"] = 1 if _enabled(data["enabled"]) else 0
        if "shared_header" in data:
            changes["shared_header"] = _shared_header(data["shared_header"])
        if "shared_secret" in data:
            key = _optional_text(data["shared_secret"], code="invalid_shared_secret",
                                 what="The key", limit=_SECRET_MAX)
            if key:
                _check_shared_key(key)
            changes["shared_secret_enc"] = secretbox.encrypt(Path(hub_dir), key) if key else None
        auth = changes.get("auth_type", row["auth_type"])
        if auth in ("none", "shared"):
            changes["client_id"] = None
            changes["client_secret_enc"] = None
        if auth == "shared":
            changes.setdefault("shared_header", row.get("shared_header") or DEFAULT_SHARED_HEADER)
            if ("url" in changes and changes["url"] != row["url"]
                    and not changes.get("shared_secret_enc")):
                # A key belongs to the server it was given for: the stored one is
                # never sent to a new address.
                raise ConnectorError("shared_key_required", "A new server needs its own key. "
                                     "Enter the key again.", 422)
            if not changes.get("shared_secret_enc", row.get("shared_secret_enc")):
                raise ConnectorError("invalid_shared_secret", "Enter the key.", 422)
        else:
            changes["shared_header"] = None
            changes["shared_secret_enc"] = None
        client_id = changes.get("client_id", row["client_id"])
        secret_enc = changes.get("client_secret_enc", row["client_secret_enc"])
        if secret_enc and not client_id:
            raise ConnectorError("invalid_client_secret", "Enter the client ID that goes with "
                                 "the client secret.", 422)
        reset = (("url" in changes and changes["url"] != row["url"])
                 or ("auth_type" in changes and changes["auth_type"] != row["auth_type"]))
        if reset:
            # A different server or authorization: the cached client and every
            # in-flight authorization belong to the old one.
            changes["client_info_enc"] = None
            conn.execute(text("DELETE FROM hz_connector_flows WHERE connector_id = :id"), {"id": cid})
        if changes:
            changes["updated_at"] = time.time()
            conn.execute(text("UPDATE hz_connectors SET "
                              + ", ".join(f"{k} = :{k}" for k in changes) + " WHERE id = :id"),
                         {**changes, "id": cid})
            _audit(conn, hub_dir, actor, "connector_update", cid)
    if changes:
        log.info("connectors: %s changed %s (%s)", normalize(actor), cid,
                 ", ".join(sorted(k for k in changes if k != "updated_at")))
    if reset:
        from . import oauth_flow, tokens

        oauth_flow.forget_discovery(row["url"])
        dropped = tokens.drop_connector(hub_dir, cid)
        if dropped:
            log.info("connectors: %s now points elsewhere; %d connection(s) removed", cid, dropped)
    return get(hub_dir, cid), reset


def delete(hub_dir, cid: str, *, actor: str) -> bool:
    """Remove the connector, every in-flight authorization for it and every
    person's connection to it (revoked at the provider in the background).
    Grants of its capability stay in the access store, shown as obsolete."""
    if not isinstance(cid, str) or not ID_RE.match(cid):
        return False
    with engine(hub_dir).begin() as conn:
        res = conn.execute(text("DELETE FROM hz_connectors WHERE id = :id"), {"id": cid})
        if res.rowcount != 1:
            return False
        conn.execute(text("DELETE FROM hz_connector_flows WHERE connector_id = :id"), {"id": cid})
        conn.execute(text("DELETE FROM hz_connector_agents WHERE connector_id = :id"), {"id": cid})
        _audit(conn, hub_dir, actor, "connector_delete", cid)
    log.info("connectors: %s removed %s", normalize(actor), cid)
    from . import tokens

    tokens.drop_connector(hub_dir, cid)
    return True


# ---------------------------------------------------------------------------
# Which agents offer a connector
# ---------------------------------------------------------------------------
def _hub(hub) -> str:
    return normalize(str(hub or ""))


def offered_in(hub_dir, hub, *, conn=None) -> set[str]:
    """The ids of the connectors offered in agent `hub`."""
    def read(c):
        return {r[0] for r in c.execute(text(
            "SELECT connector_id FROM hz_connector_agents WHERE hub = :h"), {"h": _hub(hub)})}

    if conn is not None:
        return read(conn)
    with engine(hub_dir).connect() as c:
        return read(c)


def agents_of(hub_dir, cid: str) -> list[str]:
    """The agents that offer connector `cid`."""
    with engine(hub_dir).connect() as conn:
        return sorted(r[0] for r in conn.execute(text(
            "SELECT hub FROM hz_connector_agents WHERE connector_id = :c"), {"c": cid}))


def offer(hub_dir, cid: str, hub, *, actor: str) -> bool:
    """Offer an existing connector in agent `hub`. True when it was not yet."""
    hub = _hub(hub)
    if not hub or hub == "*":
        raise ConnectorError("invalid_agent", "Choose an agent.", 422)
    with engine(hub_dir).begin() as conn:
        if not _get_row(conn, cid):
            raise ConnectorError("not_found", "This connector doesn't exist.", 404)
        if cid in offered_in(hub_dir, hub, conn=conn):
            return False
        conn.execute(text("INSERT INTO hz_connector_agents (connector_id, hub, added_by, added_at) "
                          "VALUES (:c, :h, :b, :t)"),
                     {"c": cid, "h": hub, "b": normalize(actor) or None, "t": time.time()})
        from ..access import store_for

        store_for(Path(hub_dir)).write_audit(conn, normalize(actor) or "unknown", "connector_offer",
                                             hub=hub, permission=capability(cid), surface="console")
    return True


def withdraw(hub_dir, cid: str, hub, *, actor: str) -> bool:
    """Stop offering connector `cid` in agent `hub`: people stop using it there
    and its grants in that agent are removed. The connector, people's
    connections and other agents' offers stay."""
    from ..access import store_for

    hub = _hub(hub)
    with engine(hub_dir).begin() as conn:
        res = conn.execute(text("DELETE FROM hz_connector_agents WHERE connector_id = :c "
                                "AND hub = :h"), {"c": cid, "h": hub})
        if res.rowcount != 1:
            return False
        store_for(Path(hub_dir)).write_audit(conn, normalize(actor) or "unknown",
                                             "connector_withdraw", hub=hub,
                                             permission=capability(cid), surface="console")
    gs = store_for(Path(hub_dir))
    for subject, granted_hub, permission in gs.list_grants(hub):
        if permission == capability(cid):
            gs.revoke(subject, granted_hub, permission, actor=normalize(actor) or "unknown")
    return True


# ---------------------------------------------------------------------------
# Capabilities (hubzoid.capabilities lists these through connect_journey)
# ---------------------------------------------------------------------------
def permissions(hub_dir) -> list[dict]:
    """One ``connector_<id>`` capability per connector offered in this agent
    (the hub folder's name). A switched off connector stays listed (its grants
    remain visible) but unavailable."""
    from . import existing_engine

    eng = existing_engine(hub_dir)
    if eng is None:
        return []
    with eng.connect() as conn:
        offered = offered_in(hub_dir, Path(hub_dir).name, conn=conn)
        rows = [_row(r) for r in conn.execute(text(_SELECT + " ORDER BY name, id")).fetchall()
                if r[0] in offered]
    out = []
    for c in (_connector(r) for r in rows):
        shared = c.auth_type == "shared"
        out.append({
            "permission": c.capability,
            "label": f"Use {c.name}" if shared else f"Connect {c.name}",
            "description": (f"Use {c.name} through this agent with the company's shared account."
                            if shared else
                            f"Connect and use their own {c.name} account through this agent."),
            "sensitive": True, "surfaces": ["chat", "workflow"], "group": "connectors",
            "available": c.enabled, "status": "" if c.enabled else "Switched off",
        })
    return sorted(out, key=lambda r: r["permission"])
