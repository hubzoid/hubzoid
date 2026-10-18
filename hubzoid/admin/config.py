"""``admin.yaml`` and the settings domain registry.

``admin.yaml`` (optional, deployment folder, ``version: 1``) bounds diagnostic
runs::

    version: 1
    diagnostics:
      check_timeout_seconds: 10    # 1 to 30
      max_parallel_checks: 4       # 1 to 8
      bundle_ttl_minutes: 60       # 5 to 1440
      allow_network_probes: false  # active probes still need a consent per run

Changes go through a **plan**: validate the values, record them with the
revision they were based on and the exact digest, then **apply** that plan by
id and digest (never replacement values). Plans expire after 30 minutes.

The **domain registry** describes every settings area the Console shows, with
its file, owner, read and write permissions and operations. Each domain keeps
its own store and apply rules: chat (``chat/config.yaml``), branding
(``branding.yaml``, through reviewed publication) and admin (this file).
Process settings that come from the environment are listed read-only with
their source.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..control import canonical, deploy, idempotency, yamlfile

FILE = "admin.yaml"
PLAN_TTL = 30 * 60


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


metadata = sa.MetaData()

revisions = sa.Table(
    "hz_admin_config_revisions", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("deployment_id", sa.String(64), nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("document_json", sa.Text, nullable=False),
    sa.Column("digest", sa.String(80), nullable=False),
    sa.Column("created_by", sa.String(320), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint("deployment_id", "revision", name="hz_admin_config_revisions_rev"),
)

heads = sa.Table(
    "hz_admin_config_heads", metadata,
    sa.Column("deployment_id", sa.String(64), primary_key=True),
    sa.Column("revision_id", sa.String(64), nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
)

plans = sa.Table(
    "hz_admin_config_plans", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("deployment_id", sa.String(64), nullable=False),
    sa.Column("hub_id", sa.String(255)),
    sa.Column("domain", sa.String(64), nullable=False),
    sa.Column("expected_revision", sa.Integer, nullable=False),
    sa.Column("candidate_json", sa.Text, nullable=False),
    sa.Column("digest", sa.String(80), nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("created_by_user_id", sa.String(320), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.Column("owner_plan_id", sa.String(64)),
    sa.Column("owner_operation_id", sa.String(64)),
)


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

    def active(self) -> Active:
        with self.engine.connect() as conn:
            head = conn.execute(sa.select(heads).where(heads.c.deployment_id == self.deployment_id)).first()
            if head is None:
                d = defaults()
                return Active(0, d, canonical.digest(d))
            row = conn.execute(sa.select(revisions).where(revisions.c.id == head.revision_id)).first()
        doc = validate(json.loads(row.document_json))
        return Active(row.revision, doc, row.digest, row.created_by, row.created_at)

    def show(self) -> dict:
        a = self.active()
        d = _flatten(defaults())
        provenance = {p: {"value": v, "source": "deployment" if a.revision and d.get(p) != v else "default"}
                      for p, v in _flatten(a.document).items() if p != "/version"}
        return {"domain": "admin", "file": FILE, "revision": a.revision, "digest": a.digest,
                "values": a.document, "defaults": defaults(), "provenance": provenance,
                "applied_by": a.applied_by, "applied_at": a.applied_at, "schema": AdminDoc.model_json_schema(),
                "restart_required": False, "file_state": self.file_state()}

    def file_state(self) -> dict:
        path = self.root / FILE
        if not path.is_file():
            return {"path": FILE, "exists": False}
        try:
            doc = self.parse_file(path)
        except AdminError as exc:
            return {"path": FILE, "exists": True, "valid": False,
                    "error": {"code": exc.code, "message": exc.message, **exc.extra}}
        return {"path": FILE, "exists": True, "valid": True,
                "matches_active": canonical.digest(doc) == self.active().digest}

    def parse_file(self, path: Path) -> dict:
        try:
            loaded = yamlfile.load(path)
        except yamlfile.YamlError as exc:
            raise AdminError(422, "invalid_yaml", exc.message, file=exc.path, line=exc.line)
        return validate(loaded.data, loaded=loaded)

    def plan(self, values: Any, *, expected_revision: int, actor: str, idempotency_key: str | None = None,
             now: float | None = None) -> dict:
        doc = validate(values)
        now = time.time() if now is None else now
        active = self.active()
        if expected_revision != active.revision:
            raise AdminError(409, "revision_conflict",
                             "The admin settings changed since you loaded them. Reload and plan again.",
                             current_revision=active.revision)
        dig = canonical.digest({"domain": "admin", "scope": self.deployment_id, "version": 1,
                                "base_revision": active.revision, "values": doc})

        def change(conn):
            plan_id = str(uuid.uuid4())
            conn.execute(plans.insert().values(
                id=plan_id, deployment_id=self.deployment_id, hub_id=None, domain="admin",
                expected_revision=active.revision, candidate_json=json.dumps(doc), digest=dig, state="draft",
                created_by_user_id=actor, created_at=now, expires_at=now + PLAN_TTL))
            return 201, {"plan_id": plan_id, "digest": dig, "base_revision": active.revision,
                         "diff": diff(active.document, doc), "expires_at": now + PLAN_TTL,
                         "restart_required": False}

        return idempotency.run(self.engine, deployment_id=self.deployment_id, actor_id=actor,
                               route="POST /api/admin/config/plans", key=idempotency_key,
                               body={"values": doc, "expected_revision": expected_revision},
                               change=change, kind="admin_config.plan", now=now).body

    def apply(self, plan_id: str, *, digest: str, expected_revision: int, actor: str,
              idempotency_key: str | None = None, now: float | None = None) -> dict:
        now = time.time() if now is None else now

        def change(conn):
            plan = conn.execute(sa.select(plans).where(plans.c.id == plan_id,
                                                       plans.c.deployment_id == self.deployment_id)).first()
            if plan is None:
                raise AdminError(404, "not_found", "That plan was not found.")
            if plan.expires_at < now and plan.state == "draft":
                conn.execute(plans.update().where(plans.c.id == plan_id).values(state="expired"))
                raise AdminError(410, "plan_expired", "That plan expired. Make a fresh plan.")
            if plan.state != "draft":
                raise AdminError(409, "revision_conflict", f"That plan is already {plan.state}. Make a fresh plan.")
            if plan.digest != digest:
                raise AdminError(409, "revision_conflict", "The digest doesn't match the plan. Apply the exact plan you reviewed.")
            head = conn.execute(sa.select(heads).where(heads.c.deployment_id == self.deployment_id)).first()
            current = head.revision if head else 0
            if expected_revision != current or plan.expected_revision != current:
                conn.execute(plans.update().where(plans.c.id == plan_id).values(state="conflicted"))
                raise AdminError(409, "revision_conflict",
                                 "The admin settings changed after this plan was made. Make a fresh plan.",
                                 current_revision=current)
            doc = json.loads(plan.candidate_json)
            rev_id = str(uuid.uuid4())
            new_rev = current + 1
            dig = canonical.digest(doc)
            conn.execute(revisions.insert().values(id=rev_id, deployment_id=self.deployment_id, revision=new_rev,
                                                   document_json=json.dumps(doc), digest=dig, created_by=actor,
                                                   created_at=now))
            if head is None:
                conn.execute(heads.insert().values(deployment_id=self.deployment_id, revision_id=rev_id,
                                                   revision=new_rev))
            else:
                conn.execute(heads.update().where(heads.c.deployment_id == self.deployment_id,
                                                  heads.c.revision == current)
                             .values(revision_id=rev_id, revision=new_rev))
            conn.execute(plans.update().where(plans.c.id == plan_id).values(state="applied"))
            from ..control import audit

            audit.write(conn, actor, "admin_config.applied", subject=f"revision:{new_rev}",
                        detail={"plan": plan_id, "digest": dig}, surface="admin")
            return 200, {"revision": new_rev, "digest": dig, "plan_id": plan_id}

        return idempotency.run(self.engine, deployment_id=self.deployment_id, actor_id=actor,
                               route=f"POST /api/admin/config/plans/{plan_id}/apply", key=idempotency_key,
                               body={"digest": digest, "expected_revision": expected_revision},
                               change=change, kind="admin_config.apply", now=now).body

    def list_plans(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(plans).where(plans.c.deployment_id == self.deployment_id)
                                .order_by(plans.c.created_at.desc()).limit(50)).all()
        now = time.time()
        return [{"plan_id": r.id, "domain": r.domain, "state": "expired" if r.state == "draft" and r.expires_at < now
                 else r.state, "digest": r.digest, "base_revision": r.expected_revision,
                 "created_by": r.created_by_user_id, "created_at": r.created_at, "expires_at": r.expires_at}
                for r in rows]

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
    out = [
        {"domain": "chat", "owner": "HUB-71", "file": "chat/config.yaml", "revision": chat_active.revision,
         "digest": chat_active.digest, "read_capability": "chat_config.read",
         "write_capability": "chat_config.manage", "restart_required": False,
         "cli": ["hubzoid chat config get --sources", "hubzoid chat config apply --file chat/config.yaml"],
         "api": ["GET /api/chat/config", "PUT /api/chat/config"], "console": "#/settings/chat"},
        {"domain": "branding", "owner": "HUB-82", "file": "branding.yaml", "revision": brand.get("revision"),
         "generation": brand.get("generation"), "read_capability": "branding.read",
         "write_capability": "branding.activate", "restart_required": False,
         "cli": ["hubzoid branding inspect", "hubzoid branding draft create", "hubzoid branding activate"],
         "api": ["GET /api/branding/history", "POST /api/branding/drafts"], "console": "#/settings/appearance",
         "note": "Changes go through a reviewed draft and activation."},
        {"domain": "admin", "owner": "HUB-84", "file": FILE, "revision": admin.revision, "digest": admin.digest,
         "read_capability": "admin.config.read", "write_capability": "admin.config.write",
         "restart_required": False,
         "cli": ["hubzoid admin config show", "hubzoid admin config plan --file admin.yaml",
                 "hubzoid admin config apply --plan ID --digest D --expected-revision N"],
         "api": ["GET /api/admin/config", "POST /api/admin/config/plans"], "console": "#/settings/admin"},
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
