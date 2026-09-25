"""Database schema. Every tenant-owned row carries workspace_id; relationships between
owned rows use composite (workspace_id, id) foreign keys so a row can never point at
another workspace's object."""
from __future__ import annotations

from datetime import datetime, date
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (Boolean, Computed, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer,
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
    provider: Mapped[str] = mapped_column(String(20), nullable=False)  # google
    provider_account: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    access_token_enc: Mapped[str | None] = mapped_column(Text)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # active | reconnect_required | disconnected
    state: Mapped[str] = mapped_column(String(30), default='active', nullable=False)
    detail: Mapped[str | None] = mapped_column(String(300))
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
    __table_args__ = (owned('chunks', 'chunk_id', ondelete='CASCADE'), UniqueConstraint('workspace_id', 'chunk_id', 'model'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(20), nullable=False)
    vector = mapped_column(Vector(), nullable=False)
    created_at: Mapped[datetime] = now_col()


class Claim(Base):
    __tablename__ = 'claims'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'), UniqueConstraint('workspace_id', 'claim_key'),
                      owned('people', 'person_id', ondelete='CASCADE'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
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
    __table_args__ = (UniqueConstraint('workspace_id', 'id'),)
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    activity: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str] = mapped_column(Text, default='', nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default='planning', nullable=False)
    created_at: Mapped[datetime] = now_col()


class EventCandidate(Base):
    __tablename__ = 'event_candidates'
    __table_args__ = (owned('events', 'event_id', ondelete='CASCADE'),)
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    chosen: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class EventPerson(Base):
    __tablename__ = 'event_people'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'), UniqueConstraint('workspace_id', 'event_id', 'person_id'),
                      owned('events', 'event_id', ondelete='CASCADE'), owned('people', 'person_id', ondelete='CASCADE'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # not_contacted | awaiting_reply | accepted | declined | maybe
    status: Mapped[str] = mapped_column(String(20), default='not_contacted', nullable=False)
    status_changed_at: Mapped[datetime] = now_col()
    suggested_status: Mapped[str | None] = mapped_column(String(20))
    # Set only from a workspace-scoped lookup; cleared when that source is excluded or deleted.
    suggested_source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class Draft(Base):
    __tablename__ = 'drafts'
    __table_args__ = (UniqueConstraint('workspace_id', 'id'), owned('events', 'event_id', ondelete='CASCADE'),
                      owned('people', 'person_id', ondelete='CASCADE'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)  # invitation | follow_up
    text: Mapped[str] = mapped_column(Text, nullable=False)
    edited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = now_col()
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
    """Evidence referenced by an answer or a draft, pinned to a source version."""
    __tablename__ = 'answer_sources'
    __table_args__ = (owned('answers', 'answer_id', ondelete='CASCADE'), owned('drafts', 'draft_id', ondelete='CASCADE'),
                      owned('sources', 'source_id', ondelete='CASCADE'))
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    answer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    draft_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
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
    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(15), default='reserved', nullable=False)  # reserved|reconciled|failed
    created_at: Mapped[datetime] = now_col()


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
