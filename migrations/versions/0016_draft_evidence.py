"""Evidence-grounded drafts: evidence bundles, a workspace evidence revision, conversation
voice mode and claim correction lineage.

Revision ID: 0016
Revises: 0015

Additive. Existing assistant settings whose tone values all equal the defaults switch to the
new `conversation` style mode; any deliberate non-default tone value becomes `custom`, so
the user's choice keeps applying. Storage cannot tell "chose every default" from "never
changed them"; the tone columns are retained, so such a user can opt back into custom mode.
Existing drafts are kept unchanged and have no bundle: the application labels them as
written with the previous context policy and offers an explicit rewrite.

Earlier claim corrections have no replacement link and none is guessed.

Two triggers keep the source-erasure contract even for application code without bundle
support (a rollback to 3fcf2b4): one removes a suggestion's bundle whenever its draft or
context sources are erased; the other merges the bundle's dependencies back whenever code
replaces `context_sources` wholesale while the draft still exists, so deleting any of them
still finds and erases the draft.

Downgrade refuses while any correction link exists (it would be lost); bundles are derived
data and the style mode is recomputable from the retained tone values.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = '0016'
down_revision = '0015'
branch_labels = depends_on = None

DEFAULT_TONE = "warmth = 'friendly' AND directness = 'balanced' AND humour = 'light' AND brevity = 'short' AND emoji = 'sparing'"


def upgrade():
    c = op.get_bind()
    c.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.add_column('assistant_settings', sa.Column('style_mode', sa.String(12), nullable=False,
                                                  server_default='conversation'))
    c.execute(sa.text(f"UPDATE assistant_settings SET style_mode = 'custom' WHERE NOT ({DEFAULT_TONE})"))
    op.create_check_constraint('ck_assistant_style_mode', 'assistant_settings', "style_mode IN ('conversation', 'custom')")

    op.add_column('claims', sa.Column('corrects_id', UUID(as_uuid=True)))
    op.create_foreign_key('fk_claims_corrects', 'claims', 'claims', ['workspace_id', 'corrects_id'],
                          ['workspace_id', 'id'], ondelete='SET NULL (corrects_id)')

    op.create_table('evidence_revisions',
        sa.Column('workspace_id', UUID(as_uuid=True), sa.ForeignKey('workspaces.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('revision', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))

    op.create_table('draft_evidence_bundles',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('workspace_id', UUID(as_uuid=True), sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False),
        sa.Column('suggestion_id', UUID(as_uuid=True), nullable=False),
        sa.Column('policy', sa.String(10), nullable=False),
        sa.Column('version', sa.String(40), nullable=False),
        sa.Column('account', sa.String(255)),
        sa.Column('chat', sa.String(400)),
        sa.Column('evidence_revision', sa.BigInteger(), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('dependency_digest', sa.String(64), nullable=False),
        sa.Column('source_ids', JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('payload', JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('query_vectors', JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('freshness', sa.String(10), nullable=False, server_default='current'),
        sa.Column('proposal', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('validated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('workspace_id', 'id'), sa.UniqueConstraint('workspace_id', 'suggestion_id'),
        sa.ForeignKeyConstraint(['workspace_id', 'suggestion_id'], ['suggestions.workspace_id', 'suggestions.id'],
                                ondelete='CASCADE'),
        sa.CheckConstraint("policy IN ('evidence', 'limited')", name='ck_draft_bundles_policy'),
        sa.CheckConstraint("freshness IN ('current', 'outdated')", name='ck_draft_bundles_freshness'))
    op.create_index('ix_draft_evidence_bundles_workspace_id', 'draft_evidence_bundles', ['workspace_id'])
    op.create_index('ix_draft_bundles_sources', 'draft_evidence_bundles', ['source_ids'], postgresql_using='gin')

    c.execute(sa.text("""
        CREATE FUNCTION draft_bundle_erase() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            DELETE FROM draft_evidence_bundles WHERE workspace_id = NEW.workspace_id AND suggestion_id = NEW.id;
            RETURN NULL;
        END $$"""))
    c.execute(sa.text("""
        CREATE TRIGGER trg_suggestions_erase_bundle AFTER UPDATE OF draft, context_sources ON suggestions
        FOR EACH ROW WHEN ((OLD.draft <> '' AND NEW.draft = '')
                           OR (OLD.context_sources <> '[]'::jsonb AND NEW.context_sources = '[]'::jsonb))
        EXECUTE FUNCTION draft_bundle_erase()"""))
    # Code that replaces context_sources wholesale (a review republishing its action, including
    # rolled-back code) must never drop the bundle's dependencies while its draft still exists.
    c.execute(sa.text("""
        CREATE FUNCTION draft_bundle_keep_sources() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE extra jsonb;
        BEGIN
            IF NEW.draft = '' OR NEW.context_sources = '[]'::jsonb THEN
                RETURN NEW;
            END IF;
            SELECT source_ids INTO extra FROM draft_evidence_bundles
             WHERE workspace_id = NEW.workspace_id AND suggestion_id = NEW.id;
            IF extra IS NOT NULL AND extra <> '[]'::jsonb THEN
                NEW.context_sources := (SELECT COALESCE(jsonb_agg(DISTINCT v ORDER BY v), '[]'::jsonb) FROM (
                    SELECT jsonb_array_elements_text(NEW.context_sources) AS v
                    UNION SELECT jsonb_array_elements_text(extra)) merged);
            END IF;
            RETURN NEW;
        END $$"""))
    c.execute(sa.text("""
        CREATE TRIGGER trg_suggestions_keep_bundle_sources BEFORE UPDATE OF context_sources ON suggestions
        FOR EACH ROW EXECUTE FUNCTION draft_bundle_keep_sources()"""))


def downgrade():
    c = op.get_bind()
    if c.scalar(sa.text('SELECT EXISTS (SELECT 1 FROM claims WHERE corrects_id IS NOT NULL)')):
        raise RuntimeError('claim_corrections_present')
    c.execute(sa.text('DROP TRIGGER IF EXISTS trg_suggestions_keep_bundle_sources ON suggestions'))
    c.execute(sa.text('DROP FUNCTION IF EXISTS draft_bundle_keep_sources()'))
    c.execute(sa.text('DROP TRIGGER IF EXISTS trg_suggestions_erase_bundle ON suggestions'))
    c.execute(sa.text('DROP FUNCTION IF EXISTS draft_bundle_erase()'))
    op.drop_table('draft_evidence_bundles')
    op.drop_table('evidence_revisions')
    op.drop_constraint('fk_claims_corrects', 'claims', type_='foreignkey')
    op.drop_column('claims', 'corrects_id')
    op.drop_constraint('ck_assistant_style_mode', 'assistant_settings', type_='check')
    op.drop_column('assistant_settings', 'style_mode')
