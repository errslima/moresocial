"""Admin OpenRouter setup: access control, the instrumented setup test and the shared
embedding identity policy. OpenRouter is mocked; no key or network is needed."""
import dataclasses
import json
import re

import httpx
import pytest

from app import admin, ai, config, db, operator_settings
from app.models import OperatorSetting

KEY = 'sk-or-v1-SyntheticAdminSecret-000000000001'
ALIAS = 'voyageai/voyage-4'
CANONICAL = 'voyageai/voyage-4-20260727'
REASONING = 'vendor/reasoner'


class FakeOpenRouter:
    """Catalog plus chat/embedding endpoints; each embedding call returns the next model name."""

    def __init__(self, embedding_models=(ALIAS, ALIAS), catalog_status=200, dimension=4):
        self.embedding_models = list(embedding_models)
        self.catalog_status = catalog_status
        self.dimension = dimension
        self.posts: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == 'GET':
            if self.catalog_status != 200:
                return httpx.Response(self.catalog_status, json={'error': {'message': 'down'}})
            if path.endswith('/embeddings/models'):
                return httpx.Response(200, json={'data': [{'id': ALIAS, 'canonical_slug': CANONICAL},
                                                          {'id': 'openai/text-embedding-3-small'}]})
            return httpx.Response(200, json={'data': [{'id': REASONING, 'supported_parameters': ['response_format']}]})
        body = json.loads(request.content)
        assert request.headers['authorization'] == 'Bearer ' + KEY
        self.posts.append(body)
        if path.endswith('/chat/completions'):
            return httpx.Response(200, json={'id': 'gen-synthetic-1', 'model': body['model'],
                                             'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'cost': 0.00001},
                                             'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok": true}'}}]})
        model = self.embedding_models.pop(0) if self.embedding_models else ALIAS
        return httpx.Response(200, json={'id': 'gen-synthetic-2', 'model': model, 'usage': {'prompt_tokens': 3},
                                         'data': [{'index': 0, 'embedding': [0.5] * self.dimension}]})


@pytest.fixture
def admin_on():
    original = config.get()
    config.override(dataclasses.replace(original, admin_emails=frozenset({'alex@example.test'})))
    yield
    config.override(original)
    admin.set_transport(None)


def use(fake: FakeOpenRouter) -> FakeOpenRouter:
    admin.set_transport(httpx.MockTransport(fake))
    return fake


def save(browser, reasoning=REASONING, embedding=ALIAS, key=KEY, effort=''):
    page = browser.get('/admin').text
    revision = re.search(r'name="revision" value="(\d+)"', page).group(1)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    return browser.post('/admin/ai/openrouter/save', {'api_key': key, 'reasoning_model': reasoning, 'embedding_model': embedding,
                                                      'reasoning_effort': effort, 'revision': revision, 'csrf': csrf})


def events(err: str, name: str | None = None) -> list[dict]:
    rows = [json.loads(line) for line in err.splitlines() if line.startswith('{')]
    return [r for r in rows if name is None or r['event'] == name]


def settings_without_test_record() -> dict:
    with db.session() as s:
        values = operator_settings.values(s)
    return {k: v for k, v in values.items() if not k.startswith('openrouter_last_test')}


def selected(page: str, name: str) -> str | None:
    select = re.search(rf'<select id="[^"]+" name="{name}".*?</select>', page, re.S).group(0)
    m = re.search(r'<option value="([^"]+)" selected>', select)
    return m.group(1) if m else None


def test_only_admins_can_open_or_change_admin_settings(make_browser, admin_on):
    use(FakeOpenRouter())
    alex, blake = make_browser(), make_browser()
    alex.login('alex')
    blake.login('blake')
    assert blake.get('/admin').status_code == 404 and '/moresocial/admin' not in blake.get('/home').text
    assert blake.post('/admin/ai/openrouter/disable', {'csrf': blake.csrf('/home')}).status_code == 404
    page = alex.get('/admin')
    assert page.status_code == 200 and 'Admin: OpenRouter AI' in page.text
    assert alex.post('/admin/ai/openrouter/disable', {}).status_code == 403  # CSRF still required
    assert make_browser().get('/admin').status_code == 303


def test_selected_id_response_saves_catalog_identity(browser, admin_on, capsys):
    fake = use(FakeOpenRouter(embedding_models=(ALIAS, ALIAS)))
    browser.login('alex')
    r = save(browser)
    assert r.status_code == 303, r.text
    with db.session() as s:
        values = operator_settings.values(s)
    assert values[operator_settings.EMBEDDING_MODEL] == ALIAS
    assert values[operator_settings.EMBEDDING_IDENTITY] == CANONICAL
    assert values[operator_settings.EMBEDDING_DIMENSION] == '4'
    assert [p['model'] for p in fake.posts] == [REASONING, ALIAS, ALIAS]  # the selected ID is always what is sent
    err = capsys.readouterr().err
    stages = events(err, 'admin_openrouter_stage')
    assert [e['stage'] for e in stages] == ['catalog', 'reasoning_probe', 'embedding_probe', 'query_probe', 'validation', 'commit']
    assert len({e['operation_id'] for e in stages}) == 1
    probe = next(e for e in stages if e['stage'] == 'embedding_probe')
    assert (probe['requested_model'], probe['catalog_model'], probe['returned_model']) == (ALIAS, CANONICAL, ALIAS)
    assert probe['actual_dimension'] == 4 and probe['http_status'] == 200 and probe['provider_request_id'] == 'gen-synthetic-2'
    saved = events(err, 'admin_openrouter_saved')[0]
    assert saved['audit'] is True and saved['identity'] == CANONICAL and saved['operation_id'] == probe['operation_id']
    assert KEY not in err and 'SyntheticAdminSecret' not in err and 'Synthetic setup check' not in err


