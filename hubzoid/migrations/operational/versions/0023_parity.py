"""Chat parity ledger. Revision op_0023.

* ``hz_parity_candidates``: an exact bundle (manifest, ledger, sources,
  providers, redacted inventories), its digest and validation report.
* ``hz_parity_reviews``: a person's decision bound to a candidate's digest.
* ``hz_parity_freezes``: a sealed, approved candidate. Never changed.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0023"
down_revision = "op_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_parity_candidates",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('deployment_id', sa.String(64), nullable=False),
        sa.Column('base_id', sa.String(64)),
        sa.Column('digest', sa.String(80), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('created_by', sa.String(320), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('bundle_json', sa.String(None), nullable=False),
        sa.Column('report_json', sa.String(None), nullable=False),
    )
    op.create_table(
        "hz_parity_reviews",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('candidate_id', sa.String(64), nullable=False),
        sa.Column('digest', sa.String(80), nullable=False),
        sa.Column('actor', sa.String(320), nullable=False),
        sa.Column('decision', sa.String(16), nullable=False),
        sa.Column('reason', sa.String(None)),
        sa.Column('signed', sa.Integer, nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
    )
    op.create_table(
        "hz_parity_freezes",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('deployment_id', sa.String(64), nullable=False),
        sa.Column('candidate_id', sa.String(64), nullable=False),
        sa.Column('digest', sa.String(80), nullable=False),
        sa.Column('review_id', sa.String(64), nullable=False),
        sa.Column('actor', sa.String(320), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.UniqueConstraint('candidate_id', name="hz_parity_freezes_candidate"),
    )


def downgrade() -> None:
    op.drop_table("hz_parity_freezes")
    op.drop_table("hz_parity_reviews")
    op.drop_table("hz_parity_candidates")
