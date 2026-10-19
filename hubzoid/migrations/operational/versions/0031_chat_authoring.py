"""Workspace authoring. Revision op_0031.

* ``hz_authoring_drafts``: a proposed change to one hub file (agent
  instructions, a skill, a shared prompt): the file it started from, the
  content, its digest and review state.
* ``hz_authoring_versions``: every published version of a file, for rollback.
* ``hz_personal_prompts``: a person's own prompts with variables.
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
        "hz_authoring_drafts",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('path', sa.String(512), nullable=False),
        sa.Column('base_sha256', sa.String(64)),
        sa.Column('content', sa.String(None), nullable=False),
        sa.Column('digest', sa.String(80), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('author_id', sa.String(64), nullable=False),
        sa.Column('reviewer_id', sa.String(64)),
        sa.Column('note', sa.String(None)),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
    )
    op.create_table(
        "hz_authoring_versions",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('path', sa.String(512), nullable=False, index=True),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('content', sa.String(None), nullable=False),
        sa.Column('previous_sha256', sa.String(64)),
        sa.Column('draft_id', sa.String(64)),
        sa.Column('published_by', sa.String(64), nullable=False),
        sa.Column('published_at', sa.Float, nullable=False),
    )
    op.create_table(
        "hz_personal_prompts",
        sa.Column('hub_id', sa.String(255), primary_key=True),
        sa.Column('owner_id', sa.String(64), primary_key=True),
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('content', sa.String(None), nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("hz_personal_prompts")
    op.drop_table("hz_authoring_versions")
    op.drop_table("hz_authoring_drafts")
