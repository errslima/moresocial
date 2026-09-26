"""User-authored diary entries use the same source lifecycle as imported evidence."""
from __future__ import annotations

import uuid
from sqlalchemy import select, text

from . import ai, ingest, jobs
from .models import Chunk, ChunkSource, Embedding, Job, SelfProfile, Source
from .repo import Scoped

MAX_CHARS = 30000


def save(ws: Scoped, body: str, *, entry: Source | None = None, token: str | None = None,
         occurred_at=None, legacy_field: str | None = None) -> Source:
    body = body.strip()
    if not body or len(body) > MAX_CHARS:
        raise ValueError(f'Write an entry of 1–{MAX_CHARS:,} characters.')
    ident = entry.provider_object_id if entry else token or str(uuid.uuid4())
    ws.s.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                 {'key': 'diary:' + str(ws.wid) + ':' + ident})
    if entry is None and token:
        existing = ws.first(ws.q(Source).where(Source.provider == 'diary', Source.provider_object_id == ident))
        if existing:
            return existing
    when = entry.occurred_at if entry else occurred_at or jobs.utcnow()
    result = ingest.IngestResult()
    rec = ingest.SourceRecord(provider='diary', provider_account='self', provider_object_id=ident,
        conversation_id='diary:' + ident, kind='diary', version=uuid.uuid4().hex, from_me=True, occurred_at=when,
        title='Diary entry' if not legacy_field else 'Imported profile: ' + legacy_field,
        body=body, meta={'legacy_field': legacy_field} if legacy_field else {})
    source = ingest.upsert(ws, rec, set(), result)
    if source is None:
        raise ValueError('That entry was deleted. Reload the page to write a new entry.')
    ingest.schedule_rebuilds(ws, result, 'diary:' + rec.version)
    return source


def migrate_profiles(session):
    for profile in session.scalars(select(SelfProfile)):
        ws = Scoped(session, profile.workspace_id)
        for field in ('preferences', 'goals'):
            body = getattr(profile, field).strip()
            ident = 'legacy-profile-' + field
            if body and not ws.first(ws.q(Source).where(Source.provider == 'diary', Source.provider_object_id == ident)):
                save(ws, body, token=ident, occurred_at=profile.updated_at, legacy_field=field)


def entries(ws: Scoped, offset: int = 0, limit: int = 20) -> list[dict]:
    rows = ws.all(ws.q(Source).where(Source.provider == 'diary', Source.deleted_at.is_(None))
                  .order_by(Source.occurred_at.desc(), Source.id).offset(offset).limit(limit))
    space = ai.embedder().embedding_space if ai.embedder().embedding_dimension else None
    out = []
    for entry in rows:
        chunks = ws.all(ws.q(Chunk).where(Chunk.id.in_(ws.q(ChunkSource, ChunkSource.chunk_id)
                           .where(ChunkSource.source_id == entry.id))))
        ready = bool(chunks) and space and all(ws.first(ws.q(Embedding).where(
            Embedding.chunk_id == c.id, Embedding.space == space)) for c in chunks)
        extracted = bool(chunks) and all(c.extracted_at is not None for c in chunks)
        failed = ws.first(ws.q(Job).where(Job.status == 'failed',
            Job.payload['chunk_id'].astext.in_([str(c.id) for c in chunks]))) if chunks else None
        status = ('Searchable · observations processed' if ready and extracted else
                  'Searchable · observations pending' if ready else
                  'Saved · search indexing pending' if space else 'Saved · search indexing waiting for AI setup')
        if failed:
            status += ' · processing needs attention'
        out.append({'source': entry, 'status': status})
    return out
