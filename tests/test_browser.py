"""M5 browser scenario (Playwright/Chromium) through a /moresocial prefix-stripping proxy,
at desktop and 390px mobile widths. Synthetic Google, WhatsApp and AI only."""
from __future__ import annotations

import dataclasses
import socket
import threading
import time

import pytest

playwright = pytest.importorskip('playwright.sync_api')

from app import config, google, synthetic
from conftest import drain


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def live(database):
    import uvicorn
    from app.prefix import PrefixStrip
    from app.web import create_app
    original = config.get()
    port = free_port()
    config.override(dataclasses.replace(original, public_origin=f'http://localhost:{port}'))
    google.set_provider(None)
    server = uvicorn.Server(uvicorn.Config(PrefixStrip(create_app()), host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f'http://localhost:{port}/moresocial'
    server.should_exit = True
    thread.join(5)
    config.override(original)
    google.set_provider(None)


def nav_to(page, name):
    """Follow a main-navigation link, expanding the compact mobile menu first when it is collapsed."""
    menu = page.locator('details.appnav')
    if menu.locator('summary').is_visible() and not menu.evaluate('d => d.open'):
        menu.locator('summary').click()
    menu.get_by_role('link', name=name, exact=True).click()


@pytest.fixture(scope='module')
def chromium():
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            yield browser
            browser.close()
    except Exception as exc:  # browser binaries missing
        pytest.skip(f'Chromium unavailable: {type(exc).__name__}')


@pytest.mark.parametrize('viewport', [{'width': 1280, 'height': 900}, {'width': 390, 'height': 844}], ids=['desktop', 'mobile'])
def test_full_lasergame_scenario(live, chromium, viewport):
    from worker.main import schedule
    ctx = chromium.new_context(viewport=viewport)
    ctx.grant_permissions(['clipboard-read', 'clipboard-write'], origin=live.rsplit('/moresocial', 1)[0])
    page = ctx.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    synthetic.STATE.calls.clear()

    def no_horizontal_scroll():
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'), page.url

    # Welcome: one Continue with Google, no developer setup anywhere
    page.goto(live + '/')
    assert page.get_by_role('button', name='Continue with Google').is_visible()
    assert 'client ID' not in page.content() and 'Cloud Console' not in page.content()
    no_horizontal_scroll()
    page.keyboard.press('Tab')
    assert page.evaluate('document.activeElement.className') == 'skip'
    page.get_by_role('button', name='Continue with Google').click()
    page.get_by_role('button', name='Allow').click()
    page.wait_for_url(live + '/home')
    assert page.get_by_role('heading', name='Hi Alex Synthetic').is_visible()
    assert page.context.cookies()[0]['path'] in ('/moresocial/', '/moresocial')

    # Link WhatsApp: QR for the owner, then ready (synthetic connector)
    nav_to(page, 'Connections')
    page.get_by_role('button', name='Link WhatsApp').click()
    page.wait_for_selector('img[data-wa-qr]:not([hidden])', timeout=10000)
    playwright.expect(page.locator('[data-wa-state]')).to_contain_text('Linked and syncing', timeout=15000)
    no_horizontal_scroll()

    # Background sync + memory pipeline
    schedule()
    drain()

    # Imported evidence
    page.goto(live + '/sources')
    page.get_by_role('link', name='Gmail · Lasergame?').first.click()
    assert 'I love lasergame' in page.locator('pre.source-text').inner_text()
    no_horizontal_scroll()

    # Fix a contact claim
    page.goto(live + '/people?q=Sam+Jansen')
    page.get_by_role('link', name='Sam Jansen').click()
    claim = page.locator('li.claim', has_text='Amersfoort')
    claim.locator('summary', has_text='Edit').click()
    claim.get_by_label('Corrected memory').fill('Amersfoort, near the station')
    claim.get_by_role('button', name='Save correction').click()
    assert page.locator('li.claim', has_text='Amersfoort, near the station').locator('text=added or confirmed by you').is_visible()
    no_horizontal_scroll()

    # Automatically recognize an event from imported evidence, then inspect/dismiss it.
    from app import events, db
    from app.repo import Scoped
    from conftest import workspace_of
    from test_events import sample, item
    with db.session() as s:
        ws = Scoped(s, workspace_of('alex@example.test'))
        source = sample(ws)
        events.persist(ws, 'browser-event', [source], [item(source)])
    nav_to(page, 'Events')
    assert page.get_by_role('button', name='Create', exact=True).count() == 0
    page.get_by_role('link', name='Dinner at Olive', exact=True).click()
    assert page.get_by_role('heading', name='Messages & context').is_visible()
    assert page.get_by_role('link', name='Open source →').count() == 1
    no_horizontal_scroll()
    page.screenshot(path=f".dev/events-detail-{viewport['width']}.png", full_page=True)
    page.get_by_role('button', name='Dismiss event', exact=True).click()
    page.get_by_role('link', name='Dismissed 1').click()
    assert page.get_by_role('link', name='Dinner at Olive', exact=True).is_visible()
    no_horizontal_scroll()
    page.screenshot(path=f".dev/events-overview-{viewport['width']}.png", full_page=True)

    # Ask a cited question
    nav_to(page, 'Ask')
    page.get_by_label('Question').fill('Does Sam like lasergame?')
    page.get_by_role('button', name='Ask', exact=True).click()
    page.wait_for_url(live + '/answers/*')
    assert 'Not enough evidence' not in page.content()
    first = page.locator('ul.cites details').first
    first.locator('summary').click()
    assert first.locator('blockquote.excerpt').is_visible()
    no_horizontal_scroll()

    # Mobile navigation keeps the prefix
    for href in page.locator('nav a').evaluate_all('els => els.map(e => e.getAttribute("href"))'):
        assert href.startswith('/moresocial/')

    # Nothing was ever written to a provider; the synthetic Google only saw reads and OAuth
    assert all(m == 'GET' or 'oauth2.googleapis.com' in h for m, h in synthetic.STATE.calls)
    assert not [e for e in errors if 'favicon' not in e], errors
    ctx.close()


@pytest.mark.parametrize('viewport', [{'width': 1280, 'height': 900}, {'width': 390, 'height': 844}], ids=['desktop', 'mobile'])
def test_connections_ai_status(live, chromium, viewport):
    from app import ai
    ctx = chromium.new_context(viewport=viewport)
    try:
        page = ctx.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        sign_in(page, live)
        nav_to(page, 'Connections')
        section = page.locator('section', has_text='AI assistant')
        playwright.expect(section).to_contain_text('shared OpenRouter account')
        playwright.expect(section).to_contain_text('Spending limits are managed in OpenRouter')
        assert page.locator('input[type=password]').count() == 0
        assert page.locator('form[action*="/connections/ai/"]').count() == 0
        assert 'API key' not in page.content()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')

        ai.set_provider(ai.OperatorTier(None, None))  # AI disabled by the operator
        page.reload()
        section = page.locator('section', has_text='AI assistant')
        playwright.expect(section).to_contain_text('The assistant is temporarily unavailable')
        playwright.expect(section).to_contain_text('An administrator can configure it in Admin')
        playwright.expect(section).not_to_contain_text('shared OpenRouter account')
        assert errors == []
    finally:
        ai.set_provider(None)
        ctx.close()


@pytest.mark.parametrize('viewport', [{'width': 1280, 'height': 900}, {'width': 390, 'height': 844}], ids=['desktop', 'mobile'])
def test_admin_openrouter_failure_keeps_choices_and_shows_reference(live, chromium, viewport):
    import httpx
    from app import admin
    from test_admin import ALIAS, CANONICAL, KEY, FakeOpenRouter
    config.override(dataclasses.replace(config.get(), admin_emails=frozenset({'alex@example.test'})))
    admin.set_transport(httpx.MockTransport(FakeOpenRouter(embedding_models=('openai/text-embedding-3-small', 'other/model'))))
    try:
        page = chromium.new_context(viewport=viewport).new_page()
        page.goto(live + '/')
        page.get_by_role('button', name='Continue with Google').click()
        page.get_by_role('button', name='Allow').click()
        page.wait_for_url(live + '/home')
        nav_to(page, 'Admin')
        page.get_by_label('OpenRouter API key').fill(KEY)
        page.get_by_label('Embedding model').select_option('openai/text-embedding-3-small')
        page.get_by_role('button', name='Test and save').click()
        alert = page.get_by_role('alert')
        playwright.expect(alert).to_contain_text('query embedding test (embedding_model)')
        playwright.expect(alert).to_contain_text('Reference:')
        assert page.get_by_label('Embedding model').input_value() == 'openai/text-embedding-3-small'
        assert page.get_by_label('OpenRouter API key').input_value() == ''
        assert KEY not in page.content()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
        admin.set_transport(httpx.MockTransport(FakeOpenRouter(embedding_models=(CANONICAL, CANONICAL))))
        page.get_by_label('OpenRouter API key').fill(KEY)
        page.get_by_label('Embedding model').select_option(ALIAS)
        page.get_by_role('button', name='Test and save').click()
        assert 'Global AI settings saved' in page.content()
        playwright.expect(page.locator('section', has_text='Current operation')).to_contain_text(CANONICAL)
    finally:
        admin.set_transport(None)


@pytest.mark.parametrize('viewport', [{'width': 1280, 'height': 900}, {'width': 390, 'height': 844}], ids=['desktop', 'mobile'])
def test_diary_flow(live, chromium, viewport):
    ctx = chromium.new_context(viewport=viewport)
    page = ctx.new_page()
    page.goto(live + '/')
    page.get_by_role('button', name='Continue with Google').click()
    page.get_by_role('button', name='Allow').click()
    page.wait_for_url(live + '/home')
    page.goto(live + '/me')
    assert page.locator('textarea:visible').count() == 1
    page.get_by_label('What’s on your mind?').fill('I enjoy sketching mountain landscapes.')
    page.get_by_role('button', name='Add diary entry').click()
    playwright.expect(page.get_by_role('status')).to_contain_text('Diary entry saved')
    assert page.get_by_label('What’s on your mind?').input_value() == ''
    drain()
    page.reload()
    playwright.expect(page.locator('article.card')).to_contain_text('Searchable · observations processed')
    page.locator('article.card summary').click()
    page.locator('article.card textarea').fill('Tonight I want a quiet evening.')
    page.get_by_role('button', name='Save changes').click()
    drain()
    page.reload()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
    page.screenshot(path=f'/tmp/moresocial-diary-{viewport["width"]}.png', full_page=True)
    page.get_by_role('button', name='Delete entry').click()
    assert page.locator('article.card').count() == 0
    ctx.close()


@pytest.mark.parametrize('viewport', [{'width': 1280, 'height': 900}, {'width': 390, 'height': 844}], ids=['desktop', 'mobile'])
def test_suggestions_browser(live, chromium, viewport):
    from conftest import workspace_of
    from test_suggestions import add, review
    from app import db
    from app.models import Suggestion
    ctx = chromium.new_context(viewport=viewport)
    ctx.grant_permissions(['clipboard-read', 'clipboard-write'], origin=live.rsplit('/moresocial', 1)[0])
    page = ctx.new_page()
    page.goto(live + '/')
    page.get_by_role('button', name='Continue with Google').click()
    page.get_by_role('button', name='Allow').click()
    page.wait_for_url(live + '/home')
    wid = workspace_of('alex@example.test')
    add(wid, 'Can you join dinner on Friday?')
    row = review(wid)[0]
    nav_to(page, 'Suggestions')
    page.get_by_text('Why this appeared', exact=True).click()
    assert page.get_by_text('Can you join dinner on Friday?', exact=True).is_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
    page.screenshot(path=f'/tmp/moresocial-suggestions-{viewport["width"]}.png', full_page=True)
    page.get_by_role('button', name='Draft a reply').click()
    page.wait_for_url(live + f'/suggestions/{row.id}')
    page.get_by_label('Edit your message').fill('Yes, Friday works!')
    page.get_by_role('button', name='Save edits').click()
    page.get_by_role('button', name='Copy message').click()
    playwright.expect(page.locator('[data-copy-status]')).to_contain_text('Copied the text shown above')
    assert page.evaluate('navigator.clipboard.readText()') == 'Yes, Friday works!'
    with db.session() as s:
        assert s.get(Suggestion, row.id).state == 'active'
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
    page.screenshot(path=f'/tmp/moresocial-suggestion-draft-{viewport["width"]}.png', full_page=True)
    page.get_by_role('button', name='No reply needed').click()
    page.get_by_role('link', name='History', exact=True).click()
    assert page.get_by_role('link', name='Reply about dinner').is_visible()
    ctx.close()


def sign_in(page, live):
    page.goto(live + '/')
    page.get_by_role('button', name='Continue with Google').click()
    page.get_by_role('button', name='Allow').click()
    page.wait_for_url(live + '/home')


def seed_whatsapp():
    from test_whatsapp_pages import ALEX_WA, GROUP, NOOR, put
    put('alex@example.test', NOOR, [(f'n{i:03d}', 20000 - i * 60, NOOR if i % 3 else None,
                                     f'Noor message {i:03d} ' + ('with a very long unbroken link https://example.test/' + 'x' * 200 if i == 129 else ''))
                                    for i in range(130)], name='Noor', contacts=[{'id': NOOR, 'saved_name': 'Noor Bakker'}])
    put('alex@example.test', GROUP, [('g1', 30000, '31600000002@c.us', 'Climbing on Saturday?'),
                                     ('g2', 29000, '31600000003@c.us', '', 'ptt')], name='Friends',
        contacts=[{'id': '31600000002@c.us', 'saved_name': 'Sam'}, {'id': '31600000003@c.us', 'saved_name': 'Mila'}])
    return ALEX_WA, NOOR, GROUP


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 900}, {'width': 390, 'height': 844}], ids=['desktop', 'mobile'])
def test_sidebar_and_whatsapp_browser(live, chromium, viewport):
    mobile = viewport['width'] < 900
    config.override(dataclasses.replace(config.get(), admin_emails=frozenset({'blake@example.test'})))
    ctx = chromium.new_context(viewport=viewport)
    page = ctx.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)

    def no_horizontal_scroll():
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'), page.url

    sign_in(page, live)
    menu = page.locator('details.appnav')
    main_nav = page.get_by_role('navigation', name='Main')
    labels = main_nav.get_by_role('link').all_inner_texts() if not mobile else None
    if mobile:
        # Compact header: links collapsed behind one native <details> menu (no duplicate navigation).
        assert page.get_by_role('link', name='WhatsApp', exact=True).is_hidden()
        assert page.locator('nav[aria-label="Main"]').count() == 1
        menu.locator('summary').click()
        labels = main_nav.get_by_role('link').all_inner_texts()
    else:
        box = page.locator('.sidebar').bounding_box()
        assert 230 <= box['width'] <= 250 and box['height'] >= 890
        assert menu.locator('summary').is_hidden()
        logout = page.get_by_role('button', name='Sign out').bounding_box()
        assert logout['y'] > 780  # anchored at the bottom of the sidebar
    assert [l.strip() for l in labels] == ['Home', 'WhatsApp', 'Suggestions', 'Assistant', 'People', 'Ask', 'Events', 'About me',
                                         'Connections']
    assert page.get_by_role('link', name='Admin').count() == 0  # not an administrator
    assert page.get_by_role('link', name='Home', exact=True).get_attribute('aria-current') == 'page'
    home_main = page.locator('main').bounding_box()
    assert home_main['width'] <= 1121
    page.screenshot(path=f'/tmp/moresocial-home-{viewport["width"]}.png', full_page=True)
    no_horizontal_scroll()

    account, noor, group = seed_whatsapp()
    nav_to(page, 'WhatsApp')
    page.wait_for_url(live + '/whatsapp')
    assert page.title() == 'WhatsApp · Moresocial'
    assert page.locator('a[aria-current=page]').inner_text().strip() == 'WhatsApp'
    playwright.expect(page.locator('.wa-conv')).to_have_count(2)
    conv_names = lambda: [t.split('\n')[0] for t in page.locator('.wa-conv .wa-name').all_inner_texts()]
    assert conv_names() == ['Noor Bakker', 'Friends']
    if mobile:
        assert page.locator('.wa-reader').is_hidden() and page.locator('.wa-list').is_visible()
    else:
        assert page.get_by_role('heading', name='Select a conversation').is_visible()
        assert 370 <= page.locator('.wa-list').bounding_box()['width'] <= 400
    page.screenshot(path=f'/tmp/moresocial-whatsapp-{viewport["width"]}.png')
    no_horizontal_scroll()

    # Search is a GET form: its state lives in the URL.
    page.get_by_label('Search conversations').fill('friends')
    page.get_by_label('Search conversations').press('Enter')
    page.wait_for_url(live + '/whatsapp?q=friends')
    assert conv_names() == ['Friends']
    page.locator('.wa-conv', has_text='Friends').click()
    page.wait_for_url('**/whatsapp?q=friends&account=*')
    reader = page.locator('.wa-reader')
    playwright.expect(reader.locator('.wa-sender').first).to_have_text('Sam')
    playwright.expect(reader.locator('.wa-media')).to_have_text('Voice message')
    if mobile:
        assert page.locator('.wa-list').is_hidden()
        reader.get_by_role('link', name='Back to conversations').click()
        page.wait_for_url(live + '/whatsapp?q=friends')  # search/list state preserved
        assert page.get_by_label('Search conversations').input_value() == 'friends'
    page.get_by_role('link', name='Clear search').first.click()
    page.wait_for_url(live + '/whatsapp')

    # Open a long conversation: newest message in view, older history on request.
    page.locator('.wa-conv', has_text='Noor Bakker').click()
    page.wait_for_url('**/whatsapp?account=*')
    chat_url = page.url
    assert '%40c.us' in chat_url  # encoded query values
    history = page.locator('.wa-history')
    playwright.expect(history.locator('.wa-msg')).to_have_count(100)
    assert history.locator('.wa-msg.out').count() > 0 and history.locator('.wa-msg.in').count() > 0
    at_bottom = 'e => e.scrollTop + e.clientHeight >= e.scrollHeight - 4'
    playwright.expect(history.locator('.wa-msg').last).to_contain_text('Noor message 129')
    page.wait_for_function(f'(e => {at_bottom[5:]})(document.querySelector(".wa-history"))')
    assert page.locator('.wa-list').is_hidden() == mobile
    assert page.get_by_role('link', name='Back to conversations').is_visible() == mobile
    # Nothing to send with: read-only history.
    assert page.locator('textarea').count() == 0 and page.get_by_role('button', name='Send').count() == 0
    page.screenshot(path=f'/tmp/moresocial-whatsapp-chat-{viewport["width"]}.png')
    no_horizontal_scroll()
    assert page.evaluate('document.documentElement.scrollHeight <= innerHeight + 1')

    page.get_by_role('link', name='Older messages').click()
    page.wait_for_url('**&before=*')
    playwright.expect(history.locator('.wa-msg')).to_have_count(30)
    assert history.evaluate('e => e.scrollTop') == 0  # older history does not jump to the bottom
    assert page.evaluate('document.documentElement.scrollHeight <= innerHeight + 1')
    playwright.expect(history.locator('.wa-msg').first).to_contain_text('Noor message 000')
    page.reload()
    playwright.expect(history.locator('.wa-msg')).to_have_count(30)
    jump = page.get_by_role('link', name='Jump to latest messages')
    # The floating link must be reachable without first scrolling the older page.
    playwright.expect(jump).to_be_in_viewport()
    jump.click()
    page.wait_for_url(chat_url)
    playwright.expect(history.locator('.wa-msg')).to_have_count(100)

    # A direct link opens the same conversation in a fresh tab.
    other = ctx.new_page()
    other.on('pageerror', lambda e: errors.append(str(e)))
    other.goto(chat_url)
    playwright.expect(other.locator('.wa-history .wa-msg')).to_have_count(100)
    assert other.locator('.wa-head h2').inner_text() == 'Noor Bakker'
    other.close()

    # Message details still lead to the existing source page.
    history.locator('.wa-msg').last.get_by_role('link', name='Details of this message').click()
    page.wait_for_url(live + '/sources/*')
    assert 'Noor message 129' in page.locator('pre.source-text').inner_text()
    page.go_back()
    page.wait_for_url(chat_url)

    # Sign out keeps the existing POST form with its CSRF token.
    if mobile:
        menu.locator('summary').click()
    page.get_by_role('button', name='Sign out').click()
    page.wait_for_url(live + '/?notice=signed-out')
    assert page.locator('details.appnav').count() == 0 and page.get_by_role('button', name='Continue with Google').is_visible()
    assert not [e for e in errors if 'favicon' not in e], errors
    ctx.close()


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 900}, {'width': 390, 'height': 844}], ids=['desktop', 'mobile'])
def test_admin_link_in_sidebar(live, chromium, viewport):
    config.override(dataclasses.replace(config.get(), admin_emails=frozenset({'alex@example.test'})))
    page = chromium.new_context(viewport=viewport).new_page()
    sign_in(page, live)
    nav_to(page, 'Admin')
    page.wait_for_url(live + '/admin')
    assert page.locator('a[aria-current=page]').inner_text().strip() == 'Admin'
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_visual_review_screens(live, chromium, scheme):
    """Screenshots for visual review at the reference sizes, in both colour schemes."""
    for viewport in ({'width': 1440, 'height': 900}, {'width': 390, 'height': 844}):
        ctx = chromium.new_context(viewport=viewport, color_scheme=scheme)
        page = ctx.new_page()
        sign_in(page, live)
        if viewport['width'] == 1440:
            account, noor, group = seed_whatsapp()
        w = viewport['width']
        page.screenshot(path=f'/tmp/moresocial-review-{scheme}-{w}-home.png')
        page.goto(live + '/whatsapp')
        page.screenshot(path=f'/tmp/moresocial-review-{scheme}-{w}-whatsapp.png')
        page.locator('.wa-conv').first.click()
        page.wait_for_url('**/whatsapp?account=*')
        page.screenshot(path=f'/tmp/moresocial-review-{scheme}-{w}-chat.png')
        if w == 390:
            page.locator('details.appnav summary').click()
            page.screenshot(path=f'/tmp/moresocial-review-{scheme}-{w}-menu.png')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
        ctx.close()


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 900}, {'width': 390, 'height': 844}], ids=['desktop', 'mobile'])
def test_navigation_without_javascript(live, chromium, viewport):
    page = chromium.new_context(viewport=viewport, java_script_enabled=False).new_page()
    sign_in(page, live)
    link = page.get_by_role('navigation', name='Main').get_by_role('link', name='WhatsApp')
    if viewport['width'] < 900:
        assert link.is_hidden()
        page.locator('details.appnav summary').click()
    assert link.is_visible()
    link.click()
    page.wait_for_url(live + '/whatsapp')
    assert page.get_by_role('heading', name='WhatsApp', exact=True).is_visible()


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 900}, {'width': 390, 'height': 844}], ids=['desktop', 'mobile'])
def test_long_chat_list_and_main_page_bounds(live, chromium, viewport):
    from conftest import workspace_of
    from test_whatsapp_pages import put
    from app import db
    from app.models import Person, PersonIdentifier
    from sqlalchemy import select
    config.override(dataclasses.replace(config.get(), admin_emails=frozenset({'alex@example.test'})))
    context = chromium.new_context(viewport=viewport)
    page = context.new_page()
    sign_in(page, live)
    for i in range(25):
        put('alex@example.test', f'12036300000000{i:04d}@g.us',
            [(f'g{i}', 60 + i, '31600000002@c.us', 'Group update')], name=f'Group {i}')
    with db.session() as s:
        person = s.scalar(select(Person))
        s.add(PersonIdentifier(workspace_id=workspace_of('alex@example.test'), person_id=person.id,
                               kind='email', value='long-address-' + 'x' * 200 + '@example.test'))
    page.goto(live + '/whatsapp')
    assert page.locator('.wa-conv').count() == 25
    assert page.evaluate('document.documentElement.scrollHeight <= innerHeight + 1')
    pane = page.locator('.wa-scroll')
    assert pane.evaluate('e => e.scrollHeight > e.clientHeight')
    pane.evaluate('e => e.scrollTop = e.scrollHeight')
    assert page.locator('.wa-conv').last.is_visible()
    assert page.evaluate('document.documentElement.scrollHeight <= innerHeight + 1')
    for route in ('home', 'suggestions', 'people', 'ask', 'events', 'me', 'connections', 'admin', 'sources'):
        response = page.goto(live + '/' + route)
        assert response.status == 200
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), route
        main = page.locator('main').bounding_box()
        if viewport['width'] > 900:
            sidebar = page.locator('.sidebar').bounding_box()
            assert main['x'] == sidebar['x'] + sidebar['width'], route
            assert main['width'] == min(1120, viewport['width'] - sidebar['width']), route
    context.close()


