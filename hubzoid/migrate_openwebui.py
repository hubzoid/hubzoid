"""``hubzoid migrate openwebui``: move an Open WebUI install to the Hubzoid web app.

The Open WebUI database is only ever read (SQLite ``mode=ro``, PostgreSQL in a
read-only transaction). What moves into the operational store:

* **People.** Every account keeps its id, email, name and role (``pending``
  becomes status ``pending``). Password hashes (bcrypt or argon2) move as they
  are, so nobody has to reset a password; the web app verifies them and
  upgrades bcrypt at the next sign-in. Google, Microsoft and OpenID Connect
  links move to ``hz_user_identities``; ``hz_identities.owui_id`` keeps the id.
* **Groups.** Every group with members, same id and name, members by email.
* **Access.** A hub whose access Open WebUI still decided becomes
  Console-managed. Its visibility groups and restricted-tool groups become
  ``group:<id>`` grants wherever that keeps exactly who may use what: the
  matrix ``access.migrate.plan_from_owui`` computes, denied people included.
  A capability whose group also holds people who cannot open the hub keeps
  per-person grants (a grant carries hub entry, so a group grant would let
  them in). When this build's access store cannot resolve group grants at
  all, the hub gets the per-person migration. Every plan is checked against
  the matrix before anything is written. Console-managed hubs keep their
  grants; their groups are imported for reference.
* **Chats.** Every conversation keeps its id, owner, branches, current branch,
  title, archive state and attachments. Assistant markup written by 1.0.x
  (tool lines and ``<details>`` tool blocks, tool errors, ``<think>``
  reasoning, Open WebUI's own reasoning items) becomes message parts
  (contract 6.4). Share links keep their ids, so ``/s/<id>`` keeps working.

A dry run (the default) changes nothing, not even the target's schema.
``--apply`` writes in two steps: access first (one transaction, with a backup
per hub for ``hubzoid access rollback``), then everything else (one
transaction). If the second step fails, the first is undone. Re-running is
safe: rows are matched by id, changes made in Hubzoid since the previous
import are kept, and what was deleted in Hubzoid is not brought back.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import time
from collections import defaultdict
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator

import typer
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine, URL, make_url
from sqlalchemy.pool import NullPool

log = logging.getLogger("hubzoid.migrate_openwebui")

migrate_app = typer.Typer(
    help="Move an existing install to the Hubzoid web app (accounts, groups, chats, shares).",
    no_args_is_help=True,
)

MARKER_PREFIX = "openwebui_migration:"
ACTOR = "openwebui-migration"
MIGRATED = "migrated"
GOOGLE_ISSUER = "https://accounts.google.com"
LOCAL_OWNER_EMAIL = "admin@localhost"
THINKING_PLACEHOLDER = "_Thinking…_"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_BCRYPT_RE = re.compile(r"^\$2[abxy]\$\d{2}\$[A-Za-z0-9+/.]{53}$")
_ARGON2_RE = re.compile(
    r"^\$argon2(?:id|i|d)\$v=\d+\$m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/]+={0,2}\$[A-Za-z0-9+/]+={0,2}$")
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_TRUE = {"1", "true", "yes", "on"}
_PROVIDERS = {"google", "microsoft", "oidc"}
_FILE_KINDS = {"file", "image"}


class MigrationBlocked(Exception):
    """A condition that makes the migration unsafe or impossible. The message
    is written for the person running the command."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _email(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _ts(value: Any, default: float | None = None) -> float | None:
    """Epoch seconds from an Open WebUI time (seconds, milliseconds or nanoseconds)."""
    if value is None or isinstance(value, bool):
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    if seconds != seconds or seconds <= 0:
        return default
    while seconds > 1e11:
        seconds /= 1000.0
    return seconds


def _json(value: Any) -> Any:
    """A JSON object or array column as a Python value. Accepts text, bytes, an
    already decoded value or double-encoded text; anything else reads as None."""
    for _ in range(3):
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", "replace")
        if not isinstance(value, str):
            return value
        raw = value.strip()
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
    return None if isinstance(value, str) else value


def _json_scalar(value: Any) -> Any:
    """One level of JSON decoding for a column that may hold a JSON string."""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE


def _safe_name(raw: Any) -> str:
    """A file name safe as one path component (mirrors ``server._safe_path_component``)."""
    if not isinstance(raw, str) or not raw:
        return ""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip().replace("\x00", "")
    return "" if name in ("", ".", "..") else name


def _scrub(message: str) -> str:
    """An error text without credentials in any URL it quotes."""
    return re.sub(r"(\w+://)[^/@\s]*@", r"\1***@", message)


def _redact(url: Any) -> str:
    try:
        return (url if isinstance(url, URL) else make_url(str(url))).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - a description only
        return "(unreadable URL)"


def _chunks(items: list, size: int = 400) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _alt_id(conversation_id: str, message_id: str) -> str:
    """Deterministic replacement for a message id another conversation already
    uses (Open WebUI clones copy message ids) or that does not fit the id rules."""
    digest = hashlib.sha256(f"{conversation_id}:{message_id}".encode()).hexdigest()
    return "m" + digest[:31]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
class Report:
    """Counts and decisions. Holds no message content and no email address;
    ids are listed only with --verbose."""

    SECTIONS = ("users", "identities", "groups", "conversations", "messages", "parts",
                "files", "shares", "content")

    def __init__(self, mode: str):
        self.mode = mode
        self.info: dict[str, Any] = {}
        self.counts: dict[str, dict[str, int]] = {s: {} for s in self.SECTIONS}
        self.skipped: dict[str, dict[str, int]] = {}
        self.ids: dict[str, list[dict]] = {}
        self.access: dict[str, dict] = {}
        self.unknown_models: dict[str, int] = {}
        self.blocking: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.backups: list[str] = []
        self.applied = False

    def bump(self, section: str, key: str, n: int = 1) -> None:
        if n:
            bucket = self.counts.setdefault(section, {})
            bucket[key] = bucket.get(key, 0) + n

    def skip(self, section: str, reason: str, ident: str | None = None) -> None:
        bucket = self.skipped.setdefault(section, {})
        bucket[reason] = bucket.get(reason, 0) + 1
        if ident:
            self.ids.setdefault(section, []).append({"id": ident, "reason": reason})

    def listed(self, section: str, reason: str, ident: str) -> None:
        """Record an id for --verbose without counting it as skipped."""
        self.ids.setdefault(section, []).append({"id": ident, "reason": reason})

    def block(self, message: str) -> None:
        if message not in self.blocking:
            self.blocking.append(message)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    def to_dict(self, verbose: bool = False) -> dict:
        out = {
            "mode": self.mode,
            "applied": self.applied,
            **self.info,
            "counts": {k: dict(sorted(v.items())) for k, v in self.counts.items() if v},
            "skipped": {k: dict(sorted(v.items())) for k, v in self.skipped.items() if v},
            "access": self.access,
            "unknown_models": dict(sorted(self.unknown_models.items())),
            "blocking": list(self.blocking),
            "warnings": list(self.warnings),
            "notes": list(self.notes),
            "backups": list(self.backups),
        }
        if verbose:
            out["ids"] = self.ids
        return out


# ---------------------------------------------------------------------------
# Locating the source and the target
# ---------------------------------------------------------------------------
@dataclass
class HubInfo:
    key: str
    name: str
    path: Path
    model_id: str


@dataclass
class Setup:
    entry: Path
    kind: str  # 'hub' | 'gateway'
    hubs: list[HubInfo]
    manifest: dict
    source_url: URL
    source_schema: str | None
    uploads_dir: Path | None
    operational_url: str
    public: dict[str, bool] = field(default_factory=dict)
    public_basis: str = ""
    aliases: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


def _normalize_source_url(value: str) -> URL:
    try:
        url = make_url(value)
    except Exception as exc:  # noqa: BLE001 - reported without the value (it may hold a password)
        raise MigrationBlocked("--owui-db is not a database URL.") from exc
    if url.drivername in {"postgres", "postgresql", "postgresql+asyncpg"}:
        url = url.set(drivername="postgresql+psycopg")
    if url.get_backend_name() not in {"sqlite", "postgresql"}:
        raise MigrationBlocked("The Open WebUI database must be SQLite or PostgreSQL.")
    return url


def _operational_url(hub_dir: Path, env) -> str:
    """``db.operational_url`` without its side effect (it creates ``.hubzoid``)."""
    from . import deployment

    configured = deployment.read(Path(hub_dir), env).get("operational_url")
    explicit = (env.get("HUBZOID_OPERATIONAL_DB") or "").strip()
    if configured:
        if explicit and explicit != configured:
            raise MigrationBlocked("HUBZOID_OPERATIONAL_DB differs from the registered deployment.")
        return configured
    return explicit or (env.get("DATABASE_URL") or "").strip() or \
        f"sqlite:///{Path(hub_dir).resolve() / '.hubzoid' / 'hub.db'}"


def _hub_env(hub: Path) -> dict[str, str]:
    from dotenv import dotenv_values

    path = Path(hub) / ".env"
    try:
        return {k: v for k, v in dotenv_values(path).items() if v is not None} if path.is_file() else {}
    except Exception:  # noqa: BLE001 - an unreadable .env contributes nothing
        return {}


def _setting(setup: Setup, key: str, env) -> str | None:
    """A setting as the deployment saw it: the environment, then each hub's .env."""
    if env.get(key) not in (None, ""):
        return env.get(key)
    for hub in setup.hubs:
        value = _hub_env(hub.path).get(key)
        if value not in (None, ""):
            return value
    return None


def locate(path: Path, *, owui_db: str | None = None, operational_db: str | None = None,
           aliases: dict[str, str] | None = None, env=None) -> Setup:
    """Find the hubs, the Open WebUI database and the operational store for
    PATH: a hub folder, a hub registered in a gateway, or a gateway data folder."""
    from . import deployment
    from .access import owui_db as owui_db_mod

    env = dict(os.environ if env is None else env)
    path = Path(path).expanduser().resolve()
    if not path.is_dir():
        raise MigrationBlocked(f"{path} is not a folder.")
    entry = path
    gateway_manifest = path / "deployment.json"
    if gateway_manifest.is_file() and not (path / ".hubzoid" / "deployment.json").is_file():
        try:
            data = json.loads(gateway_manifest.read_text())
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict) and data.get("version") == 1 and data.get("hubs"):
            entry = Path(data["hubs"][0]["path"]).resolve()
    try:
        manifest = deployment.read(entry, env)
        registered = deployment.hubs(entry)
    except (ValueError, OSError, KeyError) as exc:
        raise MigrationBlocked(f"The deployment could not be read: {exc}") from exc
    hubs = [HubInfo(key=str(h["key"]).strip().lower(), name=str(h.get("name") or h["key"]),
                    path=Path(h["path"]).resolve(), model_id=str(h.get("model_id") or ""))
            for h in registered]
    if not hubs:
        raise MigrationBlocked("No hub was found.")
    for alias, goal in (aliases or {}).items():
        if not any(goal in (h.model_id, h.key) or goal.lower() == h.key for h in hubs):
            raise MigrationBlocked(f"--model-alias {alias}={goal}: no agent in this deployment has "
                                   "that model id or hub key.")
    if owui_db:
        source_url = _normalize_source_url(owui_db)
        schema = (env.get("DATABASE_SCHEMA") or "").strip() or None
    else:
        try:
            source_url, schema = owui_db_mod.database_config(entry)
        except ValueError as exc:
            raise MigrationBlocked(str(exc)) from exc
    if source_url.get_backend_name() == "sqlite":
        db_file = Path(source_url.database or "").expanduser().resolve()
        if not db_file.is_file():
            raise MigrationBlocked(f"No Open WebUI database at {db_file}. Nothing to migrate.")
        source_url = URL.create("sqlite", database=str(db_file))
        uploads = db_file.parent / "uploads"
    else:
        uploads = owui_db_mod.db_path(entry).parent / "uploads"
    setup = Setup(
        entry=entry, kind="gateway" if manifest else "hub", hubs=hubs, manifest=manifest,
        source_url=source_url, source_schema=schema,
        uploads_dir=uploads if uploads.is_dir() else None,
        operational_url=operational_db or _operational_url(entry, env),
        aliases=dict(aliases or {}), env=env,
    )
    _legacy_entry(setup, env)
    return setup


