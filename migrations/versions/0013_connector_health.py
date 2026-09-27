"""Receipt-time WhatsApp connector heartbeats.

Revision ID: 0013
Revises: 0012

Additive. Existing connectors start without a heartbeat and are shown as "status
unavailable" until a heartbeat-capable connector image reports.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0013'
down_revision = '0012'
branch_labels = depends_on = None


def upgrade():
    op.add_column('whatsapp_connectors', sa.Column('health_checked_at', sa.DateTime(timezone=True)))
    op.add_column('whatsapp_connectors', sa.Column('health', JSONB(), nullable=False,
                                                   server_default=sa.text("'{}'::jsonb")))


def downgrade():
    op.drop_column('whatsapp_connectors', 'health')
    op.drop_column('whatsapp_connectors', 'health_checked_at')
