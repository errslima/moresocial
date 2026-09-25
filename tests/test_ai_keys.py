"""User-supplied AI API keys: storage, validation, per-workspace billing, fallback and the
provider adapters. Synthetic providers and mock transports only; no real key is used."""
import dataclasses
import json

import httpx
import pytest
from sqlalchemy import func, select, text, update

from app import ai, ai_keys, config, db, memory, retrieval, security
from app.gatherings import DRAFT_SCHEMA, RSVP_SCHEMA
from app.models import Base, Claim, Connection, Job, ProviderUsage, UsageBudget, Workspace
from conftest import drain, workspace_of
from worker.main import schedule

ANTHROPIC_KEY = 'sk-ant-api03-SyntheticSecretValue-0001-abcd'
OPENAI_KEY = 'sk-proj-SyntheticSecretValue-0002-wxyz'


@pytest.fixture
def keys_on():
    original = config.get()
    config.override(dataclasses.replace(original, user_ai_keys=('anthropic', 'openai'), openai_model='gpt-test'))
    yield
    config.override(original)
    ai.set_user_adapter_factory(None)
    ai_keys.set_transport(None)


class Recording(ai.FakeProvider):
    """A user-key adapter that remembers its key, so tests can see which key served a call."""

    def __init__(self, name, key):
        super().__init__()
        self.name, self.key, self.generation_model = name, key, 'fake-' + name


def record_tiers(monkeypatch) -> list:
    served = []
    original = ai._generate_on

    def spy(tier, workspace_id, **kw):
        served.append((workspace_id, tier.billing, getattr(tier.provider, 'key', None)))
        return original(tier, workspace_id, **kw)
    monkeypatch.setattr(ai, '_generate_on', spy)
    return served


def add_key(browser, provider, key):
    return browser.post(f'/connections/ai/{provider}/key', {'api_key': key, 'csrf': browser.csrf('/connections')})


def key_rows(wid=None):
    with db.session() as s:
        q = select(Connection).where(Connection.provider.in_(['anthropic', 'openai']))
        if wid:
            q = q.where(Connection.workspace_id == wid)
        rows = s.scalars(q).all()
        s.expunge_all()
        return rows


def tables_containing(needle: str) -> list[str]:
    with db.engine().connect() as c:
        return [t.name for t in Base.metadata.sorted_tables
                if c.execute(text(f'SELECT count(*) FROM {t.name} t WHERE t::text LIKE :n'), {'n': f'%{needle}%'}).scalar()]


# ---------------- storage and key management ----------------

def test_saved_key_is_encrypted_and_never_shown_or_logged(browser, keys_on, capsys):
    browser.login('alex')
    r = add_key(browser, 'anthropic', '  ' + ANTHROPIC_KEY + '\n')
    assert r.status_code == 303 and 'SyntheticSecret' not in r.headers['location']
    (row,) = key_rows()
    assert row.state == 'active' and row.key_hint == 'abcd' and row.validated_at is not None
    assert row.provider_account == ai_keys.fingerprint(ANTHROPIC_KEY)
    assert security.decrypt(row.access_token_enc) == ANTHROPIC_KEY
    assert tables_containing('SyntheticSecret') == []  # only the ciphertext is stored
    page = browser.get('/connections?notice=ai-key-saved').text
    assert 'SyntheticSecret' not in page and 'abcd' in page and 'working' in page
    assert 'runs on your Anthropic key' in page
    assert 'SyntheticSecret' not in capsys.readouterr().err


def test_rejected_or_malformed_keys_are_not_stored(browser, keys_on):
    browser.login('alex')
    bad = 'sk-ant-reject-SyntheticSecretValue-9999'
    r = add_key(browser, 'anthropic', bad)
    assert r.status_code == 400 and 'did not accept this key' in r.text and bad not in r.text
    r = add_key(browser, 'anthropic', 'hello')
    assert r.status_code == 400 and 'look like an Anthropic (Claude) API key' in r.text
    r = add_key(browser, 'openai', ANTHROPIC_KEY)
    assert r.status_code == 400 and 'That is an Anthropic key' in r.text and ANTHROPIC_KEY not in r.text
    oauth = 'sk-ant-oat01-SyntheticSubscriptionToken-5'
    r = add_key(browser, 'anthropic', oauth)
    assert r.status_code == 400 and 'subscription sign-in token, not an API key' in r.text and oauth not in r.text
    r = add_key(browser, 'anthropic', 'sk-ant-nomodel-SyntheticSecretValue-1')
    assert r.status_code == 400 and 'cannot use the model' in r.text
    assert key_rows() == []


