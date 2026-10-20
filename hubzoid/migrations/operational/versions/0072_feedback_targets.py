"""Feedback from every surface. Revision op_0072.

``hz_feedback`` (op_0025) held one person's rating of one native chat answer.
Ratings now also come from the ``record_feedback`` tool, Slack and WhatsApp,
so a row says what it rates, who rated it and where:

* ``target_kind``: ``message`` (a native chat answer, ``message_id``) or
  ``channel_reply`` (a delivered channel reply, ``message_id`` holds its
  ``hz_operations`` id). NULL reads as ``message``.
* ``source``: web, tool, slack, whatsapp or cli. NULL reads as ``web``.
* ``author_kind``: ``account`` (``author_id`` is an account id) or
  ``external`` (an opaque id for an outside sender, never an account).
  ``author_label`` is how an external author is shown. NULL reads as
  ``account``.
* ``channel_id`` and ``thread_id``: the channel thread a rating came from.
* ``request_key``: the request that last wrote the row (a tool call's
  inbound message, a provider event), so a repeat changes nothing.

``conversation_id`` becomes nullable: a WhatsApp reply has no conversation,
and none is invented. Existing rows are not changed.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0072"
down_revision = "op_0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("hz_feedback") as batch:
        batch.alter_column("conversation_id", existing_type=sa.String(64), nullable=True)
        batch.add_column(sa.Column("target_kind", sa.String(16)))
        batch.add_column(sa.Column("source", sa.String(16)))
        batch.add_column(sa.Column("author_kind", sa.String(16)))
        batch.add_column(sa.Column("author_label", sa.String(120)))
        batch.add_column(sa.Column("channel_id", sa.String(64)))
        batch.add_column(sa.Column("thread_id", sa.String(64)))
        batch.add_column(sa.Column("request_key", sa.String(160)))
    op.create_index("ix_hz_feedback_hub_updated", "hz_feedback", ["hub_id", "updated_at"])


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
