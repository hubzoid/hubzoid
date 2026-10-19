"""Chat files and artifacts. Revision op_0028.

* ``hz_chat_storage_configs``: each storage adapter configuration that wrote
  objects (redacted, immutable), so old files stay readable after a change.
* ``hz_chat_files``: immutable file identity: owner, storage object, SHA-256,
  size, type, original name, state.
* ``hz_chat_file_uploads``: resumable uploads (server offset, expected hash).
* ``hz_chat_file_references``: where a file is used (a conversation, an
  artifact); reading follows these, deletion needs none left.
* ``hz_chat_artifacts`` and ``hz_chat_artifact_versions``: documents made in a
  conversation and their numbered versions (generated or edited).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0028"
down_revision = "op_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_chat_storage_configs",
        sa.Column('id', sa.String(80), primary_key=True),
        sa.Column('adapter', sa.String(16), nullable=False),
        sa.Column('config_json', sa.String(None), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
    )
    op.create_table(
        "hz_chat_files",
        sa.Column('file_id', sa.String(64), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('owner_principal_id', sa.String(64), nullable=False, index=True),
        sa.Column('storage_config_id', sa.String(80), nullable=False),
        sa.Column('object_key', sa.String(255), nullable=False),
        sa.Column('content_sha256', sa.String(64), nullable=False),
        sa.Column('size_bytes', sa.Integer, nullable=False),
        sa.Column('mime_type', sa.String(255), nullable=False),
        sa.Column('original_name', sa.String(255), nullable=False),
        sa.Column('origin', sa.String(16), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('processing_error', sa.String(None)),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('deleted_at', sa.Float),
    )
    op.create_table(
        "hz_chat_file_uploads",
        sa.Column('upload_id', sa.String(64), primary_key=True),
        sa.Column('file_id', sa.String(64), nullable=False),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('owner_principal_id', sa.String(64), nullable=False),
        sa.Column('conversation_id', sa.String(64)),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('mime_type', sa.String(255), nullable=False),
        sa.Column('size_bytes', sa.Integer, nullable=False),
        sa.Column('expected_sha256', sa.String(64)),
        sa.Column('next_offset', sa.Integer, nullable=False),
        sa.Column('last_chunk_sha256', sa.String(64)),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('idempotency_key', sa.String(255)),
        sa.Column('expires_at', sa.Float, nullable=False),
        sa.UniqueConstraint('owner_principal_id', 'hub_id', 'idempotency_key', name="uq_hz_file_upload_key"),
    )
    op.create_table(
        "hz_chat_file_references",
        sa.Column('reference_id', sa.String(64), primary_key=True),
        sa.Column('file_id', sa.String(64), nullable=False, index=True),
        sa.Column('resource_type', sa.String(16), nullable=False),
        sa.Column('resource_id', sa.String(64), nullable=False),
        sa.Column('conversation_id', sa.String(64), index=True),
        sa.Column('local_name', sa.String(255)),
        sa.Column('created_by', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('deleted_at', sa.Float),
    )
    op.create_table(
        "hz_chat_artifacts",
        sa.Column('artifact_id', sa.String(64), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('owner_principal_id', sa.String(64), nullable=False),
        sa.Column('conversation_id', sa.String(64), nullable=False, index=True),
        sa.Column('branch_id', sa.String(64)),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('current_version_id', sa.String(64)),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
        sa.UniqueConstraint('conversation_id', 'title', name="uq_hz_artifact_title"),
    )
    op.create_table(
        "hz_chat_artifact_versions",
        sa.Column('version_id', sa.String(64), primary_key=True),
        sa.Column('artifact_id', sa.String(64), nullable=False, index=True),
        sa.Column('file_id', sa.String(64), nullable=False),
        sa.Column('ordinal', sa.Integer, nullable=False),
        sa.Column('base_version_id', sa.String(64)),
        sa.Column('author_id', sa.String(64), nullable=False),
        sa.Column('run_id', sa.String(64)),
        sa.Column('message_id', sa.String(64)),
        sa.Column('provenance', sa.String(16), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.UniqueConstraint('artifact_id', 'ordinal', name="uq_hz_artifact_ordinal"),
    )


def downgrade() -> None:
    op.drop_table("hz_chat_artifact_versions")
    op.drop_table("hz_chat_artifacts")
    op.drop_table("hz_chat_file_references")
    op.drop_table("hz_chat_file_uploads")
    op.drop_table("hz_chat_files")
    op.drop_table("hz_chat_storage_configs")
