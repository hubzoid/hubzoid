"""The connector registry: remote MCP servers the deployment offers, defined in
files: ``connectors/<id>.yaml`` in the deployment folder (the hub folder for a
deployment of one)::

    version: 1
    name: GitHub
    url: https://api.githubcopilot.com/mcp/
    sign_in: oauth            # oauth (each person), shared (one company key) or none
    client_id: Iv1.abc        # optional, a client registered in advance
    client_secret: env:GITHUB_MCP_CLIENT_SECRET   # a reference, never the value
    scopes: [repo]
    tools: [list_issues, get_issue]               # optional allow-list
    hubs: [support-hub, ops-hub]                  # the agents that offer it
    enabled: true

Settings shows connectors read-only; each person connects their own account
there. The database keeps only what people and providers create: each
person's tokens, sign-in flows in progress, and clients registered with a
provider dynamically (the ``hz_connectors`` row caches those).

What follows describes a connector's fields as they were stored before 2.0
(``hz_connectors``, for ``hubzoid upgrade``):

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
# Reads: the connector files are the definitions
# ---------------------------------------------------------------------------
FILE_KEYS = {"version", "name", "url", "sign_in", "client_id", "client_secret", "scopes", "tools",
             "enabled", "shared_header", "shared_key", "hubs"}


@dataclass(frozen=True)
class Definition:
    connector: "Connector"
    hubs: tuple[str, ...]
    client_secret_ref: str | None
    shared_key_ref: str | None
    path: Path


def folder(hub_dir) -> Path:
    """Where the deployment's connector files live: ``connectors/`` in the
    deployment folder (the hub folder for a deployment of one)."""
    from ..control.filesettings import settings_root

    return settings_root(Path(hub_dir)) / "connectors"


def _ref(value, what: str) -> str | None:
    if value in (None, ""):
        return None
    from ..secretstore import RefError, parse

    try:
        parse(str(value))
    except RefError as exc:
        raise ConnectorError("invalid_ref", f"{what} is a reference such as env:NAME, never the "
                             f"value itself ({exc}).", 422) from None
    return str(value)


def parse_file(path: Path) -> Definition:
    """One ``connectors/<id>.yaml``. Raises ConnectorError naming the file."""
    from ..control import yamlfile

    cid = check_id(path.stem)
    try:
        data = yamlfile.load(path).data or {}
    except yamlfile.YamlError as exc:
        raise ConnectorError("invalid_file", f"{path.name}: {exc.message}", 422) from None
    if not isinstance(data, dict):
        raise ConnectorError("invalid_file", f"{path.name} is a mapping of settings.", 422)
    unknown = sorted(set(data) - FILE_KEYS)
    if unknown:
        raise ConnectorError("invalid_file", f"{path.name}: unknown key {unknown[0]!r}.", 422)
    try:
        auth = _auth_type(data.get("sign_in", "oauth"))
        client_id = _optional_text(data.get("client_id"), code="invalid_client_id", what="client_id",
                                   limit=_CLIENT_ID_MAX) if auth == "oauth" else None
        secret_ref = _ref(data.get("client_secret"), "client_secret") if auth == "oauth" else None
        shared_ref = _ref(data.get("shared_key"), "shared_key") if auth == "shared" else None
        if auth == "shared" and not shared_ref:
            raise ConnectorError("invalid_shared_secret", "A shared connector names its key: "
                                 "shared_key: env:NAME.", 422)
        hubs = data.get("hubs") or []
        if not isinstance(hubs, list) or not all(isinstance(h, str) and h.strip() for h in hubs):
            raise ConnectorError("invalid_hubs", "hubs lists the agents (hub folder names) that offer "
                                 "this connector.", 422)
        connector = Connector(
            id=cid, name=_name(data.get("name") or cid), url=net.check_url(data.get("url")), auth_type=auth,
            client_id=client_id, has_client_secret=bool(secret_ref),
            scopes=_scopes(data.get("scopes")) if auth == "oauth" else None,
            tool_allowlist=tuple(_allowlist(data.get("tools")) or ()) or None,
            enabled=_enabled(data.get("enabled", True)), created_by="file", created_at=0.0, updated_at=0.0,
            registered=False, shared_header=_shared_header(data.get("shared_header")) if auth == "shared" else None,
            has_shared_secret=bool(shared_ref))
    except ConnectorError as exc:
        raise ConnectorError(exc.code, f"{path.name}: {exc.message}", 422) from None
    return Definition(connector, tuple(_hub(h) for h in hubs), secret_ref, shared_ref, path)


_seen: dict[str, tuple] = {}


def definitions(hub_dir) -> dict[str, Definition]:
    """Every valid connector file, by id; cached until the files change. A
    file that becomes invalid keeps its last good definition while the hub
    runs; one that never was valid is left out (doctor names it)."""
    base = folder(hub_dir)
    paths = sorted(p for p in base.glob("*.y*ml") if p.suffix in (".yaml", ".yml")) if base.is_dir() else []
    out: dict[str, Definition] = {}
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        stamp = (st.st_mtime_ns, st.st_size)
        hit = _seen.get(str(path))
        if hit is not None and hit[0] == stamp:
            if hit[1] is not None:
                out[hit[1].connector.id] = hit[1]
            continue
        try:
            d = parse_file(path)
        except ConnectorError as exc:
            log.warning("connectors: %s", exc.message)
            d = hit[1] if hit is not None else None
        _seen[str(path)] = (stamp, d)
        if d is not None:
            out[d.connector.id] = d
    return out


def problems(hub_dir) -> list[str]:
    """What is wrong with each connector file, for doctor and Settings."""
    base = folder(hub_dir)
    out = []
    for path in sorted(base.glob("*.yaml")) if base.is_dir() else []:
        try:
            parse_file(path)
        except ConnectorError as exc:
            out.append(exc.message)
    return out


def _with_cache(hub_dir, d: Definition) -> "Connector":
    from dataclasses import replace

    try:
        with engine(hub_dir).connect() as conn:
            r = conn.execute(text("SELECT client_info_enc FROM hz_connectors WHERE id = :id"),
                             {"id": d.connector.id}).fetchone()
    except Exception:  # noqa: BLE001 - a store that can't be read means no cached client
        r = None
    return replace(d.connector, registered=bool(r and r[0]))


def get(hub_dir, cid: str) -> Connector | None:
    if not isinstance(cid, str) or not ID_RE.match(cid):
        return None
    d = definitions(hub_dir).get(cid)
    return _with_cache(hub_dir, d) if d else None


def list_all(hub_dir) -> list[Connector]:
    return sorted((_with_cache(hub_dir, d) for d in definitions(hub_dir).values()),
                  key=lambda c: (c.name, c.id))


def _resolve(hub_dir, ref: str | None, what: str) -> str | None:
    if not ref:
        return None
    from ..secretstore import resolve

    try:
        return resolve(ref, Path(hub_dir))
    except Exception:  # noqa: BLE001 - SecretError and friends
        log.error("connectors: %s (%s) is not set", what, ref)
        return None


def shared_headers(hub_dir, cid: str) -> dict | None:
    """The request header of a Shared key connector, from the reference its
    file names, or None. A key sent as ``Authorization`` without a scheme is
    sent as a Bearer token."""
    d = definitions(hub_dir).get(cid)
    if d is None or d.connector.auth_type != "shared":
        return None
    key = _resolve(hub_dir, d.shared_key_ref, f"the shared key of {cid}")
    if not key:
        return None
    try:
        _check_shared_key(key)
    except ConnectorError:
        log.error("connectors: the shared key of %s (%s) is not a valid header value", cid, d.shared_key_ref)
        return None
    header = d.connector.shared_header or DEFAULT_SHARED_HEADER
    if header.lower() == "authorization" and " " not in key.strip():
        key = f"Bearer {key.strip()}"
    return {header: key}


def client_secret(hub_dir, cid: str) -> str | None:
    """The pre-registered client secret, from the reference its file names."""
    d = definitions(hub_dir).get(cid)
    if d is None or not d.client_secret_ref:
        return None
    value = _resolve(hub_dir, d.client_secret_ref, f"the client secret of {cid}")
    if value is None:
        raise ConnectorError("secret_unreadable", f"{d.client_secret_ref} (the client secret of {cid}) "
                             "is not set. Add it to the deployment's .env.", 500)
    return value


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
                d = definitions(hub_dir).get(cid)
                if d is None:
                    raise ConnectorError("not_found", "This connector no longer exists.", 404)
                # The row only caches clients registered with the provider.
                now = time.time()
                conn.execute(text("INSERT INTO hz_connectors (id, name, url, auth_type, enabled, created_at, "
                                  "updated_at) VALUES (:id, :n, :u, :a, 1, :t, :t)"),
                             {"id": cid, "n": d.connector.name, "u": d.connector.url,
                              "a": d.connector.auth_type, "t": now})
                r = (None,)
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


def _file_owned(cid: str | None = None) -> ConnectorError:
    where = f"connectors/{cid}.yaml" if cid else "connectors/<id>.yaml"
    return ConnectorError("file_owned", f"Connectors are defined in {where} in the deployment folder. "
                          "Edit the file (or ask Claude Code or Codex to); every agent picks it up. Each "
                          "person still connects their own account here.", 409)


def create(hub_dir, data, *, actor: str) -> Connector:
    raise _file_owned()


def update(hub_dir, cid: str, data, *, actor: str) -> tuple[Connector, bool]:
    raise _file_owned(cid)


def delete(hub_dir, cid: str, *, actor: str) -> bool:
    raise _file_owned(cid)


def stored_definitions(hub_dir) -> list[dict]:
    """Connectors defined in the database before 2.0 (Settings, 1.2 to 1.3),
    with their secrets decrypted, for ``hubzoid upgrade`` to write into files
    and .env. Never shown or logged."""
    out = []
    with engine(hub_dir).connect() as conn:
        rows = [_row(r) for r in conn.execute(text(_SELECT + " ORDER BY id")).fetchall()]
        agents = {}
        try:
            for cid, hub in conn.execute(text("SELECT connector_id, hub FROM hz_connector_agents")):
                agents.setdefault(cid, []).append(hub)
        except Exception:  # noqa: BLE001 - the table may already be gone
            pass
    for row in rows:
        def dec(value):
            if not value:
                return None
            try:
                return secretbox.decrypt_text(Path(hub_dir), value)
            except secretbox.SecretKeyError:
                return None
        out.append({**{k: row[k] for k in ("id", "name", "url", "auth_type", "client_id", "scopes",
                                           "tool_allowlist", "enabled", "shared_header")},
                    "client_secret": dec(row["client_secret_enc"]), "shared_secret": dec(row["shared_secret_enc"]),
                    "hubs": sorted(agents.get(row["id"], []))})
    return out


def _hub(hub) -> str:
    return normalize(str(hub or ""))


def offered_in(hub_dir, hub, *, conn=None) -> set[str]:
    """The ids of the connectors offered in agent `hub` (their files' ``hubs``)."""
    hub = _hub(hub)
    return {cid for cid, d in definitions(hub_dir).items() if hub in d.hubs}


def agents_of(hub_dir, cid: str) -> list[str]:
    """The agents that offer connector `cid`."""
    d = definitions(hub_dir).get(cid)
    return sorted(d.hubs) if d else []


def offer(hub_dir, cid: str, hub, *, actor: str) -> bool:
    raise _file_owned(cid)


def withdraw(hub_dir, cid: str, hub, *, actor: str) -> bool:
    raise _file_owned(cid)


# ---------------------------------------------------------------------------
# Capabilities (hubzoid.capabilities lists these through connect_journey)
# ---------------------------------------------------------------------------
def permissions(hub_dir) -> list[dict]:
    """One ``connector_<id>`` capability per connector offered in this agent
    (the hub folder's name). A switched off connector stays listed (its grants
    remain visible) but unavailable."""
    offered = offered_in(hub_dir, Path(hub_dir).name)
    out = []
    for c in sorted((d.connector for cid, d in definitions(hub_dir).items() if cid in offered),
                    key=lambda c: (c.name, c.id)):
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
