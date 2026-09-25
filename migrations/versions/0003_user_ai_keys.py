"""AI keys: users' own keys (connections metadata, provider preference, usage billing) and
the admin page's global AI settings (operator_settings).

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    # Keys reuse `connections` (provider anthropic|openai); all additions are nullable or defaulted.
    op.add_column('connections', sa.Column('key_hint', sa.String(8), nullable=True))
    op.add_column('connections', sa.Column('validated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('workspaces', sa.Column('ai_preference', sa.String(20), nullable=True))
    op.add_column('provider_usage', sa.Column('provider', sa.String(20), nullable=False, server_default='anthropic'))
    op.add_column('provider_usage', sa.Column('billing', sa.String(10), nullable=False, server_default='operator'))
    op.create_table('operator_settings',
                    sa.Column('name', sa.String(40), primary_key=True),
                    sa.Column('value', sa.Text(), nullable=True),
                    sa.Column('hint', sa.String(8), nullable=True),
                    sa.Column('updated_by', sa.String(320), nullable=True),
                    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))


def downgrade():
    op.drop_table('operator_settings')
    op.execute("DELETE FROM connections WHERE provider IN ('anthropic', 'openai')")
    op.drop_column('provider_usage', 'billing')
    op.drop_column('provider_usage', 'provider')
    op.drop_column('workspaces', 'ai_preference')
    op.drop_column('connections', 'validated_at')
    op.drop_column('connections', 'key_hint')
