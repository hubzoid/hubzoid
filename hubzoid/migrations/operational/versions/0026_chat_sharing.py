"""Chat sharing. Revision op_0026.

* ``hz_share_manifests``: a published snapshot link: its audience, projection,
  digest, copy policy, expiry, status, and the hashed and encrypted link token.
* ``hz_share_previews``: a private candidate the owner reviews before publishing.
* ``hz_share_copy_operations``: independent copies made from a link.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0026"
down_revision = "op_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_share_manifests",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('conversation_id', sa.String(64), nullable=False, index=True),
        sa.Column('owner_id', sa.String(64), nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('audience_type', sa.String(16), nullable=False),
        sa.Column('audience_json', sa.String(None), nullable=False),
        sa.Column('snapshot_hash', sa.String(80), nullable=False),
        sa.Column('projection_json', sa.String(None), nullable=False),
        sa.Column('source_graph_revision', sa.Integer, nullable=False),
        sa.Column('allow_copy', sa.Integer, nullable=False),
        sa.Column('expires_at', sa.Float),
        sa.Column('token_hash', sa.String(64), nullable=False),
        sa.Column('token_box', sa.String(None), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('revoked_at', sa.Float),
        sa.UniqueConstraint('token_hash', name="uq_hz_share_token"),
    )
    op.create_table(
        "hz_share_previews",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('actor_id', sa.String(64), nullable=False),
        sa.Column('conversation_id', sa.String(64), nullable=False),
        sa.Column('source_revision', sa.Integer, nullable=False),
        sa.Column('payload_hash', sa.String(80), nullable=False),
        sa.Column('payload_json', sa.String(None), nullable=False),
        sa.Column('expires_at', sa.Float, nullable=False),
    )
    op.create_table(
        "hz_share_copy_operations",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('actor_id', sa.String(64), nullable=False),
        sa.Column('share_id', sa.String(64), nullable=False),
        sa.Column('digest', sa.String(80), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('new_conversation_id', sa.String(64)),
        sa.Column('omissions_json', sa.String(None)),
        sa.Column('error_code', sa.String(64)),
        sa.Column('created_at', sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("hz_share_copy_operations")
    op.drop_table("hz_share_previews")
    op.drop_table("hz_share_manifests")
