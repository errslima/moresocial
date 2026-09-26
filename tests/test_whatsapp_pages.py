"""Read-only WhatsApp conversation browser: scoping, grouping, search, paging and rendering."""
from html import unescape
import re
import time
from urllib.parse import urlencode

import pytest
from sqlalchemy import select

from app import db, ingest, whatsapp
from app.models import Person, PersonIdentifier, Source
from app.repo import Scoped
from conftest import workspace_of

ALEX_WA = '31600000009@c.us'
NOOR = '31600000001@c.us'
GROUP = '120363000000000001@g.us'


def put(email, chat, messages, name=None, account=ALEX_WA, contacts=()):
    """Ingest through the real connector path; messages are (id, seconds_ago, sender, body[, kind])."""
    now = int(time.time())
    packet = {'chat': {'id': chat, 'name': name, 'is_group': chat.endswith('@g.us')}, 'contacts': list(contacts),
              'messages': [{'id': m[0], 'ts': now - m[1], 'sender': m[2] or account, 'from_me': m[2] is None,
                            'kind': m[4] if len(m) > 4 else 'chat', 'body': m[3]} for m in messages]}
    return whatsapp.ingest(workspace_of(email), packet, account)


def page(browser, **params):
    return browser.get('/whatsapp' + ('?' + urlencode(params) if params else ''))


def reader(response):
    return response.text.split('<section class="wa-reader"', 1)[1]


def names(html):
    return [unescape(n) for n in re.findall(r'<span class="wa-name">([^<]*)', html)]


@pytest.fixture
def alex(browser):
    browser.login('alex')
    return browser


def test_requires_sign_in(browser):
    r = page(browser)
    assert r.status_code == 303 and r.headers['location'] == '/moresocial/'
    assert page(browser, account=ALEX_WA, chat=NOOR).status_code == 303


def test_conversations_grouped_sorted_and_named(alex):
    put('alex@example.test', NOOR, [('n1', 3000, NOOR, 'Old hello'), ('n2', 100, None, 'See you Friday')], name='Noor',
        contacts=[{'id': NOOR, 'saved_name': 'Noor Bakker'}])
    put('alex@example.test', GROUP, [('g1', 500, '31600000002@c.us', 'Climbing?')], name='Friends',
        contacts=[{'id': '31600000002@c.us', 'saved_name': 'Sam'}])
    # The same chat seen through a second linked account stays a separate conversation.
    put('alex@example.test', NOOR, [('n9', 50, NOOR, 'Other phone')], name='Noor', account='31600000077@c.us')
    r = page(alex)
    assert r.status_code == 200 and 'aria-current="page"' in r.text
    assert re.search(r'<a href="/moresocial/whatsapp"\s+aria-current="page"', r.text)
    assert names(r.text) == ['Noor Bakker', 'Noor Bakker', 'Friends']
    assert 'You: See you Friday' in r.text and 'Sam: Climbing?' in r.text and 'Old hello' not in r.text
    assert 'Select a conversation' in r.text and 'Choose a chat to read its imported history.' in r.text
    one = page(alex, account=ALEX_WA, chat=NOOR)
    assert 'Old hello' in reader(one) and 'See you Friday' in reader(one) and 'Other phone' not in reader(one)
    assert one.text.count('aria-current="true"') == 1
    other = page(alex, account='31600000077@c.us', chat=NOOR)
    assert 'Other phone' in reader(other) and 'Old hello' not in reader(other)
    group = page(alex, account=ALEX_WA, chat=GROUP)
    assert '<p class="wa-sender">Sam</p>' in group.text and '>Group<' in group.text


def test_workspace_isolation_with_identical_conversation_ids(make_browser):
    a, b = make_browser(), make_browser()
    a.login('alex')
    b.login('blake')
    put('alex@example.test', NOOR, [('same-id', 60, NOOR, 'Alex private text')], name='Noor', account='whatsapp')
    put('blake@example.test', NOOR, [('same-id', 60, NOOR, 'Blake private text')], name='Nora', account='whatsapp',
        contacts=[{'id': NOOR, 'saved_name': 'Nora Blake-contact'}])
    ra, rb = page(a, account='whatsapp', chat=NOOR), page(b, account='whatsapp', chat=NOOR)
    assert 'Alex private text' in ra.text and 'Blake' not in ra.text
    assert 'Blake private text' in rb.text and 'Alex private text' not in rb.text and 'Nora Blake-contact' in rb.text
    assert names(page(a, q='Nora').text) == [] and 'No conversations match' in page(a, q='Nora').text
    # A source ID from the other workspace is not a valid cursor here.
    with db.session() as s:
        blake_src = s.scalar(select(Source.id).where(Source.workspace_id == workspace_of('blake@example.test')))
    assert page(a, account='whatsapp', chat=NOOR, before=blake_src).status_code == 404


