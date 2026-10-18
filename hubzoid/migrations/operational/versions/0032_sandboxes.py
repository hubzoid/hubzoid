"""Sandboxes and their read stamps. Revision op_0032.

* ``hz_sandboxes``: one row per sandbox Hubzoid created, so it can be found
  again after a restart, reported lost when it disappeared, and deleted once.
  It pins the profile it was made from and records the provider resources,
  both network policies, the setup result, checkouts and exported patches.
  ``live_key`` is the owner while the sandbox is in use and NULL once it ends,
  so an owner has at most one live sandbox.
* ``hz_sandbox_read_stamps``: what an agent last read of each file, so a write
  over a file it has not read, or that changed since, is refused.

Tables only: no existing row is read or changed.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0032"
down_revision = "op_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_sandboxes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("hub", sa.String(255), nullable=False),
        sa.Column("deployment", sa.String(64), nullable=False),
        sa.Column("owner_kind", sa.String(32), nullable=False),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("item_id", sa.String(36)),
        sa.Column("live_key", sa.String(300)),
        sa.Column("profile_id", sa.String(64), nullable=False),
        sa.Column("profile_digest", sa.String(80), nullable=False),
        sa.Column("profile_json", sa.Text, nullable=False),
        sa.Column("provider_binding", sa.String(64), nullable=False),
        sa.Column("provider_kind", sa.String(32), nullable=False),
        sa.Column("provider_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("image", sa.String(512)),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("end_reason", sa.String(32)),
        sa.Column("network_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("setup_json", sa.Text),
        sa.Column("checkouts_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("exports_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("error", sa.Text),
        sa.Column("created_by", sa.String(320), nullable=False, server_default=""),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("ready_at", sa.Float),
        sa.Column("last_activity", sa.Float),
        sa.Column("stopped_at", sa.Float),
        sa.Column("ended_at", sa.Float),
        sa.Column("lifetime_deadline", sa.Float, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("updated", sa.Float, nullable=False),
    )
    op.create_index("hz_sandboxes_live", "hz_sandboxes", ["hub", "live_key"], unique=True)
    op.create_index("hz_sandboxes_owner", "hz_sandboxes", ["hub", "owner_kind", "owner_id"])
    op.create_index("hz_sandboxes_state", "hz_sandboxes", ["hub", "state"])

    op.create_table(
        "hz_sandbox_read_stamps",
        sa.Column("sandbox_id", sa.String(36), primary_key=True),
        sa.Column("path", sa.String(1024), primary_key=True),
        sa.Column("sha256", sa.String(80), nullable=False),
        sa.Column("bytes", sa.Integer, nullable=False),
        sa.Column("updated", sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("hz_sandbox_read_stamps")
    op.drop_index("hz_sandboxes_state", table_name="hz_sandboxes")
    op.drop_index("hz_sandboxes_owner", table_name="hz_sandboxes")
    op.drop_index("hz_sandboxes_live", table_name="hz_sandboxes")
    op.drop_table("hz_sandboxes")
