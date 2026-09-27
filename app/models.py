"""Database schema. Every tenant-owned row carries workspace_id; relationships between
owned rows use composite (workspace_id, id) foreign keys so a row can never point at
another workspace's object."""
from __future__ import annotations

from datetime import datetime, date
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (Boolean, CheckConstraint, Computed, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, Numeric,
                        BigInteger, String, Text, UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

PIPELINE_VERSION = 'v2-segments'


class Base(DeclarativeBase):
    pass


def pk():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def ws_fk():
    return mapped_column(UUID(as_uuid=True), ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False, index=True)


def now_col(**kw):
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, **kw)


def owned(table, *cols, **kw):
    """Composite FK (workspace_id, col) -> table(workspace_id, id)."""
    return ForeignKeyConstraint(['workspace_id', *cols], [f'{table}.workspace_id', f'{table}.id'], **kw)


class Workspace(Base):
    __tablename__ = 'workspaces'
    id: Mapped[uuid.UUID] = pk()
    status: Mapped[str] = mapped_column(String(20), default='active', nullable=False)  # active|deleting
    timezone: Mapped[str] = mapped_column(String(64), default='Europe/Amsterdam', nullable=False)
    ai_preference: Mapped[str | None] = mapped_column(String(20))  # anthropic | openai, when both keys work
    created_at: Mapped[datetime] = now_col()


class Account(Base):
    __tablename__ = 'accounts'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'),)
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('workspaces.id', ondelete='CASCADE'),
                                                    nullable=False, unique=True)
    google_sub: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = now_col()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Session(Base):
    __tablename__ = 'sessions'
    __table_args__ = (owned('accounts', 'account_id', ondelete='CASCADE'),)
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = now_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthFlow(Base):
    """One row per authorization attempt, so concurrent tabs/users never overwrite each other."""
    __tablename__ = 'oauth_flows'
    id: Mapped[uuid.UUID] = pk()
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    browser_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cookie_name: Mapped[str] = mapped_column(String(64), nullable=False)
    verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)  # login | grant
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey('workspaces.id', ondelete='CASCADE'))
    created_at: Mapped[datetime] = now_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Connection(Base):
    __tablename__ = 'connections'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'), UniqueConstraint('workspace_id', 'provider'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    # google (OAuth grant) | anthropic | openai (user API key: access_token_enc holds the key,
    # provider_account a non-reversible fingerprint, key_hint its last four characters)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_account: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    access_token_enc: Mapped[str | None] = mapped_column(Text)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # active | reconnect_required | disconnected
    state: Mapped[str] = mapped_column(String(30), default='active', nullable=False)
    detail: Mapped[str | None] = mapped_column(String(300))
    key_hint: Mapped[str | None] = mapped_column(String(8))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = now_col()
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())


class SyncStream(Base):
    __tablename__ = 'sync_streams'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'), UniqueConstraint('workspace_id', 'connection_id', 'stream'),
                      owned('connections', 'connection_id', ondelete='CASCADE'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    stream: Mapped[str] = mapped_column(String(20), nullable=False)  # gmail | calendar
    # never_synced | syncing | ok | error | disabled | paused
    status: Mapped[str] = mapped_column(String(20), default='never_synced', nullable=False)
    cursor: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    items_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(300))


class WhatsAppConnector(Base):
    __tablename__ = 'whatsapp_connectors'
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('workspaces.id', ondelete='CASCADE'),
                                                    nullable=False, unique=True)
    desired: Mapped[str] = mapped_column(String(20), default='running', nullable=False)  # running|stopped|deleted
    # pending|capacity|starting|pairing|loading|ready|disconnected|error|stopped|removed
    observed: Mapped[str] = mapped_column(String(20), default='pending', nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    key_enc: Mapped[str] = mapped_column(Text, nullable=False)
    account_wid: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    detail: Mapped[str | None] = mapped_column(String(300))
    requested_at: Mapped[datetime] = now_col()
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())
    # Last authenticated connector heartbeat, stamped with server receipt time (NULL: never
    # reported, e.g. a connector image from before heartbeats). See app/whatsapp_health.py.
    health_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False)


