"""Conversations, messages and read-only shares for the Hubzoid web app.

A conversation belongs to one person and one agent (one hub). Messages form a
tree through ``parent_id`` so edits and regenerations keep earlier versions;
``head_id`` records the branch the person last looked at. ``content`` holds the
message parts as JSON (text, reasoning, tool calls, attachments); ``text`` is a
plain-text copy for search. A share stores a snapshot of one branch, so later
edits never leak into a link someone already opened.

Revision ID: op_0010
Revises: op_0009
"""
import sqlalchemy as sa
from alembic import op

revision = "op_0010"
down_revision = "op_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_conversations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("owner_email", sa.String(320), nullable=False),
        # the hub's key (folder name, lowercase) and the agent's model id
        sa.Column("hub", sa.String(255), nullable=False),
        sa.Column("agent", sa.String(255), nullable=False),
        sa.Column("title", sa.Text),
        # 'pending' | 'auto' | 'user' | 'fallback' | 'migrated'
        sa.Column("title_source", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("archived", sa.Integer, nullable=False, server_default="0"),
        sa.Column("head_id", sa.String(64)),
        # 'web' | 'migrated'
        sa.Column("source", sa.String(16), nullable=False, server_default="web"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_index("hz_conversations_owner", "hz_conversations",
                    ["owner_id", "archived", "updated_at"])

    op.create_table(
        "hz_messages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("conversation_id", sa.String(64), nullable=False),
        sa.Column("parent_id", sa.String(64)),
        # 'user' | 'assistant'
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("text", sa.Text),
        # 'complete' | 'running' | 'cancelled' | 'error'
        sa.Column("status", sa.String(16), nullable=False, server_default="complete"),
        sa.Column("error", sa.Text),
        sa.Column("model", sa.String(255)),
        sa.Column("usage", sa.Text),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_index("hz_messages_conversation", "hz_messages", ["conversation_id", "created_at"])
    op.create_index("hz_messages_parent", "hz_messages", ["parent_id"])

    op.create_table(
        "hz_shares",
        # the public share token used in /s/<id>
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("conversation_id", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("title", sa.Text),
        sa.Column("agent", sa.String(255)),
        sa.Column("snapshot", sa.Text, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("hz_shares_conversation", "hz_shares", ["conversation_id"])


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
