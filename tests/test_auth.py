"""M1: combined Google onboarding, sessions and isolation (synthetic Google)."""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select, update

from app import config, db, security, synthetic
from app.models import Account, Connection, OAuthFlow, Session, SyncStream
from conftest import ORIGIN, workspace_of


def connection(email):
    with db.session() as s:
        return s.scalars(select(Connection).where(Connection.workspace_id == workspace_of(email))).one()


def test_start_requests_identity_gmail_calendar_offline_with_pkce(browser):
    r = browser.start_google()
    q = parse_qs(urlsplit(r.headers['location']).query)
    assert set(q['scope'][0].split()) == set(config.GOOGLE_SCOPES)
    assert q['access_type'] == ['offline'] and q['code_challenge_method'] == ['S256']
    assert q['redirect_uri'] == [ORIGIN + '/moresocial/api/auth/google/callback']
    assert 'consent' not in q['prompt'][0]  # no forced consent on ordinary sign-in
    cookie = next(c for c in r.headers.get_list('set-cookie') if c.startswith('moresocial_oauth_'))
    assert 'HttpOnly' in cookie and 'Secure' in cookie and 'Path=/moresocial/api/auth/google/callback' in cookie
    assert 'samesite=lax' in cookie.lower()


def test_full_consent_creates_account_session_and_grant(browser):
    r = browser.login('alex')
    assert r.status_code == 303 and r.headers['location'] == '/moresocial/home'
    assert 'code=' not in r.headers['location']
    cookie = next(c for c in r.headers.get_list('set-cookie') if c.startswith('moresocial_session='))
    for attr in ('HttpOnly', 'Secure', 'Path=/moresocial/'):
        assert attr in cookie
    assert 'Domain' not in cookie
    conn = connection('alex@example.test')
    assert conn.state == 'active' and config.GMAIL_SCOPE in conn.scopes and config.CALENDAR_SCOPE in conn.scopes
    assert conn.refresh_token_enc and 'refresh' not in conn.refresh_token_enc  # encrypted at rest
    assert security.decrypt(conn.refresh_token_enc)
    page = browser.get('/home')
    assert page.status_code == 200 and 'Alex Synthetic' in page.text
    assert 'client_secret' not in page.text and 'Cloud Console' not in browser.get('/connections').text


def test_partial_consent_keeps_sign_in_and_disables_feature(browser):
    browser.login('alex', gmail=False, calendar=True)
    conn = connection('alex@example.test')
    assert config.GMAIL_SCOPE not in conn.scopes
    with db.session() as s:
        streams = {st.stream: st for st in s.scalars(select(SyncStream).where(SyncStream.workspace_id == conn.workspace_id))}
    assert streams['gmail'].status == 'disabled' and streams['gmail'].next_run_at is None
    assert streams['calendar'].next_run_at is not None
    page = browser.get('/connections').text
    assert 'not allowed' in page and 'Retry: allow Gmail and Calendar' in page


def test_cancelled_consent(browser):
    r = browser.login('alex', action='cancel')
    assert r.headers['location'] == '/moresocial/?notice=google-cancelled'
    with db.session() as s:
        assert s.scalars(select(Account)).first() is None


