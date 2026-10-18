"""Console administration. Revision op_0022.

* ``hz_admin_config_revisions`` and ``hz_admin_config_heads``: applied
  ``admin.yaml`` documents and the active one per deployment.
* ``hz_admin_config_plans``: a validated candidate with its base revision and
  digest, applied by id and digest within 30 minutes.
* ``hz_admin_diagnostic_reports`` and ``hz_admin_diagnostic_checks``: bounded,
  read-only diagnostic runs with a worker lease (an expired lease means
  interrupted, never rerun) and each check's safe result.
* ``hz_admin_support_bundles``: expiring, metadata-only support bundles.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0022"
down_revision = "op_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hz_admin_config_revisions",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('deployment_id', sa.String(64), nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('document_json', sa.String(None), nullable=False),
        sa.Column('digest', sa.String(80), nullable=False),
        sa.Column('created_by', sa.String(320), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.UniqueConstraint('deployment_id', 'revision', name="hz_admin_config_revisions_rev"),
    )
    op.create_table(
        "hz_admin_config_heads",
        sa.Column('deployment_id', sa.String(64), primary_key=True),
        sa.Column('revision_id', sa.String(64), nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
    )
    op.create_table(
        "hz_admin_config_plans",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('deployment_id', sa.String(64), nullable=False),
        sa.Column('hub_id', sa.String(255)),
        sa.Column('domain', sa.String(64), nullable=False),
        sa.Column('expected_revision', sa.Integer, nullable=False),
        sa.Column('candidate_json', sa.String(None), nullable=False),
        sa.Column('digest', sa.String(80), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('created_by_user_id', sa.String(320), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('expires_at', sa.Float, nullable=False),
        sa.Column('owner_plan_id', sa.String(64)),
        sa.Column('owner_operation_id', sa.String(64)),
    )
    op.create_table(
        "hz_admin_diagnostic_reports",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('deployment_id', sa.String(64), nullable=False),
        sa.Column('hub_id', sa.String(255)),
        sa.Column('requested_by_user_id', sa.String(320), nullable=False),
        sa.Column('profile', sa.String(16), nullable=False),
        sa.Column('check_ids_json', sa.String(None)),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('started_at', sa.Float),
        sa.Column('completed_at', sa.Float),
        sa.Column('expires_at', sa.Float),
        sa.Column('lease_owner', sa.String(64)),
        sa.Column('lease_expires_at', sa.Float),
        sa.Column('config_revision', sa.Integer, nullable=False),
        sa.Column('idempotency_key', sa.String(255)),
        sa.Column('request_hash', sa.String(64)),
        sa.Column('error_code', sa.String(64)),
        sa.Column('cancel_requested', sa.Integer, nullable=False),
        sa.Column('previous_report_id', sa.String(64)),
    )
    op.create_table(
        "hz_admin_diagnostic_checks",
        sa.Column('report_id', sa.String(64), primary_key=True),
        sa.Column('check_id', sa.String(64), primary_key=True),
        sa.Column('attempt', sa.Integer, nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('duration_ms', sa.Integer),
        sa.Column('summary', sa.String(None)),
        sa.Column('safe_details_json', sa.String(None)),
        sa.Column('remediation_code', sa.String(64)),
        sa.Column('remediation', sa.String(None)),
        sa.Column('owner_domain', sa.String(64)),
        sa.Column('owner_resource_id', sa.String(255)),
        sa.Column('started_at', sa.Float),
        sa.Column('completed_at', sa.Float),
    )
    op.create_table(
        "hz_admin_support_bundles",
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('report_id', sa.String(64), nullable=False),
        sa.Column('deployment_id', sa.String(64), nullable=False),
        sa.Column('created_by_user_id', sa.String(320), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('expires_at', sa.Float, nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('relative_storage_key', sa.String(255)),
        sa.Column('sha256', sa.String(64)),
        sa.Column('size_bytes', sa.Integer),
        sa.Column('manifest_json', sa.String(None)),
        sa.Column('error_code', sa.String(64)),
    )


def downgrade() -> None:
    op.drop_table("hz_admin_support_bundles")
    op.drop_table("hz_admin_diagnostic_checks")
    op.drop_table("hz_admin_diagnostic_reports")
    op.drop_table("hz_admin_config_plans")
    op.drop_table("hz_admin_config_heads")
    op.drop_table("hz_admin_config_revisions")
