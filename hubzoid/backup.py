# Hubzoid. Apache-2.0 licensed like the rest of the repository.
"""`hubzoid backup` and `hubzoid restore`: one archive of a deployment's state.

The archive holds every SQLite database (copied with SQLite's online backup, so
a live deployment gives a consistent copy), the chat UI data (Open WebUI's
`webui.db`, uploads and vector store, not its model cache) and each hub's
`.hubzoid`, `.inbound`, `logs` and `output`. A hub inside a gateway is backed up
with the whole gateway, since they share one operational database.

PostgreSQL databases go in as `pg_dump` archives (`postgres/<n>.dump`). The
password reaches pg_dump through its environment, never its command line.

Every file's SHA-256 is in the index, and `verify` reads the archive back:
every checksum, an SQLite integrity check of each database and a listing of
each PostgreSQL dump. A backup that fails verification is deleted and never
recorded as done. `rehearse` restores an archive into a scratch folder (and a
scratch PostgreSQL database), upgrades the copies to this release and reports,
without touching the deployment.

Not in the archive:
  - hub content (AGENTS.md, skills, knowledge, tools). It belongs in the hub's
    git repository.
  - the backups folder itself (`.hubzoid/backups`).
  - secrets (`.env`, `.hubzoid/artifact_secret`, the deployment key
    `secret.key`, `.webui_secret_key`) and the database passwords in the
    gateway's `deployment.json` (saved as `***`), unless asked for with
    `include_secrets`. The deployment key is backed up separately from the data
    it protects (see hubzoid.secretbox). Only those locations hold secrets: a
    chat attachment or an agent's file with one of those names is data, and is
    saved like any other.
  - temporary files (Hubzoid's half-written `.tmp` files) and SQLite journals
    beside a database, which the database's copy already includes.

A backup holds new scheduled runs and waits for running ones to finish. Chat
keeps working throughout. Due runs fire when the hold ends.

Restore puts each saved directory back where it was, or under a new prefix
(`moves`), and rewrites the absolute paths Hubzoid and Open WebUI store.
Whatever was at a target is kept beside it as `<name>.pre-restore-<stamp>`.
Secret files the archive does not hold stay as they were at the target, so
restoring data in place never replaces the deployment key or the link secret.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zlib import error as zlib_error

from . import datadir

log = logging.getLogger(__name__)

FORMAT = 1
INDEX = "hubzoid-backup.json"
STATE_DIRS = datadir.WRITTEN
UI_DIR = ".openwebui-data"
SECRET_FILES = {".env", "artifact_secret", "secret.key", ".webui_secret_key", ".admin_token"}
_TEMP_SUFFIXES = (".tmp", ".part")
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
_SQLITE_MAGIC = b"SQLite format 3\x00"
# Enough to cover a long copy. The hold is cleared when the backup ends, and
# expires on its own if the backup process dies.
_HOLD_MARGIN = 3 * 3600
# Seconds between setting the hold and the first look for running work, so a
# scheduler that passed its hold check just before is seen queuing its run.
_SETTLE = 5.0
_ROOT_ID = re.compile(r"r\d+")
BACKUPS_DIR = "backups"           # <state>/backups: scheduled archives
_PG_ARC = re.compile(r"postgres/\d+\.dump")
_CHUNK = 1024 * 1024


class BackupError(RuntimeError):
    pass


@dataclass
class Root:
    """One saved directory or file, stored in the archive under `id`."""

    id: str
    path: Path
    kind: str  # "state" | "ui" | "file"
    hub: str | None = None

    def to_json(self) -> dict:
        return {"id": self.id, "path": str(self.path), "kind": self.kind, "hub": self.hub}


@dataclass
class Plan:
    hubs: list[tuple[str, Path]]
    roots: list[Root]
    stores: list[str]            # distinct operational database URLs
    dbos: list[tuple[str, str]]  # (hub name, DBOS URL)
    external: list[str] = field(default_factory=list)  # PostgreSQL, redacted
    postgres: dict[str, list[str]] = field(default_factory=dict)  # URL -> roles (operational, hub, dbos)
    sqlite_paths: set[Path] = field(default_factory=set)
    operational_paths: set[Path] = field(default_factory=set)
    exclude_dirs: set[Path] = field(default_factory=set)  # the scheduled backups folder


def _redact(url: str) -> str:
    url = re.sub(r"//([^:/@]+):[^@]*@", r"//\1:***@", url)
    return re.sub(r"([?&]password=)[^&]*", r"\1***", url)


def _sqlite_path(url: str) -> Path | None:
    if not url.startswith("sqlite"):
        return None
    raw = url.split(":///", 1)[1] if ":///" in url else ""
    return Path(raw).resolve() if raw else None


def _under(path: Path, roots: list[Root]) -> bool:
    return any(r.kind != "file" and (path == r.path or r.path in path.parents) for r in roots)


def plan(hub_dir: Path, *, include_secrets: bool = False) -> Plan:
    """What a backup of the deployment containing `hub_dir` covers."""
    from . import db, deployment

    hub_dir = Path(hub_dir).resolve()
    manifest = deployment.read(hub_dir)
    hubs = [(h["key"], Path(h["path"]).resolve()) for h in manifest.get("hubs", [])] \
        or [(hub_dir.name.lower(), hub_dir)]
    roots: list[Root] = []

    def add(path: Path, kind: str, hub: str | None = None) -> None:
        roots.append(Root(f"r{len(roots)}", path, kind, hub))

    for key, path in hubs:
        state_root = datadir.root(path)          # HUBZOID_DATA_DIR, or the hub folder
        for name in STATE_DIRS:
            if (state_root / name).is_dir():
                add(state_root / name, "state", key)
        if (path / UI_DIR).is_dir():
            add(path / UI_DIR, "ui", key)
        if include_secrets and (path / ".env").is_file():
            add(path / ".env", "file", key)
    if manifest:
        # The gateway's data folder holds its manifest (and, in Open WebUI mode,
        # Open WebUI's database). The web app mode records no Open WebUI path.
        located = deployment.manifest_path(hub_dir)
        if located is not None:
            gw_data = located.resolve().parent
        elif manifest.get("owui_db"):
            gw_data = Path(manifest["owui_db"]).resolve().parent
        else:
            raise BackupError("Cannot find this gateway's data folder. Set HUBZOID_DEPLOYMENT "
                              "to its deployment.json and run hubzoid backup again.")
        if any(gw_data == h or gw_data in h.parents for _, h in hubs):
            raise BackupError(f"The gateway data directory {gw_data} contains a hub. "
                              "Give the gateway its own --data-dir to use hubzoid backup.")
        if gw_data.is_dir():
            add(gw_data, "ui")

    from . import backup_schedule

    out = Plan(hubs=hubs, roots=roots, stores=[], dbos=[],
               exclude_dirs={backup_schedule.folder(hub_dir).resolve()})
    urls: list[tuple[str, str, str]] = []
    for key, path in hubs:
        op = db.operational_url(path)
        if op not in out.stores:
            out.stores.append(op)
        urls += [(key, op, "operational"), (key, db.resolve_url(path), "hub")]
        dbos = db.dbos_url(path)
        out.dbos.append((path.name, dbos))
        urls.append((key, dbos, "dbos"))
    for key, url, role in urls:
        p = _sqlite_path(url)
        if p is None:
            if _redact(url) not in out.external:
                out.external.append(_redact(url))
            if url.startswith("postgres"):
                roles = out.postgres.setdefault(url, [])
                if role not in roles:
                    roles.append(role)
            continue
        out.sqlite_paths.add(p)
        if url in out.stores:
            out.operational_paths.add(p)
        if p.exists() and not _under(p, roots) and not any(r.path == p for r in roots):
            add(p, "file", key)
    return out


# ---------------------------------------------------------------- backup


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


def _people_files(rel: Path, root: Root) -> bool:
    """Under a folder of files that people attached or the agent made: a
    conversation's folder (`.hubzoid/chats/<id>/`), the agent's `output`, and
    Open WebUI's `uploads`. Their names are chosen by people and the agent."""
    first = rel.parts[:1]
    if root.kind == "ui":
        return first == ("uploads",)
    return root.path.name == "output" or (root.path.name == ".hubzoid" and first == ("chats",))


