"""The running cost of a branch's Claude session. Revision op_0082.

Claude Code reports ``total_cost_usd`` for the whole session, not the turn,
when a turn resumes a session (seen with SDK 0.2.178, Claude Code 2.1.318).
Hubzoid resumes the branch's bound session on every chat turn, so each usage
row held the session's running total and the Console added the same money up
again on every turn.

``hz_chat_runtime_bindings.native_cost_usd`` keeps the total the session had
reached at the end of the last run, so the next resumed turn records only the
difference. NULL means not known (a binding written before this revision, or
a runtime that reports no cost). Existing rows are not changed.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0082"
down_revision = "op_0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("hz_chat_runtime_bindings") as batch:
        batch.add_column(sa.Column("native_cost_usd", sa.Float, nullable=True))


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
