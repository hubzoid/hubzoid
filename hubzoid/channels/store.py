"""Rows for channel connections, threads, deliveries and account links, in the
shared operational store.

Admission is one short write transaction serialized per connection (SQLite
``BEGIN IMMEDIATE``, a PostgreSQL advisory lock), backed by unique indexes on
the provider's delivery id and on the event's own key. No transaction is held
across a network call. Changes to a thread are compare-and-set on `revision`.
"""
from __future__ import annotations

import json
import time
import uuid

from sqlalchemy import text

from ..work_items import store as wstore
from .errors import not_found, revision_conflict

engine = wstore.engine
locked = wstore.locked
now = wstore.now
dumps = wstore.dumps

#: delivery outcomes. `accepted` waits to be routed; `routed` reached work.
DELIVERY_STATES = ("accepted", "routed", "waiting", "duplicate", "ignored", "disabled", "denied",
                   "quarantined", "failed")
OPEN_DELIVERY = ("accepted", "waiting")


def new_id() -> str:
    return str(uuid.uuid4())


def _row(row, json_cols=()) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for col in json_cols:
        if out.get(col) is not None:
            out[col] = json.loads(out[col])
    return out


# -- connections ----------------------------------------------------------------

_CONN_JSON = ("config_json", "state_json")


def get_connection(hub_dir, hub: str, cid: str, *, conn=None) -> dict | None:
    sql = text("SELECT * FROM hz_channel_connections WHERE hub=:h AND id=:i")
    if conn is not None:
        return _row(conn.execute(sql, {"h": hub, "i": cid}).mappings().first(), _CONN_JSON)
    with engine(hub_dir).connect() as c:
        return _row(c.execute(sql, {"h": hub, "i": cid}).mappings().first(), _CONN_JSON)


def list_connections(hub_dir, hub: str) -> list[dict]:
    with engine(hub_dir).connect() as c:
        rows = c.execute(text("SELECT * FROM hz_channel_connections WHERE hub=:h ORDER BY id"),
                         {"h": hub}).mappings().all()
    return [_row(r, _CONN_JSON) for r in rows]


def put_connection(conn, *, hub: str, cid: str, provider: str, config: dict, digest: str,
                   expected_revision, status: str, file_path: str, actor: str) -> dict:
    """Insert or replace the active revision. `expected_revision` is an int,
    or "absent" to create only."""
    cur = conn.execute(text("SELECT revision FROM hz_channel_connections WHERE hub=:h AND id=:i"),
                       {"h": hub, "i": cid}).first()
    t = now()
    if cur is None:
        if expected_revision not in ("absent", None):
            raise revision_conflict(None)
        conn.execute(text(
            "INSERT INTO hz_channel_connections (hub,id,provider,config_json,digest,revision,status,"
            "file_path,applied_by,state_json,created,updated) VALUES (:h,:i,:p,:c,:d,1,:s,:f,:a,'{}',:t,:t)"),
            {"h": hub, "i": cid, "p": provider, "c": dumps(config), "d": digest, "s": status,
             "f": file_path, "a": actor, "t": t})
        return {"revision": 1, "created": True}
    if expected_revision == "absent" or (expected_revision is not None and int(expected_revision) != cur[0]):
        raise revision_conflict(cur[0])
    conn.execute(text(
        "UPDATE hz_channel_connections SET provider=:p, config_json=:c, digest=:d, revision=revision+1, "
        "status=:s, file_path=:f, applied_by=:a, updated=:t WHERE hub=:h AND id=:i"),
        {"h": hub, "i": cid, "p": provider, "c": dumps(config), "d": digest, "s": status, "f": file_path,
         "a": actor, "t": t})
    return {"revision": cur[0] + 1, "created": False}


def set_connection_state(hub_dir, hub: str, cid: str, **state) -> None:
    """Merge provider bookkeeping (verified ids, last delivery time) into the
    connection's state. Never configuration."""
    with locked(hub_dir, f"channel:{hub}:{cid}") as c:
        row = c.execute(text("SELECT state_json FROM hz_channel_connections WHERE hub=:h AND id=:i"),
                        {"h": hub, "i": cid}).first()
        if row is None:
            return
        merged = {**json.loads(row[0] or "{}"), **state}
        c.execute(text("UPDATE hz_channel_connections SET state_json=:s WHERE hub=:h AND id=:i"),
                  {"s": dumps(merged), "h": hub, "i": cid})


def delete_connection(conn, hub: str, cid: str) -> None:
    conn.execute(text("DELETE FROM hz_channel_connections WHERE hub=:h AND id=:i"), {"h": hub, "i": cid})


