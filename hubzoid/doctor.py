# Hubzoid. Apache-2.0 licensed like the rest of the repository.
"""`hubzoid doctor`: checks a hub and its deployment, for people and for scripts.

Every check has a stable id (`auth.bridge_keys`, `db.operational`, ...) that
scripts and monitoring can key on. Ids are only ever added, never renamed.
Statuses: `ok`, `info` (worth knowing), `warn` (works, but needs attention) and
`fail` (will not work, or is unsafe). `hubzoid doctor` exits 1 when any check
fails.

Doctor reads only. It does not create databases, run migrations or start the
engine, so it is safe against a running deployment. It reads each named AWS
secret once to prove it is reachable (skip with `fetch_secrets=False`, the
`--skip-secret-fetch` flag). It reports key names and sources, never values.
"""
from __future__ import annotations

import os
import shutil
import time
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from pathlib import Path

FORMAT = 1
_PACKAGES = ("hubzoid", "open-webui", "dbos", "litellm", "openai-agents", "claude-agent-sdk",
             "alembic", "sqlalchemy", "casbin", "fastmcp", "pwdlib", "authlib")
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
        if type(rt).__name__ == "CodexRuntime":
            from .factory_codex import codex_available, SUPPORTED_CODEX_VERSION
            ready = codex_available()
            out.append(Check("runtime.codex_login", "ok" if ready else "fail",
                             "Codex CLI login available" if ready else f"Install Codex CLI {SUPPORTED_CODEX_VERSION} and complete file-backed codex login as the service user"))
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


_SECRET_CHECK_IDS = {"deployment": "secrets.deployment", "hub": "secrets.hub", "restricted": "secrets.restricted"}


def _config_checks(hub: Path, *, fetch_secrets: bool) -> tuple[list[Check], dict[str, str], bool]:
    """Configuration layers and AWS secrets, before anything loads the hub.

    Returns (checks, the unfiltered deployment secret, whether a named secret
    could not be read). Names, sources and AWS error classes only."""
    from . import config_secrets as cs

    out: list[Check] = []
    deployment_values: dict[str, str] = {}
    unreadable = False
    for p in cs.pointers(hub):
        check_id = _SECRET_CHECK_IDS[p.layer]
        where = p.name + (f" ({p.region})" if p.region else "")
        detail = {"name": p.name, "region": p.region, "source": p.source}
        if not fetch_secrets:
            out.append(Check(check_id, "info", f"The {p.layer} secret {where} was not read (--skip-secret-fetch)",
                             detail))
            continue
        try:
            values = cs.load_secret(p.name, region=p.region, layer=p.layer)
        except cs.SecretFetchError as exc:
            unreadable = True
            out.append(Check(check_id, "fail", str(exc), detail))
            continue
        detail["keys"] = len(values)
        if p.layer == cs.DEPLOYMENT:
            deployment_values = values
            if p.filtered:
                detail["gateway_only"] = sorted(k for k in values if not cs.bridge_deployment_key(k))
        out.append(Check(check_id, "ok", f"The {p.layer} secret {where} is readable: {len(values)} key(s)", detail))

    from dotenv import dotenv_values

    hub_file = dotenv_values(hub / ".env") if (hub / ".env").is_file() else {}
    restricted_file = (dotenv_values(hub / "restricted" / ".env")
                       if (hub / "restricted" / ".env").is_file() else {})
    ignored = []
    if hub_file.get("AWS_SECRET_NAME") and cs.registered(hub, os.environ, hub_file):
        ignored.append("AWS_SECRET_NAME in the hub .env is ignored for a gateway hub. The gateway's "
                       "environment names the deployment secret. Use HUBZOID_HUB_SECRET_NAME for a hub secret.")
    if not hub_file.get("HUBZOID_HUB_SECRET_NAME") and (
            os.environ.get("HUBZOID_HUB_SECRET_NAME") or restricted_file.get("HUBZOID_HUB_SECRET_NAME")):
        ignored.append("HUBZOID_HUB_SECRET_NAME is read from the hub .env only. It is set elsewhere and ignored.")
    if not restricted_file.get("HUBZOID_RESTRICTED_SECRET_NAME") and (
            os.environ.get("HUBZOID_RESTRICTED_SECRET_NAME") or hub_file.get("HUBZOID_RESTRICTED_SECRET_NAME")):
        ignored.append("HUBZOID_RESTRICTED_SECRET_NAME is read from restricted/.env only. It is set elsewhere "
                       "and ignored.")
    if ignored:
        out.append(Check("secrets.names", "warn", f"{len(ignored)} secret name(s) are set where they are ignored",
                         ignored))

    rows = cs.layer_report(hub, fetch_secrets=fetch_secrets and not unreadable)
    if rows:
        by_layer: dict[str, int] = {}
        for row in rows:
            by_layer[row["layer"]] = by_layer.get(row["layer"], 0) + 1
        summary = ", ".join(f"{n} from the {layer}" for layer, n in by_layer.items())
        out.append(Check("config.layers", "info", f"{len(rows)} configured key(s): {summary}", rows))
    return out, deployment_values, unreadable


