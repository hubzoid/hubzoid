"""Baseline: the operational tables as of Hubzoid 0.9.6.

Existing installs created these lazily, so any subset may already exist. This
revision creates what is missing and verifies the columns of what is present.

Revision ID: op_0001
Revises:
"""
from alembic import op
from sqlalchemy import text

from hubzoid.migrations import verify_columns

revision = "op_0001"
down_revision = None
branch_labels = None
depends_on = None

_DDL = [
    """CREATE TABLE IF NOT EXISTS hz_grants (
        subject    TEXT NOT NULL,
        hub        TEXT NOT NULL,
        permission TEXT NOT NULL,
        PRIMARY KEY (subject, hub, permission))""",
    "CREATE INDEX IF NOT EXISTS hz_grants_hub ON hz_grants (hub)",
    """CREATE TABLE IF NOT EXISTS hz_policy_revision (
        id  INTEGER PRIMARY KEY,
        rev INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS hz_identities (
        subject  TEXT PRIMARY KEY,
        email    TEXT,
        owui_id  TEXT,
        phone    TEXT,
        display  TEXT,
        pending  INTEGER NOT NULL DEFAULT 0,
        created  DOUBLE PRECISION)""",
    "CREATE INDEX IF NOT EXISTS hz_identities_email ON hz_identities (email)",
    "CREATE INDEX IF NOT EXISTS hz_identities_owui ON hz_identities (owui_id)",
    "CREATE INDEX IF NOT EXISTS hz_identities_phone ON hz_identities (phone)",
    """CREATE TABLE IF NOT EXISTS hz_identity_attrs (
        hub     TEXT NOT NULL,
        subject TEXT NOT NULL,
        k       TEXT NOT NULL,
        v       TEXT,
        PRIMARY KEY (hub, subject, k))""",
    """CREATE TABLE IF NOT EXISTS hz_meta (
        k TEXT PRIMARY KEY,
        v TEXT)""",
    """CREATE TABLE IF NOT EXISTS hz_access_audit (
        ts         DOUBLE PRECISION NOT NULL,
        actor      TEXT,
        action     TEXT NOT NULL,
        subject    TEXT,
        hub        TEXT,
        permission TEXT)""",
    "CREATE INDEX IF NOT EXISTS hz_access_audit_ts ON hz_access_audit (ts)",
    """CREATE TABLE IF NOT EXISTS hz_workflows (
        hub      TEXT NOT NULL,
        name     TEXT NOT NULL,
        schedule TEXT,
        timezone TEXT,
        updated  DOUBLE PRECISION,
        PRIMARY KEY (hub, name))""",
    """CREATE TABLE IF NOT EXISTS hz_workflow_kv (
        hub      TEXT NOT NULL,
        workflow TEXT NOT NULL,
        k        TEXT NOT NULL,
        v        TEXT,
        PRIMARY KEY (hub, workflow, k))""",
]

_COLUMNS = {
    "hz_grants": {"subject", "hub", "permission"},
    "hz_policy_revision": {"id", "rev"},
    "hz_identities": {"subject", "email", "owui_id", "phone", "display", "pending", "created"},
    "hz_identity_attrs": {"hub", "subject", "k", "v"},
    "hz_meta": {"k", "v"},
    "hz_access_audit": {"ts", "actor", "action", "subject", "hub", "permission"},
    "hz_workflows": {"hub", "name", "schedule", "timezone", "updated"},
    "hz_workflow_kv": {"hub", "workflow", "k", "v"},
}


def upgrade() -> None:
    conn = op.get_bind()
    for table, cols in _COLUMNS.items():
        verify_columns(conn, table, cols)
    for stmt in _DDL:
        conn.execute(text(stmt))
    if conn.dialect.name == "postgresql":
        # Early Postgres installs created these time columns as REAL.
        for table, column in (("hz_identities", "created"), ("hz_access_audit", "ts"),
                              ("hz_workflows", "updated")):
            kind = conn.execute(text(
                "SELECT data_type FROM information_schema.columns WHERE "
                "table_schema=current_schema() AND table_name=:t AND column_name=:c"),
                {"t": table, "c": column}).scalar()
            if kind == "real":
                conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE DOUBLE PRECISION"))
    conn.execute(text("INSERT INTO hz_policy_revision (id, rev) VALUES (1, 0) ON CONFLICT (id) DO NOTHING"))


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