# -- threads --------------------------------------------------------------------

_THREAD_JSON = ("origin_json", "conversations_json")


def thread_for(conn, *, hub: str, cid: str, origin: dict) -> tuple[dict, bool]:
    """The thread row for an origin, created on first sight. The stored origin
    never changes afterwards."""
    key = {"h": hub, "c": cid, "w": origin["workspace_id"], "k": origin["thread_key"]}
    row = conn.execute(text("SELECT * FROM hz_channel_threads WHERE hub=:h AND connection_id=:c AND "
                            "workspace_id=:w AND thread_key=:k"), key).mappings().first()
    if row is not None:
        return _row(row, _THREAD_JSON), False
    t = now()
    tid = new_id()
    conn.execute(text(
        "INSERT INTO hz_channel_threads (id,hub,connection_id,workspace_id,thread_key,origin_json,subscribed,"
        "conversations_json,requester,revision,created,updated) VALUES (:id,:h,:c,:w,:k,:o,0,'{}','',1,:t,:t)"),
        {**key, "id": tid, "o": dumps(origin), "t": t})
    return _row(conn.execute(text("SELECT * FROM hz_channel_threads WHERE id=:i"), {"i": tid})
                .mappings().first(), _THREAD_JSON), True


def get_thread(hub_dir, tid: str, *, conn=None) -> dict | None:
    sql = text("SELECT * FROM hz_channel_threads WHERE id=:i")
    if conn is not None:
        return _row(conn.execute(sql, {"i": tid}).mappings().first(), _THREAD_JSON)
    with engine(hub_dir).connect() as c:
        return _row(c.execute(sql, {"i": tid}).mappings().first(), _THREAD_JSON)


def require_thread(hub_dir, hub: str, cid: str, tid: str) -> dict:
    row = get_thread(hub_dir, tid)
    if row is None or row["hub"] != hub or row["connection_id"] != cid:
        raise not_found("thread")
    return row


def update_thread(hub_dir, tid: str, *, expected_revision: int | None = None, conn=None, **fields) -> dict:
    def _do(c):
        cur = c.execute(text("SELECT revision FROM hz_channel_threads WHERE id=:i"), {"i": tid}).first()
        if cur is None:
            raise not_found("thread")
        if expected_revision is not None and int(expected_revision) != cur[0]:
            raise revision_conflict(cur[0])
        sets, params = ["revision=revision+1", "updated=:t"], {"i": tid, "t": now()}
        for k, v in fields.items():
            sets.append(f"{k}=:{k}")
            params[k] = dumps(v) if k in _THREAD_JSON else v
        c.execute(text(f"UPDATE hz_channel_threads SET {', '.join(sets)} WHERE id=:i"), params)
        return _row(c.execute(text("SELECT * FROM hz_channel_threads WHERE id=:i"), {"i": tid})
                    .mappings().first(), _THREAD_JSON)

    if conn is not None:
        return _do(conn)
    with locked(hub_dir, f"thread:{tid}") as c:
        return _do(c)


def list_threads(hub_dir, hub: str, cid: str, *, limit: int = 50, cursor: str | None = None) -> dict:
    limit = max(1, min(int(limit), 100))
    params = {"h": hub, "c": cid, "n": limit + 1}
    where = "hub=:h AND connection_id=:c"
    if cursor:
        try:
            t, tid = cursor.split("|", 1)
            where += " AND (updated < :ct OR (updated = :ct AND id < :ci))"
            params.update(ct=float(t), ci=tid)
        except ValueError:
            pass
    with engine(hub_dir).connect() as c:
        rows = c.execute(text(f"SELECT * FROM hz_channel_threads WHERE {where} "
                              "ORDER BY updated DESC, id DESC LIMIT :n"), params).mappings().all()
    items = [_row(r, _THREAD_JSON) for r in rows[:limit]]
    nxt = f"{items[-1]['updated']}|{items[-1]['id']}" if len(rows) > limit and items else None
    return {"items": items, "next_cursor": nxt}


def thread_by_conversation(hub_dir, hub: str, conversation_id: str) -> dict | None:
    """The thread a native conversation mirrors, if any."""
    like = f'%"{conversation_id}"%'
    with engine(hub_dir).connect() as c:
        rows = c.execute(text("SELECT * FROM hz_channel_threads WHERE hub=:h AND conversations_json LIKE :l"),
                         {"h": hub, "l": like}).mappings().all()
    for r in rows:
        row = _row(r, _THREAD_JSON)
        if conversation_id in (row["conversations_json"] or {}).values():
            return row
    return None