def _legacy_entry(setup: Setup, env) -> None:
    """Whether Open WebUI showed each hub's agent to everyone signed in.

    A single hub launched Open WebUI with BYPASS_MODEL_ACCESS_CONTROL on unless
    its settings said otherwise (``webui.start``). A gateway forces it off
    unless HUBZOID_GATEWAY_ALLOW_BYPASS is set, so its model access decided."""
    bypass = _setting(setup, "BYPASS_MODEL_ACCESS_CONTROL", env)
    if setup.kind == "hub":
        auth = _setting(setup, "WEBUI_AUTH", env)
        public = (auth is not None and not _truthy(auth)) or bypass is None or _truthy(bypass)
        setup.public = {h.key: public for h in setup.hubs}
        setup.public_basis = (
            "single hub: Open WebUI showed the agent to everyone signed in (BYPASS_MODEL_ACCESS_CONTROL)"
            if public else "single hub: Open WebUI model access decided (BYPASS_MODEL_ACCESS_CONTROL off)")
        return
    open_gateway = _truthy(env.get("HUBZOID_GATEWAY_ALLOW_BYPASS"))
    public = open_gateway and (bypass is None or _truthy(bypass))
    setup.public = {h.key: public for h in setup.hubs}
    setup.public_basis = (
        "gateway with HUBZOID_GATEWAY_ALLOW_BYPASS: every agent was open to everyone signed in"
        if public else "gateway: each agent's Open WebUI model access decided who could use it")


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------
def _source_engine(url: URL, schema: str | None) -> Engine:
    """A strictly read-only engine on the Open WebUI database."""
    if url.get_backend_name() == "sqlite":
        path = Path(url.database or "").resolve()
        ro = URL.create("sqlite", database=path.as_uri(), query={"mode": "ro", "uri": "true"})
        engine = create_engine(ro, poolclass=NullPool)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT count(*) FROM sqlite_master"))
        except Exception as exc:  # noqa: BLE001 - fall back only when that is safe
            engine.dispose()
            wal = path.with_name(path.name + "-wal")
            if wal.exists() and wal.stat().st_size > 0:
                raise MigrationBlocked(
                    "The Open WebUI database could not be opened read-only and has changes in "
                    "its write-ahead log. Stop Open WebUI and try again, or run against a copy "
                    "(--rehearse).") from exc
            immutable = URL.create("sqlite", database=path.as_uri(),
                                   query={"mode": "ro", "immutable": "1", "uri": "true"})
            engine = create_engine(immutable, poolclass=NullPool)
        return engine
    engine = create_engine(url, poolclass=NullPool, hide_parameters=True,
                           isolation_level="REPEATABLE READ",
                           execution_options={"postgresql_readonly": True},
                           connect_args={"connect_timeout": 10})
    if schema:
        quoted = '"' + schema.replace('"', '""') + '"'

        @event.listens_for(engine, "connect")
        def _search_path(dbapi_conn, _record):  # pragma: no cover - PostgreSQL only
            cursor = dbapi_conn.cursor()
            cursor.execute(f"SET search_path TO {quoted}")
            cursor.close()
            dbapi_conn.commit()
    return engine


def _target_engine(url: str, *, readonly: bool) -> Engine | None:
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        database = parsed.database or ""
        if database in ("", ":memory:"):
            raise MigrationBlocked("The operational store must be a database file or server.")
        path = Path(database).expanduser().resolve()
        if readonly:
            if not path.is_file():
                return None
            return create_engine(URL.create("sqlite", database=path.as_uri(),
                                            query={"mode": "ro", "uri": "true"}), poolclass=NullPool)
        path.parent.mkdir(parents=True, exist_ok=True)
        return create_engine(URL.create("sqlite", database=str(path)),
                             connect_args={"timeout": 30}, poolclass=NullPool)
    if parsed.drivername in {"postgres", "postgresql"}:
        parsed = parsed.set(drivername="postgresql+psycopg")
    options = {"postgresql_readonly": True} if readonly else {}
    return create_engine(parsed, poolclass=NullPool, hide_parameters=True, execution_options=options)


# ---------------------------------------------------------------------------
# Reading Open WebUI
# ---------------------------------------------------------------------------
class OwuiSource:
    """Read access to the Open WebUI tables this migration needs. Older and
    newer 0.x layouts are handled where they differ (``oauth_sub`` vs
    ``oauth``, ``group.user_ids`` vs ``group_member``, ``shared-<chat id>``
    copy rows vs ``shared_chat``, ``content`` vs ``output`` items)."""

    _TABLES = ("user", "auth", "group", "group_member", "chat", "chat_message", "shared_chat",
               "model", "access_grant", "file", "alembic_version")

    def __init__(self, engine: Engine):
        self.engine = engine
        insp = inspect(engine)
        self.tables = set(insp.get_table_names())
        if not {"user", "chat"} <= self.tables:
            raise MigrationBlocked("This does not look like an Open WebUI database (no user or chat table).")
        self.columns = {t: {c["name"] for c in insp.get_columns(t)}
                        for t in self._TABLES if t in self.tables}
        self._files: dict[str, dict | None] = {}

    def has(self, table: str, column: str | None = None) -> bool:
        return table in self.tables and (column is None or column in self.columns.get(table, set()))

    def _select(self, table: str, wanted: tuple[str, ...]) -> str:
        cols = [c for c in wanted if c in self.columns.get(table, set())]
        return "SELECT " + ", ".join(f'"{c}"' for c in cols) + f' FROM "{table}"'

    def revision(self) -> str | None:
        if "alembic_version" not in self.tables:
            return None
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()

    def users(self) -> dict[str, dict]:
        sql = self._select("user", ("id", "email", "name", "role", "oauth", "oauth_sub",
                                    "created_at", "updated_at", "last_active_at"))
        with self.engine.connect() as conn:
            return {r["id"]: dict(r) for r in conn.execute(text(sql)).mappings() if r["id"]}

    def auth(self) -> dict[str, dict]:
        if "auth" not in self.tables:
            return {}
        with self.engine.connect() as conn:
            return {r["id"]: dict(r) for r in conn.execute(
                text(self._select("auth", ("id", "password", "active")))).mappings()}

    def groups(self) -> dict[str, dict]:
        if "group" not in self.tables:
            return {}
        sql = self._select("group", ("id", "name", "description", "user_ids", "created_at", "updated_at"))
        with self.engine.connect() as conn:
            return {r["id"]: dict(r) for r in conn.execute(text(sql)).mappings() if r["id"]}

    def memberships(self, groups: dict[str, dict]) -> dict[str, dict[str, float | None]]:
        """group id -> {user id: when they joined, or None when unknown}."""
        out: dict[str, dict[str, float | None]] = {gid: {} for gid in groups}
        if "group_member" in self.tables:
            when = "created_at" if self.has("group_member", "created_at") else "NULL"
            with self.engine.connect() as conn:
                for gid, uid, created in conn.execute(
                        text(f"SELECT group_id, user_id, {when} FROM group_member")):
                    if gid in out and uid:
                        out[gid][uid] = _ts(created)
        else:
            for gid, group in groups.items():
                ids = _json(group.get("user_ids")) or []
                if not isinstance(ids, list):
                    raise MigrationBlocked("A group's member list could not be read.")
                out[gid] = {uid: None for uid in ids if isinstance(uid, str)}
        return out

    def model_bases(self) -> dict[str, str]:
        """Workspace model id -> the model it is built on (``base_model_id``)."""
        if not self.has("model", "base_model_id"):
            return {}
        with self.engine.connect() as conn:
            return {mid: base for mid, base in conn.execute(
                text("SELECT id, base_model_id FROM model WHERE base_model_id IS NOT NULL")) if mid and base}

    def share_meta(self) -> dict[str, dict]:
        """Every share token -> where its snapshot lives (without the snapshot)."""
        out: dict[str, dict] = {}
        with self.engine.connect() as conn:
            if "shared_chat" in self.tables:
                for r in conn.execute(text(
                        "SELECT id, chat_id, title, created_at, updated_at FROM shared_chat")).mappings():
                    out[r["id"]] = dict(chat_id=r["chat_id"], title=r["title"], legacy=False,
                                        created_at=_ts(r["created_at"]), updated_at=_ts(r["updated_at"]))
            for r in conn.execute(text(
                    "SELECT id, user_id, title, created_at, updated_at FROM chat "
                    "WHERE user_id LIKE 'shared-%'")).mappings():
                out.setdefault(r["id"], dict(chat_id=r["user_id"][len("shared-"):], title=r["title"],
                                             legacy=True, created_at=_ts(r["created_at"]),
                                             updated_at=_ts(r["updated_at"])))
        return out

    def share_snapshot(self, conn: Connection, share_id: str, legacy: bool) -> dict | None:
        table = "chat" if legacy else "shared_chat"
        value = _json(conn.execute(text(f"SELECT chat FROM {table} WHERE id=:id"), {"id": share_id}).scalar())
        return value if isinstance(value, dict) else None

    def share_audiences(self) -> dict[str, str]:
        """chat id -> who could open its share link in Open WebUI: 'anyone'
        (no sign-in), 'signed_in', 'restricted' (named people or groups) or
        'owner'. Before Open WebUI had share access, everyone signed in could."""
        if "access_grant" not in self.tables:
            return defaultdict(lambda: "signed_in")
        found: dict[str, set[str]] = defaultdict(set)
        with self.engine.connect() as conn:
            for rid, ptype, pid, perm in conn.execute(text(
                    "SELECT resource_id, principal_type, principal_id, permission FROM access_grant "
                    "WHERE resource_type='shared_chat'")):
                if perm not in ("read", "write"):
                    continue
                if ptype == "anyone":
                    found[rid].add("anyone")
                elif ptype == "user" and pid == "*":
                    found[rid].add("signed_in")
                else:
                    found[rid].add("restricted")
        out: dict[str, str] = defaultdict(lambda: "owner")
        for rid, kinds in found.items():
            out[rid] = next(k for k in ("anyone", "signed_in", "restricted") if k in kinds)
        return out

    def iter_chats(self, conn: Connection) -> Iterator[dict]:
        sql = self._select("chat", ("id", "user_id", "title", "chat", "created_at", "updated_at",
                                    "share_id", "archived", "pinned", "meta", "folder_id",
                                    "current_message_id")) + " ORDER BY created_at, id"
        result = conn.execution_options(stream_results=True, max_row_buffer=50).execute(text(sql))
        for row in result.mappings():
            yield dict(row)

    def chat_message_rows(self, conn: Connection, chat_id: str) -> dict[str, dict]:
        """Open WebUI 0.9+ also keeps each message in ``chat_message`` (keyed
        ``<chat id>-<message id>``). Used only when a chat's history is empty."""
        if "chat_message" not in self.tables:
            return {}
        sql = self._select("chat_message", ("id", "role", "parent_id", "content", "output", "model_id",
                                            "files", "done", "error", "usage", "created_at"))
        prefix = f"{chat_id}-"
        out: dict[str, dict] = {}
        for r in conn.execute(text(sql + " WHERE chat_id=:c"), {"c": chat_id}).mappings():
            mid = r["id"][len(prefix):] if r["id"].startswith(prefix) else r["id"]
            out[mid] = {"id": mid, "role": r.get("role"), "parentId": r.get("parent_id"),
                        "content": _json_scalar(r.get("content")), "output": _json(r.get("output")),
                        "model": r.get("model_id"), "files": _json(r.get("files")),
                        "done": r.get("done"), "error": _json_scalar(r.get("error")),
                        "usage": _json(r.get("usage")), "timestamp": r.get("created_at")}
        return out

    def file_row(self, conn: Connection, file_id: str) -> dict | None:
        if file_id in self._files:
            return self._files[file_id]
        row = None
        if "file" in self.tables and file_id:
            found = conn.execute(text(self._select("file", ("id", "filename", "path")) + " WHERE id=:id"),
                                 {"id": file_id}).mappings().first()
            row = dict(found) if found else None
        self._files[file_id] = row
        return row


def _fingerprint(users: dict[str, dict], url: URL) -> str:
    """Identifies this Open WebUI install for the re-run marker: its first
    account (stable for the life of an install), or its location without one."""
    if users:
        first = min(users.values(), key=lambda u: (_ts(u.get("created_at")) or 0, u["id"]))
        basis = f"{first['id']}:{int(_ts(first.get('created_at')) or 0)}"
    else:
        basis = _redact(url)
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Reading the target (the operational store)
# ---------------------------------------------------------------------------
class TargetState:
    """What the operational store already holds. Works on a read-only
    connection and on a store whose schema is older than this release (missing
    tables read as empty), so a dry run never writes."""

    def __init__(self) -> None:
        self.tables: set[str] = set()
        self.revision: str | None = None
        self.meta: dict[str, str] = {}
        self.users: dict[str, dict] = {}
        self.users_by_email: dict[str, dict] = {}
        self.user_identities: dict[tuple[str, str], str] = {}
        self.identities: dict[str, dict] = {}
        self.groups: dict[str, dict] = {}
        self.group_names: dict[str, str] = {}
        self.members: dict[tuple[str, str], dict] = {}
        self.conversations: dict[str, dict] = {}
        self.shares: dict[str, dict] = {}

    @classmethod
    def load(cls, engine: Engine | None) -> "TargetState":
        state = cls()
        if engine is None:
            return state
        state.tables = set(inspect(engine).get_table_names())
        with engine.connect() as conn:
            def rows(table: str, sql: str):
                return conn.execute(text(sql)).mappings() if table in state.tables else []

            if "hz_alembic_operational" in state.tables:
                state.revision = conn.execute(text("SELECT version_num FROM hz_alembic_operational")).scalar()
            for r in rows("hz_meta", "SELECT k, v FROM hz_meta WHERE k LIKE 'casbin_authoritative%' "
                                     "OR k LIKE 'suspended:%' OR k LIKE 'account_unavailable:%' "
                                     "OR k LIKE 'openwebui_migration:%'"):
                state.meta[r["k"]] = r["v"]
            for r in rows("hz_users", "SELECT id, email, name, role, status, password_hash, "
                                      "password_enabled, source, created_at, updated_at, last_login_at "
                                      "FROM hz_users"):
                row = dict(r)
                state.users[row["id"]] = row
                state.users_by_email[_email(row["email"])] = row
            for r in rows("hz_user_identities", "SELECT issuer, subject, user_id FROM hz_user_identities"):
                state.user_identities[(r["issuer"], r["subject"])] = r["user_id"]
            for r in rows("hz_identities", "SELECT subject, owui_id, pending, display FROM hz_identities"):
                state.identities[r["subject"]] = dict(r)
            for r in rows("hz_groups", "SELECT id, name, source FROM hz_groups"):
                state.groups[r["id"]] = dict(r)
                state.group_names[r["name"]] = r["id"]
            for r in rows("hz_group_members", "SELECT group_id, email, added_by, added_at FROM hz_group_members"):
                state.members[(r["group_id"], _email(r["email"]))] = dict(r)
            for r in rows("hz_conversations", "SELECT id, owner_id, source, title, archived, head_id, "
                                              "updated_at FROM hz_conversations"):
                state.conversations[r["id"]] = dict(r)
            for r in rows("hz_shares", "SELECT id, conversation_id, created_at FROM hz_shares"):
                state.shares[r["id"]] = dict(r)
        return state

    def authoritative(self, hub: str) -> bool:
        marker = self.meta.get(f"casbin_authoritative:{hub}")
        if marker is not None:
            return marker == "1"
        return self.meta.get("casbin_authoritative") == "1"

    def previous(self, fingerprint: str) -> dict | None:
        raw = self.meta.get(MARKER_PREFIX + fingerprint)
        try:
            value = json.loads(raw) if raw else None
        except ValueError:
            return None
        return value if isinstance(value, dict) and value.get("time") else None


