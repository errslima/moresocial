"""Keep embedding spaces separate even when dimensions match.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('embeddings', sa.Column('space', sa.String(180), nullable=True))
    op.execute("UPDATE embeddings SET space = 'legacy:' || model || ':' || dimension::text WHERE space IS NULL")
    op.alter_column('embeddings', 'space', nullable=False)
    op.drop_constraint('embeddings_workspace_id_chunk_id_model_key', 'embeddings', type_='unique')
    op.create_unique_constraint('embeddings_workspace_id_chunk_id_space_key', 'embeddings', ['workspace_id', 'chunk_id', 'space'])

def downgrade():
    op.drop_constraint('embeddings_workspace_id_chunk_id_space_key', 'embeddings', type_='unique')
    op.create_unique_constraint('embeddings_workspace_id_chunk_id_model_key', 'embeddings', ['workspace_id', 'chunk_id', 'model'])
    op.drop_column('embeddings', 'space')