def test_invalid_selection_is_not_found(alex):
    put('alex@example.test', NOOR, [('n1', 60, NOOR, 'Hi')], name='Noor')
    assert page(alex, account=ALEX_WA, chat='31699999999@c.us').status_code == 404
    assert page(alex, account='someone-else', chat=NOOR).status_code == 404
    assert page(alex, chat=NOOR).status_code == 404
    assert page(alex, account=ALEX_WA, chat=NOOR, before='not-a-uuid').status_code == 404
    assert page(alex, account='x' * 300, chat=NOOR).status_code == 404
    assert page(alex, page=0).status_code == 400


def test_search_names_case_insensitive_and_escaped(alex):
    put('alex@example.test', NOOR, [('n1', 60, NOOR, 'secret body words')], name='Noor',
        contacts=[{'id': NOOR, 'saved_name': 'Noor Bakker'}])
    put('alex@example.test', GROUP, [('g1', 30, '31600000002@c.us', 'Hi')], name='Climbing 100% crew')
    put('alex@example.test', '31600000004@c.us', [('m1', 90, '31600000004@c.us', 'Hi')], name='Mila_B')
    assert names(page(alex, q='bakker').text) == ['Noor Bakker']
    assert names(page(alex, q='  CLIMB ').text) == ['Climbing 100% crew']
    assert names(page(alex, q='100%').text) == ['Climbing 100% crew']
    assert names(page(alex, q='%').text) == ['Climbing 100% crew']  # a literal percent sign, not a wildcard
    assert names(page(alex, q='a_b').text) == ['Mila_B']
    assert names(page(alex, q='l_b').text) == []  # underscore is literal
    assert 'No conversations match' in page(alex, q='secret body').text  # search finds conversations, not text
    assert 'Clear search' in page(alex, q='zzz').text
    long = page(alex, q='N' * 5000)
    assert long.status_code == 200 and 'N' * 101 not in long.text
    # Opening a match shows the full history, not just matching messages.
    opened = page(alex, q='bakker', account=ALEX_WA, chat=NOOR)
    assert 'secret body words' in opened.text and 'q=bakker' in opened.text


def test_corrected_contact_name_and_opaque_ids(alex):
    put('alex@example.test', NOOR, [('n1', 60, NOOR, 'Hi')], name=None)
    put('alex@example.test', '123456789012345@lid', [('l1', 30, '123456789012345@lid', 'Hello')], name=None)
    html = page(alex).text
    assert sorted(names(html)) == ['+31600000001', 'Unnamed conversation']  # phone JIDs may show the number; @lid never does
    visible = re.sub(r'href="[^"]*"', '', html)
    assert '123456789012345' not in visible and '>UC<' not in visible
    with db.session() as s:
        wid = workspace_of('alex@example.test')
        ident = s.scalars(select(PersonIdentifier).where(PersonIdentifier.workspace_id == wid,
                                                         PersonIdentifier.value == NOOR)).one()
        person = s.get(Person, ident.person_id)
        person.display_name, person.name_source = 'Noor (corrected)', 'user'
    assert 'Noor (corrected)' in names(page(alex).text)
    assert names(page(alex, q='corrected').text) == ['Noor (corrected)']


def test_deleted_excluded_and_hidden_messages(alex):
    put('alex@example.test', NOOR, [('n1', 300, NOOR, 'Kept'), ('n2', 200, NOOR, 'Revoked soon'),
                                    ('n3', 100, NOOR, 'Not included')], name='Noor')
    put('alex@example.test', NOOR, [('n2', 200, NOOR, '', 'revoked')], name='Noor')
    put('alex@example.test', GROUP, [('g1', 50, '31600000002@c.us', 'Excluded chat text')], name='Friends')
    with db.session() as s:
        wid = workspace_of('alex@example.test')
        ws = Scoped(s, wid)
        ws.update(Source, Source.provider_object_id == 'n3', included=False)
        ingest.exclude(ws, ws.first(ws.q(Source).where(Source.provider_object_id == 'g1')), 'conversation')
    html = page(alex).text
    assert names(html) == ['Noor'] and 'Friends' not in html and 'Kept' in html
    chat = page(alex, account=ALEX_WA, chat=NOOR).text
    assert 'Kept' in chat and 'Revoked soon' not in chat and 'Not included' not in chat
    assert page(alex, account=ALEX_WA, chat=GROUP).status_code == 404


def test_media_placeholders_and_escaped_content(alex):
    put('alex@example.test', NOOR, [('v1', 500, NOOR, '', 'ptt'), ('p1', 400, NOOR, 'Beach day', 'image'),
                                    ('s1', 300, NOOR, '', 'sticker'), ('d1', 250, NOOR, '', 'document'),
                                    ('x1', 200, NOOR, '<script>alert(1)</script>\nsecond line'),
                                    ('u1', 100, NOOR, 'https://example.test/' + 'a' * 300)], name='<b>Noor</b>')
    chat = page(alex, account=ALEX_WA, chat=NOOR).text
    for label in ('Voice message', 'Photo', 'Non-text message', 'Document'):
        assert f'<p class="wa-media">{label}</p>' in chat
    assert '<p class="wa-text">Beach day</p>' in chat
    assert '<script>alert(1)' not in chat and '&lt;script&gt;alert(1)&lt;/script&gt;\nsecond line' in chat
    assert '<b>Noor</b>' not in chat and '&lt;b&gt;Noor&lt;/b&gt;' in chat
    assert chat.count('/moresocial/sources/') == 6
    listing = page(alex).text
    assert 'https://example.test/' in listing  # latest message preview
    put('alex@example.test', NOOR, [('p2', 10, None, 'Look', 'image')], name='Noor')
    assert 'You: Photo: Look' in page(alex).text


