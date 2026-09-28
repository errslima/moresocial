"""Read-only WhatsApp conversation summaries and history, built from imported sources.

A conversation is one (provider_account, conversation_id) pair inside the workspace, so
two linked numbers never share a chat and two workspaces never see each other's rows.
Every statement, including contact lookups, filters on the authenticated workspace.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, func, or_, select, tuple_
from sqlalchemy.orm import aliased

from .models import Person, PersonIdentifier, Source
from .repo import Scoped, as_uuid

PAGE_SIZE = 50
HISTORY_SIZE = 100
SEARCH_MAX = 100
UNNAMED = 'Unnamed conversation'
OPAQUE_RE = re.compile(r'^[0-9a-z._:-]{1,80}@(c\.us|g\.us|lid|s\.whatsapp\.net)$', re.I)
PHONE_DOMAINS = ('c.us', 's.whatsapp.net')
KIND_LABELS = {'ptt': 'Voice message', 'audio': 'Voice message', 'image': 'Photo', 'video': 'Video',
               'document': 'Document', 'sticker': 'Sticker', 'location': 'Location', 'vcard': 'Contact card',
               'poll_creation': 'Poll', 'call_log': 'Call'}
# Icon names from icons.html; any other non-text kind uses 'attach'.
KIND_ICONS = {'ptt': 'mic', 'audio': 'mic', 'image': 'photo', 'video': 'video', 'document': 'file',
              'sticker': 'sticker', 'location': 'pin', 'vcard': 'person', 'poll_creation': 'poll', 'call_log': 'phone'}
SENDER_COLORS = 8


def eligible(ws: Scoped):
    return (Source.workspace_id == ws.wid, Source.provider == 'whatsapp', Source.deleted_at.is_(None),
            Source.included.is_(True), Source.conversation_id.is_not(None))


def sort_time():
    return func.coalesce(Source.occurred_at, Source.created_at)


def is_group_chat(chat: str, meta: dict | None) -> bool:
    return chat.endswith('@g.us') or bool((meta or {}).get('is_group'))


def readable(name: str | None, *ids: str | None) -> str | None:
    """A display name, unless it is really an opaque WhatsApp identifier."""
    n = (name or '').strip()
    if not n or OPAQUE_RE.match(n):
        return None
    for ident in ids:
        if ident and n.lower() in (ident.lower(), ident.split('@')[0].lower()):
            return None
    return n


def phone_label(ident: str | None) -> str | None:
    """+number only for phone-based JIDs; an @lid is never a phone number."""
    number, _, domain = (ident or '').partition('@')
    return '+' + number if domain in PHONE_DOMAINS and number.isascii() and number.isdigit() else None


def conversation_name(chat: str, group: bool, contact_name: str | None, title: str | None) -> str:
    if group:
        return readable(title, chat) or UNNAMED
    return readable(contact_name, chat) or readable(title, chat) or phone_label(chat) or UNNAMED


def sender_name(author: str | None, person_name: str | None, label: str | None) -> str:
    return readable(person_name, author) or readable(label, author) or phone_label(author) or 'Group member'


def initials(name: str) -> str:
    if name == UNNAMED:
        return ''
    letters = [w[0] for w in name.split() if w[:1].isalpha()]
    return ''.join(letters[:2]).upper()


def kind_label(meta: dict | None, body: str) -> str | None:
    kind = (meta or {}).get('kind') or 'chat'
    if kind == 'chat':
        return None if body.strip() else 'Non-text message'
    return KIND_LABELS.get(kind, 'Non-text message')


def kind_icon(meta: dict | None) -> str:
    return KIND_ICONS.get((meta or {}).get('kind'), 'attach')


def escape_like(text: str) -> str:
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def clean_query(q: str) -> str:
    return ' '.join(q.split())[:SEARCH_MAX]


def local(value: datetime | None, tz: ZoneInfo) -> datetime | None:
    return value.astimezone(tz) if value else None


def list_time(value: datetime | None, now: datetime) -> str:
    if value is None:
        return ''
    if value.date() == now.date():
        return value.strftime('%H:%M')
    if value.date() == (now - timedelta(days=1)).date():
        return 'Yesterday'
    if now.date() - value.date() < timedelta(days=7):
        return value.strftime('%a')
    return value.strftime('%-d %b' if value.year == now.year else '%-d %b %Y')


def day_label(value: datetime, now: datetime) -> str:
    if value.date() == now.date():
        return 'Today'
    if value.date() == (now - timedelta(days=1)).date():
        return 'Yesterday'
    return value.strftime('%A %-d %B' if value.year == now.year else '%A %-d %B %Y')


@dataclass
class Summary:
    account: str
    chat: str
    name: str
    group: bool
    initials: str
    avatar: int
    at: datetime | None
    time: str
    preview: str
    prefix: str


def _summaries(ws: Scoped):
    """One row per conversation: its latest eligible message plus resolved names."""
    part = (Source.provider_account, Source.conversation_id)
    ranked = (select(Source.id, Source.provider_account.label('account'), Source.conversation_id.label('chat'),
                     Source.conversation_title.label('title'), Source.from_me, Source.author, Source.author_label,
                     Source.body, Source.meta, sort_time().label('at'),
                     func.row_number().over(partition_by=part, order_by=(sort_time().desc(), Source.id.desc())).label('rn'))
              .where(*eligible(ws)).subquery())
    group = or_(ranked.c.chat.like('%@g.us'), func.coalesce(ranked.c.meta['is_group'].as_boolean(), False))
    contact, contact_person = aliased(PersonIdentifier), aliased(Person)
    author, author_person = aliased(PersonIdentifier), aliased(Person)
    stmt = (select(ranked, contact_person.display_name.label('contact_name'), author_person.display_name.label('author_name'))
            .outerjoin(contact, and_(contact.workspace_id == ws.wid, contact.kind == 'whatsapp',
                                     contact.value == ranked.c.chat, ~group))
            .outerjoin(contact_person, and_(contact_person.workspace_id == ws.wid, contact_person.id == contact.person_id))
            .outerjoin(author, and_(author.workspace_id == ws.wid, author.kind == 'whatsapp', author.value == ranked.c.author))
            .outerjoin(author_person, and_(author_person.workspace_id == ws.wid, author_person.id == author.person_id))
            .where(ranked.c.rn == 1))
    return stmt, ranked, contact_person


def _summary(row, tz: ZoneInfo, now: datetime) -> Summary:
    group = is_group_chat(row.chat, row.meta)
    name = conversation_name(row.chat, group, row.contact_name, row.title)
    label = kind_label(row.meta, row.body)
    text = ' '.join(row.body.split())[:160]
    preview = f'{label}: {text}' if label and text else (label or text)
    prefix = 'You: ' if row.from_me else (sender_name(row.author, row.author_name, row.author_label) + ': ' if group else '')
    at = local(row.at, tz)
    return Summary(account=row.account, chat=row.chat, name=name, group=group, initials=initials(name),
                   avatar=sum(map(ord, row.account + row.chat)) % 6, at=at, time=list_time(at, now),
                   preview=preview, prefix=prefix)


def conversations(ws: Scoped, tz: ZoneInfo, q: str = '', page: int = 1) -> tuple[list[Summary], bool]:
    """Newest-first conversations for one page; search runs in SQL before pagination."""
    stmt, ranked, contact_person = _summaries(ws)
    q = clean_query(q)
    if q:
        like = '%' + escape_like(q) + '%'
        stmt = stmt.where(or_(contact_person.display_name.ilike(like, escape='\\'),
                              and_(ranked.c.title.ilike(like, escape='\\'), ranked.c.title != ranked.c.chat)))
    rows = ws.s.execute(stmt.order_by(ranked.c.at.desc(), ranked.c.id.desc())
                        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)).all()
    now = datetime.now(tz)
    return [_summary(r, tz, now) for r in rows[:PAGE_SIZE]], len(rows) > PAGE_SIZE


def conversation(ws: Scoped, tz: ZoneInfo, account: str, chat: str) -> Summary | None:
    stmt, ranked, _ = _summaries(ws)
    row = ws.s.execute(stmt.where(ranked.c.account == account, ranked.c.chat == chat)).first()
    return _summary(row, tz, datetime.now(tz)) if row else None


def has_history(ws: Scoped) -> bool:
    return bool(ws.s.scalar(select(exists().where(*eligible(ws)))))


@dataclass
class Message:
    id: uuid.UUID
    from_me: bool
    sender: str | None
    label: str | None
    text: str
    at: datetime | None
    time: str
    day: str | None
    icon: str | None = None
    # First message of a run from one speaker on one day: it shows the bubble tail and group sender name.
    run_start: bool = True
    sender_color: int = 0


def history(ws: Scoped, tz: ZoneInfo, convo: Summary, before: str = '') -> tuple[list[Message], bool] | None:
    """Up to HISTORY_SIZE messages in chronological order, ending just before `before`
    (a source ID inside this conversation) or at the latest message. None: invalid cursor."""
    where = (*eligible(ws), Source.provider_account == convo.account, Source.conversation_id == convo.chat)
    author, author_person = aliased(PersonIdentifier), aliased(Person)
    stmt = (select(Source, author_person.display_name)
            .outerjoin(author, and_(author.workspace_id == ws.wid, author.kind == 'whatsapp', author.value == Source.author))
            .outerjoin(author_person, and_(author_person.workspace_id == ws.wid, author_person.id == author.person_id))
            .where(*where))
    if before:
        cursor_id = as_uuid(before)
        cursor = ws.s.execute(select(sort_time(), Source.id).where(*where, Source.id == cursor_id)).first() if cursor_id else None
        if cursor is None:
            return None
        stmt = stmt.where(tuple_(sort_time(), Source.id) < tuple_(cursor[0], cursor[1]))
    rows = ws.s.execute(stmt.order_by(sort_time().desc(), Source.id.desc()).limit(HISTORY_SIZE + 1)).all()
    older = len(rows) > HISTORY_SIZE
    now, previous, previous_speaker, out = datetime.now(tz), None, None, []
    for src, person_name in reversed(rows[:HISTORY_SIZE]):
        at = local(src.occurred_at or src.created_at, tz)
        day = at.date() if at else None
        speaker = (src.from_me, None if src.from_me or not convo.group else src.author)
        label = kind_label(src.meta, src.body)
        out.append(Message(id=src.id, from_me=src.from_me,
                           sender=None if src.from_me or not convo.group else sender_name(src.author, person_name, src.author_label),
                           label=label, text=src.body, at=at,
                           time=at.strftime('%H:%M') if at else '',
                           day=day_label(at, now) if at and day != previous else None,
                           icon=kind_icon(src.meta) if label else None,
                           run_start=not out or day != previous or speaker != previous_speaker,
                           sender_color=sum(map(ord, src.author or '')) % SENDER_COLORS))
        previous, previous_speaker = day, speaker
    return out, older