def _existed_before(prev: dict | None, created: float | None, section: str, ident: str) -> bool:
    """True when the previous import saw this item (it existed then and was not
    skipped), so its absence now means it was deleted in Hubzoid."""
    if not prev or created is None:
        return False
    if ident in set((prev.get("skipped") or {}).get(section) or ()):
        return False
    return created <= float(prev["time"])


def _changed_since(prev: dict | None, updated: Any) -> bool:
    """True when a row changed in Hubzoid after the previous import, or when
    there is no previous import to compare with (it is then kept as it is)."""
    if not prev:
        return True
    value = _ts(updated)
    return value is None or value > float(prev["time"])


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------
@dataclass
class People:
    rows: dict[str, dict] = field(default_factory=dict)          # hz_users values by id
    action: dict[str, str] = field(default_factory=dict)         # insert | update | same | keep
    email_of: dict[str, str] = field(default_factory=dict)       # account id -> email in Hubzoid
    identity_rows: list[dict] = field(default_factory=list)      # hz_user_identities inserts
    inactive_new: set[str] = field(default_factory=set)          # emails suspended on insert
    inactive: set[str] = field(default_factory=set)              # deactivated in Open WebUI
    raw_pending: set[str] = field(default_factory=set)           # pending in Open WebUI
    identities: list[dict] = field(default_factory=list)         # for GrantStore.apply_migration
    display: dict[str, str] = field(default_factory=dict)
    skipped_ids: set[str] = field(default_factory=set)           # not imported (retried next run)
    not_restored: set[str] = field(default_factory=set)          # deleted in Hubzoid (ids)
    removed_emails: set[str] = field(default_factory=set)        # their emails: never re-granted


def _issuer(provider: str, setup: Setup) -> tuple[str, bool]:
    """(issuer, known). An unknown issuer gets a placeholder the web app can
    upgrade at the person's next sign-in, matching provider and subject."""
    if provider == "google":
        return GOOGLE_ISSUER, True
    if provider == "microsoft":
        tenant = (_setting(setup, "MICROSOFT_CLIENT_TENANT_ID", setup.env) or "").strip()
        if _GUID_RE.match(tenant):
            return f"https://login.microsoftonline.com/{tenant.lower()}/v2.0", True
    if provider == "oidc":
        url = (_setting(setup, "OPENID_PROVIDER_URL", setup.env) or "").strip()
        suffix = "/.well-known/openid-configuration"
        if url.startswith("https://") and url.endswith(suffix):
            return url[: -len(suffix)], True
    return f"openwebui-migrated:{provider}", False


def _oauth_links(user: dict) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    oauth = _json(user.get("oauth"))
    if isinstance(oauth, dict):
        for provider, data in oauth.items():
            sub = data.get("sub") if isinstance(data, dict) else None
            if isinstance(provider, str) and sub not in (None, ""):
                links.append((provider.strip().lower(), str(sub)))
    legacy = user.get("oauth_sub")
    if isinstance(legacy, str) and legacy.strip():
        provider, sub = legacy.split("@", 1) if "@" in legacy else ("oidc", legacy)
        if (provider.strip().lower(), sub) not in links:
            links.append((provider.strip().lower(), sub))
    return links


def plan_people(setup: Setup, users: dict[str, dict], auth: dict[str, dict], target: TargetState,
                prev: dict | None, report: Report) -> People:
    people = People()
    seen: set[str] = set()
    service = _email(setup.env.get("HUBZOID_GATEWAY_ADMIN_EMAIL"))
    for user in sorted(users.values(), key=lambda u: (_ts(u.get("created_at")) or 0, u["id"])):
        uid, email = user["id"], _email(user.get("email"))
        report.bump("users", "in_open_webui")
        if len(uid) > 64:
            report.skip("users", "id longer than 64 characters", uid)
            people.skipped_ids.add(uid)
            continue
        if "@" not in email or len(email) > 320:
            report.skip("users", "no usable email address", uid)
            people.skipped_ids.add(uid)
            continue
        if email in seen:
            report.block("Two Open WebUI accounts share one email address (ignoring case). Merge or "
                         "remove one in Open WebUI, then run the migration again.")
            report.skip("users", "email shared with another account", uid)
            people.skipped_ids.add(uid)
            continue
        seen.add(email)
        raw_role = (user.get("role") or "").strip().lower()
        role = "admin" if raw_role == "admin" else "user"
        status = "pending" if raw_role == "pending" else "active"
        if raw_role == "pending":
            people.raw_pending.add(email)
        elif raw_role not in ("admin", "user"):
            report.bump("users", "unknown_role_imported_as_user")
        credentials = auth.get(uid) or {}
        if credentials and credentials.get("active") in (False, 0):
            people.inactive.add(email)
        hashed = credentials.get("password") if isinstance(credentials.get("password"), str) else None
        hashed = hashed.strip() if hashed else None
        if hashed and not (_BCRYPT_RE.match(hashed) or _ARGON2_RE.match(hashed)):
            report.bump("users", "password_format_unrecognized")
            hashed = None
        if hashed and email == LOCAL_OWNER_EMAIL:
            # Open WebUI's sign-in-off account carries the published password "admin".
            report.note("The local owner account of sign-in-off mode was imported without its "
                        "default password.")
            hashed = None
        created = _ts(user.get("created_at"), time.time())
        row = dict(id=uid, email=email, name=(user.get("name") or "").strip() or email.split("@")[0],
                   role=role, status=status, password_hash=hashed, password_enabled=1 if hashed else 0,
                   source=MIGRATED, created_at=created,
                   updated_at=max(_ts(user.get("updated_at"), created) or created, created),
                   last_login_at=_ts(user.get("last_active_at")))
        existing = target.users.get(uid)
        if existing is None:
            if _existed_before(prev, created, "users", uid):
                report.bump("users", "deleted_in_hubzoid_not_restored")
                report.listed("users", "deleted in Hubzoid since the last import", uid)
                people.not_restored.add(uid)
                people.removed_emails.add(email)
                continue
            action = "insert"
        elif _changed_since(prev, existing.get("updated_at")):
            action, row = "keep", dict(existing)
        else:
            fields = ("email", "name", "role", "status", "password_hash", "password_enabled")
            action = "same" if all(existing.get(f) == row.get(f) for f in fields) else "update"
        final_email = _email(row["email"])
        holder = target.users_by_email.get(final_email)
        if holder is not None and holder["id"] != uid:
            report.block("Hubzoid already has an account with the email of an Open WebUI account "
                         "but a different id (created before this migration). Delete that Hubzoid "
                         "account, then run the migration again so the Open WebUI account keeps "
                         "its id, password, chats and shares.")
            report.skip("users", "email already used by another Hubzoid account", uid)
            people.skipped_ids.add(uid)
            continue
        bound = (target.identities.get(final_email) or {}).get("owui_id")
        if bound and bound != uid:
            report.block("The access store links an email to a different Open WebUI account than "
                         "the one in this database (the account was re-created). Refresh accounts "
                         "in the Console and review that person's access, then run the migration again.")
            report.skip("users", "access store bound to another account", uid)
            people.skipped_ids.add(uid)
            continue
        if action == "insert" and email in people.inactive:
            people.inactive_new.add(email)
        people.action[uid] = action
        people.rows[uid] = row
        people.email_of[uid] = final_email
        people.display[final_email] = row.get("name") or ""
        report.bump("users", {"insert": "to_import", "update": "to_update", "same": "unchanged",
                              "keep": "kept_changed_in_hubzoid"}[action])
        if row["role"] == "admin":
            report.bump("users", "admins")
        if row["status"] == "pending":
            report.bump("users", "pending")
        report.bump("users", "with_password" if row.get("password_hash") else "without_password")
        if email in people.inactive:
            report.bump("users", "deactivated_in_open_webui_blocked")
        if service and email == service:
            report.note("The gateway's service account (HUBZOID_GATEWAY_ADMIN_EMAIL) was imported "
                        "as an administrator. Remove it in the Console if nobody signs in with it.")
        links = _oauth_links(user)
        if links:
            report.bump("users", "with_external_identity")
        for provider, subject in links:
            if provider not in _PROVIDERS:
                report.bump("identities", "unsupported_provider_not_imported")
                report.warn("Some accounts were linked to a sign-in provider the web app does not "
                            "offer (only Google, Microsoft and OpenID Connect); those people sign in "
                            "with their password or a set-password link.")
                continue
            issuer, known = _issuer(provider, setup)
            report.bump("identities", provider)
            if not known:
                report.bump("identities", "issuer_placeholder")
                report.warn(f"{provider} sign-in links were imported with a placeholder issuer "
                            f"(openwebui-migrated:{provider}) because the issuer is not configured "
                            "here; the web app can match them by provider and subject.")
            holder_id = target.user_identities.get((issuer, subject))
            if holder_id == uid:
                report.bump("identities", "already_present")
                continue
            if holder_id is not None:
                report.bump("identities", "linked_to_another_account_not_imported")
                report.listed("identities", "linked to another Hubzoid account", uid)
                continue
            if action == "keep":
                continue
            people.identity_rows.append(dict(provider=provider, issuer=issuer, subject=subject,
                                             user_id=uid, email=final_email, created_at=row["created_at"]))
            report.bump("identities", "to_import")
    known = set(users)
    for uid, row in target.users.items():
        if row.get("source") == MIGRATED and uid not in known:
            report.bump("users", "imported_earlier_not_in_open_webui_now")
            report.listed("users", "no longer in Open WebUI", uid)
    if report.counts["users"].get("imported_earlier_not_in_open_webui_now"):
        report.warn("Some people imported earlier no longer exist in Open WebUI. They keep their "
                    "Hubzoid account; remove them in the Console if they should not have access.")
    # hz_identities: owui_id is the account id; pending mirrors the resulting status.
    for uid, row in people.rows.items():
        email = people.email_of[uid]
        pending = row.get("status") == "pending"
        current = target.identities.get(email) or {}
        unavailable = target.meta.get("account_unavailable:" + email)
        if (current.get("owui_id") != uid or bool(current.get("pending")) != pending
                or unavailable != ("1" if pending else "0")):
            people.identities.append(dict(email=email, owui_id=uid, pending=pending))
    return people


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------
@dataclass
class GroupPlan:
    members: dict[str, set[str]] = field(default_factory=dict)       # gid -> emails in Open WebUI
    inserts: list[dict] = field(default_factory=list)
    add_members: list[dict] = field(default_factory=list)
    remove_members: list[tuple[str, str]] = field(default_factory=list)
    available: set[str] = field(default_factory=set)                  # gids that exist after apply
    skipped_ids: set[str] = field(default_factory=set)


