"""Personal connections: remote MCP servers an administrator registers, each
person's OAuth tokens for them, and in-flight authorization requests.

Every secret column holds a value encrypted with the deployment key
(``hubzoid.secretbox``), never the clear value.

Revision ID: op_0012
Revises: op_0011
"""
import sqlalchemy as sa
from alembic import op

revision = "op_0012"
down_revision = "op_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_connectors",
        # a short slug: also the app key of the connector_<id> capability
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        # 'oauth' | 'none'
        sa.Column("auth_type", sa.String(16), nullable=False, server_default="oauth"),
        sa.Column("client_id", sa.Text),
        sa.Column("client_secret_enc", sa.Text),
        # discovered metadata and any dynamically registered client (encrypted JSON)
        sa.Column("client_info_enc", sa.Text),
        sa.Column("scopes", sa.Text),
        # JSON list of allowed tool names; empty or null means every tool
        sa.Column("tool_allowlist", sa.Text),
        sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(320)),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )

    op.create_table(
        "hz_connector_tokens",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("connector_id", sa.String(64), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        # encrypted JSON: access_token, refresh_token, token_type, expires_at, scope
        sa.Column("token_enc", sa.Text, nullable=False),
        sa.Column("expires_at", sa.Float),
        # 'ok' | 'expired' | 'error'
        sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
        sa.Column("error", sa.Text),
        # when this authorization was made (a refresh keeps it)
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        # bumped on every write: refreshes compare-and-set on it across processes
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
        # a refresh in progress somewhere holds this lease until the given time
        sa.Column("refresh_lock_until", sa.Float),
    )
    op.create_index("hz_connector_tokens_connector", "hz_connector_tokens", ["connector_id"])

    op.create_table(
        "hz_connector_flows",
        # SHA-256 (hex) of the OAuth state; the state itself is never stored
        sa.Column("state", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("connector_id", sa.String(64), nullable=False),
        # encrypted JSON: PKCE verifier, redirect URI, issuer, resource, scopes
        sa.Column("payload_enc", sa.Text, nullable=False),
        sa.Column("return_to", sa.Text),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
    )
    op.create_index("hz_connector_flows_expires", "hz_connector_flows", ["expires_at"])


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
