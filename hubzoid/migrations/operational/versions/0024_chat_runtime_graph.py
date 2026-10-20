"""Chat runtime graph. Revision op_0024.

* ``hz_conversations`` gains ``graph_revision``, ``temporary``, ``expires_at``
  and ``deleted_at``; ``hz_messages`` gains its branch, the message it
  revises, author and editor, its run, the requested model and deletion marks.
* ``hz_chat_branches``: one writer's line of work and its head.
* ``hz_chat_views``: each person's selected branch and head.
* ``hz_chat_runs``: one assistant reply being written, with its input snapshot.
* ``hz_chat_branch_leases``: one fenced writer lease per branch.
* ``hz_chat_queue``: acknowledged follow-ups per branch.
* ``hz_chat_events``: the per-conversation event log for replay.
* ``hz_chat_copy_lineage``: where a copied message came from.
* ``hz_chat_runtime_bindings``: a branch's native runtime session (Claude
  session id, Codex thread, OpenAI Session) and the head it ended at.
* ``hz_chat_members`` / ``hz_chat_member_sets``: live collaborators of a
  conversation (people or groups, editor or viewer) and their revision.

Columns are added only when missing, so a database rewound by tests (or a
half-applied upgrade) upgrades again cleanly.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0024"
down_revision = "op_0023"
branch_labels = None
depends_on = None

_CONVERSATION_COLUMNS = (
    ("graph_revision", sa.Integer), ("temporary", sa.Integer), ("expires_at", sa.Float),
    ("deleted_at", sa.Float),
)
_MESSAGE_COLUMNS = (
    ("branch_id", sa.String(64)), ("revises_id", sa.String(64)), ("author_id", sa.String(64)),
    ("editor_id", sa.String(64)), ("run_id", sa.String(64)), ("requested_model", sa.String(255)),
    ("historical_only", sa.Integer), ("context_changed", sa.Integer), ("deleted_at", sa.Float),
)


def _add_missing(table: str, columns) -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}
    for name, type_ in columns:
        if name not in have:
            op.add_column(table, sa.Column(name, type_))


def upgrade() -> None:
    _add_missing("hz_conversations", _CONVERSATION_COLUMNS)
    _add_missing("hz_messages", _MESSAGE_COLUMNS)
    op.create_table(
        "hz_chat_branches",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("conversation_id", sa.String(64), nullable=False, index=True),
        sa.Column("writer_actor_id", sa.String(64), nullable=False),
        sa.Column("base_message_id", sa.String(64)),
        sa.Column("head_id", sa.String(64)),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("queue_paused", sa.Integer, nullable=False),
        sa.Column("queue_revision", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_table(
        "hz_chat_views",
        sa.Column("conversation_id", sa.String(64), primary_key=True),
        sa.Column("account_id", sa.String(64), primary_key=True),
        sa.Column("branch_id", sa.String(64)),
        sa.Column("view_head_id", sa.String(64)),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_table(
        "hz_chat_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("conversation_id", sa.String(64), nullable=False, index=True),
        sa.Column("branch_id", sa.String(64), nullable=False),
        sa.Column("message_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("input_snapshot", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("cancel_requested", sa.Integer, nullable=False),
        sa.Column("operation_id", sa.String(64)),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_table(
        "hz_chat_branch_leases",
        sa.Column("branch_id", sa.String(64), primary_key=True),
        sa.Column("conversation_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("fence", sa.Integer, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
    )
    op.create_table(
        "hz_chat_queue",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("conversation_id", sa.String(64), nullable=False, index=True),
        sa.Column("branch_id", sa.String(64), nullable=False),
        sa.Column("author_id", sa.String(64), nullable=False),
        sa.Column("lane", sa.String(8), nullable=False),
        sa.Column("rank", sa.Float, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("content_json", sa.Text, nullable=False),
        sa.Column("predecessor_id", sa.String(64)),
        sa.Column("reason", sa.Text),
        sa.Column("dispatched_run_id", sa.String(64)),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_table(
        "hz_chat_events",
        sa.Column("conversation_id", sa.String(64), primary_key=True),
        sa.Column("sequence", sa.Integer, primary_key=True),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("object_id", sa.String(64)),
        sa.Column("graph_revision", sa.Integer, nullable=False),
        sa.Column("payload_json", sa.Text, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_table(
        "hz_chat_copy_lineage",
        sa.Column("new_message_id", sa.String(64), primary_key=True),
        sa.Column("source_conversation_id", sa.String(64), nullable=False),
        sa.Column("source_message_id", sa.String(64), nullable=False),
        sa.Column("copy_operation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_table(
        "hz_chat_members",
        sa.Column("conversation_id", sa.String(64), primary_key=True),
        sa.Column("principal_type", sa.String(8), primary_key=True),
        sa.Column("principal_id", sa.String(320), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("added_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_table(
        "hz_chat_member_sets",
        sa.Column("conversation_id", sa.String(64), primary_key=True),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_table(
        "hz_chat_runtime_bindings",
        sa.Column("branch_id", sa.String(64), primary_key=True),
        sa.Column("conversation_id", sa.String(64), nullable=False),
        sa.Column("runtime", sa.String(32), nullable=False),
        sa.Column("native_session_id", sa.String(255), nullable=False),
        sa.Column("head_message_id", sa.String(64), nullable=False),
        sa.Column("config_digest", sa.String(80), nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )


def downgrade() -> None:
    for table in ("hz_chat_runtime_bindings", "hz_chat_member_sets", "hz_chat_members", "hz_chat_copy_lineage", "hz_chat_events", "hz_chat_queue", "hz_chat_branch_leases",
                  "hz_chat_runs", "hz_chat_views", "hz_chat_branches"):
        op.drop_table(table)