def plan_groups(groups: dict[str, dict], memberships: dict[str, dict[str, float | None]],
                people: People, target: TargetState, prev: dict | None, report: Report) -> GroupPlan:
    plan = GroupPlan()
    taken = dict(target.group_names)
    in_hubzoid: dict[str, dict[str, dict]] = defaultdict(dict)
    for (member_gid, email), row in target.members.items():
        in_hubzoid[member_gid][email] = row
    for gid, group in sorted(groups.items(), key=lambda kv: (_ts(kv[1].get("created_at")) or 0, kv[0])):
        report.bump("groups", "in_open_webui")
        joined = memberships.get(gid, {})
        emails = {people.email_of[uid] for uid in joined if uid in people.email_of}
        plan.members[gid] = emails
        report.bump("groups", "memberships_in_open_webui", len(joined))
        existing = target.groups.get(gid)
        if len(gid) > 64:
            report.skip("groups", "id longer than 64 characters", gid)
            plan.skipped_ids.add(gid)
            continue
        if not emails and existing is None:
            report.skip("groups", "no members", gid)
            plan.skipped_ids.add(gid)
            continue
        created = _ts(group.get("created_at"), time.time())
        changed = _ts(group.get("updated_at"), created) or created
        if existing is None:
            if _existed_before(prev, created, "groups", gid):
                report.bump("groups", "deleted_in_hubzoid_not_restored")
                report.listed("groups", "deleted in Hubzoid since the last import", gid)
                continue
            base = ((group.get("name") or "").strip() or "Group")[:240]
            name, n = base, 1
            while name in taken and taken[name] != gid:
                n += 1
                name = f"{base} ({n})"
            if name != base:
                report.bump("groups", "renamed_because_the_name_is_taken")
            taken[name] = gid
            plan.inserts.append(dict(id=gid, name=name, description=group.get("description") or None,
                                     source=MIGRATED, created_by=ACTOR, created_at=created,
                                     updated_at=max(changed, created)))
            report.bump("groups", "to_import")
        else:
            report.bump("groups", "already_present")
        plan.available.add(gid)
        for uid, joined_at in sorted(joined.items()):
            email = people.email_of.get(uid)
            if not email:
                continue
            if email in in_hubzoid[gid]:
                report.bump("groups", "members_already_present")
                continue
            since = joined_at if joined_at is not None else changed
            if existing is not None and prev and since <= float(prev["time"]):
                report.bump("groups", "members_removed_in_hubzoid_not_restored")
                continue
            plan.add_members.append(dict(group_id=gid, email=email, added_by=ACTOR,
                                         added_at=joined_at or created))
            report.bump("groups", "members_to_import")
        if existing is not None and prev:
            # Members this migration added earlier and Open WebUI no longer has.
            for email, row in sorted(in_hubzoid[gid].items()):
                if row.get("added_by") == ACTOR and email not in emails:
                    plan.remove_members.append((gid, email))
                    report.bump("groups", "members_removed_in_open_webui")
    return plan


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------
@dataclass
class HubAccess:
    hub: HubInfo
    managed: bool
    blocked: bool = False
    strategy: str = ""               # groups | mixed | people
    grants: list[tuple[str, str, str]] = field(default_factory=list)
    attrs: list[tuple[str, str, str, str]] = field(default_factory=list)
    expected: list[tuple[str, str, str, bool]] = field(default_factory=list)
    person_grants: list[tuple[str, str, str]] = field(default_factory=list)
    visibility_backup: dict | None = None
    summary: dict = field(default_factory=dict)


def _subject_kind(subject: str) -> str:
    if subject == "*":
        return "everyone"
    return "group" if subject.startswith("group:") else "person"


def store_resolves_groups() -> bool:
    """Whether this build's access store honours ``group:<id>`` grants for
    members (contract 6.7). Probed on a scratch in-memory store."""
    from .access.store import GrantStore

    engine = create_engine("sqlite://")
    try:
        store = GrantStore(engine)
        now = time.time()
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO hz_groups (id, name, source, created_at, updated_at) "
                              "VALUES ('hzprobe01', 'probe', 'console', :t, :t)"), {"t": now})
            conn.execute(text("INSERT INTO hz_group_members (group_id, email, added_at) "
                              "VALUES ('hzprobe01', 'probe@example.invalid', :t)"), {"t": now})
        store.grant("group:hzprobe01", "probe-hub", "use_hub", actor="probe")
        return bool(store.can("probe@example.invalid", "probe-hub", "use_hub"))
    except Exception:  # noqa: BLE001 - any failure means "not supported"
        log.debug("group grant probe failed", exc_info=True)
        return False
    finally:
        engine.dispose()


def verify(hubs: dict[str, HubAccess], grants: dict[str, list], groups: GroupPlan,
           people: People) -> dict[str, list[dict]]:
    """Every expected decision, allowed and denied, against a clean candidate
    store holding what apply would write. Returns the differences per hub."""
    from .access.store import GrantStore

    engine = create_engine("sqlite://")
    try:
        candidate = GrantStore(engine)
        now = time.time()
        with engine.begin() as conn:
            for gid in sorted(groups.available):
                conn.execute(text("INSERT INTO hz_groups (id, name, source, created_at, updated_at) "
                                  "VALUES (:i, :i, 'migrated', :t, :t)"), {"i": gid, "t": now})
                for email in sorted(groups.members.get(gid, ())):
                    conn.execute(text("INSERT INTO hz_group_members (group_id, email, added_at) "
                                      "VALUES (:g, :e, :t)"), {"g": gid, "e": email, "t": now})
            for email in sorted(people.inactive):
                conn.execute(text("INSERT INTO hz_meta (k, v) VALUES (:k, '1')"), {"k": "suspended:" + email})
        identities = [dict(email=email, owui_id=uid, pending=email in people.raw_pending)
                      for uid, email in people.email_of.items()]
        flat = [g for key in hubs for g in grants.get(key, [])]
        candidate.apply_migration(flat, [], list(hubs), replace=True, authoritative=True,
                                  identities=identities, carry_over_public=True, actor="verify")
        out: dict[str, list[dict]] = {}
        for key, access in hubs.items():
            diffs = []
            for subject, hub, permission, allowed in access.expected:
                actual = candidate.can(subject, hub, permission)
                if actual != allowed:
                    diffs.append(dict(subject=subject, permission=permission, expected=allowed, actual=actual))
            out[key] = diffs
        return out
    finally:
        engine.dispose()


