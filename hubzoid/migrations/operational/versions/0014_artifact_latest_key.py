"""A published artifact may carry a latest key.

`hz_artifacts.latest_key` names a series of publishes (a board a workflow
republishes on a schedule). `/portal/latest/<hub>/<key>` opens the newest one.
Existing rows get NULL: they belong to no series.

Revision ID: op_0014
Revises: op_0013
"""
import sqlalchemy as sa
from alembic import op

revision = "op_0014"
down_revision = "op_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("hz_artifacts") as batch:
        batch.add_column(sa.Column("latest_key", sa.Text))
    op.create_index("hz_artifacts_latest", "hz_artifacts", ["hub", "latest_key", "created"])


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
