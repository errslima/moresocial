"""OpenRouter is mocked here: CI never needs an operator key."""
import json

import httpx
import pytest

from app import ai, config


def test_openrouter_uses_fixed_host_schema_and_strict_policy():
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/chat/completions'):
            return httpx.Response(200, json={'model': 'vendor/generation', 'usage': {'prompt_tokens': 3, 'completion_tokens': 4},
                                             'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok": true}'}}]})
        return httpx.Response(200, json={'model': 'vendor/embed', 'usage': {'prompt_tokens': 2},
                                         'data': [{'index': 1, 'embedding': [0, 1]}, {'index': 0, 'embedding': [1, 0]}]})
    p = ai.OpenRouterProvider('secret-not-logged', 'gen', 'embed', 2, ('provider-a',), ('provider-b',), httpx.MockTransport(handler))
    assert p.generate(system='s', prompt='p', schema={'type': 'object'}, max_tokens=10, effort='low', context={}).data == {'ok': True}
    assert p.embed(['a', 'b'], 'document').vectors == [[1.0, 0.0], [0.0, 1.0]]
    for request in requests:
        assert str(request.url).startswith('https://openrouter.ai/api/v1/')
        body = json.loads(request.content)
        assert body['provider']['allow_fallbacks'] is False
        assert body['provider']['data_collection'] == 'deny' and body['provider']['zdr'] is True
        assert body['provider']['only']
    assert json.loads(requests[0].content)['provider']['require_parameters'] is True


def test_openrouter_rejects_incomplete_configuration():
    with pytest.raises(config.ConfigError):
        config.load({'MORESOCIAL_MODE': 'test', 'DATABASE_URL': 'postgresql://x/y', 'AI_PROVIDER': 'openrouter'})


def test_openrouter_rejects_bad_embedding_indices():
    p = ai.OpenRouterProvider('secret', 'gen', 'embed', 2, ('a',), ('b',),
                              httpx.MockTransport(lambda r: httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1, 0]},
                                                                                                  {'index': 0, 'embedding': [1, 0]}]})))
    with pytest.raises(ai.InvalidOutput, match='embedding_index'):
        p.embed(['a', 'b'], 'document')
