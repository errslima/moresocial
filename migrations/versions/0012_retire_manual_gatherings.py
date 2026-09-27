"""Retire empty manual gathering tables; keep personal-key data and automatic Events.

Revision ID: 0012
Revises: 0011

Non-additive: stop old web/worker and connector ingestion before upgrading.
Nonempty legacy data causes an atomic refusal, never implicit deletion.
"""
from alembic import op
import sqlalchemy as sa

revision = '0012'
down_revision = '0011'
branch_labels = depends_on = None


def upgrade():
    c = op.get_bind()
    c.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    c.execute(sa.text("SET LOCAL statement_timeout = '60s'"))
    c.execute(sa.text('LOCK TABLE answer_sources, drafts, event_candidates, event_people IN ACCESS EXCLUSIVE MODE'))
    for table in ('drafts', 'event_candidates', 'event_people'):
        if c.scalar(sa.text(f'SELECT EXISTS (SELECT 1 FROM {table})')):
            raise RuntimeError('legacy_gathering_data_present:' + table)
    if c.scalar(sa.text('SELECT EXISTS (SELECT 1 FROM answer_sources WHERE draft_id IS NOT NULL OR answer_id IS NULL)')):
        raise RuntimeError('legacy_gathering_answer_references_present')
    fks = [fk for fk in sa.inspect(c).get_foreign_keys('answer_sources')
           if fk['constrained_columns'] == ['workspace_id', 'draft_id'] and fk['referred_table'] == 'drafts']
    if len(fks) != 1:
        raise RuntimeError('legacy_gathering_foreign_key_unexpected')
    op.drop_constraint(fks[0]['name'], 'answer_sources', type_='foreignkey')
    op.drop_index('ix_answer_sources_draft_id', table_name='answer_sources')
    op.drop_column('answer_sources', 'draft_id')
    for table in ('drafts', 'event_people', 'event_candidates'):
        op.drop_table(table)


def downgrade():
    # Recreate exactly the empty structures from 0001; no application ORM imports.
    op.create_table('drafts',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.UUID(), nullable=False),
        sa.Column('event_id', sa.UUID(), nullable=True),
        sa.Column('person_id', sa.UUID(), nullable=True),
        sa.Column('purpose', sa.String(length=20), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('edited', sa.Boolean(), nullable=False),
        sa.Column('stale', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['workspace_id', 'event_id'], ['events.workspace_id', 'events.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id', 'person_id'], ['people.workspace_id', 'people.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('workspace_id', 'id')
        )
    op.create_index(op.f('ix_drafts_event_id'), 'drafts', ['event_id'], unique=False)
    op.create_index(op.f('ix_drafts_workspace_id'), 'drafts', ['workspace_id'], unique=False)
    op.create_table('event_candidates',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.UUID(), nullable=False),
        sa.Column('event_id', sa.UUID(), nullable=False),
        sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('chosen', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['workspace_id', 'event_id'], ['events.workspace_id', 'events.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
        )
    op.create_index(op.f('ix_event_candidates_event_id'), 'event_candidates', ['event_id'], unique=False)
    op.create_index(op.f('ix_event_candidates_workspace_id'), 'event_candidates', ['workspace_id'], unique=False)
    op.create_table('event_people',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.UUID(), nullable=False),
        sa.Column('event_id', sa.UUID(), nullable=False),
        sa.Column('person_id', sa.UUID(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('status_changed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('suggested_status', sa.String(length=20), nullable=True),
        sa.Column('suggested_source_id', sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(['workspace_id', 'event_id'], ['events.workspace_id', 'events.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id', 'person_id'], ['people.workspace_id', 'people.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('workspace_id', 'event_id', 'person_id'),
        sa.UniqueConstraint('workspace_id', 'id')
        )
    op.create_index(op.f('ix_event_people_event_id'), 'event_people', ['event_id'], unique=False)
    op.create_index(op.f('ix_event_people_workspace_id'), 'event_people', ['workspace_id'], unique=False)

    op.add_column('answer_sources', sa.Column('draft_id', sa.UUID(), nullable=True))
    op.create_foreign_key('answer_sources_workspace_id_draft_id_fkey', 'answer_sources', 'drafts',
                          ['workspace_id', 'draft_id'], ['workspace_id', 'id'], ondelete='CASCADE')
    op.create_index('ix_answer_sources_draft_id', 'answer_sources', ['draft_id'], unique=False)
