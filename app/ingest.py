"""Source ingestion, identity resolution, exclusions and invalidation of derived data.

Upserts are keyed by workspace + provider + provider account + provider object ID, so
repeated pages, retries and live/history duplicates never create extra rows. People are
matched only by exact provider identifiers; similar names never merge.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid

from sqlalchemy import and_, delete, exists, func, or_, select, update

from . import jobs
from .models import (Answer, AnswerSource, Chunk, ChunkSource, Claim, ClaimSource, Draft, EventPerson, Exclusion,
                     Person, PersonIdentifier, RelationshipNote, Source, SourceParticipant, Account, WhatsAppConnector,
                     Connection)
from .repo import Scoped

EMAIL_RE = re.compile(r'^[^@\s<>]{1,200}@[A-Za-z0-9.-]{1,200}\.[A-Za-z]{2,}$')
WA_RE = re.compile(r'^[0-9a-z._-]{1,80}@(c\.us|lid|s\.whatsapp\.net)$')
REMOVED_ANSWER = 'This answer was removed because evidence it relied on was deleted or excluded.'


def norm_email(value: str | None) -> str | None:
    v = (value or '').strip().lower()
    return v if EMAIL_RE.match(v) else None


def norm_wa(value: str | None) -> str | None:
    v = (value or '').strip().lower()
    return v if WA_RE.match(v) else None


def normalize(kind: str, value: str | None) -> str | None:
    return norm_email(value) if kind == 'email' else norm_wa(value) if kind == 'whatsapp' else None


@dataclass
class Participant:
    kind: str          # email | whatsapp
    identifier: str
    role: str
    label: str | None = None


@dataclass
class SourceRecord:
    provider: str
    provider_account: str
    provider_object_id: str
    kind: str
    version: str | None = None
    conversation_id: str | None = None
    conversation_title: str | None = None
    title: str | None = None
    author: Participant | None = None
    from_me: bool = False
    occurred_at: datetime | None = None
    ends_at: datetime | None = None
    body: str = ''
    body_truncated: bool = False
    meta: dict = field(default_factory=dict)
    participants: list[Participant] = field(default_factory=list)
    deleted: bool = False


def self_identifiers(ws: Scoped) -> set[str]:
    ids = set()
    acct = ws.s.scalars(select(Account).where(Account.workspace_id == ws.wid)).first()
    if acct:
        ids.add(acct.email.lower())
    conn = ws.first(ws.q(Connection).where(Connection.provider == 'google'))
    if conn and conn.email:
        ids.add(conn.email.lower())
    wa = ws.first(ws.q(WhatsAppConnector))
    if wa and wa.account_wid:
        ids.add(wa.account_wid.lower())
    return ids


def resolve_person(ws: Scoped, kind: str, value: str, label: str | None, self_ids: set[str]) -> Person | None:
    value = normalize(kind, value)
    if not value or value in self_ids:
        return None
    ident = ws.first(ws.q(PersonIdentifier).where(PersonIdentifier.kind == kind, PersonIdentifier.value == value))
    if ident:
        person = ws.get(Person, ident.person_id)
        if person and label and person.name_source == 'provider' and person.display_name == value:
            person.display_name = label[:200]
        if label and not ident.label:
            ident.label = label[:200]
        return person
    person = ws.add(Person(id=uuid.uuid4(), display_name=(label or value)[:200], name_source='provider'))
    ws.add(PersonIdentifier(id=uuid.uuid4(), person_id=person.id, kind=kind, value=value, label=(label or None)))
    ws.s.flush()
    return person


def is_excluded(ws: Scoped, provider: str, object_id: str, conversation_id: str | None) -> bool:
    keys = [and_(Exclusion.scope == 'object', Exclusion.key == object_id)]
    if conversation_id:
        keys.append(and_(Exclusion.scope == 'conversation', Exclusion.key == conversation_id))
    return ws.first(ws.q(Exclusion).where(Exclusion.provider == provider, or_(*keys))) is not None


def content_hash(rec: SourceRecord) -> str:
    payload = json.dumps([rec.title, rec.body, rec.occurred_at.isoformat() if rec.occurred_at else None,
                          rec.ends_at.isoformat() if rec.ends_at else None, rec.deleted, rec.meta.get('status'),
                          sorted((p.kind, p.identifier, p.role) for p in rec.participants)], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class IngestResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    deleted: int = 0
    conversations: set = field(default_factory=set)
    changes: list = field(default_factory=list)  # (source id, version) of every change


def upsert(ws: Scoped, rec: SourceRecord, self_ids: set[str], result: IngestResult) -> Source | None:
    if is_excluded(ws, rec.provider, rec.provider_object_id, rec.conversation_id):
        result.skipped += 1
        return None
    existing = ws.first(ws.q(Source).where(Source.provider == rec.provider, Source.provider_account == rec.provider_account,
                                           Source.provider_object_id == rec.provider_object_id).with_for_update())
    if rec.deleted:
        if existing and existing.deleted_at is None:
            invalidate(ws, [existing.id])
            existing.deleted_at = datetime.now(timezone.utc)
            existing.body = ''
            existing.version = 'deleted'
            existing.content_hash = hashlib.sha256(b'deleted').hexdigest()
            existing.meta = {**(existing.meta or {}), 'status': rec.meta.get('status', 'deleted')}
            result.deleted += 1
            result.conversations.add((existing.provider, existing.conversation_id))
            result.changes.append((str(existing.id), 'deleted'))
        else:
            result.unchanged += 1
        return existing
    digest = content_hash(rec)
    version = (rec.version or digest[:24])[:200]
    if existing and existing.content_hash == digest and existing.deleted_at is None:
        result.unchanged += 1
        return existing
    author_person = None
    if rec.author:
        author_person = resolve_person(ws, rec.author.kind, rec.author.identifier, rec.author.label, self_ids)
    fields = dict(
        version=version, conversation_id=(rec.conversation_id or '')[:400] or None,
        conversation_title=(rec.conversation_title or '')[:300] or None, title=(rec.title or '')[:300] or None,
        author=normalize(rec.author.kind, rec.author.identifier) if rec.author else None,
        author_label=((rec.author.label if rec.author else None) or '')[:200] or None, from_me=rec.from_me,
        occurred_at=rec.occurred_at, ends_at=rec.ends_at, body=rec.body, body_truncated=rec.body_truncated,
        meta=rec.meta, content_hash=digest, deleted_at=None, included=True)
    if existing:
        invalidate(ws, [existing.id], rebuild=False)
        src = existing
        for k, v in fields.items():
            setattr(src, k, v)
        result.updated += 1
    else:
        src = ws.add(Source(id=uuid.uuid4(), provider=rec.provider, provider_account=rec.provider_account,
                            provider_object_id=rec.provider_object_id[:400], kind=rec.kind, **fields))
        result.created += 1
    ws.s.flush()
    ws.delete(SourceParticipant, SourceParticipant.source_id == src.id)
    seen = set()
    for p in ([rec.author] if rec.author else []) + rec.participants:
        value = normalize(p.kind, p.identifier)
        if not value or (value, p.role) in seen:
            continue
        seen.add((value, p.role))
        person = author_person if (rec.author and p is rec.author) else resolve_person(ws, p.kind, value, p.label, self_ids)
        ws.add(SourceParticipant(id=uuid.uuid4(), source_id=src.id, person_id=person.id if person else None,
                                 identifier=value, role=p.role))
    result.conversations.add((src.provider, src.conversation_id))
    result.changes.append((str(src.id), digest))
    return src


def schedule_rebuilds(ws: Scoped, result: IngestResult, token: str = '') -> None:
    """One rebuild job per touched conversation, keyed by the exact changes it covers, so a
    retried page is a no-op and a later change always gets its own job."""
    changes = token + '|' + '|'.join(f'{a}:{b}' for a, b in sorted(result.changes))
    for provider, conv in sorted(result.conversations, key=lambda x: (x[0], x[1] or '')):
        key = 'rebuild:' + hashlib.sha256(f'{provider}|{conv}|{changes}'.encode()).hexdigest()[:40]
        jobs.enqueue(ws.s, ws.wid, 'rebuild_conversation', key, {'provider': provider, 'conversation_id': conv})


def invalidate(ws: Scoped, source_ids: list[uuid.UUID], rebuild: bool = True) -> None:
    """Remove derived data built from these sources: chunks, embeddings, unsupported claims,
    cached answers and draft evidence; clear RSVP suggestions citing them."""
    if not source_ids:
        return
    chunk_ids = list(ws.s.scalars(ws.q(ChunkSource, ChunkSource.chunk_id).where(ChunkSource.source_id.in_(source_ids))))
    convs = set(ws.s.execute(ws.q(Source, Source.provider, Source.conversation_id).where(Source.id.in_(source_ids))).all())
    if chunk_ids:
        ws.delete(Chunk, Chunk.id.in_(chunk_ids))
    claim_ids = list(ws.s.scalars(ws.q(ClaimSource, ClaimSource.claim_id).where(ClaimSource.source_id.in_(source_ids))))
    ws.delete(ClaimSource, ClaimSource.source_id.in_(source_ids))
    if claim_ids:
        unsupported = ~exists().where(ClaimSource.workspace_id == ws.wid, ClaimSource.claim_id == Claim.id)
        ws.delete(Claim, Claim.id.in_(claim_ids), Claim.origin == 'extraction', Claim.status != 'confirmed', unsupported)
        # Confirmed claims stay (the user endorsed them) but no longer cite deleted text.
        ws.update(Claim, Claim.id.in_(claim_ids), Claim.status == 'confirmed', unsupported, basis='user')
    answer_ids = list(ws.s.scalars(ws.q(AnswerSource, AnswerSource.answer_id).where(
        AnswerSource.source_id.in_(source_ids), AnswerSource.answer_id.is_not(None))))
    draft_ids = list(ws.s.scalars(ws.q(AnswerSource, AnswerSource.draft_id).where(
        AnswerSource.source_id.in_(source_ids), AnswerSource.draft_id.is_not(None))))
    if answer_ids:
        ws.update(Answer, Answer.id.in_(answer_ids), answer=REMOVED_ANSWER, stale=True)
        ws.delete(AnswerSource, AnswerSource.answer_id.in_(answer_ids))
    if draft_ids:
        ws.update(Draft, Draft.id.in_(draft_ids), stale=True)
        ws.delete(AnswerSource, AnswerSource.draft_id.in_(draft_ids), AnswerSource.source_id.in_(source_ids))
    ws.update(EventPerson, EventPerson.suggested_source_id.in_(source_ids), suggested_status=None, suggested_source_id=None)
    if rebuild:
        result = IngestResult(conversations=convs, changes=[(str(i), 'invalidated') for i in source_ids])
        schedule_rebuilds(ws, result, 'invalidate:' + jobs.utcnow().isoformat())


def exclude(ws: Scoped, source: Source, scope: str) -> int:
    """Tombstone and delete one source or its whole conversation. Returns sources removed."""
    key = source.provider_object_id if scope == 'object' else source.conversation_id
    if not key:
        scope, key = 'object', source.provider_object_id
    label = source.conversation_title or source.author_label or source.title or source.provider
    exists_row = ws.first(ws.q(Exclusion).where(Exclusion.provider == source.provider, Exclusion.scope == scope,
                                                Exclusion.key == key))
    if not exists_row:
        ws.add(Exclusion(id=uuid.uuid4(), provider=source.provider, scope=scope, key=key, label=(label or '')[:300]))
    if scope == 'object':
        ids = [source.id]
    else:
        ids = list(ws.s.scalars(ws.q(Source, Source.id).where(Source.provider == source.provider,
                                                              Source.conversation_id == source.conversation_id)))
    invalidate(ws, ids)
    ws.delete(Source, Source.id.in_(ids))
    return len(ids)


def delete_provider_data(ws: Scoped, providers: list[str]) -> int:
    ids = list(ws.s.scalars(ws.q(Source, Source.id).where(Source.provider.in_(providers))))
    invalidate(ws, ids, rebuild=False)
    ws.delete(Source, Source.id.in_(ids))
    prune_people(ws)
    return len(ids)


def prune_people(ws: Scoped) -> None:
    """Remove provider-created people with no remaining evidence or user data."""
    used = or_(
        exists().where(SourceParticipant.workspace_id == ws.wid, SourceParticipant.person_id == Person.id),
        exists().where(Claim.workspace_id == ws.wid, Claim.person_id == Person.id),
        exists().where(RelationshipNote.workspace_id == ws.wid, RelationshipNote.person_id == Person.id),
        exists().where(EventPerson.workspace_id == ws.wid, EventPerson.person_id == Person.id),
        exists().where(Draft.workspace_id == ws.wid, Draft.person_id == Person.id))
    ws.delete(Person, Person.pinned.is_(False), Person.name_source == 'provider',
              ~exists().where(PersonIdentifier.workspace_id == ws.wid, PersonIdentifier.person_id == Person.id,
                              PersonIdentifier.linked_by_user.is_(True)), ~used)


# ---- manual identity links

def link_people(ws: Scoped, keep: Person, other: Person) -> None:
    """User-confirmed merge: move identifiers and references from `other` into `keep`."""
    if keep.id == other.id:
        return
    ws.update(PersonIdentifier, PersonIdentifier.person_id == other.id, person_id=keep.id, linked_by_user=True)
    ws.update(PersonIdentifier, PersonIdentifier.person_id == keep.id, linked_by_user=True)
    ws.update(SourceParticipant, SourceParticipant.person_id == other.id, person_id=keep.id)
    ws.update(Claim, Claim.person_id == other.id, person_id=keep.id)
    ws.update(RelationshipNote, RelationshipNote.person_id == other.id, person_id=keep.id)
    ws.update(Draft, Draft.person_id == other.id, person_id=keep.id)
    kept_events = set(ws.s.scalars(ws.q(EventPerson, EventPerson.event_id).where(EventPerson.person_id == keep.id)))
    for ep in ws.all(ws.q(EventPerson).where(EventPerson.person_id == other.id)):
        if ep.event_id in kept_events:
            ws.s.delete(ep)
        else:
            ep.person_id = keep.id
    keep.pinned = keep.pinned or other.pinned
    ws.s.flush()
    ws.s.delete(other)


def unlink_identifier(ws: Scoped, ident: PersonIdentifier) -> Person | None:
    """Split one identifier into its own person, moving evidence it authored."""
    remaining = ws.s.scalar(select(func.count()).select_from(PersonIdentifier).where(
        PersonIdentifier.workspace_id == ws.wid, PersonIdentifier.person_id == ident.person_id))
    if remaining <= 1:
        return None
    new = ws.add(Person(id=uuid.uuid4(), display_name=(ident.label or ident.value)[:200], name_source='provider'))
    ws.s.flush()
    old_person = ident.person_id
    ident.person_id = new.id
    ident.linked_by_user = False
    ws.update(SourceParticipant, SourceParticipant.person_id == old_person, SourceParticipant.identifier == ident.value,
              person_id=new.id)
    authored = select(Source.id).where(Source.workspace_id == ws.wid, Source.author == ident.value)
    moved = select(ClaimSource.claim_id).where(ClaimSource.workspace_id == ws.wid, ClaimSource.source_id.in_(authored))
    ws.update(Claim, Claim.person_id == old_person, Claim.id.in_(moved), person_id=new.id)
    return new


def register_contacts(ws: Scoped, contacts: list[dict], self_ids: set[str]) -> None:
    """Connector contact records. Only explicit WhatsApp id/phone mappings join identifiers."""
    for c in contacts[:200]:
        if not isinstance(c, dict):
            continue
        ids = [norm_wa(x) for x in [c.get('id'), c.get('phone_identity'), *(c.get('aliases') or [])[:20]]]
        ids = [x for x in dict.fromkeys(ids) if x and x not in self_ids]
        if not ids:
            continue
        label = next((str(c[k])[:200] for k in ('saved_name', 'profile_name') if isinstance(c.get(k), str) and c[k].strip()), None)
        owners = {i.value: i.person_id for i in ws.all(ws.q(PersonIdentifier).where(
            PersonIdentifier.kind == 'whatsapp', PersonIdentifier.value.in_(ids)))}
        people = set(owners.values())
        if len(people) > 1:
            continue  # provider identifiers already belong to different people; never merge implicitly
        person = ws.get(Person, next(iter(people))) if people else resolve_person(ws, 'whatsapp', ids[0], label, self_ids)
        if person is None:
            continue
        for value in ids:
            if value not in owners and not ws.first(ws.q(PersonIdentifier).where(PersonIdentifier.kind == 'whatsapp',
                                                                                   PersonIdentifier.value == value)):
                ws.add(PersonIdentifier(id=uuid.uuid4(), person_id=person.id, kind='whatsapp', value=value, label=label))
        if label and person.name_source == 'provider' and (person.display_name in ids or '@' in person.display_name):
            person.display_name = label
        ws.s.flush()
