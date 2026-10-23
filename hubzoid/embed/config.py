"""The ``apps:`` section of ``hubzoid.yaml``: apps that embed Hubzoid's chat.

::

    apps:
      erpnext:                          # the client id
        name: ERPNext
        origins: [https://erp.acme.com] # browsers that may call (CORS), never identity
        mode: pass                      # pass | jwt
        secret_ref: secret:deployment/app-erpnext   # pass mode, or a jwt shared secret
        access:                         # the most this app may allow
          agents: [finance]
          tools: [erp_reads, "connector:erp"]
        token_to: ["connector:erp"]     # tools that receive the person's app token
        max_lifetime: 3600              # seconds a token may last (default 1 hour)

      portal:                           # the app's own JWT (Auth0, Clerk, Keycloak...)
        mode: jwt
        issuer: https://acme.eu.auth0.com/
        audience: https://hubzoid.acme.com
        jwks_url: https://acme.eu.auth0.com/.well-known/jwks.json
        user_claim: sub
        scopes: {claim: scope, map: {"chat:finance": {agents: [finance], tools: ["*"]}}}
        # or: all_users: true  (every signed-in user of the app gets the maximum)

Tool names are the agent's own tool names, ``connector:<id>`` for a connector
added in Settings, ``mcp:<server>`` for one of the hub's MCP servers, or ``*``.

Like the other file-owned settings (``control.filesettings``) the section is
read again when the file changes; an invalid edit keeps the last good value.
Without a ``hubzoid.yaml``, ``apps.yaml`` in the deployment folder is read.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

FILE = "apps.yaml"
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
MODES = ("pass", "jwt")
DEFAULT_AUDIENCE = "hubzoid"
DEFAULT_LIFETIME = 3600
MAX_LIFETIME = 24 * 3600
_KEYS = {"name", "enabled", "origins", "mode", "secret_ref", "jwks_url", "public_key", "issuer",
         "audience", "user_claim", "name_claim", "email_claim", "access", "scopes", "all_users",
         "token_to", "token_header", "max_lifetime"}


class AppsError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class Access:
    """Agents (hub keys) and tool names. ``"*"`` in tools means every tool."""

    agents: frozenset[str] = frozenset()
    tools: frozenset[str] = frozenset()

    def all_tools(self) -> bool:
        return "*" in self.tools


@dataclass(frozen=True)
class App:
    id: str
    name: str
    enabled: bool
    origins: tuple[str, ...]
    mode: str
    issuer: str
    audience: str
    maximum: Access
    secret_ref: str | None = None
    jwks_url: str | None = None
    public_key: str | None = None
    user_claim: str = "sub"
    name_claim: str = "name"
    email_claim: str = "email"
    scope_claim: str | None = None
    scope_map: dict = field(default_factory=dict, hash=False, compare=False)
    all_users: bool = False
    token_to: tuple[str, ...] = ()
    token_header: str = "Authorization"
    max_lifetime: int = DEFAULT_LIFETIME


def _norm(name: str) -> str:
    return (name or "").strip().lower()


def _origin_ok(origin: str) -> bool:
    try:
        u = urlsplit(origin)
    except ValueError:
        return False
    if u.path not in ("", "/") or u.query or u.fragment or not u.hostname:
        return False
    if u.scheme == "https":
        return True
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")


def _secure_url(url: str) -> bool:
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    if not u.hostname:
        return False
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1"))


def _names(value, where: str, *, tools: bool = False) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"{where} is a list of names.")
    out = set()
    for v in value:
        v = v.strip()
        if tools:
            out.add(v if v == "*" else v.strip())
        else:
            out.add(_norm(v))
    return frozenset(out)


def _access(value, where: str) -> Access:
    if value is None:
        return Access()
    if not isinstance(value, dict) or set(value) - {"agents", "tools"}:
        raise ValueError(f"{where} has agents and tools.")
    return Access(agents=_names(value.get("agents"), f"{where}.agents"),
                  tools=_names(value.get("tools"), f"{where}.tools", tools=True))


def _one(app_id: str, raw: Any) -> App:
    where = f"apps.{app_id}"
    if not ID_RE.match(app_id or ""):
        raise ValueError(f"{where}: an app id is 2 to 40 lowercase letters, digits or _, starting "
                         "with a letter (for example erpnext).")
    if not isinstance(raw, dict):
        raise ValueError(f"{where} is a mapping of settings.")
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown key {unknown[0]!r}. Known keys: " + ", ".join(sorted(_KEYS)) + ".")
    mode = raw.get("mode", "pass")
    if mode not in MODES:
        raise ValueError(f"{where}.mode is pass or jwt.")
    origins = raw.get("origins") or []
    if isinstance(origins, str):
        origins = [origins]
    if not isinstance(origins, list) or not all(isinstance(o, str) and _origin_ok(o.strip().rstrip("/"))
                                                for o in origins):
        raise ValueError(f"{where}.origins is a list of web addresses such as https://erp.example.com "
                         "(http:// only on this machine).")
    maximum = _access(raw.get("access"), f"{where}.access")
    if not maximum.agents:
        raise ValueError(f"{where}.access.agents names at least one agent this app may use.")
    lifetime = raw.get("max_lifetime", DEFAULT_LIFETIME)
    if not isinstance(lifetime, int) or isinstance(lifetime, bool) or not 60 <= lifetime <= MAX_LIFETIME:
        raise ValueError(f"{where}.max_lifetime is a number of seconds from 60 to {MAX_LIFETIME}.")
    secret_ref = raw.get("secret_ref")
    if secret_ref is not None:
        from ..secretstore import RefError, parse

        if not isinstance(secret_ref, str):
            raise ValueError(f"{where}.secret_ref is a secret reference such as secret:deployment/app-{app_id}.")
        try:
            parse(secret_ref)
        except RefError as exc:
            raise ValueError(f"{where}.secret_ref: {exc}") from exc
    jwks_url, public_key = raw.get("jwks_url"), raw.get("public_key")
    if jwks_url is not None and (not isinstance(jwks_url, str) or not _secure_url(jwks_url)):
        raise ValueError(f"{where}.jwks_url is an https:// address.")
    if public_key is not None and (not isinstance(public_key, str) or "BEGIN" not in public_key):
        raise ValueError(f"{where}.public_key is a PEM public key (-----BEGIN PUBLIC KEY-----).")
    sources = [s for s in (secret_ref, jwks_url, public_key) if s]
    if mode == "pass":
        if not secret_ref or jwks_url or public_key:
            raise ValueError(f"{where}: a pass-mode app has secret_ref (made by hubzoid embed secret {app_id}).")
        issuer = raw.get("issuer") or app_id
        if issuer != app_id:
            raise ValueError(f"{where}.issuer: a pass is issued by the app itself, so its issuer is {app_id!r}.")
    else:
        if len(sources) != 1:
            raise ValueError(f"{where}: a jwt-mode app has exactly one of jwks_url, public_key or secret_ref.")
        issuer = raw.get("issuer")
        if not isinstance(issuer, str) or not issuer.strip():
            raise ValueError(f"{where}.issuer is the iss value of the app's tokens.")
    audience = raw.get("audience", DEFAULT_AUDIENCE if mode == "pass" else None)
    if not isinstance(audience, str) or not audience.strip():
        raise ValueError(f"{where}.audience is the aud value tokens for Hubzoid carry.")
    claims = {}
    for key, default in (("user_claim", "sub"), ("name_claim", "name"), ("email_claim", "email")):
        value = raw.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{where}.{key} names a claim.")
        claims[key] = value.strip()
    scope_claim, scope_map, all_users = None, {}, bool(raw.get("all_users", False))
    if not isinstance(raw.get("all_users", False), bool):
        raise ValueError(f"{where}.all_users is true or false.")
    scopes = raw.get("scopes")
    if mode == "jwt":
        if scopes is None and not all_users:
            raise ValueError(f"{where}: say how access is decided: scopes (map the token's scope values) "
                             "or all_users: true (every signed-in user of the app gets the maximum).")
        if scopes is not None and all_users:
            raise ValueError(f"{where}: use scopes or all_users, not both.")
        if scopes is not None:
            if not isinstance(scopes, dict) or set(scopes) - {"claim", "map"} or not isinstance(scopes.get("map"), dict):
                raise ValueError(f"{where}.scopes has claim (default scope) and map.")
            scope_claim = scopes.get("claim", "scope")
            if not isinstance(scope_claim, str) or not scope_claim.strip():
                raise ValueError(f"{where}.scopes.claim names a claim.")
            for value, grant in scopes["map"].items():
                scope_map[str(value)] = _access(grant, f"{where}.scopes.map.{value}")
    elif scopes is not None or all_users:
        raise ValueError(f"{where}: scopes and all_users are for jwt mode; a pass lists the access itself.")
    token_to = raw.get("token_to") or []
    if isinstance(token_to, str):
        token_to = [token_to]
    if not isinstance(token_to, list) or not all(isinstance(t, str) and t.strip() and t != "*" for t in token_to):
        raise ValueError(f"{where}.token_to lists the tools that receive the person's app token.")
    header = raw.get("token_header", "Authorization")
    if not isinstance(header, str) or not re.match(r"^[A-Za-z0-9-]{1,64}$", header):
        raise ValueError(f"{where}.token_header is an HTTP header name.")
    if _norm(header).startswith(("x-hubzoid-", "x-openwebui-")):
        raise ValueError(f"{where}.token_header can't be a Hubzoid identity header.")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"{where}.enabled is true or false.")
    name = raw.get("name", app_id)
    return App(id=app_id, name=str(name), enabled=enabled,
               origins=tuple(o.strip().rstrip("/") for o in origins), mode=mode, issuer=issuer.strip(),
               audience=audience.strip(), maximum=maximum, secret_ref=secret_ref, jwks_url=jwks_url,
               public_key=public_key, user_claim=claims["user_claim"], name_claim=claims["name_claim"],
               email_claim=claims["email_claim"], scope_claim=scope_claim, scope_map=scope_map,
               all_users=all_users, token_to=tuple(t.strip() for t in token_to), token_header=header,
               max_lifetime=lifetime)


def parse(data: Any) -> dict[str, App]:
    """``{client id: App}`` from the section's data. Raises ValueError naming the key."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("apps is a mapping of app id to its settings.")
    apps = {str(k): _one(str(k), v) for k, v in data.items()}
    seen: dict[tuple[str, str], str] = {}
    for app in apps.values():
        pair = (app.issuer, app.audience)
        if pair in seen:
            raise ValueError(f"apps.{app.id} and apps.{seen[pair]} have the same issuer and audience; "
                             "tokens must tell them apart.")
        seen[pair] = app.id
    return apps


