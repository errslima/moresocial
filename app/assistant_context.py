"""One bounded, workspace-scoped context builder for ordinary reviews, proactive preparation
and drafting.

Everything here is read-only. The builder resolves the exact one-to-one destination from
stored messages and provider identities (never from a name), gathers direct messages,
relationship notes, explicit user settings, related Events through their existing field
projection and provenance, and applicable shareable updates. It returns model input with
distinct citation namespaces (M = chat messages, E = event evidence, EV = events, U =
updates), internal maps for server-side validation, and a fingerprint covering all of it.

Imported text is untrusted data. User-authored settings are instructions about style and
initiative only. Another guest's private messages never enter an outgoing draft's context.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from .models import (AssistantSettings, Event, EventMention, Person, PersonIdentifier, PersonPreference, RelationshipNote,
                     ShareableUpdate, ShareableUpdatePerson, Source, UpdateProposal, WhatsAppConnector, Workspace)
from .repo import Scoped

PROMPT_VERSION = 'assistant-v1'
EVENT_CANDIDATES = 5
EVENT_MESSAGES = 40
EVENT_CHARS = 24000
EVENT_MESSAGE_CHARS = 2000
MESSAGE_CHARS = 1600
DEFAULT_INTERVAL = 30
STYLE = {'warmth': ('reserved', 'friendly', 'warm'), 'directness': ('gentle', 'balanced', 'direct'),
         'humour': ('none', 'light', 'playful'), 'brevity': ('brief', 'short', 'detailed'),
         'emoji': ('none', 'sparing', 'often')}
INITIATIVES = ('low', 'balanced', 'high')
ATTENDANCE = {'accepted': 'confirmed', 'maybe': 'unsure', 'declined': 'not joining', 'awaiting_reply': 'invited, no reply yet'}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def tz(ws: Scoped) -> ZoneInfo:
    return ZoneInfo(ws.s.get(Workspace, ws.wid).timezone)


def today(ws: Scoped) -> dict:
    """The workspace-local date. Part of every draft context, so words like "tomorrow" are
    written against a known day and a draft stops being current at local midnight."""
    local = utcnow().astimezone(tz(ws))
    return {'date': local.date().isoformat(), 'weekday': local.strftime('%A')}


def sources_of(messages=(), evc: 'EventContext | None' = None) -> list[str]:
    """Every source whose text or facts enter a context: the messages supplied and all
    sources supporting each supplied event (its projected facts derive from them)."""
    ids = {str(m['id']) for m in messages}
    if evc is not None:
        ids |= {m['id'] for m in evc.messages}
        for e in evc.internal:
            ids |= set(e['sources'])
    return sorted(ids)


# ---------------- explicit user settings

DEFAULTS = dict(instructions='', style_mode='conversation', warmth='friendly', directness='balanced', humour='light',
                brevity='short', emoji='sparing', language='', examples_preferred='', examples_avoid='', initiative='balanced',
                event_preparation=True, check_ins=True, paused=False, revision=0)
STYLE_MODES = ('conversation', 'custom')
# The default voice: the user's own messages to this person are the main guide.
VOICE = '''Write as the user would write to this person. Use the supplied examples of the user's own
messages (style.examples, W labels) as the main guide to language, phrasing, warmth, punctuation, emoji
and length. Adapt to the current situation. Do not imitate the other person's voice. Examples
demonstrate style only: their dates, names, plans, promises and circumstances are NOT current facts and
must not appear in the new message. W labels are never evidence. Follow explicit user wording
preferences where supplied, without adding facts or commitments. When style.coverage is "sparse" or
"none", write plainly and concisely and do not invent familiarity, nicknames or in-jokes.'''


def settings(ws: Scoped):
    """The persona row, or defaults (revision 0) when the user never saved one."""
    return ws.s.get(AssistantSettings, ws.wid) or SimpleNamespace(workspace_id=ws.wid, **DEFAULTS)


def persona(row) -> dict:
    """User-authored style preferences for drafts, as the model sees them. In the default
    conversation mode the legacy tone controls are omitted: the user's own messages to the
    recipient guide style instead. Custom mode supplies the tone controls the user chose."""
    mode = getattr(row, 'style_mode', None) or 'conversation'
    out = {'mode': 'match how I write to this person' if mode == 'conversation' else 'custom tone settings',
           'instructions': row.instructions, 'language': row.language or 'match the conversation',
           'examples_of_preferred_wording': row.examples_preferred, 'examples_of_wording_to_avoid': row.examples_avoid,
           'initiative': row.initiative}
    if mode == 'custom':
        out['style'] = {k: getattr(row, k) for k in STYLE}
    return out


def preference(ws: Scoped, person_id):
    return ws.first(ws.q(PersonPreference).where(PersonPreference.person_id == person_id))


def check_ins_enabled(person: Person, pref) -> bool:
    """Only explicitly enabled contacts, or pinned ones by default: never the whole address book."""
    return pref.check_ins if pref is not None and pref.check_ins is not None else bool(person.pinned)


def relationship(ws: Scoped, person: Person) -> dict:
    pref = preference(ws, person.id)
    return {'check_ins': check_ins_enabled(person, pref), 'interval_days': pref.interval_days if pref else DEFAULT_INTERVAL,
            'style_and_boundaries': pref.style if pref else '', 'revision': pref.revision if pref else 0}


def notes(ws: Scoped, person_id) -> list[dict]:
    rows = ws.all(ws.q(RelationshipNote).where(RelationshipNote.person_id == person_id)
                  .order_by(RelationshipNote.created_at.desc()).limit(10))
    return [{'id': str(n.id), 'kind': n.kind, 'text': n.body[:1500], 'at': n.updated_at.isoformat()} for n in rows]


# ---------------- destination

@dataclass
class Destination:
    account: str | None
    chat: str | None
    reason: str | None = None   # why there is no target: no_whatsapp_identity | no_conversation | ambiguous_account

    @property
    def ok(self) -> bool:
        return self.reason is None


def destination(ws: Scoped, person_id) -> Destination:
    """The exact one-to-one WhatsApp conversation for a person, from stored messages and
    provider identities. Several linked accounts with no current one are ambiguous."""
    from .ingest import norm_wa, self_identifiers
    mine = self_identifiers(ws)
    idents = [v for v in ws.s.scalars(ws.q(PersonIdentifier, PersonIdentifier.value).where(
        PersonIdentifier.person_id == person_id, PersonIdentifier.kind == 'whatsapp')) if norm_wa(v) and v not in mine]
    if not idents:
        return Destination(None, None, 'no_whatsapp_identity')
    rows = ws.s.execute(ws.q(Source, Source.provider_account, Source.conversation_id, func.max(Source.occurred_at))
        .where(Source.provider == 'whatsapp', Source.included.is_(True), Source.deleted_at.is_(None),
               Source.conversation_id.in_(idents), Source.meta['is_group'].as_boolean().is_not(True))
        .group_by(Source.provider_account, Source.conversation_id)).all()
    if not rows:
        return Destination(None, None, 'no_conversation')
    connector = ws.first(ws.q(WhatsAppConnector))
    current = connector.account_wid if connector and connector.desired == 'running' else None
    accounts = {r[0] for r in rows}
    if current in accounts:
        pool = [r for r in rows if r[0] == current]
    elif len(accounts) == 1:
        pool = rows
    else:
        return Destination(None, None, 'ambiguous_account')
    account, chat, _ = max(pool, key=lambda r: (r[2] or datetime.min.replace(tzinfo=timezone.utc), r[1]))
    return Destination(account, chat)


def direct_messages(ws: Scoped, dest: Destination, *, recent_days=30, limit=80, older=20, now=None) -> list[Source]:
    """Included one-to-one messages for one account/chat: recent ones, and older context when
    the chat is quiet so inactive contacts do not disappear from view."""
    now = now or utcnow()
    base = ws.q(Source).where(Source.provider == 'whatsapp', Source.provider_account == dest.account,
                              Source.conversation_id == dest.chat, Source.included.is_(True), Source.deleted_at.is_(None),
                              Source.occurred_at.is_not(None))
    rows = ws.all(base.where(Source.occurred_at >= now - timedelta(days=recent_days))
                  .order_by(Source.occurred_at.desc(), Source.id.desc()).limit(limit))
    if len(rows) < older:
        seen = {r.id for r in rows}
        rows += [r for r in ws.all(base.order_by(Source.occurred_at.desc(), Source.id.desc()).limit(older)) if r.id not in seen]
    return sorted(rows[:limit], key=lambda m: (m.occurred_at, str(m.id)))


def message_lines(rows: list[Source], prefix='M') -> list[dict]:
    return [{'label': f'{prefix}{n + 1}', 'id': str(m.id), 'version': m.version, 'from_me': m.from_me,
             'at': m.occurred_at.isoformat(), 'text': m.body[:MESSAGE_CHARS],
             'partial': m.body_truncated or len(m.body) > MESSAGE_CHARS or bool((m.meta or {}).get('media'))}
            for n, m in enumerate(rows)]


# ---------------- events

@dataclass
class EventContext:
    events: list = field(default_factory=list)      # model view, EV labels
    messages: list = field(default_factory=list)    # E-labelled evidence the model may read
    coverage: dict = field(default_factory=dict)
    internal: list = field(default_factory=list)    # per event: rep, members and sources, for validation/linking


def _visible(ws: Scoped, ids) -> set[str]:
    ids = [uuid.UUID(x) for x in ids]
    if not ids:
        return set()
    return {str(x) for x in ws.s.scalars(ws.q(Source, Source.id).where(
        Source.id.in_(ids), Source.included.is_(True), Source.deleted_at.is_(None)))}


def group_sources(ws: Scoped, rep: Event) -> tuple[list[Event], dict[str, set[str]]]:
    """Members of a group and each member's visible cited sources."""
    from . import event_identity
    members = event_identity.members(ws, rep.id)
    mentions = ws.all(ws.q(EventMention).where(EventMention.event_id.in_([m.id for m in members])))
    visible = _visible(ws, {sid for m in mentions for sid in m.evidence})
    own = {str(m.id): set() for m in members}
    for m in mentions:
        own[str(m.event_id)] |= set(m.evidence) & visible
    return members, own