def group_grants(access: HubAccess, public: bool, groups: GroupPlan, group_names: dict[str, str]
                 ) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Group grants where they keep the legacy matrix, per-person grants elsewhere.

    Starts from the per-person plan and moves coverage onto groups. A
    visibility group takes over its members' ``use_hub``. A group named after a
    capability takes over its members' grant of it only when every member may
    open the hub, because a capability grant also opens the hub. Returns the
    grants and the capabilities that stay per person."""
    from .access.store import EVERYONE, USE_HUB

    key = access.hub.key
    person = [g for g in access.person_grants if g[1] == key]
    entry = {s for s, _h, _p in person if s != EVERYONE}
    grants: list[tuple[str, str, str]] = []
    covered: dict[str, set[str]] = defaultdict(set)
    per_person: set[str] = set()
    visibility: list[str] = []
    for grant in (access.visibility_backup or {}).get("access_grants") or []:
        gid = grant.get("principal_id")
        if (grant.get("principal_type") == "group" and grant.get("permission") in ("read", "write")
                and gid in groups.available and gid not in visibility):
            visibility.append(gid)
    if public:
        grants.append((EVERYONE, key, USE_HUB))
    else:
        for gid in visibility:
            grants.append((f"group:{gid}", key, USE_HUB))
            covered[USE_HUB] |= groups.members.get(gid, set())
    for permission in sorted({p for _s, _h, p, _a in access.expected if p != USE_HUB}):
        named = sorted(g for g in groups.available
                       if (group_names.get(g) or "").strip().lower() == permission)
        for gid in named:
            members = groups.members.get(gid, set())
            if public or members <= entry:
                grants.append((f"group:{gid}", key, permission))
                covered[permission] |= members
            else:
                per_person.add(permission)
    for subject, hub, permission in person:
        if subject == EVERYONE or subject in covered.get(permission, set()):
            continue
        grants.append((subject, hub, permission))
    return grants, sorted(per_person)


def plan_access(setup: Setup, source_engine: Engine, target: TargetState, people: People,
                groups: GroupPlan, group_names: dict[str, str], report: Report,
                *, grants_mode: str = "auto") -> dict[str, HubAccess]:
    from .access import migrate as access_migrate
    from .deployment import permission_catalog

    result: dict[str, HubAccess] = {}
    convert: dict[str, HubAccess] = {}
    for hub in setup.hubs:
        access = HubAccess(hub=hub, managed=target.authoritative(hub.key))
        result[hub.key] = access
        if access.managed:
            access.summary = {"state": "console-managed",
                              "detail": "already Console-managed: grants kept, groups imported for reference"}
            continue
        public = setup.public.get(hub.key, False)
        try:
            plan = access_migrate.plan_from_csv(hub.path, hub.key)
            legacy = access_migrate.plan_from_owui(
                source_engine, hub.key, model_id=None if public else hub.model_id, plan=plan,
                permissions=[p["permission"] for p in permission_catalog(hub.path)],
                standalone_public=public)
        except access_migrate.MigrationBlocked as exc:
            reason = str(exc)
            if "select an existing OWUI model" in reason:
                reason = (f"Open WebUI has no model entry for this agent ({hub.model_id}), so who "
                          "could use it cannot be read. Register the agent's model in Open WebUI, "
                          "then run the migration again")
            report.block(f"Access for {hub.key} cannot be migrated: {reason}.")
            access.blocked = True
            access.summary = {"state": "blocked", "detail": reason}
            continue
        except Exception as exc:  # noqa: BLE001 - reported, never guessed
            report.block(f"Access for {hub.key} could not be read ({type(exc).__name__}: {_scrub(str(exc))}).")
            access.blocked = True
            access.summary = {"state": "blocked", "detail": type(exc).__name__}
            continue
        # People deleted in Hubzoid since an earlier import get nothing back, and
        # deactivated accounts could not sign in, so they could use nothing.
        removed = people.removed_emails
        access.person_grants = [g for g in legacy.grants if g[0] not in removed]
        access.attrs = list(legacy.attrs)
        access.visibility_backup = legacy.visibility_backup
        access.expected = [(s, h, p, a and s not in people.inactive and s not in removed)
                           for s, h, p, a in legacy.expected]
        for conflict in legacy.conflicts:
            report.block(f"Access for {hub.key}: {conflict}.")
        for warning in legacy.warnings:
            report.note(f"{hub.key}: {warning}")
        convert[hub.key] = access
    if not convert:
        return result
    supported = grants_mode == "auto" and store_resolves_groups()
    if grants_mode == "auto" and not supported:
        report.note("This build's access store does not resolve group grants, so converted agents "
                    "get per-person grants. Group membership still moves to Hubzoid groups.")
    chosen: dict[str, list] = {}
    per_person: dict[str, list[str]] = {}
    for key, access in convert.items():
        if supported:
            chosen[key], per_person[key] = group_grants(access, setup.public.get(key, False),
                                                         groups, group_names)
            access.strategy = "mixed" if per_person[key] else "groups"
        else:
            chosen[key], per_person[key] = list(access.person_grants), []
            access.strategy = "people"
    diffs = verify(convert, chosen, groups, people)
    retry = [k for k, d in diffs.items() if d and convert[k].strategy != "people"]
    for key in retry:
        report.note(f"{key}: group grants did not reproduce who could use what exactly, so this "
                    "agent gets the per-person migration.")
        chosen[key], per_person[key] = list(convert[key].person_grants), []
        convert[key].strategy = "people"
    if retry:
        diffs = verify(convert, chosen, groups, people)
    for key, access in convert.items():
        access.grants = chosen[key]
        allowed = sum(1 for *_x, a in access.expected if a)
        kinds: dict[str, int] = defaultdict(int)
        for subject, _h, _p in access.grants:
            kinds[_subject_kind(subject)] += 1
        access.summary = {
            "state": "convert",
            "detail": "Open WebUI decided access; becomes Console-managed",
            "entry": "everyone signed in" if setup.public.get(key) else "Open WebUI model access",
            "strategy": access.strategy,
            "grants": dict(sorted(kinds.items())),
            "grants_total": len(access.grants),
            "matrix_checked": len(access.expected),
            "matrix_allowed": allowed,
            "matrix_denied": len(access.expected) - allowed,
            "differences": len(diffs.get(key) or []),
        }
        if per_person[key]:
            access.summary["per_person_capabilities"] = per_person[key]
            access.summary["notes"] = [
                "per-person grants for " + ", ".join(per_person[key]) + ": the group with that "
                "name includes people who cannot open this agent"]
        if not access.grants:
            report.note(f"{key}: nobody could use this agent in Open WebUI, so it is locked until "
                        "someone is granted access in the Console.")
        if diffs.get(key):
            report.block(f"Access for {key}: the migrated grants would change who may use what "
                         f"({len(diffs[key])} decisions differ). Nothing is applied.")
            access.summary["examples"] = [dict(permission=d["permission"], expected=d["expected"],
                                               actual=d["actual"]) for d in diffs[key][:5]]
    if any(a.strategy in ("groups", "mixed") for a in convert.values()):
        report.note("Going forward, membership of Hubzoid groups decides access to converted "
                    "agents. A capability granted to a group also lets its members open the agent.")
    return result


# ---------------------------------------------------------------------------
# Assistant markup to message parts (contract 6.4)
# ---------------------------------------------------------------------------
_MARKUP_RE = re.compile(
    r"(?P<think><think>(?P<think_body>.*?)(?:</think>|\Z))"
    r"|(?P<rdetails><details\s+type=\"reasoning\"[^>]*>(?P<rd_body>.*?)</details>)"
    r"|(?P<tdetails><details\s+type=\"tool_calls\"(?P<td_attrs>[^>]*)>(?P<td_body>.*?)</details>)"
    r"|(?P<compact><details>\s*<summary>\s*(?P<c_mark>[✓↳])\s+(?P<c_name>[^<\n]+?)\s*</summary>"
    r"(?P<c_body>(?:(?!</details>).){0,2000}?)</details>)"
    r"|(?P<line>^>[ \t]?(?P<l_mark>[✓↳])[ \t]+\*\*(?P<l_name>[^*\n]+)\*\*"
    r"(?:[ \t]+`(?P<l_args>[^`\n]*)`)?[ \t]*$)"
    r"|(?P<error>^>[ \t]?⚠️?[ \t]+\*\*(?P<e_name>[^*\n]+)\*\*(?:[ \t]+(?P<e_msg>[^\n]*?))?[ \t]*$)",
    re.DOTALL | re.MULTILINE,
)
_FENCE_RE = re.compile(r"^[ \t]{0,3}(```+|~~~+)", re.MULTILINE)
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
_KV_RE = re.compile(r"(?:^|(?<=\s))([A-Za-z_][A-Za-z0-9_]*)=")


def _fences(value: str) -> list[tuple[int, int]]:
    """Spans of fenced code blocks, where markup is only text."""
    spans, start, marker = [], None, ""
    for m in _FENCE_RE.finditer(value):
        token = m.group(1)
        if start is None:
            start, marker = m.start(), token[0] * 3
        elif token.startswith(marker):
            end = value.find("\n", m.end())
            spans.append((start, len(value) if end < 0 else end))
            start = None
    if start is not None:
        spans.append((start, len(value)))
    return spans


def preview_args(preview: str) -> dict:
    """1.0.x showed a preview of a call's arguments, not the arguments: JSON
    text from some runtimes, ``key=value`` pairs from others, cut with …"""
    preview = (preview or "").strip()
    if not preview:
        return {}
    if preview.startswith("{"):
        try:
            value = json.loads(preview)
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    keys = list(_KV_RE.finditer(preview))
    if keys and keys[0].start() == 0 and len(keys) <= 2:
        out = {}
        for i, m in enumerate(keys):
            end = keys[i + 1].start() if i + 1 < len(keys) else len(preview)
            out[m.group(1)] = preview[m.end():end].strip()
        return out
    return {"preview": preview}


def _short(name: str) -> str:
    return name.split("__")[-1] if name.startswith("mcp__") else name


def _item_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or value.get("content") or "")
    if isinstance(value, list):
        return "".join(str(p.get("text")) for p in value if isinstance(p, dict) and p.get("text") is not None)
    return ""


class PartsBuilder:
    """One message's parts. Turns 1.0.x text markup into structured parts;
    a tool error attaches to the call it belongs to."""

    def __init__(self, message_id: str, stats: dict | None = None):
        self.message_id = message_id
        self.parts: list[dict] = []
        self.stats = stats if stats is not None else {}
        self._n = 0

    def _count(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1

    def text(self, value: str) -> None:
        if not value or not value.strip():
            return
        value = value.strip("\n")
        if self.parts and self.parts[-1]["type"] == "text":
            self.parts[-1]["text"] = self.parts[-1]["text"].rstrip("\n") + "\n\n" + value
        else:
            self.parts.append({"type": "text", "text": value})

    def reasoning(self, value: str) -> None:
        value = value or ""
        if value.strip() == THINKING_PLACEHOLDER:
            value = ""
            self._count("reasoning_placeholders_emptied")
        self.parts.append({"type": "reasoning", "text": value.strip("\n")})

    def call(self, name: str, args: Any, call_id: str | None = None, ok: bool = True,
             message: str | None = None) -> dict:
        if not call_id:
            self._n += 1
            call_id = f"{self.message_id}-t{self._n}"
        if not isinstance(args, dict):
            args = {} if args in (None, "") else {"input": args}
        part = {"type": "tool-call", "toolCallId": call_id, "toolName": (name or "tool").strip(),
                "args": args,
                "result": {"status": "ok"} if ok else {"status": "error", "message": message or ""}}
        self.parts.append(part)
        if not ok:
            self._count("tool_errors")
        return part

    def error(self, name: str, message: str) -> None:
        name = name.strip()
        for part in reversed(self.parts):
            if (part["type"] == "tool-call" and part["result"].get("status") == "ok"
                    and (part["toolName"] == name or _short(part["toolName"]) == _short(name))):
                part["result"] = {"status": "error", "message": message}
                self._count("tool_errors")
                return
        self.call(name, {}, ok=False, message=message)
        self._count("tool_errors_without_a_call")

    def markup(self, value: Any) -> None:
        """Parse text that may hold 1.0.x markup: tool lines and blocks, tool
        errors, ``<think>`` blocks, Open WebUI reasoning and tool details."""
        if not isinstance(value, str) or not value:
            return
        fences = _fences(value)
        cursor = 0
        for m in _MARKUP_RE.finditer(value):
            if any(start <= m.start() < end for start, end in fences):
                continue
            self.text(value[cursor:m.start()])
            cursor = m.end()
            if m.group("think") is not None:
                self.reasoning(m.group("think_body") or "")
            elif m.group("rdetails") is not None:
                body = re.sub(r"<summary>.*?</summary>", "", m.group("rd_body") or "", flags=re.DOTALL)
                self.reasoning("\n".join(re.sub(r"^>\s?", "", line) for line in body.strip("\n").splitlines()))
            elif m.group("tdetails") is not None:
                attrs = {k: html.unescape(v) for k, v in _ATTR_RE.findall(m.group("td_attrs") or "")}
                args: Any = attrs.get("arguments") or ""
                for _ in range(2):
                    if isinstance(args, str) and args.strip():
                        try:
                            args = json.loads(args)
                        except ValueError:
                            break
                self.call(attrs.get("name") or "tool", args, call_id=attrs.get("id") or None)
            elif m.group("compact") is not None:
                body = (m.group("c_body") or "").strip()
                if not body or body == "_(no arguments)_":
                    args = {}
                elif len(body) >= 2 and body.startswith("`") and body.endswith("`"):
                    args = preview_args(body[1:-1])
                else:
                    args = preview_args(body)
                self.call(m.group("c_name"), args)
            elif m.group("line") is not None:
                self.call(m.group("l_name"), preview_args(m.group("l_args") or ""))
            elif m.group("error") is not None:
                self.error(m.group("e_name"), (m.group("e_msg") or "").strip())
        self.text(value[cursor:])

    def output(self, items: list) -> None:
        """Open WebUI 0.9+ ``output`` items: reasoning, message, function calls."""
        calls: dict[str, dict] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "reasoning":
                self.reasoning(_item_text(item.get("content")) or _item_text(item.get("summary")))
            elif kind == "message":
                self.markup(_item_text(item.get("content")))
            elif kind == "function_call":
                args: Any = item.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except ValueError:
                        args = {"input": args}
                cid = str(item.get("call_id") or item.get("id") or "") or None
                part = self.call(str(item.get("name") or "tool"), args, call_id=cid,
                                 ok=item.get("status") not in ("failed", "incomplete"))
                if cid:
                    calls[cid] = part
            elif kind == "function_call_output":
                part = calls.get(str(item.get("call_id") or ""))
                failed = item.get("status") in ("failed", "incomplete") or bool(item.get("error"))
                if part is not None and failed and part["result"].get("status") == "ok":
                    part["result"] = {"status": "error", "message": _item_text(item.get("error"))}
                    self._count("tool_errors")
            else:
                self._count("unsupported_output_items")


def assistant_parts(message: dict, message_id: str, stats: dict | None = None) -> list[dict]:
    """Parts of one Open WebUI assistant message (``output`` items when
    present, else its text with 1.0.x markup)."""
    builder = PartsBuilder(message_id, stats)
    output = message.get("output")
    if isinstance(output, list) and output:
        builder.output(output)
    else:
        builder.markup(_item_text(message.get("content")))
    return builder.parts


def parts_text(parts: list[dict]) -> str:
    """Plain text for search: the text parts, then attachment names."""
    texts = [p["text"] for p in parts if p.get("type") == "text" and p.get("text")]
    names = [p["name"] for p in parts if p.get("type") in ("file", "image") and p.get("name")]
    return "\n\n".join(texts + names)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
class Attachments:
    """Resolves a message's files against the chat's uploads folder (where the
    1.0.x bridge copied what people attached), bringing in bytes Open WebUI
    still holds when the folder lacks them. Writes files only when applying."""

    def __init__(self, source: OwuiSource, uploads_dir: Path | None, report: Report, apply: bool):
        self.source = source
        self.owui_uploads = uploads_dir.resolve() if uploads_dir else None
        self.report = report
        self.apply = apply

    def _owui_bytes(self, conn: Connection, file_id: str, name: str) -> Path | None:
        if not self.owui_uploads:
            return None
        candidates: list[Path] = []
        row = self.source.file_row(conn, file_id) if file_id else None
        if row and isinstance(row.get("path"), str) and os.path.isabs(row["path"]):
            candidates.append(Path(row["path"]))
        if file_id:
            candidates.append(self.owui_uploads / f"{file_id}_{name}")
            if row and row.get("filename"):
                candidates.append(self.owui_uploads / f"{file_id}_{row['filename']}")
        for path in candidates:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if self.owui_uploads in resolved.parents and resolved.is_file():
                return resolved
        return None

    @staticmethod
    def _write(folder: Path, name: str, payload: bytes | None, source: Path | None, mime: str) -> None:
        from . import uploads as uploads_lib

        folder.mkdir(parents=True, exist_ok=True)
        target = (folder / name).resolve()
        if target.parent != folder.resolve():
            raise OSError("attachment name leaves the uploads folder")
        tmp = folder / f".migrating-{hashlib.sha256(name.encode()).hexdigest()[:16]}"
        try:
            if payload is not None:
                tmp.write_bytes(payload)
            else:
                shutil.copyfile(source, tmp)
            with open(tmp, "rb") as fh:
                head = fh.read(4096)
            size = tmp.stat().st_size
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
        (folder / f"{name}{uploads_lib.SIDECAR_SUFFIX}").write_text(
            json.dumps({"mime": mime, "size": size, "kind": uploads_lib.classify(mime, head)}),
            encoding="utf-8")

    def parts(self, conn: Connection, files: Any, folder: Path, *, count: bool = True) -> list[dict]:
        from . import uploads as uploads_lib

        bump = self.report.bump if count else (lambda *_a, **_k: None)
        out: list[dict] = []
        if not isinstance(files, list):
            return out
        for entry in files:
            if not isinstance(entry, dict):
                continue
            kind_in = str(entry.get("type") or "file").strip().lower()
            if kind_in not in _FILE_KINDS:
                bump("files", "non_file_attachments_not_imported")
                continue
            inner = entry.get("file") if isinstance(entry.get("file"), dict) else {}
            meta = inner.get("meta") if isinstance(inner.get("meta"), dict) else {}
            url = entry.get("url") if isinstance(entry.get("url"), str) else ""
            name = entry.get("name") or inner.get("filename") or meta.get("name") or ""
            mime = entry.get("content_type") or meta.get("content_type") or ""
            payload: bytes | None = None
            if url.startswith("data:"):
                match = re.match(r"^data:([^;,]+)?(?:;[^,]*)?;base64,(.*)$", url, re.DOTALL)
                if match:
                    try:
                        payload = base64.b64decode(match.group(2))
                    except (binascii.Error, ValueError):
                        payload = None
                    mime = mime or (match.group(1) or "")
                if payload is None:
                    bump("files", "unreadable_inline_attachment_not_imported")
                    continue
                if not name:
                    ext = mimetypes.guess_extension(mime or "") or ".bin"
                    name = f"image-{hashlib.sha256(payload).hexdigest()[:12]}{ext}"
            file_id = str(entry.get("id") or inner.get("id") or "")
            if not file_id and "/api/v1/files/" in url:
                file_id = url.split("/api/v1/files/", 1)[1].split("/", 1)[0].split("?", 1)[0]
            safe = _safe_name(name) or (_safe_name(f"attachment-{file_id[:12]}") if file_id else "")
            if not safe:
                bump("files", "unnamed_attachment_not_imported")
                continue
            mime = mime or uploads_lib.guess_mime(safe)
            kind = "image" if kind_in == "image" or mime.startswith("image/") else "file"
            source_path = None if payload is not None else self._owui_bytes(conn, file_id, name)
            source_size = len(payload) if payload is not None else (
                source_path.stat().st_size if source_path else None)
            stored, target = safe, folder / safe
            outcome = "missing"
            if target.is_file() and (source_size is None or target.stat().st_size == source_size):
                outcome = "present"
            elif source_size is not None:
                if target.is_file():  # a different file with the same name (another turn)
                    stem, dot, ext = safe.rpartition(".")
                    tag = re.sub(r"[^A-Za-z0-9_-]", "", file_id)[:8] or \
                        hashlib.sha256(payload or b"").hexdigest()[:8]
                    stored = f"{stem} ({tag}).{ext}" if dot and stem else f"{safe} ({tag})"
                    target = folder / stored
                if target.is_file():
                    outcome = "present"
                else:
                    outcome = "extracted_inline_image" if payload is not None else "copied_from_open_webui"
                    if self.apply:
                        try:
                            self._write(folder, stored, payload, source_path, mime)
                        except OSError as exc:
                            outcome = "could_not_be_written"
                            self.report.warn("Some attachments could not be written into their chat's "
                                             f"uploads folder ({type(exc).__name__}); the messages keep "
                                             "their names. Check the hub folder's permissions and run "
                                             "the migration again.")
            size: Any = entry.get("size") or meta.get("size")
            if target.is_file():
                size = target.stat().st_size
                sidecar = uploads_lib.read_meta(folder, stored)
                if sidecar and sidecar.get("mime"):
                    mime = sidecar["mime"]
            elif source_size is not None:
                size = source_size
            bump("files", outcome)
            try:
                size = int(size) if size is not None else 0
            except (TypeError, ValueError):
                size = 0
            out.append({"type": kind, "file_id": stored, "name": name or stored, "mime": mime, "size": size})
        return out


# ---------------------------------------------------------------------------
# Conversations and shares
# ---------------------------------------------------------------------------
@dataclass
class ChatOutcome:
    conversation: dict | None = None
    action: str = "skip"             # insert | update | same | keep | skip
    insert_messages: list[dict] = field(default_factory=list)
    update_messages: list[dict] = field(default_factory=list)
    shares: list[tuple[str, dict]] = field(default_factory=list)   # (insert | update, row)


def _history(data: dict) -> tuple[dict[str, dict], str | None]:
    history = data.get("history") if isinstance(data.get("history"), dict) else {}
    raw = history.get("messages") if isinstance(history.get("messages"), dict) else {}
    messages = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, dict)}
    current = history.get("currentId")
    if not messages and isinstance(data.get("messages"), list):  # very old chats: a flat list
        parent = None
        for m in data["messages"]:
            if isinstance(m, dict) and isinstance(m.get("id"), str):
                messages[m["id"]] = {**m, "parentId": m.get("parentId", parent)}
                parent = m["id"]
        current = current or parent
    return messages, current if isinstance(current, str) else None


def _branch(messages: dict[str, dict], head: str | None) -> list[str]:
    """Ids from the root to ``head``: the branch a person sees."""
    out, seen = [], set()
    while head and head in messages and head not in seen:
        seen.add(head)
        out.append(head)
        head = messages[head].get("parentId")
    return list(reversed(out))


def _user_parts(message: dict) -> tuple[list[dict], list]:
    """(text parts, inline image entries) of a user message's content."""
    content = message.get("content")
    if isinstance(content, str):
        return ([{"type": "text", "text": content}] if content.strip() else []), []
    if isinstance(content, list):
        txt = _item_text([c for c in content if isinstance(c, dict) and c.get("type") == "text"])
        inline = [{"type": "image", "url": (c.get("image_url") or {}).get("url")}
                  for c in content if isinstance(c, dict) and c.get("type") == "image_url"
                  and isinstance(c.get("image_url"), dict)]
        return ([{"type": "text", "text": txt}] if txt.strip() else []), inline
    return [], []


