"""Secrets set in the Console, the CLI or the API. Revision op_0040.

* ``hz_secrets``: one row per stored secret, its value encrypted with the
  deployment key (``hubzoid.secretbox``). ``scope`` is ``deployment``, ``hub``
  or ``channel``. ``target`` is '' for the deployment, the hub key for a hub,
  and ``<hub>/<channel>`` for a channel. ``restricted`` marks a hub secret that
  joins the restricted layer. ``destination`` is the digest of where the value
  may be sent (an Azure endpoint and API version), or null. Values are never
  read back through any API.

Numbered 0040 so it does not collide with other 2.0 work in progress. Whichever
branch merges second points its ``down_revision`` at the other.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0040"
down_revision = "op_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_secrets",
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("target", sa.String(320), nullable=False, server_default=""),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("restricted", sa.Integer, nullable=False, server_default="0"),
        sa.Column("value_enc", sa.Text, nullable=False),
        sa.Column("destination", sa.String(80)),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(320), nullable=False),
        sa.Column("updated_by", sa.String(320), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.PrimaryKeyConstraint("scope", "target", "name", name="hz_secrets_pk"),
    )


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
