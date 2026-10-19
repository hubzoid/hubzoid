"""Company directories: SAML sign-in and SCIM provisioning. Revision op_0033.

* ``hz_users.managed_by``: the directory provider that manages the account
  (it signs in only through that provider and has no local password).
* ``hz_directory_providers``: a SAML identity provider and its settings (the
  identity provider's metadata, how people are matched).
* ``hz_directory_accounts``: one person as the directory provisioned them over
  SCIM: the SCIM id, ``userName``, ``externalId``, active flag and the resource.
* ``hz_scim_tokens``: provider-scoped SCIM bearer tokens (digests only).
* ``hz_saml_replay``: assertion ids already used, until they expire.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0033"
down_revision = "op_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("hz_users") as batch:
        batch.add_column(sa.Column("managed_by", sa.String(32)))
    op.create_table(
        "hz_directory_providers",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Integer, nullable=False, server_default="0"),
        sa.Column("config", sa.Text, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.Column("updated_by", sa.String(320)),
    )
    op.create_table(
        "hz_directory_accounts",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("provider_id", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("user_name", sa.String(320), nullable=False),
        sa.Column("external_id", sa.String(255)),
        sa.Column("active", sa.Integer, nullable=False, server_default="1"),
        sa.Column("resource", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.Column("deleted_at", sa.Float),
        sa.UniqueConstraint("provider_id", "user_id", name="hz_directory_accounts_user"),
    )
    op.create_index("hz_directory_accounts_name", "hz_directory_accounts",
                    ["provider_id", "user_name"])
    op.create_table(
        "hz_scim_tokens",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("provider_id", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("prefix", sa.String(24), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_by", sa.String(320)),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("last_used_at", sa.Float),
        sa.Column("revoked_at", sa.Float),
    )
    op.create_table(
        "hz_saml_replay",
        sa.Column("provider_id", sa.String(32), primary_key=True),
        sa.Column("assertion_hash", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.Float, nullable=False),
    )


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
