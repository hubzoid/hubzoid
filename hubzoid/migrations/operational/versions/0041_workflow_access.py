"""Workflow access. Revision op_0041.

* ``hz_workflow_access``: who may do which action on which workflow of a hub.
* ``hz_workflow_access_revisions``: every applied change, with what it added
  and removed, for audit and compare-and-set writes.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0041"
down_revision = "op_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_workflow_access",
        sa.Column("hub_id", sa.String(255), primary_key=True),
        sa.Column("subject", sa.String(320), primary_key=True),
        sa.Column("workflow", sa.String(255), primary_key=True),
        sa.Column("action", sa.String(32), primary_key=True),
        sa.Column("granted_by", sa.String(320), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_table(
        "hz_workflow_access_revisions",
        sa.Column("hub_id", sa.String(255), primary_key=True),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("document_sha256", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(320), nullable=False),
        sa.Column("added_json", sa.String(None), nullable=False),
        sa.Column("removed_json", sa.String(None), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("hz_workflow_access_revisions")
    op.drop_table("hz_workflow_access")