def _credential(rel: Path, root: Root) -> bool:
    """A credential file where Hubzoid and Open WebUI keep one: the top of a
    hub's `.hubzoid` (the link secret, the deployment key) or of a chat UI or
    gateway data folder (the deployment key next to the manifest, `.env`,
    Open WebUI's key files). The same name anywhere else is data."""
    return (rel.name in SECRET_FILES and len(rel.parts) == 1
            and (root.kind == "ui" or root.path.name == ".hubzoid"))


def _skip(rel: Path, root: Root, include_secrets: bool) -> bool:
    name = rel.name
    if name.startswith(".hubzoid-restore-") or "__pycache__" in rel.parts:
        return True
    # Earlier archives: never inside the next one.
    if root.kind == "state" and root.path.name == datadir.STATE and rel.parts[:1] == (BACKUPS_DIR,):
        return True
    # Open WebUI's model cache is large and rebuilt on demand.
    if root.kind == "ui" and rel.parts[:1] == ("cache",):
        return True
    # Half-written files from Hubzoid's atomic writes. Among people's files only
    # hidden ones are Hubzoid's (an artifact being written): uploaded names
    # never start with a dot.
    if name.endswith(_TEMP_SUFFIXES) and (name.startswith(".") or not _people_files(rel, root)):
        return True
    # A journal beside a SQLite database: the database's copy includes it.
    if name.endswith(_SQLITE_SIDECARS) and _is_sqlite(
            (root.path / rel).with_name(name.rsplit("-", 1)[0])):
        return True
    return not include_secrets and _credential(rel, root)


