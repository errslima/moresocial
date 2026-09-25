"""PostgreSQL job queue with leases, retries and idempotency keys.

Jobs are claimed atomically with FOR UPDATE SKIP LOCKED. A crashed worker's job is
recovered once its lease expires. Enqueueing is part of the caller's transaction, so
a source update and the jobs it implies commit together.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .models import Job

LEASE_SECONDS = 120


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(s: Session, workspace_id: uuid.UUID, kind: str, key: str, payload: dict | None = None,
            run_after: datetime | None = None, max_attempts: int = 5) -> bool:
    """Insert a job unless one with the same (workspace, idempotency key) exists. Returns True if new."""
    values = dict(id=uuid.uuid4(), workspace_id=workspace_id, kind=kind, idempotency_key=key[:200],
                  payload=payload or {}, status='queued', attempts=0, max_attempts=max_attempts,
                  run_after=run_after or utcnow())
    result = s.execute(insert(Job).values(**values).on_conflict_do_nothing(
        index_elements=['workspace_id', 'idempotency_key']))
    return bool(result.rowcount)


CLAIM_SQL = text("""
UPDATE jobs SET status = 'running', attempts = jobs.attempts + 1, lease_owner = :owner,
       lease_expires_at = now() + make_interval(secs => :lease), updated_at = now()
WHERE id = (
    SELECT j.id FROM jobs j JOIN workspaces w ON w.id = j.workspace_id
    WHERE w.status = 'active'
      AND ((j.status = 'queued' AND j.run_after <= now())
           OR (j.status = 'running' AND j.lease_expires_at < now()))
      AND j.attempts < j.max_attempts
      AND (CAST(:kinds AS text[]) IS NULL OR j.kind = ANY(CAST(:kinds AS text[])))
    ORDER BY j.run_after
    FOR UPDATE OF j SKIP LOCKED
    LIMIT 1)
RETURNING id
""")


def claim(s: Session, owner: str, kinds: list[str] | None = None, lease: int = LEASE_SECONDS) -> Job | None:
    # Expired leases whose attempts are exhausted become failed, not silently retried forever.
    s.execute(text("""UPDATE jobs SET status='failed', last_error='Lease expired after final attempt', updated_at=now()
                      WHERE status='running' AND lease_expires_at < now() AND attempts >= max_attempts"""))
    row = s.execute(CLAIM_SQL, {'owner': owner, 'lease': lease, 'kinds': kinds}).first()
    if not row:
        return None
    return s.get(Job, row[0], populate_existing=True)


def renew(s: Session, job_id: uuid.UUID, owner: str, lease: int = LEASE_SECONDS) -> bool:
    r = s.execute(update(Job).where(Job.id == job_id, Job.lease_owner == owner, Job.status == 'running')
                  .values(lease_expires_at=utcnow() + timedelta(seconds=lease)))
    return bool(r.rowcount)


def complete(s: Session, job_id: uuid.UUID, owner: str) -> bool:
    r = s.execute(update(Job).where(Job.id == job_id, Job.lease_owner == owner, Job.status == 'running')
                  .values(status='done', lease_owner=None, lease_expires_at=None, last_error=None))
    return bool(r.rowcount)


def retry_delay(attempts: int) -> int:
    return min(3600, 30 * 2 ** max(0, attempts - 1))


def fail(s: Session, job_id: uuid.UUID, owner: str, error: str, retry: bool = True, delay: int | None = None) -> None:
    job = s.get(Job, job_id, populate_existing=True)
    if job is None or job.lease_owner != owner:
        return
    final = not retry or job.attempts >= job.max_attempts
    job.status = 'failed' if final else 'queued'
    job.last_error = error[:300]
    job.lease_owner = None
    job.lease_expires_at = None
    if not final:
        job.run_after = utcnow() + timedelta(seconds=delay if delay is not None else retry_delay(job.attempts))


def defer(s: Session, job_id: uuid.UUID, owner: str, until: datetime, reason: str) -> None:
    """Pause without consuming an attempt (e.g. AI budget exhausted until tomorrow)."""
    s.execute(update(Job).where(Job.id == job_id, Job.lease_owner == owner)
              .values(status='queued', run_after=until, attempts=Job.attempts - 1, lease_owner=None,
                      lease_expires_at=None, last_error=reason[:300]))


def cancel(s: Session, workspace_id: uuid.UUID, kinds: list[str] | None = None) -> int:
    q = update(Job).where(Job.workspace_id == workspace_id, Job.status.in_(['queued', 'running']))
    if kinds:
        q = q.where(Job.kind.in_(kinds))
    return s.execute(q.values(status='cancelled', lease_owner=None, lease_expires_at=None)).rowcount


def still_leased(s: Session, job_id: uuid.UUID, owner: str) -> bool:
    return s.execute(select(Job.id).where(Job.id == job_id, Job.lease_owner == owner, Job.status == 'running')).first() is not None
