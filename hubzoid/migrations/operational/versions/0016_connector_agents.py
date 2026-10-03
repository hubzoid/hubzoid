"""Which agents offer each personal connector.

A connector is registered once for the deployment (one OAuth client, one
connection per person) and offered in the agents listed here. Its
`connector_<id>` capability exists only in those agents, and a person's
connection is used only there. Existing connectors are offered in every agent
the store already knows (each hub with a grant), so nothing changes on upgrade.

Revision ID: op_0016
Revises: op_0015
"""
import time

import sqlalchemy as sa
from alembic import op

revision = "op_0016"
down_revision = "op_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_connector_agents",
        sa.Column("connector_id", sa.String(64), primary_key=True),
        sa.Column("hub", sa.String(255), primary_key=True),
        sa.Column("added_by", sa.String(320)),
        sa.Column("added_at", sa.Float, nullable=False),
    )
    op.create_index("hz_connector_agents_hub", "hz_connector_agents", ["hub"])
    conn = op.get_bind()
    connectors = [r[0] for r in conn.execute(sa.text("SELECT id FROM hz_connectors"))]
    hubs = [r[0] for r in conn.execute(sa.text(
        "SELECT DISTINCT hub FROM hz_grants WHERE hub <> '*'"))]
    now = time.time()
    for cid in connectors:
        for hub in hubs:
            conn.execute(sa.text("INSERT INTO hz_connector_agents (connector_id, hub, added_by, "
                                 "added_at) VALUES (:c, :h, 'migration', :t)"),
                         {"c": cid, "h": hub, "t": now})


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
