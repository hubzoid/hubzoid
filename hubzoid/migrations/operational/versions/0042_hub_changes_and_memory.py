"""Hub changes and personal memory. Revision op_0042.

* ``hz_hub_changes``: a proposed change to a hub's own files (exact edits,
  the bytes each file had when it was proposed, the bytes it will have), its
  review and the commit that applied it.
* ``hz_personal_memory``: one short note per person per hub, which the agent
  reads at the start of each chat.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0042"
down_revision = "op_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_hub_changes",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("hub_id", sa.String(255), nullable=False, index=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("files_json", sa.Text, nullable=False),
        sa.Column("digest", sa.String(80), nullable=False),
        sa.Column("proposed_by", sa.String(320), nullable=False),
        sa.Column("proposed_via", sa.String(24), nullable=False),
        sa.Column("conversation_id", sa.String(64)),
        sa.Column("undoes", sa.Integer),
        sa.Column("decided_by", sa.String(320)),
        sa.Column("note", sa.Text),
        sa.Column("commit_sha", sa.String(64)),
        sa.Column("publish_state", sa.String(24)),
        sa.Column("problem", sa.Text),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_table(
        "hz_personal_memory",
        sa.Column("hub_id", sa.String(255), primary_key=True),
        sa.Column("account_id", sa.String(255), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("hz_personal_memory")
    op.drop_table("hz_hub_changes")
