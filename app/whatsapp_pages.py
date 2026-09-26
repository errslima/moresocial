"""WhatsApp conversation browser and explicit, CSRF-protected text sends."""
from __future__ import annotations

import uuid
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request

from . import config, conversations, whatsapp_send, suggestions
from .models import Suggestion, SuggestionReview, WhatsAppConnector, WhatsAppSend, Workspace
from .web import Ctx, Problem, redirect, render, require_mutation, require_user

router = APIRouter()


def page_url(**params) -> str:
    query = urlencode({k: v for k, v in params.items() if v not in (None, '', 1)})
    return config.get().url('/whatsapp') + ('?' + query if query else '')


@router.get('/whatsapp')
def whatsapp_page(request: Request, q: str = '', page: int = 1, account: str = '', chat: str = '', before: str = '',
                  suggestion: str = '', receipt: str = '', ctx: Ctx = Depends(require_user)):
    return conversation_page(request, ctx, q, page, account, chat, before, suggestion, receipt)


def conversation_page(request, ctx, q="", page=1, account="", chat="", before="", suggestion="", receipt="",
                      error=None, status=200, compose_text=None, request_id=None):
    if page < 1 or page > 10000:
        raise Problem(400, 'Choose a valid page.')
    ws = ctx.ws
    q = conversations.clean_query(q)
    tz = ZoneInfo(ctx.s.get(Workspace, ws.wid).timezone)
    selected, messages, older = None, [], False
    if account or chat or before:
        if not account or not chat or len(account) > 255 or len(chat) > 400:
            raise Problem(404, 'That conversation was not found.')
        selected = whatsapp_send.conversation(ws, tz, account, chat)
        loaded = conversations.history(ws, tz, selected, before[:64]) if selected else None
        if loaded is None:
            raise Problem(404, 'That conversation was not found.')
        messages, older = loaded
    draft = ''
    suggestion_row = None
    if suggestion:
        suggestion_row = ws.get(Suggestion, suggestion)
        review = ws.get(SuggestionReview, suggestion_row.review_id) if suggestion_row else None
        target = whatsapp_send.suggestion_target(ws, suggestion_row, review) if review else None
        if target != {'account': account, 'chat': chat}:
            raise Problem(404, 'That suggestion does not belong to this conversation.')
        if (suggestion_row.state in suggestions.OPEN and not suggestion_row.stale and not review.paused
                and suggestion_row.draft_review_revision == review.revision and suggestions.evidence(ws, suggestion_row)):
            draft = suggestion_row.draft
    result = ws.get(WhatsAppSend, receipt) if receipt else None
    if receipt and (not result or result.account != account or result.chat != chat):
        raise Problem(404, 'That send receipt was not found.')
    rows, more = conversations.conversations(ws, tz, q, page)
    connector = ws.first(ws.q(WhatsAppConnector))
    linked = bool(connector and connector.desired == 'running')
    stored = bool(rows) or conversations.has_history(ws)
    return render(request, 'whatsapp.html', ctx, status=status, error=error,
                  compose_text=draft if compose_text is None else compose_text,
                  request_id=request_id or uuid.uuid4(), can_send=selected and whatsapp_send.available(connector, account),
                  self_chat=connector.account_wid if connector and whatsapp_send.available(connector, connector.account_wid) else None,
                  receipt=result, suggestion_row=suggestion_row, rows=rows, more=more, q=q, page=page, selected=selected,
                  messages=messages, older=older, before=before, linked=linked, stored=stored, wa_url=page_url,
                  list_url=page_url(q=q, page=page))


@router.post('/whatsapp/send')
def send_message(request: Request, account: str = Form(...), chat: str = Form(...), text: str = Form(''),
                 request_id: uuid.UUID = Form(...), ctx: Ctx = Depends(require_mutation)):
    try:
        receipt = whatsapp_send.send(ctx.ws, account, chat, text, request_id)
    except Problem as exc:
        if exc.status not in (400, 409):
            raise
        return conversation_page(request, ctx, account=account, chat=chat, error=exc.message,
                                 status=exc.status, compose_text=text, request_id=request_id)
    if receipt.state != 'sent':
        return conversation_page(request, ctx, account=account, chat=chat, receipt=str(receipt.id),
                                 status=202 if receipt.state == 'unknown' else 409, compose_text=text,
                                 request_id=request_id if receipt.state == 'unknown' else None)
    return redirect('/whatsapp', account=account, chat=chat, receipt=str(receipt.id))
