# Hubzoid. MIT licensed like the rest of the repository.
"""`hubzoid backup` and `hubzoid restore`: one archive of a deployment's state.

The archive holds every SQLite database (copied with SQLite's online backup, so
a live deployment gives a consistent copy), the chat UI data (Open WebUI's
`webui.db`, uploads and vector store, not its model cache) and each hub's
`.hubzoid`, `.inbound`, `logs` and `output`. A hub inside a gateway is backed up
with the whole gateway, since they share one operational database.

Not in the archive:
  - hub content (AGENTS.md, skills, knowledge, tools). It belongs in the hub's
    git repository.
  - PostgreSQL databases. The backup names them; docs/BACKUP.md has the
    pg_dump route.
  - secrets (`.env`, `.hubzoid/artifact_secret`, `.webui_secret_key`) and the
    database passwords in the gateway's `deployment.json` (saved as `***`),
    unless asked for with `include_secrets`.

A backup holds new scheduled runs and waits for running ones to finish. Chat
keeps working throughout. Due runs fire when the hold ends.

Restore puts each saved directory back where it was, or under a new prefix
(`moves`), and rewrites the absolute paths Hubzoid and Open WebUI store.
Whatever was at a target is kept beside it as `<name>.pre-restore-<stamp>`.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

FORMAT = 1
INDEX = "hubzoid-backup.json"
STATE_DIRS = (".hubzoid", ".inbound", "logs", "output")
UI_DIR = ".openwebui-data"
SECRET_FILES = {".env", "artifact_secret", ".webui_secret_key", ".admin_token"}
_SKIP_SUFFIXES = ("-wal", "-shm", "-journal", ".tmp", ".part")
_SQLITE_MAGIC = b"SQLite format 3\x00"
# Enough to cover a long copy. The hold is cleared when the backup ends, and
# expires on its own if the backup process dies.
_HOLD_MARGIN = 3 * 3600
# Seconds between setting the hold and the first look for running work, so a
# scheduler that passed its hold check just before is seen queuing its run.
_SETTLE = 5.0
_ROOT_ID = re.compile(r"r\d+")


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
    sqlite_paths: set[Path] = field(default_factory=set)
    operational_paths: set[Path] = field(default_factory=set)


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
        for name in STATE_DIRS:
            if (path / name).is_dir():
                add(path / name, "state", key)
        if (path / UI_DIR).is_dir():
            add(path / UI_DIR, "ui", key)
        if include_secrets and (path / ".env").is_file():
            add(path / ".env", "file", key)
    if manifest:
        gw_data = Path(manifest["owui_db"]).resolve().parent
        if any(gw_data == h or gw_data in h.parents for _, h in hubs):
            raise BackupError(f"The gateway data directory {gw_data} contains a hub. "
                              "Give the gateway its own --data-dir to use hubzoid backup.")
        if gw_data.is_dir():
            add(gw_data, "ui")

    out = Plan(hubs=hubs, roots=roots, stores=[], dbos=[])
    urls: list[tuple[str, str]] = []
    for key, path in hubs:
        op = db.operational_url(path)
        if op not in out.stores:
            out.stores.append(op)
        urls += [(key, op), (key, db.resolve_url(path))]
        dbos = db.dbos_url(path)
        out.dbos.append((path.name, dbos))
        urls.append((key, dbos))
    for key, url in urls:
        p = _sqlite_path(url)
        if p is None:
            if _redact(url) not in out.external:
                out.external.append(_redact(url))
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


def _skip(rel: Path, root: Root, include_secrets: bool) -> bool:
    name = rel.name
    if name.endswith(_SKIP_SUFFIXES) or name.startswith(".hubzoid-restore-"):
        return True
    if name in SECRET_FILES and not include_secrets:
        return True
    parts = rel.parts
    if "__pycache__" in parts:
        return True
    # Open WebUI's model cache is large and rebuilt on demand.
    return root.kind == "ui" and parts[:1] == ("cache",)


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


def _files(root: Root, include_secrets: bool, exclude: set[Path]):
    if root.kind == "file":
        yield root.path, Path(root.path.name)
        return
    for dirpath, dirnames, filenames in os.walk(root.path):
        base = Path(dirpath)
        dirnames[:] = [d for d in dirnames
                       if not (base / d).is_symlink()
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

        self.engine = create_engine(url)
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
        index = _write_archive(p, out, include_secrets)
        for s in stores:  # read by `hubzoid doctor` (backup.age)
            s.put("backup:last", {"at": time.time(), "path": str(out), "by": actor})
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

    exclude = {out}
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
    }
    part = out.with_name(out.name + ".part")
    # Chat UI databases hold password hashes and connection keys: owner-only.
    private = lambda path, flags: os.open(path, flags, 0o600)  # noqa: E731
    try:
        with tempfile.TemporaryDirectory(prefix="hubzoid-backup-") as tmp, \
                open(part, "wb", opener=private) as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
            for root in p.roots:
                for path, rel in _files(root, include_secrets, exclude):
                    arc = f"{root.id}/{rel.as_posix()}"
                    if _is_sqlite(path):
                        copy = Path(tmp) / f"{len(index['sqlite'])}.db"
                        _snapshot(path, copy, drop_hold=path.resolve() in p.operational_paths)
                        tar.add(copy, arcname=arc)
                        copy.unlink()
                        index["sqlite"].append(arc)
                    elif path.name == "deployment.json" and not include_secrets \
                            and (clean := _without_passwords(path)) is not None:
                        info = tar.gettarinfo(path, arcname=arc)
                        info.size = len(clean)
                        tar.addfile(info, io.BytesIO(clean))
                        index["redacted"].append(arc)
                    else:
                        tar.add(path, arcname=arc, recursive=False)
            data = json.dumps(index, indent=2).encode()
            info = tarfile.TarInfo(INDEX)
            info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
            tar.addfile(info, io.BytesIO(data))
        os.replace(part, out)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return index


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
    for name in members:
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


def restore(archive: Path, moves: list[tuple[str, str]] = (), *, say=log.info) -> dict:
    """Put a backup back. Stop the hub or gateway first."""
    archive = Path(archive).resolve()
    index = read_index(archive)
    moves = list(moves)
    _, move_text = _mover(moves)
    targets = _targets(index, moves)

    for arc in index["sqlite"]:
        rid, rel = arc.split("/", 1)
        root = next(r for r in index["roots"] if r["id"] == rid)
        target = targets[rid] if root["kind"] == "file" else targets[rid] / rel
        if target.exists() and _in_use(target):
            raise BackupError(f"{target} is in use. Stop the hub or gateway, then restore.")

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
            if target.exists():
                aside = target.with_name(f"{target.name}.pre-restore-{stamp}")
                os.replace(target, aside)
                kept.append(str(aside))
                for side in ("-wal", "-shm", "-journal"):
                    stale = Path(str(target) + side)
                    if stale.exists():
                        os.replace(stale, Path(str(aside) + side))
            shutil.move(str(src), str(target))
            say(f"Restored {target}")

    if any(a != b for a, b in moves):
        _rewrite_paths(index, targets, move_text)
    redacted = [targets[arc.split("/", 1)[0]] / arc.split("/", 1)[1]
                for arc in index.get("redacted", [])]
    return {"restored": [str(t) for t in targets.values()], "kept": kept,
            "not_included": index.get("not_included", []),
            "redacted": [str(p) for p in redacted if p.is_file()]}


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
