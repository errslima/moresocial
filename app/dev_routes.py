"""Synthetic Google consent page. Mounted only when SYNTHETIC_PROVIDERS is enabled, which
configuration refuses in production and on the production URL."""
from __future__ import annotations

import hashlib
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from . import config, synthetic, synthetic_data
from .web import render

router = APIRouter()


@router.get('/dev/google/authorize')
def authorize_page(request: Request):
    q = request.query_params
    return render(request, 'dev_authorize.html', None, users=synthetic_data.USERS, state=q.get('state', ''),
                  nonce=q.get('nonce', ''), challenge=q.get('code_challenge', ''), consent=q.get('prompt', ''))


@router.post('/dev/google/authorize')
def authorize_submit(state: str = Form(''), nonce: str = Form(''), user: str = Form('alex'), gmail: str = Form(''),
                     calendar: str = Form(''), offline: str = Form(''), action: str = Form('allow')):
    target = config.get().url('/api/auth/google/callback')
    if action != 'allow' or user not in synthetic_data.USERS:
        return RedirectResponse(target + '?' + urlencode({'state': state, 'error': 'access_denied'}), status_code=303)
    scopes = list(config.GOOGLE_SCOPES_IDENTITY)
    if gmail:
        scopes.append(config.GMAIL_SCOPE)
    if calendar:
        scopes.append(config.CALENDAR_SCOPE)
    code = synthetic.issue_code(user, scopes, nonce, offline=(offline == 'yes'))
    return RedirectResponse(target + '?' + urlencode({'state': state, 'code': code}), status_code=303)
