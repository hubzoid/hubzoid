"""Usage details. Revision op_0083.

``hz_usage`` gains five nullable columns for the Usage page:

* ``origin``: what started the call (named so because TRIGGER is an SQL
  keyword). chat, schedule, manual, event, mcp or
  eval. NULL for rows written before this revision.
* ``cache_read_tokens`` and ``cache_write_tokens``: input tokens read from and
  written to the provider's prompt cache. Both are part of ``input_tokens``.
* ``reasoning_tokens``: output tokens spent reasoning, part of
  ``output_tokens``. NULL when the provider does not report them (Claude).
* ``first_token_ms``: chat turns only, time to the first answer text.

``input_tokens`` and ``output_tokens`` keep their meaning, so budgets and cost
estimates are unchanged. Existing rows are not changed.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0083"
down_revision = "op_0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("hz_usage") as batch:
        batch.add_column(sa.Column("origin", sa.Text))
        batch.add_column(sa.Column("cache_read_tokens", sa.BigInteger))
        batch.add_column(sa.Column("cache_write_tokens", sa.BigInteger))
        batch.add_column(sa.Column("reasoning_tokens", sa.BigInteger))
        batch.add_column(sa.Column("first_token_ms", sa.Integer))


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
