"""Audit events: one row per tool call and sign-in event, when the database
sink is on (``HUBZOID_AUDIT=db`` or ``both``).

Restricted-tool decisions keep their own table (``hz_access_decisions``) and
admin changes theirs (``hz_access_audit``). This table holds what had none:
every tool call (allowed or refused, its outcome and duration) and sign-in and
API key events. ``detail`` is JSON (reason, error class, argument summary,
result size, trace id), already redacted. Rows older than
``HUBZOID_AUDIT_RETENTION_DAYS`` (default 90) are removed.

Revision ID: op_0090
Revises: op_0082
"""
import sqlalchemy as sa
from alembic import op

revision = "op_0090"
down_revision = "op_0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_audit_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.Float(precision=53), nullable=False),  # UTC epoch seconds
        sa.Column("kind", sa.Text, nullable=False),               # tool | auth
        sa.Column("event", sa.Text, nullable=False),              # tool.call, auth.sign_in_failed, ...
        sa.Column("hub", sa.Text),
        sa.Column("subject", sa.Text),
        sa.Column("surface", sa.Text),
        sa.Column("tool", sa.Text),
        sa.Column("server", sa.Text),                             # MCP server or connector
        sa.Column("decision", sa.Text),                           # allow | deny
        sa.Column("outcome", sa.Text),                            # ok | error
        sa.Column("duration_ms", sa.Integer),
        sa.Column("client", sa.Text),
        sa.Column("chat_id", sa.Text),
        sa.Column("run_id", sa.Text),
        sa.Column("detail", sa.Text),
    )
    op.create_index("hz_audit_events_ts", "hz_audit_events", ["ts"])
    op.create_index("hz_audit_events_hub_ts", "hz_audit_events", ["hub", "ts"])
    op.create_index("hz_audit_events_kind_ts", "hz_audit_events", ["kind", "ts"])


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
