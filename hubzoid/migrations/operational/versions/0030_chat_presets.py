"""Chat presets. Revision op_0030.

* ``hz_chat_presets``: a person's named model choices (a model, or models to
  compare, and parameters), revisioned. Shared presets are hub files.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0030"
down_revision = "op_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_chat_presets",
        sa.Column('hub_id', sa.String(255), primary_key=True),
        sa.Column('owner_id', sa.String(64), primary_key=True),
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('body_json', sa.String(None), nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("hz_chat_presets")
