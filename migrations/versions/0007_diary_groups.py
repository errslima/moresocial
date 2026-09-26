"""Diary sources and reversible semantic observation groups.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

revision = '0007'
down_revision = '0006'
branch_labels = depends_on = None


def upgrade():
    op.create_table('observation_groups',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('workspace_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('workspace_id', 'id'))
    op.create_index('ix_observation_groups_workspace_id', 'observation_groups', ['workspace_id'])
    op.add_column('claims', sa.Column('group_id', postgresql.UUID(as_uuid=True)))
    op.add_column('claims', sa.Column('auto_group', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('claims', sa.Column('grouping_revision', sa.String(64)))
    op.create_foreign_key('claims_observation_group_fk', 'claims', 'observation_groups', ['workspace_id', 'group_id'], ['workspace_id', 'id'])
    op.create_index('ix_claims_group_id', 'claims', ['group_id'])
    op.create_table('claim_embeddings',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('workspace_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False),
        sa.Column('claim_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('space', sa.String(180), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('vector', Vector(), nullable=False),
        sa.ForeignKeyConstraint(['workspace_id', 'claim_id'], ['claims.workspace_id', 'claims.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('workspace_id', 'claim_id', 'space'))
    op.create_index('ix_claim_embeddings_workspace_id', 'claim_embeddings', ['workspace_id'])
    # Keep the old fields for rollback. Copying is idempotent and preserves their last edit date.
    from app.diary import migrate_profiles
    from sqlalchemy.orm import Session
    with Session(bind=op.get_bind()) as session:
        migrate_profiles(session)
        session.flush()


def downgrade():
    op.drop_table('claim_embeddings')
    op.drop_constraint('claims_observation_group_fk', 'claims', type_='foreignkey')
    op.drop_index('ix_claims_group_id', table_name='claims')
    for column in ('group_id', 'auto_group', 'grouping_revision'):
        op.drop_column('claims', column)
    op.drop_table('observation_groups')
    # Diary source text is deliberately retained.
