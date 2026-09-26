"""Workflow identity, published artifacts and owner email.

- `hz_workflow_kv` gains `owner` in its key: a workflow's `hub.state` belongs to
  the account the run acts as. Existing rows get owner '' and are kept as they
  are, assigned to no one (workflows/state.py).
- `hz_artifacts`: one row per published file (owner, hub, run, storage, audience).
- `hz_artifact_shares`: the people and groups a "specific people" artifact is
  shared with.
- `hz_artifact_links`: public links, stored as a SHA-256 of the token only.
- `hz_email_deliveries`: one row per owner email, the record retries consult.

Revision ID: op_0005
Revises: op_0004
"""
import sqlalchemy as sa
from alembic import op

revision = "op_0005"
down_revision = "op_0004"
branch_labels = None
depends_on = None


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.Float(precision=53), nullable=nullable)  # UTC epoch seconds


def upgrade() -> None:
    # Rebuild rather than alter: SQLite cannot change a primary key in place.
    op.create_table(
        "hz_workflow_kv_new",
        sa.Column("hub", sa.Text, nullable=False),
        sa.Column("workflow", sa.Text, nullable=False),
        sa.Column("owner", sa.Text, nullable=False, server_default=""),
        sa.Column("k", sa.Text, nullable=False),
        sa.Column("v", sa.Text),
        sa.PrimaryKeyConstraint("hub", "workflow", "owner", "k"),
    )
    op.execute("INSERT INTO hz_workflow_kv_new (hub, workflow, owner, k, v) "
               "SELECT hub, workflow, '', k, v FROM hz_workflow_kv")
    op.drop_table("hz_workflow_kv")
    op.rename_table("hz_workflow_kv_new", "hz_workflow_kv")

    op.create_table(
        "hz_artifacts",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("hub", sa.Text, nullable=False),
        sa.Column("owner", sa.Text, nullable=False),
        sa.Column("owner_account", sa.Text),
        sa.Column("workflow", sa.Text),
        sa.Column("run_id", sa.Text),
        sa.Column("idem_key", sa.Text, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("content_type", sa.Text, nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("storage", sa.Text, nullable=False),  # hub-relative path
        # owner|people|hub|link
        sa.Column("audience", sa.Text, nullable=False, server_default="owner"),
        _ts("created", nullable=False),
        _ts("updated", nullable=False),
        _ts("deleted"),
    )
    op.create_index("hz_artifacts_owner", "hz_artifacts", ["owner", "created"])
    op.create_index("hz_artifacts_hub", "hz_artifacts", ["hub", "created"])

    op.create_table(
        "hz_artifact_shares",
        sa.Column("artifact_id", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),  # user|group
        sa.Column("principal", sa.Text, nullable=False),
        sa.Column("added_by", sa.Text),
        _ts("added", nullable=False),
        sa.PrimaryKeyConstraint("artifact_id", "kind", "principal"),
    )

    op.create_table(
        "hz_artifact_links",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("artifact_id", sa.Text, nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False, unique=True),
        _ts("created", nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        _ts("expires", nullable=False),
        _ts("revoked"),
    )
    op.create_index("hz_artifact_links_artifact", "hz_artifact_links", ["artifact_id"])

    op.create_table(
        "hz_email_deliveries",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("idem_key", sa.Text, unique=True),
        sa.Column("hub", sa.Text, nullable=False),
        sa.Column("workflow", sa.Text),
        sa.Column("run_id", sa.Text),
        sa.Column("owner", sa.Text, nullable=False),
        sa.Column("recipient", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("artifacts", sa.Text),  # JSON list of artifact ids
        sa.Column("mode", sa.Text, nullable=False),  # smtp|preview
        # pending|connecting|sending|accepted|failed|ambiguous|previewed
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("detail", sa.Text),
        sa.Column("smtp_code", sa.Integer),
        sa.Column("message_id", sa.Text),
        _ts("created", nullable=False),
        _ts("updated", nullable=False),
    )
    op.create_index("hz_email_deliveries_owner", "hz_email_deliveries", ["owner", "created"])


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
