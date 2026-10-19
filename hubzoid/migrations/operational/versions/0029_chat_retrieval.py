"""Sources and retrieval. Revision op_0029.

* ``hz_knowledge_collections``: a person's named sources (revisioned manifest,
  readers, writers, agents) and the generation that answers now.
* ``hz_knowledge_sources``: each source of a collection, its version and state.
* ``hz_knowledge_generations``: one complete index build; published at once.
* ``hz_knowledge_chunks``: passages with their location; on SQLite an FTS5
  table ``hz_knowledge_fts`` indexes them.
* ``hz_knowledge_jobs``: syncs and their outcome.
* ``hz_knowledge_citations``: the exact source version and passage behind each
  citation shown in an answer.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0029"
down_revision = "op_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_knowledge_collections",
        sa.Column('hub_id', sa.String(255), primary_key=True),
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('owner_principal_id', sa.String(64), nullable=False),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('manifest_json', sa.String(None), nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('active_generation_id', sa.String(64)),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
        sa.Column('deleted_at', sa.Float),
    )
    op.create_table(
        "hz_knowledge_sources",
        sa.Column('source_id', sa.String(64), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('collection_id', sa.String(64), nullable=False, index=True),
        sa.Column('key', sa.String(255), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('locator', sa.String(None), nullable=False),
        sa.Column('source_version', sa.Integer, nullable=False),
        sa.Column('content_sha256', sa.String(64)),
        sa.Column('file_id', sa.String(64)),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('error', sa.String(None)),
        sa.Column('observed_at', sa.Float, nullable=False),
        sa.Column('deleted_at', sa.Float),
        sa.UniqueConstraint('hub_id', 'collection_id', 'key', name="uq_hz_knowledge_source_key"),
    )
    op.create_table(
        "hz_knowledge_generations",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('collection_id', sa.String(64), nullable=False),
        sa.Column('manifest_revision', sa.Integer, nullable=False),
        sa.Column('fingerprint', sa.String(80), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('published_at', sa.Float),
    )
    op.create_table(
        "hz_knowledge_chunks",
        sa.Column('chunk_id', sa.String(64), primary_key=True),
        sa.Column('generation_id', sa.String(64), nullable=False, index=True),
        sa.Column('source_id', sa.String(64), nullable=False),
        sa.Column('source_version', sa.Integer, nullable=False),
        sa.Column('ordinal', sa.Integer, nullable=False),
        sa.Column('locator_json', sa.String(None), nullable=False),
        sa.Column('text', sa.String(None), nullable=False),
    )
    op.create_table(
        "hz_knowledge_jobs",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('collection_id', sa.String(64)),
        sa.Column('operation', sa.String(16), nullable=False),
        sa.Column('principal_id', sa.String(64), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('counters_json', sa.String(None), nullable=False),
        sa.Column('error_code', sa.String(64)),
        sa.Column('error', sa.String(None)),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
    )
    op.create_table(
        "hz_knowledge_citations",
        sa.Column('citation_id', sa.String(64), primary_key=True),
        sa.Column('hub_id', sa.String(255), nullable=False),
        sa.Column('collection_id', sa.String(64), nullable=False),
        sa.Column('source_id', sa.String(64), nullable=False),
        sa.Column('source_version', sa.Integer, nullable=False),
        sa.Column('chunk_id', sa.String(64), nullable=False),
        sa.Column('generation_id', sa.String(64), nullable=False),
        sa.Column('principal_id', sa.String(64), nullable=False),
        sa.Column('conversation_id', sa.String(64)),
        sa.Column('run_id', sa.String(64)),
        sa.Column('created_at', sa.Float, nullable=False),
    )


    if op.get_bind().dialect.name == "sqlite":
        op.execute("CREATE VIRTUAL TABLE IF NOT EXISTS hz_knowledge_fts USING fts5("
                   "chunk_id UNINDEXED, text, tokenize='unicode61 remove_diacritics 2')")


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TABLE IF EXISTS hz_knowledge_fts")
    op.drop_table("hz_knowledge_citations")
    op.drop_table("hz_knowledge_jobs")
    op.drop_table("hz_knowledge_chunks")
    op.drop_table("hz_knowledge_generations")
    op.drop_table("hz_knowledge_sources")
    op.drop_table("hz_knowledge_collections")
