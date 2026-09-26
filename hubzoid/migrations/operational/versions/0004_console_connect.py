"""Console change requests, connection journeys, and richer access audit.

- `hz_change_requests`: a proposed access or account change (from an agent tool
  or the API) waiting for the proposing manager to confirm it in the Console.
- `hz_connect_states`: one row per "connect my <app>" journey, bound to the
  person and chat that asked for it.
- `hz_access_audit` gains `surface` and `request_id`, so a change made through a
  confirmed request can be traced to where it was proposed.

Revision ID: op_0004
Revises: op_0003
"""
import sqlalchemy as sa
from alembic import op

revision = "op_0004"
down_revision = "op_0003"
branch_labels = None
depends_on = None


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.Float(precision=53), nullable=nullable)  # UTC epoch seconds


def upgrade() -> None:
    op.create_table(
        "hz_change_requests",
        sa.Column("id", sa.Text, primary_key=True),
        _ts("created", nullable=False),
        _ts("expires", nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("surface", sa.Text),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("hub", sa.Text),
        sa.Column("target", sa.Text),
        sa.Column("plan", sa.Text, nullable=False),  # JSON
        sa.Column("plan_hash", sa.Text, nullable=False),
        sa.Column("base_revision", sa.Integer),
        # pending|applying|confirmed|rejected|expired|failed
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        _ts("decided"),
        sa.Column("decided_by", sa.Text),
        sa.Column("result", sa.Text),
    )
    op.create_index("hz_change_requests_actor_status", "hz_change_requests", ["actor", "status"])
    op.create_index("hz_change_requests_expires", "hz_change_requests", ["expires"])

    op.create_table(
        "hz_connect_states",
        sa.Column("id", sa.Text, primary_key=True),
        _ts("created", nullable=False),
        _ts("expires", nullable=False),
        sa.Column("hub", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("surface", sa.Text),
        sa.Column("chat_id", sa.Text),
        sa.Column("handle", sa.Text),
        sa.Column("app", sa.Text, nullable=False),
        sa.Column("provider", sa.Text),
        sa.Column("provider_ref", sa.Text),
        # pending|started|connected|cancelled|failed|expired|superseded
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        _ts("started"),
        _ts("finished"),
        sa.Column("continuation", sa.Text),
        # none|offered|used|declined|expired
        sa.Column("continuation_status", sa.Text, nullable=False, server_default="none"),
        _ts("notified"),
    )
    op.create_index("hz_connect_states_hub_status", "hz_connect_states", ["hub", "status"])
    op.create_index("hz_connect_states_subject_app_status", "hz_connect_states", ["subject", "app", "status"])

    with op.batch_alter_table("hz_access_audit") as batch:
        batch.add_column(sa.Column("surface", sa.Text))
        batch.add_column(sa.Column("request_id", sa.Text))


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
