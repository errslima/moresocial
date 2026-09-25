"""Synthetic Google for tests and local development only (never production).

Codes and tokens are HMAC-signed and stateless so separate web and worker processes
agree. Tests may mutate `SyntheticGoogle` state (new mail, deletions, revocations,
broken cursors) in-process.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx

from . import config, synthetic_data
from .google import GoogleProvider

CLIENT = config.GoogleClient(client_id='synthetic-client.apps.googleusercontent.com', client_secret='synthetic-secret',
                             redirect_uris=())
SECRET = b'moresocial-synthetic-provider-not-a-secret'


def _sign(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode().rstrip('=')
    mac = hmac.new(SECRET, raw.encode(), hashlib.sha256).hexdigest()[:32]
    return raw + '.' + mac


def _unsign(value: str) -> dict | None:
    try:
        raw, mac = value.rsplit('.', 1)
    except (ValueError, AttributeError):
        return None
    if not hmac.compare_digest(mac, hmac.new(SECRET, raw.encode(), hashlib.sha256).hexdigest()[:32]):
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
    except ValueError:
        return None


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')


def issue_code(user: str, scopes: list[str], nonce: str, offline: bool = True, **extra) -> str:
    return _sign({'t': 'code', 'u': user, 's': scopes, 'n': nonce, 'o': offline, 'exp': time.time() + 300, **extra})


def id_token(user: str, nonce: str, **overrides) -> str:
    """overrides: {'claims': {...}} replaces individual ID-token claims (for negative tests)."""
    info = synthetic_data.USERS[user]
    claims = {'iss': 'https://accounts.google.com', 'aud': CLIENT.client_id, 'sub': info['sub'], 'email': info['email'],
              'email_verified': True, 'name': info['name'], 'nonce': nonce, 'exp': time.time() + 600}
    claims.update(overrides.get('claims') or {})
    return _sign({'t': 'id', 'c': claims})


def verify_id_token(token: str, client_id: str) -> dict:
    data = _unsign(token)
    if not data or data.get('t') != 'id':
        raise ValueError('bad synthetic id token')
    return data['c']


class SyntheticGoogle:
    def __init__(self):
        self.revoked: set[str] = set()      # refresh-token user ids revoked in-process
        self.extra_mail: dict[str, list[dict]] = {}
        self.deleted_mail: dict[str, set[str]] = {}
        self.history_base = 1000
        self.calls: list[tuple[str, str]] = []  # (method, path) for assertions (no bodies)
        self.fail_next: list[int] = []          # HTTP statuses to return for the next API calls
        self.invalid_history = False
        self.invalid_sync_token = False
        self.cancelled_events: dict[str, set[str]] = {}

    # ---- fixture views
    def messages(self, user: str) -> list[dict]:
        rows = synthetic_data.gmail(user) + self.extra_mail.get(user, [])
        out = []
        for n, m in enumerate(rows):
            labels = m.get('labels', ['INBOX'])
            deleted = m['id'] in self.deleted_mail.get(user, set())
            out.append({**m, 'labels': labels, 'history': self.history_base + n + 1, 'deleted': deleted})
        return out

    def gmail_message(self, m: dict) -> dict:
        when = synthetic_data.anchor() + timedelta(days=m['days'], hours=9)
        headers = [{'name': 'From', 'value': m['frm']}, {'name': 'To', 'value': m['to']},
                   {'name': 'Subject', 'value': m['subject']}, {'name': 'Date', 'value': when.strftime('%a, %d %b %Y %H:%M:%S +0000')}]
        if 'html' in m:
            payload = {'mimeType': 'multipart/alternative', 'headers': headers, 'parts': [
                {'mimeType': 'text/html', 'headers': [{'name': 'Content-Type', 'value': 'text/html; charset="utf-8"'}],
                 'body': {'data': _b64(m['html'])}},
                {'mimeType': 'image/png', 'filename': 'photo.png', 'body': {'attachmentId': 'att1', 'size': 1200}}]}
        else:
            payload = {'mimeType': 'text/plain', 'headers': headers, 'body': {'data': _b64(m['body'])}}
        return {'id': m['id'], 'threadId': m['thread'], 'labelIds': m['labels'], 'internalDate': str(int(when.timestamp() * 1000)),
                'historyId': str(m['history']), 'snippet': (m.get('body') or '')[:80], 'payload': payload}

    def events(self, user: str) -> list[dict]:
        out = []
        for n, e in enumerate(synthetic_data.calendar(user)):
            status = 'cancelled' if e.get('status') == 'cancelled' or e['id'] in self.cancelled_events.get(user, set()) else 'confirmed'
            end = e['start'] + timedelta(hours=e['hours'])
            item = {'id': e['id'], 'status': status, 'summary': e['title'], 'etag': f'"{n}-{status}"',
                    'updated': (synthetic_data.anchor() - timedelta(days=30)).isoformat(),
                    'start': {'dateTime': e['start'].isoformat()}, 'end': {'dateTime': end.isoformat()},
                    'organizer': {'email': synthetic_data.USERS[user]['email'], 'self': True},
                    'attendees': [{'email': a, 'responseStatus': 'needsAction'} for a in e.get('attendees', [])]}
            if e.get('recurring'):
                item['recurringEventId'] = e['recurring']
            out.append(item)
        return out

    # ---- HTTP
    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        path = url.path
        self.calls.append((request.method, url.netloc + path))
        if url.netloc == 'oauth2.googleapis.com':
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if path == '/token':
                return self.token(form)
            if path == '/revoke':
                data = _unsign(form.get('token', ''))
                if data:
                    self.revoked.add(data['u'])
                return httpx.Response(200, json={})
        if request.method != 'GET':
            return httpx.Response(405, json={'error': {'message': 'read-only synthetic provider'}})
        if self.fail_next:
            status = self.fail_next.pop(0)
            reason = 'rateLimitExceeded' if status in (403, 429) else 'backendError'
            return httpx.Response(status, json={'error': {'errors': [{'reason': reason}]}})
        auth = request.headers.get('authorization', '')
        token = _unsign(auth.removeprefix('Bearer '))
        if not token or token.get('t') != 'access' or token.get('exp', 0) < time.time() or token['u'] in self.revoked:
            return httpx.Response(401, json={'error': {'status': 'UNAUTHENTICATED'}})
        user, scopes = token['u'], set(token['s'])
        params = parse_qs(url.query)
        q = lambda k, d=None: params.get(k, [d])[0]
        if url.netloc == 'gmail.googleapis.com':
            if config.GMAIL_SCOPE not in scopes:
                return httpx.Response(403, json={'error': {'errors': [{'reason': 'insufficientPermissions'}]}})
            return self.gmail(user, path, q)
        if url.netloc == 'www.googleapis.com' and path.endswith('/calendars/primary/events'):
            if config.CALENDAR_SCOPE not in scopes:
                return httpx.Response(403, json={'error': {'errors': [{'reason': 'insufficientPermissions'}]}})
            return self.calendar(user, q)
        return httpx.Response(404, json={'error': {'status': 'NOT_FOUND'}})

    def token(self, form: dict) -> httpx.Response:
        if form.get('client_id') != CLIENT.client_id or form.get('client_secret') != CLIENT.client_secret:
            return httpx.Response(401, json={'error': 'invalid_client'})
        if form.get('grant_type') == 'authorization_code':
            code = _unsign(form.get('code', ''))
            if not code or code.get('t') != 'code' or code['exp'] < time.time():
                return httpx.Response(400, json={'error': 'invalid_grant'})
            if code.get('verifier_hash') and code['verifier_hash'] != hashlib.sha256(form.get('code_verifier', '').encode()).hexdigest():
                return httpx.Response(400, json={'error': 'invalid_grant'})
            self.revoked.discard(code['u'])
            body = {'access_token': self._access(code['u'], code['s']), 'expires_in': 3599, 'token_type': 'Bearer',
                    'scope': ' '.join(code['s']), 'id_token': id_token(code['u'], code['n'], claims=code.get('claims'))}
            if code.get('o'):
                body['refresh_token'] = _sign({'t': 'refresh', 'u': code['u'], 's': code['s']})
            return httpx.Response(200, json=body)
        if form.get('grant_type') == 'refresh_token':
            data = _unsign(form.get('refresh_token', ''))
            if not data or data.get('t') != 'refresh' or data['u'] in self.revoked:
                return httpx.Response(400, json={'error': 'invalid_grant'})
            return httpx.Response(200, json={'access_token': self._access(data['u'], data['s']), 'expires_in': 3599,
                                             'scope': ' '.join(data['s'])})
        return httpx.Response(400, json={'error': 'unsupported_grant_type'})

    def _access(self, user: str, scopes: list[str]) -> str:
        return _sign({'t': 'access', 'u': user, 's': scopes, 'exp': time.time() + 3600})

    def gmail(self, user: str, path: str, q) -> httpx.Response:
        msgs = self.messages(user)
        visible = [m for m in msgs if not m['deleted']]
        if path.endswith('/profile'):
            return httpx.Response(200, json={'emailAddress': synthetic_data.USERS[user]['email'],
                                             'historyId': str(max(m['history'] for m in msgs))})
        if path.endswith('/messages'):
            rows = [m for m in visible if not ({'SPAM', 'TRASH'} & set(m['labels']))]
            rows.sort(key=lambda m: -m['days'])
            start, size = int(q('pageToken', '0')), int(q('maxResults', '100'))
            page = rows[start:start + size]
            body = {'messages': [{'id': m['id'], 'threadId': m['thread']} for m in page], 'resultSizeEstimate': len(rows)}
            if start + size < len(rows):
                body['nextPageToken'] = str(start + size)
            return httpx.Response(200, json=body)
        if '/messages/' in path:
            mid = path.rsplit('/', 1)[1]
            m = next((m for m in visible if m['id'] == mid), None)
            return httpx.Response(200, json=self.gmail_message(m)) if m else httpx.Response(404, json={'error': {}})
        if path.endswith('/history'):
            if self.invalid_history:
                return httpx.Response(404, json={'error': {'status': 'NOT_FOUND'}})
            start = int(q('startHistoryId', '0'))
            history = []
            for m in msgs:
                if m['history'] > start:
                    kind = 'messagesDeleted' if m['deleted'] else 'messagesAdded'
                    history.append({'id': str(m['history']), kind: [{'message': {'id': m['id'], 'threadId': m['thread'],
                                                                             'labelIds': m['labels']}}]})
            for mid in self.deleted_mail.get(user, set()):
                m = next((x for x in msgs if x['id'] == mid), None)
                if m and m['history'] <= start:
                    history.append({'id': str(start + 1), 'messagesDeleted': [{'message': {'id': mid, 'threadId': m['thread']}}]})
            # Two pages to exercise pagination.
            half = (len(history) + 1) // 2
            page = q('pageToken')
            items = history[half:] if page == 'p2' else history[:half]
            body = {'history': items, 'historyId': str(max([start] + [m['history'] for m in msgs]))}
            if page != 'p2' and len(history) > half:
                body['nextPageToken'] = 'p2'
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={'error': {}})

    def calendar(self, user: str, q) -> httpx.Response:
        events = self.events(user)
        if q('syncToken') is not None:
            if q('timeMin') or q('timeMax'):
                return httpx.Response(400, json={'error': {'errors': [{'reason': 'invalidParameter'}]}})
            if self.invalid_sync_token:
                return httpx.Response(410, json={'error': {'errors': [{'reason': 'fullSyncRequired'}]}})
            data = _unsign(q('syncToken')) or {}
            seen = data.get('etags', {})
            changed = [e for e in events if seen.get(e['id']) != e['etag']]
            return httpx.Response(200, json={'items': changed, 'nextSyncToken': self._sync_token(events)})
        tmin, tmax = q('timeMin'), q('timeMax')
        inside = [e for e in events if tmin <= e['start']['dateTime'] < tmax] if tmin and tmax else events
        start, size = int(q('pageToken', '0')), int(q('maxResults', '250'))
        size = min(size, 5)  # small pages exercise pagination
        page = inside[start:start + size]
        body = {'items': page}
        if start + size < len(inside):
            body['nextPageToken'] = str(start + size)
        else:
            body['nextSyncToken'] = self._sync_token(events)
        return httpx.Response(200, json=body)

    def _sync_token(self, events: list[dict]) -> str:
        return _sign({'t': 'sync', 'etags': {e['id']: e['etag'] for e in events}})


STATE = SyntheticGoogle()


def google_provider(state: SyntheticGoogle | None = None) -> GoogleProvider:
    s = state or STATE
    return GoogleProvider(transport=httpx.MockTransport(s.handler), authorize_url=config.get().url('/dev/google/authorize'),
                          verifier=verify_id_token, client=CLIENT)


def fake_qr_data_url() -> str:
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 29 29'><rect width='29' height='29' fill='#fff'/>"
           "<path d='M2 2h7v7H2zM20 2h7v7h-7zM2 20h7v7H2zM12 12h5v5h-5z' fill='#000'/></svg>")
    return 'data:image/svg+xml;base64,' + base64.b64encode(svg.encode()).decode()


def now() -> datetime:
    return datetime.now(timezone.utc)