class ConnectorCleanup(Base):
    """Operator cleanup outbox. Intentionally survives workspace/account deletion.
    Contains only the resource ID; removed after the provisioner acknowledges cleanup.
    """
    __tablename__ = 'connector_cleanups'
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = now_col()


class Person(Base):
    __tablename__ = 'people'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'),)
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_source: Mapped[str] = mapped_column(String(20), default='provider', nullable=False)  # provider | user
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = now_col()


class PersonIdentifier(Base):
    __tablename__ = 'person_identifiers'
    __table_args__ = (UniqueConstraint('workspace_id', 'kind', 'value'),
                      owned('people', 'person_id', ondelete='CASCADE'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # email | whatsapp
    value: Mapped[str] = mapped_column(String(320), nullable=False)
    label: Mapped[str | None] = mapped_column(String(200))
    linked_by_user: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = now_col()


class Source(Base):
    __tablename__ = 'sources'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'),
                      UniqueConstraint('workspace_id', 'provider', 'provider_account', 'provider_object_id'),
                      Index('ix_sources_conversation', 'workspace_id', 'provider', 'conversation_id'),
                      Index('ix_sources_time', 'workspace_id', 'occurred_at'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    provider: Mapped[str] = mapped_column(String(20), nullable=False)  # gmail | calendar | whatsapp
    provider_account: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_object_id: Mapped[str] = mapped_column(String(400), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(400))
    conversation_title: Mapped[str | None] = mapped_column(String(300))
    version: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # email | event | message
    title: Mapped[str | None] = mapped_column(String(300))
    author: Mapped[str | None] = mapped_column(String(320))   # provider identifier
    author_label: Mapped[str | None] = mapped_column(String(200))
    from_me: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    body: Mapped[str] = mapped_column(Text, default='', nullable=False)
    body_truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    included: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # provider deletion/cancellation
    created_at: Mapped[datetime] = now_col()
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())


class SuggestionReview(Base):
    __tablename__ = 'suggestion_reviews'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'),
                     UniqueConstraint('workspace_id', 'conversation_id'),
                     # Lets proactive suggestions prove their target is this review's conversation.
                     UniqueConstraint('workspace_id', 'id', 'conversation_id', name='uq_suggestion_reviews_target'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    conversation_id: Mapped[str] = mapped_column(String(400), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


LEGACY_KINDS_SQL = "('reply', 'promise', 'follow_up')"


class Suggestion(Base):
    """One action in the Suggestions inbox. `conversation` rows come from chat reviews and are
    anchored to a message; `proactive` rows are prepared by the assistant from policy (event
    checkpoint, contact rhythm, a shareable update) for an exact one-to-one target. Nothing
    here can send: only the WhatsApp composer's explicit Send message does."""
    __tablename__ = 'suggestions'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'),
                     UniqueConstraint('workspace_id', 'review_id', 'anchor_id', 'kind'),
                     UniqueConstraint('workspace_id', 'action_key', name='uq_suggestions_action_key'),
                     owned('suggestion_reviews', 'review_id', ondelete='CASCADE'),
                     owned('sources', 'anchor_id', ondelete='CASCADE'),
                     owned('people', 'person_id', ondelete='CASCADE'),
                     owned('events', 'event_id', ondelete='CASCADE', name='fk_suggestions_event'),
                     owned('shareable_updates', 'update_id', ondelete='CASCADE', name='fk_suggestions_update'),
                     ForeignKeyConstraint(['workspace_id', 'review_id', 'target_chat'],
                                          ['suggestion_reviews.workspace_id', 'suggestion_reviews.id',
                                           'suggestion_reviews.conversation_id'], name='fk_suggestions_target_review'),
                     CheckConstraint("producer IN ('conversation', 'proactive')", name='ck_suggestions_producer'),
                     CheckConstraint(f"(producer = 'conversation') = (kind IN {LEGACY_KINDS_SQL})", name='ck_suggestions_producer_kind'),
                     CheckConstraint(f"kind NOT IN {LEGACY_KINDS_SQL} OR anchor_id IS NOT NULL", name='ck_suggestions_legacy_anchor'),
                     CheckConstraint("producer = 'conversation' OR (target_account IS NOT NULL AND target_chat IS NOT NULL)",
                                     name='ck_suggestions_proactive_target'),
                     CheckConstraint("event_link IS NULL OR event_link IN ('auto', 'user')", name='ck_suggestions_event_link'),
                     CheckConstraint("event_link IS DISTINCT FROM 'auto' OR event_id IS NOT NULL", name='ck_suggestions_auto_event'),
                     CheckConstraint("draft_origin IS NULL OR draft_origin IN ('generated', 'user')", name='ck_suggestions_draft_origin'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    anchor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    producer: Mapped[str] = mapped_column(String(20), default='conversation', server_default='conversation', nullable=False)
    # Server-created identity: legacy (review, anchor, kind); proactive recipient + purpose + event/cycle/update.
    action_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # Primary ORIGINAL event identity (not a representative, which a split can change).
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    event_link: Mapped[str | None] = mapped_column(String(10))  # auto | user (user with NULL event = "not related")
    target_account: Mapped[str | None] = mapped_column(String(255))
    target_chat: Mapped[str | None] = mapped_column(String(400))
    update_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    rationale: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False)
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    draft_origin: Mapped[str | None] = mapped_column(String(10))  # generated | user
    draft_fingerprint: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(20), default='active', nullable=False)
    disposition: Mapped[str | None] = mapped_column(String(30))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    draft: Mapped[str] = mapped_column(Text, default='', nullable=False)
    draft_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    draft_review_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = now_col()
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())


class SuggestionSource(Base):
    __tablename__ = 'suggestion_sources'
    __table_args__ = (owned('suggestions', 'suggestion_id', ondelete='CASCADE'),
                     owned('sources', 'source_id', ondelete='CASCADE'),
                     UniqueConstraint('workspace_id', 'suggestion_id', 'source_id'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    suggestion_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_version: Mapped[str] = mapped_column(String(200), nullable=False)


class SourceParticipant(Base):
    __tablename__ = 'source_participants'
    __table_args__ = (owned('sources', 'source_id', ondelete='CASCADE'),
                      owned('people', 'person_id', ondelete='CASCADE'),
                      UniqueConstraint('workspace_id', 'source_id', 'identifier', 'role'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    identifier: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # from|to|cc|author|attendee|organizer


class Chunk(Base):
    __tablename__ = 'chunks'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'), UniqueConstraint('workspace_id', 'chunk_key'),
                      Index('ix_chunks_tsv', 'tsv', postgresql_using='gin'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    chunk_key: Mapped[str] = mapped_column(String(64), nullable=False)  # hash(pipeline, source ids+versions)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(400))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(20), nullable=False)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tsv = mapped_column(TSVECTOR, Computed("to_tsvector('simple', text)", persisted=True))
    created_at: Mapped[datetime] = now_col()


class ChunkSource(Base):
    __tablename__ = 'chunk_sources'
    __table_args__ = (owned('chunks', 'chunk_id', ondelete='CASCADE'), owned('sources', 'source_id', ondelete='CASCADE'),
                      UniqueConstraint('workspace_id', 'chunk_id', 'source_id'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_version: Mapped[str] = mapped_column(String(200), nullable=False)
    label: Mapped[str] = mapped_column(String(10), nullable=False)  # S1.. inside the chunk text
    segment_text: Mapped[str] = mapped_column(Text, nullable=False, server_default='')


class Embedding(Base):
    __tablename__ = 'embeddings'
    __table_args__ = (owned('chunks', 'chunk_id', ondelete='CASCADE'), UniqueConstraint('workspace_id', 'chunk_id', 'space'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    space: Mapped[str] = mapped_column(String(180), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(20), nullable=False)
    vector = mapped_column(Vector(), nullable=False)
    created_at: Mapped[datetime] = now_col()


class ObservationGroup(Base):
    __tablename__ = 'observation_groups'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'),)
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    created_at: Mapped[datetime] = now_col()


class Claim(Base):
    __tablename__ = 'claims'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'), UniqueConstraint('workspace_id', 'claim_key'),
                      owned('people', 'person_id', ondelete='CASCADE'),
                      owned('observation_groups', 'group_id'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    auto_group: Mapped[bool] = mapped_column(Boolean, default=True, server_default='true', nullable=False)
    grouping_revision: Mapped[str | None] = mapped_column(String(64))
    subject: Mapped[str] = mapped_column(String(10), nullable=False)  # person | self
    person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    category: Mapped[str] = mapped_column(String(20), nullable=False)  # interest|life_update|preference|commitment|note
    attribute: Mapped[str | None] = mapped_column(String(40))  # e.g. job, location for life updates
    value: Mapped[str] = mapped_column(String(500), nullable=False)
    basis: Mapped[str] = mapped_column(String(10), nullable=False)  # explicit|observed|inferred|user
    # active | confirmed | rejected | superseded | conflicting
    status: Mapped[str] = mapped_column(String(15), default='active', nullable=False)
    origin: Mapped[str] = mapped_column(String(15), nullable=False)  # extraction | user
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = now_col()
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())


class ClaimEmbedding(Base):
    __tablename__ = 'claim_embeddings'
    __table_args__ = (owned('claims', 'claim_id', ondelete='CASCADE'),
                      UniqueConstraint('workspace_id', 'claim_id', 'space'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    space: Mapped[str] = mapped_column(String(180), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    vector = mapped_column(Vector(), nullable=False)


class ClaimSource(Base):
    __tablename__ = 'claim_sources'
    __table_args__ = (owned('claims', 'claim_id', ondelete='CASCADE'), owned('sources', 'source_id', ondelete='CASCADE'),
                      UniqueConstraint('workspace_id', 'claim_id', 'source_id'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_version: Mapped[str] = mapped_column(String(200), nullable=False)


class ClaimRejection(Base):
    """User rejections are authoritative: re-extraction may not recreate the same claim."""
    __tablename__ = 'claim_rejections'
    __table_args__ = (UniqueConstraint('workspace_id', 'rejection_key'),)
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    rejection_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = now_col()


class RelationshipNote(Base):
    __tablename__ = 'relationship_notes'
    __table_args__ = (owned('people', 'person_id', ondelete='CASCADE'),)
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), default='note', nullable=False)  # note|preference|shared_history
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = now_col()
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())


class SelfProfile(Base):
    __tablename__ = 'self_profiles'
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('workspaces.id', ondelete='CASCADE'),
                                                    primary_key=True)
    preferences: Mapped[str] = mapped_column(Text, default='', nullable=False)
    goals: Mapped[str] = mapped_column(Text, default='', nullable=False)
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())


class Event(Base):
    __tablename__ = 'events'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'),
                      ForeignKeyConstraint(['workspace_id', 'canonical_event_id'], ['events.workspace_id', 'events.id'],
                                           name='fk_events_canonical'),
                      Index('ix_events_canonical', 'workspace_id', 'canonical_event_id'),
                      CheckConstraint('canonical_event_id IS NULL OR canonical_event_id <> id', name='ck_events_canonical_not_self'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    activity: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str] = mapped_column(Text, default='', nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default='planning', nullable=False)
    created_at: Mapped[datetime] = now_col()
    origin: Mapped[str] = mapped_column(String(20), default='legacy', nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    locked_fields: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # Materialized group membership (see app/event_identity.py): NULL = represents its group.
    canonical_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # The user paused message preparation; like dismissal, any paused member pauses its group.
    preparation_paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text('false'), nullable=False)


class EventScan(Base):
    __tablename__ = 'event_scans'
    __table_args__ = (UniqueConstraint('workspace_id', 'scan_key'),)
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    scan_key: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class EventMention(Base):
    __tablename__ = 'event_mentions'
    __table_args__ = (owned('events', 'event_id', ondelete='CASCADE'),)
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    scan_key: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class EventIdentityDecision(Base):
    """Audit history of same/different/uncertain verdicts between two original events.
    Stores IDs, hashes and reason codes only, never copied message text or URLs."""
    __tablename__ = 'event_identity_decisions'
    __table_args__ = (CheckConstraint('event_a < event_b', name='ck_event_identity_pair_order'),
                      CheckConstraint("verdict IN ('same', 'different', 'uncertain')", name='ck_event_identity_verdict'),
                      CheckConstraint("origin IN ('user', 'automatic')", name='ck_event_identity_origin'),
                      owned('events', 'event_a', ondelete='CASCADE'), owned('events', 'event_b', ondelete='CASCADE'),
                      Index('ix_event_identity_a', 'workspace_id', 'event_a'),
                      Index('ix_event_identity_b', 'workspace_id', 'event_b'),
                      Index('uq_event_identity_current', 'workspace_id', 'event_a', 'event_b', 'origin',
                            unique=True, postgresql_where=text('active')))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    event_a: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_b: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    verdict: Mapped[str] = mapped_column(String(12), nullable=False)  # same | different | uncertain
    origin: Mapped[str] = mapped_column(String(12), nullable=False)  # user | automatic
    via: Mapped[str] = mapped_column(String(12), default='web', server_default='web', nullable=False)  # web|operator|resolver|lexical
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text('true'), nullable=False)
    # Whether a same verdict participates in grouping (false: observe-mode resolver verdicts).
    applied: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text('true'), nullable=False)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = now_col()
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_reason: Mapped[str | None] = mapped_column(String(40))
    resolver_version: Mapped[str] = mapped_column(String(40), default='', server_default='', nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), default='', server_default='', nullable=False)
    citations: Mapped[list] = mapped_column(JSONB, default=list, server_default='[]', nullable=False)  # [[source_id, hash]]
    reasons: Mapped[list] = mapped_column(JSONB, default=list, server_default='[]', nullable=False)  # reason codes


class EventPairScan(Base):
    """Latest resolver evaluation per pair: unchanged inputs never repeat a paid call."""
    __tablename__ = 'event_pair_scans'
    __table_args__ = (CheckConstraint('event_a < event_b', name='ck_event_pair_scans_order'),
                      UniqueConstraint('workspace_id', 'event_a', 'event_b'),
                      owned('events', 'event_a', ondelete='CASCADE'), owned('events', 'event_b', ondelete='CASCADE'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    event_a: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_b: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    resolver_version: Mapped[str] = mapped_column(String(40), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # evaluated | rejected | stale
    verdict: Mapped[str | None] = mapped_column(String(12))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default='0', nullable=False)
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())


class Answer(Base):
    __tablename__ = 'answers'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'),)
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    insufficient: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = now_col()


class AnswerSource(Base):
    """Evidence referenced by an answer, pinned to a source version."""
    __tablename__ = 'answer_sources'
    __table_args__ = (owned('answers', 'answer_id', ondelete='CASCADE'),
                      owned('sources', 'source_id', ondelete='CASCADE'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    answer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_version: Mapped[str] = mapped_column(String(200), nullable=False)
    label: Mapped[str] = mapped_column(String(10), nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)


class Job(Base):
    __tablename__ = 'jobs'
    __table_args__ = (UniqueConstraint('workspace_id', 'idempotency_key'),
                      Index('ix_jobs_claim', 'status', 'run_after'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # queued | running | done | failed | cancelled | paused
    status: Mapped[str] = mapped_column(String(15), default='queued', nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    run_after: Mapped[datetime] = now_col()
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = now_col()
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())


class UsageBudget(Base):
    """Committed tokens (outstanding reservations + reconciled usage) per scope and day."""
    __tablename__ = 'usage_budgets'
    scope: Mapped[str] = mapped_column(String(60), primary_key=True)  # global | workspace:<uuid>
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class ProviderUsage(Base):
    __tablename__ = 'provider_usage'
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    day: Mapped[date] = mapped_column(Date, nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)  # generate | embed
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), default='anthropic', server_default='anthropic', nullable=False)
    billing: Mapped[str] = mapped_column(String(10), default='operator', server_default='operator', nullable=False)  # operator|user
    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    # OpenRouter reports USD for some responses. NULL means unknown, never zero.
    cost: Mapped[object | None] = mapped_column(Numeric(18, 8))
    currency: Mapped[str | None] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(15), default='reserved', nullable=False)  # reserved|reconciled|failed
    created_at: Mapped[datetime] = now_col()


class OperatorSetting(Base):
    """Global AI configuration set on the admin page; overrides the server environment.
    Secret values (API keys) are encrypted. A removed value is NULLed, not deleted, so
    max(updated_at) always advances and processes notice the change."""
    __tablename__ = 'operator_settings'
    name: Mapped[str] = mapped_column(String(40), primary_key=True)  # ai_provider | <provider>_api_key
    value: Mapped[str | None] = mapped_column(Text)
    hint: Mapped[str | None] = mapped_column(String(8))
    updated_by: Mapped[str | None] = mapped_column(String(320))
    updated_at: Mapped[datetime] = now_col()


class Exclusion(Base):
    """Deletion tombstones: stop a provider object or whole conversation being reimported."""
    __tablename__ = 'exclusions'
    __table_args__ = (UniqueConstraint('workspace_id', 'provider', 'scope', 'key'),)
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    scope: Mapped[str] = mapped_column(String(15), nullable=False)  # object | conversation
    key: Mapped[str] = mapped_column(String(400), nullable=False)
    label: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = now_col()


class NextStepState(Base):
    __tablename__ = 'next_step_states'
    __table_args__ = (UniqueConstraint('workspace_id', 'step_key'),)
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    step_key: Mapped[str] = mapped_column(String(200), nullable=False)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # NULL = dismissed
    created_at: Mapped[datetime] = now_col()


class WhatsAppSend(Base):
    """Durable send receipts. Claim before calling WhatsApp; never retry uncertain sends."""
    __tablename__ = 'whatsapp_sends'
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    account: Mapped[str] = mapped_column(String(255), nullable=False)
    chat: Mapped[str] = mapped_column(String(400), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = now_col()


class AssistantSettings(Base):
    """The workspace's one editable assistant persona. User-authored instructions shape wording
    and initiative only; they cannot change facts, boundaries, privacy or the explicit-send rule."""
    __tablename__ = 'assistant_settings'
    __table_args__ = (
        CheckConstraint("warmth IN ('reserved', 'friendly', 'warm')", name='ck_assistant_warmth'),
        CheckConstraint("directness IN ('gentle', 'balanced', 'direct')", name='ck_assistant_directness'),
        CheckConstraint("humour IN ('none', 'light', 'playful')", name='ck_assistant_humour'),
        CheckConstraint("brevity IN ('brief', 'short', 'detailed')", name='ck_assistant_brevity'),
        CheckConstraint("emoji IN ('none', 'sparing', 'often')", name='ck_assistant_emoji'),
        CheckConstraint("initiative IN ('low', 'balanced', 'high')", name='ck_assistant_initiative'),
        CheckConstraint('char_length(instructions) <= 4000', name='ck_assistant_instructions'),
        CheckConstraint('char_length(examples_preferred) + char_length(examples_avoid) <= 2000', name='ck_assistant_examples'))
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('workspaces.id', ondelete='CASCADE'),
                                                    primary_key=True)
    instructions: Mapped[str] = mapped_column(Text, default='', server_default='', nullable=False)
    warmth: Mapped[str] = mapped_column(String(12), default='friendly', server_default='friendly', nullable=False)
    directness: Mapped[str] = mapped_column(String(12), default='balanced', server_default='balanced', nullable=False)
    humour: Mapped[str] = mapped_column(String(12), default='light', server_default='light', nullable=False)
    brevity: Mapped[str] = mapped_column(String(12), default='short', server_default='short', nullable=False)
    emoji: Mapped[str] = mapped_column(String(12), default='sparing', server_default='sparing', nullable=False)
    language: Mapped[str] = mapped_column(String(40), default='', server_default='', nullable=False)  # '' = match conversation
    examples_preferred: Mapped[str] = mapped_column(Text, default='', server_default='', nullable=False)
    examples_avoid: Mapped[str] = mapped_column(Text, default='', server_default='', nullable=False)
    initiative: Mapped[str] = mapped_column(String(10), default='balanced', server_default='balanced', nullable=False)
    event_preparation: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text('true'), nullable=False)
    check_ins: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text('true'), nullable=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text('false'), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default='1', nullable=False)
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())


class PersonPreference(Base):
    """Explicit per-contact preparation preferences. check_ins NULL = default (pinned only)."""
    __tablename__ = 'person_preferences'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'), UniqueConstraint('workspace_id', 'person_id'),
                      owned('people', 'person_id', ondelete='CASCADE'),
                      CheckConstraint('interval_days BETWEEN 1 AND 365', name='ck_person_preferences_interval'),
                      CheckConstraint('char_length(style) <= 1000', name='ck_person_preferences_style'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    check_ins: Mapped[bool | None] = mapped_column(Boolean)
    interval_days: Mapped[int] = mapped_column(Integer, default=30, server_default='30', nullable=False)
    style: Mapped[str] = mapped_column(Text, default='', server_default='', nullable=False)  # preferred style / boundaries
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default='1', nullable=False)
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())


class ShareableUpdate(Base):
    """A short update about the user that they explicitly chose to share, with an audience.
    Diary entries and observations are private unless the user writes one of these."""
    __tablename__ = 'shareable_updates'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'),
                      CheckConstraint("audience IN ('eligible', 'selected')", name='ck_shareable_updates_audience'),
                      CheckConstraint("state IN ('active', 'revoked')", name='ck_shareable_updates_state'),
                      CheckConstraint("state = 'revoked' OR char_length(text) BETWEEN 1 AND 300", name='ck_shareable_updates_text'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    text: Mapped[str] = mapped_column(String(300), nullable=False)
    audience: Mapped[str] = mapped_column(String(10), nullable=False)  # eligible (check-in contacts) | selected
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(10), default='active', server_default='active', nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default='1', nullable=False)
    created_at: Mapped[datetime] = now_col()
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())


class ShareableUpdatePerson(Base):
    __tablename__ = 'shareable_update_people'
    __table_args__ = (UniqueConstraint('workspace_id', 'update_id', 'person_id'),
                      owned('shareable_updates', 'update_id', ondelete='CASCADE'),
                      owned('people', 'person_id', ondelete='CASCADE'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    update_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class UpdateProposal(Base):
    """Which update was already proposed to which person, so rewording never repeats it.
    Proposal is not delivery: `delivered_at` is set only from a confirmed send receipt."""
    __tablename__ = 'update_proposals'
    __table_args__ = (UniqueConstraint('workspace_id', 'update_id', 'person_id'),
                      owned('shareable_updates', 'update_id', ondelete='CASCADE'),
                      owned('people', 'person_id', ondelete='CASCADE'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    update_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    update_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = now_col()
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_receipt: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # whatsapp_sends.id


class ProactiveReview(Base):
    """Durable state for one proactive candidate identity (recipient + purpose + event, contact
    cycle or update). Work runs as ordinary jobs; this row remembers what was last evaluated so
    unchanged input never repeats a model call."""
    __tablename__ = 'proactive_reviews'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'), UniqueConstraint('workspace_id', 'scope_key'),
                      owned('people', 'person_id', ondelete='CASCADE'),
                      owned('events', 'event_id', ondelete='CASCADE', name='fk_proactive_reviews_event'),
                      owned('shareable_updates', 'update_id', ondelete='CASCADE', name='fk_proactive_reviews_update'),
                      CheckConstraint("kind IN ('event_reminder', 'event_question', 'check_in', 'share_update')",
                                      name='ck_proactive_reviews_kind'),
                      Index('ix_proactive_reviews_due', 'workspace_id', 'next_check_at'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    scope_key: Mapped[str] = mapped_column(String(64), nullable=False)  # = the suggestion action_key it would create
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    update_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    cycle_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default='1', nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), default='', server_default='', nullable=False)
    result: Mapped[str] = mapped_column(String(30), default='pending', server_default='pending', nullable=False)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = now_col()