def test_replayed_and_expired_state(browser):
    start = browser.start_google()
    loc = urlsplit(start.headers['location'])
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    r = browser.c.post('/moresocial/dev/google/authorize', data={'state': q['state'], 'nonce': q['nonce'], 'user': 'alex',
                                                                  'gmail': '1', 'calendar': '1', 'offline': 'yes'})
    cb = r.headers['location']
    cb_path = urlsplit(cb).path + '?' + urlsplit(cb).query
    assert browser.c.get(cb_path).headers['location'] == '/moresocial/home'
    replay = browser.c.get(cb_path)
    assert replay.headers['location'] == '/moresocial/?notice=google-expired'
    # expired flow
    start = browser.start_google()
    with db.session() as s:
        s.execute(update(OAuthFlow).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    r = browser.consent(start)
    assert r.headers['location'] == '/moresocial/?notice=google-expired'


def test_browser_binding_required(make_browser):
    a, b = make_browser(), make_browser()
    start = a.start_google()
    loc = urlsplit(start.headers['location'])
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    code = synthetic.issue_code('alex', list(config.GOOGLE_SCOPES), q['nonce'])
    r = b.c.get('/moresocial/api/auth/google/callback', params={'state': q['state'], 'code': code})
    assert r.headers['location'] == '/moresocial/?notice=google-failed'
    with db.session() as s:
        assert s.scalars(select(Account)).first() is None


def test_bad_nonce_and_unverified_email_rejected(browser):
    for claims in ({'nonce': 'wrong'}, {'email_verified': False}, {'aud': 'someone-else'}):
        start = browser.start_google()
        q = {k: v[0] for k, v in parse_qs(urlsplit(start.headers['location']).query).items()}
        code = synthetic.issue_code('alex', list(config.GOOGLE_SCOPES), q['nonce'], claims=claims)
        r = browser.c.get('/moresocial/api/auth/google/callback', params={'state': q['state'], 'code': code})
        assert r.headers['location'] == '/moresocial/?notice=google-failed', claims
    with db.session() as s:
        assert s.scalars(select(Account)).first() is None


def test_not_allowlisted_is_refused_before_storing_credentials(browser):
    from app import synthetic_data
    synthetic_data.USERS['mallory'] = {'sub': 'synthetic-sub-mallory', 'email': 'mallory@example.test', 'name': 'M',
                                       'whatsapp': '31600000029@c.us'}
    try:
        r = browser.login('mallory')
    finally:
        del synthetic_data.USERS['mallory']
    assert r.headers['location'] == '/moresocial/?notice=not-invited'
    with db.session() as s:
        assert s.scalars(select(Connection)).first() is None and s.scalars(select(Account)).first() is None
    assert 'mallory' in synthetic.STATE.revoked  # the unused grant was revoked


def test_missing_refresh_token_keeps_existing_and_flags_first_time(make_browser):
    a = make_browser()
    a.login('alex')
    stored = connection('alex@example.test').refresh_token_enc
    b = make_browser()
    b.login('alex', offline=False)  # returning sign-in: Google omits the refresh token
    assert connection('alex@example.test').refresh_token_enc == stored
    assert connection('alex@example.test').state == 'active'
    c = make_browser()
    c.login('blake', offline=False)  # first-time grant without offline access
    conn = connection('blake@example.test')
    assert conn.state == 'reconnect_required' and conn.detail == 'offline_access_missing'
    assert 'ongoing read access' in c.get('/connections').text


def test_concurrent_flows_do_not_overwrite_each_other(make_browser):
    a, b = make_browser(), make_browser()
    sa, sb = a.start_google(), b.start_google()
    sa2 = a.start_google()  # second tab in the same browser
    rb = b.consent(sb, 'blake')
    ra = a.consent(sa, 'alex')
    ra2 = a.consent(sa2, 'alex')
    assert rb.headers['location'] == ra.headers['location'] == ra2.headers['location'] == '/moresocial/home'
    assert 'Alex Synthetic' in a.get('/home').text and 'Blake Synthetic' in b.get('/home').text


def test_reconnect_requires_same_google_subject(browser):
    browser.login('alex')
    csrf = browser.csrf('/connections')
    start = browser.start_google('grant', csrf)
    q = parse_qs(urlsplit(start.headers['location']).query)
    assert 'consent' in q['prompt'][0] and q['login_hint'] == ['alex@example.test']
    r = browser.consent(start, 'blake')
    assert r.headers['location'] == '/moresocial/connections?notice=different-account'
    assert connection('alex@example.test').provider_account == 'synthetic-sub-alex'
    with db.session() as s:
        assert s.scalars(select(Account).where(Account.email == 'blake@example.test')).first() is None
    csrf = browser.csrf('/connections')
    r = browser.consent(browser.start_google('grant', csrf), 'alex')
    assert r.headers['location'] == '/moresocial/connections?notice=reconnected'


def test_revoked_grant_becomes_reconnect_required(browser):
    browser.login('alex')
    from app import accounts
    synthetic.STATE.revoked.add('alex')
    with db.session() as s:
        conn = s.scalars(select(Connection)).one()
        conn.access_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        try:
            accounts.access_token(s, conn)
        except Exception:
            pass
    assert connection('alex@example.test').state == 'reconnect_required'
    assert 'Reconnect Google' in browser.get('/connections').text


def test_logout_and_csrf(browser):
    browser.login('alex')
    assert browser.post('/api/auth/logout', {'csrf': 'wrong'}).status_code == 403
    r = browser.post('/api/auth/logout', {'csrf': browser.csrf()})
    assert r.headers['location'] == '/moresocial/?notice=signed-out'
    assert browser.get('/home').headers['location'] == '/moresocial/'
    with db.session() as s:
        assert all(x.revoked_at for x in s.scalars(select(Session)))


def test_mutations_need_csrf(browser):
    browser.login('alex')
    assert browser.post('/gatherings', {'title': 'x'}).status_code == 403
    assert browser.post('/gatherings', {'title': 'x', 'csrf': browser.csrf()}).status_code == 303


def test_two_users_are_isolated(make_browser):
    a, b = make_browser(), make_browser()
    a.login('alex')
    b.login('blake')
    r = a.post('/gatherings', {'title': 'Alex lasergame', 'csrf': a.csrf()})
    gid = r.headers['location'].rsplit('/', 1)[1]
    assert a.get('/gatherings/' + gid).status_code == 200
    assert b.get('/gatherings/' + gid).status_code == 404
    assert b.post(f'/gatherings/{gid}/edit', {'title': 'hijack', 'csrf': b.csrf()}).status_code == 404
    assert 'Alex lasergame' not in b.get('/gatherings').text


def test_state_changing_get_is_not_possible(browser):
    browser.login('alex')
    assert browser.get('/api/auth/logout').status_code in (404, 405)
    assert browser.get('/account/delete').status_code in (404, 405)
