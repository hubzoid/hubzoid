"""Workflow owner readiness, admitted events and alert outbox."""
from __future__ import annotations
import sqlalchemy as sa
from alembic import op
revision = 'op_0013'
down_revision = 'op_0012'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('hz_workflow_owner',
        sa.Column('hub', sa.String(255), primary_key=True),
        sa.Column('boot', sa.String(64), nullable=False),
        sa.Column('generation', sa.Integer, nullable=False),
        sa.Column('expires', sa.Float, nullable=False),
        sa.Column('ready', sa.Text, nullable=False))
    op.create_table('hz_workflow_events',
        sa.Column('id', sa.String(255), primary_key=True),
        sa.Column('hub', sa.String(255), nullable=False),
        sa.Column('webhook', sa.String(255), nullable=False),
        sa.Column('workflow', sa.String(255), nullable=False),
        sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('payload', sa.Text, nullable=False),
        sa.Column('state', sa.String(32), nullable=False),
        sa.Column('created', sa.Float, nullable=False),
        sa.Column('updated', sa.Float, nullable=False),
        sa.Column('attempt', sa.Integer, nullable=False, server_default='0'),
        sa.Column('redrive', sa.Integer, nullable=False, server_default='0'),
        sa.Column('version', sa.String(128)),
        sa.Column('error', sa.Text))
    op.create_index('hz_workflow_events_pending', 'hz_workflow_events', ['hub', 'state', 'created'])
    op.create_table('hz_workflow_alerts',
        sa.Column('id', sa.String(255), primary_key=True),
        sa.Column('hub', sa.String(255), nullable=False),
        sa.Column('kind', sa.String(64), nullable=False),
        sa.Column('payload', sa.Text, nullable=False),
        sa.Column('destination', sa.Text, nullable=False),
        sa.Column('state', sa.String(32), nullable=False),
        sa.Column('attempt', sa.Integer, nullable=False, server_default='0'),
        sa.Column('due', sa.Float, nullable=False),
        sa.Column('created', sa.Float, nullable=False),
        sa.Column('error', sa.Text))
    op.create_index('hz_workflow_alerts_due', 'hz_workflow_alerts', ['hub', 'state', 'due'])


def downgrade():
    for name in ('hz_workflow_alerts', 'hz_workflow_events', 'hz_workflow_owner'):
        op.drop_table(name)
