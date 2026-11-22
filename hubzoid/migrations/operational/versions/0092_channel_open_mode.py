"""Channel open mode: "Anyone in this Slack workspace" per connection
(``hubzoid.channels.open_mode``).

One row per connection that ever had the switch set: whether it is on, the
guests' daily cap, and the Slack workspace and app it was approved for.
Additive: a new table only, so an older Hubzoid keeps working during a deploy.

Revision ID: op_0092
Revises: op_0091
"""
import sqlalchemy as sa
from alembic import op

revision = "op_0092"
down_revision = "op_0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_channel_open_mode",
        sa.Column("hub", sa.Text, primary_key=True),
        sa.Column("connection_id", sa.Text, primary_key=True),
        sa.Column("enabled", sa.Integer, nullable=False, server_default="0"),
        sa.Column("daily_cap", sa.Integer, nullable=False, server_default="20"),
        sa.Column("team", sa.Text),           # the Slack workspace the approval is for
        sa.Column("app", sa.Text),            # the Slack app the approval is for
        sa.Column("updated_by", sa.Text),
        sa.Column("updated", sa.Float(precision=53)),
    )


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