def test_conversation_pagination(alex):
    for i in range(55):
        put('alex@example.test', f'316100000{i:02d}@c.us', [(f'm{i}', 1000 - i, f'316100000{i:02d}@c.us', f'msg {i}')],
            name=f'Contact {i:02d}')
    first = page(alex).text
    assert len(names(first)) == 50 and names(first)[0] == 'Contact 54' and names(first)[-1] == 'Contact 05'
    assert 'More conversations' in first and 'Newer conversations' not in first
    second = page(alex, page=2).text
    assert names(second) == ['Contact 04', 'Contact 03', 'Contact 02', 'Contact 01', 'Contact 00']
    assert 'More conversations' not in second and 'Newer conversations' in second
    assert 'No more conversations' in page(alex, page=3).text
    # Selecting from page 2 keeps the list page, and Back returns to it.
    sel = page(alex, page=2, account=ALEX_WA, chat='31610000003@c.us').text
    assert names(sel)[1] == 'Contact 03' and 'href="/moresocial/whatsapp?page=2" class' not in sel
    assert 'class="wa-back" href="/moresocial/whatsapp?page=2"' in sel


def test_equal_timestamps_sort_deterministically(alex):
    put('alex@example.test', NOOR, [('t1', 100, NOOR, 'a')], name='Noor')
    put('alex@example.test', GROUP, [('t2', 100, '31600000002@c.us', 'b')], name='Friends')
    orders = {tuple(names(page(alex).text)) for _ in range(3)}
    assert len(orders) == 1


def test_history_pagination_with_cursor(alex):
    put('alex@example.test', NOOR, [(f'h{i:03d}', 10000 - i * 10, NOOR, f'message number {i:03d}') for i in range(130)],
        name='Noor')
    latest = page(alex, account=ALEX_WA, chat=NOOR).text
    shown = re.findall(r'message number (\d{3})', latest.split('<section class="wa-reader"')[1])
    assert shown == [f'{i:03d}' for i in range(30, 130)]  # newest 100, oldest first
    assert 'Older messages' in latest and 'data-wa-history="latest"' in latest
    before = re.search(r'before=([0-9a-f-]{36})', latest).group(1)
    older = page(alex, account=ALEX_WA, chat=NOOR, before=before).text
    assert re.findall(r'message number (\d{3})', older.split('<section class="wa-reader"')[1]) == [f'{i:03d}' for i in range(30)]
    assert 'Older messages' not in older and 'Jump to latest messages' in older and 'data-wa-history="older"' in older
    # The cursor must belong to the selected conversation.
    put('alex@example.test', GROUP, [('g1', 50, '31600000002@c.us', 'x')], name='Friends')
    assert page(alex, account=ALEX_WA, chat=GROUP, before=before).status_code == 404


def test_empty_states_and_no_live_connector_call(alex, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError('page rendering must not call the connector')
    monkeypatch.setattr(whatsapp, 'call', refuse)
    monkeypatch.setattr(whatsapp.FAKE_CONNECTOR, 'status', refuse)
    html = page(alex).text
    assert 'No WhatsApp conversations yet' in html and '/moresocial/connections' in html
    assert alex.post('/connections/whatsapp/connect', {'csrf': alex.csrf('/connections')}).status_code == 303
    assert 'Imported conversations will appear here' in page(alex).text
    put('alex@example.test', NOOR, [('n1', 60, NOOR, 'Stored message')], name='Noor')
    assert alex.post('/connections/whatsapp/disconnect', {'csrf': alex.csrf('/connections')}).status_code == 303
    html = page(alex).text
    assert 'Noor' in names(html) and 'stored chats stay readable' in html
    assert 'Stored message' in page(alex, account=ALEX_WA, chat=NOOR).text


def test_no_sending_controls(alex):
    put('alex@example.test', NOOR, [('n1', 60, NOOR, 'Hi')], name='Noor')
    html = page(alex, account=ALEX_WA, chat=NOOR).text
    assert '<textarea' not in html and '>Send<' not in html
    assert re.findall(r'<form method="post"[^>]*>', html) == ['<form method="post" action="/moresocial/api/auth/logout" class="logout">']
    assert alex.post('/whatsapp', {'csrf': alex.csrf('/whatsapp'), 'body': 'x'}).status_code == 405
