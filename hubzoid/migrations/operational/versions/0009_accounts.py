"""Hubzoid accounts: people, external sign-in identities, sessions, one-time
links and sign-in rate limits.

Replaces the accounts Open WebUI held in 1.0.x. ``hz_users.id`` keeps the id a
migrated person had in Open WebUI, so ``hz_identities.owui_id``, workflow
``run_as`` bindings and report shares stay valid without a rewrite. New people
get a random id. Secrets are never stored in clear: passwords as argon2 (or a
migrated bcrypt hash upgraded at next sign-in), session and link tokens as
SHA-256 digests.

Revision ID: op_0009
Revises: op_0008
"""
import sqlalchemy as sa
from alembic import op

revision = "op_0009"
down_revision = "op_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_users",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.Text),
        # 'admin' | 'user'
        sa.Column("role", sa.String(16), nullable=False, server_default="user"),
        # 'active' | 'pending' (suspension stays in hz_meta, see access.store)
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("password_hash", sa.Text),
        # 0 = this person signs in with an external provider only
        sa.Column("password_enabled", sa.Integer, nullable=False, server_default="1"),
        # 'local' | 'admin' | 'signup' | 'bootstrap' | 'migrated' | 'oidc'
        sa.Column("source", sa.String(16), nullable=False, server_default="admin"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.Column("last_login_at", sa.Float),
    )
    op.create_index("hz_users_email", "hz_users", ["email"], unique=True)

    op.create_table(
        "hz_user_identities",
        # 'google' | 'microsoft' | 'oidc'
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("issuer", sa.String(512), primary_key=True),
        sa.Column("subject", sa.String(255), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("email", sa.String(320)),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("last_login_at", sa.Float),
    )
    op.create_index("hz_user_identities_user", "hz_user_identities", ["user_id"])

    op.create_table(
        "hz_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("last_seen_at", sa.Float, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("idle_seconds", sa.Integer, nullable=False),
        sa.Column("user_agent", sa.Text),
        sa.Column("ip", sa.String(64)),
        # 'password' | 'google' | 'microsoft' | 'oidc' | 'link' | 'local'
        sa.Column("method", sa.String(16)),
        sa.Column("revoked_at", sa.Float),
    )
    op.create_index("hz_sessions_user", "hz_sessions", ["user_id"])

    op.create_table(
        "hz_auth_links",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        # 'set_password' | 'reset_password'
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("created_by", sa.String(320)),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("used_at", sa.Float),
    )
    op.create_index("hz_auth_links_user", "hz_auth_links", ["user_id"])

    op.create_table(
        "hz_auth_attempts",
        # 'ip:<address>' or 'email:<normalized email>'
        sa.Column("key", sa.String(400), primary_key=True),
        sa.Column("window_start", sa.Float, nullable=False),
        sa.Column("failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("locked_until", sa.Float),
    )


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