@pytest.mark.parametrize('viewport', [{'width':1440,'height':900}, {'width':390,'height':844}], ids=['desktop','mobile'])
def test_suggestion_to_conversation_and_send(live, chromium, viewport):
    from app import db, whatsapp, suggestions
    from app.models import Source
    from app.repo import Scoped
    from conftest import workspace_of
    from test_suggestions import add, review, CHAT
    account = '31600000009@c.us'
    ctx = chromium.new_context(viewport=viewport)
    page = ctx.new_page()
    page.goto(live + '/')
    page.get_by_role('button', name='Continue with Google').click()
    page.get_by_role('button', name='Allow').click()
    page.wait_for_url(live + '/home')
    wid = workspace_of('alex@example.test')
    src = add(wid, 'Can you join dinner on Friday?')
    with db.session() as s:
        s.get(Source, src).provider_account = account
        conn = whatsapp.request_connect(Scoped(s, wid))
        conn.observed, conn.account_wid = 'ready', account
    row = review(wid)[0]
    suggestions.draft(wid, row.id)
    page.goto(live + '/suggestions')
    page.get_by_role('link', name='Open conversation', exact=True).click()
    page.wait_for_url('**/whatsapp?account=*')
    assert page.locator('input[name=chat]').input_value() == CHAT
    assert page.locator('#wa-message').input_value().strip()
    composer = page.locator('#wa-message')
    composer.fill('Browser send test — hello!')
    initial_height = composer.bounding_box()['height']
    send_requests = []
    page.on('request', lambda r: send_requests.append(r) if r.method == 'POST' and r.url.endswith('/whatsapp/send') else None)
    composer.press('End')
    composer.press('Enter')
    composer.press('Enter')
    assert composer.input_value() == 'Browser send test — hello!\n\n'
    assert composer.bounding_box()['height'] > initial_height
    assert not send_requests
    composer.fill('Browser send test — hello!')
    page.get_by_role('button', name='Send message', exact=True).click()
    page.wait_for_url('**&receipt=*')
    playwright.expect(page.get_by_role('status')).to_contain_text('Message sent to WhatsApp')
    playwright.expect(page.locator('.wa-msg.out').last).to_contain_text('Browser send test — hello!')
    assert page.locator('#wa-message').input_value() == ''
    page.reload()
    assert page.locator('.wa-msg.out', has_text='Browser send test — hello!').count() == 1
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    assert page.evaluate('document.documentElement.scrollHeight <= innerHeight + 1')
    page.screenshot(path=f'/tmp/moresocial-send-{viewport["width"]}.png')
    ctx.close()