def thread_by_work_item(hub_dir, hub: str, item_id: str) -> dict | None:
    with engine(hub_dir).connect() as c:
        row = c.execute(text("SELECT * FROM hz_channel_threads WHERE hub=:h AND work_item_id=:w"),
                        {"h": hub, "w": item_id}).mappings().first()
    return _row(row, _THREAD_JSON)


# -- deliveries -----------------------------------------------------------------

_DELIVERY_JSON = ("actor_json", "route_json")


def find_delivery(conn, *, hub: str, cid: str, delivery_id: str, index: int) -> dict | None:
    row = conn.execute(text("SELECT * FROM hz_channel_deliveries WHERE hub=:h AND connection_id=:c AND "
                            "delivery_id=:d AND event_index=:x"),
                       {"h": hub, "c": cid, "d": delivery_id, "x": index}).mappings().first()
    return _row(row, _DELIVERY_JSON)


def find_semantic(conn, *, hub: str, cid: str, semantic_key: str) -> dict | None:
    row = conn.execute(text("SELECT * FROM hz_channel_deliveries WHERE hub=:h AND connection_id=:c AND "
                            "semantic_key=:s"), {"h": hub, "c": cid, "s": semantic_key}).mappings().first()
    return _row(row, _DELIVERY_JSON)


def insert_delivery(conn, **fields) -> dict:
    t = now()
    fields = {"id": new_id(), "event_index": 0, "semantic_key": None, "thread_id": None, "actor_json": {},
              "event_enc": None, "reason": None, "route_json": {}, "verification": "", **fields}
    params = {k: (dumps(v) if k in _DELIVERY_JSON else v) for k, v in fields.items()}
    params.update(created=t, updated=t, revision=1)
    cols = ",".join(params)
    conn.execute(text(f"INSERT INTO hz_channel_deliveries ({cols}) VALUES ({','.join(':' + k for k in params)})"),
                 params)
    return _row(conn.execute(text("SELECT * FROM hz_channel_deliveries WHERE id=:i"), {"i": fields["id"]})
                .mappings().first(), _DELIVERY_JSON)


def get_delivery(hub_dir, did: str, *, conn=None) -> dict | None:
    sql = text("SELECT * FROM hz_channel_deliveries WHERE id=:i")
    if conn is not None:
        return _row(conn.execute(sql, {"i": did}).mappings().first(), _DELIVERY_JSON)
    with engine(hub_dir).connect() as c:
        return _row(c.execute(sql, {"i": did}).mappings().first(), _DELIVERY_JSON)


def update_delivery(hub_dir, did: str, *, expect_status=None, conn=None, **fields) -> dict | None:
    """Compare-and-set on status when `expect_status` is given. Returns the
    row, or None when the status had moved on."""
    def _do(c):
        sets, params = ["revision=revision+1", "updated=:t"], {"i": did, "t": now()}
        for k, v in fields.items():
            sets.append(f"{k}=:{k}")
            params[k] = dumps(v) if k in _DELIVERY_JSON else v
        where = "id=:i"
        if expect_status is not None:
            states = (expect_status,) if isinstance(expect_status, str) else tuple(expect_status)
            where += " AND status IN (" + ",".join(f":s{n}" for n in range(len(states))) + ")"
            params.update({f"s{n}": s for n, s in enumerate(states)})
        n = c.execute(text(f"UPDATE hz_channel_deliveries SET {', '.join(sets)} WHERE {where}"), params).rowcount
        if not n:
            return None
        return _row(c.execute(text("SELECT * FROM hz_channel_deliveries WHERE id=:i"), {"i": did})
                    .mappings().first(), _DELIVERY_JSON)

    if conn is not None:
        return _do(conn)
    with engine(hub_dir).begin() as c:
        return _do(c)


def list_deliveries(hub_dir, hub: str, cid: str, *, status: str | None = None, thread: str | None = None,
                    limit: int = 50, cursor: str | None = None) -> dict:
    limit = max(1, min(int(limit), 100))
    where, params = "hub=:h AND connection_id=:c", {"h": hub, "c": cid, "n": limit + 1}
    if status:
        where += " AND status=:s"
        params["s"] = status
    if thread:
        where += " AND thread_id=:th"
        params["th"] = thread
    if cursor:
        try:
            t, did = cursor.split("|", 1)
            where += " AND (created < :ct OR (created = :ct AND id < :ci))"
            params.update(ct=float(t), ci=did)
        except ValueError:
            pass
    with engine(hub_dir).connect() as c:
        rows = c.execute(text(f"SELECT * FROM hz_channel_deliveries WHERE {where} "
                              "ORDER BY created DESC, id DESC LIMIT :n"), params).mappings().all()
    items = [_row(r, _DELIVERY_JSON) for r in rows[:limit]]
    nxt = f"{items[-1]['created']}|{items[-1]['id']}" if len(rows) > limit and items else None
    return {"items": items, "next_cursor": nxt}


