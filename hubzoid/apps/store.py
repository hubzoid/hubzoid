"""Card revisions, resources and instances in the operational database.

The same tables and queries on SQLite and PostgreSQL (migration ``op_0054``).
A revision is applied with compare-and-set under the hub's write lock, so two
applies against one revision give one new revision and a conflict.
"""
from __future__ import annotations

import json
import time

from sqlalchemy import text

from ..work_items import store as wi_store
from ..work_items.errors import WorkItemError, not_found, revision_conflict

_CACHE_SECONDS = 3.0
_active_cache: dict[str, tuple[float, dict | None]] = {}


def engine(hub_dir):
    return wi_store.engine(hub_dir)


def active_row(hub_dir, hub: str, *, fresh: bool = False) -> dict | None:
    """The active revision ``{revision, digest, config, actor, created}`` or None.
    Cached for a few seconds: a tool call reads it on the hot path."""
    now = time.monotonic()
    hit = _active_cache.get(hub)
    if hit and not fresh and now - hit[0] < _CACHE_SECONDS:
        return hit[1]
    with engine(hub_dir).connect() as c:
        row = c.execute(text("SELECT revision, digest, config_json, actor, created FROM hz_app_config_revisions "
                             "WHERE hub=:h AND active=1"), {"h": hub}).mappings().first()
    out = None if row is None else {"revision": row["revision"], "digest": row["digest"],
                                     "config": json.loads(row["config_json"]), "actor": row["actor"],
                                     "created": row["created"]}
    _active_cache[hub] = (now, out)
    return out


def apply(hub_dir, hub: str, *, expected: int | None, digest: str, config: dict, resources: dict[str, tuple],
          actor: str) -> dict:
    """Make ``config`` the next active revision when the active one is still
    ``expected`` (None: no revision yet). ``resources``: {sha256: (card_id, html)}."""
    with wi_store.locked(hub_dir, f"apps:{hub}") as c:
        cur = c.execute(text("SELECT revision, digest FROM hz_app_config_revisions WHERE hub=:h AND active=1"),
                        {"h": hub}).mappings().first()
        current = cur["revision"] if cur else None
        if current != expected:
            raise revision_conflict(current or 0)
        if cur is not None and cur["digest"] == digest:
            raise WorkItemError("unchanged", "This configuration is already active.", status=409,
                                current_revision=current)
        top = c.execute(text("SELECT MAX(revision) FROM hz_app_config_revisions WHERE hub=:h"),
                        {"h": hub}).scalar() or 0
        for sha, (card_id, html) in resources.items():
            have = c.execute(text("SELECT 1 FROM hz_app_resources WHERE hub=:h AND sha256=:s"),
                             {"h": hub, "s": sha}).first()
            if have is None:
                c.execute(text("INSERT INTO hz_app_resources (hub, sha256, card_id, mime, html, created) "
                               "VALUES (:h, :s, :c, :m, :x, :t)"),
                          {"h": hub, "s": sha, "c": card_id, "m": "text/html;profile=mcp-app", "x": html,
                           "t": time.time()})
        c.execute(text("UPDATE hz_app_config_revisions SET active=0 WHERE hub=:h AND active=1"), {"h": hub})
        c.execute(text("INSERT INTO hz_app_config_revisions (hub, revision, digest, config_json, actor, created, "
                       "active) VALUES (:h, :r, :d, :j, :a, :t, 1)"),
                  {"h": hub, "r": top + 1, "d": digest, "j": wi_store.dumps(config), "a": actor, "t": time.time()})
    _active_cache.pop(hub, None)
    return {"revision": top + 1, "digest": digest}


def history(hub_dir, hub: str, limit: int = 20) -> list[dict]:
    with engine(hub_dir).connect() as c:
        rows = c.execute(text("SELECT revision, digest, actor, created, active FROM hz_app_config_revisions "
                              "WHERE hub=:h ORDER BY revision DESC LIMIT :n"), {"h": hub, "n": limit}).mappings()
        return [dict(r) for r in rows]


def resource(hub_dir, hub: str, sha256: str) -> str | None:
    with engine(hub_dir).connect() as c:
        return c.execute(text("SELECT html FROM hz_app_resources WHERE hub=:h AND sha256=:s"),
                         {"h": hub, "s": sha256}).scalar()


def create_instance(hub_dir, row: dict) -> dict:
    row = dict(row, created=time.time())
    with engine(hub_dir).begin() as c:
        c.execute(text(
            "INSERT INTO hz_app_instances (id, hub, card_id, kind, server_id, tool, config_revision, "
            "resource_sha256, requester, surface, conversation_id, arguments_json, result_json, text, created) "
            "VALUES (:id, :hub, :card_id, :kind, :server_id, :tool, :config_revision, :resource_sha256, "
            ":requester, :surface, :conversation_id, :arguments_json, :result_json, :text, :created)"), row)
    return row


def get_instance(hub_dir, hub: str, instance_id: str) -> dict:
    with engine(hub_dir).connect() as c:
        row = c.execute(text("SELECT * FROM hz_app_instances WHERE id=:i AND hub=:h"),
                        {"i": instance_id, "h": hub}).mappings().first()
    if row is None:
        raise not_found("card")
    out = dict(row)
    out["arguments"] = json.loads(out.pop("arguments_json") or "{}")
    out["result"] = json.loads(out.pop("result_json") or "{}")
    return out


def list_instances(hub_dir, hub: str, *, requester: str | None, limit: int = 50) -> list[dict]:
    q = ("SELECT id, card_id, kind, tool, requester, surface, conversation_id, config_revision, "
         "resource_sha256, created, revoked FROM hz_app_instances WHERE hub=:h")
    params: dict = {"h": hub, "n": max(1, min(int(limit), 200))}
    if requester is not None:
        q += " AND requester=:r"
        params["r"] = requester
    with engine(hub_dir).connect() as c:
        return [dict(r) for r in c.execute(text(q + " ORDER BY created DESC LIMIT :n"), params).mappings()]


def revoke(hub_dir, hub: str, instance_id: str, *, actor: str, reason: str) -> dict:
    with engine(hub_dir).begin() as c:
        c.execute(text("UPDATE hz_app_instances SET revoked=:t, revoked_by=:a, revoke_reason=:r "
                       "WHERE id=:i AND hub=:h AND revoked IS NULL"),
                  {"t": time.time(), "a": actor, "r": reason[:500], "i": instance_id, "h": hub})
    return get_instance(hub_dir, hub, instance_id)
