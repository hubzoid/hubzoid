"""Chat configuration: the deployment's chat defaults, from one file.

Their one home is the ``chat:`` section of ``hubzoid.yaml`` (for this release,
``chat/config.yaml`` in the deployment folder when there is no section).
Without either, every setting has its product default and a fresh hub works.
Every bridge reads the file again when it changes
(``hubzoid.control.filesettings``); Settings shows the values read-only and
there is no apply step or copy in the database. An edit that breaks the file
keeps the last good values and says so; a start with a broken file stops.

Namespaces. ``presentation`` (appearance, text size, density, timezone) is
owned here. Each later chat feature registers its own namespace
(``register_namespace``) with a pydantic model. A namespace the file names but
this install does not provide is refused with ``capability_unavailable``, never
reported as enabled just because a file says so.

Personal preferences (``hz_chat_preferences``) hold one person's overrides of
the keys the deployment lets people change, with their own revision. Effective
value precedence for presentation keys: deployment lock > account preference >
device choice > deployment default > product default. A preference never
changes permissions, sign-in or who may approve anything.

English is the only interface language (``presentation.locale.allowed: [en]``).
"""
from __future__ import annotations

import difflib
import json
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..control import canonical, deploy, idempotency
from ..control import yamlfile

FILE = "chat/config.yaml"
VERSION = 1
MAX_DOCUMENT_BYTES = 256 * 1024
PREFERENCE_WRITES_PER_MINUTE = 30

