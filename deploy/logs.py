#!/usr/bin/env python3
"""Moresocial diagnostic log store: search, retention, alerts and scoped debug (host tool, stdlib only).

Layout under /srv/moresocial/logs (root 0750), outside every container's lifetime:
  app/                   web, worker, migrate: <service>-<YYYYMMDD>-<host>-<seg>.jsonl  (uid 10001)
  connectors/<id>/       one per workspace connector: events.jsonl[.1-4]                 (uid 1000)
  host/                  provisioner, backup, release and this tool: <service>-<YYYYMMDD>.jsonl
  app/debug.json         optional short-lived scoped DEBUG (see `debug`)

Every line is one sanitized JSON event (app/diagnostics.py schema). Examples:
  sudo python3 /srv/moresocial/src/deploy/logs.py find 6b41cd8b25b94cbda19266902d2fbe52
  sudo python3 /srv/moresocial/src/deploy/logs.py tail --service web --level warn -n 50
  sudo python3 /srv/moresocial/src/deploy/logs.py status
  sudo python3 /srv/moresocial/src/deploy/logs.py debug --service web --events admin_,provider_ --minutes 30
Retention and alerts run hourly from moresocial-logs.timer (`prune` then `check`).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(os.environ.get('MORESOCIAL_LOG_DIR', '/srv/moresocial/logs'))
READY_URL = os.environ.get('MORESOCIAL_READY_URL', 'http://127.0.0.1:8772/ready')
ID_FIELDS = ('reference', 'request_id', 'operation_id', 'parent_operation_id', 'job_id', 'workspace_id')
LEVELS = {'debug': 10, 'info': 20, 'warn': 30, 'error': 40}
SAFE = re.compile(r'^[A-Za-z0-9_.:/-]{1,120}$')


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def parse_ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


def files(root: Path = ROOT) -> list[Path]:
    if not root.exists():
        return []
    return sorted((p for p in root.rglob('*') if p.is_file() and '.jsonl' in p.name), key=lambda p: p.stat().st_mtime)


def read(paths, since: datetime | None = None, needle: str | None = None):
    """Yield events from files modified since `since` (cheap substring filter first)."""
    cutoff = since.timestamp() if since else 0
    for path in paths:
        try:
            if path.stat().st_mtime < cutoff:
                continue
            with path.open('r', errors='replace') as fh:
                for line in fh:
                    if needle and needle not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        if since is not None:
                            ts = parse_ts(row.get('ts'))
                            if ts is None or ts < since:
                                continue
                        yield row
        except OSError:
            continue


def emit(event: str, level: str = 'info', service: str = 'host', root: Path = ROOT, **fields) -> dict:
    """Append one event to the host store (and stdout, for the journal). Never raises."""
    row = {'schema': 1, 'ts': iso(now()), 'level': level if level in LEVELS else 'info', 'service': service,
           'event': event if re.fullmatch(r'[a-z][a-z0-9_]{0,79}', event) else 'invalid_event',
           'reference': secrets.token_hex(8)}
    release = os.environ.get('MORESOCIAL_RELEASE', '')
    if re.fullmatch(r'[0-9a-f]{7,40}', release):
        row['release'] = release
    for key, value in fields.items():
        if value is None or not re.fullmatch(r'[a-z][a-z0-9_]{0,40}', key):
            continue
        if isinstance(value, bool) or isinstance(value, int):
            row[key] = value
        elif isinstance(value, float) and value == value:
            row[key] = round(value, 3)
        elif isinstance(value, str) and SAFE.match(value):
            row[key] = value
    line = json.dumps(row, separators=(',', ':')) + '\n'
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except Exception:
        pass
    try:
        host = root / 'host'
        if host.is_dir():
            fd = os.open(host / f'{service}-{now():%Y%m%d}.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode())
            finally:
                os.close(fd)
    except OSError:
        pass
    return row


# ---------------- search ----------------

def find(ident: str, root: Path = ROOT, days: int = 30) -> list[dict]:
    """Events carrying `ident` in any correlation field, plus one hop of related operations."""
    if not re.fullmatch(r'[0-9a-f-]{16,36}', ident):
        raise SystemExit('expected a reference, request, operation, job or workspace ID')
    since = now() - timedelta(days=days)
    paths = files(root)
    direct = [r for r in read(paths, since, ident) if any(r.get(k) == ident for k in ID_FIELDS)]
    related = {r.get(k) for r in direct for k in ('request_id', 'operation_id') if r.get(k)} - {ident}
    rows = {json.dumps(r, sort_keys=True): r for r in direct}
    if related:
        for r in read(paths, since):
            if any(r.get(k) in related for k in ('request_id', 'operation_id', 'parent_operation_id')):
                rows[json.dumps(r, sort_keys=True)] = r
    return sorted(rows.values(), key=sort_key)


def sort_key(row: dict):
    return parse_ts(row.get('ts')) or datetime.min.replace(tzinfo=timezone.utc)


def show(rows, as_json: bool) -> None:
    for r in rows:
        if as_json:
            print(json.dumps(r, separators=(',', ':')))
            continue
        head = f"{r.get('ts', '?')} {r.get('level', '?'):5} {r.get('service', '?'):11} {r.get('event', '?')}"
        rest = ' '.join(f'{k}={v}' for k, v in r.items() if k not in ('schema', 'ts', 'level', 'service', 'event'))
        print(head + ' ' + rest)


# ---------------- retention ----------------

def usage(root: Path = ROOT) -> tuple[int, int]:
    paths = files(root)
    return len(paths), sum(p.stat().st_size for p in paths)


def prune(root: Path = ROOT, days: int = 14, max_bytes: int = 1024 ** 3) -> dict:
    """Delete events older than `days`, then the oldest files until the store fits `max_bytes`."""
    cutoff = time.time() - days * 86400
    deleted = freed = 0
    for path in files(root):
        if path.stat().st_mtime < cutoff:
            freed += path.stat().st_size
            path.unlink(missing_ok=True)
            deleted += 1
    paths = files(root)
    total = sum(p.stat().st_size for p in paths)
    for path in paths:  # oldest first
        if total <= max_bytes:
            break
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        total -= size
        freed += size
        deleted += 1
    # Empty connector directories stay: one may be bind-mounted into a running connector.
    count, total = usage(root)
    free = shutil.disk_usage(root).free if root.exists() else 0
    emit('log_retention', root=root, files=count, bytes=total, deleted_files=deleted, freed_bytes=freed, free_bytes=free,
         window_s=days * 86400, threshold=max_bytes)
    return {'files': count, 'bytes': total, 'deleted_files': deleted, 'freed_bytes': freed, 'free_bytes': free}


# ---------------- alerts ----------------

RULES = {
    # rule: (level, threshold, description)
    'error_rate': ('error', 20, 'error-level events in the window'),
    'terminal_jobs': ('warn', 1, 'jobs that failed terminally'),
    'queue_lag': ('warn', 30 * 60 * 1000, 'oldest due job age (ms)'),
    'sync_stalls': ('warn', 1, 'overdue or stuck Google sync streams'),
    'provider_failures': ('warn', 5, 'failed provider calls'),
    'backup_failures': ('error', 1, 'failed or missing daily backups'),
    'readiness': ('error', 1, 'failed readiness probe'),
    'dropped_events': ('warn', 1, 'diagnostic events the store could not write'),
    'disk_pressure': ('error', 1, 'log disk nearly full or store over its cap'),
    'collector_silent': ('warn', 1, 'no worker queue_stats reached the store (collector or worker down)'),
}
REALERT_SECONDS = 6 * 3600


def readiness() -> tuple[bool, int | None]:
    try:
        with urllib.request.urlopen(READY_URL, timeout=10) as r:
            return r.status == 200, r.status
    except urllib.error.HTTPError as exc:
        return False, exc.code
    except Exception:
        return False, None


def evaluate(root: Path = ROOT, window_minutes: int = 60, max_bytes: int = 1024 ** 3, probe=readiness) -> dict[str, int]:
    """Measure every rule over the window; returns rule -> observed value."""
    since = now() - timedelta(minutes=window_minutes)
    rows = list(read(files(root), since))
    stats = [r for r in rows if r.get('event') == 'queue_stats']
    latest = max(stats, key=sort_key) if stats else {}
    ok, status = probe()
    emit('readiness_probe', root=root, ok=ok, status=status, level='info' if ok else 'error')
    day = list(read(files(root / 'host'), now() - timedelta(hours=26))) if (root / 'host').exists() else []
    backups_ok = sum(1 for r in day if r.get('event') == 'backup_completed')
    backups_failed = sum(1 for r in day if r.get('event') == 'backup_failed')
    # A missing daily backup counts only once the store has existed for a full backup cycle.
    stamp = root / 'host' / 'installed'
    try:
        installed = float(stamp.read_text())
    except (OSError, ValueError):
        installed = time.time()
        try:
            stamp.write_text(str(installed))
        except OSError:
            pass
    expect_backup = time.time() - installed > 26 * 3600
    count, total = usage(root)
    disk = shutil.disk_usage(root) if root.exists() else None
    return {
        'error_rate': sum(1 for r in rows if r.get('level') == 'error'),
        'terminal_jobs': sum(1 for r in rows if r.get('event') == 'job_failed_terminal'),
        'queue_lag': int(latest.get('oldest_queue_age_ms') or 0),
        'sync_stalls': int(latest.get('stalled_streams') or 0),
        'provider_failures': sum(1 for r in rows if r.get('event') == 'provider_call' and r.get('outcome') == 'failed'),
        'backup_failures': backups_failed + (1 if not backups_ok and expect_backup else 0),
        'readiness': 0 if ok else 1,
        'dropped_events': sum(int(r.get('dropped') or 0) for r in rows),
        'disk_pressure': int(bool(disk and disk.free < 0.1 * disk.total) or total > 0.9 * max_bytes),
        'collector_silent': 0 if stats else 1,
    }


def check(root: Path = ROOT, window_minutes: int = 60, max_bytes: int = 1024 ** 3, probe=readiness) -> list[dict]:
    """Emit deduplicated `alert` events (one per rule per 6 h, with suppressed counts)."""
    observed = evaluate(root, window_minutes, max_bytes, probe)
    state_file = root / 'host' / 'alerts-state.json'
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        state = {}
    fired, stamp = [], time.time()
    for rule, (level, threshold, _) in RULES.items():
        value = observed[rule]
        entry = state.get(rule) or {}
        if value < threshold:
            if entry.get('active'):
                emit('alert_resolved', root=root, rule=rule, count=value)
            state[rule] = {'active': False}
            continue
        if entry.get('active') and stamp - entry.get('at', 0) < REALERT_SECONDS:
            entry['suppressed'] = entry.get('suppressed', 0) + 1
            state[rule] = entry
            fired.append({'rule': rule, 'count': value, 'suppressed': True})
            continue
        emit('alert', root=root, level=level, rule=rule, count=value, threshold=threshold,
             suppressed=entry.get('suppressed') or None, window_s=window_minutes * 60)
        state[rule] = {'active': True, 'at': stamp, 'suppressed': 0}
        fired.append({'rule': rule, 'count': value, 'suppressed': False})
    try:
        if state_file.parent.is_dir():
            tmp = state_file.with_suffix('.tmp')
            tmp.write_text(json.dumps(state))
            os.replace(tmp, state_file)
    except OSError:
        pass
    return fired


# ---------------- scoped debug ----------------

def debug(root: Path, services: list[str], events: list[str], minutes: int) -> dict:
    minutes = max(1, min(120, minutes))
    scope = {'until': time.time() + minutes * 60, 'services': services, 'events': events}
    path = root / 'app' / 'debug.json'
    path.write_text(json.dumps(scope))
    os.chmod(path, 0o644)
    emit('debug_scope_set', root=root, count=minutes, target=','.join(services) or 'all')
    return scope


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=ROOT)
    sub = parser.add_subparsers(dest='command', required=True)
    f = sub.add_parser('find', help='events for a reference/request/operation/job/workspace ID')
    f.add_argument('id')
    f.add_argument('--days', type=int, default=30)
    f.add_argument('--json', action='store_true')
    t = sub.add_parser('tail', help='recent events')
    t.add_argument('-n', type=int, default=40)
    t.add_argument('--service')
    t.add_argument('--level', default='debug')
    t.add_argument('--event')
    t.add_argument('--hours', type=int, default=24)
    t.add_argument('--json', action='store_true')
    sub.add_parser('status', help='store size, newest event per service, dropped counts')
    p = sub.add_parser('prune', help='enforce time retention and the disk cap')
    p.add_argument('--days', type=int, default=int(os.environ.get('LOG_RETENTION_DAYS', '14')))
    p.add_argument('--max-mb', type=int, default=int(os.environ.get('LOG_MAX_MB', '1024')))
    c = sub.add_parser('check', help='evaluate alert rules; exit 2 while any alert is active')
    c.add_argument('--window-minutes', type=int, default=60)
    c.add_argument('--max-mb', type=int, default=int(os.environ.get('LOG_MAX_MB', '1024')))
    d = sub.add_parser('debug', help='enable scoped DEBUG for a short time (max 120 minutes)')
    d.add_argument('--service', action='append', default=[])
    d.add_argument('--events', default='')
    d.add_argument('--minutes', type=int, default=30)
    e = sub.add_parser('emit', help='append one host event (used by backup/release scripts)')
    e.add_argument('event')
    e.add_argument('--level', default='info')
    e.add_argument('--service', default='host')
    e.add_argument('--field', action='append', default=[], help='key=value (integers are kept as numbers)')
    args = parser.parse_args(argv)
    root = args.root
    if args.command == 'find':
        show(find(args.id, root, args.days), args.json)
    elif args.command == 'tail':
        since = now() - timedelta(hours=args.hours)
        rows = [r for r in read(files(root), since) if (not args.service or r.get('service') == args.service)
                and LEVELS.get(r.get('level'), 0) >= LEVELS.get(args.level, 0) and (not args.event or r.get('event') == args.event)]
        show(sorted(rows, key=sort_key)[-args.n:], args.json)
    elif args.command == 'status':
        count, total = usage(root)
        newest: dict[str, str] = {}
        dropped = 0
        for r in read(files(root), now() - timedelta(days=1)):
            service = str(r.get('service'))
            newest[service] = max(newest.get(service, ''), str(r.get('ts')))
            dropped += int(r.get('dropped') or 0)
        print(json.dumps({'files': count, 'bytes': total, 'newest': newest, 'dropped_24h': dropped,
                          'free_bytes': shutil.disk_usage(root).free if root.exists() else None}, indent=2))
    elif args.command == 'prune':
        print(json.dumps(prune(root, args.days, args.max_mb * 1024 * 1024)))
    elif args.command == 'check':
        fired = check(root, args.window_minutes, args.max_mb * 1024 * 1024)
        print(json.dumps(fired))
        return 2 if fired else 0
    elif args.command == 'debug':
        prefixes = [p for p in args.events.split(',') if p]
        print(json.dumps(debug(root, args.service, prefixes, args.minutes)))
    elif args.command == 'emit':
        fields = {}
        for item in args.field:
            key, _, value = item.partition('=')
            fields[key] = int(value) if re.fullmatch(r'-?\d{1,15}', value) else value
        emit(args.event, args.level, args.service, root, **fields)
    return 0


if __name__ == '__main__':
    sys.exit(main())
