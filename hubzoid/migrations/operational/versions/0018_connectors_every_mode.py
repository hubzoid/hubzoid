"""Console connectors in every UI mode. Revision op_0018.

* ``hz_connect_states.account``: the account id a connection link was made
  for, so a link works only for that account (not just the same email).
* ``hz_connectors.shared_header`` and ``shared_secret_enc``: the header name
  and the encrypted value of a Shared key connector (one company account).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0018"
down_revision = "op_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("hz_connect_states") as batch:
        batch.add_column(sa.Column("account", sa.String(64)))
    with op.batch_alter_table("hz_connectors") as batch:
        batch.add_column(sa.Column("shared_header", sa.String(64)))
        batch.add_column(sa.Column("shared_secret_enc", sa.Text))


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
