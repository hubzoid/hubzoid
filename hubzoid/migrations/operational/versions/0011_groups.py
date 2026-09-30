"""Groups of people that can hold access like a person.

A grant to ``group:<id>`` in ``hz_grants`` applies to every member. Members are
recorded by normalized email, the same subject the rest of the access store uses.

Revision ID: op_0011
Revises: op_0010
"""
import sqlalchemy as sa
from alembic import op

revision = "op_0011"
down_revision = "op_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_groups",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text),
        # 'console' | 'migrated'
        sa.Column("source", sa.String(16), nullable=False, server_default="console"),
        sa.Column("created_by", sa.String(320)),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_index("hz_groups_name", "hz_groups", ["name"], unique=True)

    op.create_table(
        "hz_group_members",
        sa.Column("group_id", sa.String(64), primary_key=True),
        sa.Column("email", sa.String(320), primary_key=True),
        sa.Column("added_by", sa.String(320)),
        sa.Column("added_at", sa.Float, nullable=False),
    )
    op.create_index("hz_group_members_email", "hz_group_members", ["email"])


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
