# Hubzoid. MIT licensed like the rest of the repository.
"""`hubzoid doctor`: checks a hub and its deployment, for people and for scripts.

Every check has a stable id (`auth.bridge_keys`, `db.operational`, ...) that
scripts and monitoring can key on. Ids are only ever added, never renamed.
Statuses: `ok`, `info` (worth knowing), `warn` (works, but needs attention) and
`fail` (will not work, or is unsafe). `hubzoid doctor` exits 1 when any check
fails.

Doctor reads only. It does not create databases, run migrations or start the
engine, so it is safe against a running deployment.
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

FORMAT = 1
_PACKAGES = ("hubzoid", "open-webui", "dbos", "litellm", "openai-agents", "claude-agent-sdk",
             "alembic", "sqlalchemy", "casbin", "fastmcp")
_BACKUP_WARN_DAYS = 7


@dataclass
class Check:
    id: str
    status: str  # ok | info | warn | fail
    summary: str
    detail: dict | list | None = field(default=None)


def _sqlite_missing(url: str) -> bool:
    return url.startswith("sqlite") and not Path(url.split(":///", 1)[-1]).exists()


def _hub_checks(hub: Path) -> list[Check]:
    out: list[Check] = []
    out.append(Check("hub.agents_md", "ok", "AGENTS.md present") if (hub / "AGENTS.md").is_file()
               else Check("hub.agents_md", "fail", "AGENTS.md is missing at the hub root"))
    out.append(Check("hub.env", "ok", ".env present") if (hub / ".env").is_file()
               else Check("hub.env", "info", "No .env; settings come from the environment"))
    try:
        from . import runtime as runtime_lib

        rt = runtime_lib.build(hub)
        out.append(Check("runtime.build", "ok", f"Agent builds: {rt.name!r} via {type(rt).__name__}"))
    except Exception as exc:  # noqa: BLE001
        out.append(Check("runtime.build", "fail", f"Agent does not build: {type(exc).__name__}: {exc}"))

    try:
        from . import scheduling as sch

        tasks, problems = sch.load_tasks(hub)
        enabled = [t.name for t in tasks if t.enabled]
        if problems:
            out.append(Check("schedule.tasks", "fail", f"{len(problems)} schedule file(s) are invalid",
                             [f"schedule/{p}" for p in problems]))
        elif tasks:
            out.append(Check("schedule.tasks", "ok", f"{len(enabled)} enabled task(s)",
                             sorted(t.name for t in tasks)))
    except Exception as exc:  # noqa: BLE001
        out.append(Check("schedule.tasks", "fail", f"Schedules could not be read: {exc}"))

    from .workflows.observe import definitions

    defs = definitions(hub)
    broken = [f"{w['source']}: {w['error']}" for w in defs if w["error"]]
    if broken:
        out.append(Check("workflows.definitions", "fail", f"{len(broken)} workflow(s) do not load", broken))
    elif defs:
        out.append(Check("workflows.definitions", "ok", f"{len(defs)} workflow(s)",
                         [f"{w['name']} ({w['schedule'] or 'manual'}, {w['timezone']})" for w in defs]))

    try:
        from . import access

        restricted = access.load_restricted(hub)
        if restricted:
            out.append(Check("access.restricted", "info", f"{len(restricted)} restricted tool(s)",
                             sorted({p for _, p in restricted})))
    except Exception as exc:  # noqa: BLE001
        out.append(Check("access.restricted", "fail", f"Restricted tools do not load: {exc}"))
    try:
        from .access.resolver import load_resolver

        if load_resolver(hub) is not None:
            out.append(Check("identity.resolver", "info", "Roster resolver present (identity/access)"))
    except Exception as exc:  # noqa: BLE001
        out.append(Check("identity.resolver", "fail", f"Identity roster does not load: {exc}"))
    return out


def _versions() -> Check:
    from importlib.metadata import PackageNotFoundError, version

    found = {}
    for name in _PACKAGES:
        try:
            found[name] = version(name)
        except PackageNotFoundError:
            found[name] = None
    return Check("deps.versions", "info", f"hubzoid {found.get('hubzoid')}", found)


def _sqlite(hub: Path) -> Check | None:
    import sqlite3

    from . import db
    from .workflows.runtime import sqlite_problem

    try:
        url = db.dbos_url(hub)
    except Exception:  # noqa: BLE001 — reported by the db checks
        return None
    problem = sqlite_problem(url)
    if problem:
        return Check("deps.sqlite", "fail", problem[0].upper() + problem[1:], {"sqlite": sqlite3.sqlite_version})
    if url.startswith("sqlite"):
        return Check("deps.sqlite", "ok", f"SQLite {sqlite3.sqlite_version}", {"sqlite": sqlite3.sqlite_version})
    return None


def _schema(hub: Path) -> list[Check]:
    from sqlalchemy import create_engine

    from . import db, migrations

    out = []
    for check_id, store, url_fn in (("db.operational", "operational", db.operational_url),
                                    ("db.hub", "hub", db.resolve_url)):
        try:
            url = url_fn(hub)
        except Exception as exc:  # noqa: BLE001 — e.g. a manifest disagreeing with the env
            out.append(Check(check_id, "fail", f"Database is misconfigured: {exc}"))
            continue
        if _sqlite_missing(url):
            out.append(Check(check_id, "info", "Not created yet; created at first start"))
            continue
        engine = create_engine(url)
        try:
            cur, head = migrations.current(engine, store), migrations.head(store)
        except Exception as exc:  # noqa: BLE001
            out.append(Check(check_id, "fail", f"Database unreachable: {type(exc).__name__}: {exc}"))
            continue
        finally:
            engine.dispose()
        if cur == head:
            out.append(Check(check_id, "ok", f"Schema at {head}"))
        elif cur is None:
            out.append(Check(check_id, "info", f"Unversioned; upgraded to {head} at next start"))
        else:
            try:
                migrations._script(store)[1].get_revision(cur)
                known = True
            except Exception:  # noqa: BLE001
                known = False
            out.append(Check(check_id, "warn", f"Schema at {cur}; upgraded to {head} at next start")
                       if known else
                       Check(check_id, "fail", f"Schema at unknown revision {cur}: written by a newer "
                                               "Hubzoid. Upgrade Hubzoid or restore a backup."))
    return out


def _auth() -> list[Check]:
    from .webui import _TRUTHY, _validate_auth_env

    out = []
    keys = [k.strip() for k in os.environ.get("BRIDGE_API_KEYS", "").split(",") if k.strip()]
    if not keys:
        out.append(Check("auth.bridge_keys", "fail",
                         "BRIDGE_API_KEYS is not set, so the bridge accepts the public key 'dev'"))
    elif "dev" in keys:
        out.append(Check("auth.bridge_keys", "fail", "BRIDGE_API_KEYS includes the public key 'dev'"))
    elif any(len(k) < 16 for k in keys):
        out.append(Check("auth.bridge_keys", "warn", "A bridge key is shorter than 16 characters"))
    else:
        out.append(Check("auth.bridge_keys", "ok", f"{len(keys)} bridge key(s) set"))

    auth_on = os.environ.get("WEBUI_AUTH", "").strip().lower() in _TRUTHY
    if auth_on:
        try:
            _validate_auth_env(dict(os.environ))
            out.append(Check("auth.chat_signin", "ok", "Chat app sign-in is on"))
        except RuntimeError as exc:
            out.append(Check("auth.chat_signin", "fail", str(exc).splitlines()[0]))
    else:
        exposed = _exposed()
        out.append(Check("auth.chat_signin", "fail" if exposed else "info",
                         "Chat app sign-in is off and the port is exposed" if exposed
                         else "Chat app sign-in is off (fine for local use only)"))
    return out


def _exposed() -> bool:
    return os.environ.get("HUBZOID_HOST", "127.0.0.1").strip() in ("0.0.0.0", "::", "")


def _exposure() -> Check:
    host = os.environ.get("HUBZOID_HOST", "127.0.0.1").strip() or "0.0.0.0"
    if _exposed():
        return Check("exposure.bind", "warn",
                     f"The public port listens on {host} (all interfaces). Put TLS in front of it.",
                     {"host": host, "bridge": "127.0.0.1"})
    return Check("exposure.bind", "ok", f"The public port listens on {host}; bridges stay on 127.0.0.1",
                 {"host": host, "bridge": "127.0.0.1"})


def _model(hub: Path) -> Check:
    from . import runtime as runtime_lib, settings as settingslib

    model = runtime_lib._resolve_model_id(hub, settingslib.load(hub))
    if model == "claude-local" or model.startswith("claude-local/"):
        if any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")):
            return Check("model.credentials", "ok", f"{model}: Anthropic credentials set")
        if shutil.which("claude"):
            return Check("model.credentials", "info",
                         f"{model}: uses the local `claude` login (check it with `claude /status`)")
        return Check("model.credentials", "fail",
                     f"{model}: no ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN or `claude` login")
    try:
        import litellm

        status = litellm.validate_environment(model=model)
    except Exception as exc:  # noqa: BLE001
        return Check("model.credentials", "warn", f"{model}: could not check credentials ({exc})")
    if status.get("keys_in_environment"):
        return Check("model.credentials", "ok", f"{model}: credentials set")
    missing = status.get("missing_keys") or []
    return Check("model.credentials", "fail",
                 f"{model}: missing {', '.join(missing) or 'provider credentials'}", missing)


def _meta(engine, key: str, default=None):
    import json

    from sqlalchemy import text

    with engine.connect() as c:
        row = c.execute(text("SELECT v FROM hz_meta WHERE k=:k"), {"k": key}).fetchone()
    return json.loads(row[0]) if row and row[0] else default


def _store_checks(hub: Path) -> list[Check]:
    """Checks that read the operational store. Skipped when it does not exist yet."""
    from sqlalchemy import create_engine, inspect

    from . import db

    try:
        url = db.operational_url(hub)
    except Exception:  # noqa: BLE001 — reported by db.operational
        return []
    if _sqlite_missing(url):
        return []
    engine = create_engine(url)
    out: list[Check | None] = []
    try:
        if not inspect(engine).has_table("hz_meta"):
            return []
        last = _meta(engine, "backup:last")
        if not last:
            out.append(Check("backup.age", "warn", "No backup recorded. Run `hubzoid backup`."))
        else:
            days = (time.time() - float(last.get("at", 0))) / 86400
            out.append(Check("backup.age", "warn" if days > _BACKUP_WARN_DAYS else "ok",
                             f"Last backup {days:.1f} days ago", last))
        out.append(_scheduler(hub, engine))
    finally:
        engine.dispose()
    return [c for c in out if c is not None]


def _scheduler(hub: Path, engine) -> Check | None:
    from . import scheduling as sch
    from .access.identity import normalize
    from .workflows.observe import _stale, definitions

    tasks, _ = sch.load_tasks(hub)
    defs = definitions(hub)
    if not tasks and not defs:
        return None
    detail = {"paused": sorted(_meta(engine, "workflow_paused:" + normalize(hub.name), []))}
    hold = _meta(engine, "maintenance:hold")
    if hold and float(hold.get("until", 0)) > time.time():
        return Check("scheduler.health", "warn", "New scheduled runs are held by a backup in progress", hold)
    health = _meta(engine, "workflow_health:" + normalize(hub.name), {})
    if defs and health:
        detail["heartbeat"] = health.get("heartbeat")
        if health.get("error"):
            return Check("scheduler.health", "fail", f"Workflow dispatch failed: {health['error']}", detail)
        if health.get("enabled") and _stale(health.get("heartbeat")):
            return Check("scheduler.health", "warn",
                         "No dispatcher heartbeat recently: the hub is not running or has stopped", detail)
    if detail["paused"]:
        return Check("scheduler.health", "info", f"{len(detail['paused'])} paused", detail)
    return Check("scheduler.health", "ok", "Scheduled work is not held or paused", detail)


def run(hub: Path) -> list[Check]:
    hub = Path(hub).resolve()
    checks = _hub_checks(hub)  # loads the hub's .env through runtime.build
    from . import settings as settingslib

    settingslib.load(hub)
    checks.append(_versions())
    sqlite_check = _sqlite(hub)
    if sqlite_check:
        checks.append(sqlite_check)
    checks += _schema(hub)
    checks += _auth()
    checks.append(_exposure())
    try:
        checks.append(_model(hub))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("model.credentials", "warn", f"Could not check the model: {exc}"))
    try:
        checks += _store_checks(hub)
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("db.read", "fail", f"Operational store unreadable: {type(exc).__name__}: {exc}"))
    return checks


def report(hub: Path, checks: list[Check]) -> dict:
    from . import __version__

    return {"format": FORMAT, "hub": str(Path(hub).resolve()), "hubzoid": __version__,
            "ok": not any(c.status == "fail" for c in checks),
            "checks": [asdict(c) for c in checks]}
