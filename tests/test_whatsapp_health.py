"""WhatsApp sync health: authenticated heartbeats, receipt-time freshness, one classifier."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import db, security, whatsapp, whatsapp_health
from app.internal import create_internal_app
from app.models import Job, Source, WhatsAppConnector
from conftest import workspace_of

NOW = datetime(2026, 9, 27, 1, 10, tzinfo=timezone.utc)


def row(checked=NOW - timedelta(seconds=20), desired='running', updated=NOW - timedelta(minutes=42), **report):
    at = report.pop('at', (NOW - timedelta(seconds=20)).isoformat())
    return SimpleNamespace(desired=desired, observed='ready', updated_at=updated, health_checked_at=checked,
                           health=whatsapp_health.validate(whatsapp.fake_health(at=at, chats=50, **report)) if checked else {})


def classify(r, now=NOW):
    return whatsapp_health.classify(r, now)


# ---------------- classifier (pure)

def test_old_row_with_fresh_ready_heartbeat_is_healthy():
    # The production observation: row unchanged for 42 minutes, live state ready, zero pending.
    h = classify(row())
    assert h.code == 'ok' and not h.warning


def test_missing_or_expired_heartbeat_is_unknown_not_disconnected():
    never = SimpleNamespace(desired='running', observed='ready', updated_at=NOW, health_checked_at=None, health={})
    assert classify(never).code == 'unknown' and classify(never).warning
    assert classify(row(checked=NOW - timedelta(minutes=2, seconds=59))).code == 'ok'
    expired = classify(row(checked=NOW - timedelta(minutes=3, seconds=1)))
    assert expired.code == 'unknown' and 'unavailable' in expired.message and 'disconnected' not in expired.message


def test_disconnect_pairing_and_not_linked_are_actionable():
    assert classify(row(state='disconnected')).code == 'disconnected'
    assert classify(row(state='auth_failure')).level == 'action'
    assert classify(row(state='pairing')).code == 'needs_pairing'
    assert classify(None).code == 'not_linked'
    assert classify(row(desired='stopped')).code == 'not_linked'


def test_pending_error_and_partial_sweeps_report_delay_or_incomplete():
    assert classify(row(pending=3, pending_age_s=5)).code == 'ok'  # draining normally
    assert classify(row(pending=3, pending_age_s=75)).code == 'delayed'
    assert classify(row(ingest_failing=True, pending=1)).code == 'delayed'
    assert classify(row(state='error', error_code='browser_failed')).code == 'delayed'
    partial = classify(row(sweep={'outcome': 'partial', 'failed_chats': 2}))
    assert partial.code == 'incomplete' and '2 chat' in partial.message


def test_quiet_idle_connector_and_sweep_timing_do_not_warn():
    # A 15-minute sweep interval is not judged by the three-minute heartbeat rule.
    for minutes in (14, 16, 45, 59):
        assert classify(row(at=(NOW - timedelta(minutes=minutes)).isoformat())).code == 'ok', minutes
    overdue = classify(row(at=(NOW - timedelta(minutes=61)).isoformat()))
    assert overdue.code == 'delayed'
    # A running sweep that started long after the last one finished is not overdue.
    assert classify(row(at=(NOW - timedelta(minutes=61)).isoformat(), syncing=True)).code == 'ok'


def test_initial_history_sweep_is_informational_and_does_not_claim_completeness():
    h = classify(row(syncing=True, history={'history_done': 12, 'history_chats': 50}))
    assert h.code == 'history_loading' and h.level == 'info' and not h.warning and '12 of 50' in h.message
    first = classify(row(sweep={'last_at': None, 'last_complete_at': None, 'outcome': None}))
    assert first.code == 'history_loading'


@pytest.mark.parametrize('damage', [
    {'version': 2}, {'state': 'teleporting'}, {'pending': -1}, {'pending': 'many'}, {'pending': True},
    {'syncing': 'yes'}, {'sweep': {'outcome': 'great'}}, {'sweep': {'last_at': 'yesterday'}},
    {'sweep': {'last_at': '2026-09-27T01:00:00'}}, {'history': {'history_done': 10 ** 9}}, {'error_code': 'ENOSPC detail'},
    {'sweep': None}])
def test_malformed_reports_are_rejected(damage):
    with pytest.raises(ValueError):
        whatsapp_health.validate(whatsapp.fake_health(**damage))


# ---------------- internal endpoint

@pytest.fixture
def internal(database):
    return TestClient(create_internal_app())


def linked(make_browser, *users):
    out = []
    for user in users:
        b = make_browser()
        b.login(user)
        assert b.post('/connections/whatsapp/connect', {'csrf': b.csrf('/connections')}).status_code == 303
        out.append(b)
    return out


def key_of(email):
    with db.session() as s:
        return security.decrypt(s.scalars(select(WhatsAppConnector).where(
            WhatsAppConnector.workspace_id == workspace_of(email))).one().key_enc)


def connector(email):
    with db.session() as s:
        return s.scalars(select(WhatsAppConnector).where(WhatsAppConnector.workspace_id == workspace_of(email))).one()


def auth(email):
    return {'Authorization': 'Bearer ' + key_of(email)}


def test_heartbeat_is_scoped_by_key_bounded_and_stored_at_receipt_time(make_browser, internal):
    linked(make_browser, 'alex', 'blake')
    with db.session() as s:
        jobs_before = s.scalar(select(func.count()).select_from(Job))
        c = s.scalars(select(WhatsAppConnector).where(WhatsAppConnector.workspace_id == workspace_of('alex@example.test'))).one()
        c.account_wid, c.detail = '31600000123@c.us', 'Previous ingest detail'
    # A connector clock in the past or future does not decide freshness.
    report = whatsapp.fake_health(at='2020-01-01T00:00:00+00:00', account='31699999999@c.us', qr='data:image/png;base64,AAA',
                                  workspace=workspace_of('blake@example.test').hex)
    r = internal.post('/internal/connector/health', json=report, headers=auth('alex@example.test'))
    assert r.status_code == 200 and r.json()['stored'] is True
    alex, blake = connector('alex@example.test'), connector('blake@example.test')
    assert blake.health_checked_at is None and blake.health == {}
    assert abs((alex.health_checked_at - datetime.now(timezone.utc)).total_seconds()) < 30
    assert 'account' not in alex.health and 'qr' not in alex.health and 'workspace' not in alex.health
    assert alex.account_wid == '31600000123@c.us' and alex.detail == 'Previous ingest detail'
    assert alex.observed == 'ready'
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == jobs_before  # no AI or sync work


def test_invalid_foreign_or_oversized_heartbeats_are_refused(make_browser, internal):
    linked(make_browser, 'alex')
    url = '/internal/connector/health'
    assert internal.post(url, json=whatsapp.fake_health(), headers={'Authorization': 'Bearer not-a-key'}).status_code == 401
    assert internal.post(url, json=whatsapp.fake_health(),
                         headers={'Authorization': 'Bearer synthetic-operator-key-for-tests-only'}).status_code == 401
    headers = auth('alex@example.test')
    assert internal.post(url, json=whatsapp.fake_health(state='weird'), headers=headers).status_code == 400
    assert internal.post(url, content=b'{not json', headers=headers).status_code == 400
    assert internal.post(url, content=b'[1, 2]', headers=headers).status_code == 400
    big = whatsapp.fake_health(padding='x' * 5000)
    assert internal.post(url, json=big, headers=headers).status_code == 400
    assert connector('alex@example.test').health_checked_at is None


def test_rejected_heartbeat_does_not_block_ingestion(make_browser, internal):
    linked(make_browser, 'alex')
    headers = auth('alex@example.test')
    assert internal.post('/internal/connector/health', json={'version': 1}, headers=headers).status_code == 400
    import time
    packet = {'chat': {'id': '31600000001@c.us', 'name': 'Noor', 'is_group': False}, 'contacts': [],
              'messages': [{'id': 'false_31600000001@c.us_H1', 'ts': int(time.time()) - 60, 'sender': '31600000001@c.us',
                            'from_me': False, 'kind': 'chat', 'body': 'Still there?'}]}
    assert internal.post('/internal/connector/ingest', json=packet, headers=headers).json()['created'] == 1


def test_stopped_connector_cannot_report(make_browser, internal):
    b, = linked(make_browser, 'alex')
    key = auth('alex@example.test')
    assert b.post('/connections/whatsapp/disconnect', {'csrf': b.csrf('/connections')}).status_code == 303
    assert internal.post('/internal/connector/health', json=whatsapp.fake_health(), headers=key).status_code == 401


def test_heartbeat_state_change_updates_observed_state(make_browser, internal):
    linked(make_browser, 'alex')
    internal.post('/internal/connector/health', json=whatsapp.fake_health(state='disconnected'), headers=auth('alex@example.test'))
    assert connector('alex@example.test').observed == 'disconnected'


# ---------------- pages

def put_health(email, checked, **report):
    with db.session() as s:
        c = s.scalars(select(WhatsAppConnector).where(WhatsAppConnector.workspace_id == workspace_of(email))).one()
        c.observed, c.health, c.health_checked_at = 'ready', whatsapp.fake_health(**report), checked
    with db.session() as s:  # onupdate stamps updated_at; age it like the production row
        s.execute(WhatsAppConnector.__table__.update().values(updated_at=datetime.now(timezone.utc) - timedelta(hours=1)))


def test_suggestions_page_uses_heartbeat_not_row_age(make_browser):
    b, = linked(make_browser, 'alex')
    now = datetime.now(timezone.utc)
    put_health('alex@example.test', now - timedelta(seconds=30))
    page = b.get('/suggestions').text
    assert 'data-wa-health' not in page and 'status is unavailable' not in page
    put_health('alex@example.test', now - timedelta(minutes=10))
    assert 'data-wa-health="unknown"' in b.get('/suggestions').text
    put_health('alex@example.test', now - timedelta(seconds=30), state='disconnected')
    assert 'data-wa-health="disconnected"' in b.get('/suggestions').text
    put_health('alex@example.test', now - timedelta(seconds=30), pending=4, pending_age_s=120)
    assert 'data-wa-health="delayed"' in b.get('/suggestions').text


def test_connections_shows_heartbeat_detail_and_unlinked_suggestions_warn(make_browser, browser):
    b, = linked(make_browser, 'alex')
    now = datetime.now(timezone.utc)
    put_health('alex@example.test', now - timedelta(seconds=30), sweep={'outcome': 'partial', 'failed_chats': 1,
                                                                        'last_complete_at': None})
    page = b.get('/connections').text
    assert 'data-wa-health="incomplete"' in page and 'no complete check yet' in page
    browser.login('blake')
    assert 'data-wa-health="not_linked"' in browser.get('/suggestions').text


def test_synthetic_connector_reports_health(browser):
    browser.login('alex')
    assert browser.post('/connections/whatsapp/connect', {'csrf': browser.csrf('/connections')}).status_code == 303
    browser.get('/api/whatsapp/status.json')
    browser.get('/api/whatsapp/status.json')
    c = connector('alex@example.test')
    assert c.health_checked_at is not None and whatsapp_health.classify(c).code == 'ok'
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Source).where(Source.provider == 'whatsapp'))