def _google_merge(deployment_values: dict[str, str]) -> Check | None:
    """Google sign-in onto a Console-created account needs merge by email."""
    def value(key: str) -> str:
        return (os.environ.get(key) or deployment_values.get(key) or "").strip()

    if not value("GOOGLE_CLIENT_ID"):
        return None
    if value("OAUTH_MERGE_ACCOUNTS_BY_EMAIL").lower() == "true":
        return Check("auth.google_merge", "ok", "Google sign-in merges onto existing accounts by email")
    return Check("auth.google_merge", "warn",
                 "GOOGLE_CLIENT_ID is set without OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true, so people with a "
                 "Console-created account cannot sign in with Google (see docs/auth.md)")


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
        engine = create_engine(db.sqlalchemy_url(url))
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


def _auth(hub: Path | None = None) -> list[Check]:
    from . import appmode
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

    if not appmode.is_legacy(hub):
        out.append(_web_app_signin(hub))
        return out
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


def _web_app_signin(hub: Path | None) -> Check:
    """Sign-in for the Hubzoid web app (HUBZOID_AUTH, or the 1.0 name WEBUI_AUTH).
    Exposure without sign-in is the `exposure.local_mode` check."""
    from . import appmode, upgrade

    if not appmode.auth_enabled(hub):
        return Check("auth.chat_signin", "info",
                     "Sign-in is off: local mode, whoever opens the web app is the hub's owner "
                     "(admin@localhost). Fine on this machine.", {"mode": "local"})
    try:
        accounts = upgrade.hubzoid_accounts(hub) if hub is not None else 0
    except upgrade.AccountsUnreadable as exc:
        return Check("auth.chat_signin", "warn",
                     f"Sign-in is on, but the Hubzoid accounts could not be read ({exc}). "
                     "See the db.operational check.", {"mode": "accounts", "accounts": None})
    bootstrap = any((os.environ.get(k) or "").strip()
                    for k in ("HUBZOID_ADMIN_EMAIL", "WEBUI_ADMIN_EMAIL"))
    detail = {"mode": "accounts", "accounts": accounts, "first_admin_configured": bootstrap}
    if not accounts and not bootstrap:
        return Check("auth.chat_signin", "warn",
                     "Sign-in is on but there is no Hubzoid account yet and no first administrator "
                     "configured: set HUBZOID_ADMIN_EMAIL and HUBZOID_ADMIN_PASSWORD for the first "
                     "start (see `hubzoid admin --help`)", detail)
    return Check("auth.chat_signin", "ok", "Sign-in is on: people sign in with Hubzoid accounts", detail)


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


def _app_checks(hub: Path) -> list[Check]:
    """The web experience: which one runs, what it needs, and what an upgrade
    from Open WebUI still has to do. Read-only like every check."""
    from . import appmode

    summary = appmode.mode_summary(hub)
    legacy = summary["ui_mode"] == appmode.UI_OPENWEBUI
    source = ("HUBZOID_UI" if (os.environ.get("HUBZOID_UI") or "").strip()
              else "deployment record" if legacy else "default")
    out = [Check("ui.mode", "info",
                 "Web app: Open WebUI (legacy mode, removed in a later release)" if legacy
                 else "Web app: Hubzoid", {**summary, "source": source})]
    if legacy:
        out.append(_openwebui_extra())
    else:
        found = _openwebui_data(hub, auth_on=summary["auth"])
        if found:
            out.append(found)
        out.append(_local_mode_guard(auth_on=summary["auth"]))
    out.append(_deployment_key(hub))
    return [c for c in out if c is not None]


def _openwebui_extra() -> Check:
    import importlib.util

    from . import webui

    binary = webui._find_binary()
    if binary and importlib.util.find_spec("open_webui") is not None:
        return Check("ui.openwebui_extra", "ok", "The openwebui extra is installed", {"binary": binary})
    return Check("ui.openwebui_extra", "fail",
                 'HUBZOID_UI=openwebui needs the openwebui extra: pip install "hubzoid[openwebui]" '
                 "(or remove HUBZOID_UI to use the Hubzoid web app)")


