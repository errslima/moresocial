"""WhatsApp: desired connector state, status/QR relay and idempotent ingestion.

Each workspace has its own connector container (QR-linked WhatsApp Web, LocalAuth) on its
own private network. The web app records the desired state; the host provisioner
reconciles containers. The ingest workspace always comes from the connector's scoped key,
never from the payload. There is no send path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import re
import uuid

import httpx
from sqlalchemy import func, select

from . import config, db, jobs, security
from .ingest import IngestResult, Participant, SourceRecord, register_contacts, schedule_rebuilds, self_identifiers, upsert
from .models import WhatsAppConnector, Account
from .repo import Scoped

CHAT_RE = re.compile(r'^[0-9a-z._-]{1,80}@(c\.us|g\.us|lid|s\.whatsapp\.net)$')
MSG_ID_RE = re.compile(r'^[\w@.:-]{1,300}$')
MAX_AGE_DAYS = 90
MAX_MESSAGES = 300
BODY_LIMIT = 32 * 1024
PUBLIC_STATES = {'pending', 'capacity', 'starting', 'pairing', 'loading', 'ready', 'disconnected', 'error', 'stopped',
                 'auth_failure', 'removed'}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def connector_url(workspace_id: uuid.UUID) -> str:
    template = os.environ.get('CONNECTOR_URL_TEMPLATE', 'http://ms-wa-{id}:8790')
    return template.format(id=workspace_id.hex)


def active_count(s) -> int:
    return s.scalar(select(func.count()).select_from(WhatsAppConnector).where(WhatsAppConnector.desired == 'running')) or 0


def request_connect(ws: Scoped) -> WhatsAppConnector:
    """Record that this workspace wants a running connector, within the configured cap."""
    # Serialize cap checks across concurrent requests.
    ws.s.execute(select(func.pg_advisory_xact_lock(0x6d73776163)))
    row = ws.first(ws.q(WhatsAppConnector).with_for_update())
    if row is None:
        key = security.token(32)
        row = ws.add(WhatsAppConnector(id=uuid.uuid4(), desired='stopped', observed='pending',
                                       key_hash=security.digest(key), key_enc=security.encrypt(key), status={}))
        ws.s.flush()
    if row.desired != 'running':
        if active_count(ws.s) >= config.get().connector_cap:
            row.observed, row.detail = 'capacity', 'All WhatsApp connection slots are in use. Try again later.'
            return row
        row.desired, row.observed, row.detail = 'running', 'pending', None
        row.requested_at = utcnow()
    return row


def stop(ws: Scoped, remove_session: bool) -> WhatsAppConnector | None:
    """Disconnect: log out of WhatsApp Web (best effort), stop reconnect loops and, if asked,
    have the provisioner delete the session directory."""
    row = ws.first(ws.q(WhatsAppConnector).with_for_update())
    if row is None:
        return None
    if row.desired == 'running' and not config.get().synthetic_providers:
        try:
            call(row, 'POST', '/disconnect', timeout=20)
        except ConnectorUnavailable:
            pass
    if config.get().synthetic_providers:
        FAKE.pop(ws.wid, None)
    row.desired = 'deleted' if remove_session else 'stopped'
    row.observed = 'stopped'
    row.detail = None
    row.status = {}
    if remove_session:
        row.account_wid = None
    return row


class ConnectorUnavailable(Exception):
    pass


def call(row: WhatsAppConnector, method: str, path: str, timeout: float = 8) -> dict:
    key = security.decrypt(row.key_enc)
    if not key:
        raise ConnectorUnavailable()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as h:
            r = h.request(method, connector_url(row.workspace_id) + path, headers={'Authorization': 'Bearer ' + key})
    except httpx.HTTPError as exc:
        security.emit('connector_unreachable', exc)
        raise ConnectorUnavailable() from None
    if not r.is_success:
        raise ConnectorUnavailable()
    try:
        return r.json()
    except ValueError:
        raise ConnectorUnavailable() from None


def status(ws: Scoped) -> dict:
    """Owner-only live status. The QR is returned only while pairing and never stored."""
    row = ws.first(ws.q(WhatsAppConnector))
    if row is None:
        return {'state': 'not_connected'}
    view = {'state': row.observed if row.observed in PUBLIC_STATES else 'error', 'detail': row.detail,
            'history': row.status.get('history') if row.status else None}
    if row.desired != 'running':
        view['state'] = 'not_connected' if row.observed in ('stopped', 'removed', 'pending') else view['state']
        return view
    try:
        live = FAKE_CONNECTOR.status(ws) if config.get().synthetic_providers else call(row, 'GET', '/status')
    except ConnectorUnavailable:
        return {**view, 'state': 'starting' if row.observed in ('pending', 'starting') else view['state']}
    state = live.get('state') if live.get('state') in PUBLIC_STATES else 'error'
    account = live.get('account') if isinstance(live.get('account'), str) and CHAT_RE.match(live['account']) else None
    history = {k: live.get(k) for k in ('chats_total', 'history_chats', 'history_done', 'chats_synced', 'last_sync')
               if isinstance(live.get(k), (int, str))}
    row.observed = state
    if account:
        row.account_wid = account
    row.status = {'history': history, 'pending': live.get('pending') if isinstance(live.get('pending'), int) else None}
    row.detail = str(live.get('error'))[:300] if live.get('error') else None
    qr = live.get('qr') if state == 'pairing' and isinstance(live.get('qr'), str) and live['qr'].startswith('data:image/') else None
    return {'state': state, 'qr': qr, 'detail': row.detail, 'history': history}


def connector_for_key(s, raw_key: str | None) -> WhatsAppConnector | None:
    if not raw_key or len(raw_key) > 200:
        return None
    return s.scalars(select(WhatsAppConnector).where(WhatsAppConnector.key_hash == security.digest(raw_key),
                                                     WhatsAppConnector.desired == 'running')).first()


def _ts(value) -> datetime | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v <= 0 or v > 10 ** 11:
        return None
    return datetime.fromtimestamp(v, timezone.utc)


def records_from_packet(packet: dict, account: str) -> tuple[list[SourceRecord], list[dict]]:
    chat = packet.get('chat') if isinstance(packet, dict) else None
    if not isinstance(chat, dict) or not isinstance(chat.get('id'), str) or not CHAT_RE.match(chat['id']):
        raise ValueError('invalid chat')
    messages = packet.get('messages')
    if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
        raise ValueError('invalid messages')
    is_group = bool(chat.get('is_group')) or chat['id'].endswith('@g.us')
    title = str(chat.get('name') or chat['id'])[:300]
    cutoff = utcnow() - timedelta(days=MAX_AGE_DAYS)
    out = []
    for m in messages:
        if not isinstance(m, dict) or not isinstance(m.get('id'), str) or not MSG_ID_RE.match(m['id']):
            continue
        ts = _ts(m.get('ts'))
        if ts is None or ts < cutoff:
            continue  # outside the 90-day history bound
        kind = m.get('kind') if isinstance(m.get('kind'), str) else 'unsupported'
        body = m.get('body') if isinstance(m.get('body'), str) and kind in ('chat', 'image', 'video', 'document') else ''
        body = body.encode('utf-8')[:BODY_LIMIT].decode('utf-8', 'ignore')
        sender = m.get('sender') if isinstance(m.get('sender'), str) else None
        from_me = m.get('from_me') is True
        author = Participant('whatsapp', sender, 'author') if sender and not from_me else None
        participants = [] if is_group else [Participant('whatsapp', chat['id'], 'member', title)]
        if author and not is_group:
            author.label = title
        out.append(SourceRecord(
            provider='whatsapp', provider_account=account, provider_object_id=m['id'], kind='message',
            conversation_id=chat['id'], conversation_title=title, author=author, from_me=from_me, occurred_at=ts,
            body=body if kind != 'revoked' else '', meta={'kind': kind, 'is_group': is_group,
                                                          'media': kind in ('image', 'video', 'document', 'audio', 'ptt')},
            participants=participants, deleted=(kind == 'revoked')))
    contacts = packet.get('contacts') if isinstance(packet.get('contacts'), list) else []
    return out, contacts


def ingest(workspace_id: uuid.UUID, packet: dict, account: str | None) -> IngestResult:
    """Same idempotent path for history and live packets."""
    account = account if account and CHAT_RE.match(account) else 'whatsapp'
    records, contacts = records_from_packet(packet, account)
    with db.session() as s:
        ws = Scoped(s, workspace_id)
        self_ids = self_identifiers(ws)
        register_contacts(ws, contacts, self_ids)
        result = IngestResult()
        for rec in records:
            upsert(ws, rec, self_ids, result)
        schedule_rebuilds(ws, result)
        return result


# ---------- synthetic connector (development/tests only) ----------

FAKE: dict[uuid.UUID, int] = {}


class _FakeConnector:
    def status(self, ws: Scoped) -> dict:
        from . import synthetic, synthetic_data
        polls = FAKE.get(ws.wid, 0)
        FAKE[ws.wid] = polls + 1
        acct = ws.s.scalars(select(Account).where(Account.workspace_id == ws.wid)).first()
        user = next((k for k, v in synthetic_data.USERS.items() if acct and v['email'] == acct.email), None)
        if polls < 1 or user is None:
            return {'state': 'pairing', 'qr': synthetic.fake_qr_data_url()}
        me = synthetic_data.USERS[user]['whatsapp']
        if polls == 1:
            row = ws.first(ws.q(WhatsAppConnector))
            row.account_wid = me
            ws.s.flush()
            for packet in synthetic_data.whatsapp(user):
                records, contacts = records_from_packet(packet, me)
                self_ids = self_identifiers(ws)
                register_contacts(ws, contacts, self_ids)
                result = IngestResult()
                for rec in records:
                    upsert(ws, rec, self_ids, result)
                schedule_rebuilds(ws, result)
        n = len(synthetic_data.whatsapp(user))
        return {'state': 'ready', 'account': me, 'chats_total': n, 'history_chats': n, 'history_done': n, 'chats_synced': n,
                'last_sync': utcnow().isoformat()}


FAKE_CONNECTOR = _FakeConnector()
