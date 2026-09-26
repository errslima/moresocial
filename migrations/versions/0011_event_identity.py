"""Event identity: reversible canonical grouping, identity decisions and pair scans.

Additive. Existing events become singleton groups (canonical_event_id NULL); no event,
mention or source data is modified or removed.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB
revision = '0011'
down_revision = '0010'
branch_labels = depends_on = None


def upgrade():
    op.add_column('events', sa.Column('canonical_event_id', UUID(as_uuid=True)))
    op.create_foreign_key('fk_events_canonical', 'events', 'events', ['workspace_id', 'canonical_event_id'],
                          ['workspace_id', 'id'])
    op.create_index('ix_events_canonical', 'events', ['workspace_id', 'canonical_event_id'])
    op.create_check_constraint('ck_events_canonical_not_self', 'events', 'canonical_event_id IS NULL OR canonical_event_id <> id')
    op.create_table('event_identity_decisions',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('workspace_id', UUID(as_uuid=True), sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False),
        sa.Column('event_a', UUID(as_uuid=True), nullable=False), sa.Column('event_b', UUID(as_uuid=True), nullable=False),
        sa.Column('verdict', sa.String(12), nullable=False), sa.Column('origin', sa.String(12), nullable=False),
        sa.Column('via', sa.String(12), nullable=False, server_default='web'),
        sa.Column('active', sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column('applied', sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column('operation_id', UUID(as_uuid=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('superseded_at', sa.DateTime(timezone=True)),
        sa.Column('superseded_reason', sa.String(40)),
        sa.Column('resolver_version', sa.String(40), nullable=False, server_default=''),
        sa.Column('fingerprint', sa.String(64), nullable=False, server_default=''),
        sa.Column('citations', JSONB, nullable=False, server_default='[]'),
        sa.Column('reasons', JSONB, nullable=False, server_default='[]'),
        sa.CheckConstraint('event_a < event_b', name='ck_event_identity_pair_order'),
        sa.CheckConstraint("verdict IN ('same', 'different', 'uncertain')", name='ck_event_identity_verdict'),
        sa.CheckConstraint("origin IN ('user', 'automatic')", name='ck_event_identity_origin'),
        sa.ForeignKeyConstraint(['workspace_id', 'event_a'], ['events.workspace_id', 'events.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id', 'event_b'], ['events.workspace_id', 'events.id'], ondelete='CASCADE'))
    op.create_index('ix_event_identity_decisions_workspace_id', 'event_identity_decisions', ['workspace_id'])
    op.create_index('ix_event_identity_a', 'event_identity_decisions', ['workspace_id', 'event_a'])
    op.create_index('ix_event_identity_b', 'event_identity_decisions', ['workspace_id', 'event_b'])
    op.create_index('uq_event_identity_current', 'event_identity_decisions', ['workspace_id', 'event_a', 'event_b', 'origin'],
                    unique=True, postgresql_where=sa.text('active'))
    op.create_table('event_pair_scans',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('workspace_id', UUID(as_uuid=True), sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False),
        sa.Column('event_a', UUID(as_uuid=True), nullable=False), sa.Column('event_b', UUID(as_uuid=True), nullable=False),
        sa.Column('resolver_version', sa.String(40), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('verdict', sa.String(12)),
        sa.Column('attempts', sa.Integer, nullable=False, server_default='0'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('event_a < event_b', name='ck_event_pair_scans_order'),
        sa.UniqueConstraint('workspace_id', 'event_a', 'event_b'),
        sa.ForeignKeyConstraint(['workspace_id', 'event_a'], ['events.workspace_id', 'events.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id', 'event_b'], ['events.workspace_id', 'events.id'], ondelete='CASCADE'))
    op.create_index('ix_event_pair_scans_workspace_id', 'event_pair_scans', ['workspace_id'])


def downgrade():
    op.drop_table('event_pair_scans')
    op.drop_table('event_identity_decisions')
    op.drop_constraint('ck_events_canonical_not_self', 'events')
    op.drop_index('ix_events_canonical', 'events')
    op.drop_constraint('fk_events_canonical', 'events', type_='foreignkey')
    op.drop_column('events', 'canonical_event_id')
