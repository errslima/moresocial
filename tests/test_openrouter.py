"""OpenRouter is mocked here: CI never needs an operator key."""
import json

import httpx
import pytest

from app import ai, config


def test_openrouter_uses_fixed_host_schema_and_current_routing_policy():
    """Current contract (docs/operator-setup.md): exact model IDs, strict JSON schema with
    ``require_parameters``, and no provider allowlists, fallbacks flags or privacy routing."""
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/chat/completions'):
            return httpx.Response(200, json={'model': 'vendor/generation', 'usage': {'prompt_tokens': 3, 'completion_tokens': 4},
                                             'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok": true}'}}]})
        return httpx.Response(200, json={'model': 'vendor/embed', 'usage': {'prompt_tokens': 2},
                                         'data': [{'index': 1, 'embedding': [0, 1]}, {'index': 0, 'embedding': [1, 0]}]})
    p = ai.OpenRouterProvider('secret-not-logged', 'vendor/generation', 'vendor/embed', 2, transport=httpx.MockTransport(handler))
    assert p.generate(system='s', prompt='p', schema={'type': 'object'}, max_tokens=10, effort='low', context={}).data == {'ok': True}
    assert p.embed(['a', 'b'], 'document').vectors == [[1.0, 0.0], [0.0, 1.0]]
    for request in requests:
        assert str(request.url).startswith('https://openrouter.ai/api/v1/')
        body = json.loads(request.content)
        assert set(body.get('provider', {})) <= {'require_parameters'}
        assert 'data_collection' not in json.dumps(body) and 'zdr' not in json.dumps(body)
    generation = json.loads(requests[0].content)
    assert generation['provider'] == {'require_parameters': True}
    assert generation['response_format']['json_schema']['strict'] is True
    assert json.loads(requests[1].content) == {'model': 'vendor/embed', 'input': ['a', 'b']}


def test_openrouter_bootstrap_needs_both_models_or_neither():
    base = {'MORESOCIAL_MODE': 'test', 'DATABASE_URL': 'postgresql://x/y', 'AI_PROVIDER': 'openrouter'}
    assert config.load(base).openrouter_embedding_model == ''  # Admin can configure everything later
    with pytest.raises(config.ConfigError):
        config.load({**base, 'OPENROUTER_GENERATION_MODEL': 'vendor/generation'})


def test_openrouter_rejects_bad_embedding_indices():
    p = ai.OpenRouterProvider('secret', 'gen', 'embed', 2,
                              transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1, 0]},
                                                                                                           {'index': 0, 'embedding': [1, 0]}]})))
    with pytest.raises(ai.InvalidOutput, match='embedding_index'):
        p.embed(['a', 'b'], 'document')


def test_identity_policy_is_exact():
    assert ai.identity_accepted('a/m', 'a/m', 'a/m-2026')
    assert ai.identity_accepted('a/m-2026', 'a/m', 'a/m-2026')
    assert not ai.identity_accepted('m', 'a/m', 'a/m-2026')          # no prefix stripping
    assert not ai.identity_accepted('a/m-2027', 'a/m', 'a/m-2026')   # an alias re-pointed elsewhere
    assert not ai.identity_accepted(None, 'a/m', None)


def events(err):
    return [json.loads(line) for line in err.splitlines() if line.startswith('{')]


def test_provider_calls_emit_metadata_without_content(capsys, monkeypatch):
    monkeypatch.setattr(ai.time, 'sleep', lambda s: None)
    replies = iter([httpx.Response(503, json={'error': {'message': 'PRIVATE_BODY'}}),
                    httpx.Response(200, json={'id': 'gen-abc123', 'model': 'vendor/embed',
                                              'usage': {'prompt_tokens': 7, 'cost': 0.0002},
                                              'data': [{'index': 0, 'embedding': [0.1, 0.2]}]})])
    p = ai.OpenRouterProvider('sk-or-v1-PRIVATEKEY0000000000', 'gen', 'vendor/embed', 2,
                              transport=httpx.MockTransport(lambda r: next(replies)))
    p.embed(['PRIVATE_PROMPT'], 'query')
    err = capsys.readouterr().err
    assert 'PRIVATE' not in err and '0.1' not in err
    retry, ok = [e for e in events(err) if e['event'] == 'provider_call']
    assert (retry['outcome'], retry['http_status'], retry['error_category'], retry['delay_ms'], retry['level']) == (
        'retrying', 503, 'http_5xx', 1000, 'warn')
    assert (ok['outcome'], ok['attempt'], ok['returned_model'], ok['provider_request_id']) == ('ok', 1, 'vendor/embed', 'gen-abc123')
    assert (ok['input_tokens'], ok['cost_usd'], ok['operation'], ok['requested_model']) == (7, 0.0002, 'embedding', 'vendor/embed')
    for row in (retry, ok):
        assert row['schema'] == 1 and row['ts'].endswith('Z') and row['service']


def test_rejections_carry_http_status_and_rate_limit_delay(capsys):
    p = ai.OpenRouterProvider('secret', 'gen', 'embed', 2, transport=httpx.MockTransport(
        lambda r: httpx.Response(429, headers={'retry-after': '7'})))
    with pytest.raises(ai.AIUnavailable) as exc:
        p.embed(['a'], 'document')
    assert (str(exc.value), exc.value.http_status, exc.value.retry_after) == ('rate_limited', 429, 7.0)
    row = [e for e in events(capsys.readouterr().err) if e['event'] == 'provider_call'][0]
    assert (row['error_category'], row['delay_ms'], row['outcome']) == ('rate_limited', 7000, 'failed')
    p = ai.OpenRouterProvider('secret', 'gen', 'embed', 2, transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(ai.KeyRejected) as exc:
        p.embed(['a'], 'document')
    assert (exc.value.reason, exc.value.http_status) == ('invalid_key', 401)
