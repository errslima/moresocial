"""Durable receipts for user-initiated WhatsApp sends."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = '0009'
down_revision = '0008'
branch_labels = depends_on = None


def upgrade():
    op.create_table('whatsapp_sends',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('workspace_id', UUID(as_uuid=True), sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False),
        sa.Column('account', sa.String(255), nullable=False),
        sa.Column('chat', sa.String(400), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('state', sa.String(20), nullable=False),
        sa.Column('message_id', sa.String(300)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index('ix_whatsapp_sends_workspace_id', 'whatsapp_sends', ['workspace_id'])


def downgrade():
    op.drop_table('whatsapp_sends')
