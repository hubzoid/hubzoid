"""Baseline: the per-hub tables as of Hubzoid 0.9.6 (inbound chat history).

Created lazily before versioning, so it may or may not exist; create it if
missing and verify it if present.

Revision ID: hub_0001
Revises:
"""
from alembic import op
from sqlalchemy import Column, Float, Integer, MetaData, String, Table, Text

from hubzoid.migrations import verify_columns

revision = "hub_0001"
down_revision = None
branch_labels = None
depends_on = None

_metadata = MetaData()
_history = Table(
    "hz_inbound_history",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("chat_id", String(255), index=True),
    Column("role", String(32)),
    Column("content", Text),
    Column("created_at", Float),
)


def upgrade() -> None:
    conn = op.get_bind()
    verify_columns(conn, "hz_inbound_history", {"id", "chat_id", "role", "content", "created_at"})
    _metadata.create_all(conn, tables=[_history], checkfirst=True)


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