def test_replace_remove_and_preference(browser, keys_on):
    browser.login('alex')
    wid = workspace_of('alex@example.test')
    add_key(browser, 'anthropic', ANTHROPIC_KEY)
    add_key(browser, 'anthropic', ANTHROPIC_KEY[:-4] + 'efgh')
    (row,) = key_rows()
    assert row.key_hint == 'efgh' and security.decrypt(row.access_token_enc).endswith('efgh')
    add_key(browser, 'openai', OPENAI_KEY)
    assert ai.generator_for(wid).provider.name == 'anthropic'  # configured order when no preference
    browser.post('/connections/ai/preference', {'provider': 'openai', 'csrf': browser.csrf('/connections')})
    assert ai.generator_for(wid).provider.name == 'openai'
    assert 'Use for the assistant' in browser.get('/connections').text
    r = browser.post('/connections/ai/openai/remove', {'csrf': browser.csrf('/connections')})
    assert r.status_code == 303
    assert [k.provider for k in key_rows()] == ['anthropic']
    with db.session() as s:
        assert s.get(Workspace, wid).ai_preference is None
    assert ai.generator_for(wid).provider.name == 'anthropic'


def test_key_routes_require_csrf_enabled_provider_and_are_throttled(browser, keys_on):
    browser.login('alex')
    assert browser.post('/connections/ai/anthropic/key', {'api_key': ANTHROPIC_KEY}).status_code == 403
    config.override(dataclasses.replace(config.get(), user_ai_keys=('anthropic',)))
    csrf = browser.csrf('/connections')
    assert browser.post('/connections/ai/openai/key', {'api_key': OPENAI_KEY, 'csrf': csrf}).status_code == 404
    assert 'OpenAI' not in browser.get('/connections').text
    for _ in range(10):
        assert browser.post('/connections/ai/anthropic/key', {'api_key': 'nope', 'csrf': csrf}).status_code == 400
    r = browser.post('/connections/ai/anthropic/key', {'api_key': ANTHROPIC_KEY, 'csrf': csrf})
    assert r.status_code == 429 and key_rows() == []


def test_feature_off_hides_keys(browser):
    browser.login('alex')
    page = browser.get('/connections').text
    assert 'API key' not in page and 'AI assistant' in page
    assert browser.post('/connections/ai/anthropic/key',
                        {'api_key': ANTHROPIC_KEY, 'csrf': browser.csrf('/connections')}).status_code == 404


def test_account_deletion_removes_keys(browser, keys_on):
    browser.login('alex')
    add_key(browser, 'anthropic', ANTHROPIC_KEY)
    assert browser.post('/account/delete', {'confirm': 'DELETE', 'csrf': browser.csrf('/connections')}).status_code == 303
    assert key_rows() == [] and tables_containing(ai_keys.fingerprint(ANTHROPIC_KEY)) == []


# ---------------- billing, isolation and fallback ----------------

def test_each_workspace_is_served_by_its_own_key(make_browser, keys_on, monkeypatch):
    ai.set_user_adapter_factory(Recording)
    served = record_tiers(monkeypatch)
    alex, blake = make_browser(), make_browser()
    alex.login('alex')
    add_key(alex, 'anthropic', ANTHROPIC_KEY)
    blake.login('blake')
    add_key(blake, 'openai', OPENAI_KEY)
    schedule()
    drain()
    wa, wb = workspace_of('alex@example.test'), workspace_of('blake@example.test')
    assert served and {(w, b, k) for w, b, k in served} == {(wa, 'user', ANTHROPIC_KEY), (wb, 'user', OPENAI_KEY)}
    with db.session() as s:
        failed = s.scalars(select(Job).where(Job.status == 'failed')).all()
        assert failed == [], [(j.kind, j.last_error) for j in failed]
        usage = s.scalars(select(ProviderUsage)).all()
        gen = [u for u in usage if u.operation == 'generate']
        assert gen and {(u.workspace_id, u.billing, u.provider) for u in gen} == {(wa, 'user', 'anthropic'), (wb, 'user', 'openai')}
        assert all(u.billing == 'operator' for u in usage if u.operation == 'embed')  # embeddings stay on the operator
        budgets = {b.scope: b.tokens for b in s.scalars(select(UsageBudget))}
        user_a = sum(u.input_tokens + u.output_tokens for u in gen if u.workspace_id == wa)
        embed_total = sum((u.input_tokens or 0) + (u.output_tokens or 0) for u in usage if u.operation == 'embed')
        assert budgets['userkey:' + str(wa)] == user_a
        assert budgets['global'] == embed_total  # user-key generation never touches the operator's allowance
        assert s.scalar(select(func.count()).select_from(Claim).where(Claim.workspace_id == wa)) > 0


