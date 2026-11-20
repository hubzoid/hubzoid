"""Signing keys for the MCP authorization server's JWT access tokens (RFC 9068).

One key set per deployment. The private key is encrypted with the deployment
key (``secretbox``); the public JWK is published at the issuer's ``/jwks``.
"""

import sqlalchemy as sa
from alembic import op

revision = "op_0082"
down_revision = "op_0081"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "hz_mcp_signing_keys",
        sa.Column("kid", sa.String(64), primary_key=True),
        sa.Column("private_key", sa.Text, nullable=False),   # secretbox-encrypted PEM
        sa.Column("public_jwk", sa.Text, nullable=False),    # JSON, no private parts
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("superseded_at", sa.Float, nullable=True),  # a newer key signs since
        sa.Column("retired_at", sa.Float, nullable=True),     # no longer verifies
    )


def downgrade():
    raise NotImplementedError(
        "Hubzoid migrations are forward-only; restore a backup instead"
    )
