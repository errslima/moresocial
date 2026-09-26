"""Sending: tenancy, account binding, CSRF, durable duplicate protection and drafts."""
from html import unescape
import re
import uuid
from urllib.parse import urlsplit, parse_qs

import pytest

from app import db, whatsapp, whatsapp_send, suggestions
from app.models import WhatsAppConnector, WhatsAppSend, Source, Suggestion
from app.repo import Scoped
from test_whatsapp_pages import put, ALEX_WA, NOOR, GROUP, page
from test_suggestions import add, review
from conftest import workspace_of


@pytest.fixture
def ready(browser):
    browser.login('alex')
    wid = workspace_of('alex@example.test')
    put('alex@example.test', NOOR, [('send-source', 60, NOOR, 'Hello')], name='Noor')
    with db.session() as s:
        conn = whatsapp.request_connect(Scoped(s, wid))
        conn.observed, conn.account_wid = 'ready', ALEX_WA
    return wid


def form(browser, **kw):
    return {'csrf': browser.csrf('/whatsapp'), 'account': ALEX_WA, 'chat': NOOR,
            'text': 'Hello from MoreSocial!', 'request_id': str(uuid.uuid4()), **kw}


def test_send_and_replay_once(browser, ready, monkeypatch):
    calls = []
    original = whatsapp_send.fake_send
    def send(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(whatsapp_send, 'fake_send', send)
    data = form(browser)
    response = browser.post('/whatsapp/send', data)
    assert response.status_code == 303
    receipt = browser.c.get(response.headers['location'])
    assert 'Message sent to WhatsApp' in receipt.text and 'Hello from MoreSocial!' in receipt.text
    assert browser.post('/whatsapp/send', data).headers['location'] == response.headers['location']
    assert len(calls) == 1
    assert browser.post('/whatsapp/send', {**data, 'text': 'Changed text'}).status_code == 409
    with db.session() as s:
        ws = Scoped(s, ready)
        assert len(ws.all(ws.q(Source).where(Source.body == data['text']))) == 1
        assert ws.get(WhatsAppSend, data['request_id']).state == 'sent'


def test_uncertain_send_is_not_retried(browser, ready, monkeypatch):
    calls = []
    def timeout(*args):
        calls.append(args)
        raise whatsapp.ConnectorUnavailable()
    monkeypatch.setattr(whatsapp_send, 'fake_send', timeout)
    data = form(browser)
    r = browser.post('/whatsapp/send', data)
    assert r.status_code == 202 and 'could not confirm whether' in r.text and data['text'] in r.text
    assert browser.post('/whatsapp/send', data).status_code == 202
    assert len(calls) == 1


def test_validation_and_account_scoping(browser, ready, monkeypatch):
    monkeypatch.setattr(whatsapp_send, 'fake_send', lambda *a: pytest.fail('must not send'))
    for text in ('', ' ', 'x' * 4001, 'a\0b'):
        assert browser.post('/whatsapp/send', form(browser, text=text)).status_code == 400
    assert browser.post('/whatsapp/send', form(browser, csrf='bad')).status_code == 403
    assert browser.post('/whatsapp/send', form(browser, chat=GROUP)).status_code == 404
    put('alex@example.test', NOOR, [('old-account', 30, NOOR, 'Old account')], account='31600000077@c.us')
    assert browser.post('/whatsapp/send', form(browser, account='31600000077@c.us')).status_code == 409
    with db.session() as s:
        ws = Scoped(s, ready)
        ws.first(ws.q(WhatsAppConnector)).desired = 'stopped'
    r = browser.post('/whatsapp/send', form(browser))
    assert r.status_code == 409 and 'Hello from MoreSocial!' in r.text


def test_other_workspace_cannot_send_or_read_receipt(browser, make_browser, ready):
    r = browser.post('/whatsapp/send', form(browser))
    other = make_browser(); other.login('blake')
    assert other.c.get(r.headers['location']).status_code == 404
    assert other.post('/whatsapp/send', form(other)).status_code == 404


def test_group_send_and_lid_are_supported(browser, ready):
    for chat in (GROUP, '123456789012345@lid'):
        put('alex@example.test', chat, [('context-' + chat, 30, NOOR, 'Hi')], name='Conversation')
        r = browser.post('/whatsapp/send', form(browser, chat=chat))
        assert r.status_code == 303
        assert 'Message sent to WhatsApp' in browser.c.get(r.headers['location']).text


def test_suggestion_opens_evidence_account_with_current_draft(browser, ready):
    add(ready, 'Can you join dinner on Friday?', conv=NOOR)
    row = review(ready)[0]
    suggestions.draft(ready, row.id)
    html = browser.get(f'/suggestions/{row.id}').text
    link = unescape(re.search(r'<a class="button" href="([^"]+)">Open conversation', html).group(1))
    assert parse_qs(urlsplit(link).query)['account'] == ['self']  # evidence account, not currently linked one
    opened = browser.c.get(link)
    assert opened.status_code == 200
    with db.session() as s:
        draft = s.get(Suggestion, row.id).draft
    assert draft in opened.text
    assert 'Send message</button>' not in opened.text
    assert link.replace('&', '&amp;') in browser.get('/suggestions').text
    # New conversation context invalidates the draft: never prefill stale text.
    add(ready, 'Can you let me know soon?', conv=NOOR, age=1)
    assert draft not in browser.c.get(link).text


def test_self_chat_can_start_but_not_arbitrary_new_conversations(browser, ready):
    own = page(browser, account=ALEX_WA, chat=ALEX_WA)
    assert own.status_code == 200 and 'Send message</button>' in own.text
    r = browser.post('/whatsapp/send', form(browser, chat=ALEX_WA))
    assert r.status_code == 303 and 'Message sent to WhatsApp' in browser.c.get(r.headers['location']).text
    assert browser.post('/whatsapp/send', form(browser, chat='31612345999@c.us')).status_code == 404


def test_concurrent_submissions_claim_only_once(browser, ready, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered, release = Event(), Event()
    calls = []
    original = whatsapp_send.fake_send
    def delayed(*args):
        calls.append(args); entered.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(whatsapp_send, 'fake_send', delayed)
    data = form(browser)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(browser.post, '/whatsapp/send', data)
        assert entered.wait(10)
        try:
            second = pool.submit(browser.post, '/whatsapp/send', data).result(10)
            assert second.status_code == 202
        finally:
            release.set()
        assert first.result(10).status_code == 303
    assert len(calls) == 1
