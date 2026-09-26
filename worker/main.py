"""Background worker: sync, chunking, embeddings and extraction jobs.

Run with `python -m worker.main`. Several workers may run; claims are atomic and leases
recover jobs from crashed workers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import random
import signal
import socket
import threading
import time
import uuid

from sqlalchemy import and_, exists, func, select, text

from app import accounts, ai, config, db, diagnostics, google_sync, jobs, memory, observation_groups, operator_settings, security
from app.google import GoogleError
from app.models import Chunk, Claim, Connection, Embedding, Job, SyncStream, Workspace, PIPELINE_VERSION

OWNER = f'{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}'
STOP = threading.Event()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Heartbeat:
    """Renews the lease while a long job runs; the job stops if the lease was lost."""

    def __init__(self, job_id):
        self.job_id = job_id

    def __call__(self):
        with db.session() as s:
            if not jobs.renew(s, self.job_id, OWNER):
                raise LeaseLost()


class LeaseLost(Exception):
    pass


def handle(job: Job):
    p, wid = job.payload, job.workspace_id
    beat = Heartbeat(job.id)
    if job.kind in ('sync_gmail', 'sync_calendar'):
        return google_sync.run(wid, uuid.UUID(p['stream_id']), beat)
    elif job.kind == 'rebuild_conversation':
        return memory.rebuild_conversation(wid, p['provider'], p.get('conversation_id'))
    elif job.kind == 'embed_chunk':
        return memory.embed_chunk(wid, uuid.UUID(p['chunk_id']), job.id, p.get('space'))
    elif job.kind == 'group_claim':
        return observation_groups.consolidate(wid, uuid.UUID(p['claim_id']), job.id, beat)
    elif job.kind == 'extract_chunk':
        return memory.extract_chunk(wid, uuid.UUID(p['chunk_id']), job.id)
    else:
        raise ValueError('unknown job kind')


def _failed(status: str | None, code: str, exc: BaseException | None = None, **fields) -> None:
    """One event per failed attempt: terminal, retry scheduled, or lease already lost."""
    if status == 'failed':
        security.emit('job_failed_terminal', exc, level='error', code=code, final=True, **fields)
    elif status == 'queued':
        security.emit('job_retry_scheduled', exc, level='warn', code=code, **fields)
    else:
        security.emit('job_lease_lost', level='warn', code=code, **fields)


def _code(exc: BaseException, prefix: str = '') -> str:
    value = prefix + str(exc).split(':')[-1].strip().replace(' ', '_')
    return value if diagnostics.TOKEN.match(value) else prefix.rstrip('_') or 'error'


def run_one(kinds: list[str] | None = None) -> bool:
    with db.session() as s:
        job = jobs.claim(s, OWNER, kinds)
        if job is None:
            return False
        queue_age = max(0, int((utcnow() - job.run_after).total_seconds() * 1000)) if job.run_after else None
        s.expunge(job)
    trace = (job.payload or {}).get('_trace') or {}
    with diagnostics.bind(job_id=str(job.id), operation_id=diagnostics.new_id(),
                          parent_operation_id=diagnostics.valid_id(trace.get('op'))):
        started = time.monotonic()
        base = {'kind': job.kind, 'attempt': job.attempts, 'max_attempts': job.max_attempts, 'queue_age_ms': queue_age}
        elapsed = lambda: int((time.monotonic() - started) * 1000)  # noqa: E731
        security.emit('job_claimed', level='debug', **base)
        try:
            result = handle(job)
        except ai.BudgetExhausted as exc:
            with db.session() as s:
                reason = ('AI paused: daily limit for your API key reached' if exc.scope == 'userkey'
                          else f'AI paused: daily {exc.scope} budget reached')
                jobs.defer(s, job.id, OWNER, ai.tomorrow_start(), reason)
            security.emit('job_deferred', code='budget_exhausted', scope=exc.scope, duration_ms=elapsed(), **base)
            return True
        except LeaseLost:
            security.emit('job_lease_lost', level='warn', code='heartbeat', duration_ms=elapsed(), **base)
            return True
        except GoogleError as exc:
            with db.session() as s:
                # Sync backoff lives on the stream; the job itself does not retry auth/permission errors.
                status = jobs.fail(s, job.id, OWNER, f'google:{exc.kind}', retry=False)
            _failed(status, _code(exc.kind, 'google_'), duration_ms=elapsed(), reason=exc.reason, **base)
            return True
        except ai.InvalidOutput as exc:
            with db.session() as s:
                status = jobs.fail(s, job.id, OWNER, f'invalid model output: {exc}', retry=job.attempts < 2)
            _failed(status, _code(exc), duration_ms=elapsed(), **base)
            return True
        except ai.AIUnavailable as exc:
            if str(exc) == 'AI provider is not configured':
                with db.session() as s:
                    jobs.defer(s, job.id, OWNER, utcnow() + timedelta(seconds=600),
                               'AI waiting: provider configuration')
                security.emit('job_deferred', code='ai_not_configured', duration_ms=elapsed(), **base)
                return True
            if str(exc) == 'rate_limited':
                # Pace backfills: wait for the provider without spending one of the job's attempts.
                wait = min(ai.RATE_LIMIT_MAX_WAIT, exc.retry_after or ai.RATE_LIMIT_DEFAULT_WAIT) + random.uniform(0, 30)
                with db.session() as s:
                    jobs.defer(s, job.id, OWNER, utcnow() + timedelta(seconds=wait), 'AI waiting: provider rate limit')
                security.emit('job_deferred', code='rate_limited', delay_ms=int(wait * 1000), duration_ms=elapsed(), **base)
                return True
            with db.session() as s:
                status = jobs.fail(s, job.id, OWNER, f'ai unavailable: {exc}', delay=600 if 'configured' in str(exc) else None)
            _failed(status, 'ai_not_configured' if 'configured' in str(exc) else _code(exc), duration_ms=elapsed(),
                    http_status=exc.http_status, **base)
            return True
        except Exception as exc:
            ref = security.emit('job_failed', exc, **base)
            with db.session() as s:
                status = jobs.fail(s, job.id, OWNER, f'error reference {ref}')
            _failed(status, 'unexpected', duration_ms=elapsed(), **base)
            return True
        with db.session() as s:
            completed = jobs.complete(s, job.id, OWNER)
        outcome = result if isinstance(result, str) else 'done'
        if completed:
            security.emit('job_completed', outcome=outcome, count=result if isinstance(result, int) else None,
                          duration_ms=elapsed(), **base)
        else:
            security.emit('job_lease_lost', level='warn', code='complete', duration_ms=elapsed(), **base)
    return True


def queue_stats() -> None:
    """Low-cardinality queue and sync health, for alerts on lag, failures and stalls."""
    with db.session() as s:
        queued, running, failed, oldest = s.execute(text("""
            SELECT count(*) FILTER (WHERE status = 'queued' AND run_after <= now()),
                   count(*) FILTER (WHERE status = 'running'),
                   count(*) FILTER (WHERE status = 'failed' AND updated_at > now() - interval '1 hour'),
                   coalesce(extract(epoch FROM now() - min(run_after) FILTER (WHERE status = 'queued' AND run_after <= now())), 0)
            FROM jobs""")).one()
        stalled = s.scalar(text("""
            SELECT count(*) FROM sync_streams st
            JOIN connections c ON c.id = st.connection_id AND c.workspace_id = st.workspace_id AND c.state = 'active'
            JOIN workspaces w ON w.id = st.workspace_id AND w.status = 'active'
            WHERE st.status <> 'disabled' AND (st.next_run_at < now() - interval '30 minutes'
                  OR (st.status = 'syncing' AND st.last_attempt_at < now() - interval '1 hour'))"""))
    security.emit('queue_stats', queued=queued, running=running, failed=failed,
                  oldest_queue_age_ms=int(float(oldest) * 1000), stalled_streams=stalled)


def schedule() -> int:
    """Enqueue due Google syncs (idempotent per due time)."""
    n = 0
    with db.session() as s:
        rows = s.execute(select(SyncStream, Connection).join(Connection, and_(Connection.id == SyncStream.connection_id,
                                                                            Connection.workspace_id == SyncStream.workspace_id))
                         .join(Workspace, Workspace.id == SyncStream.workspace_id)
                         .where(Workspace.status == 'active', Connection.state == 'active',
                                SyncStream.next_run_at.is_not(None), SyncStream.next_run_at <= utcnow(),
                                SyncStream.status != 'disabled')).all()
        for stream, conn in rows:
            if accounts.STREAM_SCOPES[stream.stream] not in conn.scopes:
                continue
            busy = s.scalar(select(exists().where(Job.workspace_id == stream.workspace_id,
                                                  Job.kind == 'sync_' + stream.stream, Job.status.in_(['queued', 'running']),
                                                  Job.payload['stream_id'].astext == str(stream.id))))
            if busy:
                continue
            key = f'sync:{stream.id}:{stream.next_run_at.isoformat()}'
            n += jobs.enqueue(s, stream.workspace_id, 'sync_' + stream.stream, key, {'stream_id': str(stream.id)},
                              max_attempts=3)
    return n


def reconcile_ai(batch: int = 200) -> int:
    """Chunks missing an embedding for the configured model (new chunks, crashes, or a model
    change = reindex) or never extracted get idempotent jobs."""
    targets = ai.embedding_targets()
    generation_ready = ai.provider().can_generate()
    n = 0
    with db.session() as s:
        legacy = s.execute(select(Chunk.workspace_id, Chunk.provider, Chunk.conversation_id).where(
            Chunk.pipeline_version != PIPELINE_VERSION).distinct().limit(batch)).all()
        for wid, provider, conversation in legacy:
            key = 'pipeline:' + memory.h(PIPELINE_VERSION, provider, conversation)
            n += jobs.enqueue(s, wid, 'rebuild_conversation', key,
                              {'provider': provider, 'conversation_id': conversation})
        for space, p in targets.items():
            missing = s.execute(select(Chunk.workspace_id, Chunk.id, Chunk.chunk_key).where(
                Chunk.pipeline_version == PIPELINE_VERSION, ~exists().where(
                Embedding.workspace_id == Chunk.workspace_id, Embedding.chunk_id == Chunk.id,
                Embedding.space == space)).limit(batch)).all()
            for wid, cid, key in missing:
                n += jobs.enqueue(s, wid, 'embed_chunk', f'embed:{key}:{space}', {'chunk_id': str(cid), 'space': space})
        pending = s.execute(select(Chunk.workspace_id, Chunk.id, Chunk.chunk_key).where(
            Chunk.pipeline_version == PIPELINE_VERSION, Chunk.extracted_at.is_(None),
            Chunk.provider.in_(sorted(memory.EXTRACT_PROVIDERS))).limit(batch)).all()
        for wid, cid, key in pending:
            if generation_ready:
                # Recover only configuration failures for still-current, unextracted chunks.
                # Never revive cancellations or malformed-output failures.
                job = s.scalar(select(Job).where(
                    Job.workspace_id == wid, Job.kind == 'extract_chunk',
                    Job.idempotency_key == f'extract:{key}',
                    Job.status == 'failed',
                    Job.last_error == 'ai unavailable: AI provider is not configured',
                    exists().where(Workspace.id == wid, Workspace.status == 'active')
                ).with_for_update(skip_locked=True))
                if job is not None:
                    job.status, job.attempts, job.run_after = 'queued', 0, utcnow()
                    job.lease_owner = job.lease_expires_at = job.last_error = None
                    security.emit('job_retry_scheduled', job_id=job.id, kind=job.kind,
                                  code='ai_configuration_restored')
                    n += 1
            n += jobs.enqueue(s, wid, 'extract_chunk', f'extract:{key}', {'chunk_id': str(cid)})
        if generation_ready and ai.embedder().embedding_dimension:
            from app.repo import Scoped
            rev = observation_groups.revision()
            pending_claims = s.scalars(select(Claim).where(Claim.auto_group.is_(True),
                (Claim.grouping_revision.is_(None)) | (Claim.grouping_revision != rev))
                .order_by(Claim.created_at, Claim.id).limit(batch)).all()
            for claim in pending_claims:
                n += observation_groups.enqueue(Scoped(s, claim.workspace_id), claim)
        # Cut over only after the complete pending space is covered. Chunks are locked
        # before the final check so an ingestion commit cannot be missed.
        values = operator_settings.values(s)
        pending_model = values.get(operator_settings.PENDING_EMBEDDING_MODEL)
        if pending_model:
            pending = next((p for p in targets.values() if p.embedding_model == pending_model and p.embedding_space != ai.embedder().embedding_space), None)
            if pending:
                s.execute(select(Chunk.id).where(Chunk.pipeline_version == PIPELINE_VERSION).with_for_update()).all()
                uncovered = s.scalar(select(Chunk.id).where(Chunk.pipeline_version == PIPELINE_VERSION, ~exists().where(
                    Embedding.workspace_id == Chunk.workspace_id, Embedding.chunk_id == Chunk.id,
                    Embedding.space == pending.embedding_space)).limit(1))
                total = s.scalar(select(func.count()).select_from(Chunk).where(Chunk.pipeline_version == PIPELINE_VERSION))
                covered = s.scalar(select(func.count()).select_from(Chunk).where(
                    Chunk.pipeline_version == PIPELINE_VERSION, exists().where(
                        Embedding.workspace_id == Chunk.workspace_id, Embedding.chunk_id == Chunk.id,
                        Embedding.space == pending.embedding_space)))
                security.emit('embedding_rebuild_progress', space=pending.embedding_space, covered=covered, total=total)
                if uncovered is None:
                    operator_settings.promote_pending(s)
                    security.emit('embedding_cutover', audit=True, space=pending.embedding_space, total=total,
                                  revision=operator_settings.revision(s))
    security.emit('ai_reconcile', level='debug', enqueued=n, count=len(targets))
    return n


def drain(max_jobs: int = 1000, kinds: list[str] | None = None) -> int:
    """Process jobs until none are due (used by tests and the demo seed)."""
    done = 0
    while done < max_jobs and run_one(kinds):
        done += 1
    return done


def main() -> None:
    config.get()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    last_schedule = last_reconcile = last_stats = 0.0
    security.emit('worker_started')
    while not STOP.is_set():
        now = time.monotonic()
        try:
            if now - last_schedule > 15:
                with diagnostics.operation():
                    n = schedule()
                    if n:
                        security.emit('sync_scheduled', count=n)
                last_schedule = now
            if now - last_reconcile > 600:
                with diagnostics.operation():
                    reconcile_ai()
                last_reconcile = now
            if now - last_stats > 60:
                last_stats = now
                queue_stats()
            if not run_one():
                STOP.wait(2)
        except Exception as exc:
            security.emit('worker_loop_failed', exc)
            STOP.wait(5)
    security.emit('worker_stopped')


if __name__ == '__main__':
    main()