def _without_passwords(path: Path) -> bytes | None:
    """The gateway manifest with each database URL's password replaced by
    `***`, or None when it holds no password."""
    text = path.read_text()

    def clean(value):
        if isinstance(value, str):
            return _redact(value)
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        return value

    try:
        data = json.loads(text)
        new = clean(data)
        return None if new == data else (json.dumps(new, indent=2) + "\n").encode()
    except ValueError:  # not JSON: redact line by line
        new = "".join(_redact(line) for line in text.splitlines(keepends=True))
        return None if new == text else new.encode()


def _files(root: Root, include_secrets: bool, exclude: set[Path], exclude_dirs: set[Path] = frozenset()):
    if root.kind == "file":
        yield root.path, Path(root.path.name)
        return
    for dirpath, dirnames, filenames in os.walk(root.path):
        base = Path(dirpath)
        dirnames[:] = [d for d in dirnames
                       if not (base / d).is_symlink() and (base / d).resolve() not in exclude_dirs
                       and not _skip((base / d).relative_to(root.path), root, include_secrets)]
        for name in filenames:
            path = base / name
            rel = path.relative_to(root.path)
            if path.is_symlink() or not path.is_file() or path.resolve() in exclude:
                continue
            if _skip(rel, root, include_secrets):
                continue
            yield path, rel


def _snapshot(src: Path, dst: Path, *, drop_hold: bool) -> None:
    """A consistent copy of a live SQLite database."""
    source = sqlite3.connect(src, timeout=30)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
            if drop_hold:
                has_meta = target.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hz_meta'").fetchone()
                if has_meta:
                    target.execute("DELETE FROM hz_meta WHERE k='maintenance:hold'")
                    target.commit()
        finally:
            target.close()
    finally:
        source.close()


def running_runs(p: Plan, unknown: list[str] | None = None) -> list[str]:
    """Scheduled runs that are queued or running, across the deployment. A hub
    whose run history cannot be read (for example a DBOS database from an older
    release, before the upgrade) is added to `unknown` instead."""
    from dbos import DBOSClient

    from .workflows.runtime import _app_name

    busy: list[str] = []
    for hub_name, url in p.dbos:
        path = _sqlite_path(url)
        if path is not None and not path.exists():
            continue  # this hub has never run anything scheduled
        try:
            client = DBOSClient(system_database_url=url, application_name=_app_name(hub_name),
                                retry_connection_errors=False)
            try:
                rows = client.list_workflows(status=["PENDING", "ENQUEUED"],
                                             load_input=False, load_output=False)
            finally:
                client.destroy()
        except Exception:  # noqa: BLE001 — never block a backup on reading run history
            log.warning("backup: could not read run history for %s", hub_name, exc_info=True)
            if unknown is not None and hub_name not in unknown:
                unknown.append(hub_name)
            continue
        busy += [f"{hub_name}: {r.workflow_id}" for r in rows]
    return sorted(set(busy))


class _Store:
    """The few `hz_meta` writes a backup makes, done with plain SQL so a backup
    never creates or migrates a table. That matters when the new release backs
    up a deployment the old release wrote, before the first start upgrades it."""

    def __init__(self, url: str):
        from sqlalchemy import create_engine, inspect

        from . import db

        # postgresql:// means psycopg (3), as for every engine of Hubzoid's own.
        self.engine = create_engine(db.sqlalchemy_url(url))
        self.ok = inspect(self.engine).has_table("hz_meta")

    def put(self, key: str, value: dict) -> None:
        if not self.ok:
            return
        from sqlalchemy import text

        with self.engine.begin() as c:
            c.execute(text("INSERT INTO hz_meta(k, v) VALUES(:k, :v) "
                           "ON CONFLICT (k) DO UPDATE SET v=excluded.v"),
                      {"k": key, "v": json.dumps(value)})

    def drop(self, key: str) -> None:
        if not self.ok:
            return
        from sqlalchemy import text

        with self.engine.begin() as c:
            c.execute(text("DELETE FROM hz_meta WHERE k=:k"), {"k": key})


HOLD_KEY = "maintenance:hold"  # read by GrantStore.schedule_hold


