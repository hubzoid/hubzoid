"""Durable work items and safe external writes. Revision op_0050.

Execution history, stage checkpoints, messages and versions stay in the workflow
engine (DBOS). These tables hold only what it does not:

* ``hz_work_items``: one row per piece of requested work, by logical key and
  generation, with the route it may follow, its state and outcome, and the DBOS
  workflow id of its current execution. Follow-up messages wait in a small
  bounded inbox on the row, so they survive a parked item being continued.
* ``hz_operations``: one row per consequential external write, keyed by a
  logical key that excludes workflow ids and attempts. It holds the exact intent
  (an encrypted canonical payload and its digest), the write mode, the recovery
  class, the approval, each attempt and the outcome, including ``ambiguous``.
* ``hz_idempotency``: replies to creating requests, by caller, route and
  ``Idempotency-Key``, so a retried request returns the first result.

Tables only: no existing row is read or changed.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0050"
down_revision = "op_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_work_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("hub", sa.String(255), nullable=False),
        sa.Column("logical_key", sa.String(512), nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("workflow", sa.String(255), nullable=False),
        sa.Column("route", sa.String(64), nullable=False),
        sa.Column("route_json", sa.Text, nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column("input_digest", sa.String(80), nullable=False),
        sa.Column("input_json", sa.Text, nullable=False),
        sa.Column("result_json", sa.Text),
        sa.Column("error", sa.Text),
        sa.Column("requester", sa.String(320), nullable=False, server_default=""),
        sa.Column("origin_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("execution", sa.Integer, nullable=False, server_default="0"),
        sa.Column("workflow_id", sa.String(255)),
        sa.Column("app_version", sa.String(64)),
        sa.Column("counters_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("inbox_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("resume_step", sa.Integer),
        sa.Column("cancel_requested", sa.Integer, nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("updated", sa.Float, nullable=False),
    )
    op.create_index("hz_work_items_key", "hz_work_items",
                    ["hub", "logical_key", "generation"], unique=True)
    op.create_index("hz_work_items_state", "hz_work_items", ["hub", "state"])
    op.create_index("hz_work_items_created", "hz_work_items", ["hub", "created"])

    op.create_table(
        "hz_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("hub", sa.String(255), nullable=False),
        sa.Column("logical_key", sa.String(80), nullable=False),
        sa.Column("item_id", sa.String(36)),
        sa.Column("generation", sa.Integer),
        sa.Column("stage", sa.String(128)),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("action", sa.String(128)),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("recovery", sa.String(16), nullable=False),
        sa.Column("target_json", sa.Text, nullable=False),
        sa.Column("args_digest", sa.String(80), nullable=False),
        sa.Column("payload_enc", sa.Text, nullable=False),
        sa.Column("target_version", sa.String(255)),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("approval_json", sa.Text),
        sa.Column("attempts_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("result_json", sa.Text),
        sa.Column("evidence_json", sa.Text),
        sa.Column("resolution_json", sa.Text),
        sa.Column("error", sa.Text),
        sa.Column("requester", sa.String(320), nullable=False, server_default=""),
        sa.Column("workflow", sa.String(255)),
        sa.Column("workflow_id", sa.String(255)),
        sa.Column("resume_step", sa.Integer),
        sa.Column("provider_key", sa.String(255)),
        sa.Column("provider_key_expires", sa.Float),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("updated", sa.Float, nullable=False),
    )
    op.create_index("hz_operations_key", "hz_operations", ["logical_key"], unique=True)
    op.create_index("hz_operations_state", "hz_operations", ["hub", "state"])
    op.create_index("hz_operations_item", "hz_operations", ["item_id"])

    op.create_table(
        "hz_idempotency",
        sa.Column("caller", sa.String(320), primary_key=True),
        sa.Column("route", sa.String(128), primary_key=True),
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("digest", sa.String(80), nullable=False),
        sa.Column("status_code", sa.Integer, nullable=False),
        sa.Column("response_json", sa.Text, nullable=False),
        sa.Column("created", sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("hz_idempotency")
    op.drop_index("hz_operations_item", table_name="hz_operations")
    op.drop_index("hz_operations_state", table_name="hz_operations")
    op.drop_index("hz_operations_key", table_name="hz_operations")
    op.drop_table("hz_operations")
    op.drop_index("hz_work_items_created", table_name="hz_work_items")
    op.drop_index("hz_work_items_state", table_name="hz_work_items")
    op.drop_index("hz_work_items_key", table_name="hz_work_items")
    op.drop_table("hz_work_items")
