"""Team spaces, notes and calendar. Revision op_0032.

* ``hz_spaces`` and ``hz_space_members``: channels and direct messages, who is
  in them, who muted them and what they last read.
* ``hz_space_posts`` and ``hz_space_reactions``: posts and threads with their
  verified author (a person, or the agent on someone's behalf), revisions,
  pins and reactions.
* ``hz_notes`` and ``hz_note_revisions``: personal and channel notes with every
  saved revision.
* ``hz_calendar_events`` and ``hz_calendar_reminders``: events with a time zone
  and repeat, and each reminder to deliver (pending, sent or cancelled).
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
        "hz_spaces",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False, index=True),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('open', sa.Integer, nullable=False),
        sa.Column('created_by', sa.String(64), nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('archived_at', sa.Float),
    )
    op.create_table(
        "hz_space_members",
        sa.Column('space_id', sa.String(64), primary_key=True),
        sa.Column('user_id', sa.String(64), primary_key=True),
        sa.Column('role', sa.String(16), nullable=False),
        sa.Column('muted', sa.Integer, nullable=False),
        sa.Column('last_read_at', sa.Float),
        sa.Column('added_at', sa.Float, nullable=False),
    )
    op.create_table(
        "hz_space_posts",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('space_id', sa.String(64), nullable=False, index=True),
        sa.Column('parent_id', sa.String(64)),
        sa.Column('author_id', sa.String(64), nullable=False),
        sa.Column('author_kind', sa.String(16), nullable=False),
        sa.Column('on_behalf_of', sa.String(64)),
        sa.Column('body', sa.String(None), nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('editor_id', sa.String(64)),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('pinned_by', sa.String(64)),
        sa.Column('pinned_at', sa.Float),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
    )
    op.create_table(
        "hz_space_reactions",
        sa.Column('post_id', sa.String(64), primary_key=True),
        sa.Column('user_id', sa.String(64), primary_key=True),
        sa.Column('emoji', sa.String(16), primary_key=True),
        sa.Column('created_at', sa.Float, nullable=False),
    )
    op.create_table(
        "hz_notes",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False, index=True),
        sa.Column('owner_id', sa.String(64), nullable=False),
        sa.Column('space_id', sa.String(64)),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('body', sa.String(None), nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('updated_by', sa.String(64), nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
        sa.Column('deleted_at', sa.Float),
    )
    op.create_table(
        "hz_note_revisions",
        sa.Column('note_id', sa.String(64), primary_key=True),
        sa.Column('revision', sa.Integer, primary_key=True),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('body', sa.String(None), nullable=False),
        sa.Column('author_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
    )
    op.create_table(
        "hz_calendar_events",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False, index=True),
        sa.Column('owner_id', sa.String(64), nullable=False),
        sa.Column('space_id', sa.String(64)),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('starts_at', sa.Float, nullable=False),
        sa.Column('ends_at', sa.Float, nullable=False),
        sa.Column('timezone', sa.String(64), nullable=False),
        sa.Column('repeat', sa.String(16), nullable=False),
        sa.Column('repeat_until', sa.Float),
        sa.Column('attendees_json', sa.String(None), nullable=False),
        sa.Column('details', sa.String(None)),
        sa.Column('reminder_minutes', sa.Integer),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('deleted_at', sa.Float),
    )
    op.create_table(
        "hz_calendar_reminders",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('event_id', sa.String(64), nullable=False, index=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('occurrence_at', sa.Float, nullable=False),
        sa.Column('due_at', sa.Float, nullable=False, index=True),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('sent_at', sa.Float),
        sa.UniqueConstraint('event_id', 'user_id', 'occurrence_at', name="uq_hz_reminder_occurrence"),
    )


def downgrade() -> None:
    op.drop_table("hz_calendar_reminders")
    op.drop_table("hz_calendar_events")
    op.drop_table("hz_note_revisions")
    op.drop_table("hz_notes")
    op.drop_table("hz_space_reactions")
    op.drop_table("hz_space_posts")
    op.drop_table("hz_space_members")
    op.drop_table("hz_spaces")
