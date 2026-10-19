"""Chat configuration and idempotent writes. Revision op_0020.

* ``hz_chat_operations``: one row per ``Idempotency-Key`` (scoped to
  deployment, hub, caller and route), holding the request hash and the first
  response, kept at least 24 hours. Shared by every chat and control-plane
  route.
* ``hz_chat_config_revisions``: immutable applied ``chat/config.yaml``
  documents, numbered per deployment.
* ``hz_chat_config_heads``: the active revision per deployment, moved by
  compare-and-set.
* ``hz_chat_preferences``: one person's presentation overrides, with their own
  revision.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0020"
down_revision = "op_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_chat_operations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("deployment_id", sa.String(64), nullable=False),
        sa.Column("hub_id", sa.String(255), nullable=False),
        sa.Column("actor_id", sa.String(320), nullable=False),
        sa.Column("route", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("resource_refs_json", sa.Text),
        sa.Column("result_json", sa.Text),
        sa.Column("error_json", sa.Text),
        sa.Column("operation_id", sa.String(64)),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.UniqueConstraint("deployment_id", "hub_id", "actor_id", "route", "idempotency_key",
                            name="hz_chat_operations_scope"),
    )
    op.create_index("hz_chat_operations_expires", "hz_chat_operations", ["expires_at"])
    op.create_table(
        "hz_chat_config_revisions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("deployment_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("namespace_versions", sa.Text, nullable=False),
        sa.Column("document_json", sa.Text, nullable=False),
        sa.Column("digest", sa.String(80), nullable=False),
        sa.Column("created_by", sa.String(320), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.UniqueConstraint("deployment_id", "revision", name="hz_chat_config_revisions_rev"),
    )
    op.create_table(
        "hz_chat_config_heads",
        sa.Column("scope_key", sa.String(64), primary_key=True),
        sa.Column("revision_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
    )
    op.create_table(
        "hz_chat_preferences",
        sa.Column("deployment_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("values_json", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("hz_chat_preferences")
    op.drop_table("hz_chat_config_heads")
    op.drop_table("hz_chat_config_revisions")
    op.drop_index("hz_chat_operations_expires", table_name="hz_chat_operations")
    op.drop_table("hz_chat_operations")
