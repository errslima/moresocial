"""Admin page: only ADMIN_EMAILS may set the global AI provider and operator keys; the
operator tier follows those settings in every process. Synthetic providers only."""
import dataclasses
import json

import httpx
import pytest
from sqlalchemy import select

from app import ai, ai_keys, config, db, operator_settings, security
from app.models import OperatorSetting
from conftest import workspace_of
from test_ai_keys import tables_containing

OPENAI_KEY = 'sk-proj-SyntheticAdminSecret-0001-wxyz'
ANTHROPIC_KEY = 'sk-ant-api03-SyntheticAdminSecret-0002-abcd'


@pytest.fixture
def admin_on():
    original = config.get()
    config.override(dataclasses.replace(original, admin_emails=frozenset({'alex@example.test'}),
                                        user_ai_keys=('anthropic', 'openai')))
    yield
    config.override(original)
    ai_keys.set_transport(None)


def post(browser, path, **form):
    return browser.post(path, {**form, 'csrf': browser.csrf('/home')})


def test_only_admins_can_open_or_change_admin_settings(make_browser, admin_on):
    alex, blake = make_browser(), make_browser()
    alex.login('alex')
    blake.login('blake')
    assert blake.get('/admin').status_code == 404 and '/moresocial/admin' not in blake.get('/home').text
    for path, form in [('/admin/ai/provider', {'provider': 'none'}), ('/admin/ai/keys/openai', {'api_key': OPENAI_KEY}),
                       ('/admin/ai/keys/openai/remove', {})]:
        assert post(blake, path, **form).status_code == 404
    with db.session() as s:
        assert s.scalars(select(OperatorSetting)).all() == []
    page = alex.get('/admin')
    assert page.status_code == 200 and 'Admin: global AI' in page.text
    assert '/moresocial/admin' in alex.get('/home').text
    assert alex.post('/admin/ai/provider', {'provider': 'none'}).status_code == 403  # CSRF still required
    assert make_browser().get('/admin').status_code == 303  # signed out: back to the welcome page


def test_admin_sets_global_openai_for_users_without_their_own_key(make_browser, admin_on, capsys):
    alex, blake = make_browser(), make_browser()
    alex.login('alex')
    blake.login('blake')
    wb = workspace_of('blake@example.test')
    r = post(alex, '/admin/ai/provider', provider='openai')
    assert r.status_code == 400 and 'Add a working OpenAI key below' in r.text
    r = post(alex, '/admin/ai/keys/openai', api_key=OPENAI_KEY)
    assert r.status_code == 303
    assert post(alex, '/admin/ai/provider', provider='openai').status_code == 303
    tier = ai.generator_for(wb)
    assert (tier.billing, tier.provider.name) == ('operator', 'openai')
    page = alex.get('/admin').text
    assert 'wxyz' in page and 'alex@example.test' in page and 'SyntheticAdminSecret' not in page
    assert 'model <code>fake-openai</code>' in page
    assert tables_containing('SyntheticAdminSecret') == []
    with db.session() as s:
        assert operator_settings.values(s)['openai_api_key'] == OPENAI_KEY
    assert 'SyntheticAdminSecret' not in capsys.readouterr().err
    assert "Moresocial's shared allowance" in blake.get('/connections').text


def test_global_off_leaves_only_users_with_keys(make_browser, admin_on):
    alex, blake = make_browser(), make_browser()
    alex.login('alex')
    blake.login('blake')
    blake.post('/connections/ai/anthropic/key', {'api_key': ANTHROPIC_KEY, 'csrf': blake.csrf('/connections')})
    assert post(alex, '/admin/ai/provider', provider='none').status_code == 303
    wa, wb = workspace_of('alex@example.test'), workspace_of('blake@example.test')
    assert ai.generator_for(wa).billing == 'none' and ai.generator_for(wb).billing == 'user'
    assert 'The assistant is off' in alex.get('/connections').text
    assert 'Users with their own working key: 1' in alex.get('/admin').text


