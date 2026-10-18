"""Isolated agents (stations) and handoff documents. Revision op_0031.

* ``hz_station_calls``: one row per station call, by its logical key (work item,
  generation, stage, revision). It pins the station definition the call runs
  (instructions, tools, schema, model and their digest), counts its single
  orchestrator retry and its crash recoveries, and holds the validated output
  or the error. Each attempt runs as one workflow-engine step.
* ``hz_handoffs``: immutable documents a station saves for another station of
  the same work item. Readable only within that work item.

Tables only: no existing row is read or changed.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0031"
down_revision = "op_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_station_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("hub", sa.String(255), nullable=False),
        sa.Column("item_id", sa.String(36), nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("stage", sa.String(128), nullable=False),
        sa.Column("revision_no", sa.Integer, nullable=False),
        sa.Column("agent", sa.String(128), nullable=False),
        sa.Column("definition_digest", sa.String(80), nullable=False),
        sa.Column("definition_json", sa.Text, nullable=False),
        sa.Column("message_digest", sa.String(80), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("output_json", sa.Text),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error", sa.Text),
        sa.Column("retry_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("clarification", sa.Text),
        sa.Column("recovery_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("attempts_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("usage_json", sa.Text),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("updated", sa.Float, nullable=False),
    )
    op.create_index("hz_station_calls_key", "hz_station_calls",
                    ["hub", "item_id", "generation", "stage", "revision_no"], unique=True)
    op.create_index("hz_station_calls_item", "hz_station_calls", ["item_id"])

    op.create_table(
        "hz_handoffs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("hub", sa.String(255), nullable=False),
        sa.Column("item_id", sa.String(36), nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("call_id", sa.String(36)),
        sa.Column("producer_key", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("markdown", sa.Text, nullable=False),
        sa.Column("sha256", sa.String(80), nullable=False),
        sa.Column("characters", sa.Integer, nullable=False),
        sa.Column("created_by", sa.String(320), nullable=False, server_default=""),
        sa.Column("created", sa.Float, nullable=False),
    )
    op.create_index("hz_handoffs_producer", "hz_handoffs", ["hub", "item_id", "producer_key"], unique=True)
    op.create_index("hz_handoffs_item", "hz_handoffs", ["item_id"])


def downgrade() -> None:
    op.drop_index("hz_handoffs_item", table_name="hz_handoffs")
    op.drop_index("hz_handoffs_producer", table_name="hz_handoffs")
    op.drop_table("hz_handoffs")
    op.drop_index("hz_station_calls_item", table_name="hz_station_calls")
    op.drop_index("hz_station_calls_key", table_name="hz_station_calls")
    op.drop_table("hz_station_calls")
