"""Evidence-backed suggestions and persistent conversation review state."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = '0008'
down_revision = '0007'
branch_labels = depends_on = None


def common():
    return [sa.Column('id', UUID(as_uuid=True), primary_key=True),
            sa.Column('workspace_id', UUID(as_uuid=True), sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False)]


def owned(table, col):
    return sa.ForeignKeyConstraint(['workspace_id', col], [table + '.workspace_id', table + '.id'], ondelete='CASCADE')


def upgrade():
    op.create_table('suggestion_reviews', *common(),
        sa.Column('conversation_id', sa.String(400), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('paused', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('checked_at', sa.DateTime(timezone=True)),
        sa.Column('next_review_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('workspace_id', 'id'), sa.UniqueConstraint('workspace_id', 'conversation_id'))
    op.create_table('suggestions', *common(),
        sa.Column('review_id', UUID(as_uuid=True), nullable=False),
        sa.Column('anchor_id', UUID(as_uuid=True), nullable=False),
        sa.Column('person_id', UUID(as_uuid=True), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('state', sa.String(20), nullable=False, server_default='active'),
        sa.Column('disposition', sa.String(30)),
        sa.Column('due_at', sa.DateTime(timezone=True)),
        sa.Column('snoozed_until', sa.DateTime(timezone=True)),
        sa.Column('stale', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('draft', sa.Text(), nullable=False, server_default=''),
        sa.Column('draft_revision', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('draft_review_revision', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        owned('suggestion_reviews', 'review_id'), owned('sources', 'anchor_id'), owned('people', 'person_id'),
        sa.UniqueConstraint('workspace_id', 'id'), sa.UniqueConstraint('workspace_id', 'review_id', 'anchor_id', 'kind'))
    op.create_table('suggestion_sources', *common(),
        sa.Column('suggestion_id', UUID(as_uuid=True), nullable=False),
        sa.Column('source_id', UUID(as_uuid=True), nullable=False),
        sa.Column('source_version', sa.String(200), nullable=False),
        owned('suggestions', 'suggestion_id'), owned('sources', 'source_id'),
        sa.UniqueConstraint('workspace_id', 'suggestion_id', 'source_id'))
    for table in ('suggestion_reviews', 'suggestions', 'suggestion_sources'):
        op.create_index('ix_' + table + '_workspace_id', table, ['workspace_id'])


def downgrade():
    for table in ('suggestion_sources', 'suggestions', 'suggestion_reviews'):
        op.drop_table(table)
