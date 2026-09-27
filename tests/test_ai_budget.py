"""Usage accounting only charges work a provider can bill, rate limits pace the worker
instead of burning job attempts, and provider failures are logged (without content)."""
from datetime import timedelta
import json

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


def test_openrouter_rate_limits_are_not_billed_and_logged_without_content(capsys):
    key, prompt, body = 'sk-or-v1-SyntheticKeyCanary0000', 'PROMPT_CANARY about Sam', 'BODY_CANARY'
    p = ai.OpenRouterProvider(key, 'vendor/generation', 'vendor/embed', 2, transport=httpx.MockTransport(
        lambda r: httpx.Response(429, headers={'retry-after': '7'}, json={'error': {'message': body}})))
    with pytest.raises(ai.AIUnavailable) as exc:
        p.generate(system='SYSTEM_CANARY', prompt=prompt, schema={'type': 'object'}, max_tokens=10, effort='low', context={})
    assert (str(exc.value), exc.value.not_billed, exc.value.retry_after, exc.value.http_status) == ('rate_limited', True, 7.0, 429)
    with pytest.raises(ai.AIUnavailable) as exc:
        p.embed([prompt], 'document')
    assert (str(exc.value), exc.value.not_billed, exc.value.retry_after) == ('rate_limited', True, 7.0)
    err = capsys.readouterr().err
    calls = [json.loads(line) for line in err.splitlines() if line.startswith('{') and '"provider_call"' in line]
    assert [(c['operation'], c['requested_model']) for c in calls] == [('generation', 'vendor/generation'),
                                                                        ('embedding', 'vendor/embed')]
    for c in calls:
        assert (c['event'], c['provider'], c['http_status'], c['error_category'], c['delay_ms'], c['outcome'], c['level']) == (
            'provider_call', 'openrouter', 429, 'rate_limited', 7000, 'failed', 'error')
    for canary in (key, 'SyntheticKeyCanary', 'PROMPT_CANARY', 'SYSTEM_CANARY', body):
        assert canary not in err


def test_workspaces_share_the_provider_with_separate_usage_accounting(make_browser):
    alex, blake = make_browser(), make_browser()
    alex.login('alex', calendar=False)
    blake.login('blake', calendar=False)
    schedule()
    drain()
    wa, wb = workspace_of('alex@example.test'), workspace_of('blake@example.test')
    with db.session() as s:
        usage = s.scalars(select(ProviderUsage)).all()
        scopes = {b.scope: b.tokens for b in s.scalars(select(UsageBudget))}
    spent = {w: sum(u.input_tokens + u.output_tokens for u in usage if u.workspace_id == w) for w in (wa, wb)}
    assert {u.workspace_id for u in usage if u.operation == 'generate'} == {wa, wb}
    assert all(u.status == 'reconciled' and u.billing == 'operator' and u.provider == 'fake' for u in usage)
    assert spent[wa] and spent[wb]
    assert scopes == {'workspace:' + str(wa): spent[wa], 'workspace:' + str(wb): spent[wb], 'global': spent[wa] + spent[wb]}


def test_allowance_migration_resumes_only_shared_budget_waits(browser, database):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config
    from app import jobs

    browser.login('alex', calendar=False)
    wid = workspace_of('alex@example.test')
    cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    cfg.attributes['database_url'] = database['runtime_url']
    command.downgrade(cfg, '0005')
    until = jobs.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1, minutes=1)
    reasons = [
        ('workspace', 'queued', 'AI paused: daily workspace budget reached'),
        ('global', 'queued', 'AI paused: daily global budget reached'),
        ('userkey', 'queued', 'AI paused: daily limit for your API key reached'),
        ('rate', 'queued', 'AI waiting: provider rate limit'),
        ('cancelled', 'cancelled', 'AI paused: daily global budget reached'),
    ]
    try:
        with db.session() as s:
            for key, status, reason in reasons:
                jobs.enqueue(s, wid, 'extract_chunk', 'migration:' + key, run_after=until)
                job = s.scalars(select(Job).where(Job.idempotency_key == 'migration:' + key)).one()
                job.status, job.last_error = status, reason
    finally:
        command.upgrade(cfg, 'head')
    with db.session() as s:
        for key, status, reason in reasons:
            job = s.scalars(select(Job).where(Job.idempotency_key == 'migration:' + key)).one()
            assert job.status == status and job.attempts == 0
            if key in ('workspace', 'global'):
                assert job.last_error is None and job.run_after <= jobs.utcnow()
            else:
                assert job.last_error == reason and job.run_after == until
