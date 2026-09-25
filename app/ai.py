"""Generation and embedding providers with per-workspace and global daily token budgets.

Production adapter: Anthropic Messages API (structured JSON output) for generation and
Voyage AI for embeddings; both configured by the operator. The deterministic fake is for
tests and local development only. Prompts and responses are never logged. Imported
content is passed as untrusted data; no tools are ever offered to the model.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import re
import threading
import time
import uuid

import httpx
from sqlalchemy import text

from . import config, db, security
from .models import ProviderUsage


class AIUnavailable(Exception):
    """Provider not configured or failed after bounded retries."""


class BudgetExhausted(Exception):
    def __init__(self, scope: str):
        super().__init__(scope)
        self.scope = scope


class InvalidOutput(Exception):
    """The provider answered (and billed) but the output is unusable."""

    def __init__(self, reason: str, input_tokens: int | None = None, output_tokens: int | None = None):
        super().__init__(reason)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


@dataclass
class Generated:
    data: dict
    input_tokens: int
    output_tokens: int
    model: str


@dataclass
class Embedded:
    vectors: list[list[float]]
    tokens: int
    model: str


def estimate_tokens(text_: str) -> int:
    return max(1, math.ceil(len(text_) / 3.5))


# ---------------- budgets ----------------

def today() -> date:
    return datetime.now(timezone.utc).date()


def tomorrow_start() -> datetime:
    t = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return t + timedelta(days=1, minutes=1)


RESERVE_SQL = text("""
INSERT INTO usage_budgets(scope, day, tokens) VALUES (:scope, :day, 0) ON CONFLICT DO NOTHING;
""")
TAKE_SQL = text("""UPDATE usage_budgets SET tokens = tokens + :n WHERE scope = :scope AND day = :day AND tokens + :n <= :limit
                   RETURNING tokens""")


def reserve(workspace_id: uuid.UUID, operation: str, model: str, tokens: int, job_id: uuid.UUID | None = None) -> uuid.UUID:
    """Atomically reserve estimated tokens against both the workspace and global budgets."""
    settings = config.get()
    day = today()
    scopes = [('workspace:' + str(workspace_id), settings.ai_daily_tokens_workspace), ('global', settings.ai_daily_tokens_global)]
    with db.session() as s:
        for scope, limit in scopes:
            s.execute(RESERVE_SQL, {'scope': scope, 'day': day})
        for scope, limit in scopes:
            if s.execute(TAKE_SQL, {'scope': scope, 'day': day, 'n': tokens, 'limit': limit}).first() is None:
                s.rollback()
                raise BudgetExhausted('workspace' if scope != 'global' else 'global')
        usage = ProviderUsage(id=uuid.uuid4(), workspace_id=workspace_id, job_id=job_id, day=day, operation=operation,
                              model=model, reserved_tokens=tokens, status='reserved')
        s.add(usage)
        return usage.id


def reconcile(usage_id: uuid.UUID, input_tokens: int | None, output_tokens: int | None, failed: bool = False) -> None:
    """Replace the reservation by actual usage (a failed call keeps what the provider billed, if known)."""
    with db.session() as s:
        usage = s.get(ProviderUsage, usage_id, with_for_update=True)
        if usage is None or usage.status != 'reserved':
            return
        if failed and input_tokens is None and output_tokens is None:
            delta = 0  # unknown provider-side cost: keep the reservation (conservative)
        else:
            delta = (input_tokens or 0) + (output_tokens or 0) - usage.reserved_tokens
        for scope in ('workspace:' + str(usage.workspace_id), 'global'):
            s.execute(text('UPDATE usage_budgets SET tokens = GREATEST(0, tokens + :d) WHERE scope = :scope AND day = :day'),
                      {'d': delta, 'scope': scope, 'day': usage.day})
        usage.input_tokens, usage.output_tokens = input_tokens, output_tokens
        usage.status = 'failed' if failed else 'reconciled'


def budget_state(workspace_id: uuid.UUID) -> dict:
    settings = config.get()
    with db.session() as s:
        rows = dict(s.execute(text('SELECT scope, tokens FROM usage_budgets WHERE day = :d AND scope IN (:w, :g)'),
                              {'d': today(), 'w': 'workspace:' + str(workspace_id), 'g': 'global'}).all())
    used, glob = rows.get('workspace:' + str(workspace_id), 0), rows.get('global', 0)
    return {'used': used, 'limit': settings.ai_daily_tokens_workspace,
            'paused': used >= settings.ai_daily_tokens_workspace * 0.98 or glob >= settings.ai_daily_tokens_global * 0.98}


_slots: threading.BoundedSemaphore | None = None


def _slot() -> threading.BoundedSemaphore:
    global _slots
    if _slots is None:
        _slots = threading.BoundedSemaphore(max(1, config.get().ai_concurrency))
    return _slots


# ---------------- providers ----------------

class Provider:
    name = 'none'
    generation_model = 'none'
    embedding_model = 'none'
    embedding_dimension = 0

    def generate(self, *, system: str, prompt: str, schema: dict, max_tokens: int, effort: str, context: dict) -> Generated:
        raise AIUnavailable('AI provider is not configured')

    def embed(self, texts: list[str], input_type: str) -> Embedded:
        raise AIUnavailable('Embedding provider is not configured')


class AnthropicVoyageProvider(Provider):
    name = 'anthropic'
    VOYAGE_URL = 'https://api.voyageai.com/v1/embeddings'

    def __init__(self, settings: config.Settings, voyage_transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.generation_model = settings.anthropic_model
        self.embedding_model = settings.embedding_model
        self.embedding_dimension = settings.embedding_dimension
        self._client = None
        self._voyage_transport = voyage_transport

    def client(self):
        if self._client is None:
            key = config.read_secret(self.settings.anthropic_api_key_file)
            if not key:
                raise AIUnavailable('Anthropic API key is not configured')
            import anthropic
            self._client = anthropic.Anthropic(api_key=key, timeout=90.0, max_retries=2)
        return self._client

    def generate(self, *, system, prompt, schema, max_tokens, effort, context) -> Generated:
        import anthropic
        request = dict(model=self.generation_model, max_tokens=max_tokens, system=system,
                       messages=[{'role': 'user', 'content': prompt}],
                       output_config={'effort': effort, 'format': {'type': 'json_schema', 'schema': schema}})
        try:
            if self.settings.anthropic_fallbacks:
                # Server-side refusal fallback: a declined request is re-run on a fallback model.
                response = self.client().beta.messages.create(**request, betas=['server-side-fallback-2026-07-01'],
                                                              fallbacks='default')
            else:
                response = self.client().messages.create(**request)
        except anthropic.RateLimitError as exc:
            raise AIUnavailable('rate_limited') from exc
        except anthropic.APIStatusError as exc:
            security.emit('ai_generation_failed', exc, status=exc.status_code)
            raise AIUnavailable('provider_error') from exc
        except anthropic.APIConnectionError as exc:
            security.emit('ai_generation_failed', exc)
            raise AIUnavailable('connection') from exc
        usage = response.usage
        tokens_in = (usage.input_tokens or 0) + (getattr(usage, 'cache_read_input_tokens', 0) or 0) + \
            (getattr(usage, 'cache_creation_input_tokens', 0) or 0)
        tokens_out = usage.output_tokens or 0
        if response.stop_reason in ('refusal', 'max_tokens'):
            raise InvalidOutput(response.stop_reason, tokens_in, tokens_out)
        text_ = next((b.text for b in response.content if getattr(b, 'type', '') == 'text'), None)
        try:
            data = json.loads(text_ or '')
        except ValueError:
            raise InvalidOutput('invalid_json', tokens_in, tokens_out) from None
        if not isinstance(data, dict):
            raise InvalidOutput('invalid_json', tokens_in, tokens_out)
        return Generated(data=data, input_tokens=tokens_in, output_tokens=tokens_out, model=response.model)

    def embed(self, texts, input_type) -> Embedded:
        key = config.read_secret(self.settings.voyage_api_key_file)
        if not key:
            raise AIUnavailable('Voyage API key is not configured')
        body = {'input': texts, 'model': self.embedding_model, 'input_type': input_type,
                'output_dimension': self.embedding_dimension}
        for attempt in range(3):
            try:
                with httpx.Client(timeout=30, transport=self._voyage_transport) as h:
                    r = h.post(self.VOYAGE_URL, json=body, headers={'Authorization': 'Bearer ' + key})
            except httpx.HTTPError as exc:
                security.emit('ai_embedding_failed', exc, attempt=attempt)
                r = None
            if r is not None and r.is_success:
                data = r.json()
                rows = sorted(data.get('data') or [], key=lambda d: d.get('index', 0))
                vectors = [row['embedding'] for row in rows]
                if len(vectors) != len(texts) or any(len(v) != self.embedding_dimension for v in vectors):
                    raise InvalidOutput('embedding_shape')
                return Embedded(vectors=vectors, tokens=int((data.get('usage') or {}).get('total_tokens') or 0),
                                model=self.embedding_model)
            if r is not None and r.status_code not in (429, 500, 502, 503, 504):
                security.emit('ai_embedding_failed', status=r.status_code)
                raise AIUnavailable('provider_error')
            time.sleep(min(8, 2 ** attempt))
        raise AIUnavailable('embedding_unavailable')


WORD = re.compile(r"[\w']+", re.UNICODE)


class FakeProvider(Provider):
    """Deterministic provider for tests/dev. Behaves like a cautious model: it uses only
    the evidence it is given and never follows instructions inside that evidence."""
    name = 'fake'
    generation_model = 'fake-generator'

    def __init__(self, dimension: int = 64, model: str = 'fake-hash-64'):
        self.embedding_dimension = dimension
        self.embedding_model = model
        self.calls: list[dict] = []

    def embed(self, texts, input_type) -> Embedded:
        vectors = []
        for t in texts:
            v = [0.0] * self.embedding_dimension
            for w in WORD.findall(t.lower()):
                if len(w) < 3:
                    continue
                h = int(hashlib.sha256(w.encode()).hexdigest(), 16)
                v[h % self.embedding_dimension] += 1.0 if (h >> 8) % 2 else -1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            vectors.append([x / norm for x in v])
        return Embedded(vectors=vectors, tokens=sum(estimate_tokens(t) for t in texts), model=self.embedding_model)

    def generate(self, *, system, prompt, schema, max_tokens, effort, context) -> Generated:
        self.calls.append({'purpose': context.get('purpose'), 'prompt': prompt, 'tools': None})
        from . import fake_generation
        data = fake_generation.respond(context)
        return Generated(data=data, input_tokens=estimate_tokens(system + prompt), output_tokens=estimate_tokens(json.dumps(data)),
                         model=self.generation_model)


_provider: Provider | None = None


def provider() -> Provider:
    global _provider
    if _provider is None:
        settings = config.get()
        if settings.ai_provider == 'fake':
            _provider = FakeProvider(settings.embedding_dimension, settings.embedding_model)
        elif settings.ai_provider == 'anthropic':
            _provider = AnthropicVoyageProvider(settings)
        else:
            _provider = Provider()
    return _provider


def set_provider(p: Provider | None) -> None:
    global _provider
    _provider = p


# ---------------- budgeted calls ----------------

def generate(workspace_id: uuid.UUID, *, system: str, prompt: str, schema: dict, context: dict, max_tokens: int = 2000,
             effort: str = 'low', job_id: uuid.UUID | None = None) -> Generated:
    p = provider()
    # Reserve input + max output (+ one retry's worth of input for provider-side retries).
    estimate = 2 * estimate_tokens(system + prompt) + max_tokens
    usage = reserve(workspace_id, 'generate', p.generation_model, estimate, job_id)
    try:
        with _slot():
            out = p.generate(system=system, prompt=prompt, schema=schema, max_tokens=max_tokens, effort=effort, context=context)
    except InvalidOutput as exc:
        reconcile(usage, exc.input_tokens, exc.output_tokens, failed=True)
        raise
    except BaseException:
        reconcile(usage, None, None, failed=True)
        raise
    reconcile(usage, out.input_tokens, out.output_tokens)
    return out


def embed(workspace_id: uuid.UUID, texts: list[str], input_type: str, job_id: uuid.UUID | None = None) -> Embedded:
    p = provider()
    usage = reserve(workspace_id, 'embed', p.embedding_model, 2 * sum(estimate_tokens(t) for t in texts), job_id)
    try:
        with _slot():
            out = p.embed(texts, input_type)
    except InvalidOutput as exc:
        reconcile(usage, exc.input_tokens, exc.output_tokens, failed=True)
        raise
    except BaseException:
        reconcile(usage, None, None, failed=True)
        raise
    reconcile(usage, out.tokens, 0)
    return out