def backup(hub_dir: Path, out: Path, *, include_secrets: bool = False, wait: float = 600,
           actor: str = "cli", poll: float = 2.0, settle: float = _SETTLE, say=log.info) -> dict:
    """Write one archive of the deployment containing `hub_dir` to `out`.
    Reads the databases, never migrates them."""
    p = plan(hub_dir, include_secrets=include_secrets)
    out = Path(out).resolve()
    if out.exists():
        raise BackupError(f"{out} already exists")
    stores = [_Store(url) for url in p.stores if _sqlite_path(url) is None or _sqlite_path(url).exists()]
    try:
        for s in stores:
            s.put(HOLD_KEY, {"reason": "backup", "by": actor, "until": time.time() + wait + _HOLD_MARGIN})
        say("New scheduled runs are held. Chat keeps working.")
        unknown: list[str] = []
        deadline = time.monotonic() + wait
        # A scheduler that checked the hold just before it was set may still be
        # queuing a run. Give it time, so the look below sees that run and waits
        # for it. --wait 0 takes the backup at once.
        if wait > 0 and settle > 0:
            time.sleep(min(settle, wait))
        busy = running_runs(p, unknown)
        while busy and time.monotonic() < deadline:
            say(f"Waiting for {len(busy)} scheduled run(s) to finish")
            time.sleep(poll)
            busy = running_runs(p, unknown)
        if busy and wait > 0:
            raise BackupError(
                "Scheduled runs are still in progress: " + ", ".join(busy[:10])
                + ". Try again later, cancel them with `hubzoid schedule cancel`,"
                " or pass --wait 0 to take the backup anyway.")
        if unknown:
            say("Run history could not be read for: " + ", ".join(unknown)
                + ". If those hubs are running scheduled work, stop them first.")
        try:
            index = _write_archive(p, out, include_secrets)
            checked = verify(out)
            if not checked["ok"]:
                out.unlink(missing_ok=True)
                raise BackupError("The backup did not pass verification and was deleted: "
                                  + "; ".join(checked["problems"][:5]))
        except BaseException as exc:
            for s in stores:  # read by `hubzoid doctor` (backup.age)
                try:
                    s.put("backup:failed", {"at": time.time(), "by": actor, "error": str(exc)[:500]})
                except Exception:  # noqa: BLE001 — the failure itself is what is raised
                    log.warning("backup: could not record the failure", exc_info=True)
            raise
        index["verified"] = checked
        for s in stores:  # read by `hubzoid doctor` (backup.age)
            s.put("backup:last", {"at": time.time(), "path": str(out), "by": actor, "verified": True,
                                  "bytes": out.stat().st_size, "files": checked["files"]})
    finally:
        for s in stores:
            try:
                s.drop(HOLD_KEY)
            except Exception:  # noqa: BLE001 — the hold also expires on its own
                log.warning("backup: could not clear the schedule hold", exc_info=True)
            s.engine.dispose()
        say("Scheduled runs resumed.")
    return index


def _write_archive(p: Plan, out: Path, include_secrets: bool) -> dict:
    from . import __version__

    part = out.with_name(out.name + ".part")
    exclude = {out, part}  # never the archive itself, wherever it is written
    index = {
        "format": FORMAT,
        "hubzoid": __version__,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "hubs": [{"key": k, "path": str(v)} for k, v in p.hubs],
        "roots": [r.to_json() for r in p.roots],
        "sqlite": [],
        "not_included": p.external,
        "secrets": include_secrets,
        "redacted": [],  # files saved with their database passwords as ***
        "operational": [],  # the SQLite copies of an operational store
        "postgres": [],  # {"arc", "url" (redacted), "roles"}: pg_dump archives
        "files": {},  # arc -> sha256, checked by verify()
    }
    # Chat UI databases hold password hashes and connection keys: owner-only.
    private = lambda path, flags: os.open(path, flags, 0o600)  # noqa: E731
    try:
        with tempfile.TemporaryDirectory(prefix="hubzoid-backup-") as tmp, \
                open(part, "wb", opener=private) as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
            for root in p.roots:
                for path, rel in _files(root, include_secrets, exclude, p.exclude_dirs):
                    arc = f"{root.id}/{rel.as_posix()}"
                    if _is_sqlite(path):
                        copy = Path(tmp) / f"{len(index['sqlite'])}.db"
                        _snapshot(path, copy, drop_hold=path.resolve() in p.operational_paths)
                        index["files"][arc] = _sha256(copy)
                        tar.add(copy, arcname=arc)
                        copy.unlink()
                        index["sqlite"].append(arc)
                        if path.resolve() in p.operational_paths:
                            index["operational"].append(arc)
                    elif path.name == "deployment.json" and not include_secrets \
                            and (clean := _without_passwords(path)) is not None:
                        info = tar.gettarinfo(path, arcname=arc)
                        info.size = len(clean)
                        index["files"][arc] = hashlib.sha256(clean).hexdigest()
                        tar.addfile(info, io.BytesIO(clean))
                        index["redacted"].append(arc)
                    else:
                        if path.is_file():
                            index["files"][arc] = _sha256(path)
                        tar.add(path, arcname=arc, recursive=False)
            for n, (url, roles) in enumerate(p.postgres.items()):
                arc = f"postgres/{n}.dump"
                dump = Path(tmp) / f"pg{n}.dump"
                _pg_dump(url, dump)
                index["files"][arc] = _sha256(dump)
                tar.add(dump, arcname=arc)
                dump.unlink()
                index["postgres"].append({"arc": arc, "url": _redact(url), "roles": roles})
            index["not_included"] = [u for u in index["not_included"]
                                     if u not in {d["url"] for d in index["postgres"]}]
            data = json.dumps(index, indent=2).encode()
            info = tarfile.TarInfo(INDEX)
            info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
            tar.addfile(info, io.BytesIO(data))
        os.replace(part, out)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return index


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------- PostgreSQL