def test_user_without_key_uses_operator_tier(make_browser, keys_on, monkeypatch):
    ai.set_user_adapter_factory(Recording)
    served = record_tiers(monkeypatch)
    alex, blake = make_browser(), make_browser()
    alex.login('alex')
    add_key(alex, 'anthropic', ANTHROPIC_KEY)
    blake.login('blake')
    schedule()
    drain()
    wb = workspace_of('blake@example.test')
    assert {(b, k) for w, b, k in served if w == wb} == {('operator', None)}
    assert "Moresocial's shared allowance" in blake.get('/connections').text


def test_user_key_cap_pauses_with_its_own_message(browser, keys_on):
    config.override(dataclasses.replace(config.get(), ai_daily_tokens_user_key=50))
    browser.login('alex', calendar=False)
    add_key(browser, 'anthropic', ANTHROPIC_KEY)
    schedule()
    drain()
    with db.session() as s:
        paused = s.scalars(select(Job).where(Job.kind == 'extract_chunk')).all()
        assert paused and all(j.status == 'queued' and j.last_error == 'AI paused: daily limit for your API key reached'
                              for j in paused)
        assert s.scalars(select(ProviderUsage).where(ProviderUsage.operation == 'generate',
                                                     ProviderUsage.billing == 'operator')).all() == []
    page = browser.get('/connections').text
    assert "today's limit for your API key is used up" in page
    r = browser.post('/ask', {'question': 'Does Sam like lasergame?', 'csrf': browser.csrf('/ask')})
    assert r.status_code == 503 and 'limit for your own API key' in r.text


def test_key_refused_at_run_time_is_flagged_and_work_falls_back(browser, keys_on, monkeypatch):
    served = record_tiers(monkeypatch)
    browser.login('alex', calendar=False)
    add_key(browser, 'anthropic', 'sk-ant-revoked-SyntheticSecretValue-3')  # passes validation, refused later
    schedule()
    drain()
    (row,) = key_rows()
    assert row.state == 'reconnect_required' and row.detail == 'invalid_key'
    assert served[0][1] == 'user' and served[-1][1] == 'operator'
    with db.session() as s:
        assert s.scalars(select(Job).where(Job.status == 'failed')).all() == []
        assert s.scalar(select(func.count()).select_from(Claim)) > 0
        refused = s.scalars(select(ProviderUsage).where(ProviderUsage.billing == 'user')).all()
        assert refused and all(u.status == 'failed' and u.input_tokens == 0 for u in refused)
    page = browser.get('/connections').text
    assert 'needs attention' in page and 'did not accept this key' in page and "uses Moresocial's shared allowance" in page


def test_key_turns_ai_on_without_operator_and_resumes_waiting_jobs(browser, keys_on):
    ai.set_provider(ai.Provider())  # operator has no AI at all
    browser.login('alex', calendar=False)
    wid = workspace_of('alex@example.test')
    assert ai.generator_for(wid).billing == 'none'
    assert 'The assistant is off' in browser.get('/connections').text
    schedule()
    drain()
    with db.session() as s:
        waiting = s.scalars(select(Job).where(Job.kind == 'extract_chunk')).all()
        assert waiting and all(j.status == 'queued' and j.last_error.startswith('ai unavailable') for j in waiting)
    add_key(browser, 'anthropic', ANTHROPIC_KEY)
    drain()
    with db.session() as s:
        assert all(j.status == 'done' for j in s.scalars(select(Job).where(Job.kind == 'extract_chunk')))
        assert s.scalar(select(func.count()).select_from(Claim)) > 0


def test_adding_key_resumes_jobs_paused_by_operator_budget(browser, keys_on):
    config.override(dataclasses.replace(config.get(), ai_daily_tokens_workspace=50))
    browser.login('alex', calendar=False)
    schedule()
    drain()
    with db.session() as s:
        assert s.scalars(select(Job).where(Job.last_error.like('AI paused%'))).all()
    add_key(browser, 'anthropic', ANTHROPIC_KEY)
    drain()
    with db.session() as s:
        extract = s.scalars(select(Job).where(Job.kind == 'extract_chunk')).all()
        assert extract and all(j.status == 'done' for j in extract)


