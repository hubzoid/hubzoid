"""Join the feedback and one-home histories without rewriting either branch.

Both op_0073 and op_0080 have been used independently. Alembic runs the
missing branch before this join, preserving the one-home export guard and
adding feedback columns to databases that already completed the cleanup.
"""
from __future__ import annotations

revision = "op_0081"
down_revision = ("op_0073", "op_0080")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