def _eligible(ws: Scoped, rep: Event) -> bool:
    from . import events
    return rep.origin != 'legacy' and rep.status != 'unsupported' and not events.group_dismissed(ws, rep)


def event_candidates(ws: Scoped, *, source_ids=(), person_id=None, primary=None, now=None) -> list[dict]:
    """Candidate event groups, ranked: source overlap with the supplied messages first
    (across every original member), then groups where the person is a participant. Overlap is
    evidence of association; participation alone only makes an event a visible possibility."""
    from . import event_identity
    now = now or utcnow()
    found: dict = {}

    def consider(rep, basis):
        if rep.id in found or not _eligible(ws, rep):
            return
        members, own = group_sources(ws, rep)
        union = set().union(*own.values()) if own else set()
        found[rep.id] = {'rep': rep, 'members': members, 'own': own, 'sources': union,
                         'overlap': union & {str(x) for x in source_ids}, 'basis': basis}

    if primary is not None:
        consider(event_identity.representative(ws, primary), 'primary')
    ids = sorted({str(x) for x in source_ids})
    if ids:
        arr = postgresql.array(ids)
        cited = {m.event_id for m in ws.all(ws.q(EventMention).where(EventMention.evidence.op('?|')(arr)))}
        for e in ws.all(ws.q(Event).where(Event.id.in_(cited))) if cited else []:
            rep = event_identity.representative(ws, e)
            if rep.starts_at and now - timedelta(days=14) <= rep.starts_at <= now + timedelta(days=60):
                consider(rep, 'shared_messages')
    if person_id is not None:
        window = ws.all(ws.q(Event).where(Event.canonical_event_id.is_(None), Event.origin != 'legacy',
                                          Event.starts_at >= now - timedelta(days=1), Event.starts_at <= now + timedelta(days=14))
                        .order_by(Event.starts_at, Event.id))
        for rep in window:
            if any(p.get('person') == str(person_id) for p in (rep.details or {}).get('participants', [])):
                consider(rep, 'participant')
    order = {'primary': 0, 'shared_messages': 1, 'participant': 2}
    ranked = sorted(found.values(), key=lambda c: (order[c['basis']], -len(c['overlap']), c['rep'].starts_at, str(c['rep'].id)))
    for c in ranked:
        if c['basis'] == 'participant' and c['overlap']:
            c['basis'] = 'shared_messages'
    return ranked[:EVENT_CANDIDATES]


