"""Administrator writes to the same connector files operators edit by hand.

The deployment lock serializes UI writes across bridges. Atomic replacement
keeps readers from seeing partial YAML. Revisions reject stale browser forms;
an additional content check catches manual edits made while preparing a save.
Secrets use fresh env names on each replacement, never overwriting a reference
that another connector may use or a value inherited by a running process.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import text

from .. import _file_lock
from ..control import yamlfile
from . import ConnectorError, engine, registry as r

log = logging.getLogger(__name__)

# Names Windows reserves for devices: con.yaml cannot be created there.
_WINDOWS_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                               *(f"lpt{i}" for i in range(1, 10))})


def _linked(path: Path) -> bool:
    """A symbolic link, or a Windows junction (a reparse point is_symlink misses)."""
    if path.is_symlink():
        return True
    try:
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _safe(path: Path) -> None:
    if _linked(path):
        raise ConnectorError("unsafe_path", "Settings cannot write through a symbolic link. "
                             "Use a regular connector folder, YAML file and .env file.", 409)


@contextmanager
def _locked(hub_dir):
    base = r.folder(hub_dir)
    try:
        _safe(base)
        base.mkdir(parents=True, exist_ok=True)
        lock = base / ".settings.lock"
        _safe(lock)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "a+b") as handle:
            _file_lock.lock(handle)
            try:
                yield base
            finally:
                _file_lock.unlock(handle)
    except OSError:
        raise ConnectorError("file_write_failed", "Could not save connector files. Check the "
                             "deployment folder permissions and available disk space.", 500) from None


def _paths(base: Path, cid: str) -> list[Path]:
    return [p for p in base.iterdir()
            if p.suffix in (".yaml", ".yml") and p.stem.lower() == cid]


def _read(base: Path, cid: str):
    paths = _paths(base, cid)
    if not paths:
        raise ConnectorError("not_found", "This connector doesn't exist.", 404)
    if len(paths) > 1:
        raise ConnectorError("conflict", "More than one file defines this connector. "
                             "Keep one definition before editing it in Settings.", 409)
    path = paths[0]
    _safe(path)
    raw = path.read_text(encoding="utf-8")
    try:
        doc = yamlfile.loads(raw).data
    except yamlfile.YamlError:
        raise ConnectorError("invalid_file", "Repair the connector YAML file before editing "
                             "it in Settings.", 422) from None
    definition = r.parse_document(path, doc)
    return path, raw, doc, definition.connector


def _check_revision(raw: str, expected: str | None) -> None:
    if expected is not None and expected != hashlib.sha256(raw.encode()).hexdigest():
        raise ConnectorError("conflict", "This connector changed since you opened it. "
                             "Refresh the page and reopen it before saving.", 409)


_ANY = object()  # restoring a file: no content check


def _atomic(path: Path, value: str, *, previous) -> None:
    _safe(path)
    fd, name = tempfile.mkstemp(prefix=".connector-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(value)
            out.flush()
            os.fsync(out.fileno())
        for attempt in range(10):
            # Catch a manual edit made while this save was being prepared,
            # checked as late as possible: just before the file is replaced.
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if previous is not _ANY and current != previous:
                raise ConnectorError("conflict", "The file changed during this save. Refresh and "
                                     "try again.", 409)
            # mkstemp's 0600 also protects an existing, previously permissive .env.
            _safe(path)
            try:
                os.replace(name, path)
                return
            except PermissionError:
                # Windows refuses to replace a file another process is reading
                # for a moment (a bridge loading it); try again shortly.
                if os.name != "nt" or attempt == 9:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def _undo_on_failure(path: Path, *, previous: str | None, written: str | None):
    """When the database part of a change fails, put the definition file back
    as it was: the API reports a failure, so the change must not stay active.
    Only a file still holding what this save wrote (``written``; None when it
    was deleted) is restored, so a manual edit made meanwhile is kept."""
    try:
        yield
    except BaseException:
        try:
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current == written:
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic(path, previous, previous=_ANY)
        except (OSError, ConnectorError):
            log.error("connectors: could not restore %s after a failed save", path.name)
        r._seen.pop(str(path), None)
        raise


def _prepare(path: Path, doc: dict, data: dict) -> tuple[dict, dict[str, str]]:
    """Merge known input fields; validate everything before writing either file."""
    doc = dict(doc)
    secrets: dict[str, str] = {}
    if ("client_id" in data and "client_secret" not in data
            and (data["client_id"] or None) != (doc.get("client_id") or None)):
        # A secret belongs to its client ID.
        doc.pop("client_secret", None)
    for source, dest in (("name", "name"), ("url", "url"), ("auth_type", "sign_in"),
                         ("client_id", "client_id"), ("scopes", "scopes"),
                         ("tool_allowlist", "tools"), ("enabled", "enabled"),
                         ("shared_header", "shared_header")):
        if source in data:
            doc[dest] = data[source]
    auth = r._auth_type(doc.get("sign_in", "oauth"))
    for source, dest, used in (("client_secret", "client_secret", auth == "oauth"),
                                ("shared_secret", "shared_key", auth == "shared")):
        if not used:
            doc.pop(dest, None)
        elif source in data:
            value = r._optional_text(data[source], code=f"invalid_{source}",
                                     what=source, limit=r._SECRET_MAX)
            if not value:
                doc.pop(dest, None)
            else:
                if source == "shared_secret":
                    r._check_shared_key(value)
                key = f"HUBZOID_CONNECTOR_{path.stem.upper()}_{dest.upper()}_{uuid.uuid4().hex.upper()}"
                secrets[key] = value
                doc[dest] = f"env:{key}"
    if auth != "oauth":
        for key in ("client_id", "client_secret", "scopes"):
            doc.pop(key, None)
    if auth != "shared":
        for key in ("shared_header", "shared_key"):
            doc.pop(key, None)
    parsed = r.parse_document(path, doc).connector
    if parsed.has_client_secret and not parsed.client_id:
        raise ConnectorError("invalid_client_secret", "Enter the client ID that goes with "
                             "the client secret.", 422)
    # Persist normalized values rather than having files and the UI disagree.
    doc.update(name=parsed.name, url=parsed.url, sign_in=parsed.auth_type, enabled=parsed.enabled)
    return doc, secrets


def _save(path: Path, written: str, secrets: dict[str, str], *, previous: str | None) -> None:
    if secrets:
        env = path.parent.parent / ".env"
        _safe(env)
        old = env.read_text(encoding="utf-8") if env.exists() else None
        content = old or ""
        if content and not content.endswith("\n"):
            content += "\n"
        content += "# Connector secrets saved by Settings (values are never shown in the UI).\n"
        for key, value in secrets.items():
            quoted = value.replace("\\", "\\\\").replace("'", "\\'")
            content += f"{key}='{quoted}'\n"
        # Only values that read back exactly from the whole file are stored: an
        # unfinished quoted line earlier in .env would swallow them.
        parsed = dotenv_values(stream=io.StringIO(content), interpolate=False)
        if any(parsed.get(key) != value for key, value in secrets.items()):
            raise ConnectorError("invalid_secret", "The secret could not be saved in the deployment "
                                 ".env file. Check .env for an unfinished quoted value, and the "
                                 "secret for unusual characters.", 422)
        _atomic(env, content, previous=old)
    # If YAML fails after .env succeeded, only unreferenced new keys remain.
    # Existing definitions and their secrets are still intact.
    _atomic(path, written, previous=previous)
    r._seen.pop(str(path), None)


def create(hub_dir, data, *, actor: str, hub: str = "") -> r.Connector:
    data = r._body(data)
    name = r._name(data.get("name"))
    cid = r.check_id(data.get("id") or r.slugify(name))
    if cid in _WINDOWS_RESERVED:
        raise ConnectorError("invalid_id", f"{cid} is a name Windows reserves for devices. "
                             "Choose another ID.", 422)
    with _locked(hub_dir) as base:
        if _paths(base, cid):
            raise ConnectorError("exists", f"A connector with the ID {cid} already exists.", 409)
        path = base / f"{cid}.yaml"
        doc, secrets = _prepare(path, {"version": 1, "hubs": [r._hub(hub)] if hub else []}, data)
        written = yamlfile.dumps(doc)
        with _undo_on_failure(path, previous=None, written=written), engine(hub_dir).begin() as conn:
            _save(path, written, secrets, previous=None)
            r._audit(conn, hub_dir, actor, "connector_create", cid)
        return r.get(hub_dir, cid)


def update(hub_dir, cid: str, data, *, actor: str) -> tuple[r.Connector, bool]:
    cid = r.check_id(cid)
    if not isinstance(data, dict):
        r._body(data)
    data = dict(data)
    expected = data.pop("expected_revision", None)
    r._body(data)
    if "id" in data:
        raise ConnectorError("immutable_id", "A connector's ID cannot be changed.", 422)
    with _locked(hub_dir) as base:
        path, raw, doc, old = _read(base, cid)
        _check_revision(raw, expected)
        newdoc, secrets = _prepare(path, doc, data)
        new = r.parse_document(path, newdoc).connector
        reset = (new.url, new.auth_type) != (old.url, old.auth_type)
        if old.auth_type == new.auth_type == "shared" and old.url != new.url and not secrets:
            raise ConnectorError("shared_key_required", "A new server needs its own key. "
                                 "Enter the key again.", 422)
        # Nor is a stored client secret sent to a new server's sign-in.
        if (old.auth_type == new.auth_type == "oauth" and old.url != new.url
                and new.has_client_secret and "client_secret" not in data):
            raise ConnectorError("client_secret_required", "A new server needs its client "
                                 "secret again. Enter it, or remove the stored secret.", 422)
        written = yamlfile.dumps(newdoc)
        with _undo_on_failure(path, previous=raw, written=written), engine(hub_dir).begin() as conn:
            _save(path, written, secrets, previous=raw)
            # Tokens are removed after committing to avoid a second writer on SQLite.
            if reset:
                conn.execute(text("DELETE FROM hz_connector_flows WHERE connector_id = :id"), {"id": cid})
                conn.execute(text("DELETE FROM hz_connectors WHERE id = :id"), {"id": cid})
            r._audit(conn, hub_dir, actor, "connector_update", cid)
        if reset:
            from . import oauth_flow, tokens
            oauth_flow.forget_discovery(old.url)
            tokens.drop_connector(hub_dir, cid)
        return r.get(hub_dir, cid), reset


def delete(hub_dir, cid: str, *, actor: str, expected_revision: str | None = None) -> bool:
    cid = r.check_id(cid)
    with _locked(hub_dir) as base:
        if not _paths(base, cid):
            return False
        path, raw, _, _ = _read(base, cid)
        _check_revision(raw, expected_revision)
        with _undo_on_failure(path, previous=raw, written=None), engine(hub_dir).begin() as conn:
            # A manual edit since the file was read wins over this delete.
            if path.read_text(encoding="utf-8") != raw:
                raise ConnectorError("conflict", "The file changed during this delete. Refresh and "
                                     "try again.", 409)
            path.unlink()
            r._seen.pop(str(path), None)
            conn.execute(text("DELETE FROM hz_connector_flows WHERE connector_id = :id"), {"id": cid})
            conn.execute(text("DELETE FROM hz_connectors WHERE id = :id"), {"id": cid})
            r._audit(conn, hub_dir, actor, "connector_delete", cid)
        from . import tokens
        tokens.drop_connector(hub_dir, cid)
        return True


def offering(hub_dir, cid: str, hub, *, actor: str, offered: bool) -> bool:
    cid = r.check_id(cid)
    hub = r._hub(hub)
    if not hub or hub == "*":
        raise ConnectorError("invalid_agent", "Choose an agent.", 422)
    with _locked(hub_dir) as base:
        path, raw, doc, _ = _read(base, cid)
        hubs = list(r.parse_document(path, doc).hubs)
        if (hub in hubs) == offered:
            return False
        doc["hubs"] = sorted(set(hubs + [hub])) if offered else [h for h in hubs if h != hub]
        written = yamlfile.dumps(doc)
        with _undo_on_failure(path, previous=raw, written=written), engine(hub_dir).begin() as conn:
            _save(path, written, {}, previous=raw)
            from ..access import store_for
            store_for(Path(hub_dir)).write_audit(
                conn, r.normalize(actor) or "unknown",
                "connector_offer" if offered else "connector_withdraw",
                hub=hub, permission=r.capability(cid), surface="console")
        if not offered:
            gs = store_for(Path(hub_dir))
            for subject, granted_hub, permission in gs.list_grants(hub):
                if permission == r.capability(cid):
                    gs.revoke(subject, granted_hub, permission, actor=r.normalize(actor) or "unknown")
        return True