def test_rejected_admin_key_is_not_stored_and_removal_turns_provider_off(browser, admin_on):
    browser.login('alex')
    r = post(browser, '/admin/ai/keys/openai', api_key='sk-proj-reject-SyntheticAdminSecret-9')
    assert r.status_code == 400 and 'did not accept this key' in r.text and 'SyntheticAdminSecret' not in r.text
    r = post(browser, '/admin/ai/keys/anthropic', api_key='sk-ant-oat01-SyntheticAdminSecret-8')
    assert r.status_code == 400 and 'subscription sign-in token' in r.text
    with db.session() as s:
        assert operator_settings.values(s) == {}
    post(browser, '/admin/ai/keys/anthropic', api_key=ANTHROPIC_KEY)
    post(browser, '/admin/ai/provider', provider='anthropic')
    assert ai.provider().can_generate() and ai.provider().name == 'anthropic'
    assert post(browser, '/admin/ai/keys/anthropic/remove').status_code == 303
    assert not ai.provider().can_generate()
    page = browser.get('/admin').text
    assert 'the selected provider has no key' in page
    with db.session() as s:
        row = s.get(OperatorSetting, 'anthropic_api_key')
        assert row.value is None and row.updated_by == 'alex@example.test'


def test_other_processes_pick_up_changes(browser, admin_on):
    browser.login('alex')
    assert ai.provider().name == 'fake'  # environment default in tests
    with db.session() as s:  # as if saved by the web process while this one is a worker
        operator_settings.set_key(s, 'openai', OPENAI_KEY, 'alex@example.test')
        operator_settings.set_provider(s, 'openai', 'alex@example.test')
    assert ai.provider().name == 'fake'  # cached until the refresh interval passes
    ai._operator_checked = 0.0
    assert ai.provider().name == 'openai'


def test_build_operator_for_production_settings():
    base = config.load({'MORESOCIAL_MODE': 'test', 'DATABASE_URL': 'postgresql://x/y', 'AI_PROVIDER': 'none'})
    assert (base.anthropic_model, base.openai_model) == ('claude-sonnet-5', 'gpt-5.6-terra')
    tier = ai.build_operator(base, {})
    assert not tier.can_generate() and tier.embedding_dimension == 0
    tier = ai.build_operator(base, {'ai_provider': 'openai', 'openai_api_key': OPENAI_KEY, 'voyage_api_key': 'pa-synthetic-1'})
    assert isinstance(tier.generator, ai.OpenAIGenerator) and tier.generation_model == 'gpt-5.6-terra'
    assert isinstance(tier.embedder, ai.VoyageEmbedder) and tier.embedding_dimension == 1024 and tier.embed_name == 'voyage'
    assert OPENAI_KEY not in repr(tier.generator) and 'pa-synthetic' not in repr(tier.embedder)
    tier = ai.build_operator(base, {'ai_provider': 'anthropic', 'anthropic_api_key': ANTHROPIC_KEY})
    assert isinstance(tier.generator, ai.AnthropicGenerator) and tier.generation_model == 'claude-sonnet-5'
    assert not ai.build_operator(base, {'ai_provider': 'anthropic'}).can_generate()  # no key anywhere
    env = config.load({'MORESOCIAL_MODE': 'test', 'DATABASE_URL': 'postgresql://x/y', 'AI_PROVIDER': 'none',
                       'ANTHROPIC_MODEL': 'claude-opus-5-5', 'OPENAI_MODEL': 'gpt-5.6-luna',
                       'ADMIN_EMAILS': ' Errslima@Gmail.com , second@example.test'})
    assert (env.anthropic_model, env.openai_model) == ('claude-opus-5-5', 'gpt-5.6-luna')
    assert env.admin_emails == frozenset({'errslima@gmail.com', 'second@example.test'})


def test_voyage_key_validation_request(admin_on):
    seen = []

    def respond(status):
        def handler(request):
            seen.append(request)
            return httpx.Response(status, json={'data': [], 'usage': {'total_tokens': 1}})
        return handler
    from app import admin
    for status, expected in [(200, None), (401, 'invalid_key'), (400, 'model_unavailable'), (503, 'unreachable')]:
        ai_keys.set_transport(httpx.MockTransport(respond(status)))
        assert admin.check_key('voyage', 'pa-SyntheticVoyageKey-01') == expected
    assert admin.check_key('voyage', 'short') == 'format'
    request = seen[0]
    assert str(request.url) == 'https://api.voyageai.com/v1/embeddings'
    assert request.headers['authorization'] == 'Bearer pa-SyntheticVoyageKey-01'
    assert json.loads(request.content)['input'] == ['ok']
