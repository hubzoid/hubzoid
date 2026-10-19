"""Customer branding with reviewed publication. Revision op_0021.

Drafts (complete candidates with a digest), stored assets (content-addressed),
reviews bound to a digest, immutable activated revisions, the active pointer
moved by compare-and-set on its generation, the activation log, and each
person's terms acceptance and announcement dismissals.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0021"
down_revision = "op_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_branding_drafts",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('deployment_id', sa.String(64), nullable=False),
        sa.Column('base_revision', sa.String(64), nullable=False),
        sa.Column('configuration_json', sa.String(None), nullable=False),
        sa.Column('theme_json', sa.String(None), nullable=False),
        sa.Column('assets_json', sa.String(None), nullable=False),
        sa.Column('digest', sa.String(80), nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('validation_json', sa.String(None), nullable=False),
        sa.Column('creator', sa.String(320), nullable=False),
        sa.Column('validator_version', sa.String(32), nullable=False),
        sa.Column('source', sa.String(32), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
    )
    op.create_table(
        "hz_branding_assets",
        sa.Column('deployment_id', sa.String(64), primary_key=True),
        sa.Column('digest', sa.String(80), primary_key=True),
        sa.Column('media_type', sa.String(64), nullable=False),
        sa.Column('byte_size', sa.Integer, nullable=False),
        sa.Column('width', sa.Integer),
        sa.Column('height', sa.Integer),
        sa.Column('storage_key', sa.String(255), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
    )
    op.create_table(
        "hz_branding_reviews",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('deployment_id', sa.String(64), nullable=False),
        sa.Column('draft_id', sa.String(64), nullable=False),
        sa.Column('digest', sa.String(80), nullable=False),
        sa.Column('draft_revision', sa.Integer, nullable=False),
        sa.Column('base_revision', sa.String(64), nullable=False),
        sa.Column('validator_version', sa.String(32), nullable=False),
        sa.Column('actor', sa.String(320), nullable=False),
        sa.Column('decision', sa.String(16), nullable=False),
        sa.Column('reason', sa.String(None)),
        sa.Column('created_at', sa.Float, nullable=False),
    )
    op.create_table(
        "hz_branding_revisions",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('deployment_id', sa.String(64), nullable=False),
        sa.Column('digest', sa.String(80), nullable=False),
        sa.Column('configuration_json', sa.String(None), nullable=False),
        sa.Column('theme_json', sa.String(None), nullable=False),
        sa.Column('assets_json', sa.String(None), nullable=False),
        sa.Column('css_text', sa.String(None)),
        sa.Column('validator_version', sa.String(32), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.UniqueConstraint('deployment_id', 'digest', name="hz_branding_revisions_digest"),
    )
    op.create_table(
        "hz_branding_active",
        sa.Column('deployment_id', sa.String(64), primary_key=True),
        sa.Column('revision_id', sa.String(64), nullable=False),
        sa.Column('generation', sa.Integer, nullable=False),
    )
    op.create_table(
        "hz_branding_activations",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('deployment_id', sa.String(64), nullable=False),
        sa.Column('from_revision', sa.String(64)),
        sa.Column('to_revision', sa.String(64), nullable=False),
        sa.Column('review_id', sa.String(64), nullable=False),
        sa.Column('actor', sa.String(320), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('generation', sa.Integer, nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
    )
    op.create_table(
        "hz_branding_legal",
        sa.Column('deployment_id', sa.String(64), primary_key=True),
        sa.Column('user_id', sa.String(64), primary_key=True),
        sa.Column('policy_hash', sa.String(80), primary_key=True),
        sa.Column('policy_version', sa.String(80), nullable=False),
        sa.Column('accepted_at', sa.Float, nullable=False),
    )
    op.create_table(
        "hz_branding_dismissals",
        sa.Column('deployment_id', sa.String(64), primary_key=True),
        sa.Column('user_id', sa.String(64), primary_key=True),
        sa.Column('announcement_id', sa.String(80), primary_key=True),
        sa.Column('dismissed_at', sa.Float, nullable=False),
    )
    op.create_index("hz_branding_drafts_dep", "hz_branding_drafts", ["deployment_id", "updated_at"])
    op.create_index("hz_branding_activations_dep", "hz_branding_activations", ["deployment_id", "created_at"])


def downgrade() -> None:
    op.drop_index("hz_branding_activations_dep", table_name="hz_branding_activations")
    op.drop_index("hz_branding_drafts_dep", table_name="hz_branding_drafts")
    op.drop_table("hz_branding_dismissals")
    op.drop_table("hz_branding_legal")
    op.drop_table("hz_branding_activations")
    op.drop_table("hz_branding_active")
    op.drop_table("hz_branding_revisions")
    op.drop_table("hz_branding_reviews")
    op.drop_table("hz_branding_assets")
    op.drop_table("hz_branding_drafts")
