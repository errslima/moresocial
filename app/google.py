"""Google OAuth and read-only Gmail/Calendar API access.

The same code runs against Google and against the synthetic provider: only the HTTP
transport, the authorization URL and the ID-token verifier are swapped. Nothing here
logs tokens, codes, or response bodies.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import quote, urlencode

import httpx

from . import config, security

AUTHORIZE_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
REVOKE_URL = 'https://oauth2.googleapis.com/revoke'
GMAIL = 'https://gmail.googleapis.com/gmail/v1/users/me/'
CALENDAR_EVENTS = 'https://www.googleapis.com/calendar/v3/calendars/primary/events'
ISSUERS = ('https://accounts.google.com', 'accounts.google.com')


class GoogleError(Exception):
    """kind: auth (grant revoked/expired), forbidden, throttled, gone, not_found, unavailable, invalid."""

    def __init__(self, kind: str, status: int | None = None, reason: str | None = None):
        super().__init__(kind)
        self.kind = kind
        self.status = status
        self.reason = reason


@dataclass
class TokenResult:
    access_token: str
    expires_in: int
    scopes: frozenset[str]
    refresh_token: str | None
    id_token: str | None


@dataclass
class Identity:
    sub: str
    email: str
    name: str | None


class GoogleProvider:
    def __init__(self, transport: httpx.BaseTransport | None = None, authorize_url: str = AUTHORIZE_URL,
                 verifier: Callable[[str, str], dict] | None = None, client: config.GoogleClient | None = None):
        self.transport = transport
        self.authorize_url = authorize_url
        self._verifier = verifier
        self._client = client

    # ---- configuration
    def client(self) -> config.GoogleClient:
        c = self._client or config.get().google_client()
        if c is None:
            raise GoogleError('unconfigured')
        return c

    def http(self) -> httpx.Client:
        return httpx.Client(timeout=20, follow_redirects=False, transport=self.transport)

    # ---- OAuth
    def authorization_url(self, *, state: str, nonce: str, verifier: str, consent: bool, login_hint: str | None) -> str:
        settings = config.get()
        params = {'client_id': self.client().client_id, 'redirect_uri': settings.google_redirect_uri,
                  'response_type': 'code', 'scope': ' '.join(config.GOOGLE_SCOPES), 'state': state, 'nonce': nonce,
                  'code_challenge': security.pkce_challenge(verifier), 'code_challenge_method': 'S256',
                  'access_type': 'offline', 'include_granted_scopes': 'true',
                  'prompt': 'consent select_account' if consent else 'select_account'}
        if login_hint:
            params['login_hint'] = login_hint
        return self.authorize_url + '?' + urlencode(params)

    def exchange_code(self, code: str, verifier: str) -> TokenResult:
        c = self.client()
        data = self._post(TOKEN_URL, {'client_id': c.client_id, 'client_secret': c.client_secret, 'code': code,
                                      'code_verifier': verifier, 'grant_type': 'authorization_code',
                                      'redirect_uri': config.get().google_redirect_uri})
        return self._token(data)

    def refresh(self, refresh_token: str) -> TokenResult:
        c = self.client()
        data = self._post(TOKEN_URL, {'client_id': c.client_id, 'client_secret': c.client_secret,
                                      'grant_type': 'refresh_token', 'refresh_token': refresh_token})
        return self._token(data)

    def revoke(self, token: str) -> bool:
        try:
            with self.http() as h:
                r = h.post(REVOKE_URL, data={'token': token})
            return r.status_code in (200, 400)  # 400: already invalid
        except httpx.HTTPError as exc:
            security.emit('google_revoke_failed', exc)
            return False

    def verify_identity(self, id_token: str | None, nonce: str) -> Identity:
        if not id_token:
            raise GoogleError('invalid', reason='missing_id_token')
        client_id = self.client().client_id
        try:
            claims = self._verifier(id_token, client_id) if self._verifier else _verify_google_id_token(id_token, client_id)
        except GoogleError:
            raise
        except Exception as exc:
            security.emit('google_id_token_invalid', exc)
            raise GoogleError('invalid', reason='id_token') from None
        # google-auth checks signature, audience and expiry; recheck the fields we rely on.
        now = datetime.now(timezone.utc).timestamp()
        if claims.get('iss') not in ISSUERS or claims.get('aud') != client_id or float(claims.get('exp', 0)) < now:
            raise GoogleError('invalid', reason='claims')
        if not security.same(str(claims.get('nonce', '')), nonce):
            raise GoogleError('invalid', reason='nonce')
        if claims.get('email_verified') is not True or not isinstance(claims.get('email'), str):
            raise GoogleError('invalid', reason='email_unverified')
        sub = claims.get('sub')
        if not isinstance(sub, str) or not sub or len(sub) > 255:
            raise GoogleError('invalid', reason='subject')
        name = claims.get('name') if isinstance(claims.get('name'), str) else None
        return Identity(sub=sub, email=claims['email'].strip().lower(), name=(name or '')[:200] or None)

    def _post(self, url: str, form: dict) -> dict:
        try:
            with self.http() as h:
                r = h.post(url, data=form)
        except httpx.HTTPError as exc:
            security.emit('google_token_request_failed', exc)
            raise GoogleError('unavailable') from None
        if r.status_code in (400, 401):
            error = ''
            try:
                error = str(r.json().get('error', ''))
            except ValueError:
                pass
            security.emit('google_token_refused', status=r.status_code, code=error[:40] or None)
            raise GoogleError('auth', r.status_code, error or None)
        if r.status_code == 429:
            raise GoogleError('throttled', 429)
        if not r.is_success:
            raise GoogleError('unavailable', r.status_code)
        try:
            return r.json()
        except ValueError:
            raise GoogleError('unavailable', r.status_code) from None

    @staticmethod
    def _token(data: dict) -> TokenResult:
        if not isinstance(data.get('access_token'), str):
            raise GoogleError('invalid', reason='access_token')
        return TokenResult(access_token=data['access_token'], expires_in=min(int(data.get('expires_in') or 3600), 3600),
                           scopes=frozenset(str(data.get('scope', '')).split()), refresh_token=data.get('refresh_token'),
                           id_token=data.get('id_token'))

    # ---- read-only API
    def get(self, access_token: str, url: str, params: dict | None = None) -> dict:
        try:
            with self.http() as h:
                r = h.get(url, params=params, headers={'Authorization': 'Bearer ' + access_token})
        except httpx.HTTPError as exc:
            security.emit('google_api_failed', exc)
            raise GoogleError('unavailable') from None
        if r.is_success:
            try:
                return r.json()
            except ValueError:
                raise GoogleError('unavailable', r.status_code) from None
        reason = None
        try:
            err = r.json().get('error', {})
            if isinstance(err, dict):
                reason = next((e.get('reason') for e in err.get('errors') or [] if isinstance(e, dict)), None) or err.get('status')
        except (ValueError, AttributeError):
            pass
        security.emit('google_api_refused', status=r.status_code, code=(reason or '')[:40] or None)
        if r.status_code == 401:
            raise GoogleError('auth', 401, reason)
        if r.status_code == 429 or (r.status_code == 403 and reason in ('rateLimitExceeded', 'userRateLimitExceeded', 'RESOURCE_EXHAUSTED')):
            raise GoogleError('throttled', r.status_code, reason)
        if r.status_code == 403:
            raise GoogleError('forbidden', 403, reason)
        if r.status_code == 404:
            raise GoogleError('not_found', 404, reason)
        if r.status_code == 410:
            raise GoogleError('gone', 410, reason)
        raise GoogleError('unavailable', r.status_code, reason)

    def gmail_profile(self, token: str) -> dict:
        return self.get(token, GMAIL + 'profile')

    def gmail_list(self, token: str, query: str, page_token: str | None, max_results: int) -> dict:
        params = {'q': query, 'maxResults': max_results, 'includeSpamTrash': 'false'}
        if page_token:
            params['pageToken'] = page_token
        return self.get(token, GMAIL + 'messages', params)

    def gmail_message(self, token: str, message_id: str) -> dict:
        return self.get(token, GMAIL + 'messages/' + quote(message_id, safe=''), {'format': 'full'})

    def gmail_history(self, token: str, start: str, page_token: str | None) -> dict:
        params = {'startHistoryId': start, 'maxResults': 500,
                  'historyTypes': ['messageAdded', 'messageDeleted', 'labelAdded', 'labelRemoved']}
        if page_token:
            params['pageToken'] = page_token
        return self.get(token, GMAIL + 'history', params)

    def calendar_window(self, token: str, time_min: str, time_max: str, page_token: str | None) -> dict:
        # Bounded full refresh: time filters, no syncToken.
        params = {'timeMin': time_min, 'timeMax': time_max, 'singleEvents': 'true', 'showDeleted': 'true',
                  'maxResults': 250}
        if page_token:
            params['pageToken'] = page_token
        return self.get(token, CALENDAR_EVENTS, params)

    def calendar_incremental(self, token: str, sync_token: str, page_token: str | None) -> dict:
        # Incremental: syncToken with the same singleEvents setting, never timeMin/timeMax.
        params = {'syncToken': sync_token, 'singleEvents': 'true', 'maxResults': 250}
        if page_token:
            params['pageToken'] = page_token
        return self.get(token, CALENDAR_EVENTS, params)


def _verify_google_id_token(token: str, client_id: str) -> dict:
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token
    return id_token.verify_oauth2_token(token, Request(), client_id)


_provider: GoogleProvider | None = None


def provider() -> GoogleProvider:
    global _provider
    if _provider is None:
        settings = config.get()
        if settings.synthetic_providers:
            from . import synthetic
            _provider = synthetic.google_provider()
        else:
            _provider = GoogleProvider()
    return _provider


def set_provider(p: GoogleProvider | None) -> None:
    global _provider
    _provider = p


def expiry(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=max(0, seconds - 60))