def _local(value, zone):
    return value.astimezone(zone) if value else None


def projection_digest(rep: Event) -> str:
    """The event facts a message could depend on (not the free-text summary)."""
    d = rep.details or {}
    return digest([str(rep.id), rep.title, rep.activity, rep.starts_at, rep.ends_at, rep.status,
                   {k: v for k, v in d.items() if k != 'summary'}])


def event_context(ws: Scoped, candidates: list[dict], *, person_id=None, recipient_identifiers=(), chat=None,
                  message_labels: dict | None = None) -> EventContext:
    """Model view of candidate events with field provenance. Facts cite E labels (or M labels
    for messages already in the conversation input). Text is supplied only for your own
    messages, the recipient's own messages, this conversation, and email/Calendar sources
    that establish schedule, place, title or status; other guests' messages are omitted."""
    zone = tz(ws)
    out = EventContext(coverage={'events': len(candidates), 'event_messages': 0, 'omitted_private': 0, 'omitted_limit': 0})
    message_labels = message_labels or {}
    labels: dict[str, str] = {}
    size = 0
    recipient_ids = set(recipient_identifiers)

    def cite(sid: str, fact: str) -> str | None:
        nonlocal size
        if sid in message_labels:
            return message_labels[sid]
        if sid in labels:
            return labels[sid]
        src = ws.get(Source, sid)
        if src is None or not src.included or src.deleted_at:
            return None
        public_fact = src.provider in ('gmail', 'calendar') and fact in ('schedule', 'place', 'title', 'status')
        if not (src.from_me or public_fact or (chat and src.conversation_id == chat) or
                (src.author and src.author in recipient_ids)):
            out.coverage['omitted_private'] += 1
            return None
        from . import events
        text = ((src.title + '\n') if src.title else '') + events.message_text(src)
        text = text[:EVENT_MESSAGE_CHARS]
        if len(out.messages) >= EVENT_MESSAGES or size + len(text) > EVENT_CHARS:
            out.coverage['omitted_limit'] += 1
            return None
        size += len(text)
        label = f'E{len(out.messages) + 1}'
        labels[sid] = label
        out.messages.append({'label': label, 'id': sid, 'version': src.version,
                             'channel': {'gmail': 'email', 'calendar': 'calendar'}.get(src.provider, 'whatsapp'),
                             'from_me': src.from_me, 'at': src.occurred_at.isoformat() if src.occurred_at else None,
                             'text': text})
        return label

    for n, c in enumerate(candidates):
        rep, d = c['rep'], c['rep'].details or {}
        prov = d.get('provenance') or {}
        facts = {}
        for fact in ('schedule', 'place', 'status', 'title'):
            cited = [cite(sid, fact) for sid in (prov.get(fact) or [])[:3] if sid in c['sources']]
            if any(cited):
                facts[fact] = [x for x in cited if x]
        attendance = next((p for p in d.get('participants', []) if person_id and p.get('person') == str(person_id)), None)
        if attendance and attendance.get('evidence') in c['sources']:
            cited = cite(attendance['evidence'], 'attendance')
            if cited:
                facts['recipient_attendance'] = [cited]
        start, end = _local(rep.starts_at, zone), _local(rep.ends_at, zone)
        all_day = bool(d.get('all_day'))
        label = f'EV{n + 1}'
        out.events.append({
            'label': label, 'title': rep.title, 'activity': rep.activity or '',
            'date': start.date().isoformat() if start else None, 'weekday': start.strftime('%A') if start else None,
            'booked_start': None if all_day or not start else start.strftime('%H:%M'),
            'end': None if all_day or not end else end.strftime('%H:%M'),
            'time_known': not all_day, 'place': d.get('place') or None,
            'status': 'cancelled' if rep.status == 'cancelled' else 'planned',
            'your_role': d.get('role') or 'unknown',
            'recipient_attendance': ATTENDANCE.get(attendance.get('status'), 'unknown') if attendance else 'unknown',
            'confirmed_count': sum(1 for p in d.get('participants', []) if p.get('status') == 'accepted'),
            'needs_checking': bool(d.get('needs_clarification')),
            'earlier_times': [_local(datetime.fromisoformat(h['starts_at']), zone).strftime('%a %d %b %H:%M')
                              for h in d.get('history', [])[-3:] if h.get('starts_at')],
            'relation': {'primary': 'this message is about this event',
                         'shared_messages': 'shares messages with this conversation',
                         'participant': 'the recipient takes part; not confirmed as related'}[c['basis']],
            'facts': facts})
        out.internal.append({'label': label, 'rep': str(rep.id), 'basis': c['basis'],
                             'members': {k: sorted(v) for k, v in c['own'].items()}, 'sources': sorted(c['sources']),
                             'projection': projection_digest(rep)})
    out.coverage['event_messages'] = len(out.messages)
    return out