class ConversationImporter:
    def __init__(self, setup: Setup, source: OwuiSource, people: People, target: TargetState,
                 prev: dict | None, report: Report, *, apply: bool):
        self.setup = setup
        self.source = source
        self.people = people
        self.target = target
        self.prev = prev
        self.report = report
        self.by_model = {h.model_id: h for h in setup.hubs if h.model_id}
        for alias, goal in setup.aliases.items():
            hub = self.by_model.get(goal) or next((h for h in setup.hubs if h.key == goal.lower()), None)
            if hub is not None:  # locate() refused unknown targets
                self.by_model[alias] = hub
        self.via_workspace_model: set[str] = set()
        for model_id, base in source.model_bases().items():
            if model_id not in self.by_model and base in self.by_model:
                self.by_model[model_id] = self.by_model[base]
                self.via_workspace_model.add(model_id)
        self.claimed: set[str] = set()
        self.share_meta = source.share_meta()
        self.shares_by_chat: dict[str, list[str]] = defaultdict(list)
        for sid, meta in sorted(self.share_meta.items()):
            self.shares_by_chat[meta["chat_id"]].append(sid)
        self.audience = source.share_audiences()
        self.attachments = Attachments(source, setup.uploads_dir, report, apply)
        self.skipped: dict[str, set[str]] = {"conversations": set(), "shares": set()}
        self.stats: dict[str, int] = {}

    def _hub_for(self, data: dict, messages: dict[str, dict]) -> tuple[HubInfo | None, str]:
        candidates = [m for m in (data.get("models") or []) if isinstance(m, str) and m]
        candidates += [m.get("model") for m in messages.values()
                       if m.get("role") == "assistant" and isinstance(m.get("model"), str)]
        for model in candidates:
            if model in self.by_model:
                if model in self.via_workspace_model:
                    self.report.bump("conversations", "agent_found_through_a_workspace_model")
                return self.by_model[model], model
        return None, (candidates[0] if candidates else "")

    def _message_ids(self, conn: Connection | None, cid: str, ids: list[str]) -> dict[str, str]:
        """Open WebUI message id -> Hubzoid id for one chat. The id stays unless
        another conversation already uses it (clones) or it breaks the id rules."""
        existing: dict[str, str] = {}
        if conn is not None and "hz_messages" in self.target.tables and ids:
            wanted = list(ids) + [_alt_id(cid, i) for i in ids]
            for chunk in _chunks(wanted):
                params = {f"i{n}": v for n, v in enumerate(chunk)}
                sql = ("SELECT id, conversation_id FROM hz_messages WHERE id IN (" +
                       ", ".join(f":i{n}" for n in range(len(chunk))) + ")")
                for mid, conv in conn.execute(text(sql), params):
                    existing[mid] = conv
        out: dict[str, str] = {}
        for mid in ids:
            alt = _alt_id(cid, mid)
            if existing.get(mid) == cid:
                out[mid] = mid
            elif existing.get(alt) == cid:
                out[mid] = alt
            elif _ID_RE.match(mid) and mid not in existing and mid not in self.claimed:
                out[mid] = mid
            else:
                out[mid] = alt
                self.report.bump("messages", "ids_replaced_copied_or_invalid")
            self.claimed.add(out[mid])
        return out

    def _existing_messages(self, conn: Connection | None, cid: str) -> dict[str, dict]:
        if conn is None or "hz_messages" not in self.target.tables or cid not in self.target.conversations:
            return {}
        rows = conn.execute(text("SELECT id, parent_id, content, status, updated_at FROM hz_messages "
                                 "WHERE conversation_id=:c"), {"c": cid}).mappings()
        return {r["id"]: dict(r) for r in rows}

    def _rows(self, conn: Connection, cid: str, messages: dict[str, dict], ids: dict[str, str],
              folder: Path, hub: HubInfo, created: float) -> list[dict]:
        report = self.report
        skipped_parent: dict[str, Any] = {}
        for mid, message in messages.items():
            if message.get("role") not in ("user", "assistant"):
                skipped_parent[mid] = message.get("parentId")
                report.skip("messages", f"role {message.get('role') or 'missing'} not imported")
        rows: list[dict] = []
        for mid, message in messages.items():
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            parent, hops = message.get("parentId"), 0
            while parent in skipped_parent and hops < 10000:
                parent, hops = skipped_parent[parent], hops + 1
            if parent is not None and parent not in messages:
                report.bump("messages", "unknown_parent_made_root")
                parent = None
            if role == "assistant":
                parts = assistant_parts(message, ids[mid], self.stats)
                parts += self.attachments.parts(conn, message.get("files"), folder)
            else:
                parts, inline = _user_parts(message)
                parts += self.attachments.parts(conn, inline, folder)
                parts += self.attachments.parts(conn, message.get("files"), folder)
            error = message.get("error")
            error_text = None
            if error:
                error_text = (str(error.get("content") or error.get("message") or _dumps(error))
                              if isinstance(error, dict) else str(error))
            status = "error" if error else ("cancelled" if message.get("done") is False else "complete")
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
            if usage:
                usage = {"input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
                         "output_tokens": usage.get("output_tokens", usage.get("completion_tokens"))}
                usage = usage if any(v is not None for v in usage.values()) else None
            when = _ts(message.get("timestamp"), created) or created
            rows.append(dict(
                id=ids[mid], conversation_id=cid, parent_id=ids.get(parent) if parent else None,
                role=role, content=_dumps(parts), text=parts_text(parts), status=status,
                error=(error_text or "")[:4000] or None,
                model=(message.get("model") or hub.model_id) if role == "assistant" else None,
                usage=_dumps(usage) if usage else None, created_at=when, updated_at=when))
            report.bump("messages", role)
            if status != "complete":
                report.bump("messages", f"status_{status}")
            for part in parts:
                report.bump("parts", part["type"])
            if role == "assistant":
                blob = parts_text(parts)
                if "$$" in blob or "\\(" in blob or "\\[" in blob:
                    report.bump("content", "assistant_messages_with_math")
                if "```mermaid" in blob:
                    report.bump("content", "assistant_messages_with_mermaid")
        by_id = {r["id"]: r for r in rows}
        for row in rows:  # break parent cycles: never seen in practice, never trusted
            seen, node = set(), row
            while node is not None and node["parent_id"]:
                if node["id"] in seen:
                    row["parent_id"] = None
                    report.bump("messages", "parent_cycle_broken")
                    break
                seen.add(node["id"])
                node = by_id.get(node["parent_id"])
        return rows

    def _head(self, rows: list[dict], current: str | None, ids: dict[str, str]) -> str | None:
        head = ids.get(current) if current else None
        if head in {r["id"] for r in rows}:
            return head
        if not rows:
            return None
        self.report.bump("conversations", "current_branch_repaired")
        parents = {r["parent_id"] for r in rows}
        leaves = [r for r in rows if r["id"] not in parents] or rows
        return max(leaves, key=lambda r: (r["created_at"], r["id"]))["id"]

    def _snapshot(self, data: dict, ids: dict[str, str], hub: HubInfo, conn: Connection,
                  folder: Path, title: str, audience: str) -> str:
        messages, current = _history(data)
        if current not in messages and messages:
            current = max(messages, key=lambda k: (_ts(messages[k].get("timestamp")) or 0, k))
        out, parent = [], None
        for mid in _branch(messages, current):
            message = messages[mid]
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            new_id = ids.get(mid) or mid
            if role == "assistant":
                parts = assistant_parts(message, new_id)
            else:
                parts, inline = _user_parts(message)
                parts += self.attachments.parts(conn, inline, folder, count=False)
            parts += self.attachments.parts(conn, message.get("files"), folder, count=False)
            out.append(dict(id=new_id, parent_id=parent, role=role, content=parts,
                            status="error" if message.get("error") else "complete",
                            created_at=_ts(message.get("timestamp"))))
            parent = new_id
        return _dumps({"version": 1, "source": "openwebui", "title": title, "agent": hub.model_id,
                       "audience": audience, "head_id": parent, "messages": out})

    def run(self, target_conn: Connection | None, writer: "Writer | None") -> None:
        with self.source.engine.connect() as stream, self.source.engine.connect() as aux:
            for chat in self.source.iter_chats(stream):
                outcome = self._one(chat, aux, target_conn)
                if writer is not None and outcome.action != "skip":
                    writer.conversation(outcome)
        for key, value in sorted(self.stats.items()):
            self.report.bump("parts", key, value)

    def _skip(self, reason: str, cid: str) -> ChatOutcome:
        self.report.skip("conversations", reason, cid)
        self.skipped["conversations"].add(cid)
        for sid in self.shares_by_chat.get(cid, []):
            self.report.skip("shares", "conversation not imported", sid)
            self.skipped["shares"].add(sid)
        return ChatOutcome()

    def _one(self, chat: dict, aux: Connection, target_conn: Connection | None) -> ChatOutcome:
        cid, owner = str(chat.get("id") or ""), str(chat.get("user_id") or "")
        if owner.startswith("shared-"):
            self.report.bump("conversations", "share_copies_in_open_webui")
            return ChatOutcome()
        self.report.bump("conversations", "in_open_webui")
        meta = _json(chat.get("meta"))
        if isinstance(meta, dict) and meta.get("internal") is True:
            return self._skip("internal Open WebUI chat (notes, automations)", cid)
        if not _ID_RE.match(cid):
            return self._skip("unsupported conversation id", cid)
        if owner not in self.people.email_of:
            if owner in self.people.not_restored:
                return self._skip("owner deleted in Hubzoid", cid)
            if owner in self.people.skipped_ids:
                return self._skip("owner not imported", cid)
            return self._skip("owner account missing in Open WebUI", cid)
        data = _json(chat.get("chat"))
        if not isinstance(data, dict):
            return self._skip("unreadable chat data", cid)
        messages, current = _history(data)
        if not messages:
            rows = self.source.chat_message_rows(aux, cid)
            if rows:
                messages = rows
                self.report.bump("conversations", "history_read_from_chat_message_table")
        current = current or chat.get("current_message_id")
        hub, model = self._hub_for(data, messages)
        if hub is None:
            key = model or "(none)"
            self.report.unknown_models[key] = self.report.unknown_models.get(key, 0) + 1
            return self._skip("agent (model) not in this deployment", cid)
        created = _ts(chat.get("created_at"), time.time()) or time.time()
        updated = max(_ts(chat.get("updated_at"), created) or created, created)
        existing = self.target.conversations.get(cid)
        if existing is not None and existing.get("source") != MIGRATED:
            return self._skip("a Hubzoid conversation already uses this id", cid)
        if existing is not None and existing.get("owner_id") != owner:
            return self._skip("existing conversation belongs to someone else", cid)
        if existing is None and _existed_before(self.prev, created, "conversations", cid):
            self.report.bump("conversations", "deleted_in_hubzoid_not_restored")
            self.report.listed("conversations", "deleted in Hubzoid since the last import", cid)
            return ChatOutcome()
        if existing is None:
            action = "insert"
        elif _changed_since(self.prev, existing.get("updated_at")):
            action = "keep"
        else:
            action = "update"
        from .memory import chat_root, sanitize_chat_id

        folder = chat_root(hub.path, sanitize_chat_id(cid) or cid) / "uploads"
        ids = self._message_ids(target_conn, cid, list(messages))
        rows = self._rows(aux, cid, messages, ids, folder, hub, created)
        title = str(chat.get("title") or data.get("title") or "").strip() or "New chat"
        conversation = dict(id=cid, owner_id=owner, owner_email=self.people.email_of[owner],
                            hub=hub.key, agent=hub.model_id, title=title, title_source=MIGRATED,
                            archived=1 if chat.get("archived") in (True, 1) else 0,
                            head_id=self._head(rows, current, ids), source=MIGRATED,
                            created_at=created, updated_at=updated)
        if action == "update" and all(existing.get(k) == conversation[k]
                                      for k in ("title", "archived", "head_id", "updated_at")):
            action = "same"
        outcome = ChatOutcome(conversation=conversation, action=action)
        have = self._existing_messages(target_conn, cid)
        for row in rows:
            old = have.get(row["id"])
            if old is None:
                outcome.insert_messages.append(row)
            elif (action in ("update", "same") and not _changed_since(self.prev, old.get("updated_at"))
                  and (old.get("content"), old.get("parent_id"), old.get("status"))
                  != (row["content"], row["parent_id"], row["status"])):
                outcome.update_messages.append(row)
        report = self.report
        report.bump("conversations", {"insert": "to_import", "update": "to_update", "same": "unchanged",
                                      "keep": "kept_changed_in_hubzoid"}[action])
        report.bump("conversations", f"for_{hub.key}")
        if conversation["archived"]:
            report.bump("conversations", "archived")
        if chat.get("pinned") in (True, 1):
            report.bump("conversations", "pinned_now_normal")
        if chat.get("folder_id"):
            report.bump("conversations", "in_folders_now_listed_together")
        if len({r["parent_id"] for r in rows if r["parent_id"]}) < sum(1 for r in rows if r["parent_id"]):
            report.bump("conversations", "with_branches")
        report.bump("messages", "to_import", len(outcome.insert_messages))
        report.bump("messages", "to_update", len(outcome.update_messages))
        self._shares(chat, data, cid, ids, hub, aux, folder, title, outcome)
        return outcome

    def _shares(self, chat: dict, data: dict, cid: str, ids: dict[str, str], hub: HubInfo,
                aux: Connection, folder: Path, title: str, outcome: ChatOutcome) -> None:
        tokens = list(dict.fromkeys(([chat["share_id"]] if chat.get("share_id") else [])
                                    + self.shares_by_chat.get(cid, [])))
        for sid in tokens:
            self.report.bump("shares", "in_open_webui")
            if not _ID_RE.match(sid):
                self.report.skip("shares", "unsupported share id", sid)
                self.skipped["shares"].add(sid)
                continue
            meta = self.share_meta.get(sid)
            existing = self.target.shares.get(sid)
            if existing is not None and existing.get("conversation_id") != cid:
                self.report.skip("shares", "share id used by another conversation", sid)
                self.skipped["shares"].add(sid)
                continue
            created = (meta or {}).get("created_at") or _ts(chat.get("updated_at")) or time.time()
            if existing is None and _existed_before(self.prev, created, "shares", sid):
                self.report.bump("shares", "removed_in_hubzoid_not_restored")
                continue
            if existing is not None:
                refreshed = (meta or {}).get("updated_at")
                if not (self.prev and refreshed and refreshed > float(self.prev["time"])):
                    self.report.bump("shares", "already_present")
                    continue
            snapshot = self.source.share_snapshot(aux, sid, meta["legacy"]) if meta else None
            if snapshot is None:
                snapshot = data
                self.report.bump("shares", "from_current_branch")
            else:
                self.report.bump("shares", "from_share_snapshot")
            snap_title = str((meta or {}).get("title") or title).strip() or title
            audience = self.audience[cid]
            row = dict(id=sid, conversation_id=cid, owner_id=chat["user_id"], title=snap_title,
                       agent=hub.model_id, created_at=created,
                       snapshot=self._snapshot(snapshot, ids, hub, aux, folder, snap_title, audience))
            outcome.shares.append(("update" if existing is not None else "insert", row))
            self.report.bump("shares", "to_update" if existing is not None else "to_import")
            self.report.bump("shares", f"audience_{audience}")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
