"""Record which sources fed each suggestion's derived text.

Revision ID: 0015
Revises: 0014

Additive. Existing event-linked suggestions are backfilled with every source cited by their
event group, so excluding such a source erases text derived from it. Earlier text derived
from unlinked event candidates cannot be reconstructed and is refreshed on the next review.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0015'
down_revision = '0014'
branch_labels = depends_on = None


def upgrade():
    op.add_column('suggestions', sa.Column('context_sources', JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.get_bind().execute(sa.text("""
        UPDATE suggestions s SET context_sources = COALESCE((
            SELECT jsonb_agg(DISTINCT src)
            FROM events linked
            JOIN events member ON member.workspace_id = linked.workspace_id
                AND COALESCE(member.canonical_event_id, member.id) = COALESCE(linked.canonical_event_id, linked.id)
            JOIN event_mentions m ON m.workspace_id = member.workspace_id AND m.event_id = member.id
            CROSS JOIN LATERAL jsonb_array_elements_text(m.evidence) AS src
            WHERE linked.workspace_id = s.workspace_id AND linked.id = s.event_id), '[]'::jsonb)
        WHERE s.event_id IS NOT NULL"""))


def downgrade():
    op.drop_column('suggestions', 'context_sources')