def _pg_tool(name: str) -> str:
    """pg_dump or pg_restore: from HUBZOID_PG_BIN, else PATH. Its major version
    must be the server's or newer (pg_dump refuses a newer server)."""
    folder = os.environ.get("HUBZOID_PG_BIN", "").strip()
    found = str(Path(folder) / name) if folder else shutil.which(name)
    if not found or not Path(found).is_file():
        raise BackupError(f"{name} is not installed. Install the PostgreSQL client tools (the same major "
                          "version as the server or newer), or set HUBZOID_PG_BIN to their folder.")
    return found


def _pg_connection(url: str) -> tuple[list[str], dict]:
    """Connection flags and environment for a PostgreSQL URL. The password
    goes in PGPASSWORD, never on a command line another process can read."""
    from sqlalchemy.engine import make_url

    u = make_url(url)
    args = ["--host", u.host or "localhost", "--port", str(u.port or 5432),
            "--username", u.username or "postgres", "--dbname", u.database or "postgres"]
    env = {**os.environ, "PGCONNECT_TIMEOUT": "15"}
    password = u.password or u.query.get("password")
    if password:
        env["PGPASSWORD"] = str(password)
    if u.query.get("sslmode"):
        env["PGSSLMODE"] = str(u.query["sslmode"])
    return args, env


def _pg_run(cmd: list[str], env: dict, what: str, timeout: float = 3600) -> subprocess.CompletedProcess:
    try:
        done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BackupError(f"{what} did not finish within {int(timeout)} s") from None
    if done.returncode != 0:
        # pg tools print connection errors and SQL, never the password.
        raise BackupError(f"{what} failed: {(done.stderr or done.stdout).strip()[:400]}")
    return done


def _pg_dump(url: str, dest: Path) -> None:
    args, env = _pg_connection(url)
    _pg_run([_pg_tool("pg_dump"), "--format=custom", "--no-owner", "--no-privileges",
             "--file", str(dest), *args], env, f"pg_dump of {_redact(url)}")


def _pg_restore(dump: Path, url: str) -> None:
    """Load a dump into the database at ``url``, replacing what it holds, in
    one transaction: a failure leaves the database as it was."""
    args, env = _pg_connection(url)
    _pg_run([_pg_tool("pg_restore"), "--clean", "--if-exists", "--no-owner", "--no-privileges",
             "--single-transaction", "--exit-on-error", *args, str(dump)], env,
            f"pg_restore into {_redact(url)}")


# ------------------------------------------------------------------ verify


def verify(archive: Path) -> dict:
    """Read an archive back and check it: every file's checksum, an SQLite
    integrity check of each database and a listing of each PostgreSQL dump.
    Never raises for a bad archive; ``ok`` is False and ``problems`` says why."""
    archive = Path(archive)
    out = {"ok": False, "archive": str(archive), "files": 0, "databases": 0, "postgres": 0,
           "problems": [], "warnings": []}
    try:
        index = read_index(archive)
    except (BackupError, OSError, tarfile.TarError, EOFError) as exc:
        out["problems"].append(str(exc))
        return out
    sums = index.get("files")
    if not isinstance(sums, dict):
        out["warnings"].append("This archive was made by an earlier release and has no checksums; "
                               "only its databases are checked.")
        sums = None
    sqlite_arcs = set(index["sqlite"])
    dumps = {d["arc"] for d in index.get("postgres", [])}
    seen: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="hubzoid-verify-") as tmp, tarfile.open(archive, "r:gz") as tar:
        try:
            for member in tar:
                if not member.isfile() or member.name == INDEX:
                    continue
                keep = member.name in sqlite_arcs or member.name in dumps
                copy = Path(tmp) / f"{len(seen)}.bin" if keep else None
                digest = hashlib.sha256()
                stream = tar.extractfile(member)
                with (copy.open("wb") if copy else io.BytesIO()) as sink:
                    while chunk := stream.read(_CHUNK):
                        digest.update(chunk)
                        if copy:
                            sink.write(chunk)
                seen.add(member.name)
                out["files"] += 1
                if sums is not None and sums.get(member.name) != digest.hexdigest():
                    out["problems"].append(f"{member.name}: " + (
                        "not in the checksum list" if member.name not in sums else "checksum does not match"))
                if member.name in sqlite_arcs:
                    out["databases"] += 1
                    problem = _sqlite_problem(copy)
                    if problem:
                        out["problems"].append(f"{member.name}: {problem}")
                elif member.name in dumps:
                    out["postgres"] += 1
                    try:
                        _pg_run([_pg_tool("pg_restore"), "--list", str(copy)], dict(os.environ),
                                f"pg_restore --list of {member.name}", timeout=600)
                    except BackupError as exc:
                        out["problems" if "not installed" not in str(exc) else "warnings"].append(str(exc))
                if copy:
                    copy.unlink()
        except (tarfile.TarError, EOFError, OSError, zlib_error) as exc:
            out["problems"].append(f"The archive cannot be read to the end: {exc}")
    if sums is not None:
        out["problems"] += [f"{arc}: missing from the archive" for arc in sorted(set(sums) - seen)]
    out["ok"] = not out["problems"]
    return out


