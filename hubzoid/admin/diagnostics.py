"""Read-only diagnostic reports and support bundles.

A report runs a fixed set of checks and records each one's status: ``ok``,
``info``, ``warn``, ``fail``, ``skipped`` or ``timeout``, with a short summary,
safe details and a recovery step. A completed report can contain failed
checks.

* **Passive** checks read sanitised metadata: storage, schema, sign-in setup,
  model sign-in readiness, the scheduler, connectors, telemetry and the chat,
  branding and admin configuration. They never import hub code, build a model
  runtime, call a model or read a secret's value.
* **Active** checks contact a service (connector reachability). They run only
  when ``admin.yaml`` allows network probes AND the request consents.

Reports are claimed by one worker with a lease. A worker that dies leaves a
lease that expires: at the next start the report is marked ``interrupted``,
never rerun on its own. "Run again" makes a new report. Each report is bounded
(per-check timeout, parallel checks, 120 s wall time) and a deployment runs at
most two reports at once with twenty queued.

Support bundles are zip files of four allowlisted JSON documents
(``manifest.json``, ``checks.json``, ``versions.json``,
``config-provenance.json``) with no message bodies, prompts, attachments,
environment dumps, paths, URLs with queries or secrets. They expire.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import io
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import sqlalchemy as sa

from ..control import deploy
from .config import AdminError, service_for as config_service

log = logging.getLogger("hubzoid.admin.diagnostics")

STATUSES = ("ok", "info", "warn", "fail", "skipped", "timeout")
REPORT_WALL_SECONDS = 120
LEASE_SECONDS = 30
RENEW_SECONDS = 10
MAX_RUNNING = 2
MAX_QUEUED = 20
DETAIL_LIMIT = 8 * 1024

metadata = sa.MetaData()

reports = sa.Table(
    "hz_admin_diagnostic_reports", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("deployment_id", sa.String(64), nullable=False),
    sa.Column("hub_id", sa.String(255)),
    sa.Column("requested_by_user_id", sa.String(320), nullable=False),
    sa.Column("profile", sa.String(16), nullable=False),
    sa.Column("check_ids_json", sa.Text),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("started_at", sa.Float),
    sa.Column("completed_at", sa.Float),
    sa.Column("expires_at", sa.Float),
    sa.Column("lease_owner", sa.String(64)),
    sa.Column("lease_expires_at", sa.Float),
    sa.Column("config_revision", sa.Integer, nullable=False),
    sa.Column("idempotency_key", sa.String(255)),
    sa.Column("request_hash", sa.String(64)),
    sa.Column("error_code", sa.String(64)),
    sa.Column("cancel_requested", sa.Integer, nullable=False),
    sa.Column("previous_report_id", sa.String(64)),
)

checks_table = sa.Table(
    "hz_admin_diagnostic_checks", metadata,
    sa.Column("report_id", sa.String(64), primary_key=True),
    sa.Column("check_id", sa.String(64), primary_key=True),
    sa.Column("attempt", sa.Integer, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("duration_ms", sa.Integer),
    sa.Column("summary", sa.Text),
    sa.Column("safe_details_json", sa.Text),
    sa.Column("remediation_code", sa.String(64)),
    sa.Column("remediation", sa.Text),
    sa.Column("owner_domain", sa.String(64)),
    sa.Column("owner_resource_id", sa.String(255)),
    sa.Column("started_at", sa.Float),
    sa.Column("completed_at", sa.Float),
)

bundles = sa.Table(
    "hz_admin_support_bundles", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("report_id", sa.String(64), nullable=False),
    sa.Column("deployment_id", sa.String(64), nullable=False),
    sa.Column("created_by_user_id", sa.String(320), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("relative_storage_key", sa.String(255)),
    sa.Column("sha256", sa.String(64)),
    sa.Column("size_bytes", sa.Integer),
    sa.Column("manifest_json", sa.Text),
    sa.Column("error_code", sa.String(64)),
)


# -- checks ---------------------------------------------------------------------------
@dataclass
class Result:
    status: str
    summary: str
    details: dict = field(default_factory=dict)
    remediation: str | None = None
    remediation_code: str | None = None


@dataclass(frozen=True)
class CheckDef:
    id: str
    profile: str  # passive | active
    purpose: str
    owner: str
    fn: Callable[[Path], Result]


_SECRETISH = re.compile(r"(secret|token|password|passwd|api[_-]?key|fingerprint|credential|cookie|authorization)", re.I)
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)\?[^\s]*")


def scrub(value):
    """Allowlist-style cleanup of details: drop secret-looking keys, strip
    absolute paths and URL queries from strings, cap sizes."""
    if isinstance(value, dict):
        return {str(k): scrub(v) for k, v in value.items() if not _SECRETISH.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in list(value)[:50]]
    if isinstance(value, str):
        out = _URL_QUERY.sub(r"\1?…", value)
        out = re.sub(r"(/Users|/home|/root|/private|/var|/tmp|[A-Za-z]:\\)[^\s'\"]*", "<path>", out)
        return out[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:200]


def _from_doctor(check) -> Result:
    return Result(check.status if check.status in STATUSES else "info", check.summary,
                  scrub(check.detail) if isinstance(getattr(check, "detail", None), dict) else {})


def _first(fn, hub: Path, check_id: str | None = None) -> Result:
    found = fn(hub) if hub is not None else fn()
    if isinstance(found, list):
        found = next((c for c in found if check_id is None or c.id == check_id), found[0] if found else None)
    if found is None:
        return Result("skipped", "Not applicable here")
    return _from_doctor(found)


def check_storage(hub: Path) -> Result:
    from .. import db

    try:
        engine = db.operational_engine(hub)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        return Result("fail", f"The operational database is unreachable ({type(exc).__name__}).",
                      remediation="Check DATABASE_URL in the hub's .env, then restart.",
                      remediation_code="database_unreachable")
    return Result("ok", f"The operational database answers ({engine.dialect.name}).",
                  {"dialect": engine.dialect.name})


def check_schema(hub: Path) -> Result:
    from .. import doctor

    results = doctor._schema(hub)
    worst = next((c for c in results if c.status == "fail"), None) or next(
        (c for c in results if c.status == "warn"), None) or (results[0] if results else None)
    if worst is None:
        return Result("info", "No schema to check yet.")
    r = _from_doctor(worst)
    if r.status in ("fail", "warn"):
        r.remediation = "Restart the hub to upgrade the schema, or restore a backup if it was written by a newer release."
        r.remediation_code = "schema_upgrade"
    return r


def check_disk(hub: Path) -> Result:
    usage = shutil.disk_usage(deploy.root(hub))
    free_gb = usage.free / 1e9
    details = {"free_gb": round(free_gb, 1), "total_gb": round(usage.total / 1e9, 1)}
    if free_gb < 0.5:
        return Result("fail", f"Only {free_gb:.1f} GB free on the deployment disk.", details,
                      "Free disk space or move the deployment to a larger volume.", "disk_full")
    if free_gb < 2:
        return Result("warn", f"{free_gb:.1f} GB free on the deployment disk.", details,
                      "Free disk space soon.", "disk_low")
    return Result("ok", f"{free_gb:.1f} GB free on the deployment disk.", details)


def check_model_auth(hub: Path) -> Result:
    model = (os.environ.get("MODEL") or "claude-local").strip()
    low = model.lower()
    if low.startswith("claude-local"):
        found = shutil.which("claude") is not None or Path.home().joinpath(".claude").is_dir()
        if found or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"):
            return Result("ok", "Claude sign-in is available for the claude-local runtime.", {"runtime": "claude-local"})
        return Result("fail", "Model provider credential is not configured.", {"runtime": "claude-local"},
                      "Run `claude` once and sign in, or set CLAUDE_CODE_OAUTH_TOKEN in the hub's .env.",
                      "model_signin")
    if low.startswith("codex-local"):
        if shutil.which("codex"):
            return Result("ok", "The Codex CLI is installed for the codex-local runtime.", {"runtime": "codex-local"})
        return Result("fail", "Model provider credential is not configured.", {"runtime": "codex-local"},
                      "Install Codex and run `codex login`.", "model_signin")
    provider = low.split("/", 1)[0]
    names = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "openrouter": "OPENROUTER_API_KEY",
             "azure": "AZURE_API_KEY", "gemini": "GEMINI_API_KEY"}
    name = names.get(provider)
    if name and not (os.environ.get(name) or "").strip():
        return Result("fail", "Model provider credential is not configured.", {"runtime": "openai-agents",
                                                                               "provider": provider},
                      f"Set the {provider} key in the hub's .env (Models), then restart.", "model_signin")
    return Result("ok", f"A credential for {provider} is configured.", {"runtime": "openai-agents",
                                                                         "provider": provider})


def check_signin(hub: Path) -> Result:
    from .. import doctor

    return _from_doctor(doctor._web_app_signin(hub))


def check_exposure(hub: Path) -> Result:
    from .. import doctor

    return _from_doctor(doctor._exposure())


def check_scheduler(hub: Path) -> Result:
    from ..access import store_for

    try:
        hold = store_for(hub).schedule_hold()
    except Exception as exc:  # noqa: BLE001
        return Result("warn", f"Scheduler state is unavailable ({type(exc).__name__}).")
    if hold:
        return Result("warn", "Schedules are on hold.", {"reason": scrub(hold.get("reason"))},
                      "Clear the hold with `hubzoid schedule resume` when ready.", "schedule_hold")
    return Result("ok", "Schedules are not on hold.")


def check_connectors(hub: Path) -> Result:
    from .. import db

    try:
        with db.operational_engine(hub).connect() as conn:
            count = conn.execute(sa.text("SELECT COUNT(*) FROM hz_connectors")).scalar() or 0
    except Exception:  # noqa: BLE001
        return Result("info", "No connector registry yet.")
    return Result("ok" if count else "info", f"{count} connector(s) registered.", {"connectors": int(count)})


def check_telemetry(hub: Path) -> Result:
    if (os.environ.get("HUBZOID_OTEL_ENDPOINT") or "").strip():
        return Result("ok", "Telemetry export is configured.")
    return Result("info", "No telemetry destination is configured (optional).")


def check_chat_config(hub: Path) -> Result:
    from ..chat import config as chat_cfg

    state = chat_cfg.service_for(hub).file_state()
    if not state.get("exists"):
        return Result("ok", "No chat/config.yaml: product defaults apply.")
    if state.get("valid") is False:
        return Result("fail", f"chat/config.yaml is invalid: {state['error']['message']}",
                      {"line": state["error"].get("line")},
                      "Fix the file, then run `hubzoid chat config validate`. The last applied revision stays active.",
                      "chat_config_invalid")
    if not state.get("matches_active"):
        return Result("warn", "chat/config.yaml has changes that are not applied.", {"pending": state.get("pending_changes")},
                      "Review with `hubzoid chat config diff`, then apply.", "chat_config_pending")
    return Result("ok", "chat/config.yaml matches the active revision.")


def check_branding(hub: Path) -> Result:
    from ..ui import branding_store

    svc = branding_store.service_for(hub)
    src = svc.active_source()
    if src is None:
        return Result("info", "No brand activated: the default brand shows.")
    missing = [a["key"] for a in src["assets"] if svc.asset_bytes(a["digest"]) is None]
    if missing:
        return Result("fail", f"{len(missing)} brand file(s) of the active revision are missing.", {"missing": missing},
                      "Roll back to an earlier revision or activate a new draft.", "branding_assets_missing")
    return Result("ok", f"Brand generation {src['generation']} is active and complete.")


def check_access(hub: Path) -> Result:
    from ..access import store_for
    from ..access.store import MANAGE_ACCESS, ORG

    try:
        admins = [s for s, d, p in store_for(hub).list_grants(ORG) if p == MANAGE_ACCESS]
    except Exception as exc:  # noqa: BLE001
        return Result("fail", f"The access store is unavailable ({type(exc).__name__}).")
    if not admins:
        return Result("warn", "Nobody can administer this deployment.",
                      remediation="Run `hubzoid access bootstrap --admin you@example.com`.",
                      remediation_code="no_admin")
    return Result("ok", f"{len(admins)} organization administrator(s).", {"admins": len(admins)})


def check_connector_reachability(hub: Path) -> Result:
    """Active: TCP reachability of each registered connector host (no request
    body, no credentials, a fixed short timeout)."""
    from urllib.parse import urlparse

    from .. import db

    try:
        with db.operational_engine(hub).connect() as conn:
            urls = [r[0] for r in conn.execute(sa.text("SELECT url FROM hz_connectors"))]
    except Exception:  # noqa: BLE001
        urls = []
    if not urls:
        return Result("skipped", "No connectors to reach.")
    down = []
    for url in urls[:20]:
        parsed = urlparse(url)
        host, port = parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection((host, port), timeout=3):
                pass
        except OSError:
            down.append(host)
    if down:
        return Result("warn", f"{len(down)} of {len(urls)} connector host(s) did not answer.", {"unreachable": down},
                      "Check the connector's URL in the agent's Connectors tab.", "connector_unreachable")
    return Result("ok", f"All {len(urls)} connector host(s) answered.")


CHECKS: tuple[CheckDef, ...] = (
    CheckDef("storage.operational", "passive", "The operational database answers.", "HUB-109", check_storage),
    CheckDef("storage.schema", "passive", "The database schema is current.", "HUB-109", check_schema),
    CheckDef("storage.disk", "passive", "Free space on the deployment disk.", "HUB-109", check_disk),
    CheckDef("model.signin", "passive", "The model runtime has a sign-in or key.", "HUB-101", check_model_auth),
    CheckDef("auth.signin", "passive", "How people sign in.", "HUB-86", check_signin),
    CheckDef("exposure.bind", "passive", "Which address the app listens on.", "HUB-86", check_exposure),
    CheckDef("access.admins", "passive", "Someone can administer the deployment.", "HUB-39", check_access),
    CheckDef("scheduler.state", "passive", "Schedules are running.", "HUB-91", check_scheduler),
    CheckDef("connectors.registry", "passive", "Connectors are registered.", "HUB-102", check_connectors),
    CheckDef("telemetry.config", "passive", "Telemetry export is set up.", "HUB-109", check_telemetry),
    CheckDef("config.chat", "passive", "chat/config.yaml is valid and applied.", "HUB-71", check_chat_config),
    CheckDef("config.branding", "passive", "The active brand is complete.", "HUB-82", check_branding),
    CheckDef("connectors.reachability", "active", "Connector hosts answer (network probe).", "HUB-102",
             check_connector_reachability),
)
BY_ID = {c.id: c for c in CHECKS}


def catalog() -> list[dict]:
    return [{"id": c.id, "profile": c.profile, "purpose": c.purpose, "owner": c.owner,
             "required_capability": "admin.diagnostics.network" if c.profile == "active" else "admin.diagnostics.run"}
            for c in CHECKS]


# -- reports ------------------------------------------------------------------------
_pools: dict[str, cf.ThreadPoolExecutor] = {}
_pools_lock = threading.Lock()
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class DiagnosticsService:
    def __init__(self, hub_dir: Path, engine=None):
        from .. import db, migrations

        self.hub_dir = Path(hub_dir)
        self.root = deploy.root(self.hub_dir)
        self.deployment_id = deploy.deployment_id(self.hub_dir)
        if engine is None:
            engine = db.operational_engine(self.hub_dir)
            migrations.upgrade(engine, "operational")
        self.engine = engine
        self.bundle_dir = self.root / ".hubzoid" / "admin" / "bundles"

    def _pool(self) -> cf.ThreadPoolExecutor:
        with _pools_lock:
            pool = _pools.get(self.deployment_id)
            if pool is None:
                pool = cf.ThreadPoolExecutor(max_workers=MAX_RUNNING, thread_name_prefix="hz-diagnostics")
                _pools[self.deployment_id] = pool
            return pool

    # -- recovery ----------------------------------------------------------------
    def recover(self, now: float | None = None) -> int:
        """Reports whose worker died (lease expired) become ``interrupted``.
        Never rerun. Also removes expired support bundles."""
        now = time.time() if now is None else now
        with self.engine.begin() as conn:
            n = conn.execute(reports.update().where(
                reports.c.deployment_id == self.deployment_id,
                reports.c.status.in_(("queued", "running")),
                sa.or_(reports.c.lease_expires_at.is_(None) & (reports.c.created_at < now - LEASE_SECONDS),
                       reports.c.lease_expires_at < now))
                .values(status="interrupted", completed_at=now, error_code="worker_lost")).rowcount
        self.purge(now)
        return n

    def purge(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.engine.begin() as conn:
            expired = conn.execute(sa.select(bundles).where(bundles.c.deployment_id == self.deployment_id,
                                                            bundles.c.expires_at < now,
                                                            bundles.c.status != "expired")).all()
            for b in expired:
                if b.relative_storage_key:
                    (self.bundle_dir / b.relative_storage_key).unlink(missing_ok=True)
                conn.execute(bundles.update().where(bundles.c.id == b.id).values(status="expired"))
            conn.execute(reports.delete().where(reports.c.deployment_id == self.deployment_id,
                                                reports.c.expires_at.is_not(None), reports.c.expires_at < now))
        # Orphaned files (a crash between write and commit).
        if self.bundle_dir.is_dir():
            with self.engine.connect() as conn:
                known = {r[0] for r in conn.execute(sa.select(bundles.c.relative_storage_key)
                                                    .where(bundles.c.status == "ready"))}
            for f in self.bundle_dir.iterdir():
                if f.name not in known and f.stat().st_mtime < now - 300:
                    f.unlink(missing_ok=True)

    # -- requests -----------------------------------------------------------------
    def start(self, *, actor: str, profile: str = "passive", check_ids: list[str] | None = None,
              hub_id: str | None = None, consent_network: bool = False, idempotency_key: str | None = None,
              previous_report_id: str | None = None, run_inline: bool = False) -> dict:
        if profile not in ("passive", "active"):
            raise AdminError(400, "invalid_request", "profile is passive or active.")
        settings = config_service(self.hub_dir).diagnostics_settings()
        selected = [c for c in CHECKS if (check_ids is None and (c.profile == "passive" or profile == "active"))
                    or (check_ids is not None and c.id in check_ids)]
        if check_ids:
            unknown = sorted(set(check_ids) - set(BY_ID))
            if unknown:
                raise AdminError(422, "validation_failed", f"Unknown check {unknown[0]!r}.")
        if any(c.profile == "active" for c in selected):
            if not settings["allow_network_probes"]:
                raise AdminError(403, "permission_denied",
                                 "Network probes are off. Set diagnostics.allow_network_probes in admin.yaml first.")
            if not consent_network:
                raise AdminError(422, "validation_failed",
                                 "Active checks contact other services. Confirm with consent_network: true.")
        now = time.time()
        rhash = hashlib.sha256(json.dumps([profile, sorted(c.id for c in selected), hub_id]).encode()).hexdigest()
        retention = None  # reports are kept until deleted (B037)
        with self.engine.begin() as conn:
            if idempotency_key:
                prior = conn.execute(sa.select(reports).where(
                    reports.c.deployment_id == self.deployment_id, reports.c.requested_by_user_id == actor,
                    reports.c.idempotency_key == idempotency_key)).first()
                if prior is not None:
                    if prior.request_hash != rhash:
                        raise AdminError(409, "idempotency_key_reused",
                                         "That Idempotency-Key was used for a different request.")
                    return {"report_id": prior.id, "status": prior.status, "replayed": True}
            live = conn.execute(sa.select(sa.func.count()).select_from(reports).where(
                reports.c.deployment_id == self.deployment_id,
                reports.c.status.in_(("queued", "running")))).scalar() or 0
            if live >= MAX_RUNNING + MAX_QUEUED:
                raise AdminError(429, "rate_limited", "Too many diagnostic reports are waiting. Try again shortly.",
                                 retry_after=30)
            report_id = str(uuid.uuid4())
            conn.execute(reports.insert().values(
                id=report_id, deployment_id=self.deployment_id, hub_id=hub_id, requested_by_user_id=actor,
                profile=profile, check_ids_json=json.dumps([c.id for c in selected]), status="queued",
                created_at=now, expires_at=(now + retention * 86400) if retention else None,
                config_revision=config_service(self.hub_dir).active().revision,
                idempotency_key=idempotency_key, request_hash=rhash, cancel_requested=0,
                previous_report_id=previous_report_id))
            from ..control import audit

            audit.write(conn, actor, "diagnostic.requested", subject=report_id, hub=hub_id,
                        detail={"profile": profile, "checks": len(selected)}, surface="admin")
        if run_inline:
            self._run(report_id)
        else:
            self._pool().submit(self._run, report_id)
        return {"report_id": report_id, "status": "queued", "location": f"/api/admin/diagnostics/reports/{report_id}"}

    def _claim(self, report_id: str) -> bool:
        now = time.time()
        with self.engine.begin() as conn:
            return conn.execute(reports.update().where(
                reports.c.id == report_id, reports.c.status == "queued")
                .values(status="running", started_at=now, lease_owner=WORKER_ID,
                        lease_expires_at=now + LEASE_SECONDS)).rowcount == 1

    def _renew(self, report_id: str) -> bool:
        with self.engine.begin() as conn:
            return conn.execute(reports.update().where(
                reports.c.id == report_id, reports.c.lease_owner == WORKER_ID, reports.c.status == "running")
                .values(lease_expires_at=time.time() + LEASE_SECONDS)).rowcount == 1

    def _cancelled(self, report_id: str) -> bool:
        with self.engine.connect() as conn:
            return bool(conn.execute(sa.select(reports.c.cancel_requested).where(reports.c.id == report_id)).scalar())

    def _run(self, report_id: str) -> None:
        try:
            if not self._claim(report_id):
                return
            settings = config_service(self.hub_dir).diagnostics_settings()
            with self.engine.connect() as conn:
                row = conn.execute(sa.select(reports).where(reports.c.id == report_id)).first()
            selected = [BY_ID[c] for c in json.loads(row.check_ids_json or "[]") if c in BY_ID]
            deadline = time.monotonic() + REPORT_WALL_SECONDS
            timeout = settings["check_timeout_seconds"]
            pool = cf.ThreadPoolExecutor(max_workers=settings["max_parallel_checks"], thread_name_prefix="hz-check")
            last_renew = time.monotonic()
            status = "completed"
            try:
                pending = list(selected)
                running: dict[cf.Future, tuple[CheckDef, float]] = {}
                while pending or running:
                    if self._cancelled(report_id):
                        status = "cancelled"
                        for c in pending:
                            self._record(report_id, c, Result("skipped", "Cancelled before it ran."), 0)
                        pending = []
                    while pending and len(running) < settings["max_parallel_checks"]:
                        c = pending.pop(0)
                        running[pool.submit(c.fn, self.hub_dir)] = (c, time.monotonic())
                    done, _ = cf.wait(list(running), timeout=0.5, return_when=cf.FIRST_COMPLETED)
                    now = time.monotonic()
                    for fut in list(running):
                        c, began = running[fut]
                        if fut in done:
                            try:
                                result = fut.result()
                            except Exception as exc:  # noqa: BLE001 - a broken check fails, the report goes on
                                result = Result("fail", f"The check raised {type(exc).__name__}.")
                            self._record(report_id, c, result, int((now - began) * 1000))
                            del running[fut]
                        elif now - began > timeout or now > deadline:
                            self._record(report_id, c, Result(
                                "timeout", f"{c.id} did not finish within {timeout} s.",
                                remediation=f"Inspect the service that owns {c.id} ({c.owner}).",
                                remediation_code="check_timeout"), int((now - began) * 1000))
                            fut.cancel()
                            del running[fut]
                    if now > deadline:
                        for c in pending:
                            self._record(report_id, c, Result("timeout", "The report ran out of time."), 0)
                        pending = []
                    if now - last_renew > RENEW_SECONDS:
                        if not self._renew(report_id):
                            return  # lease lost: someone marked it interrupted
                        last_renew = now
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            with self.engine.begin() as conn:
                conn.execute(reports.update().where(reports.c.id == report_id, reports.c.lease_owner == WORKER_ID)
                             .values(status=status, completed_at=time.time(), lease_expires_at=None))
                from ..control import audit

                audit.write(conn, row.requested_by_user_id, "diagnostic.completed", subject=report_id,
                            hub=row.hub_id, detail={"status": status}, surface="admin")
        except Exception:  # noqa: BLE001 - recorded as failed, never crashes the bridge
            log.exception("diagnostics: report %s failed", report_id)
            with self.engine.begin() as conn:
                conn.execute(reports.update().where(reports.c.id == report_id)
                             .values(status="failed", completed_at=time.time(), error_code="internal"))

    def _record(self, report_id: str, c: CheckDef, r: Result, ms: int) -> None:
        details = scrub(r.details)
        raw = json.dumps(details, default=str)
        if len(raw) > DETAIL_LIMIT:
            raw = json.dumps({"truncated": True})
        now = time.time()
        with self.engine.begin() as conn:
            exists = conn.execute(sa.select(checks_table.c.check_id).where(
                checks_table.c.report_id == report_id, checks_table.c.check_id == c.id)).first()
            if exists:
                return
            conn.execute(checks_table.insert().values(
                report_id=report_id, check_id=c.id, attempt=1, status=r.status, duration_ms=ms,
                summary=scrub(r.summary), safe_details_json=raw, remediation_code=r.remediation_code,
                remediation=r.remediation, owner_domain=c.owner, owner_resource_id=None,
                started_at=now - ms / 1000, completed_at=now))

    def cancel(self, report_id: str, *, actor: str) -> dict:
        with self.engine.begin() as conn:
            row = conn.execute(sa.select(reports).where(reports.c.id == report_id,
                                                        reports.c.deployment_id == self.deployment_id)).first()
            if row is None:
                raise AdminError(404, "not_found", "That report was not found.")
            if row.status == "queued":
                conn.execute(reports.update().where(reports.c.id == report_id)
                             .values(status="cancelled", cancel_requested=1, completed_at=time.time()))
            elif row.status == "running":
                conn.execute(reports.update().where(reports.c.id == report_id).values(cancel_requested=1))
        return self.get(report_id)

    def get(self, report_id: str) -> dict:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(reports).where(reports.c.id == report_id,
                                                        reports.c.deployment_id == self.deployment_id)).first()
            if row is None:
                raise AdminError(404, "not_found", "That report was not found.")
            if row.expires_at is not None and row.expires_at < time.time():
                raise AdminError(410, "report_expired", "That report expired. Run a fresh one.")
            rows = conn.execute(sa.select(checks_table).where(checks_table.c.report_id == report_id)).all()
        order = {c.id: i for i, c in enumerate(CHECKS)}
        checks = sorted(({"check_id": r.check_id, "status": r.status, "summary": r.summary,
                          "details": json.loads(r.safe_details_json or "{}"), "duration_ms": r.duration_ms,
                          "remediation": r.remediation, "remediation_code": r.remediation_code,
                          "owner": r.owner_domain} for r in rows), key=lambda c: order.get(c["check_id"], 99))
        counts = {s: sum(1 for c in checks if c["status"] == s) for s in STATUSES}
        return {"report_id": row.id, "status": row.status, "profile": row.profile, "hub_id": row.hub_id,
                "requested_by": row.requested_by_user_id, "created_at": row.created_at,
                "started_at": row.started_at, "completed_at": row.completed_at, "expires_at": row.expires_at,
                "error_code": row.error_code, "planned": json.loads(row.check_ids_json or "[]"),
                "checks": checks, "counts": counts, "previous_report_id": row.previous_report_id}

    def list(self, *, hub_ids: list[str] | None = None, limit: int = 50, cursor: str | None = None) -> dict:
        limit = max(1, min(limit, 200))
        stmt = sa.select(reports).where(reports.c.deployment_id == self.deployment_id)
        if hub_ids is not None:
            stmt = stmt.where(reports.c.hub_id.in_(hub_ids))
        if cursor:
            try:
                ts, rid = cursor.split("|", 1)
                stmt = stmt.where(sa.or_(reports.c.created_at < float(ts),
                                         sa.and_(reports.c.created_at == float(ts), reports.c.id < rid)))
            except ValueError:
                raise AdminError(400, "invalid_request", "That page link is not valid.")
        with self.engine.connect() as conn:
            rows = conn.execute(stmt.order_by(reports.c.created_at.desc(), reports.c.id.desc())
                                .limit(limit + 1)).all()
        items = [{"report_id": r.id, "status": r.status, "profile": r.profile, "hub_id": r.hub_id,
                  "requested_by": r.requested_by_user_id, "created_at": r.created_at,
                  "completed_at": r.completed_at} for r in rows[:limit]]
        nxt = f"{rows[limit - 1].created_at}|{rows[limit - 1].id}" if len(rows) > limit else None
        return {"items": items, "next_cursor": nxt}

    def hub_of(self, report_id: str) -> str | None:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(reports.c.hub_id).where(reports.c.id == report_id,
                                                                 reports.c.deployment_id == self.deployment_id)).first()
        if row is None:
            raise AdminError(404, "not_found", "That report was not found.")
        return row.hub_id

    # -- bundles ------------------------------------------------------------------
    def bundle(self, report_id: str, *, actor: str) -> dict:
        report = self.get(report_id)
        if report["status"] in ("queued", "running"):
            raise AdminError(409, "run_in_progress", "Wait for the report to finish, then make a bundle.")
        settings = config_service(self.hub_dir).diagnostics_settings()
        from .. import doctor

        versions = scrub(doctor._versions().detail or {})
        provenance = [{"domain": d["domain"], "file": d.get("file"), "revision": d.get("revision"),
                       "owner": d.get("owner")} for d in _domains(self.hub_dir)]
        docs = {
            "checks.json": {"report_id": report_id, "status": report["status"], "profile": report["profile"],
                            "checks": report["checks"], "counts": report["counts"]},
            "versions.json": versions,
            "config-provenance.json": provenance,
        }
        manifest = {"format": "hubzoid-support-bundle-v1", "report_id": report_id, "created_at": time.time(),
                    "files": {name: hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
                              for name, body in docs.items()},
                    "omitted": ["message bodies", "prompts", "attachments", "environment", "secrets", "raw logs",
                                "host paths"]}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))
            for name, body in docs.items():
                zf.writestr(name, json.dumps(body, indent=2, sort_keys=True, default=str))
        data = buf.getvalue()
        if len(data) > 10 * 1024 * 1024:
            raise AdminError(413, "too_large", "The bundle would be larger than 10 MiB.")
        bundle_id = str(uuid.uuid4())
        key = f"{bundle_id}.zip"
        self.bundle_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.bundle_dir / f".{key}.tmp"
        tmp.write_bytes(data)
        tmp.replace(self.bundle_dir / key)
        now = time.time()
        with self.engine.begin() as conn:
            conn.execute(bundles.insert().values(
                id=bundle_id, report_id=report_id, deployment_id=self.deployment_id, created_by_user_id=actor,
                created_at=now, expires_at=now + settings["bundle_ttl_minutes"] * 60, status="ready",
                relative_storage_key=key, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data),
                manifest_json=json.dumps(manifest, default=str)))
            from ..control import audit

            audit.write(conn, actor, "bundle.created", subject=bundle_id, detail={"report": report_id},
                        surface="admin")
        return self.bundle_info(bundle_id)

    def bundle_info(self, bundle_id: str) -> dict:
        with self.engine.connect() as conn:
            b = conn.execute(sa.select(bundles).where(bundles.c.id == bundle_id,
                                                      bundles.c.deployment_id == self.deployment_id)).first()
        if b is None:
            raise AdminError(404, "not_found", "That bundle was not found.")
        if b.status != "ready" or b.expires_at < time.time():
            raise AdminError(410, "report_expired", "That bundle expired. Make a new one from the report.")
        return {"bundle_id": b.id, "report_id": b.report_id, "sha256": b.sha256, "size_bytes": b.size_bytes,
                "expires_at": b.expires_at, "manifest": json.loads(b.manifest_json or "{}"),
                "download": f"/api/admin/diagnostics/bundles/{b.id}/download"}

    def bundle_bytes(self, bundle_id: str, *, actor: str) -> bytes:
        info = self.bundle_info(bundle_id)
        data = (self.bundle_dir / f"{bundle_id}.zip").read_bytes()
        if hashlib.sha256(data).hexdigest() != info["sha256"]:
            raise AdminError(503, "unavailable", "The bundle file changed on disk. Make a new one.")
        with self.engine.begin() as conn:
            from ..control import audit

            audit.write(conn, actor, "bundle.downloaded", subject=bundle_id, surface="admin")
        return data

    def bundle_report(self, bundle_id: str) -> str:
        return self.bundle_info(bundle_id)["report_id"]


def _domains(hub: Path) -> list[dict]:
    from .config import domains

    return domains(hub)


_services: dict[str, DiagnosticsService] = {}
_lock = threading.Lock()


def service_for(hub_dir: Path) -> DiagnosticsService:
    key = str(Path(hub_dir).resolve())
    with _lock:
        svc = _services.get(key)
        if svc is None:
            svc = DiagnosticsService(Path(hub_dir))
            _services[key] = svc
        return svc
