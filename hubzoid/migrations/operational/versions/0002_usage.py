"""Usage: one row per completed chat turn or workflow model call.

Written by Hubzoid itself (the bridge and workflow calls), so the Console's
numbers never depend on which chat UI fronts the hub. No message content.

Revision ID: op_0002
Revises: op_0001
"""
import sqlalchemy as sa
from alembic import op

revision = "op_0002"
down_revision = "op_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_usage",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.Float(precision=53), nullable=False),  # UTC epoch seconds
        sa.Column("hub", sa.Text, nullable=False),
        sa.Column("surface", sa.Text, nullable=False),  # web|slack|whatsapp|telegram|api|workflow
        sa.Column("kind", sa.Text, nullable=False),  # chat|llm|agent|decide
        sa.Column("subject", sa.Text),  # verified user or workflow:<name>; NULL = anonymous
        sa.Column("chat_id", sa.Text),
        sa.Column("model", sa.Text),
        sa.Column("input_tokens", sa.BigInteger),
        sa.Column("output_tokens", sa.BigInteger),
        sa.Column("cost_usd", sa.Float(precision=53)),  # estimated; NULL = unknown
        sa.Column("status", sa.Text, nullable=False),  # ok|error
        sa.Column("duration_ms", sa.Integer),
    )
    op.create_index("hz_usage_ts", "hz_usage", ["ts"])
    op.create_index("hz_usage_hub_ts", "hz_usage", ["hub", "ts"])


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
