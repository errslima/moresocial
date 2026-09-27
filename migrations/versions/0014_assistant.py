"""Assistant persona, preferences, shareable updates and proactive suggestion state.

Revision ID: 0014
Revises: 0013

Additive for existing data: every existing suggestion becomes producer `conversation`, keeps
its anchor, uniqueness, state, disposition and draft, and receives the action key the
application derives from its (review, anchor, kind) identity. Existing drafts are marked as
user-owned so nothing ever regenerates over them.

Rollback: application images before 0014 cannot read proactive suggestions (no anchor, new
kinds). The downgrade refuses while any assistant data or proactive suggestion exists;
prefer pausing proactive preparation and a forward fix. Never drop user data to roll back.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = '0014'
down_revision = '0013'
branch_labels = depends_on = None

LEGACY = "('reply', 'promise', 'follow_up')"
NEW_TABLES = ('proactive_reviews', 'update_proposals', 'shareable_update_people', 'shareable_updates',
              'person_preferences', 'assistant_settings')


def common():
    return [sa.Column('id', UUID(as_uuid=True), primary_key=True),
            sa.Column('workspace_id', UUID(as_uuid=True), sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False)]


def owned(table, col, name=None):
    return sa.ForeignKeyConstraint(['workspace_id', col], [table + '.workspace_id', table + '.id'], ondelete='CASCADE', name=name)


def upgrade():
    c = op.get_bind()
    c.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.add_column('events', sa.Column('preparation_paused', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.create_unique_constraint('uq_suggestion_reviews_target', 'suggestion_reviews', ['workspace_id', 'id', 'conversation_id'])

    op.create_table('assistant_settings',
        sa.Column('workspace_id', UUID(as_uuid=True), sa.ForeignKey('workspaces.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('instructions', sa.Text(), nullable=False, server_default=''),
        sa.Column('warmth', sa.String(12), nullable=False, server_default='friendly'),
        sa.Column('directness', sa.String(12), nullable=False, server_default='balanced'),
        sa.Column('humour', sa.String(12), nullable=False, server_default='light'),
        sa.Column('brevity', sa.String(12), nullable=False, server_default='short'),
        sa.Column('emoji', sa.String(12), nullable=False, server_default='sparing'),
        sa.Column('language', sa.String(40), nullable=False, server_default=''),
        sa.Column('examples_preferred', sa.Text(), nullable=False, server_default=''),
        sa.Column('examples_avoid', sa.Text(), nullable=False, server_default=''),
        sa.Column('initiative', sa.String(10), nullable=False, server_default='balanced'),
        sa.Column('event_preparation', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('check_ins', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('paused', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('revision', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("warmth IN ('reserved', 'friendly', 'warm')", name='ck_assistant_warmth'),
        sa.CheckConstraint("directness IN ('gentle', 'balanced', 'direct')", name='ck_assistant_directness'),
        sa.CheckConstraint("humour IN ('none', 'light', 'playful')", name='ck_assistant_humour'),
        sa.CheckConstraint("brevity IN ('brief', 'short', 'detailed')", name='ck_assistant_brevity'),
        sa.CheckConstraint("emoji IN ('none', 'sparing', 'often')", name='ck_assistant_emoji'),
        sa.CheckConstraint("initiative IN ('low', 'balanced', 'high')", name='ck_assistant_initiative'),
        sa.CheckConstraint('char_length(instructions) <= 4000', name='ck_assistant_instructions'),
        sa.CheckConstraint('char_length(examples_preferred) + char_length(examples_avoid) <= 2000', name='ck_assistant_examples'))

    op.create_table('person_preferences', *common(),
        sa.Column('person_id', UUID(as_uuid=True), nullable=False),
        sa.Column('check_ins', sa.Boolean()),
        sa.Column('interval_days', sa.Integer(), nullable=False, server_default='30'),
        sa.Column('style', sa.Text(), nullable=False, server_default=''),
        sa.Column('revision', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('workspace_id', 'id'), sa.UniqueConstraint('workspace_id', 'person_id'),
        owned('people', 'person_id'),
        sa.CheckConstraint('interval_days BETWEEN 1 AND 365', name='ck_person_preferences_interval'),
        sa.CheckConstraint('char_length(style) <= 1000', name='ck_person_preferences_style'))

    op.create_table('shareable_updates', *common(),
        sa.Column('text', sa.String(300), nullable=False),
        sa.Column('audience', sa.String(10), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True)),
        sa.Column('state', sa.String(10), nullable=False, server_default='active'),
        sa.Column('revision', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('workspace_id', 'id'),
        sa.CheckConstraint("audience IN ('eligible', 'selected')", name='ck_shareable_updates_audience'),
        sa.CheckConstraint("state IN ('active', 'revoked')", name='ck_shareable_updates_state'),
        sa.CheckConstraint("state = 'revoked' OR char_length(text) BETWEEN 1 AND 300", name='ck_shareable_updates_text'))

    op.create_table('shareable_update_people', *common(),
        sa.Column('update_id', UUID(as_uuid=True), nullable=False),
        sa.Column('person_id', UUID(as_uuid=True), nullable=False),
        sa.UniqueConstraint('workspace_id', 'update_id', 'person_id'),
        owned('shareable_updates', 'update_id'), owned('people', 'person_id'))

    op.create_table('update_proposals', *common(),
        sa.Column('update_id', UUID(as_uuid=True), nullable=False),
        sa.Column('person_id', UUID(as_uuid=True), nullable=False),
        sa.Column('update_revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('delivered_at', sa.DateTime(timezone=True)),
        sa.Column('delivery_receipt', UUID(as_uuid=True)),
        sa.UniqueConstraint('workspace_id', 'update_id', 'person_id'),
        owned('shareable_updates', 'update_id'), owned('people', 'person_id'))

    op.create_table('proactive_reviews', *common(),
        sa.Column('scope_key', sa.String(64), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('person_id', UUID(as_uuid=True), nullable=False),
        sa.Column('event_id', UUID(as_uuid=True)),
        sa.Column('update_id', UUID(as_uuid=True)),
        sa.Column('cycle_start', sa.DateTime(timezone=True)),
        sa.Column('revision', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('fingerprint', sa.String(64), nullable=False, server_default=''),
        sa.Column('result', sa.String(30), nullable=False, server_default='pending'),
        sa.Column('checked_at', sa.DateTime(timezone=True)),
        sa.Column('next_check_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('workspace_id', 'id'), sa.UniqueConstraint('workspace_id', 'scope_key'),
        owned('people', 'person_id'), owned('events', 'event_id', 'fk_proactive_reviews_event'),
        owned('shareable_updates', 'update_id', 'fk_proactive_reviews_update'),
        sa.CheckConstraint("kind IN ('event_reminder', 'event_question', 'check_in', 'share_update')",
                           name='ck_proactive_reviews_kind'))
    op.create_index('ix_proactive_reviews_due', 'proactive_reviews', ['workspace_id', 'next_check_at'])
    for table in ('person_preferences', 'shareable_updates', 'shareable_update_people', 'update_proposals', 'proactive_reviews'):
        op.create_index('ix_' + table + '_workspace_id', table, ['workspace_id'])

    # Suggestions: add, backfill, then constrain. Existing rows are all conversation reviews.
    c.execute(sa.text('LOCK TABLE suggestions IN SHARE ROW EXCLUSIVE MODE'))
    op.add_column('suggestions', sa.Column('producer', sa.String(20), nullable=False, server_default='conversation'))
    op.add_column('suggestions', sa.Column('action_key', sa.String(64)))
    op.add_column('suggestions', sa.Column('event_id', UUID(as_uuid=True)))
    op.add_column('suggestions', sa.Column('event_link', sa.String(10)))
    op.add_column('suggestions', sa.Column('target_account', sa.String(255)))
    op.add_column('suggestions', sa.Column('target_chat', sa.String(400)))
    op.add_column('suggestions', sa.Column('update_id', UUID(as_uuid=True)))
    op.add_column('suggestions', sa.Column('rationale', JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column('suggestions', sa.Column('input_fingerprint', sa.String(64)))
    op.add_column('suggestions', sa.Column('draft_origin', sa.String(10)))
    op.add_column('suggestions', sa.Column('draft_fingerprint', sa.String(64)))
    # Must equal app.suggestions.legacy_action_key(review_id, anchor_id, kind).
    c.execute(sa.text("""UPDATE suggestions SET action_key = encode(sha256(convert_to(
        'conversation:' || review_id::text || ':' || anchor_id::text || ':' || kind, 'UTF8')), 'hex')"""))
    c.execute(sa.text("UPDATE suggestions SET draft_origin = 'user' WHERE draft <> ''"))
    op.alter_column('suggestions', 'action_key', nullable=False)
    op.alter_column('suggestions', 'anchor_id', nullable=True)
    op.create_unique_constraint('uq_suggestions_action_key', 'suggestions', ['workspace_id', 'action_key'])
    op.create_foreign_key('fk_suggestions_event', 'suggestions', 'events', ['workspace_id', 'event_id'],
                          ['workspace_id', 'id'], ondelete='CASCADE')
    op.create_foreign_key('fk_suggestions_update', 'suggestions', 'shareable_updates', ['workspace_id', 'update_id'],
                          ['workspace_id', 'id'], ondelete='CASCADE')
    op.create_foreign_key('fk_suggestions_target_review', 'suggestions', 'suggestion_reviews',
                          ['workspace_id', 'review_id', 'target_chat'], ['workspace_id', 'id', 'conversation_id'])
    op.create_check_constraint('ck_suggestions_producer', 'suggestions', "producer IN ('conversation', 'proactive')")
    op.create_check_constraint('ck_suggestions_producer_kind', 'suggestions', f"(producer = 'conversation') = (kind IN {LEGACY})")
    op.create_check_constraint('ck_suggestions_legacy_anchor', 'suggestions', f"kind NOT IN {LEGACY} OR anchor_id IS NOT NULL")
    op.create_check_constraint('ck_suggestions_proactive_target', 'suggestions',
                               "producer = 'conversation' OR (target_account IS NOT NULL AND target_chat IS NOT NULL)")
    op.create_check_constraint('ck_suggestions_event_link', 'suggestions', "event_link IS NULL OR event_link IN ('auto', 'user')")
    op.create_check_constraint('ck_suggestions_auto_event', 'suggestions', "event_link IS DISTINCT FROM 'auto' OR event_id IS NOT NULL")
    op.create_check_constraint('ck_suggestions_draft_origin', 'suggestions',
                               "draft_origin IS NULL OR draft_origin IN ('generated', 'user')")


def downgrade():
    c = op.get_bind()
    if c.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM suggestions WHERE producer <> 'conversation' OR anchor_id IS NULL)")):
        raise RuntimeError('proactive_suggestions_present')
    for table in NEW_TABLES:
        if c.scalar(sa.text(f'SELECT EXISTS (SELECT 1 FROM {table})')):
            raise RuntimeError('assistant_data_present:' + table)
    if c.scalar(sa.text('SELECT EXISTS (SELECT 1 FROM events WHERE preparation_paused)')):
        raise RuntimeError('assistant_data_present:events.preparation_paused')
    for name in ('ck_suggestions_draft_origin', 'ck_suggestions_auto_event', 'ck_suggestions_event_link',
                 'ck_suggestions_proactive_target', 'ck_suggestions_legacy_anchor', 'ck_suggestions_producer_kind',
                 'ck_suggestions_producer'):
        op.drop_constraint(name, 'suggestions', type_='check')
    for name in ('fk_suggestions_target_review', 'fk_suggestions_update', 'fk_suggestions_event'):
        op.drop_constraint(name, 'suggestions', type_='foreignkey')
    op.drop_constraint('uq_suggestions_action_key', 'suggestions', type_='unique')
    op.alter_column('suggestions', 'anchor_id', nullable=False)
    for col in ('draft_fingerprint', 'draft_origin', 'input_fingerprint', 'rationale', 'update_id', 'target_chat',
                'target_account', 'event_link', 'event_id', 'action_key', 'producer'):
        op.drop_column('suggestions', col)
    for table in NEW_TABLES:
        op.drop_table(table)
    op.drop_constraint('uq_suggestion_reviews_target', 'suggestion_reviews', type_='unique')
    op.drop_column('events', 'preparation_paused')
