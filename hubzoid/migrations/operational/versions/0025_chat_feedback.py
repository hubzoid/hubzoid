"""Chat feedback, helpers and notices. Revision op_0025.

* ``hz_feedback``: one person's rating of one finished answer.
* ``hz_helper_jobs``: generated follow-ups and titles, bound to the branch
  revision they were made from.
* ``hz_notification_events`` / ``hz_notification_inbox``: one event per
  reply ending, one inbox item per recipient.
* ``hz_notification_target_sets`` / ``hz_notification_targets``: external
  destinations per scope, with the scope's revision.
* ``hz_notification_deliveries``: each send attempt to a target.
* ``hz_notification_presence``: who is looking at which conversation (never
  grants access).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0025"
down_revision = "op_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_feedback",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('conversation_id', sa.String(64), nullable=False, index=True),
        sa.Column('branch_id', sa.String(64)),
        sa.Column('message_id', sa.String(64), nullable=False),
        sa.Column('author_id', sa.String(64), nullable=False),
        sa.Column('agent_id', sa.String(255)),
        sa.Column('rating', sa.String(16), nullable=False),
        sa.Column('reason_ids', sa.String(None), nullable=False),
        sa.Column('comment', sa.String(None)),
        sa.Column('tags', sa.String(None), nullable=False),
        sa.Column('score', sa.Integer),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('source_metadata', sa.String(None)),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
        sa.Column('deleted_at', sa.Float),
        sa.UniqueConstraint('message_id', 'author_id', name="uq_hz_feedback_author_message"),
    )
    op.create_table(
        "hz_helper_jobs",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('kind', sa.String(32), nullable=False),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('principal_id', sa.String(64), nullable=False),
        sa.Column('conversation_id', sa.String(64), nullable=False, index=True),
        sa.Column('branch_id', sa.String(64), nullable=False),
        sa.Column('input_revision', sa.Integer, nullable=False),
        sa.Column('head_id', sa.String(64)),
        sa.Column('config_revision', sa.Integer, nullable=False),
        sa.Column('prompt_hash', sa.String(80), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('result', sa.String(None)),
        sa.Column('evidence', sa.String(None)),
        sa.Column('error_code', sa.String(64)),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
        sa.UniqueConstraint('kind', 'branch_id', 'input_revision', 'config_revision', 'principal_id', name="uq_hz_helper_trigger"),
    )
    op.create_table(
        "hz_notification_events",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('source_event_id', sa.String(128), nullable=False),
        sa.Column('event_kind', sa.String(32), nullable=False),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('conversation_id', sa.String(64), nullable=False),
        sa.Column('branch_id', sa.String(64)),
        sa.Column('run_id', sa.String(64)),
        sa.Column('message_id', sa.String(64)),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.UniqueConstraint('source_event_id', name="uq_hz_notification_source"),
    )
    op.create_table(
        "hz_notification_inbox",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('event_id', sa.String(64), nullable=False),
        sa.Column('recipient_id', sa.String(64), nullable=False, index=True),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('read_at', sa.Float),
        sa.Column('dismissed_at', sa.Float),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.UniqueConstraint('event_id', 'recipient_id', name="uq_hz_inbox_event_recipient"),
    )
    op.create_table(
        "hz_notification_target_sets",
        sa.Column('scope', sa.String(400), primary_key=True),
        sa.Column('revision', sa.Integer, nullable=False),
    )
    op.create_table(
        "hz_notification_targets",
        sa.Column('scope', sa.String(400), primary_key=True),
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('owner_id', sa.String(64)),
        sa.Column('type', sa.String(16), nullable=False),
        sa.Column('connection_ref', sa.String(255)),
        sa.Column('recipient_ref', sa.String(2000), nullable=False),
        sa.Column('verified_at', sa.Float),
        sa.Column('enabled', sa.Integer, nullable=False),
        sa.Column('events', sa.String(None), nullable=False),
        sa.Column('delivery', sa.String(16), nullable=False),
        sa.Column('preview', sa.String(16), nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('deleted_at', sa.Float),
    )
    op.create_table(
        "hz_notification_deliveries",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('event_id', sa.String(64), nullable=False),
        sa.Column('recipient_id', sa.String(64)),
        sa.Column('target_scope', sa.String(400), nullable=False),
        sa.Column('target_id', sa.String(64), nullable=False),
        sa.Column('target_revision', sa.Integer, nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('attempt', sa.Integer, nullable=False),
        sa.Column('next_attempt_at', sa.Float),
        sa.Column('provider_receipt', sa.String(None)),
        sa.Column('error_code', sa.String(64)),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
        sa.UniqueConstraint('event_id', 'target_scope', 'target_id', name="uq_hz_delivery_event_target"),
    )
    op.create_table(
        "hz_notification_presence",
        sa.Column('principal_id', sa.String(64), primary_key=True),
        sa.Column('device_id', sa.String(64), primary_key=True),
        sa.Column('conversation_id', sa.String(64)),
        sa.Column('focused', sa.Integer, nullable=False),
        sa.Column('lease_until', sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("hz_notification_presence")
    op.drop_table("hz_notification_deliveries")
    op.drop_table("hz_notification_targets")
    op.drop_table("hz_notification_target_sets")
    op.drop_table("hz_notification_inbox")
    op.drop_table("hz_notification_events")
    op.drop_table("hz_helper_jobs")
    op.drop_table("hz_feedback")
