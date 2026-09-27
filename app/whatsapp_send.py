"""Explicit text sends, restricted to the workspace's linked account and stored chats."""
import hashlib
import json
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.postgresql import insert

from . import config, conversations, whatsapp
from .models import Source, WhatsAppConnector, WhatsAppSend
from .web import Problem


def suggestion_target(ws, row, review):
    # Use the actual evidence account, never a phone guessed from a person's name.
    if row.producer == 'proactive':
        # Resolved from stored messages when prepared; the database ties it to this review's chat.
        if not row.target_account or row.target_chat != review.conversation_id:
            return None
        return {'account': row.target_account, 'chat': row.target_chat}
    src = ws.get(Source, row.anchor_id)
    if (not src or src.provider != 'whatsapp' or src.deleted_at or not src.included
            or src.conversation_id != review.conversation_id):
        return None
    return {'account': src.provider_account, 'chat': src.conversation_id}


def available(connector, account):
    return bool(connector and connector.desired == 'running' and connector.observed == 'ready'
                and connector.account_wid == account)


def self_chat(connector):
    return (connector.status or {}).get('self_chat') or connector.account_wid


def conversation(ws, tz, account, chat):
    stored = conversations.conversation(ws, tz, account, chat)
    if stored:
        return stored
    connector = ws.first(ws.q(WhatsAppConnector))
    if (available(connector, account) and chat in (account, self_chat(connector))
            and whatsapp.CHAT_RE.fullmatch(chat)):
        return conversations.Summary(account, chat, 'You', False, 'Y', 0, None, '', '', '')
    return None


def send(ws, account, chat, text, request_id):
    text = text.strip()
    if not text or len(text) > 4000 or '\x00' in text:
        raise Problem(400, 'Enter a message of 1–4000 characters.')
    if len(account) > 255 or len(chat) > 400 or not whatsapp.CHAT_RE.fullmatch(chat):
        raise Problem(404, 'That conversation was not found.')
    if not conversation(ws, ZoneInfo('UTC'), account, chat):
        raise Problem(404, 'That conversation was not found.')
    fingerprint = hashlib.sha256(json.dumps([account, chat, text], ensure_ascii=False).encode()).hexdigest()
    previous = ws.get(WhatsAppSend, request_id)
    if previous:
        if previous.fingerprint != fingerprint:
            raise Problem(409, 'This send request was already used. Reload the conversation before composing another message.')
        return previous
    connector = ws.first(ws.q(WhatsAppConnector))
    if not available(connector, account):
        raise Problem(409, 'This WhatsApp account is not ready to send. Check Connections, then reload this conversation.')
    # Commit the claim BEFORE the external side effect. Concurrent submissions and a
    # crash/timeout cannot cause an automatic resend. Only the winner calls the connector.
    claimed = ws.s.scalar(insert(WhatsAppSend).values(id=request_id, workspace_id=ws.wid,
        account=account, chat=chat, fingerprint=fingerprint, state='unknown')
        .on_conflict_do_nothing().returning(WhatsAppSend.id))
    ws.s.commit()
    if not claimed:
        previous = ws.get(WhatsAppSend, request_id)
        if not previous or previous.fingerprint != fingerprint:
            raise Problem(409, 'This send request was already used. Reload the conversation.')
        return previous
    receipt = ws.get(WhatsAppSend, request_id)
    try:
        result = (fake_send(account, chat, text) if config.get().synthetic_providers else
                  whatsapp.call(connector, 'POST', '/send', timeout=45,
                                payload={'account': account, 'chat': chat, 'text': text}))
    except whatsapp.ConnectorUnavailable:
        return receipt  # unknown: do not silently retry a possible delivery
    if result.get('state') == 'rejected':
        receipt.state = 'rejected'
    elif result.get('state') == 'sent':
        packet = result.get('packet') or {}
        messages = packet.get('messages') or []
        destination = packet.get('chat', {}).get('id', '')
        if (result.get('requested_chat') == chat and isinstance(destination, str)
                and whatsapp.CHAT_RE.fullmatch(destination) and len(messages) == 1
                and messages[0].get('from_me') is True and messages[0].get('body') == text
                and whatsapp.MSG_ID_RE.fullmatch(str(messages[0].get('id', '')))):
            receipt.state, receipt.message_id, receipt.chat = 'sent', messages[0]['id'], destination
            ws.s.commit()
            # Same idempotent ingest as live events; the sent bubble appears immediately.
            whatsapp.ingest(ws.wid, packet, account)
    return receipt


def fake_send(account, chat, text):
    return {'state': 'sent', 'requested_chat': chat, 'packet': {'chat': {'id': chat, 'name': chat, 'is_group': chat.endswith('@g.us')},
        'messages': [{'id': 'true_' + chat + '_' + uuid.uuid4().hex, 'ts': int(whatsapp.utcnow().timestamp()),
                      'sender': account, 'from_me': True, 'kind': 'chat', 'body': text}]}}