_UPSERT_META = "INSERT INTO hz_meta (k, v) VALUES (:k, :v) ON CONFLICT (k) DO UPDATE SET v=excluded.v"


class Writer:
    """Every write of the data step, on one connection in one transaction."""

    def __init__(self, conn: Connection, store, report: Report):
        self.conn = conn
        self.store = store
        self.report = report

    def _many(self, sql: str, rows: list[dict]) -> None:
        for chunk in _chunks(rows, 500):
            self.conn.execute(text(sql), chunk)

    def people(self, people: People) -> None:
        inserts = [r for uid, r in people.rows.items() if people.action.get(uid) == "insert"]
        updates = [r for uid, r in people.rows.items() if people.action.get(uid) == "update"]
        self._many("INSERT INTO hz_users (id, email, name, role, status, password_hash, password_enabled, "
                   "source, created_at, updated_at, last_login_at) VALUES (:id, :email, :name, :role, "
                   ":status, :password_hash, :password_enabled, :source, :created_at, :updated_at, "
                   ":last_login_at)", inserts)
        self._many("UPDATE hz_users SET email=:email, name=:name, role=:role, status=:status, "
                   "password_hash=:password_hash, password_enabled=:password_enabled, "
                   "updated_at=:updated_at WHERE id=:id", updates)
        self._many("INSERT INTO hz_user_identities (provider, issuer, subject, user_id, email, created_at) "
                   "VALUES (:provider, :issuer, :subject, :user_id, :email, :created_at)", people.identity_rows)
        self._many("UPDATE hz_identities SET display=:d WHERE subject=:s AND (display IS NULL OR display='')",
                   [dict(s=e, d=n) for e, n in sorted(people.display.items()) if n])
        for email in sorted(people.inactive_new):
            self.conn.execute(text(_UPSERT_META), {"k": "suspended:" + email, "v": "1"})
            self.store.write_audit(self.conn, ACTOR, "suspend", subject=email, hub="*", surface="migration")
        self.report.bump("users", "imported", len(inserts))
        self.report.bump("users", "updated", len(updates))
        self.report.bump("identities", "imported", len(people.identity_rows))

    def groups(self, plan: GroupPlan) -> None:
        self._many("INSERT INTO hz_groups (id, name, description, source, created_by, created_at, "
                   "updated_at) VALUES (:id, :name, :description, :source, :created_by, :created_at, "
                   ":updated_at)", plan.inserts)
        self._many("INSERT INTO hz_group_members (group_id, email, added_by, added_at) "
                   "VALUES (:group_id, :email, :added_by, :added_at)", plan.add_members)
        self._many("DELETE FROM hz_group_members WHERE group_id=:g AND email=:e AND added_by=:a",
                   [dict(g=g, e=e, a=ACTOR) for g, e in plan.remove_members])
        self.report.bump("groups", "imported", len(plan.inserts))
        self.report.bump("groups", "members_imported", len(plan.add_members))

    def conversation(self, outcome: ChatOutcome) -> None:
        conv = outcome.conversation
        if outcome.action == "insert":
            self.conn.execute(text(
                "INSERT INTO hz_conversations (id, owner_id, owner_email, hub, agent, title, title_source, "
                "archived, head_id, source, created_at, updated_at) VALUES (:id, :owner_id, :owner_email, "
                ":hub, :agent, :title, :title_source, :archived, :head_id, :source, :created_at, "
                ":updated_at)"), conv)
            self.report.bump("conversations", "imported")
        elif outcome.action == "update":
            self.conn.execute(text(
                "UPDATE hz_conversations SET title=:title, archived=:archived, head_id=:head_id, "
                "updated_at=:updated_at WHERE id=:id AND source='migrated'"), conv)
            self.report.bump("conversations", "updated")
        self._many("INSERT INTO hz_messages (id, conversation_id, parent_id, role, content, text, status, "
                   "error, model, usage, created_at, updated_at) VALUES (:id, :conversation_id, "
                   ":parent_id, :role, :content, :text, :status, :error, :model, :usage, :created_at, "
                   ":updated_at)", outcome.insert_messages)
        self._many("UPDATE hz_messages SET parent_id=:parent_id, content=:content, text=:text, "
                   "status=:status, error=:error, updated_at=:updated_at WHERE id=:id AND "
                   "conversation_id=:conversation_id", outcome.update_messages)
        self.report.bump("messages", "imported", len(outcome.insert_messages))
        self.report.bump("messages", "updated", len(outcome.update_messages))
        for kind, row in outcome.shares:
            if kind == "insert":
                self.conn.execute(text(
                    "INSERT INTO hz_shares (id, conversation_id, owner_id, title, agent, snapshot, "
                    "created_at) VALUES (:id, :conversation_id, :owner_id, :title, :agent, :snapshot, "
                    ":created_at)"), row)
                self.report.bump("shares", "imported")
            else:
                self.conn.execute(text("UPDATE hz_shares SET title=:title, snapshot=:snapshot "
                                       "WHERE id=:id AND conversation_id=:conversation_id"), row)
                self.report.bump("shares", "updated")

    def marker(self, key: str, value: dict) -> None:
        self.conn.execute(text(_UPSERT_META), {"k": key, "v": _dumps(value)})


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def _describe(setup: Setup, source: OwuiSource, target: TargetState, report: Report,
              grants_mode: str) -> None:
    from . import migrations

    report.info["deployment"] = {
        "kind": setup.kind,
        "hubs": [{"key": h.key, "model_id": h.model_id} for h in setup.hubs],
        "legacy_entry": setup.public_basis,
    }
    report.info["source"] = {
        "database": setup.source_url.get_backend_name(),
        "location": _redact(setup.source_url),
        "schema_revision": source.revision(),
        "uploads_folder": str(setup.uploads_dir) if setup.uploads_dir else None,
    }
    report.info["target"] = {
        "location": _redact(setup.operational_url),
        "schema_revision": target.revision,
        "schema_after_apply": migrations.head("operational"),
    }
    report.info["grants_mode"] = grants_mode
    if setup.aliases:
        report.info["model_aliases"] = dict(setup.aliases)


@dataclass
class Plan:
    fingerprint: str
    prev: dict | None
    people: People
    groups: GroupPlan
    access: dict[str, HubAccess]


def run(setup: Setup, *, apply: bool = False, grants_mode: str = "auto") -> Report:
    """Plan the migration against a read-only view of both stores and, with
    ``apply`` and nothing blocking, carry it out. Never writes to Open WebUI."""
    report = Report("apply" if apply else "dry-run")
    source_engine = _source_engine(setup.source_url, setup.source_schema)
    try:
        source = OwuiSource(source_engine)
        read_engine = _target_engine(setup.operational_url, readonly=True)
        try:
            target = TargetState.load(read_engine)
            _describe(setup, source, target, report, grants_mode)
            users = source.users()
            fingerprint = _fingerprint(users, setup.source_url)
            prev = target.previous(fingerprint)
            report.info["fingerprint"] = fingerprint
            if prev:
                report.info["previous_import"] = {"time": prev.get("time"), "hubzoid": prev.get("hubzoid")}
            people = plan_people(setup, users, source.auth(), target, prev, report)
            owui_groups = source.groups()
            groups = plan_groups(owui_groups, source.memberships(owui_groups), people, target, prev,
                                 report)
            group_names = {gid: str(g.get("name") or "") for gid, g in owui_groups.items()}
            access = plan_access(setup, source_engine, target, people, groups, group_names, report,
                                 grants_mode=grants_mode)
            for key, hub_access in access.items():
                report.access[key] = hub_access.summary
            if not apply or report.blocking:
                importer = ConversationImporter(setup, source, people, target, prev, report, apply=False)
                with (read_engine.connect() if read_engine is not None else nullcontext(None)) as conn:
                    importer.run(conn, None)
                _finish(report)
                return report
        finally:
            if read_engine is not None:
                read_engine.dispose()
        _apply(setup, source, report, Plan(fingerprint, prev, people, groups, access))
        return report
    finally:
        source_engine.dispose()