def _sqlite_problem(path: Path) -> str | None:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return f"not a readable database ({exc})"
    result = "; ".join(str(r[0]) for r in rows[:3])
    return None if result == "ok" else f"integrity check: {result}"


# --------------------------------------------------------------- rehearse


def rehearse(archive: Path, *, postgres_url: str | None = None, say=log.info) -> dict:
    """Restore an archive into a scratch folder (and ``postgres_url``, a
    scratch database that is overwritten), upgrade every copy of an
    operational store to this release, and report what came back. The
    deployment itself is never touched; the scratch folder is removed."""
    from sqlalchemy import create_engine, inspect, text

    from . import db, migrations

    started = time.monotonic()
    archive = Path(archive).resolve()
    report = {"ok": False, "archive": str(archive), "verify": verify(archive), "databases": [],
              "postgres": [], "problems": []}
    if not report["verify"]["ok"]:
        report["problems"] = ["verification failed: " + "; ".join(report["verify"]["problems"][:5])]
        return report
    index = read_index(archive)
    with tempfile.TemporaryDirectory(prefix="hubzoid-rehearsal-") as tmp:
        moves = [(r["path"], str(Path(tmp) / r["id"] / Path(r["path"]).name)) for r in index["roots"]]
        restore(archive, moves, say=lambda _m: None)
        _, move_text = _mover(moves)
        operational = set(index.get("operational", []))
        for arc in index["sqlite"]:
            rid, rel = arc.split("/", 1)
            root = next(r for r in index["roots"] if r["id"] == rid)
            base = Path(move_text(root["path"]))
            path = base if root["kind"] == "file" else base / rel
            entry = {"database": arc, "path_was": str(Path(root["path"]) / rel) if root["kind"] != "file"
                     else root["path"], "problems": []}
            problem = _sqlite_problem(path)
            if problem:
                entry["problems"].append(problem)
            elif arc in operational:
                entry.update(_upgrade_and_count(create_engine(db.sqlalchemy_url(f"sqlite:///{path}")),
                                                migrations, inspect, text))
            report["databases"].append(entry)
        dumps = index.get("postgres", [])
        if dumps and not postgres_url:
            report["postgres"] = [{"dump": d["arc"], "skipped": "no scratch database given"} for d in dumps]
        for d in dumps if postgres_url else []:
            entry = {"dump": d["arc"], "from": d["url"], "problems": []}
            dump = Path(tmp) / "pg.dump"
            with tarfile.open(archive, "r:gz") as tar:
                dump.write_bytes(tar.extractfile(d["arc"]).read())
            try:
                _pg_restore(dump, postgres_url)
                if "operational" in d.get("roles", []):
                    entry.update(_upgrade_and_count(create_engine(db.sqlalchemy_url(postgres_url)),
                                                    migrations, inspect, text))
            except BackupError as exc:
                entry["problems"].append(str(exc))
            finally:
                dump.unlink(missing_ok=True)
            report["postgres"].append(entry)
    for entry in report["databases"] + report["postgres"]:
        report["problems"] += [f"{entry.get('database') or entry.get('dump')}: {p}" for p in entry.get("problems", [])]
    report["seconds"] = round(time.monotonic() - started, 1)
    report["ok"] = not report["problems"]
    say("Rehearsal " + ("passed" if report["ok"] else "failed"))
    return report


def _upgrade_and_count(engine, migrations, inspect, text) -> dict:
    """Upgrade a restored operational store to this release and count what it
    holds: people, conversations and messages."""
    out: dict = {"problems": []}
    try:
        migrations.upgrade(engine, "operational")
        out["upgraded"] = True
        names = set(inspect(engine).get_table_names())
        with engine.connect() as c:
            out["rows"] = {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one()
                           for t in ("hz_users", "hz_conversations", "hz_messages") if t in names}
    except Exception as exc:  # noqa: BLE001 — a store that will not upgrade is the finding
        out["problems"].append(f"does not upgrade to this release: {type(exc).__name__}: {str(exc)[:300]}")
    finally:
        engine.dispose()
    return out


# --------------------------------------------------------------- restore


def _mover(moves: list[tuple[str, str]]):
    pairs = [(str(Path(a)).rstrip("/") or "/", str(Path(b)).rstrip("/") or "/") for a, b in moves]

    def move_path(path: str) -> str:
        for old, new in pairs:
            if path == old or path.startswith(old + "/"):
                return new + path[len(old):]
        return path

    def move_text(value: str) -> str:
        for old, new in pairs:
            value = re.sub(re.escape(old) + r"(?=/|$|\")", lambda _m, n=new: n, value)
        return value

    return move_path, move_text