def _openwebui_data(hub: Path, *, auth_on: bool) -> Check | None:
    """An Open WebUI install (1.0.x) whose people are not in Hubzoid yet."""
    from . import upgrade

    found = upgrade.openwebui_accounts(hub)
    if found is None:
        return None
    where, people = found
    detail = {"where": where, "openwebui_accounts": people}
    try:
        if upgrade.hubzoid_accounts(hub):
            return None
    except upgrade.AccountsUnreadable as exc:
        # Unknown, not zero: say so rather than send a moved install back to
        # the migration. `hubzoid run` stops in this case with sign-in on.
        detail["hubzoid_accounts"] = None
        unread = f"Open WebUI has {people} account(s) ({where}) and the Hubzoid accounts could not be read ({exc})"
        if auth_on:
            return Check("ui.openwebui_data", "fail",
                         f"{unread}: hubzoid run stops until the operational database can be read", detail)
        return Check("ui.openwebui_data", "warn",
                     f"{unread}: whether its chats were imported is unknown", detail)
    if auth_on:
        return Check("ui.openwebui_data", "fail",
                     f"Open WebUI has {people} account(s) ({where}) and Hubzoid has none: hubzoid run "
                     "stops until you run `hubzoid migrate openwebui` or set HUBZOID_UI=openwebui", detail)
    return Check("ui.openwebui_data", "info",
                 f"Chats from Open WebUI ({where}) can be imported with `hubzoid migrate openwebui`", detail)


def _local_mode_guard(*, auth_on: bool) -> Check | None:
    """`hubzoid run` keeps local mode (sign-in off) on loopback."""
    from . import appmode, settings as settingslib

    if auth_on:
        return None
    host = os.environ.get("HUBZOID_HOST", "127.0.0.1").strip()
    detail = {"host": host}
    if appmode.is_loopback_host(host):
        return Check("exposure.local_mode", "ok", f"Local mode: the web app stays on {host}", detail)
    if settingslib.truthy(os.environ.get("HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK")):
        return Check("exposure.local_mode", "warn",
                     f"Sign-in is off and the web app listens on {host}: anyone who can reach it acts "
                     "as the hub's owner (HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK=true)", detail)
    return Check("exposure.local_mode", "fail",
                 f"Sign-in is off and HUBZOID_HOST={host}: hubzoid run refuses to start. Turn sign-in on "
                 "(HUBZOID_AUTH=true) or bind 127.0.0.1", detail)


def _deployment_key(hub: Path) -> Check:
    """The key that encrypts stored credentials and signs assertions. Reported
    by fingerprint only, and never created here."""
    from . import secretbox

    if (os.environ.get("HUBZOID_SECRET_KEY") or "").strip():
        try:
            count, fingerprint = len(secretbox.keys(hub)), secretbox.fingerprint(hub)
        except secretbox.SecretKeyError as exc:
            return Check("deployment.key", "fail", str(exc), {"source": "HUBZOID_SECRET_KEY"})
        return Check("deployment.key", "ok", f"Deployment key from HUBZOID_SECRET_KEY, fingerprint {fingerprint}",
                     {"source": "HUBZOID_SECRET_KEY", "fingerprint": fingerprint, "keys": count})
    path = secretbox.key_path(hub)
    if not path.exists():
        return Check("deployment.key", "info",
                     f"No deployment key yet; one is created in {path} when first needed. "
                     "Back it up apart from the data.",
                     {"source": "file", "path": str(path)})
    try:
        count, fingerprint = len(secretbox.keys(hub)), secretbox.fingerprint(hub)
    except (secretbox.SecretKeyError, OSError) as exc:
        return Check("deployment.key", "fail", f"The deployment key in {path} cannot be read: {exc}",
                     {"source": "file", "path": str(path)})
    detail = {"source": "file", "path": str(path), "fingerprint": fingerprint, "keys": count}
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        return Check("deployment.key", "warn",
                     f"Deployment key {path} (fingerprint {fingerprint}) is readable by others: chmod 600 it",
                     {**detail, "mode": oct(mode)})
    return Check("deployment.key", "ok", f"Deployment key in {path}, fingerprint {fingerprint}", detail)


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
    engine = create_engine(db.sqlalchemy_url(url))
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


def run(hub: Path, *, fetch_secrets: bool = True) -> list[Check]:
    """All checks. `fetch_secrets=False` never calls AWS: named secrets are
    listed, not read, and the hub loads from its files alone."""
    from . import config_secrets
    from . import settings as settingslib

    hub = Path(hub).resolve()
    config, deployment_values, unreadable = _config_checks(hub, fetch_secrets=fetch_secrets)
    # Past an unreadable secret, check the rest from the files alone.
    files_only = config_secrets.fetching_disabled() if (unreadable or not fetch_secrets) else nullcontext()
    with files_only:
        checks = _hub_checks(hub)  # loads the hub's .env through runtime.build
        checks[2:2] = config
        settingslib.load(hub)
        checks.append(_versions())
        sqlite_check = _sqlite(hub)
        if sqlite_check:
            checks.append(sqlite_check)
        checks += _schema(hub)
        checks += _auth(hub)
        checks += _app_checks(hub)
        google = _google_merge(deployment_values)
        if google:
            checks.append(google)
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
