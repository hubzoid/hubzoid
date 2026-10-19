"""Chat organisation and search. Revision op_0027.

* ``hz_chat_folders``: personal folders with inherited defaults.
* ``hz_chat_user_organization``: each person's folder, pin and archive for a
  conversation.
* ``hz_chat_tags`` / ``hz_chat_conversation_tags``: personal tags.
* ``hz_chat_bookmarks``: personal bookmarks of conversations and messages.
* ``hz_chat_organization_state``: the revision of a person's organisation.
* ``hz_chat_search_documents``: searchable parts; on SQLite an FTS5 table
  ``hz_chat_search_fts`` indexes them.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0027"
down_revision = "op_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_chat_folders",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('owner_id', sa.String(64), nullable=False, index=True),
        sa.Column('parent_id', sa.String(64)),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('normalized_name', sa.String(255), nullable=False),
        sa.Column('position', sa.Integer, nullable=False),
        sa.Column('defaults_json', sa.String(None), nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
        sa.UniqueConstraint('hub_id', 'owner_id', 'parent_id', 'normalized_name', name="uq_hz_folder_name"),
    )
    op.create_table(
        "hz_chat_user_organization",
        sa.Column('hub_id', sa.String(255), primary_key=True),
        sa.Column('user_id', sa.String(64), primary_key=True),
        sa.Column('conversation_id', sa.String(64), primary_key=True),
        sa.Column('folder_id', sa.String(64)),
        sa.Column('pinned_rank', sa.Integer),
        sa.Column('archived', sa.Integer, nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
    )
    op.create_table(
        "hz_chat_tags",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False, index=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('normalized_name', sa.String(255), nullable=False),
        sa.Column('color', sa.String(32), nullable=False),
        sa.Column('position', sa.Integer, nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
        sa.UniqueConstraint('hub_id', 'user_id', 'normalized_name', name="uq_hz_tag_name"),
    )
    op.create_table(
        "hz_chat_conversation_tags",
        sa.Column('user_id', sa.String(64), primary_key=True),
        sa.Column('conversation_id', sa.String(64), primary_key=True),
        sa.Column('tag_id', sa.String(64), primary_key=True),
    )
    op.create_table(
        "hz_chat_bookmarks",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False, index=True),
        sa.Column('conversation_id', sa.String(64), nullable=False),
        sa.Column('message_id', sa.String(64)),
        sa.Column('branch_id', sa.String(64)),
        sa.Column('label', sa.String(255), nullable=False),
        sa.Column('position', sa.Integer, nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
    )
    op.create_table(
        "hz_chat_organization_state",
        sa.Column('hub_id', sa.String(255), primary_key=True),
        sa.Column('user_id', sa.String(64), primary_key=True),
        sa.Column('revision', sa.Integer, nullable=False),
    )
    op.create_table(
        "hz_chat_search_documents",
        sa.Column('id', sa.String(160), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('conversation_id', sa.String(64), nullable=False, index=True),
        sa.Column('message_id', sa.String(64)),
        sa.Column('branch_id', sa.String(64)),
        sa.Column('title', sa.String(None)),
        sa.Column('body_text', sa.String(None), nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
    )

    if op.get_bind().dialect.name == "sqlite":
        op.execute("CREATE VIRTUAL TABLE IF NOT EXISTS hz_chat_search_fts USING fts5("
                   "doc_id UNINDEXED, title, body, tokenize='unicode61 remove_diacritics 2')")


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TABLE IF EXISTS hz_chat_search_fts")
    op.drop_table("hz_chat_search_documents")
    op.drop_table("hz_chat_organization_state")
    op.drop_table("hz_chat_bookmarks")
    op.drop_table("hz_chat_conversation_tags")
    op.drop_table("hz_chat_tags")
    op.drop_table("hz_chat_user_organization")
    op.drop_table("hz_chat_folders")
