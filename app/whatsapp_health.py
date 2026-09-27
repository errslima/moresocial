"""WhatsApp sync health from connector heartbeats: validation and one pure classifier.

The connector reports roughly every minute (and after state changes) to the internal API.
Freshness is judged by server receipt time (`health_checked_at`), never by a timestamp the
connector supplies, and never by `whatsapp_connectors.updated_at`, which only records row
changes. A quiet chat is not stale sync; the 15-minute history sweep has its own, looser
overdue rule. Suggestions and Connections both use `classify`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

FRESH = timedelta(minutes=3)            # three missed 60-second heartbeats
SWEEP_OVERDUE = timedelta(minutes=60)   # four missed 15-minute history sweeps
PENDING_GRACE_SECONDS = 60              # packets normally drain within seconds
STATES = {'starting', 'pairing', 'loading', 'ready', 'disconnected', 'auth_failure', 'error'}
OUTCOMES = {'ok', 'partial', 'failed'}
ERROR_CODES = {'ingest_unavailable', 'sync_partial', 'sync_failed', 'browser_failed'}
MAX_COUNT = 1_000_000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _count(value, field):
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_COUNT:
        raise ValueError(field)
    return value


def _flag(value, field):
    if not isinstance(value, bool):
        raise ValueError(field)
    return value


def _time(value, field):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError(field)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(field) from None
    if parsed.tzinfo is None:
        raise ValueError(field)
    return parsed.astimezone(timezone.utc).isoformat()


def validate(report: dict) -> dict:
    """Strict, bounded copy of a heartbeat. Raises ValueError on anything malformed;
    unknown keys are dropped so nothing unexpected (text, IDs, QR data) is stored."""
    if not isinstance(report, dict) or report.get('version') != 1:
        raise ValueError('version')
    state = report.get('state')
    if state not in STATES:
        raise ValueError('state')
    sweep, history = report.get('sweep'), report.get('history')
    if not isinstance(sweep, dict) or not isinstance(history, dict):
        raise ValueError('shape')
    outcome = sweep.get('outcome')
    if outcome is not None and outcome not in OUTCOMES:
        raise ValueError('outcome')
    code = report.get('error_code')
    if code is not None and code not in ERROR_CODES:
        raise ValueError('error_code')
    return {'version': 1, 'state': state, 'pending': _count(report.get('pending'), 'pending'),
            'pending_age_s': _count(report.get('pending_age_s'), 'pending_age_s'),
            'ingest_failing': _flag(report.get('ingest_failing'), 'ingest_failing'),
            'syncing': _flag(report.get('syncing'), 'syncing'),
            'sweep': {'last_at': _time(sweep.get('last_at'), 'last_at'), 'outcome': outcome,
                      'failed_chats': _count(sweep.get('failed_chats'), 'failed_chats'),
                      'last_complete_at': _time(sweep.get('last_complete_at'), 'last_complete_at')},
            'history': {k: _count(history.get(k), k) for k in ('chats_total', 'history_chats', 'history_done', 'chats_synced')},
            'error_code': code}


@dataclass
class Health:
    code: str        # ok | history_loading | not_linked | unknown | disconnected | needs_pairing | connecting | delayed | incomplete
    level: str       # ok | info | warn | action
    message: str
    checked_at: datetime | None = None
    last_sweep_at: datetime | None = None
    last_complete_at: datetime | None = None
    history_done: int | None = None
    history_chats: int | None = None

    @property
    def warning(self) -> bool:
        return self.level in ('warn', 'action')


def _parse(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def classify(connector, now: datetime | None = None) -> Health:
    """Pure: the stored connector row (or None) and the current time give the user-facing state."""
    now = now or utcnow()
    if connector is None or connector.desired != 'running':
        return Health('not_linked', 'action', 'WhatsApp is not linked, so new messages are not imported.')
    checked, report = connector.health_checked_at, connector.health or {}
    if checked is None or report.get('version') != 1:
        return Health('unknown', 'warn', 'WhatsApp status is unavailable: the connection has not reported its health yet. '
                      'New replies may be missing.')
    sweep, history = report.get('sweep') or {}, report.get('history') or {}
    base = dict(checked_at=checked, last_sweep_at=_parse(sweep.get('last_at')),
                last_complete_at=_parse(sweep.get('last_complete_at')),
                history_done=history.get('history_done'), history_chats=history.get('history_chats'))
    if now - checked > FRESH:
        return Health('unknown', 'warn', 'WhatsApp status is unavailable: no recent report from the connection. '
                      'New replies may be missing.', **base)
    state = report.get('state')
    if state in ('disconnected', 'auth_failure'):
        return Health('disconnected', 'action', 'WhatsApp is disconnected. New messages are not being imported.', **base)
    if state == 'pairing':
        return Health('needs_pairing', 'action', 'WhatsApp needs to be linked again from Connections.', **base)
    if state in ('starting', 'loading'):
        return Health('connecting', 'warn', 'WhatsApp is reconnecting. New messages may be delayed.', **base)
    if state != 'ready':
        return Health('delayed', 'warn', 'WhatsApp reports a connection problem and is retrying. New messages may be delayed.', **base)
    if report.get('ingest_failing') or (report.get('pending', 0) > 0 and report.get('pending_age_s', 0) >= PENDING_GRACE_SECONDS):
        return Health('delayed', 'warn', 'WhatsApp sync is delayed: some messages are waiting to be stored and will retry.', **base)
    if sweep.get('outcome') in ('partial', 'failed'):
        failed = sweep.get('failed_chats') or 0
        detail = f'{failed} chat(s) could not be checked' if failed else 'the check failed'
        return Health('incomplete', 'warn', f'WhatsApp sync is incomplete: in the last history check {detail}. It will retry.',
                      **base)
    last_sweep = base['last_sweep_at']
    done, total = history.get('history_done', 0), history.get('history_chats', 0)
    if last_sweep is None or (report.get('syncing') and done < total):
        progress = f' ({done} of {total} chats)' if total else ''
        return Health('history_loading', 'info', f'WhatsApp is connected and still loading history{progress}. '
                      'Older messages may not be available yet.', **base)
    if not report.get('syncing') and now - last_sweep > SWEEP_OVERDUE:
        return Health('delayed', 'warn', 'WhatsApp is connected, but its periodic history check is overdue.', **base)
    return Health('ok', 'ok', 'WhatsApp is connected and syncing.', **base)
