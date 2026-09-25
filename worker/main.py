"""Background worker: sync, chunking, embeddings and extraction jobs.

Run with `python -m worker.main`. Several workers may run; claims are atomic and leases
recover jobs from crashed workers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import signal
import socket
import threading
import time
import uuid

from sqlalchemy import and_, exists, select, text

from app import accounts, ai, config, db, google_sync, jobs, memory, security
from app.google import GoogleError
from app.models import Chunk, Connection, Embedding, Job, SyncStream, Workspace, PIPELINE_VERSION

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


def handle(job: Job) -> None:
    p, wid = job.payload, job.workspace_id
    beat = Heartbeat(job.id)
    if job.kind in ('sync_gmail', 'sync_calendar'):
        google_sync.run(wid, uuid.UUID(p['stream_id']), beat)
    elif job.kind == 'rebuild_conversation':
        memory.rebuild_conversation(wid, p['provider'], p.get('conversation_id'))
    elif job.kind == 'embed_chunk':
        memory.embed_chunk(wid, uuid.UUID(p['chunk_id']), job.id)
    elif job.kind == 'extract_chunk':
        memory.extract_chunk(wid, uuid.UUID(p['chunk_id']), job.id)
    else:
        raise ValueError('unknown job kind')


def run_one(kinds: list[str] | None = None) -> bool:
    with db.session() as s:
        job = jobs.claim(s, OWNER, kinds)
        if job is None:
            return False
        s.expunge(job)
    try:
        handle(job)
    except ai.BudgetExhausted as exc:
        with db.session() as s:
            reason = ('AI paused: daily limit for your API key reached' if exc.scope == 'userkey'
                      else f'AI paused: daily {exc.scope} budget reached')
            jobs.defer(s, job.id, OWNER, ai.tomorrow_start(), reason)
        return True
    except LeaseLost:
        return True
    except GoogleError as exc:
        with db.session() as s:
            # Sync backoff lives on the stream; the job itself does not retry auth/permission errors.
            jobs.fail(s, job.id, OWNER, f'google:{exc.kind}', retry=False)
        return True
    except ai.InvalidOutput as exc:
        with db.session() as s:
            jobs.fail(s, job.id, OWNER, f'invalid model output: {exc}', retry=job.attempts < 2)
        return True
    except ai.AIUnavailable as exc:
        with db.session() as s:
            jobs.fail(s, job.id, OWNER, f'ai unavailable: {exc}', delay=600 if 'configured' in str(exc) else None)
        return True
    except Exception as exc:
        ref = security.emit('job_failed', exc, kind=job.kind, attempt=job.attempts)
        with db.session() as s:
            jobs.fail(s, job.id, OWNER, f'error reference {ref}')
        return True
    with db.session() as s:
        jobs.complete(s, job.id, OWNER)
    return True


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
    p = ai.embedder()
    n = 0
    with db.session() as s:
        legacy = s.execute(select(Chunk.workspace_id, Chunk.provider, Chunk.conversation_id).where(
            Chunk.pipeline_version != PIPELINE_VERSION).distinct().limit(batch)).all()
        for wid, provider, conversation in legacy:
            key = 'pipeline:' + memory.h(PIPELINE_VERSION, provider, conversation)
            n += jobs.enqueue(s, wid, 'rebuild_conversation', key,
                              {'provider': provider, 'conversation_id': conversation})
        if p.embedding_dimension:
            missing = s.execute(select(Chunk.workspace_id, Chunk.id, Chunk.chunk_key).where(
                Chunk.pipeline_version == PIPELINE_VERSION, ~exists().where(
                Embedding.workspace_id == Chunk.workspace_id, Embedding.chunk_id == Chunk.id,
                Embedding.model == p.embedding_model)).limit(batch)).all()
            for wid, cid, key in missing:
                n += jobs.enqueue(s, wid, 'embed_chunk', f'embed:{key}:{p.embedding_model}', {'chunk_id': str(cid)})
        pending = s.execute(select(Chunk.workspace_id, Chunk.id, Chunk.chunk_key).where(
            Chunk.pipeline_version == PIPELINE_VERSION, Chunk.extracted_at.is_(None),
            Chunk.provider.in_(sorted(memory.EXTRACT_PROVIDERS))).limit(batch)).all()
        for wid, cid, key in pending:
            n += jobs.enqueue(s, wid, 'extract_chunk', f'extract:{key}', {'chunk_id': str(cid)})
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
    last_schedule = last_reconcile = 0.0
    security.emit('worker_started')
    while not STOP.is_set():
        now = time.monotonic()
        try:
            if now - last_schedule > 15:
                schedule()
                last_schedule = now
            if now - last_reconcile > 600:
                reconcile_ai()
                last_reconcile = now
            if not run_one():
                STOP.wait(2)
        except Exception as exc:
            security.emit('worker_loop_failed', exc)
            STOP.wait(5)


if __name__ == '__main__':
    main()
