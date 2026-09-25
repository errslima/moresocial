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
    page.get_by_role('link', name='Connections').click()
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
    page.get_by_role('link', name='Gatherings').click()
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
    page.get_by_role('link', name='Ask').click()
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
    page.get_by_role('link', name='Connections').click()
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
def test_admin_sets_global_provider(live, chromium, viewport):
    config.override(dataclasses.replace(config.get(), admin_emails=frozenset({'alex@example.test'})))
    page = chromium.new_context(viewport=viewport).new_page()
    page.goto(live + '/')
    page.get_by_role('button', name='Continue with Google').click()
    page.get_by_role('button', name='Allow').click()
    page.wait_for_url(live + '/home')
    page.get_by_role('link', name='Admin').click()
    section = page.locator('.ai-key', has_text='OpenAI')
    section.get_by_label('API key').fill('sk-proj-SyntheticAdminSecret-0003-wxyz')
    section.get_by_role('button', name='Save key').click()
    assert 'Global AI settings saved' in page.content()
    page.get_by_label('OpenAI').check()
    page.get_by_role('button', name='Save provider').click()
    playwright.expect(page.locator('section', has_text='Generation:')).to_contain_text('OpenAI')
    assert 'SyntheticAdminSecret' not in page.content()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
