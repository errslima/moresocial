"""Automatic events and their workspace-scoped evidence."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB
revision = '0010'
down_revision = '0009'
branch_labels = depends_on = None


def upgrade():
    for col in [sa.Column('origin', sa.String(20), nullable=False, server_default='legacy'),
                sa.Column('starts_at', sa.DateTime(timezone=True)), sa.Column('ends_at', sa.DateTime(timezone=True)),
                sa.Column('details', JSONB, nullable=False, server_default='{}'),
                sa.Column('dismissed', sa.Boolean, nullable=False, server_default=sa.false()),
                sa.Column('locked_fields', JSONB, nullable=False, server_default='[]')]:
        op.add_column('events', col)
    op.create_table('event_scans',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('workspace_id', UUID(as_uuid=True), sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False),
        sa.Column('scan_key', sa.String(64), nullable=False), sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.UniqueConstraint('workspace_id', 'scan_key'))
    op.create_table('event_mentions',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('workspace_id', UUID(as_uuid=True), sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False),
        sa.Column('event_id', UUID(as_uuid=True), nullable=False), sa.Column('scan_key', sa.String(64), nullable=False),
        sa.Column('evidence', JSONB, nullable=False, server_default='[]'),
        sa.Column('data', JSONB, nullable=False, server_default='{}'),
        sa.ForeignKeyConstraint(['workspace_id', 'event_id'], ['events.workspace_id', 'events.id'], ondelete='CASCADE'))
    op.create_index('ix_event_mentions_event_id', 'event_mentions', ['event_id'])
    for table in ('event_scans', 'event_mentions'):
        op.create_index('ix_' + table + '_workspace_id', table, ['workspace_id'])


def downgrade():
    op.drop_table('event_mentions')
    op.drop_table('event_scans')
    for col in ('origin', 'starts_at', 'ends_at', 'details', 'dismissed', 'locked_fields'):
        op.drop_column('events', col)
