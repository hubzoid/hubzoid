# Hubzoid Enterprise · access management. Production use requires a license
# with the "access" entitlement; free to run for development. See LICENSING.md.
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
from datetime import datetime
from pathlib import Path

from .identity import normalize

log = logging.getLogger("hubzoid.access")


def _month_file(hub_dir: Path, when: datetime) -> Path:
    d = Path(hub_dir) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"access-{when:%Y-%m}.jsonl"


def record(hub_dir, *, user, surface, tool, decision, reason) -> None:
    """Append one decision line. Never raises into the caller."""
    try:
        when = datetime.now()
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
         decision: str | None = None) -> list[dict]:
    """Return the most recent decisions across all monthly files, oldest first.

    Reading does not create the logs directory; an absent log is an empty list.
    """
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
            rows.append(row)
    return rows[-limit:] if limit and limit > 0 else rows