def test_second_key_takes_over_when_preferred_key_is_refused(browser, keys_on):
    browser.login('alex')
    wid = workspace_of('alex@example.test')
    add_key(browser, 'anthropic', ANTHROPIC_KEY)
    add_key(browser, 'openai', 'sk-proj-revoked-SyntheticSecretValue-4')
    browser.post('/connections/ai/preference', {'provider': 'openai', 'csrf': browser.csrf('/connections')})
    out = ai.generate(wid, system='s', prompt='p', schema=DRAFT_SCHEMA, context={'purpose': 'rewrite', 'text': 'Hi Sam', 'style': 'warmer'})
    assert out.model == 'fake-anthropic'
    states = {k.provider: k.state for k in key_rows()}
    assert states == {'anthropic': 'active', 'openai': 'reconnect_required'}


# ---------------- validation requests ----------------

def test_validation_calls_fixed_hosts_and_maps_statuses(keys_on):
    seen = []

    def handler(status):
        def respond(request):
            seen.append(request)
            if status == 'down':
                raise httpx.ConnectError('down', request=request)
            return httpx.Response(status, json={})
        return respond
    for status, expected in [(200, None), (429, None), (401, 'invalid_key'), (402, 'no_credit'), (403, 'model_unavailable'),
                             (404, 'model_unavailable'), (500, 'unreachable'), ('down', 'unreachable')]:
        ai_keys.set_transport(httpx.MockTransport(handler(status)))
        assert ai_keys.validate('anthropic', ANTHROPIC_KEY) == expected, status
        assert ai_keys.validate('openai', OPENAI_KEY) == expected, status
    a, o = seen[0], seen[1]
    assert str(a.url) == f'https://api.anthropic.com/v1/models/{config.get().anthropic_model}'
    assert a.headers['x-api-key'] == ANTHROPIC_KEY and 'authorization' not in a.headers
    assert str(o.url) == 'https://api.openai.com/v1/models/gpt-test' and o.headers['authorization'] == 'Bearer ' + OPENAI_KEY


# ---------------- adapters ----------------

def openai_with(handler) -> ai.OpenAIGenerator:
    return ai.OpenAIGenerator(OPENAI_KEY, 'gpt-test', reasoning=True, transport=httpx.MockTransport(handler))


def responses_body(output, status='completed', **extra):
    return {'id': 'resp_1', 'model': 'gpt-test-2026', 'status': status, 'output': output,
            'usage': {'input_tokens': 11, 'output_tokens': 7}, **extra}


def call(p):
    return p.generate(system='sys', prompt='prompt', schema=DRAFT_SCHEMA, max_tokens=300, effort='low', context={})


