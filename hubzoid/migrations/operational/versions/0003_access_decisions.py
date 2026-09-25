"""Access decisions: one row per restricted-tool call, allowed or denied.

Replaces the monthly `<hub>/logs/access-*.jsonl` files. Those files are imported
once per hub the first time the hub records or reads a decision.

Revision ID: op_0003
Revises: op_0002
"""
import sqlalchemy as sa
from alembic import op

revision = "op_0003"
down_revision = "op_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_access_decisions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.Float(precision=53), nullable=False),  # UTC epoch seconds
        sa.Column("hub", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),  # "anonymous" when unknown
        sa.Column("surface", sa.Text),
        sa.Column("tool", sa.Text),
        sa.Column("decision", sa.Text, nullable=False),  # allow|deny
        sa.Column("reason", sa.Text),
    )
    op.create_index("hz_access_decisions_hub_ts", "hz_access_decisions", ["hub", "ts"])
    op.create_index("hz_access_decisions_ts", "hz_access_decisions", ["ts"])


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