def read_index(archive: Path) -> dict:
    with tarfile.open(archive, "r:gz") as tar:
        try:
            member = tar.getmember(INDEX)
        except KeyError:
            raise BackupError(f"{archive} is not a Hubzoid backup") from None
        try:
            index = json.loads(tar.extractfile(member).read())
        except ValueError:
            raise BackupError(f"{archive} has an unreadable index") from None
        found = index.get("format") if isinstance(index, dict) else None
        if found != FORMAT:
            raise BackupError(f"Unsupported backup format: {found}")
        _check_index(index, tar)
    return index


def _check_index(index: dict, tar: tarfile.TarFile) -> None:
    """Restore replaces whatever is at each saved path. Refuse an index naming
    a location `plan()` never saves, before anything is touched."""
    def refuse(why: str):
        raise BackupError(f"Refusing this archive: {why}")

    members = {m.name: m for m in tar.getmembers()}
    for name, m in members.items():
        if Path(name).is_absolute() or ".." in Path(name).parts or not (m.isfile() or m.isdir()):
            refuse(f"unsafe entry {name}")
    roots, sqlite, redacted = index.get("roots"), index.get("sqlite"), index.get("redacted", [])
    if not (isinstance(roots, list) and roots and isinstance(sqlite, list)
            and isinstance(redacted, list)):
        refuse("its index is malformed")
    ids: set[str] = set()
    for r in roots:
        if not (isinstance(r, dict) and all(isinstance(r.get(k), str) for k in ("id", "path", "kind"))):
            refuse("a saved location is malformed")
        rid, path, kind = r["id"], Path(r["path"]), r["kind"]
        if not _ROOT_ID.fullmatch(rid) or rid in ids:
            refuse(f"the location id {rid!r} is invalid or repeated")
        ids.add(rid)
        if not path.is_absolute() or ".." in path.parts:
            refuse(f"{r['path']} is not an absolute path")
        saved = {n.split("/", 1)[1] for n in members if n.startswith(rid + "/")}
        if kind == "state":
            ok = path.name in STATE_DIRS
        elif kind == "ui":  # a hub's chat UI data, or a gateway's data directory
            ok = path.name == UI_DIR or bool(saved & {"deployment.json", "webui.db"})
        elif kind == "file":
            own = members.get(f"{rid}/{path.name}")
            ok = path.name == ".env" or (
                f"{rid}/{path.name}" in sqlite and own is not None and own.isfile()
                and tar.extractfile(own).read(16) == _SQLITE_MAGIC)
        else:
            ok = False
        if not ok:
            refuse(f"{path} is not a location hubzoid backup saves")
    dumps = index.get("postgres", [])
    if not isinstance(dumps, list) or not all(
            isinstance(d, dict) and isinstance(d.get("arc"), str) and _PG_ARC.fullmatch(d["arc"])
            and d["arc"] in members for d in dumps):
        refuse("its PostgreSQL entries are malformed")
    listed = {d["arc"] for d in dumps}
    for name in members:
        if name in listed or name == "postgres":
            continue
        if name != INDEX and name.split("/", 1)[0] not in ids:
            refuse(f"the entry {name} belongs to no saved location")
    for arc in sqlite + redacted:
        if not isinstance(arc, str) or arc not in members:
            refuse(f"the index names {arc!r}, which the archive does not hold")


def _targets(index: dict, moves: list[tuple[str, str]]) -> dict[str, Path]:
    """Where each saved root goes. None may be the filesystem root or the home
    directory, and none may be inside another."""
    move_path, _ = _mover(moves)
    targets = {r["id"]: Path(move_path(r["path"])) for r in index["roots"]}
    real = {rid: Path(os.path.realpath(t)) for rid, t in targets.items()}
    home = Path(os.path.realpath(Path.home()))
    for rid, t in real.items():
        if t == Path(t.anchor) or t == home:
            raise BackupError(f"Refusing to restore over {targets[rid]}")
        for other, o in real.items():
            if other != rid and (o == t or o in t.parents):
                raise BackupError(f"Refusing to restore {targets[rid]}: it overlaps {targets[other]}")
    return targets


def restore_plan(archive: Path, moves: list[tuple[str, str]] = ()) -> list[tuple[dict, Path]]:
    """Each saved root and where it would be restored."""
    index = read_index(archive)
    targets = _targets(index, list(moves))
    return [(r, targets[r["id"]]) for r in index["roots"]]


def _in_use(db_file: Path) -> bool:
    """A running process holds a lock or a WAL beside the database."""
    if Path(str(db_file) + "-wal").exists() or Path(str(db_file) + "-shm").exists():
        return True
    try:
        conn = sqlite3.connect(db_file, timeout=0.2)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return True
    return False


def _safe_members(tar: tarfile.TarFile):
    for m in tar.getmembers():
        name = Path(m.name)
        if name.is_absolute() or ".." in name.parts or not (m.isfile() or m.isdir()):
            raise BackupError(f"Refusing unsafe archive entry: {m.name}")
        yield m


