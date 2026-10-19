"""Sign-in safety: MFA, personal API keys, one revoke counter, run identity. Revision op_0032.

* ``hz_users.auth_epoch``: the account's credential counter. Disabling,
  deleting, resetting a password or "revoke all credentials" moves it in the
  same transaction, so every API key and MCP grant issued before stops working
  on every instance. ``hz_users.deleted_at``: a deleted account is kept as a
  tombstone (its conversations and run history stay attached to its id).
* ``hz_mfa_factors`` / ``hz_mfa_recovery``: one authenticator per person (the
  secret encrypted with the deployment key) and ten one-use recovery codes
  (SHA-256 digests only).
* ``hz_auth_challenges``: short-lived, single-use sign-in and set-up steps,
  stored by digest and bound to the account and its epoch.
* ``hz_api_keys``: personal keys (digest and a display prefix only).
* ``hz_auth_settings``: deployment-wide sign-in settings (the stored
  deployment id, the MFA policy).
* ``hz_run_identities``: who each run is for, written before any model or tool
  runs and never changed.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0032"
down_revision = "op_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("hz_users") as batch:
        batch.add_column(sa.Column("auth_epoch", sa.Integer, nullable=False, server_default="0"))
        batch.add_column(sa.Column("deleted_at", sa.Float))
    with op.batch_alter_table("hz_sessions") as batch:
        batch.add_column(sa.Column("mfa_at", sa.Float))
    op.create_table(
        "hz_mfa_factors",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("secret", sa.Text, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("last_step", sa.BigInteger, nullable=False, server_default="0"),
    )
    op.create_table(
        "hz_mfa_recovery",
        sa.Column("code_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("used_at", sa.Float),
    )
    op.create_table(
        "hz_auth_challenges",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("auth_epoch", sa.Integer, nullable=False),
        sa.Column("data", sa.Text),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("used_at", sa.Float),
    )
    op.create_table(
        "hz_api_keys",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("prefix", sa.String(24), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("scopes", sa.Text, nullable=False),
        sa.Column("hubs", sa.Text),
        sa.Column("auth_epoch", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("expires_at", sa.Float),
        sa.Column("last_used_at", sa.Float),
        sa.Column("revoked_at", sa.Float),
    )
    op.create_table(
        "hz_auth_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.Column("updated_by", sa.String(320)),
    )
    op.create_table(
        "hz_run_identities",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("run_kind", sa.String(16), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("hub", sa.String(255), nullable=False),
        sa.Column("acts_as", sa.String(64)),
        sa.Column("acts_as_email", sa.String(320)),
        sa.Column("requested_by", sa.String(320)),
        sa.Column("requested_by_kind", sa.String(16), nullable=False),
        sa.Column("started_from", sa.String(16), nullable=False),
        sa.Column("origin_id", sa.String(128)),
        sa.Column("person_present", sa.Integer, nullable=False, server_default="0"),
        sa.Column("credential_kind", sa.String(16)),
        sa.Column("credential_id", sa.String(64)),
        sa.Column("scope", sa.Text),
        sa.Column("auth_epoch", sa.Integer),
        sa.Column("parent_id", sa.String(40)),
        sa.Column("legacy", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.UniqueConstraint("run_kind", "run_id", name="hz_run_identities_run"),
    )
    op.create_index("hz_run_identities_acts_as", "hz_run_identities", ["acts_as", "created_at"])


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
