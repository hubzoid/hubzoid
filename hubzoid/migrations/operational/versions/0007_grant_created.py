"""A grant records when it was made.

`hz_grants.created` (epoch seconds) is set when a grant is inserted. A public
report link stays valid only while its owner has held `share_public_links`
since before the link was made (GrantStore.held_since), so removing that grant
ends the owner's links for good and granting it again does not bring old links
back. Existing rows get NULL: treated as held since before any link, so current
links keep working.

Revision ID: op_0007
Revises: op_0006
"""
import sqlalchemy as sa
from alembic import op

revision = "op_0007"
down_revision = "op_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("hz_grants") as batch:
        batch.add_column(sa.Column("created", sa.Float))


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
