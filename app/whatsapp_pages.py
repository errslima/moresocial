"""Read-only WhatsApp conversation browser over imported history.

Selection, search and paging live in the query string so refresh, Back and direct links
work without client-side routing. Rendering reads stored rows only; it never calls the
connector, and there is no send path.
"""
from __future__ import annotations

from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request

from . import config, conversations
from .models import WhatsAppConnector, Workspace
from .web import Ctx, Problem, render, require_user

router = APIRouter()


def page_url(**params) -> str:
    query = urlencode({k: v for k, v in params.items() if v not in (None, '', 1)})
    return config.get().url('/whatsapp') + ('?' + query if query else '')


@router.get('/whatsapp')
def whatsapp_page(request: Request, q: str = '', page: int = 1, account: str = '', chat: str = '', before: str = '',
                  ctx: Ctx = Depends(require_user)):
    if page < 1 or page > 10000:
        raise Problem(400, 'Choose a valid page.')
    ws = ctx.ws
    q = conversations.clean_query(q)
    tz = ZoneInfo(ctx.s.get(Workspace, ws.wid).timezone)
    selected, messages, older = None, [], False
    if account or chat or before:
        if not account or not chat or len(account) > 255 or len(chat) > 400:
            raise Problem(404, 'That conversation was not found.')
        selected = conversations.conversation(ws, tz, account, chat)
        loaded = conversations.history(ws, tz, selected, before[:64]) if selected else None
        if loaded is None:
            raise Problem(404, 'That conversation was not found.')
        messages, older = loaded
    rows, more = conversations.conversations(ws, tz, q, page)
    connector = ws.first(ws.q(WhatsAppConnector))
    linked = bool(connector and connector.desired == 'running')
    stored = bool(rows) or conversations.has_history(ws)
    return render(request, 'whatsapp.html', ctx, rows=rows, more=more, q=q, page=page, selected=selected,
                  messages=messages, older=older, before=before, linked=linked, stored=stored, wa_url=page_url,
                  list_url=page_url(q=q, page=page))