@pytest.mark.parametrize('viewport', [{'width':1440,'height':900}, {'width':390,'height':844}], ids=['desktop','mobile'])
def test_automatic_events_page(live, chromium, viewport):
    from app import events, db
    from app.repo import Scoped
    from conftest import workspace_of
    from test_events import sample, item
    context = chromium.new_context(viewport=viewport)
    page = context.new_page()
    sign_in(page, live)
    with db.session() as s:
        ws = Scoped(s, workspace_of('alex@example.test'))
        source = sample(ws)
        events.persist(ws, 'browser-event', [source], [item(source)])
    nav_to(page, 'Events')
    assert page.get_by_role('button', name='Create', exact=True).count() == 0
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    page.screenshot(path=f".dev/events-overview-{viewport['width']}.png", full_page=True)
    page.get_by_role('link', name='Dinner at Olive', exact=True).click()
    assert page.get_by_role('heading', name='Messages & context').is_visible()
    assert page.get_by_role('link', name='Open source →').count() == 1
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    page.screenshot(path=f".dev/events-detail-{viewport['width']}.png", full_page=True)
    page.get_by_role('button', name='Dismiss event', exact=True).click()
    page.get_by_role('link', name='Dismissed 1').click()
    page.get_by_role('link', name='Dinner at Olive', exact=True).click()
    page.get_by_role('button', name='Restore event').click()
    assert page.get_by_role('heading', name='Dinner at Olive').is_visible()
    response = page.goto(live + '/gatherings')
    assert response.status == 200 and page.url.endswith('/events')
    context.close()


