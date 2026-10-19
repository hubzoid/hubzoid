"""Intake: messages from outside the organization, kept for review. Revision op_0061.

An email or a WhatsApp message is captured with its attachments before any
work starts. The channel delivery (op_0060) is its receipt; these tables hold
the intake record people review:

* ``hz_intake_items``: one per captured message, with the sender as the
  provider verified it, the trust decision and its reason, the work item it
  started, and its state (received, quarantined, ready, dispatched, rejected
  or failed). The message itself is kept encrypted.
* ``hz_intake_attachments``: each attachment's name, type, size, hash and
  state, and the file it was stored as. A missing required file keeps the
  whole item from starting work.

Tables only: no existing row is read or changed.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0061"
down_revision = "op_0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_intake_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("hub", sa.String(255), nullable=False),
        sa.Column("channel_id", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("delivery_id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(36)),
        sa.Column("provider_message_id", sa.String(512), nullable=False),
        sa.Column("sender_json", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False, server_default=""),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("envelope_enc", sa.Text, nullable=False),
        sa.Column("trust_json", sa.Text, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column("work_item_id", sa.String(36)),
        sa.Column("decided_by", sa.String(320)),
        sa.Column("decision_reason", sa.Text),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("updated", sa.Float, nullable=False),
    )
    op.create_index("hz_intake_items_delivery", "hz_intake_items", ["delivery_id"], unique=True)
    op.create_index("hz_intake_items_state", "hz_intake_items", ["hub", "channel_id", "state", "created"])
    op.create_table(
        "hz_intake_attachments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("hub", sa.String(255), nullable=False),
        sa.Column("intake_id", sa.String(36), nullable=False),
        sa.Column("part", sa.String(255), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(255), nullable=False),
        sa.Column("declared_bytes", sa.Integer),
        sa.Column("actual_bytes", sa.Integer),
        sa.Column("sha256", sa.String(80)),
        sa.Column("file_id", sa.String(64)),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("updated", sa.Float, nullable=False),
    )
    op.create_index("hz_intake_attachments_part", "hz_intake_attachments", ["intake_id", "part"], unique=True)


def downgrade() -> None:
    op.drop_index("hz_intake_attachments_part", "hz_intake_attachments")
    op.drop_table("hz_intake_attachments")
    op.drop_index("hz_intake_items_state", "hz_intake_items")
    op.drop_index("hz_intake_items_delivery", "hz_intake_items")
    op.drop_table("hz_intake_items")
