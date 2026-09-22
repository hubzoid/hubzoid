# Hubzoid access management. MIT licensed like the rest of the repository.
"""Append-only access log, written where the decision is made: the runtime.

Open WebUI never sees a tool call, so the allow/deny can only be recorded here.
One JSON line per decision: {ts, user, surface, tool, decision, reason}. Files
are date-partitioned by month under `<hub>/logs/`, so a single file never grows
without bound and a time range is one file. `hubzoid audit <hub>` reads them.

Writing is best-effort: an audit failure must never break a tool call, so a
write error is logged to the runtime logger and swallowed, never raised.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .identity import normalize

log = logging.getLogger("hubzoid.access")


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


def _month_file(hub_dir: Path, when: datetime) -> Path:
    d = Path(hub_dir) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"access-{when:%Y-%m}.jsonl"


def record(hub_dir, *, user, surface, tool, decision, reason) -> None:
    """Append one decision line. Never raises into the caller."""
    try:
        # Record UTC with an explicit offset so timestamps are unambiguous across
        # deployments and time zones; the UI renders them in the viewer's zone.
        when = datetime.now(timezone.utc)
        entry = {
            "ts": when.isoformat(timespec="seconds"),
            "user": user or "anonymous",
            "surface": surface,
            "tool": tool,
            "decision": decision,  # "allow" | "deny"
            "reason": reason,
        }
        with _month_file(Path(hub_dir), when).open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001 — audit must not crash a tool call
        log.warning("access audit write failed", exc_info=True)


def read(hub_dir, *, limit: int = 200, user: str | None = None,
         decision: str | None = None, tool: str | None = None,
         surface: str | None = None, since: str | None = None,
         until: str | None = None) -> list[dict]:
    """Return the most recent decisions across all monthly files, oldest first.

    All filters are applied while scanning, so the `limit` (and any paging the
    caller does on top) is over the filtered set, not the raw tail. `since`/`until`
    are ISO strings compared as INSTANTS against the recorded ISO `ts` — both are
    parsed to timezone-aware datetimes (a naive value is read as UTC) so a window
    expressed with a `Z`, a `+00:00`, or a different offset than the stored row still
    selects the same instants. Reading does not create the logs directory; an absent
    log is an empty list.
    """
    since_dt = _instant(since)
    until_dt = _instant(until)
    logs = Path(hub_dir) / "logs"
    if not logs.is_dir():
        return []
    rows: list[dict] = []
    for fp in sorted(logs.glob("access-*.jsonl")):
        try:
            lines = fp.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if user and normalize(row.get("user", "")) != normalize(user):
                continue
            if decision and row.get("decision") != decision:
                continue
            if tool and row.get("tool") != tool:
                continue
            if surface and row.get("surface") != surface:
                continue
            if since_dt or until_dt:
                t = _instant(row.get("ts", ""))
                if t is None:
                    # Unparseable timestamp cannot be placed in the window: exclude it
                    # from a time-filtered read rather than guess.
                    continue
                if since_dt and t < since_dt:
                    continue
                if until_dt and t > until_dt:
                    continue
            rows.append(row)
    return rows[-limit:] if limit and limit > 0 else rows