def _finish(report: Report) -> None:
    counts = report.counts
    pinned = counts["conversations"].get("pinned_now_normal")
    if pinned:
        report.note(f"{pinned} pinned conversation(s) become normal conversations (the web app has no pins).")
    folders = counts["conversations"].get("in_folders_now_listed_together")
    if folders:
        report.note(f"{folders} conversation(s) were in Open WebUI folders; the web app lists them together.")
    if counts["files"].get("missing"):
        report.warn(f"{counts['files']['missing']} attachment(s) are named in chats but their files are "
                    "in neither the chat's uploads folder nor Open WebUI's storage. The messages keep "
                    "the names.")
    if report.unknown_models:
        report.warn("Some conversations use an agent this deployment does not serve (see "
                    "unknown_models). Pass --model-alias OLD=AGENT to import them into an agent.")
    if counts["shares"].get("audience_restricted") or counts["shares"].get("audience_owner"):
        report.warn("Some share links were limited to named people or to their owner in Open WebUI. "
                    "Each snapshot records its Open WebUI audience; the web app decides who may "
                    "open /s/<id>.")


def _apply(setup: Setup, source: OwuiSource, report: Report, plan: Plan) -> None:
    from .access.store import GrantStore
    from .deployment import _write as write_json
    from .migrations import SchemaError

    engine = _target_engine(setup.operational_url, readonly=False)
    try:
        try:
            store = GrantStore(engine)  # brings the store to this release's schema
        except SchemaError as exc:
            raise MigrationBlocked(str(exc)) from exc
        convert = {k: a for k, a in plan.access.items() if not a.managed and not a.blocked}
        snapshot = store.snapshot(sorted(convert)) if convert else None
        for key, access in convert.items():
            backup = store.snapshot([key])
            if access.visibility_backup is not None:
                backup["owui_visibility"] = access.visibility_backup
            path = access.hub.path / ".hubzoid" / "backups" / f"access-{time.time_ns()}.json"
            write_json(path, backup)
            report.backups.append(str(path))
        access_written = False
        if convert or plan.people.identities:
            try:
                store.apply_migration([g for a in convert.values() for g in a.grants],
                                      [x for a in convert.values() for x in a.attrs], sorted(convert),
                                      replace=True, authoritative=True,
                                      identities=plan.people.identities, carry_over_public=True,
                                      actor=ACTOR)
            except ValueError as exc:
                raise MigrationBlocked(f"The access step was refused: {exc}") from exc
            access_written = True
        try:
            for key, access in convert.items():
                want = set()
                for subject, hub, permission in access.grants:
                    want.add((subject.strip().lower(), hub, permission))
                    if permission != "use_hub":
                        want.add((subject.strip().lower(), hub, "use_hub"))
                have = set(store.list_grants(key))
                if want != have:
                    raise MigrationBlocked(f"Access for {key} did not match the plan after writing.")
                access.summary["grants_written"] = len(have)
            target = TargetState.load(engine)
            importer = ConversationImporter(setup, source, plan.people, target, plan.prev, report,
                                            apply=True)
            with engine.begin() as conn:
                writer = Writer(conn, store, report)
                writer.people(plan.people)
                writer.groups(plan.groups)
                importer.run(conn, writer)
                _finish(report)
                from . import __version__

                writer.marker(MARKER_PREFIX + plan.fingerprint, {
                    "version": 1, "time": time.time(), "hubzoid": __version__,
                    "source": {"database": setup.source_url.get_backend_name(),
                               "schema_revision": source.revision()},
                    "converted_hubs": sorted(convert),
                    "counts": {k: v for k, v in report.counts.items() if v},
                    "skipped": {
                        "users": sorted(plan.people.skipped_ids),
                        "groups": sorted(plan.groups.skipped_ids),
                        "conversations": sorted(importer.skipped["conversations"]),
                        "shares": sorted(importer.skipped["shares"]),
                    },
                })
        except BaseException:
            if access_written and snapshot is not None:
                try:
                    store.restore(snapshot, actor=ACTOR + "-undo")
                    report.warn("The migration failed after the access step, so the access step was undone.")
                except Exception:  # noqa: BLE001 - both failures are reported
                    log.exception("undoing the access step failed")
                    report.warn("The migration failed and undoing the access step also failed. Restore "
                                "the access backups listed below with hubzoid access rollback.")
            raise
        report.applied = True
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Rehearsal on a copy
# ---------------------------------------------------------------------------
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


def _sqlite_copy(src: Path, dst: Path) -> None:
    """A consistent copy of a live SQLite database, its write-ahead log included."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, symlinks=True,
                    ignore=shutil.ignore_patterns("*-wal", "*-shm", "*-journal", "*.migrate.lock"))
    for path in list(dst.rglob("*")):
        original = src / path.relative_to(dst)
        if path.is_file() and not path.is_symlink() and _is_sqlite(original):
            _sqlite_copy(original, path)


def rehearsal_setup(setup: Setup, dest: Path) -> Setup:
    """Copy a single hub (with its Open WebUI data and operational store) into
    ``dest`` and point the migration at the copy. The original is only read."""
    if setup.kind != "hub":
        raise MigrationBlocked(
            "--rehearse copies a single hub. For a gateway, rehearse on a restored copy: "
            "hubzoid backup <hub> archive.tar.gz, then hubzoid restore archive.tar.gz --move "
            "<old folder>=<scratch folder>, then run this command on the copy.")
    if setup.source_url.get_backend_name() != "sqlite":
        raise MigrationBlocked("--rehearse needs the Open WebUI database in SQLite. For PostgreSQL, "
                               "restore a dump into a scratch database and pass --owui-db.")
    op = make_url(setup.operational_url)
    if op.get_backend_name() != "sqlite":
        raise MigrationBlocked("--rehearse needs the operational store in SQLite. For PostgreSQL, "
                               "restore a dump into a scratch database and set HUBZOID_OPERATIONAL_DB.")
    dest = Path(dest).expanduser().resolve()
    if dest.exists() and any(dest.iterdir()):
        raise MigrationBlocked(f"{dest} is not empty. Choose an empty scratch folder.")
    hub = setup.hubs[0]
    copy = dest / hub.path.name
    _copy_tree(hub.path, copy)

    def mapped(path: Path, label: str) -> Path:
        path = path.resolve()
        if hub.path in path.parents:
            return copy / path.relative_to(hub.path)
        target = dest / label / path.name
        if path.is_file():
            _sqlite_copy(path, target)
        return target

    new_owui = mapped(Path(setup.source_url.database), "openwebui")
    if setup.uploads_dir is not None and hub.path not in setup.uploads_dir.resolve().parents:
        shutil.copytree(setup.uploads_dir, new_owui.parent / "uploads", dirs_exist_ok=True)
    new_op = mapped(Path(op.database).expanduser(), "operational")
    uploads = new_owui.parent / "uploads"
    return replace(setup, entry=copy, hubs=[replace(hub, path=copy)],
                   source_url=URL.create("sqlite", database=str(new_owui)),
                   uploads_dir=uploads if uploads.is_dir() else None,
                   operational_url=f"sqlite:///{new_op}")


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------
_LABELS = {
    "users": "People", "identities": "External sign-in links", "groups": "Groups",
    "conversations": "Conversations", "messages": "Messages", "parts": "Message parts",
    "files": "Attachments", "shares": "Share links", "content": "Assistant content (counts only)",
}


def render(report: Report, verbose: bool = False) -> str:
    """The report for people. Counts, ids with --verbose; never content or emails."""
    data = report.to_dict(verbose)
    lines = [f"Open WebUI migration ({'applied' if report.applied else report.mode})", ""]
    dep = data.get("deployment") or {}
    if dep:
        hubs = ", ".join(f"{h['key']} (agent {h['model_id']})" for h in dep.get("hubs", []))
        lines += [f"Deployment ({dep.get('kind')}): {hubs}", f"  {dep.get('legacy_entry')}"]
    src, tgt = data.get("source") or {}, data.get("target") or {}
    if src:
        lines.append(f"Source, read only: {src.get('location')} (Open WebUI schema "
                     f"{src.get('schema_revision') or 'unknown'})")
    if tgt:
        lines.append(f"Target: {tgt.get('location')} (schema {tgt.get('schema_revision') or 'new'}, "
                     f"{tgt.get('schema_after_apply')} after apply)")
    if data.get("previous_import"):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(data["previous_import"]["time"]))
        lines.append(f"Previous import: {when}. Changes made in Hubzoid since then are kept.")
    for section in Report.SECTIONS:
        counts, skipped = data["counts"].get(section), data["skipped"].get(section)
        if not counts and not skipped:
            continue
        lines += ["", _LABELS[section]]
        lines += [f"  {key.replace('_', ' ')}: {value}" for key, value in (counts or {}).items()]
        lines += [f"  skipped, {reason}: {value}" for reason, value in (skipped or {}).items()]
    if data["access"]:
        lines += ["", "Access"]
        for hub, summary in data["access"].items():
            lines.append(f"  {hub}: {summary.get('detail', summary.get('state'))}")
            if summary.get("state") == "convert":
                grants = ", ".join(f"{v} {k}" for k, v in summary.get("grants", {}).items())
                lines.append(f"    entry: {summary['entry']}; strategy: {summary['strategy']}; "
                             f"grants: {grants or 'none'}")
                lines.append(f"    checked {summary['matrix_checked']} decisions "
                             f"({summary['matrix_allowed']} allowed, {summary['matrix_denied']} denied): "
                             f"{summary['differences']} differences")
                lines += [f"    {note}" for note in summary.get("notes", [])]
    if data["unknown_models"]:
        lines += ["", "Agents not in this deployment (model id: conversations)"]
        lines += [f"  {k}: {v}" for k, v in data["unknown_models"].items()]
    for title, items in (("Blocking", data["blocking"]), ("Warnings", data["warnings"]),
                         ("Notes", data["notes"]), ("Backups", data["backups"])):
        if items:
            lines += ["", title] + [f"  - {item}" for item in items]
    if verbose and data.get("ids"):
        lines += ["", "Ids"]
        for section, rows in data["ids"].items():
            lines += [f"  {section}: {row['id']} ({row['reason']})" for row in rows]
    lines.append("")
    if report.blocking:
        lines.append("Nothing was written: resolve the blocking items first." if report.mode == "apply"
                     else "A run with --apply would be refused until the blocking items are resolved.")
    elif report.applied and data.get("rehearsal_copy"):
        lines.append(f"Migrated the rehearsal copy in {data['rehearsal_copy']}. The original was not changed.")
    elif report.applied:
        lines.append("Migrated. Start the deployment in the default mode (HUBZOID_UI=hubzoid).")
    else:
        lines.append("Dry run: nothing was changed. Stop the deployment, take a backup "
                     "(hubzoid backup), then run again with --apply.")
    return "\n".join(lines)


def _aliases(values: list[str] | None) -> dict[str, str]:
    out = {}
    for value in values or []:
        old, sep, new = value.partition("=")
        if not sep or not old.strip() or not new.strip():
            raise typer.BadParameter(f"--model-alias expects OLD=AGENT, got {value!r}")
        out[old.strip()] = new.strip()
    return out


@migrate_app.callback()
def _migrate() -> None:
    """Move an existing install to the Hubzoid web app."""


@migrate_app.command("openwebui")
def openwebui(
    path: Path = typer.Argument(Path("."), help="Hub folder, hub registered in a gateway, or gateway data folder."),
    owui_db: str = typer.Option(None, "--owui-db", help="Open WebUI database URL (SQLAlchemy) instead of the one the hub or gateway uses."),
    apply: bool = typer.Option(False, "--apply", help="Write the migration. Without it: a dry run that changes nothing."),
    as_json: bool = typer.Option(False, "--json", help="Print the report as JSON."),
    verbose: bool = typer.Option(False, "--verbose", help="Also list the ids of skipped and conflicting items."),
    rehearse: Path = typer.Option(None, "--rehearse", help="Copy the hub into this empty folder and migrate the copy. The original is only read."),
    model_alias: list[str] = typer.Option(None, "--model-alias", help="OLD=AGENT: import conversations of an old model id into this agent (model id or hub key). Repeatable."),
    grants: str = typer.Option("auto", "--grants", help="auto: group grants where they keep access exact. people: per-person grants only."),
) -> None:
    """Move accounts, groups, access, conversations and share links from Open
    WebUI to the Hubzoid web app. Open WebUI is read, never changed. The
    default is a dry run; stop the deployment and take a backup before --apply."""
    from rich.console import Console

    console = Console(stderr=as_json, highlight=False)
    if grants not in ("auto", "people"):
        raise typer.BadParameter("--grants must be auto or people")
    try:
        setup = locate(path, owui_db=owui_db, aliases=_aliases(model_alias))
        if rehearse is not None:
            setup = rehearsal_setup(setup, rehearse)
            console.print(f"Rehearsing on a copy in {Path(rehearse).expanduser().resolve()}; "
                          "the original is not changed.", markup=False)
        report = run(setup, apply=apply, grants_mode=grants)
        if rehearse is not None:
            report.info["rehearsal_copy"] = str(setup.entry)
    except MigrationBlocked as exc:
        if as_json:
            typer.echo(json.dumps({"mode": "apply" if apply else "dry-run", "applied": False,
                                   "blocking": [str(exc)]}, indent=2))
        else:
            console.print(f"Migration blocked: {exc}", markup=False)
        raise typer.Exit(2)
    if as_json:
        typer.echo(json.dumps(report.to_dict(verbose), indent=2, default=str))
    else:
        console.print(render(report, verbose), markup=False)
    if report.blocking:
        raise typer.Exit(2)
