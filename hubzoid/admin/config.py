"""``admin.yaml`` and the settings domain registry.

``admin.yaml`` (optional, deployment folder, ``version: 1``) bounds diagnostic
runs::

    version: 1
    diagnostics:
      check_timeout_seconds: 10    # 1 to 30
      max_parallel_checks: 4       # 1 to 8
      bundle_ttl_minutes: 60       # 5 to 1440
      allow_network_probes: false  # active probes still need a consent per run

Its one home is the ``diagnostics:`` section of ``hubzoid.yaml`` (``admin.yaml``
in the deployment folder for this release, when there is none). Settings shows
it read-only; there is no plan, apply or copy in the database.

The **domain registry** describes every settings area the Console shows, with
its file, owner, read and write permissions and operations. Each domain keeps
one home, a file: chat (``chat:`` in hubzoid.yaml), branding (``brand:`` and
``branding/``) and admin (``diagnostics:``), all read-only in Settings.
Process settings that come from the environment are listed read-only with
their source.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..control import canonical, deploy, yamlfile

FILE = "admin.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Diagnostics(_Strict):
    check_timeout_seconds: int = Field(10, ge=1, le=30)
    max_parallel_checks: int = Field(4, ge=1, le=8)
    bundle_ttl_minutes: int = Field(60, ge=5, le=1440)
    allow_network_probes: bool = False

    # Reports are kept until deleted (no default retention). Optional
    # retention is a deployment-wide policy owned with backups and traces.


class AdminDoc(_Strict):
    version: int
    diagnostics: Diagnostics = Field(default_factory=Diagnostics)

    @field_validator("version")
    @classmethod
    def _v(cls, v: int) -> int:
        if v != 1:
            raise ValueError("admin.yaml needs version: 1")
        return v


class AdminError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = {k: v for k, v in extra.items() if v is not None}


def validate(values: Any, *, loaded: yamlfile.Loaded | None = None) -> dict:
    if not isinstance(values, dict):
        raise AdminError(422, "validation_failed", "admin.yaml must be a mapping.")
    try:
        return AdminDoc.model_validate(values).model_dump(mode="json")
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = tuple(err.get("loc") or ())
        pointer = "".join(f"/{p}" for p in loc)
        line = loaded.line_of(pointer) if loaded else None
        if err.get("type") == "extra_forbidden":
            raise AdminError(422, "validation_failed", f"Unknown key at {pointer}.", pointer=pointer, line=line)
        msg = err.get("msg", "invalid").removeprefix("Value error, ")
        raise AdminError(422, "validation_failed", f"{pointer}: {msg}.", pointer=pointer, line=line,
                         accepted=_ranges().get(pointer))


def _ranges() -> dict[str, str]:
    return {"/diagnostics/check_timeout_seconds": "1 to 30",
            "/diagnostics/max_parallel_checks": "1 to 8",
            "/diagnostics/bundle_ttl_minutes": "5 to 1440",
            "/diagnostics/allow_network_probes": "true or false"}


def defaults() -> dict:
    return validate({"version": 1})



@dataclass
class Active:
    revision: int
    document: dict
    digest: str
    applied_by: str | None = None
    applied_at: float | None = None


def _flatten(v: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(v, dict):
        out: dict = {}
        for k, c in v.items():
            out.update(_flatten(c, f"{prefix}/{k}"))
        return out
    return {prefix: v}


def diff(old: dict, new: dict) -> list[dict]:
    a, b = _flatten(old), _flatten(new)
    return [{"pointer": p, "old": a.get(p), "new": b.get(p)}
            for p in sorted(set(a) | set(b)) if p != "/version" and a.get(p) != b.get(p)]


class AdminConfigService:
    def __init__(self, hub_dir: Path, engine=None):
        from .. import db, migrations

        self.hub_dir = Path(hub_dir)
        self.root = deploy.root(self.hub_dir)
        self.deployment_id = deploy.deployment_id(self.hub_dir)
        if engine is None:
            engine = db.operational_engine(self.hub_dir)
            migrations.upgrade(engine, "operational")
        self.engine = engine
        from ..control.filesettings import FileSection

        def parse(data, loaded):
            if loaded is None:  # the `diagnostics:` section of hubzoid.yaml
                return validate({"version": 1, "diagnostics": data or {}})
            return validate(data, loaded=loaded)

        # The one home: `diagnostics:` in hubzoid.yaml (admin.yaml for this
        # release when there is none). No plan, apply or copy in the database.
        self._file = FileSection(self.hub_dir, "diagnostics", FILE, parse=parse, default=defaults,
                                 error=lambda m: AdminError(503, "admin_config_invalid", m))

    def active(self) -> Active:
        cur = self._file.current()
        return Active(1 if cur.path is not None else 0, cur.document, cur.digest, None, cur.loaded_at)

    def show(self) -> dict:
        a = self.active()
        d = _flatten(defaults())
        provenance = {p: {"value": v, "source": "deployment" if a.revision and d.get(p) != v else "default"}
                      for p, v in _flatten(a.document).items() if p != "/version"}
        return {"domain": "admin", "file": self._file.current().source, "revision": a.revision, "digest": a.digest,
                "values": a.document, "defaults": defaults(), "provenance": provenance,
                "applied_by": a.applied_by, "applied_at": a.applied_at, "schema": AdminDoc.model_json_schema(),
                "restart_required": False, "file_state": self.file_state(), "read_only": True}

    def file_state(self) -> dict:
        cur = self._file.current()
        out = {"path": cur.source, "exists": cur.path is not None, "valid": cur.error is None,
               "matches_active": True, "read_only": True}
        if cur.error:
            out["error"] = {"code": "invalid_file", "message": cur.error}
        return out

    def parse_file(self, path: Path) -> dict:
        try:
            loaded = yamlfile.load(path)
        except yamlfile.YamlError as exc:
            raise AdminError(422, "invalid_yaml", exc.message, file=exc.path, line=exc.line)
        return validate(loaded.data, loaded=loaded)

    def _refused(self) -> AdminError:
        err = self._file.refused()
        return AdminError(409, "file_owned", getattr(err, "message", str(err)))

    def plan(self, values: Any, *, expected_revision: int, actor: str, idempotency_key: str | None = None,
             now: float | None = None) -> dict:
        """Refused: diagnostic limits live in a file (``hubzoid.yaml``)."""
        raise self._refused()

    def apply(self, plan_id: str, *, digest: str, expected_revision: int, actor: str,
              idempotency_key: str | None = None, now: float | None = None) -> dict:
        raise self._refused()

    def list_plans(self) -> list[dict]:
        return []

    def export_yaml(self) -> str:
        a = self.active()
        return f"# admin.yaml exported from revision {a.revision} ({a.digest})\n" + yamlfile.dumps(a.document)

    def diagnostics_settings(self) -> dict:
        return self.active().document["diagnostics"]


# -- the domain registry -------------------------------------------------------------
def _env_source(name: str) -> dict:
    present = bool((os.environ.get(name) or "").strip())
    return {"key": name, "source": "environment" if present else "unset", "secret": False}


def domains(hub_dir: Path, principal=None) -> list[dict]:
    """Every settings area, its file, owner, permissions and operations.
    Values come from each owner; secret values are never included."""
    from ..chat import config as chat_cfg

    chat = chat_cfg.service_for(hub_dir)
    chat_active = chat.active()
    admin = service_for(hub_dir).active()
    try:
        from ..ui import branding_store

        brand = branding_store.service_for(hub_dir).active_state()
    except Exception:  # noqa: BLE001
        brand = {"revision": None, "generation": 0}
    model = os.environ.get("MODEL") or "claude-local"
    edit = "Edit hubzoid.yaml (or ask Claude Code or Codex to); every agent picks it up within a few seconds."
    out = [
        {"domain": "chat", "owner": "HUB-71", "file": chat_active.source, "revision": chat_active.revision,
         "digest": chat_active.digest, "read_capability": "chat_config.read", "read_only": True,
         "restart_required": False, "cli": ["hubzoid chat config get --sources"],
         "api": ["GET /api/chat/config"], "console": "#/settings/chat", "note": edit},
        {"domain": "branding", "owner": "HUB-82", "file": brand.get("source") or "hubzoid.yaml (brand)",
         "revision": brand.get("revision"), "generation": brand.get("generation"),
         "read_capability": "branding.read", "read_only": True, "restart_required": False,
         "cli": ["hubzoid branding inspect", "hubzoid branding validate"],
         "api": ["GET /api/branding/source"], "console": "#/settings/appearance",
         "note": "The brand and its files in branding/. " + edit},
        {"domain": "admin", "owner": "HUB-84", "file": service_for(hub_dir).file_state()["path"],
         "revision": admin.revision, "digest": admin.digest, "read_capability": "admin.config.read",
         "read_only": True, "restart_required": False, "cli": ["hubzoid admin config show"],
         "api": ["GET /api/admin/config"], "console": "#/settings/admin", "note": edit},
        {"domain": "model", "owner": "HUB-101", "file": ".env", "read_only": True,
         "values": [{"key": "MODEL", "value": model, "source": "environment" if os.environ.get("MODEL") else "default"}],
         "restart_required": True, "note": "Set in the hub's .env. Changing it needs a restart."},
        {"domain": "telemetry", "owner": "HUB-109", "file": ".env", "read_only": True,
         "values": [_env_source("HUBZOID_OTEL_ENDPOINT")], "restart_required": True},
        {"domain": "sign_in", "owner": "HUB-86", "file": ".env", "read_only": True,
         "values": [_env_source("HUBZOID_AUTH"), _env_source("HUBZOID_PUBLIC_URL")], "restart_required": True},
    ]
    if principal is not None:
        for d in out:
            d["can_read"] = principal.can(d["read_capability"]) if "read_capability" in d else principal.org_admin
            d["can_write"] = principal.can(d["write_capability"]) if "write_capability" in d else False
    return out


_services: dict[str, AdminConfigService] = {}
_lock = threading.Lock()


def service_for(hub_dir: Path) -> AdminConfigService:
    key = str(Path(hub_dir).resolve())
    with _lock:
        svc = _services.get(key)
        if svc is None:
            svc = AdminConfigService(Path(hub_dir))
            _services[key] = svc
        return svc