def test_documented_canonical_response_is_accepted_and_reload_is_consistent(browser, admin_on):
    use(FakeOpenRouter(embedding_models=(CANONICAL, CANONICAL)))
    browser.login('alex')
    assert save(browser).status_code == 303
    ai.reload_operator()
    runtime = ai.provider()
    assert isinstance(runtime, ai.OpenRouterProvider)
    assert (runtime.embedding_model, runtime.embedding_identity, runtime.embedding_dimension) == (ALIAS, CANONICAL, 4)
    assert runtime.embedding_space == f'v2:openrouter:{CANONICAL}:4:v1:plain-text:document-query'
    # Runtime validation uses the same policy as setup (the previously confirmed defect:
    # the tested identity was discarded, so a canonical response failed after saving).
    for returned, ok in [(ALIAS, True), (CANONICAL, True), ('voyage-4', False), ('voyageai/voyage-4-20270101', False)]:
        runtime._transport = httpx.MockTransport(lambda r, m=returned: httpx.Response(
            200, json={'model': m, 'data': [{'index': 0, 'embedding': [1.0, 0.0, 0.0, 0.0]}]}))
        if ok:
            assert runtime.embed(['x'], 'document').model == returned
        else:
            with pytest.raises(ai.InvalidOutput, match='embedding_model'):
                runtime.embed(['x'], 'document')
    assert set(ai.embedding_targets()) == {runtime.embedding_space}


def test_unknown_model_is_refused_without_paid_calls(browser, admin_on, capsys):
    fake = use(FakeOpenRouter())
    browser.login('alex')
    r = save(browser, embedding='vendor/not-in-catalog')
    assert r.status_code == 400 and 'unknown_model' in r.text and 'No paid request was made' in r.text
    assert fake.posts == []
    failed = events(capsys.readouterr().err, 'admin_openrouter_test_failed')[0]
    assert (failed['stage'], failed['code'], failed['catalog_status']) == ('catalog', 'unknown_model', 'ok')
    assert settings_without_test_record() == {}


def test_catalog_outage_is_reported_as_such_not_as_identity_mismatch(browser, admin_on, capsys):
    fake = use(FakeOpenRouter(catalog_status=503))
    browser.login('alex')
    r = save(browser)
    assert r.status_code == 400 and 'catalog check (catalog_unavailable)' in r.text and '(embedding_model)' not in r.text
    assert fake.posts == []
    err = capsys.readouterr().err
    failed = events(err, 'admin_openrouter_test_failed')[0]
    assert (failed['stage'], failed['code'], failed['catalog_status']) == ('catalog', 'catalog_unavailable', 'unavailable')
    assert events(err, 'openrouter_catalog_failed')[0]['http_status'] == 503
    with db.session() as s:
        assert operator_settings.values(s)[operator_settings.LAST_TEST_ERROR] == 'catalog: catalog_unavailable'


def test_catalog_outage_allows_retesting_an_unchanged_verified_configuration(browser, admin_on):
    use(FakeOpenRouter(embedding_models=(CANONICAL, CANONICAL)))
    browser.login('alex')
    assert save(browser).status_code == 303
    use(FakeOpenRouter(embedding_models=(CANONICAL, CANONICAL), catalog_status=503))
    assert save(browser, key='').status_code == 303  # blank key reuses the saved key
    with db.session() as s:
        assert operator_settings.values(s)[operator_settings.EMBEDDING_IDENTITY] == CANONICAL


def test_first_probe_mismatch_names_all_three_models(browser, admin_on, capsys):
    use(FakeOpenRouter(embedding_models=('voyage-4',)))
    browser.login('alex')
    r = save(browser)
    assert r.status_code == 400
    assert 'embedding test (embedding_model)' in r.text
    assert f'Requested {ALIAS}; catalog canonical {CANONICAL}; returned voyage-4.' in r.text
    failed = events(capsys.readouterr().err, 'admin_openrouter_test_failed')[0]
    assert (failed['stage'], failed['returned_model'], failed['catalog_model']) == ('embedding_probe', 'voyage-4', CANONICAL)


