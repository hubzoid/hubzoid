# Hubzoid access management. MIT licensed like the rest of the repository.
"""The access decision log, written where the decision is made: the runtime.

Open WebUI never sees a tool call, so the allow/deny can only be recorded here.
One row per decision in the operational database (`hz_access_decisions`):
time, hub, user, surface, tool, decision, reason. `hubzoid audit <hub>` and the
Console's Activity page read it.

`record` returns whether the row was written. The guard refuses a restricted
tool call it could not record, so every call that ran has a row. A write
failure is never raised into the caller.

Earlier releases wrote monthly `<hub>/logs/access-*.jsonl` files. They are
imported once per hub, the first time the hub records or reads a decision, and
left in place.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .identity import normalize

log = logging.getLogger("hubzoid.access")

_IMPORTED: set[tuple[str, str]] = set()


def _instant(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp to a timezone-aware datetime for instant
    comparison. Accepts a trailing `Z`, an explicit offset, or a naive value (read
    as UTC). Returns None for empty/unparseable input."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _engine(hub_dir: Path):
    from .. import db, migrations

    engine = db.operational_engine(hub_dir)
    migrations.upgrade(engine, "operational")
    return engine


def _hub(hub_dir: Path) -> str:
    return normalize(Path(hub_dir).name)


def import_legacy(hub_dir) -> int:
    """Copy the pre-database JSONL decision files into the table, once per hub.
    The claim marker and the rows commit together, so two processes never import
    the same files twice. Returns the number of rows imported."""
    hub_dir = Path(hub_dir)
    engine = _engine(hub_dir)
    key = (str(engine.url), _hub(hub_dir))
    if key in _IMPORTED:
        return 0
    files = sorted((hub_dir / "logs").glob("access-*.jsonl")) if (hub_dir / "logs").is_dir() else []
    rows = []
    for fp in files:
        try:
            lines = fp.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            when = _instant(row.get("ts"))
            if when is None or row.get("decision") not in ("allow", "deny"):
                continue
            rows.append({"t": when.timestamp(), "h": key[1], "s": row.get("user") or "anonymous",
                         "sf": row.get("surface"), "tl": row.get("tool"),
                         "d": row["decision"], "r": row.get("reason")})
    marker = "decisions_imported:" + key[1]
    try:
        with engine.begin() as c:
            if c.execute(text("SELECT 1 FROM hz_meta WHERE k=:k"), {"k": marker}).fetchone():
                _IMPORTED.add(key)
                return 0
            c.execute(text("INSERT INTO hz_meta(k, v) VALUES(:k, :v)"),
                      {"k": marker, "v": json.dumps({"rows": len(rows), "at": time.time()})})
            if rows:
                c.execute(text(
                    "INSERT INTO hz_access_decisions (ts, hub, subject, surface, tool, decision, reason) "
                    "VALUES (:t, :h, :s, :sf, :tl, :d, :r)"), rows)
    except Exception:  # noqa: BLE001 — another process claimed it, or the store is down
        log.debug("access: legacy decision import skipped for %s", key[1], exc_info=True)
        return 0
    _IMPORTED.add(key)
    if rows:
        log.info("access: imported %d decisions from %s/logs", len(rows), hub_dir)
    return len(rows)


def record(hub_dir, *, user, surface, tool, decision, reason) -> bool:
    """Write one decision row. Returns False (never raises) when it could not."""
    try:
        hub_dir = Path(hub_dir)
        import_legacy(hub_dir)
        with _engine(hub_dir).begin() as c:
            c.execute(text(
                "INSERT INTO hz_access_decisions (ts, hub, subject, surface, tool, decision, reason) "
                "VALUES (:t, :h, :s, :sf, :tl, :d, :r)"),
                {"t": time.time(), "h": _hub(hub_dir), "s": normalize(user or "") or "anonymous",
                 "sf": surface, "tl": tool, "d": decision, "r": reason})
        return True
    except Exception:  # noqa: BLE001 — the caller decides what an unrecorded decision means
        log.error("access: could not record a decision for %s", tool, exc_info=True)
        return False


def read(hub_dir, *, limit: int = 200, user: str | None = None,
         decision: str | None = None, tool: str | None = None,
         surface: str | None = None, since: str | None = None,
         until: str | None = None) -> list[dict]:
    """The most recent decisions for this hub, oldest first.

    Filters apply before the limit, so paging is over the filtered set.
    `since`/`until` are ISO instants (a naive value is read as UTC). Rows come
    back as {ts (ISO, UTC), user, surface, tool, decision, reason}."""
    hub_dir = Path(hub_dir)
    import_legacy(hub_dir)
    clauses, params = ["hub = :h"], {"h": _hub(hub_dir)}
    if user:
        clauses.append("subject = :u")
        params["u"] = normalize(user)
    for col, value in (("decision", decision), ("tool", tool), ("surface", surface)):
        if value:
            clauses.append(f"{col} = :{col}")
            params[col] = value
    for op, value in ((">=", since), ("<=", until)):
        when = _instant(value)
        if value and when is None:
            return []
        if when is not None:
            key = "since" if op == ">=" else "until"
            clauses.append(f"ts {op} :{key}")
            params[key] = when.timestamp()
    q = ("SELECT ts, subject, surface, tool, decision, reason FROM hz_access_decisions WHERE "
         + " AND ".join(clauses) + " ORDER BY ts DESC, id DESC")
    if limit and limit > 0:
        q += " LIMIT :limit"
        params["limit"] = int(limit)
    with _engine(hub_dir).connect() as c:
        rows = c.execute(text(q), params).fetchall()
    return [
        {"ts": datetime.fromtimestamp(r[0], timezone.utc).isoformat(timespec="seconds"),
         "user": r[1], "surface": r[2], "tool": r[3], "decision": r[4], "reason": r[5]}
        for r in reversed(rows)
    ]


def denials(engine, since: float, hubs: list[str] | None = None) -> dict[str, int]:
    """Denied restricted-tool calls per hub since `since` (epoch seconds)."""
    q = "SELECT hub, COUNT(*) FROM hz_access_decisions WHERE decision='deny' AND ts >= :s"
    params: dict = {"s": since}
    if hubs is not None:
        if not hubs:
            return {}
        names = {f"h{i}": normalize(h) for i, h in enumerate(hubs)}
        q += " AND hub IN (" + ", ".join(f":{k}" for k in names) + ")"
        params.update(names)
    with engine.connect() as c:
        return {h: n for h, n in c.execute(text(q + " GROUP BY hub"), params).fetchall()}