def test_openai_adapter_requests_strict_json_and_parses_output():
    captured = {}

    def handler(request):
        captured['body'] = json.loads(request.content)
        captured['auth'] = request.headers['authorization']
        return httpx.Response(200, json=responses_body([
            {'type': 'reasoning', 'summary': []},
            {'type': 'message', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': '{"text": "Hi Sam", "citations": []}', 'annotations': []}]}]))
    out = call(openai_with(handler))
    assert out.data == {'text': 'Hi Sam', 'citations': []} and (out.input_tokens, out.output_tokens) == (11, 7)
    assert out.model == 'gpt-test-2026'
    body = captured['body']
    assert body['text']['format'] == {'type': 'json_schema', 'name': 'result', 'schema': DRAFT_SCHEMA, 'strict': True}
    assert body['store'] is False and body['reasoning'] == {'effort': 'low'} and 'tools' not in body
    assert body['instructions'] == 'sys' and body['input'] == 'prompt' and body['max_output_tokens'] == 4300
    assert captured['auth'] == 'Bearer ' + OPENAI_KEY


@pytest.mark.parametrize('status,payload,expected', [
    (401, {'error': {'code': 'invalid_api_key'}}, 'invalid_key'),
    (403, {'error': {'code': 'unsupported_country'}}, 'model_unavailable'),
    (404, {'error': {'code': 'model_not_found'}}, 'model_unavailable'),
    (429, {'error': {'code': 'insufficient_quota'}}, 'no_credit'),
])
def test_openai_adapter_maps_key_refusals(status, payload, expected):
    with pytest.raises(ai.KeyRejected) as exc:
        call(openai_with(lambda r: httpx.Response(status, json=payload)))
    assert exc.value.reason == expected


def test_openai_adapter_other_failures():
    with pytest.raises(ai.AIUnavailable) as exc:
        call(openai_with(lambda r: httpx.Response(429, json={'error': {'code': 'rate_limit_exceeded'}})))
    assert not isinstance(exc.value, ai.KeyRejected) and str(exc.value) == 'rate_limited'
    with pytest.raises(ai.InvalidOutput) as exc:
        call(openai_with(lambda r: httpx.Response(200, json=responses_body(
            [], status='incomplete', incomplete_details={'reason': 'max_output_tokens'}))))
    assert str(exc.value) == 'max_tokens' and exc.value.output_tokens == 7
    with pytest.raises(ai.InvalidOutput) as exc:
        call(openai_with(lambda r: httpx.Response(200, json=responses_body(
            [{'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'no'}]}]))))
    assert str(exc.value) == 'refusal'


def anthropic_with(handler) -> ai.AnthropicGenerator:
    import httpx2  # the Anthropic SDK's HTTP library
    p = ai.AnthropicGenerator(ANTHROPIC_KEY, 'claude-test', fallbacks=False,
                              http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    p._client = p._client.with_options(max_retries=0)
    return p


def anthropic_error(status, kind, message='x'):
    import httpx2
    return lambda r: httpx2.Response(status, json={'type': 'error', 'error': {'type': kind, 'message': message}})


@pytest.mark.parametrize('status,kind,message,expected', [
    (401, 'authentication_error', 'invalid x-api-key', 'invalid_key'),
    (402, 'billing_error', 'payment', 'no_credit'),
    (400, 'invalid_request_error', 'Your credit balance is too low to access the Anthropic API.', 'no_credit'),
    (403, 'permission_error', 'no access', 'model_unavailable'),
    (404, 'not_found_error', 'model: claude-test', 'model_unavailable'),
])
def test_anthropic_adapter_maps_key_refusals(status, kind, message, expected):
    with pytest.raises(ai.KeyRejected) as exc:
        call(anthropic_with(anthropic_error(status, kind, message)))
    assert exc.value.reason == expected


def test_anthropic_adapter_success_and_other_failures():
    captured = {}

    def ok(request):
        import httpx2
        captured['key'] = request.headers['x-api-key']
        return httpx2.Response(200, json={
            'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': 'claude-test', 'stop_reason': 'end_turn',
            'stop_sequence': None, 'content': [{'type': 'text', 'text': '{"text": "Hi", "citations": []}'}],
            'usage': {'input_tokens': 9, 'output_tokens': 4}})
    out = call(anthropic_with(ok))
    assert out.data == {'text': 'Hi', 'citations': []} and captured['key'] == ANTHROPIC_KEY
    with pytest.raises(ai.AIUnavailable) as exc:
        call(anthropic_with(anthropic_error(400, 'invalid_request_error', 'messages: field required')))
    assert not isinstance(exc.value, ai.KeyRejected)
    with pytest.raises(ai.AIUnavailable) as exc:
        call(anthropic_with(anthropic_error(429, 'rate_limit_error')))
    assert str(exc.value) == 'rate_limited'


def test_generation_schemas_are_valid_openai_strict_schemas():
    def check(schema, path):
        if schema.get('type') == 'object':
            assert schema.get('additionalProperties') is False, path
            assert set(schema.get('required', [])) == set(schema.get('properties', {})), path
            for name, sub in schema['properties'].items():
                check(sub, f'{path}.{name}')
        elif schema.get('type') == 'array':
            check(schema['items'], path + '[]')
    for name, schema in [('extract', memory.EXTRACT_SCHEMA), ('answer', retrieval.ANSWER_SCHEMA),
                         ('draft', DRAFT_SCHEMA), ('rsvp', RSVP_SCHEMA)]:
        check(schema, name)


def test_adapter_cache_never_reuses_a_replaced_key(browser, keys_on):
    ai.set_user_adapter_factory(Recording)
    browser.login('alex')
    wid = workspace_of('alex@example.test')
    add_key(browser, 'anthropic', ANTHROPIC_KEY)
    assert ai.generator_for(wid).provider.key == ANTHROPIC_KEY
    replacement = ANTHROPIC_KEY[:-4] + 'zzzz'
    add_key(browser, 'anthropic', replacement)
    assert ai.generator_for(wid).provider.key == replacement


def test_migration_0003_round_trip_keeps_usage_history(browser, database):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config
    browser.login('alex', calendar=False)
    schedule()
    drain()
    with db.session() as s:
        before = s.scalar(select(func.count()).select_from(ProviderUsage))
    assert before
    cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    cfg.attributes['database_url'] = database['runtime_url']
    try:
        command.downgrade(cfg, '0002')
    finally:
        command.upgrade(cfg, 'head')
    command.check(cfg)
    with db.session() as s:
        rows = s.scalars(select(ProviderUsage)).all()
        assert len(rows) == before and all(u.billing == 'operator' and u.provider == 'anthropic' for u in rows)
