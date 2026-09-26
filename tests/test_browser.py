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

    # Create a lasergame gathering, invite, draft and copy
    nav_to(page, 'Gatherings')
    page.get_by_label('Name').fill('Lasergame evening')
    page.get_by_label('Activity').fill('lasergame')
    page.get_by_role('button', name='Create').click()
    page.get_by_label('Start').fill(time.strftime('%Y-%m-%dT19:00', time.localtime(time.time() + 6 * 86400)))
    page.get_by_role('button', name='Add date').click()
    page.get_by_label('Add', exact=True).select_option(label='Sam Jansen')
    page.get_by_role('button', name='Add invitee').click()
    page.get_by_role('button', name='Draft an invitation').click()
    draft = page.locator('textarea[data-copy-source]')
    text = draft.input_value()
    assert 'Sam' in text and 'lasergame' in text.lower()
    page.get_by_role('button', name='Copy', exact=True).click()
    playwright.expect(page.locator('[data-copy-status]')).to_contain_text('Nothing was sent')
    assert page.evaluate('navigator.clipboard.readText()') == text  # Copy copies exactly what is shown
    assert page.get_by_role('button', name='Send').count() == 0
    assert 'Not contacted' in page.locator('article.invitee h3').inner_text()  # copying does not advance state
    no_horizontal_scroll()

    # Record the RSVP manually
    page.locator('article.invitee').get_by_label('Status').select_option('accepted')
    page.locator('article.invitee').get_by_role('button', name='Update').click()
    assert 'Accepted' in page.locator('article.invitee h3').inner_text()

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
def test_ai_key_rejection_add_and_remove(live, chromium, viewport):
    config.override(dataclasses.replace(config.get(), user_ai_keys=('anthropic', 'openai'), openai_model='gpt-test'))
    page = chromium.new_context(viewport=viewport).new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(live + '/')
    page.get_by_role('button', name='Continue with Google').click()
    page.get_by_role('button', name='Allow').click()
    page.wait_for_url(live + '/home')
    nav_to(page, 'Connections')
    key = page.locator('#key-anthropic')
    assert key.get_attribute('type') == 'password' and key.get_attribute('autocomplete') == 'off'

    key.fill('sk-ant-reject-SyntheticSecretValue-0001')
    page.get_by_role('button', name='Add key').first.click()
    playwright.expect(page.get_by_role('alert')).to_contain_text('did not accept this key')
    assert 'SyntheticSecretValue' not in page.content() and page.locator('#key-anthropic').input_value() == ''

    page.locator('#key-anthropic').fill('sk-ant-api03-SyntheticSecretValue-0002-abcd')
    page.get_by_role('button', name='Add key').first.click()
    playwright.expect(page.locator('.notice, [role=status]').first).to_be_visible()
    assert 'Your API key works and is saved' in page.content()
    section = page.locator('.ai-key', has_text='Anthropic (Claude)')
    playwright.expect(section).to_contain_text('working')
    playwright.expect(section).to_contain_text('abcd')
    assert 'runs on your Anthropic key' in page.content() and 'SyntheticSecretValue' not in page.content()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')

    section.get_by_role('button', name='Remove key').click()
    assert 'Your API key was removed' in page.content()
    playwright.expect(page.locator('.ai-key', has_text='Anthropic (Claude)')).to_contain_text('No key added')
    assert errors == []


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
    assert [l.strip() for l in labels] == ['Home', 'WhatsApp', 'Suggestions', 'People', 'Ask', 'Gatherings', 'About me', 'Connections']
    assert page.get_by_role('link', name='Admin').count() == 0  # not an administrator
    assert page.get_by_role('link', name='Home', exact=True).get_attribute('aria-current') == 'page'
    home_main = page.locator('main').bounding_box()
    assert home_main['width'] <= 861
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
        assert 330 <= page.locator('.wa-list').bounding_box()['width'] <= 350
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

    page.get_by_role('link', name='Older messages').click()
    page.wait_for_url('**&before=*')
    playwright.expect(history.locator('.wa-msg')).to_have_count(30)
    assert history.evaluate('e => e.scrollTop') == 0  # older history does not jump to the bottom
    playwright.expect(history.locator('.wa-msg').first).to_contain_text('Noor message 000')
    page.reload()
    playwright.expect(history.locator('.wa-msg')).to_have_count(30)
    page.go_back()
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
