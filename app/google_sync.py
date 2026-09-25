"""Bounded, resumable, idempotent Gmail and primary-Calendar synchronization.

Gmail: last N days (default 90), at most M messages (default 500) excluding Spam/Trash;
then history-cursor increments with a bounded resync if the cursor becomes invalid.
Calendar: bounded window refresh (past/next 90 days, max 1000 instances) without a
sync token, then syncToken increments without timeMin/timeMax. A new cursor is persisted
only after every page it covers has been processed. Absence is never read as deletion.
"""
from __future__ import annotations

import base64
import codecs
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
import re
from typing import Callable

from sqlalchemy import select

from . import accounts, config, db, security
from .google import GoogleError, provider
from .ingest import IngestResult, Participant, SourceRecord, schedule_rebuilds, self_identifiers, upsert
from .models import Connection, Source, SyncStream
from .repo import Scoped

BODY_LIMIT = 32 * 1024
WINDOW_REFRESH_HOURS = 24


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------- Gmail message normalization (adapted from EnzoSocial, GPL-3.0) ----------

class _Text(HTMLParser):
    """Provider HTML becomes inert text: tags dropped, scripts/styles skipped, nothing fetched."""
    BLOCK = {'p', 'div', 'br', 'tr', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'blockquote', 'hr'}
    SKIP = {'script', 'style', 'head', 'title', 'noscript', 'template'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BLOCK:
            self.out.append('\n')

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag in self.BLOCK:
            self.out.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(value: str) -> str:
    parser = _Text()
    try:
        parser.feed(value)
        parser.close()
        text = ''.join(parser.out)
    except Exception:
        text = re.sub(r'<[^>]*>', ' ', value)
    return re.sub(r'\n{3,}', '\n\n', re.sub(r'[ \t\r\f\v]+', ' ', text)).strip()


def _charset(part: dict) -> str:
    for h in part.get('headers') or []:
        if h.get('name', '').lower() == 'content-type':
            m = re.search(r'charset="?([A-Za-z0-9._-]+)', h.get('value', ''))
            if m:
                try:
                    return codecs.lookup(m[1]).name
                except LookupError:
                    return 'utf-8'
    return 'utf-8'


def _decode(data: str, charset: str) -> str:
    return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode(charset, 'replace')


def _walk(part: dict, found: dict, depth: int = 0) -> None:
    if depth > 12 or found['attachments'] > 50:
        return
    mime = (part.get('mimeType') or '').lower()
    body = part.get('body') or {}
    if part.get('filename') or (body.get('attachmentId') and not mime.startswith('text/')):
        found['attachments'] += 1  # never downloaded
        return
    if mime.startswith('multipart/'):
        for child in (part.get('parts') or [])[:50]:
            _walk(child, found, depth + 1)
    elif mime == 'text/plain' and body.get('data'):
        found['plain'].append(_decode(body['data'], _charset(part)))
    elif mime == 'text/html' and body.get('data'):
        found['html'].append(_decode(body['data'], _charset(part)))


QUOTE_START = re.compile(r'^(On .{3,200}wrote:|Op .{3,200}(schreef|geschreven).{0,40}:|-{2,} ?Original Message ?-{2,}|'
                         r'-{2,} ?Oorspronkelijk bericht ?-{2,})\s*$', re.I)


def split_quoted(text: str) -> tuple[str, bool]:
    """New text only, so an old quoted confirmation cannot read as the sender's statement."""
    lines = text.splitlines()
    for n, line in enumerate(lines):
        if QUOTE_START.match(line.strip()):
            return '\n'.join(l for l in lines[:n] if not l.startswith('>')).strip(), True
    new = [l for l in lines if not l.lstrip().startswith('>')]
    return '\n'.join(new).strip(), len(new) != len(lines)


def clip(value: str, limit: int = BODY_LIMIT) -> tuple[str, bool]:
    data = value.encode('utf-8')
    if len(data) <= limit:
        return value, False
    return data[:limit].decode('utf-8', 'ignore'), True


def gmail_record(message: dict, account: str, own_email: str | None) -> SourceRecord:
    payload = message.get('payload') or {}
    headers = {h.get('name', '').lower(): h.get('value', '') for h in payload.get('headers') or []}
    found = {'plain': [], 'html': [], 'attachments': 0}
    _walk(payload, found)
    body = '\n'.join(found['plain']).strip() or html_to_text('\n'.join(found['html']))
    body, quoted = split_quoted(body)
    body, truncated = clip(body)
    name, address = parseaddr(headers.get('from', ''))
    address = address.lower()
    labels = [l for l in message.get('labelIds') or [] if l in ('INBOX', 'SENT', 'SPAM', 'TRASH', 'IMPORTANT')]
    try:
        ts = datetime.fromtimestamp(int(message.get('internalDate') or 0) / 1000, timezone.utc)
    except (ValueError, OverflowError):
        ts = None
    participants = []
    for role in ('to', 'cc'):
        for pname, paddr in getaddresses([headers.get(role, '')])[:50]:
            if paddr:
                participants.append(Participant('email', paddr, role, pname or None))
    from_me = bool(own_email and address == own_email.lower())
    return SourceRecord(
        provider='gmail', provider_account=account, provider_object_id=str(message.get('id', ''))[:400], kind='email',
        conversation_id=message.get('threadId'), conversation_title=headers.get('subject', '(No subject)')[:300],
        title=headers.get('subject', '(No subject)')[:300],
        author=Participant('email', address, 'from', name or None) if address else None, from_me=from_me,
        occurred_at=ts, body=body, body_truncated=truncated,
        meta={'labels': labels, 'attachments': found['attachments'], 'quoted_history_omitted': quoted},
        participants=participants, deleted=bool({'SPAM', 'TRASH'} & set(labels)))


def gmail_deleted(message_id: str, account: str) -> SourceRecord:
    return SourceRecord(provider='gmail', provider_account=account, provider_object_id=message_id, kind='email',
                        deleted=True, meta={'status': 'deleted'})


# ---------- Calendar normalization ----------

def _when(value: dict | None) -> tuple[datetime | None, bool]:
    value = value or {}
    try:
        if value.get('dateTime'):
            return datetime.fromisoformat(value['dateTime'].replace('Z', '+00:00')), False
        if value.get('date'):
            return datetime.fromisoformat(value['date']).replace(tzinfo=timezone.utc), True
    except ValueError:
        pass
    return None, False


def calendar_record(event: dict, account: str) -> SourceRecord:
    start, all_day = _when(event.get('start'))
    end, _ = _when(event.get('end'))
    organizer = (event.get('organizer') or {}).get('email')
    participants = [Participant('email', a['email'], 'attendee', a.get('displayName'))
                    for a in (event.get('attendees') or [])[:100] if isinstance(a, dict) and a.get('email') and not a.get('self')]
    own = next((a.get('responseStatus') for a in event.get('attendees') or [] if isinstance(a, dict) and a.get('self')), None)
    description = html_to_text(event.get('description') or '') if event.get('description') else ''
    location = (event.get('location') or '')[:500]
    body = '\n'.join(x for x in [description, ('Location: ' + location) if location else ''] if x)
    body, truncated = clip(body)
    return SourceRecord(
        provider='calendar', provider_account=account, provider_object_id=str(event.get('id', ''))[:400], kind='event',
        version=((event.get('etag') or '') + '|' + (event.get('updated') or ''))[:200] or None,
        conversation_id=event.get('recurringEventId') or event.get('id'), conversation_title=(event.get('summary') or 'Busy')[:300],
        title=(event.get('summary') or 'Busy')[:300],
        author=Participant('email', organizer, 'organizer') if organizer and not (event.get('organizer') or {}).get('self') else None,
        occurred_at=start, ends_at=end, body=body, body_truncated=truncated,
        meta={'status': event.get('status', 'confirmed'), 'all_day': all_day, 'self_response': own,
              'location': location or None, 'note': 'Scheduled calendar event; not proof of attendance.'},
        participants=participants, deleted=event.get('status') == 'cancelled')


# ---------- sync driver ----------

class StreamStopped(Exception):
    pass


def _load(s, workspace_id, stream_id) -> tuple[Scoped, SyncStream, Connection]:
    ws = Scoped(s, workspace_id)
    stream = ws.get(SyncStream, stream_id)
    conn = ws.get(Connection, stream.connection_id) if stream else None
    if stream is None or conn is None or conn.state != 'active':
        raise StreamStopped()
    scope = accounts.STREAM_SCOPES[stream.stream]
    if scope not in conn.scopes:
        stream.status, stream.next_run_at = 'disabled', None
        raise StreamStopped()
    return ws, stream, conn


def _token(workspace_id, stream_id) -> str:
    with db.session() as s:
        ws, stream, conn = _load(s, workspace_id, stream_id)
        return accounts.access_token(s, conn)


def _apply(workspace_id, stream_id, records: list[SourceRecord], token: str, update: Callable[[SyncStream], None]) -> None:
    """Upsert one page and advance progress in a single transaction; stop if revoked meanwhile."""
    with db.session() as s:
        ws, stream, conn = _load(s, workspace_id, stream_id)
        self_ids = self_identifiers(ws)
        result = IngestResult()
        for rec in records:
            upsert(ws, rec, self_ids, result)
        schedule_rebuilds(ws, result, token)
        update(stream)


def run(workspace_id, stream_id, heartbeat: Callable[[], None] = lambda: None) -> str:
    """Run one sync pass. Returns an outcome code; raises GoogleError on provider failures."""
    with db.session() as s:
        ws, stream, conn = _load(s, workspace_id, stream_id)
        kind = stream.stream
        stream.status = 'syncing'
        stream.last_attempt_at = utcnow()
    try:
        outcome = (_gmail if kind == 'gmail' else _calendar)(workspace_id, stream_id, heartbeat)
    except StreamStopped:
        return 'stopped'
    except GoogleError as exc:
        _record_failure(workspace_id, stream_id, exc)
        raise
    with db.session() as s:
        ws = Scoped(s, workspace_id)
        stream = ws.get(SyncStream, stream_id)
        if stream:
            stream.status, stream.failures, stream.last_error = 'ok', 0, None
            stream.last_success_at = utcnow()
            stream.next_run_at = utcnow() + timedelta(seconds=config.get().poll_seconds)
    return outcome


def _record_failure(workspace_id, stream_id, exc: GoogleError) -> None:
    with db.session() as s:
        ws = Scoped(s, workspace_id)
        stream = ws.get(SyncStream, stream_id)
        conn = ws.get(Connection, stream.connection_id) if stream else None
        if stream is None:
            return
        stream.failures += 1
        if exc.kind == 'auth' and conn is not None:
            accounts.mark_reconnect(s, conn, 'grant_revoked_or_expired')
            stream.status, stream.last_error = 'paused', 'Google access needs to be reconnected.'
            return
        if exc.kind == 'forbidden' and exc.reason in ('insufficientPermissions', 'PERMISSION_DENIED'):
            stream.status, stream.next_run_at = 'disabled', None
            stream.last_error = 'Permission for this data was not granted.'
            return
        stream.status = 'error'
        stream.last_error = {'throttled': 'Google asked us to slow down; retrying later.',
                             'forbidden': 'Google refused access to this data; retrying later.'}.get(
            exc.kind, 'Google was unavailable; retrying later.')
        backoff = min(6 * 3600, 60 * 2 ** min(stream.failures, 8))
        stream.next_run_at = utcnow() + timedelta(seconds=backoff)


def _gmail(workspace_id, stream_id, heartbeat) -> str:
    settings = config.get()
    g = provider()
    token = _token(workspace_id, stream_id)
    with db.session() as s:
        ws, stream, conn = _load(s, workspace_id, stream_id)
        account, own, cursor, progress = conn.provider_account, conn.email, stream.cursor, dict(stream.progress or {})
    if cursor and progress.get('phase') != 'initial':
        try:
            return _gmail_incremental(workspace_id, stream_id, g, token, account, own, cursor, heartbeat, progress)
        except GoogleError as exc:
            if exc.kind != 'not_found':
                raise
            # Invalid/expired history cursor: bounded resync. Upserts are idempotent; nothing is
            # treated as deleted merely because it is absent from the resync.
            progress = {}
    if progress.get('phase') != 'initial':
        profile = g.gmail_profile(token)
        progress = {'phase': 'initial', 'start_history': str(profile.get('historyId', '')), 'page': None, 'fetched': 0}
        _apply(workspace_id, stream_id, [], 'gmail-start', lambda st: setattr(st, 'progress', progress))
    query = f'newer_than:{settings.gmail_days}d -in:spam -in:trash'
    truncated = False
    while True:
        remaining = settings.gmail_max_messages - progress['fetched']
        if remaining <= 0:
            truncated = bool(progress.get('page')) or progress.get('more', False)
            break
        listing = g.gmail_list(token, query, progress.get('page'), min(100, remaining))
        records = []
        for item in (listing.get('messages') or [])[:remaining]:
            try:
                records.append(gmail_record(g.gmail_message(token, item['id']), account, own))
            except GoogleError as exc:
                if exc.kind != 'not_found':
                    raise
        nxt = listing.get('nextPageToken')
        new = {**progress, 'page': nxt, 'fetched': progress['fetched'] + len(records), 'more': bool(nxt)}

        def advance(st, new=new, n=len(records)):
            st.progress = new
            st.items_seen = new['fetched']
        _apply(workspace_id, stream_id, records, 'gmail:' + str(progress.get('page')), advance)
        progress = new
        heartbeat()
        if not nxt:
            break
    final = progress

    def done(st):
        st.cursor = final['start_history'] or None
        st.progress = {}
        st.truncated = truncated
    _apply(workspace_id, stream_id, [], 'gmail-done', done)
    return 'initial_truncated' if truncated else 'initial_complete'


def _gmail_incremental(workspace_id, stream_id, g, token, account, own, cursor, heartbeat, progress=None) -> str:
    # Commit progress per history page, not per whole poll. Retries can replay the
    # current page safely, and the final cursor never covers unprocessed messages.
    progress = progress or {}
    page = progress.get('page') if progress.get('phase') == 'history' and progress.get('start_history') == cursor else None
    while True:
        h = g.gmail_history(token, cursor, page)
        changes = {}
        for rec in h.get('history') or []:
            for m in rec.get('messagesAdded') or []:
                changes[m['message']['id']] = 'fetch'
            for m in rec.get('messagesDeleted') or []:
                changes[m['message']['id']] = 'delete'
            for m in rec.get('labelsAdded') or []:
                if {'SPAM', 'TRASH'} & set(m.get('labelIds') or []):
                    changes[m['message']['id']] = 'delete'
            for m in rec.get('labelsRemoved') or []:
                if {'SPAM', 'TRASH'} & set(m.get('labelIds') or []):
                    changes[m['message']['id']] = 'fetch'
        items = list(changes.items())
        nxt = h.get('nextPageToken')
        latest = str(h.get('historyId') or cursor)
        for n in range(0, max(1, len(items)), 100):
            records = []
            for ident, action in items[n:n + 100]:
                if action == 'delete':
                    records.append(gmail_deleted(ident, account))
                else:
                    try:
                        records.append(gmail_record(g.gmail_message(token, ident), account, own))
                    except GoogleError as exc:
                        if exc.kind != 'not_found':
                            raise
                        records.append(gmail_deleted(ident, account))
                heartbeat()
            last_batch = n + 100 >= len(items)

            def advance(st, last_batch=last_batch):
                if last_batch:
                    if nxt:
                        st.progress = {'phase': 'history', 'start_history': cursor, 'page': nxt}
                    else:
                        st.cursor = latest
                        st.progress = {}
            _apply(workspace_id, stream_id, records, f'gmail-h:{cursor}:{page}:{n}', advance)
            heartbeat()
        if not nxt:
            return 'incremental'
        page = nxt


def _calendar(workspace_id, stream_id, heartbeat) -> str:
    g = provider()
    token = _token(workspace_id, stream_id)
    with db.session() as s:
        ws, stream, conn = _load(s, workspace_id, stream_id)
        account, cursor, progress = conn.provider_account, stream.cursor, dict(stream.progress or {})
    refreshed = progress.get('window_refreshed_at')
    window_due = not refreshed or datetime.fromisoformat(refreshed) < utcnow() - timedelta(hours=WINDOW_REFRESH_HOURS)
    if cursor and not window_due and progress.get('phase') != 'window':
        try:
            return _calendar_incremental(workspace_id, stream_id, g, token, account, cursor, heartbeat)
        except GoogleError as exc:
            if exc.kind != 'gone':
                raise
            progress = {k: v for k, v in progress.items() if k == 'window_refreshed_at'}
            _apply(workspace_id, stream_id, [], 'cal-gone', lambda st: setattr(st, 'cursor', None))
    return _calendar_window(workspace_id, stream_id, g, token, account, progress, heartbeat)


def _calendar_incremental(workspace_id, stream_id, g, token, account, cursor, heartbeat) -> str:
    page, records, next_sync = None, [], None
    while True:
        data = g.calendar_incremental(token, cursor, page)
        records += [calendar_record(e, account) for e in data.get('items') or []]
        page = data.get('nextPageToken')
        next_sync = data.get('nextSyncToken') or next_sync
        heartbeat()
        if not page:
            break

    def advance(st):
        if next_sync:
            st.cursor = next_sync
    _apply(workspace_id, stream_id, records, f'cal-inc:{cursor[:40]}', advance)
    return 'incremental'


def _calendar_window(workspace_id, stream_id, g, token, account, progress, heartbeat) -> str:
    settings = config.get()
    if progress.get('phase') != 'window':
        now = utcnow().replace(microsecond=0)
        progress = {'phase': 'window', 'page': None, 'fetched': 0,
                    'min': (now - timedelta(days=settings.calendar_past_days)).isoformat(),
                    'max': (now + timedelta(days=settings.calendar_future_days)).isoformat()}
    truncated, next_sync = False, None
    while True:
        remaining = settings.calendar_max_events - progress['fetched']
        if remaining <= 0:
            truncated = True
            break
        data = g.calendar_window(token, progress['min'], progress['max'], progress.get('page'))
        items = (data.get('items') or [])[:remaining]
        records = [calendar_record(e, account) for e in items]
        nxt = data.get('nextPageToken')
        next_sync = data.get('nextSyncToken')
        if len(data.get('items') or []) > remaining:
            truncated, nxt = True, None
        new = {**progress, 'page': nxt, 'fetched': progress['fetched'] + len(records)}

        def advance(st, new=new):
            st.progress = new
            st.items_seen = new['fetched']
        _apply(workspace_id, stream_id, records, f"cal-win:{progress['min']}:{progress.get('page')}", advance)
        progress = new
        heartbeat()
        if not nxt or truncated:
            break
    done_at = utcnow().isoformat()

    def done(st):
        # Without reaching the final page there is no valid sync token: stay truncated and use
        # bounded refreshes until one completes. Never invent a cursor.
        st.cursor = next_sync if (next_sync and not truncated) else None
        st.truncated = truncated
        st.progress = {'window_refreshed_at': done_at}
    _apply(workspace_id, stream_id, [], 'cal-win-done', done)
    return 'window_truncated' if truncated else 'window_complete'
