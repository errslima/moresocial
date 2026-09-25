"""OpenRouter accounting fields.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('provider_usage', sa.Column('cost', sa.Numeric(18, 8), nullable=True))
    op.add_column('provider_usage', sa.Column('currency', sa.String(3), nullable=True))


def downgrade():
    op.drop_column('provider_usage', 'currency')
    op.drop_column('provider_usage', 'cost')
