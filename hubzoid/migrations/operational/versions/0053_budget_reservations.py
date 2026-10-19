"""Budget reservations. Revision op_0053.

``hz_budget_reservations``: what a model call holds against the hub's limits
(budgets.yaml) while it runs. Recorded usage stays in ``hz_usage``; a
reservation is settled when its call ends. One that never settles (the process
stopped) keeps holding until an administrator releases it.

Table only: no existing row is read or changed.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0053"
down_revision = "op_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_budget_reservations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("hub", sa.String(255), nullable=False),
        sa.Column("run_id", sa.String(255), nullable=False),
        sa.Column("item_id", sa.String(36)),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("model", sa.String(255)),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float, nullable=False, server_default="0"),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("note", sa.String(500)),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("settled", sa.Float),
    )
    op.create_index("hz_budget_reservations_open", "hz_budget_reservations", ["hub", "state", "month"])
    op.create_index("hz_budget_reservations_run", "hz_budget_reservations", ["run_id"])


def downgrade() -> None:
    op.drop_index("hz_budget_reservations_run", table_name="hz_budget_reservations")
    op.drop_index("hz_budget_reservations_open", table_name="hz_budget_reservations")
    op.drop_table("hz_budget_reservations")