def open_deliveries(hub_dir, hub: str, *, cid: str | None = None, thread: str | None = None,
                    limit: int = 200) -> list[dict]:
    """Accepted or waiting deliveries, oldest first: what routing still owes."""
    where, params = "hub=:h AND status IN ('accepted','waiting')", {"h": hub, "n": limit}
    if cid:
        where += " AND connection_id=:c"
        params["c"] = cid
    if thread:
        where += " AND thread_id=:t"
        params["t"] = thread
    with engine(hub_dir).connect() as c:
        rows = c.execute(text(f"SELECT * FROM hz_channel_deliveries WHERE {where} "
                              "ORDER BY created, event_index LIMIT :n"), params).mappings().all()
    return [_row(r, _DELIVERY_JSON) for r in rows]


def counts(hub_dir, hub: str, cid: str) -> dict:
    with engine(hub_dir).connect() as c:
        rows = c.execute(text("SELECT status, COUNT(*) FROM hz_channel_deliveries WHERE hub=:h AND "
                              "connection_id=:c GROUP BY status"), {"h": hub, "c": cid}).all()
        threads = c.execute(text("SELECT COUNT(*) FROM hz_channel_threads WHERE hub=:h AND connection_id=:c"),
                            {"h": hub, "c": cid}).scalar() or 0
        last = c.execute(text("SELECT MAX(created) FROM hz_channel_deliveries WHERE hub=:h AND "
                              "connection_id=:c AND status NOT IN ('quarantined')"),
                         {"h": hub, "c": cid}).scalar()
    return {"deliveries": {s: n for s, n in rows}, "threads": threads, "last_delivery_at": last}


# -- account links ----------------------------------------------------------------

def resolve_link(hub_dir, hub: str, provider: str, workspace_id: str, external_id: str) -> dict | None:
    with engine(hub_dir).connect() as c:
        row = c.execute(text("SELECT * FROM hz_channel_links WHERE hub=:h AND provider=:p AND workspace_id=:w "
                             "AND external_id=:e AND revoked IS NULL ORDER BY created DESC"),
                        {"h": hub, "p": provider, "w": workspace_id, "e": external_id}).mappings().first()
    return _row(row)


def add_link(hub_dir, *, hub: str, provider: str, workspace_id: str, external_id: str, account_id: str,
             account_email: str, display: str, actor: str) -> dict:
    with locked(hub_dir, f"channel-links:{hub}:{provider}") as c:
        cur = c.execute(text("SELECT * FROM hz_channel_links WHERE hub=:h AND provider=:p AND workspace_id=:w "
                             "AND external_id=:e AND revoked IS NULL"),
                        {"h": hub, "p": provider, "w": workspace_id, "e": external_id}).mappings().first()
        if cur is not None:
            if cur["account_id"] == account_id:
                return dict(cur)
            c.execute(text("UPDATE hz_channel_links SET revoked=:t WHERE id=:i"), {"t": now(), "i": cur["id"]})
        lid = new_id()
        c.execute(text("INSERT INTO hz_channel_links (id,hub,provider,workspace_id,external_id,account_id,"
                       "account_email,display,created_by,created,revoked) VALUES (:i,:h,:p,:w,:e,:a,:m,:d,:by,:t,NULL)"),
                  {"i": lid, "h": hub, "p": provider, "w": workspace_id, "e": external_id, "a": account_id,
                   "m": account_email, "d": display, "by": actor, "t": now()})
        return dict(c.execute(text("SELECT * FROM hz_channel_links WHERE id=:i"), {"i": lid}).mappings().first())


def revoke_link(hub_dir, hub: str, lid: str) -> bool:
    with engine(hub_dir).begin() as c:
        return c.execute(text("UPDATE hz_channel_links SET revoked=:t WHERE id=:i AND hub=:h AND revoked IS NULL"),
                         {"t": time.time(), "i": lid, "h": hub}).rowcount > 0


def list_links(hub_dir, hub: str, provider: str | None = None) -> list[dict]:
    where, params = "hub=:h AND revoked IS NULL", {"h": hub}
    if provider:
        where += " AND provider=:p"
        params["p"] = provider
    with engine(hub_dir).connect() as c:
        rows = c.execute(text(f"SELECT * FROM hz_channel_links WHERE {where} ORDER BY created DESC"),
                         params).mappings().all()
    return [dict(r) for r in rows]
