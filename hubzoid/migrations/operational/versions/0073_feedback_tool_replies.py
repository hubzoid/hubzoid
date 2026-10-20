"""Replies in which record_feedback handled a request. Revision op_0073.

``hz_feedback_tool_replies`` keeps the id of each chat reply in which the
``record_feedback`` tool ran, so "your previous answer" never means a reply
that only confirmed a rating. It is kept apart from the rating itself: editing,
removing or re-saving a rating never forgets it. Ids only, no text. Deleting
the conversation deletes its rows.

A new table: no existing row is read or changed.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0073"
down_revision = "op_0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_feedback_tool_replies",
        sa.Column("message_id", sa.String(64), primary_key=True),
        sa.Column("conversation_id", sa.String(64), nullable=False, index=True),
        sa.Column("created_at", sa.Float, nullable=False),
    )


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
