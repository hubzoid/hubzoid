"""Workflows from chat. Revision op_0019.

* ``hz_report_series``: one row per live report link. Team series belong to
  the agent (owner ''), personal series to one account. A key belongs to one
  workflow in a hub. Each publish, even an unchanged one, records a touch.
  An optional company link is stored as a hash plus the encrypted token.
* ``hz_artifacts.series_id``: the series a copy belongs to.
* ``hz_personal_schedules``: schedules people save from chat. They run as
  their owner, fired by the hub's dispatcher with a compare-and-swap on
  ``next_fire``.
* ``hz_workflow_proposals``: the first half of a two-call save, change or
  stop. Used once, expires after 10 minutes.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0019"
down_revision = "op_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_report_series",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("hub", sa.String(255), nullable=False),
        sa.Column("workflow", sa.String(255), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("owner", sa.String(320), nullable=False, server_default=""),
        sa.Column("owner_ref", sa.String(32), nullable=False, server_default=""),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("title", sa.String(300)),
        sa.Column("audience", sa.String(16), nullable=False, server_default="owner"),
        sa.Column("schedule_id", sa.String(64)),
        sa.Column("last_artifact", sa.String(64)),
        sa.Column("last_sha", sa.String(64)),
        sa.Column("last_digest", sa.String(128)),
        sa.Column("last_inputs", sa.Text),
        sa.Column("last_run", sa.String(255)),
        sa.Column("last_touch", sa.Float),
        sa.Column("updated_at", sa.Float),
        sa.Column("live", sa.Integer, nullable=False, server_default="1"),
        sa.Column("failed_at", sa.Float),
        sa.Column("domain_hash", sa.String(64)),
        sa.Column("domain_token_enc", sa.Text),
        sa.Column("domain_on", sa.Integer, nullable=False, server_default="0"),
        sa.Column("domain_by", sa.String(320)),
        sa.Column("domain_changed", sa.Float),
        sa.Column("created", sa.Float, nullable=False),
    )
    op.create_index("hz_report_series_unique", "hz_report_series",
                    ["hub", "workflow", "scope", "owner", "key"], unique=True)
    op.create_index("hz_report_series_key", "hz_report_series", ["hub", "key"])
    op.create_index("hz_report_series_domain", "hz_report_series", ["domain_hash"], unique=True)
    with op.batch_alter_table("hz_artifacts") as batch:
        batch.add_column(sa.Column("series_id", sa.String(64)))
    op.create_index("hz_artifacts_series", "hz_artifacts", ["series_id", "created"])

    op.create_table(
        "hz_personal_schedules",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("hub", sa.String(255), nullable=False),
        sa.Column("workflow", sa.String(255), nullable=False),
        sa.Column("owner", sa.String(320), nullable=False),
        sa.Column("owner_account", sa.String(64)),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("inputs", sa.Text, nullable=False),
        sa.Column("cadence", sa.String(120), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(32)),
        sa.Column("paused_by", sa.String(320)),
        sa.Column("next_fire", sa.Float),
        sa.Column("last_fire", sa.Float),
        sa.Column("last_run", sa.String(255)),
        sa.Column("last_status", sa.String(16)),
        sa.Column("last_at", sa.Float),
        sa.Column("last_cause", sa.String(200)),
        sa.Column("failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("updated", sa.Float, nullable=False),
    )
    op.create_index("hz_personal_schedules_due", "hz_personal_schedules",
                    ["hub", "state", "next_fire"])
    op.create_index("hz_personal_schedules_owner", "hz_personal_schedules", ["hub", "owner"])

    op.create_table(
        "hz_workflow_proposals",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("hub", sa.String(255), nullable=False),
        sa.Column("actor", sa.String(320), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("expires", sa.Float, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
    )
    op.create_index("hz_workflow_proposals_actor", "hz_workflow_proposals",
                    ["hub", "actor", "status"])


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
