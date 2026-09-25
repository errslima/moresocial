"""Combined Google onboarding: identity + Gmail read + Calendar read in one flow."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import time
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, text
from sqlalchemy.orm import Session as DB

from . import accounts, config, db, security
from .google import GoogleError, provider
from .models import Account, OAuthFlow
from .web import SESSION_COOKIE, Ctx, Problem, get_db, optional_user, redirect

router = APIRouter()
FLOW_SECONDS = 600
_hits: dict[str, deque] = defaultdict(deque)


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else 'unknown'
    if peer in config.get().trusted_proxy_ips:
        forwarded = request.headers.get('x-forwarded-for', '')
        if forwarded:
            return forwarded.split(',')[-1].strip()[:64]
    return peer


def throttle(request: Request, bucket: str, limit: int, window: int = 600) -> None:
    key = bucket + ':' + client_ip(request)
    q, now = _hits[key], time.monotonic()
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        raise Problem(429, 'Too many attempts. Please wait a few minutes and try again.')
    q.append(now)
    if len(_hits) > 10000:
        _hits.clear()


def callback_path() -> str:
    return config.get().url('/api/auth/google/callback')


@router.post('/api/auth/google/start')
def google_start(request: Request, purpose: str = Form('login'), csrf: str = Form(''),
                       ctx: Ctx | None = Depends(optional_user), s: DB = Depends(get_db)):
    throttle(request, 'google_start', 20)
    if purpose not in ('login', 'grant'):
        raise Problem(400, 'Unknown sign-in request.')
    if purpose == 'grant':
        if ctx is None:
            return redirect('/')
        if not security.same(csrf, ctx.csrf):
            raise Problem(403, 'This form expired. Reload the page and try again.')
    try:
        provider().client()
    except GoogleError:
        return redirect('/', notice='unconfigured')
    state, browser, verifier, nonce = (security.token(32) for _ in range(4))
    flow_id = uuid.uuid4()
    cookie = 'moresocial_oauth_' + flow_id.hex[:12]
    s.execute(delete(OAuthFlow).where(OAuthFlow.expires_at < datetime.now(timezone.utc) - timedelta(hours=1)))
    s.add(OAuthFlow(id=flow_id, state_hash=security.digest(state), browser_hash=security.digest(browser), cookie_name=cookie,
                    verifier=verifier, nonce=nonce, purpose=purpose, workspace_id=ctx.ws.wid if (ctx and purpose == 'grant') else None,
                    expires_at=datetime.now(timezone.utc) + timedelta(seconds=FLOW_SECONDS)))
    # Consent is requested only to (re)establish offline access or missing scopes.
    url = provider().authorization_url(state=state, nonce=nonce, verifier=verifier, consent=(purpose == 'grant'),
                                       login_hint=ctx.account.email if (ctx and purpose == 'grant') else None)
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(cookie, browser, max_age=FLOW_SECONDS, path=callback_path(), secure=config.get().cookie_secure,
                        httponly=True, samesite='lax')
    return response


def consume_flow(state: str) -> OAuthFlow | None:
    """Single use: mark the flow used in its own transaction so a replay fails even if the
    rest of the callback errors."""
    if not state or len(state) > 200:
        return None
    with db.session() as s:
        row = s.execute(text("""UPDATE oauth_flows SET used_at = now()
                                WHERE state_hash = :h AND used_at IS NULL AND expires_at > now()
                                RETURNING id"""), {'h': security.digest(state)}).first()
        if not row:
            return None
        flow = s.get(OAuthFlow, row[0])
        s.expunge(flow)
        return flow


@router.get('/api/auth/google/callback')
def google_callback(request: Request, state: str = '', code: str = '', error: str = ''):
    throttle(request, 'google_callback', 30)
    flow = consume_flow(state)
    if flow is None:
        return finish(redirect('/', notice='google-expired'), None)
    browser = request.cookies.get(flow.cookie_name, '')
    if not security.same(security.digest(browser), flow.browser_hash):
        security.emit('google_callback_browser_mismatch')
        return finish(redirect('/', notice='google-failed'), flow)
    back = '/connections' if flow.purpose == 'grant' else '/'
    if error or not code:
        return finish(redirect(back, notice='google-cancelled'), flow)
    g = provider()
    try:
        tokens = g.exchange_code(code, flow.verifier)
        identity = g.verify_identity(tokens.id_token, flow.nonce)
    except GoogleError as exc:
        security.emit('google_signin_failed', exc, code=exc.reason or exc.kind)
        return finish(redirect(back, notice='google-failed'), flow)
    revoke = tokens.refresh_token or tokens.access_token
    if not accounts.allowed(identity.email):
        g.revoke(revoke)
        security.emit('google_signin_not_allowlisted')
        return finish(redirect('/', notice='not-invited'), flow)
    with db.session() as s:
        if flow.purpose == 'grant':
            acct = s.query(Account).filter(Account.workspace_id == flow.workspace_id).first()
            if acct is None or acct.google_sub != identity.sub:
                # Reconnection must keep the same Google subject. Revoke the stray grant only if
                # it belongs to no Moresocial account (revoking would break that user's access).
                if s.query(Account).filter(Account.google_sub == identity.sub).first() is None:
                    g.revoke(revoke)
                return finish(redirect('/connections', notice='different-account'), flow)
        acct = accounts.account_for(s, identity)
        try:
            accounts.store_grant(s, acct, identity, tokens)
        except GoogleError:
            s.rollback()
            return finish(redirect('/connections', notice='different-account'), flow)
        accounts.revoke_session(s, request.cookies.get(SESSION_COOKIE))
        raw = accounts.create_session(s, acct)
    response = redirect('/home' if flow.purpose == 'login' else '/connections',
                        **({} if flow.purpose == 'login' else {'notice': 'reconnected'}))
    settings = config.get()
    response.set_cookie(SESSION_COOKIE, raw, max_age=settings.session_days * 86400, path=settings.cookie_path,
                        secure=settings.cookie_secure, httponly=True, samesite='lax')
    return finish(response, flow)


def finish(response, flow: OAuthFlow | None):
    if flow is not None:
        response.delete_cookie(flow.cookie_name, path=callback_path(), secure=config.get().cookie_secure,
                               httponly=True, samesite='lax')
    return response


@router.post('/api/auth/logout')
def logout(request: Request, csrf: str = Form(''), ctx: Ctx | None = Depends(optional_user)):
    if ctx is not None:
        if not security.same(csrf, ctx.csrf):
            raise Problem(403, 'This form expired. Reload the page and try again.')
        accounts.revoke_session(ctx.s, ctx.raw_token)
    response = redirect('/', notice='signed-out')
    settings = config.get()
    response.delete_cookie(SESSION_COOKIE, path=settings.cookie_path, secure=settings.cookie_secure, httponly=True,
                           samesite='lax')
    return response
