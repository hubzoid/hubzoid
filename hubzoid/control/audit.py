"""Audit rows for control-plane changes, written in the caller's transaction.

Uses the deployment's one audit log, ``hz_access_audit`` (conventions section
12), with the same keys as access changes. ``permission`` carries a short
JSON summary (revision, digest, changed keys): never customer copy, secrets or
file bytes.
"""
from __future__ import annotations

import json
import logging
import time

from sqlalchemy import text

log = logging.getLogger("hubzoid.control.audit")


def write(conn, actor: str, action: str, *, subject: str | None = None, hub: str | None = None,
          detail: dict | None = None, surface: str | None = None,
          request_id: str | None = None) -> None:
    summary = json.dumps(detail or {}, default=str, separators=(",", ":"))[:2000]
    conn.execute(text(
        "INSERT INTO hz_access_audit (ts, actor, action, subject, hub, permission, surface, request_id) "
        "VALUES (:t, :a, :ac, :s, :h, :p, :su, :r)"),
        {"t": time.time(), "a": actor, "ac": action, "s": subject, "h": hub, "p": summary,
         "su": surface, "r": request_id})
