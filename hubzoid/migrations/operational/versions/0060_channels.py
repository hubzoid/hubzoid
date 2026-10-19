"""Channel connections, threads, deliveries and account links. Revision op_0060.

Queues, leases and run state stay where they already live: the chat engine for
agent threads, the work item records and DBOS for workflow threads, and the
operation record for every reply sent to a provider. These tables hold only
what those do not:

* ``hz_channel_connections``: the active, validated revision of each
  ``channels/<id>.yaml`` file, with its digest and status.
* ``hz_channel_threads``: one row per provider thread (a Slack thread, a GitHub
  issue, a Linear agent session, an email thread, a WhatsApp chat), with its
  immutable origin coordinates, the work it is bound to, and which person's
  request is active in it (a claim with an expiry, so two workers never run
  two people's requests in one thread at once).
* ``hz_channel_deliveries``: one row per verified provider delivery and event,
  unique by the provider's delivery id and by the event's own key, so a repeat
  is recognised and a reused id with different bytes is quarantined.
* ``hz_channel_links``: which hub account a provider account acts as, set by an
  administrator (the stand-in until run identity has its own store).

The down revision names both the secret store and the station tables, because
this change sits on both. Tables only: no existing row is read or changed.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0060"
down_revision = ("op_0040", "op_0051")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_channel_connections",
        sa.Column("hub", sa.String(255), primary_key=True),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("config_json", sa.Text, nullable=False),
        sa.Column("digest", sa.String(80), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("file_path", sa.String(512), nullable=False),
        sa.Column("applied_by", sa.String(320), nullable=False, server_default=""),
        sa.Column("state_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("updated", sa.Float, nullable=False),
    )
    op.create_table(
        "hz_channel_threads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("hub", sa.String(255), nullable=False),
        sa.Column("connection_id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(255), nullable=False),
        sa.Column("thread_key", sa.String(512), nullable=False),
        sa.Column("origin_json", sa.Text, nullable=False),
        sa.Column("subscribed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("conversations_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("work_item_id", sa.String(36)),
        sa.Column("requester", sa.String(320), nullable=False, server_default=""),
        sa.Column("active_account", sa.String(320)),
        sa.Column("active_until", sa.Float),
        sa.Column("last_event_at", sa.Float),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("updated", sa.Float, nullable=False),
    )
    op.create_index("hz_channel_threads_key", "hz_channel_threads",
                    ["hub", "connection_id", "workspace_id", "thread_key"], unique=True)
    op.create_table(
        "hz_channel_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("hub", sa.String(255), nullable=False),
        sa.Column("connection_id", sa.String(64), nullable=False),
        sa.Column("delivery_id", sa.String(255), nullable=False),
        sa.Column("event_index", sa.Integer, nullable=False, server_default="0"),
        sa.Column("semantic_key", sa.String(512)),
        sa.Column("body_digest", sa.String(80), nullable=False),
        sa.Column("thread_id", sa.String(36)),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("actor_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("event_enc", sa.Text),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column("route_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("verification", sa.String(64), nullable=False, server_default=""),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("updated", sa.Float, nullable=False),
    )
    op.create_index("hz_channel_deliveries_id", "hz_channel_deliveries",
                    ["hub", "connection_id", "delivery_id", "event_index"], unique=True)
    op.create_index("hz_channel_deliveries_semantic", "hz_channel_deliveries",
                    ["hub", "connection_id", "semantic_key"], unique=True)
    op.create_index("hz_channel_deliveries_status", "hz_channel_deliveries",
                    ["hub", "connection_id", "status"])
    op.create_index("hz_channel_deliveries_thread", "hz_channel_deliveries", ["thread_id", "created"])
    op.create_table(
        "hz_channel_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("hub", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("workspace_id", sa.String(255), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("account_email", sa.String(320), nullable=False),
        sa.Column("display", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(320), nullable=False),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("revoked", sa.Float),
    )
    op.create_index("hz_channel_links_external", "hz_channel_links",
                    ["hub", "provider", "workspace_id", "external_id"])


def downgrade() -> None:
    op.drop_table("hz_channel_links")
    op.drop_index("hz_channel_deliveries_thread", "hz_channel_deliveries")
    op.drop_index("hz_channel_deliveries_status", "hz_channel_deliveries")
    op.drop_index("hz_channel_deliveries_semantic", "hz_channel_deliveries")
    op.drop_index("hz_channel_deliveries_id", "hz_channel_deliveries")
    op.drop_table("hz_channel_deliveries")
    op.drop_index("hz_channel_threads_key", "hz_channel_threads")
    op.drop_table("hz_channel_threads")
    op.drop_table("hz_channel_connections")
