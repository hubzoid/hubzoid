"""Cards (MCP Apps). Revision op_0054.

``hz_app_config_revisions``: each applied card configuration, with its digest;
one is active per hub. ``hz_app_resources``: card HTML by SHA-256, kept while
any instance refers to it, so an old message keeps its card after an upgrade.
``hz_app_instances``: one row per card a tool returned, with the result it
showed, who asked for it, and whether it was revoked.

Tables only: no existing row is read or changed.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0054"
down_revision = "op_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_app_config_revisions",
        sa.Column("hub", sa.String(255), primary_key=True),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("digest", sa.String(80), nullable=False),
        sa.Column("config_json", sa.Text, nullable=False),
        sa.Column("actor", sa.String(320), nullable=False),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("active", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_table(
        "hz_app_resources",
        sa.Column("hub", sa.String(255), primary_key=True),
        sa.Column("sha256", sa.String(64), primary_key=True),
        sa.Column("card_id", sa.String(64), nullable=False),
        sa.Column("mime", sa.String(64), nullable=False),
        sa.Column("html", sa.Text, nullable=False),
        sa.Column("created", sa.Float, nullable=False),
    )
    op.create_table(
        "hz_app_instances",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("hub", sa.String(255), nullable=False),
        sa.Column("card_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("server_id", sa.String(64), nullable=False),
        sa.Column("tool", sa.String(255), nullable=False),
        sa.Column("config_revision", sa.Integer, nullable=False),
        sa.Column("resource_sha256", sa.String(64), nullable=False),
        sa.Column("requester", sa.String(320), nullable=False),
        sa.Column("surface", sa.String(32)),
        sa.Column("conversation_id", sa.String(255)),
        sa.Column("arguments_json", sa.Text, nullable=False),
        sa.Column("result_json", sa.Text, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("revoked", sa.Float),
        sa.Column("revoked_by", sa.String(320)),
        sa.Column("revoke_reason", sa.String(500)),
    )
    op.create_index("hz_app_instances_hub", "hz_app_instances", ["hub", "created"])
    op.create_index("hz_app_instances_resource", "hz_app_instances", ["hub", "resource_sha256"])


def downgrade() -> None:
    op.drop_index("hz_app_instances_resource", table_name="hz_app_instances")
    op.drop_index("hz_app_instances_hub", table_name="hz_app_instances")
    op.drop_table("hz_app_instances")
    op.drop_table("hz_app_resources")
    op.drop_table("hz_app_config_revisions")