def related_candidates(ws: Scoped, row, evidence_ids) -> list[dict]:
    """Event groups whose sources overlap this suggestion's own evidence."""
    return [c for c in event_candidates(ws, source_ids=evidence_ids) if c['overlap']]


def choose_member(candidate: dict, evidence_ids) -> uuid.UUID:
    """The original identity to store: the member whose own evidence overlaps most (oldest on
    ties), so the link follows the right record after a split."""
    ids = {str(x) for x in evidence_ids}
    members = candidate['members']
    best = max(members, key=lambda m: (len(candidate['own'].get(str(m.id), set()) & ids),
                                       -(m.created_at.timestamp() if m.created_at else 0), str(m.id)))
    return best.id


def linked_event(ws: Scoped, row):
    """The current representative of a suggestion's primary original event, or None."""
    from . import event_identity
    if not row.event_id:
        return None
    original = ws.get(Event, row.event_id)
    if original is None:
        return None
    rep = event_identity.representative(ws, original)
    return rep if rep.status != 'unsupported' else None


# ---------------- shareable updates

def applicable_updates(ws: Scoped, person: Person, *, now=None, include_proposed=False) -> list[ShareableUpdate]:
    """Active, unexpired updates whose audience includes this person. Eligible-contact
    audiences need check-ins enabled for the person; selected audiences name them."""
    now = now or utcnow()
    rows = ws.all(ws.q(ShareableUpdate).where(ShareableUpdate.state == 'active')
                  .order_by(ShareableUpdate.created_at, ShareableUpdate.id))
    selected = {u for u in ws.s.scalars(ws.q(ShareableUpdatePerson, ShareableUpdatePerson.update_id)
                                        .where(ShareableUpdatePerson.person_id == person.id))}
    proposed = set() if include_proposed else set(ws.s.scalars(ws.q(UpdateProposal, UpdateProposal.update_id)
                                                               .where(UpdateProposal.person_id == person.id)))
    eligible = check_ins_enabled(person, preference(ws, person.id))
    out = []
    for u in rows:
        if u.expires_at and u.expires_at <= now or u.id in proposed:
            continue
        if (u.audience == 'selected' and u.id in selected) or (u.audience == 'eligible' and eligible):
            out.append(u)
    return out[:3]


def update_lines(updates) -> list[dict]:
    return [{'label': f'U{n + 1}', 'id': str(u.id), 'revision': u.revision, 'text': u.text,
             'expires': u.expires_at.date().isoformat() if u.expires_at else None} for n, u in enumerate(updates)]


def public(value):
    """Strip internal keys (leading underscore, IDs and versions) before a prompt."""
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if not k.startswith('_') and k not in ('id', 'version', 'revision')}
    if isinstance(value, list):
        return [public(v) for v in value]
    return value


def select_count(ws: Scoped, stmt) -> int:
    return ws.s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
