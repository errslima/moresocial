"""Durable connector cleanup and attributed chunk segments.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('connector_cleanups',
                    sa.Column('workspace_id', sa.UUID(), primary_key=True),
                    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.add_column('chunk_sources', sa.Column('segment_text', sa.Text(), nullable=False, server_default=''))
    # Existing v1 chunks are rebuilt by the worker's pipeline-version reconciliation.
    # Keep sources and old chunks intact during migration; no user data is removed.


def downgrade():
    op.drop_column('chunk_sources', 'segment_text')
    op.drop_table('connector_cleanups')