def restore(archive: Path, moves: list[tuple[str, str]] = (), *, postgres_url: str | None = None,
            say=log.info) -> dict:
    """Put a backup back. Stop the hub or gateway first. With ``postgres_url``,
    the archive's PostgreSQL dump also replaces what that database holds."""
    archive = Path(archive).resolve()
    index = read_index(archive)
    dumps = index.get("postgres", [])
    if postgres_url and len(dumps) != 1:
        raise BackupError("This archive holds no PostgreSQL database." if not dumps else
                          f"This archive holds {len(dumps)} PostgreSQL databases. Load each one with "
                          "pg_restore (see docs/BACKUP.md).")
    moves = list(moves)
    _, move_text = _mover(moves)
    targets = _targets(index, moves)

    for arc in index["sqlite"]:
        rid, rel = arc.split("/", 1)
        root = next(r for r in index["roots"] if r["id"] == rid)
        target = targets[rid] if root["kind"] == "file" else targets[rid] / rel
        if target.exists() and _in_use(target):
            raise BackupError(
                f"{target} may be in use: a lock or SQLite journal remains. "
                "Stop the hub or gateway before restoring. If all writers are "
                "already stopped, preserve the database and its sidecars, then "
                "checkpoint it with SQLite as described in the upgrade guide. "
                "Do not delete journal files to force a restore."
            )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    first_parent = next(iter(targets.values())).parent
    first_parent.mkdir(parents=True, exist_ok=True)
    kept: list[str] = []
    with tempfile.TemporaryDirectory(prefix=".hubzoid-restore-", dir=first_parent) as tmp:
        with tarfile.open(archive, "r:gz") as tar:
            safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tar.extractall(tmp, members=list(_safe_members(tar)), **safe)
        for root in index["roots"]:
            src, target = Path(tmp) / root["id"], targets[root["id"]]
            if root["kind"] == "file":
                src = src / Path(root["path"]).name
            if not src.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            aside = None
            if target.exists():
                aside = target.with_name(f"{target.name}.pre-restore-{stamp}")
                os.replace(target, aside)
                kept.append(str(aside))
                for side in ("-wal", "-shm", "-journal"):
                    stale = Path(str(target) + side)
                    if stale.exists():
                        os.replace(stale, Path(str(aside) + side))
            shutil.move(str(src), str(target))
            if aside is not None and root["kind"] != "file":
                _keep_secrets(aside, target, Root(root["id"], Path(root["path"]), root["kind"]))
            say(f"Restored {target}")

    if any(a != b for a, b in moves):
        _rewrite_paths(index, targets, move_text)
    restored_pg = []
    if postgres_url:
        with tempfile.TemporaryDirectory(prefix="hubzoid-restore-pg-") as tmp, tarfile.open(archive, "r:gz") as tar:
            dump = Path(tmp) / "pg.dump"
            dump.write_bytes(tar.extractfile(dumps[0]["arc"]).read())
            _pg_restore(dump, postgres_url)
        restored_pg.append(_redact(postgres_url))
        say(f"Restored the PostgreSQL database into {_redact(postgres_url)}")
    redacted = [targets[arc.split("/", 1)[0]] / arc.split("/", 1)[1]
                for arc in index.get("redacted", [])]
    return {"restored": [str(t) for t in targets.values()], "kept": kept,
            "not_included": index.get("not_included", []),
            "postgres": [d["url"] for d in dumps], "postgres_restored": restored_pg,
            "redacted": [str(p) for p in redacted if p.is_file()]}


def _keep_secrets(previous: Path, restored: Path, root: Root) -> None:
    """Copy credential files the archive did not hold (a backup made without
    secrets) from the directory the restore replaced. Restoring data must not
    swap the deployment key or the artifact link secret for new ones. Only the
    credential locations (`_credential`): a chat attachment of the same name
    is data, and is not carried over."""
    if not previous.is_dir():
        return
    for path in previous.iterdir():
        if not _credential(Path(path.name), root) or path.is_symlink() or not path.is_file():
            continue
        dest = restored / path.name
        if not dest.exists():
            shutil.copy2(path, dest)


def _rewrite_paths(index: dict, targets: dict[str, Path], move_text) -> None:
    """Absolute paths that Hubzoid and Open WebUI store, after a move."""
    for root in index["roots"]:
        target = targets[root["id"]]
        if root["kind"] == "file" or not target.is_dir():
            continue
        for name in ("deployment.json", "schedule-state.json"):
            for path in target.rglob(name):
                text = path.read_text()
                moved = move_text(text)
                if moved != text:
                    json.loads(moved)
                    path.write_text(moved)
        if root["kind"] == "ui" and (target / "webui.db").is_file():
            conn = sqlite3.connect(target / "webui.db")
            try:
                rows = conn.execute("SELECT id, path FROM file WHERE path IS NOT NULL").fetchall()
                for fid, path in rows:
                    moved = move_text(path)
                    if moved != path:
                        conn.execute("UPDATE file SET path=? WHERE id=?", (moved, fid))
                conn.commit()
            except sqlite3.OperationalError:
                log.warning("restore: could not update Open WebUI file paths", exc_info=True)
            finally:
                conn.close()
