"""E0: budgets are only charged for work a provider can bill, rate limits pace the worker
instead of burning job attempts, and provider failures are logged (without content)."""
import httpx
import pytest
from sqlalchemy import func, select

from app import ai, db
from app.models import Chunk, Job, ProviderUsage, UsageBudget
from conftest import drain, workspace_of
from worker.main import reconcile_ai, schedule


class NoEmbeddings(ai.FakeProvider):
    """Generation works; embeddings are not configured (production before a Voyage key)."""
    embedding_dimension = 0
    embedding_model = 'none'

    def __init__(self):
        super().__init__()
        self.embedding_dimension, self.embedding_model = 0, 'none'


class RateLimited(ai.FakeProvider):
    def __init__(self, exc):
        super().__init__()
        self.exc, self.generate_calls = exc, 0

    def generate(self, **kw):
        self.generate_calls += 1
        raise self.exc


def budgets():
    with db.session() as s:
        return {b.scope.split(':')[0]: b.tokens for b in s.scalars(select(UsageBudget))}


def test_no_embedder_means_no_embed_jobs_or_reservations(browser):
    ai.set_provider(NoEmbeddings())
    browser.login('alex', calendar=False)
    schedule()
    drain()
    reconcile_ai()
    drain()
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Chunk)) > 0
        assert s.scalars(select(Job).where(Job.kind == 'embed_chunk')).all() == []
        assert s.scalars(select(ProviderUsage).where(ProviderUsage.operation == 'embed')).all() == []
        usage = s.scalars(select(ProviderUsage)).all()
        assert usage and all(u.status == 'reconciled' for u in usage)
        spent = sum(u.input_tokens + u.output_tokens for u in usage)
    assert budgets() == {'workspace': spent, 'global': spent}
    with pytest.raises(ai.AIUnavailable):
        ai.embed(workspace_of('alex@example.test'), ['hello'], 'query')
    assert budgets()['global'] == spent


def test_embed_job_queued_before_embeddings_were_turned_off_is_a_no_op(browser):
    browser.login('alex', calendar=False)
    schedule()
    drain()
    wid = workspace_of('alex@example.test')
    ai.set_provider(NoEmbeddings())
    from app import jobs
    with db.session() as s:
        chunk = s.scalars(select(Chunk).where(Chunk.workspace_id == wid)).first()
        jobs.enqueue(s, wid, 'embed_chunk', 'embed:leftover', {'chunk_id': str(chunk.id)})
        before = s.scalar(select(func.count()).select_from(ProviderUsage))
    drain()
    with db.session() as s:
        assert s.scalars(select(Job).where(Job.idempotency_key == 'embed:leftover')).one().status == 'done'
        assert s.scalar(select(func.count()).select_from(ProviderUsage)) == before


def test_rate_limit_releases_reservation_pauses_provider_and_defers_jobs(browser):
    provider = RateLimited(ai.rate_limited(retry_after=12))
    ai.set_provider(provider)
    browser.login('alex', calendar=False)
    schedule()
    drain()
    with db.session() as s:
        waiting = s.scalars(select(Job).where(Job.kind == 'extract_chunk')).all()
        assert waiting and all(j.status == 'queued' and j.attempts == 0 and j.last_error == 'AI waiting: provider rate limit'
                               for j in waiting)
        gen = s.scalars(select(ProviderUsage).where(ProviderUsage.operation == 'generate')).all()
        assert len(gen) == 1 and gen[0].status == 'failed' and gen[0].input_tokens == 0
    assert provider.generate_calls == 1  # the cooldown stops every other job from calling (or reserving) again
    with db.session() as s:
        embed_tokens = s.scalar(select(func.coalesce(func.sum(ProviderUsage.input_tokens), 0))
                                .where(ProviderUsage.operation == 'embed'))
    assert budgets()['workspace'] == embed_tokens  # the generation reservation was fully released
    assert 'Paused' not in browser.get('/connections').text  # a short wait is not "budget used up"


def test_unknown_outcome_keeps_the_reservation(browser):
    ai.set_provider(RateLimited(ai.AIUnavailable('connection')))
    browser.login('alex', calendar=False)
    schedule()
    drain()
    with db.session() as s:
        gen = s.scalars(select(ProviderUsage).where(ProviderUsage.operation == 'generate')).all()
        assert gen and all(u.status == 'failed' and u.input_tokens is None for u in gen)
        kept = sum(u.reserved_tokens for u in gen)
        embed_tokens = s.scalar(select(func.coalesce(func.sum(ProviderUsage.input_tokens), 0))
                                .where(ProviderUsage.operation == 'embed'))
    assert budgets()['workspace'] == kept + embed_tokens  # the provider may have billed: stay conservative


def test_adapters_log_rate_limits_without_content(capsys):
    from test_ai_keys import OPENAI_KEY, anthropic_error, anthropic_with, call, openai_with
    with pytest.raises(ai.AIUnavailable) as exc:
        call(openai_with(lambda r: httpx.Response(429, headers={'retry-after': '7'},
                                                  json={'error': {'code': 'rate_limit_exceeded'}})))
    assert exc.value.not_billed and exc.value.retry_after == 7
    with pytest.raises(ai.AIUnavailable) as exc:
        call(anthropic_with(anthropic_error(429, 'rate_limit_error')))
    assert str(exc.value) == 'rate_limited' and exc.value.not_billed
    err = capsys.readouterr().err
    assert '"event": "ai_generation_failed"' in err and '"provider": "openai"' in err and '"provider": "anthropic"' in err
    assert '"status": 429' in err and OPENAI_KEY not in err and 'prompt' not in err