class AppsService:
    """The deployment's apps, read again when ``hubzoid.yaml`` changes."""

    def __init__(self, hub_dir: Path):
        from ..control.filesettings import FileSection

        self.hub_dir = Path(hub_dir)

        def parse_section(data, loaded):
            source = data if loaded is None else (data or {}).get("apps")
            try:
                parse(source)  # checked here, so an invalid edit keeps the last good apps
            except ValueError as exc:
                raise AppsError(503, "apps_invalid", str(exc)) from exc
            return {"apps": source or {}}

        self._file = FileSection(self.hub_dir, "apps", FILE, parse=parse_section,
                                 default=lambda: {"apps": {}},
                                 error=lambda m: AppsError(503, "apps_invalid", m))
        self._parsed: tuple[str, dict[str, App]] | None = None

    def all(self) -> dict[str, App]:
        cur = self._file.current()
        if self._parsed is None or self._parsed[0] != cur.digest:
            self._parsed = (cur.digest, parse(cur.document["apps"]))
        return self._parsed[1]

    def get(self, app_id: str) -> App | None:
        return self.all().get(app_id)

    def by_issuer(self, issuer: str, audience) -> App | None:
        """The one app whose issuer is ``issuer`` and whose audience the token
        names (``aud`` may be a list). None when none or more than one match."""
        auds = [audience] if isinstance(audience, str) else (audience if isinstance(audience, list) else [])
        found = [a for a in self.all().values() if a.issuer == issuer and a.audience in auds]
        return found[0] if len(found) == 1 else None

    def origins(self) -> set[str]:
        return {o for a in self.all().values() if a.enabled for o in a.origins}

    def state(self) -> dict:
        cur = self._file.current()
        return {"source": cur.source, "valid": cur.error is None, "error": cur.error}


_services: dict[str, AppsService] = {}
_lock = threading.Lock()


def service_for(hub_dir: Path) -> AppsService:
    key = str(Path(hub_dir).resolve())
    with _lock:
        svc = _services.get(key)
        if svc is None:
            svc = _services[key] = AppsService(Path(hub_dir))
        return svc


def reset() -> None:
    with _lock:
        _services.clear()
