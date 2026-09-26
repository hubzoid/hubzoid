"""A report share names the chat-app account it was made for.

`hz_artifact_shares.account` holds the Open WebUI account id the shared person
had when the share was made, so a replacement account that reuses the email
does not inherit access to reports shared with the previous person. Existing
rows get NULL (bound to the email only, as before).

Revision ID: op_0006
Revises: op_0005
"""
import sqlalchemy as sa
from alembic import op

revision = "op_0006"
down_revision = "op_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("hz_artifact_shares") as batch:
        batch.add_column(sa.Column("account", sa.Text))


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
