"""Small durable store for the FastMCP OAuthProvider integration.

Every credential is random and stored under SHA256; no access/refresh token,
authorization code, browser secret or Open WebUI session is persisted in plaintext.
Namespace is the canonical public MCP URL, isolating hubs in the shared store.
"""

from __future__ import annotations

import hashlib
import json
import time
from sqlalchemy import text
from . import db, migrations


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class OAuthStore:
    def __init__(self, hub_dir, resource):
        self.engine = db.operational_engine(hub_dir)
        migrations.upgrade(self.engine, "operational")
        self.namespace = resource

    def put(self, conn, key, kind, payload, expires):
        conn.execute(
            text(
                "INSERT INTO hz_mcp_oauth(namespace,digest,kind,payload,expires,used) VALUES (:n,:d,:k,:p,:e,0)"
            ),
            {
                "n": self.namespace,
                "d": digest(key),
                "k": kind,
                "p": json.dumps(payload),
                "e": int(expires),
            },
        )

    def get(self, conn, key, kind):
        row = (
            conn.execute(
                text(
                    "SELECT payload,expires,used FROM hz_mcp_oauth WHERE namespace=:n AND digest=:d AND kind=:k AND expires>:now"
                ),
                {
                    "n": self.namespace,
                    "d": digest(key),
                    "k": kind,
                    "now": int(time.time()),
                },
            )
            .mappings()
            .first()
        )
        return (
            {
                **json.loads(row["payload"]),
                "_expires": row["expires"],
                "_used": bool(row["used"]),
            }
            if row
            else None
        )

    def consume(self, conn, key, kind):
        # Compare-and-swap works across processes on SQLite and PostgreSQL.
        return (
            conn.execute(
                text(
                    "UPDATE hz_mcp_oauth SET used=1 WHERE namespace=:n AND digest=:d AND kind=:k AND used=0 AND expires>:now"
                ),
                {
                    "n": self.namespace,
                    "d": digest(key),
                    "k": kind,
                    "now": int(time.time()),
                },
            ).rowcount
            == 1
        )

    def revoke(self, conn, grant):
        self.consume(conn, grant, "grant")

    def grants(self, conn):
        rows = conn.execute(
            text(
                "SELECT payload,expires FROM hz_mcp_oauth WHERE namespace=:n AND kind='grant' AND used=0 AND expires>:now"
            ),
            {"n": self.namespace, "now": int(time.time())},
        ).mappings()
        return [{**json.loads(r["payload"]), "_expires": r["expires"]} for r in rows]

    def cleanup(self, conn):
        conn.execute(
            text("DELETE FROM hz_mcp_oauth WHERE namespace=:n AND expires<=:now"),
            {"n": self.namespace, "now": int(time.time())},
        )
