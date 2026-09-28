"""Structured, sanitized diagnostic events shared by web, worker and migrations.

One JSON object per line on stderr, and (when ``DIAGNOSTICS_DIR`` is mounted) in a
rotating file store on the host that outlives containers. Every field is validated by a
field-specific rule; anything unknown or malformed is dropped. Keys, cookies, OAuth
codes, authorization headers, URLs, prompts, messages, embeddings and provider response
bodies are never accepted. Exceptions contribute their class name and bounded
``file:function:line`` frames only: no messages, locals or source text.

Correlation comes from context variables: ``request_id`` (ingress), ``operation_id``
(a unit of work inside a request or job), ``parent_operation_id`` and ``job_id``.
Logging never raises and is never a prerequisite for serving a request or finishing a job.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import threading
import time
import traceback
import uuid

SCHEMA = 1
LEVELS = {'debug': 10, 'info': 20, 'warn': 30, 'error': 40}

HEX32 = re.compile(r'^[0-9a-f]{32}$')
HEX16 = re.compile(r'^[0-9a-f]{16}$')
TOKEN = re.compile(r'^[A-Za-z0-9_.:-]{1,80}$')
MODEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$')
SPACE = re.compile(r'^[A-Za-z0-9._:/@+-]{1,200}$')
ROUTE = re.compile(r'^/[A-Za-z0-9_./{}-]{0,120}$')
IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,79}$')
# Values that look like credentials are refused even in otherwise valid token fields.
SECRET_SHAPES = re.compile(r'^(sk[-_]|pa-|ya29\.|1//|eyJ|ghp_|gho_|xox[bp]-|AIza|Bearer)|[A-Za-z0-9+/_-]{40,}')


def _hex_id(v):
    return v if isinstance(v, str) and (HEX32.match(v) or HEX16.match(v)) else None


def _uuid(v):
    if isinstance(v, uuid.UUID):
        return str(v)
    try:
        return str(uuid.UUID(str(v))) if isinstance(v, str) and len(v) <= 36 else None
    except ValueError:
        return None


def _token(v):
    return v if isinstance(v, str) and TOKEN.match(v) and not SECRET_SHAPES.search(v) else None


def _model(v):
    return v if isinstance(v, str) and MODEL.match(v) and not SECRET_SHAPES.match(v) else None


def _space(v):
    return v if isinstance(v, str) and SPACE.match(v) and not SECRET_SHAPES.match(v) else None


def _route(v):
    return v if isinstance(v, str) and ROUTE.match(v) else None


def _int(v):
    return int(v) if isinstance(v, int) and not isinstance(v, bool) and -2**53 < v < 2**53 else None


def _number(v):
    if isinstance(v, Decimal):
        v = float(v)
    return round(float(v), 6) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e12 else None


def _bool(v):
    return v if isinstance(v, bool) else None


def _request_id(v):
    """Provider request identifiers (e.g. OpenRouter ``gen-...``); opaque, bounded."""
    return v if isinstance(v, str) and re.match(r'^[A-Za-z0-9_.:-]{1,100}$', v) and not re.match(r'^(sk[-_]|Bearer)', v) else None


FIELDS = {
    # correlation (normally filled from context)
    'request_id': _hex_id, 'operation_id': _hex_id, 'parent_operation_id': _hex_id, 'job_id': _uuid,
    'workspace_id': _uuid,
    # common
    'status': _int, 'count': _int, 'duration_ms': _int, 'attempt': _int, 'attempts': _int, 'max_attempts': _int,
    'code': _token, 'stream': _token, 'kind': _token, 'provider': _token, 'outcome': _token, 'route': _route,
    'method': _token, 'category': _token, 'stage': _token, 'operation': _token, 'state': _token, 'scope': _token,
    'reason': _token, 'billing': _token, 'input_type': _token, 'catalog_status': _token, 'error_category': _token,
    'purpose': _token, 'check': _token, 'rule': _token, 'target': _token, 'previous_state': _token,
    'sampled': _bool, 'ok': _bool, 'listed': _bool, 'truncated': _bool, 'cached': _bool, 'final': _bool,
    # AI / models
    'model': _model, 'requested_model': _model, 'catalog_model': _model, 'returned_model': _model,
    'identity': _model, 'reasoning_model': _model, 'embedding_model': _model, 'space': _space,
    'expected_dimension': _int, 'actual_dimension': _int, 'http_status': _int, 'retry_after_ms': _int,
    'provider_request_id': _request_id, 'input_tokens': _int, 'output_tokens': _int, 'tokens': _int,
    'cost_usd': _number, 'revision': _int, 'max_tokens': _int, 'finish_reason': _token,
    # jobs / sync / ingest / retrieval
    'queue_age_ms': _int, 'delay_ms': _int, 'lag_ms': _int, 'queued': _int, 'running': _int, 'failed': _int,
    'oldest_queue_age_ms': _int, 'stalled_streams': _int, 'pages': _int, 'items': _int, 'created': _int,
    'updated': _int, 'unchanged': _int, 'skipped': _int, 'messages': _int, 'contacts': _int,
    'vector_results': _int, 'keyword_results': _int, 'results': _int, 'covered': _int, 'total': _int,
    'citations': _int, 'enqueued': _int,
    # evidence-grounded drafts (counts only; never questions, answers or passages)
    'questions': _int, 'passages': _int, 'chars': _int, 'claims_count': _int, 'supported': _int, 'examples': _int,
    # event identity resolution
    'mode': _token, 'evaluated': _int, 'settled': _int, 'same': _int, 'different': _int, 'uncertain': _int,
    'rejected': _int, 'invalid': _int, 'stale': _int, 'grouped': _int, 'continued': _int, 'cached_pairs': _int,
    # host / storage
    'bytes': _int, 'files': _int, 'deleted_files': _int, 'free_bytes': _int, 'dropped': _int,
    'suppressed': _int, 'exit_code': _int, 'window_s': _int, 'threshold': _int,
}
WARN_SUFFIXES = ('_failed', '_refused', '_unavailable', '_unreachable', '_mismatch', '_skipped')
CONTEXT_KEYS = ('request_id', 'operation_id', 'parent_operation_id', 'job_id')

_context: ContextVar[dict] = ContextVar('moresocial_diagnostics', default={})


def new_id() -> str:
    """A trusted 32-hex correlation ID, generated at ingress or at the start of an operation."""
    return secrets.token_hex(16)


def valid_id(value) -> str | None:
    return value if isinstance(value, str) and HEX32.match(value) else None


def current() -> dict:
    return dict(_context.get())


@contextmanager
def bind(**ids):
    """Bind correlation IDs for the enclosed code (thread- and task-local)."""
    merged = {**_context.get(), **{k: v for k, v in ids.items() if v is not None}}
    token = _context.set(merged)
    try:
        yield merged
    finally:
        _context.reset(token)


@contextmanager
def operation(**ids):
    """Start a new operation nested under the current one."""
    parent = _context.get().get('operation_id') or _context.get().get('request_id')
    op = ids.pop('operation_id', None) or new_id()
    with bind(operation_id=op, parent_operation_id=parent, **ids) as ctx:
        yield ctx


def trace_payload() -> dict:
    """Correlation to store with an enqueued job, so the worker links back to its initiator."""
    ctx = _context.get()
    origin = ctx.get('operation_id') or ctx.get('request_id')
    return {'op': origin} if origin else {}


def frames(exc: BaseException, limit: int = 8) -> list[dict]:
    out = []
    for fr in traceback.extract_tb(exc.__traceback__)[-limit:]:
        name = os.path.basename(fr.filename or '')
        if re.match(r'^[A-Za-z0-9_.-]{1,80}$', name) and IDENT.match(fr.name or ''):
            out.append({'file': name, 'function': fr.name, 'line': int(fr.lineno or 0)})
    return out


# ---------------- sink ----------------

class Sink:
    """stderr plus an optional rotating file store. Failures are counted, never raised."""

    SEGMENT_BYTES = 16 * 1024 * 1024

    def __init__(self):
        self.lock = threading.Lock()
        self.dropped = 0
        self.reported_dropped = 0
        self.segment = 0
        self.day = None
        self.host = re.sub(r'[^A-Za-z0-9-]', '', socket.gethostname())[:24] or 'host'

    def directory(self) -> Path | None:
        root = os.environ.get('DIAGNOSTICS_DIR')
        return Path(root) if root else None

    def _path(self, root: Path, service: str) -> Path:
        day = datetime.now(timezone.utc).strftime('%Y%m%d')
        if day != self.day:
            self.day, self.segment = day, 0
        while True:
            path = root / f'{service}-{day}-{self.host}-{self.segment:03d}.jsonl'
            try:
                if path.stat().st_size < self.SEGMENT_BYTES:
                    return path
            except FileNotFoundError:
                return path
            self.segment += 1

    def write(self, service: str, line: str) -> None:
        try:
            sys.stderr.write(line)
        except Exception:
            pass
        root = self.directory()
        if root is None:
            return
        with self.lock:
            try:
                path = self._path(root, service)
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, line.encode())
                finally:
                    os.close(fd)
            except Exception:
                self.dropped += 1


SINK = Sink()


# ---------------- levels, debug scope, suppression ----------------

_debug_cache: tuple[float, dict | None] = (0.0, None)
_buckets: dict[str, list] = {}
_bucket_lock = threading.Lock()
RATE_WINDOW = 60.0
RATE_LIMIT = 50


def service() -> str:
    name = os.environ.get('MORESOCIAL_SERVICE') or ('worker' if 'worker' in ' '.join(sys.argv[:2]) else 'web')
    return name if TOKEN.match(name) else 'app'


def release() -> str | None:
    value = os.environ.get('MORESOCIAL_RELEASE', '')
    return value if re.match(r'^[0-9a-f]{7,40}$', value) else None


def _debug_scope() -> dict | None:
    """Short-lived scoped DEBUG, written by the operator as ``debug.json`` in the log store:
    {"until": <unix seconds>, "services": ["web"], "events": ["admin_", "provider_"]}.
    DEBUG never relaxes field restrictions."""
    global _debug_cache
    now = time.monotonic()
    if now - _debug_cache[0] < 10:
        return _debug_cache[1]
    scope = None
    root = SINK.directory()
    if root is not None:
        try:
            data = json.loads((root / 'debug.json').read_text())
            if isinstance(data, dict) and float(data.get('until', 0)) > time.time():
                scope = data
        except (OSError, ValueError, TypeError):
            scope = None
    _debug_cache = (now, scope)
    return scope


def enabled(level: str, event: str) -> bool:
    minimum = LEVELS.get(os.environ.get('LOG_LEVEL', 'info').lower(), 20)
    if LEVELS[level] >= minimum:
        return True
    scope = _debug_scope()
    if not scope:
        return False
    services = scope.get('services') or []
    events = scope.get('events') or []
    return (not services or service() in services) and (not events or any(event.startswith(str(p)) for p in events))


def _admit(key: str) -> tuple[bool, int]:
    """Allow RATE_LIMIT events per key per window; report how many were suppressed before."""
    now = time.monotonic()
    with _bucket_lock:
        bucket = _buckets.get(key)
        if bucket is None or now - bucket[0] > RATE_WINDOW:
            suppressed = bucket[2] if bucket else 0
            _buckets[key] = [now, 1, 0]
            if len(_buckets) > 1000:
                _buckets.clear()
            return True, suppressed
        if bucket[1] >= RATE_LIMIT:
            bucket[2] += 1
            return False, 0
        bucket[1] += 1
        return True, 0


def emit(event: str, exc: BaseException | None = None, *, level: str | None = None, audit: bool = False,
         **fields) -> str:
    """Write one sanitized event; return its reference. Never raises."""
    ref = secrets.token_hex(8)
    try:
        if not IDENT.match(event or ''):
            event = 'invalid_event'
        if level not in LEVELS:
            level = 'error' if exc is not None else ('warn' if event.endswith(WARN_SUFFIXES) else 'info')
        if not audit and not enabled(level, event):
            return ref
        row = {'schema': SCHEMA, 'ts': datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
               'level': level, 'service': service(), 'event': event, 'reference': ref}
        rel = release()
        if rel:
            row['release'] = rel
        if audit:
            row['audit'] = True
        merged = {**_context.get(), **fields}
        for key, value in merged.items():
            check = FIELDS.get(key)
            if check is None or value is None:
                continue
            clean = check(value)
            if clean is not None:
                row[key] = clean
        if exc is not None:
            name = type(exc).__name__
            row['error_type'] = name if IDENT.match(name) else 'Exception'
            row['frames'] = frames(exc)
        if not audit:
            allowed, suppressed = _admit(f'{event}:{level}:{row.get("code", "")}')
            if not allowed:
                return ref
            if suppressed:
                row['suppressed'] = suppressed
        if SINK.dropped > SINK.reported_dropped:
            row['dropped'] = SINK.dropped - SINK.reported_dropped
            SINK.reported_dropped = SINK.dropped
        SINK.write(row['service'], json.dumps(row, separators=(',', ':')) + '\n')
    except Exception:
        pass
    return ref


# ---------------- HTTP ingress ----------------

# Routine probes and the provisioner's 10-second poll: logged only when they fail.
QUIET_PATHS = ('/health', '/ready', '/internal/health', '/internal/manage/connectors')


def http_sample_rate() -> float:
    try:
        return min(1.0, max(0.0, float(os.environ.get('HTTP_LOG_SAMPLE', '0.1'))))
    except ValueError:
        return 0.1


def request_middleware(trust_incoming: bool):
    """ASGI-level HTTP middleware: assign a request ID, bind it for the request, return it
    as ``X-Request-ID`` and emit one sampled ``http_request`` event with the route template.

    The public app never trusts a client-supplied ID; the private internal app accepts a
    well-formed one from connectors so a connector request and its ingestion correlate.
    """
    import random

    async def middleware(request, call_next):
        incoming = request.headers.get('x-request-id') if trust_incoming else None
        request_id = valid_id(incoming) or new_id()
        request.scope['moresocial.request_id'] = request_id
        started = time.monotonic()
        status = 500
        with bind(request_id=request_id):
            try:
                response = await call_next(request)
                status = response.status_code
                response.headers['X-Request-ID'] = request_id
                return response
            finally:
                path = request.scope.get('path', '')
                route = getattr(request.scope.get('route'), 'path', None)
                duration = int((time.monotonic() - started) * 1000)
                quiet = path in QUIET_PATHS or path.startswith('/static/')
                if not quiet or status >= 500:
                    sampled = status < 400 and duration < 2000
                    if not sampled or random.random() < http_sample_rate():
                        emit('http_request', level='error' if status >= 500 else 'info', method=request.method,
                             route=route or ('/unmatched' if status == 404 else '/other'), status=status,
                             duration_ms=duration, sampled=sampled,
                             error_category=None if status < 400 else ('server' if status >= 500 else 'client'))

    return middleware


def request_id_of(request) -> str | None:
    return valid_id(request.scope.get('moresocial.request_id'))