def test_second_probe_mismatch_keeps_settings_and_preserves_the_form(browser, admin_on, capsys):
    use(FakeOpenRouter(embedding_models=(CANONICAL, CANONICAL)))
    browser.login('alex')
    assert save(browser).status_code == 303
    before = settings_without_test_record()
    capsys.readouterr()
    fake = use(FakeOpenRouter(embedding_models=('openai/text-embedding-3-small', 'other/model')))
    r = save(browser, embedding='openai/text-embedding-3-small', effort='yes')
    assert r.status_code == 400
    assert 'query embedding test (embedding_model)' in r.text and 'Existing settings were kept.' in r.text
    assert [p['model'] for p in fake.posts][1:] == ['openai/text-embedding-3-small', 'openai/text-embedding-3-small']
    assert settings_without_test_record() == before
    # The unsaved selections stay selected; the key field stays empty.
    assert selected(r.text, 'embedding_model') == 'openai/text-embedding-3-small'
    assert selected(r.text, 'reasoning_model') == REASONING
    assert 'name="reasoning_effort" value="yes" checked' in r.text
    assert KEY not in r.text and re.search(r'<input id="or-key" name="api_key" type="password"[^>]*value=', r.text) is None
    reference = re.search(r'Reference: <code>([0-9a-f]{32})</code>', r.text).group(1)
    err = capsys.readouterr().err
    linked = [e for e in events(err) if e.get('operation_id') == reference]
    assert {e['event'] for e in linked} >= {'admin_openrouter_test_started', 'admin_openrouter_stage', 'provider_call',
                                             'admin_openrouter_test_failed'}
    failed = events(err, 'admin_openrouter_test_failed')[0]
    assert (failed['stage'], failed['code'], failed['requested_model'], failed['returned_model']) == (
        'query_probe', 'embedding_model', 'openai/text-embedding-3-small', 'other/model')
    assert failed['expected_dimension'] == 4
    with db.session() as s:
        values = operator_settings.values(s)
    assert values[operator_settings.LAST_TEST_REFERENCE] == reference
    assert 'reference <code>' + reference in browser.get('/admin').text


def test_provider_rejection_records_http_status(browser, admin_on, capsys):
    def handler(request):
        if request.method == 'GET':
            return FakeOpenRouter()(request)
        return httpx.Response(402, json={'error': {'message': 'PRIVATE_PROVIDER_BODY'}})
    admin.set_transport(httpx.MockTransport(handler))
    browser.login('alex')
    r = save(browser)
    assert r.status_code == 400 and 'reasoning test (no_credit)' in r.text
    err = capsys.readouterr().err
    failed = events(err, 'admin_openrouter_test_failed')[0]
    assert (failed['stage'], failed['code'], failed['http_status']) == ('reasoning_probe', 'no_credit', 402)
    assert 'PRIVATE_PROVIDER_BODY' not in err


def test_embedding_change_is_staged_with_its_identity_and_promoted(browser, admin_on):
    use(FakeOpenRouter(embedding_models=(ALIAS, ALIAS)))
    browser.login('alex')
    assert save(browser).status_code == 303
    use(FakeOpenRouter(embedding_models=('openai/text-embedding-3-small',) * 2, dimension=6))
    assert save(browser, embedding='openai/text-embedding-3-small').status_code == 303
    with db.session() as s:
        values = operator_settings.values(s)
        assert values[operator_settings.EMBEDDING_IDENTITY] == CANONICAL
        assert values[operator_settings.PENDING_EMBEDDING_IDENTITY] == 'openai/text-embedding-3-small'
    ai.reload_operator()
    spaces = set(ai.embedding_targets())
    assert spaces == {f'v2:openrouter:{CANONICAL}:4:v1:plain-text:document-query',
                      'v2:openrouter:openai/text-embedding-3-small:6:v1:plain-text:document-query'}
    with db.session() as s:
        assert operator_settings.promote_pending(s)
    with db.session() as s:
        values = operator_settings.values(s)
        assert values[operator_settings.EMBEDDING_IDENTITY] == 'openai/text-embedding-3-small'
        assert operator_settings.PENDING_EMBEDDING_IDENTITY not in values


def test_legacy_settings_without_identity_keep_their_space():
    base = config.load({'MORESOCIAL_MODE': 'test', 'DATABASE_URL': 'postgresql://x/y', 'AI_PROVIDER': 'none'})
    p = ai.build_operator(base, {'ai_provider': 'openrouter', 'openrouter_api_key': KEY, 'openrouter_reasoning_model': REASONING,
                                 'openrouter_embedding_model': ALIAS, 'openrouter_embedding_dimension': '4'})
    assert p.embedding_identity == ALIAS
    assert p.embedding_space == f'v2:openrouter:{ALIAS}:4:v1:plain-text:document-query'
    assert KEY not in repr(p)


def test_disable_and_remove_are_audited(browser, admin_on, capsys):
    use(FakeOpenRouter())
    browser.login('alex')
    assert save(browser).status_code == 303
    csrf = browser.csrf('/admin')
    assert browser.post('/admin/ai/openrouter/disable', {'csrf': csrf}).status_code == 303
    assert browser.post('/admin/ai/openrouter/key/remove', {'csrf': csrf}).status_code == 303
    err = capsys.readouterr().err
    assert events(err, 'admin_openrouter_disabled')[0]['audit'] is True
    assert events(err, 'admin_openrouter_key_removed')[0]['audit'] is True
    with db.session() as s:
        assert s.get(OperatorSetting, 'openrouter_api_key').value is None
