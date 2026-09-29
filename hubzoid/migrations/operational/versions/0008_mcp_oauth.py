"""Durable MCP OAuth credentials and grants (opaque secrets stored as digests)."""

import sqlalchemy as sa
from alembic import op

revision = "op_0008"
down_revision = "op_0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "hz_mcp_oauth",
        sa.Column("namespace", sa.String(512), primary_key=True),
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("expires", sa.BigInteger, nullable=False),
        sa.Column("used", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("hz_mcp_oauth_expiry", "hz_mcp_oauth", ["namespace", "expires"])


def downgrade():
    raise NotImplementedError(
        "Hubzoid migrations are forward-only; restore a backup instead"
    )