Mode = Literal["light", "dark", "oled", "system"]
Density = Literal["comfortable", "compact"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocaleSettings(_Strict):
    default: Literal["en"] = "en"
    allowed: list[Literal["en"]] = Field(default_factory=lambda: ["en"])


class AppearanceSettings(_Strict):
    default_mode: Mode = "dark"
    allowed_modes: list[Mode] = Field(default_factory=lambda: ["light", "dark", "oled", "system"])
    default_density: Density = "compact"
    allowed_densities: list[Density] = Field(default_factory=lambda: ["comfortable", "compact"])
    default_text_scale: float = 1
    allowed_text_scales: list[float] = Field(default_factory=lambda: [1, 1.125, 1.25, 1.5])

    @field_validator("allowed_text_scales")
    @classmethod
    def _scales(cls, value: list[float]) -> list[float]:
        if not value:
            raise ValueError("allow at least one text scale")
        for scale in value:
            if not 0.75 <= scale <= 2:
                raise ValueError("text scales are from 0.75 to 2")
        return value

    @field_validator("allowed_modes", "allowed_densities")
    @classmethod
    def _non_empty(cls, value: list) -> list:
        if not value:
            raise ValueError("allow at least one value")
        if len(set(value)) != len(value):
            raise ValueError("list each value once")
        return value

    @model_validator(mode="after")
    def _defaults_allowed(self):
        if self.default_mode not in self.allowed_modes:
            raise ValueError("default_mode must be one of allowed_modes")
        if self.default_density not in self.allowed_densities:
            raise ValueError("default_density must be one of allowed_densities")
        if self.default_text_scale not in self.allowed_text_scales:
            raise ValueError("default_text_scale must be one of allowed_text_scales")
        return self


class PreferenceFlags(_Strict):
    mode: bool = True
    density: bool = True
    text_scale: bool = True
    timezone: bool = True


class Presentation(_Strict):
    locale: LocaleSettings = Field(default_factory=LocaleSettings)
    appearance: AppearanceSettings = Field(default_factory=AppearanceSettings)
    preferences: PreferenceFlags = Field(default_factory=PreferenceFlags)
    timezone: str = "UTC"
    theme_source: str | None = "branding/theme.json"

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        _check_timezone(value)
        return value

    @field_validator("theme_source")
    @classmethod
    def _theme_source(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("branding/") or ".." in value.split("/"):
            raise ValueError("theme_source is a path under branding/")
        return value


def _check_timezone(value: str) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("use an IANA timezone name such as UTC or Asia/Kolkata") from exc


# -- namespace registry -----------------------------------------------------------
@dataclass(frozen=True)
class Namespace:
    name: str
    owner: str
    model: type[BaseModel] | None
    installed: bool
    #: keys (dotted, inside the namespace) a person may override
    personal_keys: tuple[str, ...] = ()
    #: "admission" (new runs pick it up) or "restart"
    applies: str = "admission"


_REGISTRY: dict[str, Namespace] = {}
_REGISTRY_LOCK = threading.Lock()

# Namespaces later 2.0 chat issues own. They exist in the registry so a file
# naming one fails with a clear "not installed" until that feature lands.
PLANNED = {
    "runtime": "HUB-72", "organization": "HUB-73", "search": "HUB-73", "files": "HUB-74",
    "retrieval": "HUB-75", "feedback": "HUB-76", "helpers": "HUB-76",
    "notifications": "HUB-76", "voice": "HUB-77", "models": "HUB-78", "comparison": "HUB-78",
    "sharing": "HUB-79",
}


def register_namespace(name: str, model: type[BaseModel], *, owner: str,
                       personal_keys: tuple[str, ...] = (), applies: str = "admission") -> None:
    """Install a feature's settings namespace. A name may be registered once."""
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(name)
        if existing is not None and existing.installed and existing.model is not model:
            raise ValueError(f"chat config namespace {name!r} is already registered")
        _REGISTRY[name] = Namespace(name, owner, model, True, personal_keys, applies)


#: namespace -> check(key inside the namespace, value, active document) -> clean value.
#: Raises ConfigError (``policy_locked`` when the administrator manages it).
_PERSONAL: dict[str, Any] = {}


#: top-level personal key -> check(value, active document) -> clean value.
_PERSONAL_KEYS: dict[str, Any] = {}


def register_personal_key(key: str, check) -> None:
    """A feature's own top-level personal setting (``send_key``)."""
    _PERSONAL_KEYS[key] = check


def register_personal(namespace: str, check) -> None:
    """Let people keep their own value for some keys of a namespace, in the
    same account preference document as appearance."""
    _PERSONAL[namespace] = check


def _bootstrap_registry() -> None:
    with _REGISTRY_LOCK:
        if "presentation" not in _REGISTRY:
            _REGISTRY["presentation"] = Namespace(
                "presentation", "HUB-71", Presentation, True,
                ("mode", "density", "text_scale", "timezone"), "admission")
        for name, owner in PLANNED.items():
            _REGISTRY.setdefault(name, Namespace(name, owner, None, False))


_bootstrap_registry()


def namespaces() -> dict[str, Namespace]:
    return dict(_REGISTRY)


# -- errors -------------------------------------------------------------------------
class ConfigError(Exception):
    """A refused configuration. ``status`` and ``code`` follow the conventions."""

    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = {k: v for k, v in extra.items() if v is not None}

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


def _escape(token: Any) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def _pointer(loc: tuple) -> str:
    return "".join(f"/{_escape(p)}" for p in loc)


def _nearest(key: str, known) -> list[str]:
    return difflib.get_close_matches(key, list(known), n=3, cutoff=0.5)


# -- validation ---------------------------------------------------------------------
def validate_document(doc: Any, *, lines: yamlfile.Loaded | None = None) -> dict:
    """Validate a whole document (``{"version": 1, <namespace>: {...}}``) and
    return it normalised with defaults filled in. Raises ``ConfigError`` with
    the JSON Pointer (and line, when parsed from a file) of the first problem."""
    def where(pointer: str) -> dict:
        out: dict = {"pointer": pointer}
        if lines is not None:
            out["line"] = lines.line_of(pointer)
            if lines.path:
                out["file"] = lines.path
        return out

    if not isinstance(doc, dict):
        raise ConfigError(422, "validation_failed", "The configuration must be a mapping.")
    if doc.get("version") != VERSION:
        raise ConfigError(422, "unsupported_schema",
                          f"chat/config.yaml needs \"version: {VERSION}\" (this release supports "
                          f"version {VERSION}).", **where("/version"), supported=VERSION)
    registry = namespaces()
    normalized: dict = {"version": VERSION}
    for name, value in doc.items():
        if name == "version":
            continue
        ns = registry.get(name)
        if ns is None:
            raise ConfigError(422, "unknown_key", f"Unknown setting {name!r}.",
                              **where(f"/{_escape(name)}"), nearest=_nearest(name, registry))
        if not ns.installed:
            raise ConfigError(422, "capability_unavailable",
                              f"The {name!r} settings belong to a chat feature this release does "
                              f"not include yet ({ns.owner}). Remove them for now.",
                              **where(f"/{_escape(name)}"))
        try:
            model = ns.model.model_validate(value if value is not None else {})
        except ValidationError as exc:
            err = exc.errors()[0]
            loc = (name,) + tuple(err.get("loc") or ())
            pointer = _pointer(loc)
            if err.get("type") == "extra_forbidden":
                bad = str(loc[-1])
                parent_fields = _fields_at(ns.model, loc[1:-1])
                raise ConfigError(422, "unknown_key", f"Unknown setting {bad!r} at {_pointer(loc[:-1]) or '/'}.",
                                  **where(pointer), nearest=_nearest(bad, parent_fields))
            msg = err.get("msg", "invalid value").removeprefix("Value error, ")
            raise ConfigError(422, "validation_failed", f"{pointer}: {msg}.", **where(pointer))
        normalized[name] = model.model_dump(mode="json")
    for name, ns in registry.items():
        if ns.installed and name not in normalized:
            normalized[name] = ns.model().model_dump(mode="json")
    if len(canonical.canonical_bytes(normalized)) > MAX_DOCUMENT_BYTES:
        raise ConfigError(413, "too_large", "The configuration is larger than 256 KiB.")
    return normalized


def _fields_at(model: type[BaseModel], loc: tuple) -> list[str]:
    current: Any = model
    for part in loc:
        field = getattr(current, "model_fields", {}).get(part)
        if field is None:
            return []
        current = field.annotation
    return list(getattr(current, "model_fields", {}).keys())


def defaults() -> dict:
    return validate_document({"version": VERSION})


def _hoist(sub: dict, prefix: str, defs: dict) -> dict:
    """Move a namespace schema's ``$defs`` to the document root under
    ``<namespace>.<Name>`` and point its references there."""
    local = sub.pop("$defs", {})
    rename = {k: f"{prefix}.{k}" for k in local}

    def fix(node):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "$ref" and isinstance(v, str) and v.startswith("#/$defs/"):
                    out[k] = "#/$defs/" + rename.get(v[len("#/$defs/"):], v[len("#/$defs/"):])
                else:
                    out[k] = fix(v)
            return out
        if isinstance(node, list):
            return [fix(v) for v in node]
        return node

    for k, v in local.items():
        defs[rename[k]] = fix(v)
    return fix(sub)


def schema() -> dict:
    """JSON Schema of the document, with defaults, scope and personal keys."""
    props: dict = {"version": {"const": VERSION}}
    defs: dict = {}
    meta: dict = {}
    for name, ns in sorted(namespaces().items()):
        if ns.installed:
            props[name] = _hoist(ns.model.model_json_schema(), name, defs)
        meta[name] = {"owner": ns.owner, "installed": ns.installed, "scope": "deployment",
                      "personal_keys": list(ns.personal_keys), "applies": ns.applies}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "chat/config.yaml", "type": "object", "required": ["version"],
        "additionalProperties": False, "properties": props, "$defs": defs,
        "x-hubzoid": {"file": FILE, "version": VERSION, "namespaces": meta,
                      "limits": {"document_bytes": MAX_DOCUMENT_BYTES,
                                 "preference_writes_per_minute": PREFERENCE_WRITES_PER_MINUTE}},
        "defaults": defaults(),
    }


def parse_file(path: Path) -> tuple[dict, yamlfile.Loaded]:
    try:
        loaded = yamlfile.load(path)
    except yamlfile.YamlError as exc:
        raise ConfigError(422, "invalid_yaml", exc.message, file=exc.path, line=exc.line)
    return validate_document(loaded.data, lines=loaded), loaded


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """JSON Pointer -> leaf value (lists are leaves: arrays replace)."""
    if isinstance(value, dict):
        out: dict = {}
        for key, child in value.items():
            out.update(flatten(child, f"{prefix}/{_escape(key)}"))
        return out
    return {prefix: value}


def diff(old: dict, new: dict) -> list[dict]:
    a, b = flatten(old), flatten(new)
    changes = []
    for pointer in sorted(set(a) | set(b)):
        if pointer == "/version":
            continue
        if a.get(pointer, _MISSING) != b.get(pointer, _MISSING):
            changes.append({"pointer": pointer,
                            "old": a.get(pointer), "new": b.get(pointer)})
    return changes


_MISSING = object()


# -- storage ------------------------------------------------------------------------
metadata = sa.MetaData()

preferences = sa.Table(
    "hz_chat_preferences", metadata,
    sa.Column("deployment_id", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(64), primary_key=True),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("values_json", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)


@dataclass
class Active:
    revision: int  # 1 when a file sets the chat defaults, 0 for product defaults
    document: dict
    digest: str
    applied_by: str | None = None
    applied_at: float | None = None
    source: str = "default"   # where the values come from (hubzoid.yaml (chat), chat/config.yaml)
    error: str | None = None  # the file is invalid now; these are its last good values


class ChatConfigService:
    """Chat configuration for one deployment (any bridge of it)."""

    def __init__(self, hub_dir: Path, engine=None):
        self.hub_dir = Path(hub_dir)
        self.root = deploy.root(self.hub_dir)
        # Only preferences need the database: reading the chat defaults (the
        # file) must not create it, e.g. while `hubzoid doctor` builds tools.
        self._engine = engine
        self._deployment_id: str | None = None
        self._pref_writes: dict[str, list[float]] = {}
        from ..control.filesettings import FileSection

        def parse(data, loaded):
            data = {"version": VERSION, **(data or {})} if isinstance(data, dict) or data is None else data
            return validate_document(data, lines=loaded)

        # The one home: `chat:` in hubzoid.yaml (chat/config.yaml for this
        # release when there is none). No copy in the database.
        self._file = FileSection(self.hub_dir, "chat", FILE, parse=parse, default=defaults,
                                 error=lambda m: ConfigError(503, "chat_config_invalid", m))

    @property
    def engine(self):
        if self._engine is None:
            from .. import db, migrations

            engine = db.operational_engine(self.hub_dir)
            migrations.upgrade(engine, "operational")
            self._engine = engine
        return self._engine

    @property
    def deployment_id(self) -> str:
        if self._deployment_id is None:
            self._deployment_id = deploy.deployment_id(self.hub_dir)
        return self._deployment_id

    # -- read ---------------------------------------------------------------------
    def active(self) -> Active:
        cur = self._file.current()
        return Active(1 if cur.path is not None else 0, cur.document, cur.digest, None, cur.loaded_at,
                      source=cur.source, error=cur.error)

    def history(self, limit: int = 50) -> list[dict]:
        """Changes are the file's history in Git."""
        return []

    def sets_timezone(self) -> bool:
        """Whether the deployment's file names a time zone itself (even UTC,
        which the effective view can't tell from the product default)."""
        cur = self._file.current()
        if cur.path is None:
            return False
        try:
            import yaml

            raw = yaml.safe_load(Path(cur.path).read_text()) or {}
        except (OSError, ValueError, yaml.YAMLError):
            return False
        if Path(cur.path).name == "hubzoid.yaml":
            raw = raw.get("chat") or {}
        return isinstance(raw, dict) and "timezone" in (raw.get("presentation") or {})

    def file_path(self) -> Path:
        cur = self._file.current()
        return cur.path or (self.root / FILE)

    def file_state(self) -> dict:
        """Where the defaults come from, and whether that file is valid now."""
        cur = self._file.current()
        out = {"path": cur.source, "exists": cur.path is not None, "valid": cur.error is None,
               "digest": cur.digest, "matches_active": True, "pending_changes": 0, "read_only": True}
        if cur.error:
            out["error"] = {"code": "invalid_file", "message": cur.error}
        return out

    # -- write --------------------------------------------------------------------
    def apply(self, document: Any, *, expected_revision: int | None, actor: str,
              idempotency_key: str | None = None, now: float | None = None) -> dict:
        """Refused: chat defaults live in a file (``hubzoid.yaml``)."""
        refused = self._file.refused()
        raise ConfigError(409, "file_owned", str(refused.message if hasattr(refused, "message") else refused))

    # -- preferences ----------------------------------------------------------------
    def get_preferences(self, user_id: str) -> dict:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(preferences).where(
                preferences.c.deployment_id == self.deployment_id,
                preferences.c.user_id == user_id)).first()
        values = json.loads(row.values_json) if row else {}
        return {"revision": row.revision if row else 0, "values": values,
                "effective": self.effective(values)}

    def set_preferences(self, user_id: str, values: Any, *, expected_revision: int,
                        now: float | None = None) -> dict:
        """Replace this person's preference overrides (null clears one key)."""
        now = time.time() if now is None else now
        self._rate_limit(user_id, now)
        if not isinstance(values, dict):
            raise ConfigError(422, "validation_failed", "values must be an object.")
        pres = Presentation.model_validate(self.active().document["presentation"])
        clean: dict = {}
        document = None
        for key, value in values.items():
            if "." in key:
                # A feature's own personal setting ("notifications.desktop"),
                # checked by the namespace that registered it.
                ns, rest = key.split(".", 1)
                check = _PERSONAL.get(ns)
                if check is None:
                    raise ConfigError(422, "unknown_key", f"{key!r} is not a personal setting.",
                                      pointer=f"/values/{_escape(key)}")
                if value is None:
                    continue
                document = document or self.active().document
                clean[key] = check(rest, value, document)
                continue
            if key in _PERSONAL_KEYS:
                # A top-level personal key another feature registered ("send_key").
                if value is None:
                    continue
                document = document or self.active().document
                clean[key] = _PERSONAL_KEYS[key](value, document)
                continue
            if key not in ("mode", "density", "text_scale", "timezone"):
                raise ConfigError(422, "unknown_key", f"{key!r} is not a personal setting.",
                                  pointer=f"/values/{_escape(key)}",
                                  nearest=_nearest(key, ("mode", "density", "text_scale", "timezone")))
            if value is None:
                continue
            if not getattr(pres.preferences, key):
                raise ConfigError(422, "policy_locked",
                                  f"Your administrator manages {key.replace('_', ' ')} for everyone.",
                                  pointer=f"/values/{key}")
            allowed = {"mode": pres.appearance.allowed_modes,
                       "density": pres.appearance.allowed_densities,
                       "text_scale": pres.appearance.allowed_text_scales}.get(key)
            if key == "timezone":
                try:
                    _check_timezone(value if isinstance(value, str) else "")
                except ValueError as exc:
                    raise ConfigError(422, "validation_failed", str(exc), pointer="/values/timezone")
            elif value not in allowed:
                raise ConfigError(422, "validation_failed",
                                  f"{key.replace('_', ' ')} must be one of {', '.join(map(str, allowed))}.",
                                  pointer=f"/values/{key}", allowed=allowed)
            clean[key] = value
        with self.engine.begin() as conn:
            row = conn.execute(sa.select(preferences.c.revision).where(
                preferences.c.deployment_id == self.deployment_id,
                preferences.c.user_id == user_id)).first()
            current = row.revision if row else 0
            if expected_revision != current:
                raise ConfigError(409, "revision_conflict",
                                  "Your settings changed on another device. Reload them and try again.",
                                  current_revision=current)
            if row is None:
                conn.execute(preferences.insert().values(
                    deployment_id=self.deployment_id, user_id=user_id, revision=1,
                    values_json=json.dumps(clean), updated_at=now))
            else:
                done = conn.execute(preferences.update().where(
                    preferences.c.deployment_id == self.deployment_id,
                    preferences.c.user_id == user_id, preferences.c.revision == current)
                    .values(revision=current + 1, values_json=json.dumps(clean), updated_at=now)).rowcount
                if done != 1:
                    raise ConfigError(409, "revision_conflict",
                                      "Your settings changed on another device. Reload them and try again.",
                                      current_revision=current + 1)
        return {"revision": current + 1, "values": clean, "effective": self.effective(clean)}

    def _rate_limit(self, user_id: str, now: float) -> None:
        recent = [t for t in self._pref_writes.get(user_id, []) if now - t < 60]
        if len(recent) >= PREFERENCE_WRITES_PER_MINUTE:
            raise ConfigError(429, "rate_limited", "Too many settings changes. Wait a minute.",
                              retry_after=60)
        recent.append(now)
        self._pref_writes[user_id] = recent

    # -- effective --------------------------------------------------------------------
    def effective(self, personal: dict | None = None, device: dict | None = None) -> dict:
        """Each presentation key's effective value and its source:
        ``account``, ``device``, ``deployment`` or ``default``."""
        active = self.active()
        pres = active.document["presentation"]
        base = Presentation().model_dump(mode="json")
        deployed = active.revision > 0
        personal = personal or {}
        device = device or {}
        flags = pres["preferences"]
        out: dict = {}
        mapping = {"mode": ("appearance", "default_mode", "allowed_modes"),
                   "density": ("appearance", "default_density", "allowed_densities"),
                   "text_scale": ("appearance", "default_text_scale", "allowed_text_scales"),
                   "timezone": (None, "timezone", None)}
        for key, (group, default_key, allowed_key) in mapping.items():
            dep_value = pres[group][default_key] if group else pres[default_key]
            prod_value = base[group][default_key] if group else base[default_key]
            allowed = pres[group][allowed_key] if group and allowed_key else None
            source = "deployment" if deployed and dep_value != prod_value else "default"
            value = dep_value
            locked = not flags[key]
            for layer, name in ((device, "device"), (personal, "account")):
                candidate = layer.get(key)
                if candidate is None or locked:
                    continue
                if allowed is not None and candidate not in allowed:
                    continue
                value, source = candidate, name
            out[key] = {"value": value, "source": source, "locked": locked,
                        **({"allowed": allowed} if allowed is not None else {})}
        out["locale"] = {"value": "en", "source": "default", "locked": True, "allowed": ["en"]}
        return out

    def sources(self) -> dict:
        """JSON Pointer -> {value, source} for the whole active document."""
        active = self.active()
        default_flat = flatten(defaults())
        out = {}
        for pointer, value in flatten(active.document).items():
            if pointer == "/version":
                continue
            source = ("deployment" if active.revision > 0 and default_flat.get(pointer, _MISSING) != value
                      else "default")
            out[pointer] = {"value": value, "source": source}
        return out

    def capabilities(self, principal=None) -> dict:
        """This person's effective chat feature availability, per namespace."""
        out = {}
        for name, ns in sorted(namespaces().items()):
            out[name] = {"available": ns.installed, "owner": ns.owner,
                         **({} if ns.installed else {"reason": "not_installed"})}
        out["presentation"]["personal_keys"] = [
            k for k, v in self.active().document["presentation"]["preferences"].items() if v]
        if principal is not None:
            out["chat_config"] = {"available": principal.can("chat_config.manage")}
        return out

    def bootstrap(self) -> dict:
        """Public, pre-sign-in defaults: no grants, names or private settings."""
        pres = self.active().document["presentation"]
        return {
            "config_revision": self.active().revision,
            "locale": {"default": "en", "allowed": ["en"]},
            "appearance": {k: pres["appearance"][k] for k in
                           ("default_mode", "allowed_modes", "default_density", "allowed_densities",
                            "default_text_scale", "allowed_text_scales")},
            "preferences": pres["preferences"],
            "timezone": pres["timezone"],
        }


def _audit(conn, hub_dir: Path, actor: str, action: str, *, subject: str | None = None,
           detail: dict | None = None) -> None:
    from ..control import audit

    audit.write(conn, actor, action, subject=subject, detail=detail, surface="chat_config")


_services: dict[str, ChatConfigService] = {}
_services_lock = threading.Lock()


def service_for(hub_dir: Path) -> ChatConfigService:
    key = str(Path(hub_dir).resolve())
    with _services_lock:
        svc = _services.get(key)
        if svc is None:
            svc = ChatConfigService(Path(hub_dir))
            _services[key] = svc
        return svc