@pytest.mark.parametrize('viewport', [{'width':1440,'height':900}, {'width':390,'height':844}], ids=['desktop','mobile'])
def test_event_identity_review_group_split_undo(live, chromium, viewport, monkeypatch):
    from app import db, events, event_resolver
    from app.repo import Scoped
    from conftest import workspace_of
    from test_event_identity import NOW, outing
    monkeypatch.setattr(events, 'now', lambda: NOW)
    context = chromium.new_context(viewport=viewport)
    page = context.new_page()
    sign_in(page, live)
    wid = workspace_of('alex@example.test')
    with db.session() as s:
        outing(Scoped(s, wid))
    event_resolver.run(wid)  # observe mode: suggestions only
    no_overflow = lambda: page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')  # noqa: E731
    nav_to(page, 'Events')
    review = page.get_by_role('region', name='Possible duplicates')
    playwright.expect(review).to_be_visible()
    assert no_overflow()
    page.screenshot(path=f".dev/identity-review-{viewport['width']}.png", full_page=True)
    pair = review.locator('article').filter(has_text='Sunday reservation with Lumen').filter(has_text='Lasergame')
    playwright.expect(pair).to_contain_text('Both mention the same payment request')
    pair.get_by_role('button', name='Same event').click()
    playwright.expect(page.get_by_role('status')).to_contain_text('Grouped as one event')
    playwright.expect(page.get_by_role('heading', name='Is this one event?')).to_be_visible()
    assert page.locator('.event-records li').count() == 3
    assert no_overflow()
    page.screenshot(path=f".dev/identity-group-{viewport['width']}.png", full_page=True)
    # Separate the reservation again, after seeing which messages move.
    page.locator('.event-records li').filter(has_text='Sunday reservation with Lumen').get_by_role('link', name='Separate…').click()
    playwright.expect(page.get_by_role('heading', name='Becomes its own event')).to_be_visible()
    assert no_overflow()
    page.get_by_role('button', name='Separate', exact=True).click()
    playwright.expect(page.get_by_role('status')).to_contain_text('Separated')
    assert page.locator('.event-records li').count() == 2
    # Undo the keep-separate correction from the grouping history: the preview shows the result.
    page.locator('summary', has_text='Grouping history').click()
    page.get_by_role('link', name='Undo…').first.click()
    playwright.expect(page.get_by_role('heading', name='Undo this correction?')).to_be_visible()
    assert no_overflow()
    page.get_by_role('button', name='Undo correction').click()
    playwright.expect(page.get_by_role('status')).to_contain_text('Correction undone')
    # Keep separate from the review list, then dismiss and restore a group.
    nav_to(page, 'Events')
    review = page.get_by_role('region', name='Possible duplicates')
    if review.count():
        review.locator('article').first.get_by_role('button', name='Keep separate').click()
        playwright.expect(page.get_by_role('status')).to_contain_text('Kept as separate events')
    page.get_by_role('link', name='Lasergame – 2 rounds', exact=True).click()
    page.get_by_role('button', name='Dismiss event', exact=True).click()
    page.get_by_role('link', name='Dismissed 1').click()
    page.get_by_role('link', name='Lasergame – 2 rounds', exact=True).click()
    page.get_by_role('button', name='Restore event').click()
    playwright.expect(page.get_by_role('heading', name='Lasergame – 2 rounds')).to_be_visible()
    context.close()


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 900}, {'width': 390, 'height': 844}], ids=['desktop', 'mobile'])
def test_assistant_settings_prepared_draft_pause_and_composer(live, chromium, viewport, monkeypatch):
    """Settings, an event-linked prepared card, editing it, pausing an event and handing the
    draft to the composer. Nothing is sent: the send boundaries are spied on throughout."""
    import os
    import tempfile
    from datetime import timedelta, timezone
    from app import db, proactive, whatsapp, whatsapp_send
    from app.repo import Scoped
    from assistant_fixtures import aloha, at, link_connector
    from conftest import workspace_of
    from worker.main import drain as run_jobs
    shots = os.environ.get('MORESOCIAL_SCREENSHOTS', tempfile.gettempdir())
    sent = []
    monkeypatch.setattr(whatsapp_send, 'fake_send', lambda *a, **kw: sent.append(a) or {'state': 'rejected'})
    real_call = whatsapp.call
    monkeypatch.setattr(whatsapp, 'call', lambda row, m, path, *a, **kw: sent.append(path) if path == '/send' else real_call(row, m, path, *a, **kw))
    ctx = chromium.new_context(viewport=viewport)
    page = ctx.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))

    def fits():
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'), page.url

    sign_in(page, live)
    wid = workspace_of('alex@example.test')
    with db.session() as s:
        ws = Scoped(s, wid)
        link_connector(ws)
        ids = aloha(ws)
    evening = (at(ids['sunday'], 16, 15) - timedelta(hours=20)).astimezone(timezone.utc)
    monkeypatch.setattr(proactive, 'utcnow', lambda: evening)

    nav_to(page, 'Assistant')
    playwright.expect(page.locator('[data-send-rule]')).to_contain_text('messages are sent only when you press Send message')
    assert page.get_by_label('Match how I write to this person').is_checked()
    assert not page.get_by_label('Warmth').is_visible()  # tone controls are secondary in the default mode
    page.get_by_label('Use my custom tone settings').check()
    page.locator('[data-custom-tone] summary').click()
    page.get_by_label('Warmth').select_option('warm')
    page.get_by_label('Emoji').select_option('often')
    page.get_by_role('button', name='Save settings').click()
    playwright.expect(page.locator('.notice')).to_contain_text('Assistant settings saved')
    assert page.get_by_label('Warmth').input_value() == 'warm'
    fits()
    page.screenshot(path=f'{shots}/moresocial-assistant-{viewport["width"]}.png', full_page=True)

    proactive.scan(wid, {'trigger': 'scheduled'})
    run_jobs(kinds=['prepare_message'])
    page.goto(live + '/suggestions')
    card = page.locator('article.suggestion-card', has_text='Event reminder')
    playwright.expect(card.locator('[data-event-link]')).to_contain_text('Lasergame – 2 rondes')
    playwright.expect(card.locator('[data-prepared-draft]')).to_contain_text('Aloha Amsterdam')
    fits()
    page.screenshot(path=f'{shots}/moresocial-prepared-card-{viewport["width"]}.png', full_page=True)

    card.get_by_role('link', name='View context & draft').click()
    playwright.expect(page.locator('[data-rationale]')).to_contain_text('confirmed Lasergame – 2 rondes')
    page.get_by_label('Edit your message').fill('Hey Mickaël, see you at Aloha at 16:15 on Sunday!')
    page.get_by_role('button', name='Save edits').click()
    playwright.expect(page.get_by_role('button', name='Copy message')).to_be_visible()
    fits()
    page.screenshot(path=f'{shots}/moresocial-prepared-draft-{viewport["width"]}.png', full_page=True)

    page.get_by_role('link', name='Open conversation', exact=True).click()
    page.wait_for_url('**/whatsapp?account=*')
    assert page.locator('#wa-message').input_value() == 'Hey Mickaël, see you at Aloha at 16:15 on Sunday!'
    playwright.expect(page.get_by_role('button', name='Send message', exact=True)).to_be_visible()
    fits()

    rep = ids['lasergame_chat']
    page.goto(live + f'/events/{rep}')
    playwright.expect(page.locator('[data-prep-status]')).to_contain_text('Draft prepared')
    page.get_by_role('button', name='Pause message preparation').click()
    playwright.expect(page.locator('[data-prep-state]')).to_contain_text('Paused for this event')
    fits()
    page.screenshot(path=f'{shots}/moresocial-event-prep-{viewport["width"]}.png', full_page=True)
    assert sent == [] and errors == []
    ctx.close()


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 900}, {'width': 390, 'height': 844}], ids=['desktop', 'mobile'])
def test_evidence_grounded_draft_panel_rewrite_and_composer(live, chromium, viewport, monkeypatch):
    """Draft from another chat's evidence, the private "What this draft uses" panel, an edit that a
    rewrite never overwrites, and the composer handoff. Send boundaries are spied on: nothing is sent."""
    import os
    import tempfile
    from app import db, draft_evidence, whatsapp, whatsapp_send
    from app.repo import Scoped
    from assistant_fixtures import link_connector
    from conftest import workspace_of
    from draft_fixtures import JONAS, housing, reply_action
    from worker.main import drain as run_jobs
    shots = os.environ.get('MORESOCIAL_SCREENSHOTS', tempfile.gettempdir())
    sent = []
    monkeypatch.setattr(whatsapp_send, 'fake_send', lambda *a, **kw: sent.append(a) or {'state': 'rejected'})
    real_call = whatsapp.call
    monkeypatch.setattr(whatsapp, 'call', lambda row, m, path, *a, **kw: sent.append(path) if path == '/send' else real_call(row, m, path, *a, **kw))
    ctx = chromium.new_context(viewport=viewport)
    page = ctx.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))

    def fits():
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'), page.url

    sign_in(page, live)
    wid = workspace_of('alex@example.test')
    with db.session() as s:
        ws = Scoped(s, wid)
        link_connector(ws)
        ids = housing(ws)
    run_jobs()
    with db.session() as s:
        sid = reply_action(Scoped(s, wid), JONAS, ids['question'])
        draft_evidence.set_mode(s, 'all', 'test')
    page.goto(live + f'/suggestions/{sid}')
    playwright.expect(page.get_by_text('Drafting checks your other WhatsApp chats and email')).to_be_visible()
    page.get_by_role('button', name='Draft a reply').click()
    page.wait_for_url(live + f'/suggestions/{sid}')
    draft = page.get_by_label('Edit your message')
    assert "Iris's place until 15 October" in draft.input_value() and 'student room' not in draft.input_value()
    panel = page.locator('[data-draft-uses]')
    playwright.expect(panel).to_contain_text('Answered')
    playwright.expect(panel).to_contain_text('WhatsApp chat with Iris')
    playwright.expect(panel).to_contain_text('examples of how you write to Jonas')
    playwright.expect(page.get_by_role('button', name='Copy message')).to_be_visible()
    fits()
    page.screenshot(path=f'{shots}/moresocial-evidence-draft-{viewport["width"]}.png', full_page=True)

    draft.fill('Hoi Jonas! I am at Iris for now, more soon. [how you are]')
    page.get_by_role('button', name='Save edits').click()
    page.get_by_role('button', name='Rewrite with current information').click()
    playwright.expect(page.locator('.notice')).to_contain_text('Your edited text is unchanged')
    assert page.get_by_label('Edit your message').input_value() == 'Hoi Jonas! I am at Iris for now, more soon. [how you are]'
    playwright.expect(page.locator('[data-proposal]')).to_contain_text("Iris's place until 15 October")
    fits()
    page.screenshot(path=f'{shots}/moresocial-evidence-rewrite-{viewport["width"]}.png', full_page=True)
    page.get_by_role('button', name='Keep my text').click()
    playwright.expect(page.locator('[data-proposal]')).to_have_count(0)
    page.get_by_role('button', name='Save edits').click()  # re-confirm the edit against the latest bundle

    page.get_by_role('link', name='Open conversation', exact=True).click()
    page.wait_for_url('**/whatsapp?account=*')
    assert page.locator('#wa-message').input_value() == 'Hoi Jonas! I am at Iris for now, more soon. [how you are]'
    assert 'What this draft uses' not in page.content()
    fits()
    assert sent == [] and errors == []
    ctx.close()
