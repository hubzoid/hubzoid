"""Approvals and questions. Revision op_0040.

* ``hz_hitl_requests``: one approval (an exact tool call or document) or one
  question, waiting for a person, settled once.
* ``hz_hitl_grants``: what an approval allows the agent afterwards: the exact
  call once, the tool for the rest of a chat, or the tool always for one person.
* ``hz_hitl_events``: ordered request events for live views.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0040"
down_revision = "op_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_hitl_requests",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("hub_id", sa.String(255), nullable=False, index=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("requester_id", sa.String(255), nullable=False),
        sa.Column("requester_email", sa.String(320)),
        sa.Column("approvers_json", sa.String(None), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("tool", sa.String(255)),
        sa.Column("arguments_json", sa.String(None)),
        sa.Column("digest", sa.String(80), nullable=False),
        sa.Column("question", sa.String(None)),
        sa.Column("options_json", sa.String(None)),
        sa.Column("subject_json", sa.String(None)),
        sa.Column("allow_chat", sa.Boolean, nullable=False),
        sa.Column("allow_always", sa.Boolean, nullable=False),
        sa.Column("conversation_id", sa.String(64), index=True),
        sa.Column("branch_id", sa.String(64)),
        sa.Column("message_id", sa.String(64)),
        sa.Column("run_id", sa.String(64)),
        sa.Column("workflow_id", sa.String(255)),
        sa.Column("decision", sa.String(24)),
        sa.Column("answer", sa.String(None)),
        sa.Column("note", sa.String(None)),
        sa.Column("decided_by", sa.String(255)),
        sa.Column("decided_by_email", sa.String(320)),
        sa.Column("decided_via", sa.String(24)),
        sa.Column("continued", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("decided_at", sa.Float),
    )
    op.create_table(
        "hz_hitl_grants",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("hub_id", sa.String(255), nullable=False, index=True),
        sa.Column("scope", sa.String(8), nullable=False),
        sa.Column("account_id", sa.String(255), nullable=False),
        sa.Column("tool", sa.String(255), nullable=False),
        sa.Column("digest", sa.String(80)),
        sa.Column("conversation_id", sa.String(64)),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("used_at", sa.Float),
    )
    op.create_table(
        "hz_hitl_events",
        sa.Column("sequence", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("hub_id", sa.String(255), nullable=False, index=True),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("payload_json", sa.String(None), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("hz_hitl_events")
    op.drop_table("hz_hitl_grants")
    op.drop_table("hz_hitl_requests")
