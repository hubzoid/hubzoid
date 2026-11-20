"""Runner calls: one work order per call from a hub service to its AgentCore
runner (``hubzoid.runner.calls``).

The hub service writes the row before it invokes the runner. The runner claims
it once, only when the request matches ``request_digest``, checks ``state``
before every tool step and ends it. ``cancel_requested`` stops a running call.
Additive: a new table only, so an older Hubzoid keeps working during a deploy.

Revision ID: op_0091
Revises: op_0084
"""
import sqlalchemy as sa
from alembic import op

revision = "op_0091"
down_revision = "op_0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_runner_calls",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("hub", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),               # turn | agent | hello
        sa.Column("session_id", sa.Text, nullable=False),         # the AgentCore session
        sa.Column("state", sa.Text, nullable=False),              # created | running | cancel_requested | ended
        sa.Column("request_digest", sa.Text, nullable=False),     # SHA-256 of the request (protocol.digest)
        sa.Column("identity", sa.Text),                           # JSON: the caller the hub service admitted
        sa.Column("created_at", sa.Float(precision=53), nullable=False),
        sa.Column("claimed_at", sa.Float(precision=53)),
        sa.Column("heartbeat_at", sa.Float(precision=53)),     # the hub service still waits for it
        sa.Column("ended_at", sa.Float(precision=53)),
        sa.Column("outcome", sa.Text),                            # ok | error | cancelled | refused | lost
        sa.Column("runner_version", sa.Text),                     # Hubzoid version and image of the runner
        sa.Column("error", sa.Text),
    )
    op.create_index("hz_runner_calls_state", "hz_runner_calls", ["hub", "state"])
    op.create_index("hz_runner_calls_created", "hz_runner_calls", ["created_at"])


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
