"""Generation and embedding providers with per-workspace and global daily token budgets.

Operator tier: Anthropic Messages API (structured JSON output) for generation and Voyage AI
for embeddings, configured by the operator. User tier: a workspace's own Anthropic or OpenAI
API key (see `ai_keys`) pays for that workspace's generation; embeddings always stay on the
operator's embedder so every vector shares one model. The deterministic fake is for tests
and local development only. Prompts, responses and keys are never logged. Imported content
is passed as untrusted data; no tools are ever offered to the model.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import threading
import time
import uuid

import httpx
from sqlalchemy import select, text

from . import config, db, security
from .models import Connection, ProviderUsage, Workspace
from .repo import Scoped


class AIUnavailable(Exception):
    """Provider not configured or failed after bounded retries. `not_billed` marks failures
    where the provider certainly charged nothing (not configured, rate limited, key refused),
    so the budget reservation is released instead of kept."""

    def __init__(self, reason: str = '', *, not_billed: bool = False, retry_after: float | None = None):
        super().__init__(reason)
        self.not_billed = not_billed
        self.retry_after = retry_after


def rate_limited(retry_after: float | None = None) -> AIUnavailable:
    return AIUnavailable('rate_limited', not_billed=True, retry_after=retry_after)


def _retry_after(headers) -> float | None:
    try:
        value = float((headers or {}).get('retry-after'))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


class KeyRejected(AIUnavailable):
    """The provider refused a user's API key: invalid_key | no_credit | model_unavailable."""

    def __init__(self, reason: str):
        super().__init__('key_rejected:' + reason, not_billed=True)
        self.reason = reason


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
    cost: Decimal | None = None


@dataclass
class Embedded:
    vectors: list[list[float]]
    tokens: int
    model: str
    cost: Decimal | None = None


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


def _scopes(workspace_id: uuid.UUID, billing: str) -> list[tuple[str, int]]:
    """Budget scopes a call reserves against. A user's own key is capped per workspace only
    (runaway protection); it never consumes the operator's workspace or global allowance."""
    settings = config.get()
    if billing == 'user':
        return [('userkey:' + str(workspace_id), settings.ai_daily_tokens_user_key)]
    return [('workspace:' + str(workspace_id), settings.ai_daily_tokens_workspace), ('global', settings.ai_daily_tokens_global)]


def reserve(workspace_id: uuid.UUID, operation: str, model: str, tokens: int, job_id: uuid.UUID | None = None,
            billing: str = 'operator', provider_name: str = 'anthropic') -> uuid.UUID:
    """Atomically reserve estimated tokens against every budget scope of the paying tier."""
    day = today()
    scopes = _scopes(workspace_id, billing)
    with db.session() as s:
        for scope, limit in scopes:
            s.execute(RESERVE_SQL, {'scope': scope, 'day': day})
        for scope, limit in scopes:
            if s.execute(TAKE_SQL, {'scope': scope, 'day': day, 'n': tokens, 'limit': limit}).first() is None:
                s.rollback()
                raise BudgetExhausted(scope.split(':', 1)[0])
        usage = ProviderUsage(id=uuid.uuid4(), workspace_id=workspace_id, job_id=job_id, day=day, operation=operation,
                              model=model, provider=provider_name, billing=billing, reserved_tokens=tokens, status='reserved')
        s.add(usage)
        return usage.id


def reconcile(usage_id: uuid.UUID, input_tokens: int | None, output_tokens: int | None, failed: bool = False,
              cost: Decimal | None = None) -> None:
    """Replace the reservation by actual usage (a failed call keeps what the provider billed, if known)."""
    with db.session() as s:
        usage = s.get(ProviderUsage, usage_id, with_for_update=True)
        if usage is None or usage.status != 'reserved':
            return
        if failed and input_tokens is None and output_tokens is None:
            delta = 0  # unknown provider-side cost: keep the reservation (conservative)
        else:
            delta = (input_tokens or 0) + (output_tokens or 0) - usage.reserved_tokens
        for scope, _ in _scopes(usage.workspace_id, usage.billing):
            s.execute(text('UPDATE usage_budgets SET tokens = GREATEST(0, tokens + :d) WHERE scope = :scope AND day = :day'),
                      {'d': delta, 'scope': scope, 'day': usage.day})
        usage.input_tokens, usage.output_tokens = input_tokens, output_tokens
        usage.cost, usage.currency = cost, ('USD' if cost is not None else None)
        usage.status = 'failed' if failed else 'reconciled'


def budget_state(workspace_id: uuid.UUID, billing: str | None = None) -> dict:
    """Today's usage for the tier that currently pays for this workspace's generation."""
    if billing is None:
        billing = generator_for(workspace_id).billing
    scopes = dict(_scopes(workspace_id, billing))
    with db.session() as s:
        rows = dict(s.execute(text('SELECT scope, tokens FROM usage_budgets WHERE day = :d AND scope = ANY(:scopes)'),
                              {'d': today(), 'scopes': list(scopes)}).all())
    key = next(iter(scopes))
    paused = any(rows.get(scope, 0) >= limit * 0.98 for scope, limit in scopes.items()) if billing != 'none' else False
    return {'billing': billing, 'used': rows.get(key, 0), 'limit': scopes[key], 'paused': paused}


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
    cooldown_until = 0.0  # monotonic time before which this adapter is not called (after a 429)

    def cool_down(self, retry_after: float | None) -> None:
        self.cooldown_until = time.monotonic() + min(RATE_LIMIT_MAX_WAIT, retry_after or RATE_LIMIT_DEFAULT_WAIT)

    def cooling_down(self) -> float | None:
        remaining = self.cooldown_until - time.monotonic()
        return remaining if remaining > 0 else None

    def can_generate(self) -> bool:
        return False

    def generate(self, *, system: str, prompt: str, schema: dict, max_tokens: int, effort: str, context: dict) -> Generated:
        raise AIUnavailable('AI provider is not configured')

    def embed(self, texts: list[str], input_type: str) -> Embedded:
        raise AIUnavailable('Embedding provider is not configured')

    @property
    def embedding_space(self) -> str:
        """Stable identity; API model names alone are not safe vector-space identities."""
        return f'v1:{self.name}:{self.embedding_model}:{self.embedding_dimension}:document-query'


RATE_LIMIT_DEFAULT_WAIT = 30.0
RATE_LIMIT_MAX_WAIT = 300.0


def _anthropic_rejection(exc) -> str | None:
    """Classify an Anthropic status error that means the key itself cannot be used."""
    status = getattr(exc, 'status_code', None)
    if status == 401:
        return 'invalid_key'
    if status == 402:
        return 'no_credit'
    if status in (403, 404):
        return 'model_unavailable'
    message = str(getattr(exc, 'message', '') or '').lower()
    if status == 400 and ('credit balance' in message or 'spend limit' in message):
        return 'no_credit'
    return None


class AnthropicGenerator(Provider):
    """Messages API generation with one API key (the operator's or a user's)."""
    name = 'anthropic'

    def __init__(self, key: str, model: str, fallbacks: bool, http_client=None):
        # http_client: an `httpx2.Client` (the SDK's HTTP library), for tests only.
        self.generation_model = model
        self.fallbacks = fallbacks
        import anthropic
        self._client = anthropic.Anthropic(api_key=key, timeout=90.0, max_retries=2, http_client=http_client)

    def can_generate(self) -> bool:
        return True

    def generate(self, *, system, prompt, schema, max_tokens, effort, context) -> Generated:
        import anthropic
        request = dict(model=self.generation_model, max_tokens=max_tokens, system=system,
                       messages=[{'role': 'user', 'content': prompt}],
                       output_config={'effort': effort, 'format': {'type': 'json_schema', 'schema': schema}})
        try:
            if self.fallbacks:
                # Server-side refusal fallback: a declined request is re-run on a fallback model.
                response = self._client.beta.messages.create(**request, betas=['server-side-fallback-2026-07-01'],
                                                             fallbacks='default')
            else:
                response = self._client.messages.create(**request)
        except anthropic.RateLimitError as exc:
            wait = _retry_after(getattr(getattr(exc, 'response', None), 'headers', None))
            security.emit('ai_generation_failed', exc, provider='anthropic', status=429, code='rate_limited')
            raise rate_limited(wait) from exc
        except anthropic.APIStatusError as exc:
            reason = _anthropic_rejection(exc)
            if reason:
                security.emit('ai_key_refused', exc, provider='anthropic', status=exc.status_code, code=reason)
                raise KeyRejected(reason) from None
            security.emit('ai_generation_failed', exc, provider='anthropic', status=exc.status_code, code='provider_error')
            raise AIUnavailable('provider_error') from exc
        except anthropic.APIConnectionError as exc:
            security.emit('ai_generation_failed', exc, provider='anthropic', code='connection')
            raise AIUnavailable('connection') from exc
        usage = response.usage
        tokens_in = (usage.input_tokens or 0) + (getattr(usage, 'cache_read_input_tokens', 0) or 0) + \
            (getattr(usage, 'cache_creation_input_tokens', 0) or 0)
        tokens_out = usage.output_tokens or 0
        if response.stop_reason in ('refusal', 'max_tokens'):
            raise InvalidOutput(response.stop_reason, tokens_in, tokens_out)
        text_ = next((b.text for b in response.content if getattr(b, 'type', '') == 'text'), None)
        return Generated(data=_json_object(text_, tokens_in, tokens_out), input_tokens=tokens_in, output_tokens=tokens_out,
                         model=response.model)


def _json_object(text_: str | None, tokens_in: int, tokens_out: int) -> dict:
    try:
        data = json.loads(text_ or '')
    except ValueError:
        raise InvalidOutput('invalid_json', tokens_in, tokens_out) from None
    if not isinstance(data, dict):
        raise InvalidOutput('invalid_json', tokens_in, tokens_out)
    return data


def _openai_rejection(r: httpx.Response) -> str | None:
    if r.status_code == 401:
        return 'invalid_key'
    if r.status_code in (403, 404):
        return 'model_unavailable'
    if r.status_code == 429:
        try:
            code = (r.json().get('error') or {}).get('code')
        except (ValueError, AttributeError):
            code = None
        if code == 'insufficient_quota':
            return 'no_credit'
    return None


class OpenAIGenerator(Provider):
    """OpenAI Responses API with strict JSON-schema output, using a user's API key."""
    name = 'openai'
    URL = 'https://api.openai.com/v1/responses'
    REASONING_HEADROOM = 4000  # reasoning tokens count toward max_output_tokens

    def __init__(self, key: str, model: str, reasoning: bool, transport: httpx.BaseTransport | None = None):
        self._key = key
        self.generation_model = model
        self.reasoning = reasoning
        self._transport = transport

    def __repr__(self) -> str:
        return f'OpenAIGenerator(model={self.generation_model!r})'

    def can_generate(self) -> bool:
        return True

    def generate(self, *, system, prompt, schema, max_tokens, effort, context) -> Generated:
        body = {'model': self.generation_model, 'instructions': system, 'input': prompt, 'store': False,
                'max_output_tokens': max_tokens + (self.REASONING_HEADROOM if self.reasoning else 0),
                'text': {'format': {'type': 'json_schema', 'name': 'result', 'schema': schema, 'strict': True}}}
        if self.reasoning:
            body['reasoning'] = {'effort': effort}
        r = None
        for attempt in range(3):
            try:
                with httpx.Client(timeout=90, transport=self._transport) as h:
                    r = h.post(self.URL, json=body, headers={'Authorization': 'Bearer ' + self._key})
            except httpx.HTTPError as exc:
                security.emit('ai_generation_failed', exc, provider='openai', attempt=attempt, code='connection')
                r = None
            if r is not None:
                if r.is_success:
                    break
                reason = _openai_rejection(r)
                if reason:
                    security.emit('ai_key_refused', provider='openai', status=r.status_code, code=reason)
                    raise KeyRejected(reason)
                if r.status_code == 429:
                    security.emit('ai_generation_failed', provider='openai', status=429, code='rate_limited')
                    raise rate_limited(_retry_after(r.headers))
                if r.status_code not in (500, 502, 503, 504):
                    security.emit('ai_generation_failed', provider='openai', status=r.status_code, code='provider_error')
                    raise AIUnavailable('provider_error')
            if attempt < 2:
                time.sleep(min(8, 2 ** attempt))
        else:
            raise AIUnavailable('connection' if r is None else 'provider_error')
        data = r.json()
        usage = data.get('usage') or {}
        tokens_in, tokens_out = int(usage.get('input_tokens') or 0), int(usage.get('output_tokens') or 0)
        if data.get('status') == 'incomplete':
            reason = (data.get('incomplete_details') or {}).get('reason')
            raise InvalidOutput('max_tokens' if reason == 'max_output_tokens' else 'incomplete', tokens_in, tokens_out)
        text_ = None
        for item in data.get('output') or []:
            if item.get('type') != 'message':
                continue
            for part in item.get('content') or []:
                if part.get('type') == 'refusal':
                    raise InvalidOutput('refusal', tokens_in, tokens_out)
                if part.get('type') == 'output_text':
                    text_ = part.get('text')
        return Generated(data=_json_object(text_, tokens_in, tokens_out), input_tokens=tokens_in, output_tokens=tokens_out,
                         model=data.get('model') or self.generation_model)


class VoyageEmbedder(Provider):
    """Operator-paid embeddings (Voyage AI)."""
    name = 'voyage'
    VOYAGE_URL = 'https://api.voyageai.com/v1/embeddings'

    def __init__(self, key: str, model: str, dimension: int, voyage_transport: httpx.BaseTransport | None = None):
        self._key = key
        self.embedding_model = model
        self.embedding_dimension = dimension
        self._voyage_transport = voyage_transport

    def __repr__(self) -> str:
        return f'VoyageEmbedder(model={self.embedding_model!r})'

    def embed(self, texts, input_type) -> Embedded:
        key = self._key
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


class OpenRouterProvider(Provider):
    """The sole production adapter.

    Models are exact IDs selected and tested by an administrator.  Routing deliberately
    leaves provider choice to OpenRouter, which may fail over *within that same model*.
    No privacy-routing flags, provider allowlists, or alternate-model fallback exists.
    """
    name = 'openrouter'
    BASE_URL = 'https://openrouter.ai/api/v1'

    def __init__(self, key: str, generation_model: str, embedding_model: str, dimension: int,
                 generation_providers: tuple[str, ...] = (), embedding_providers: tuple[str, ...] = (),
                 transport: httpx.BaseTransport | None = None, *, reasoning_effort: bool = False,
                 embedding_contract: str = 'v1:plain-text:document-query', revision: int = 0):
        self._key, self.generation_model = key, generation_model
        self.embedding_model, self.embedding_dimension = embedding_model, dimension
        self.generation_providers, self.embedding_providers = generation_providers, embedding_providers
        self._transport = transport
        self.reasoning_effort = reasoning_effort
        self.embedding_contract = embedding_contract
        self.revision = revision

    def __repr__(self):
        return f'OpenRouterProvider(generation_model={self.generation_model!r}, embedding_model={self.embedding_model!r})'

    def can_generate(self) -> bool:
        return bool(self._key and self.generation_model)

    @property
    def embedding_space(self) -> str:
        return f'v2:openrouter:{self.embedding_model}:{self.embedding_dimension}:{self.embedding_contract}'

    def _post(self, endpoint: str, body: dict, operation: str) -> dict:
        response = None
        for attempt in range(3):
            try:
                with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), transport=self._transport) as client:
                    response = client.post(self.BASE_URL + endpoint, json=body,
                                           headers={'Authorization': 'Bearer ' + self._key, 'Content-Type': 'application/json'})
            except httpx.HTTPError as exc:
                security.emit('ai_' + operation + '_failed', exc, provider='openrouter', attempt=attempt, code='connection')
            else:
                if response.is_success:
                    try:
                        payload = response.json()
                    except ValueError:
                        raise AIUnavailable('malformed_response') from None
                    if isinstance(payload, dict) and payload.get('error'):
                        raise AIUnavailable('provider_error')
                    if isinstance(payload, dict):
                        return payload
                    raise AIUnavailable('malformed_response')
                if response.status_code == 401:
                    raise KeyRejected('invalid_key')
                if response.status_code == 402:
                    raise KeyRejected('no_credit')
                if response.status_code in (403, 404):
                    raise KeyRejected('model_unavailable')
                if response.status_code == 429:
                    raise rate_limited(_retry_after(response.headers))
                if response.status_code not in (500, 502, 503, 504):
                    security.emit('ai_' + operation + '_failed', provider='openrouter', status=response.status_code, code='provider_error')
                    raise AIUnavailable('provider_error')
            if attempt < 2:
                time.sleep(min(4, 2 ** attempt))
        raise AIUnavailable('connection' if response is None else 'provider_error')

    @staticmethod
    def _usage(payload: dict) -> tuple[int, int]:
        usage = payload.get('usage') or {}
        # Do not add reasoning_tokens: providers normally include it in completion_tokens.
        return int(usage.get('prompt_tokens') or usage.get('input_tokens') or 0), int(usage.get('completion_tokens') or usage.get('output_tokens') or 0)

    @staticmethod
    def _cost(payload: dict) -> Decimal | None:
        raw = (payload.get('usage') or {}).get('cost')
        if raw is None:
            return None
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return None
        return value if value >= 0 else None

    def generate(self, *, system, prompt, schema, max_tokens, effort, context) -> Generated:
        if not self.can_generate():
            raise AIUnavailable('AI provider is not configured', not_billed=True)
        body = {'model': self.generation_model, 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}],
                'max_tokens': max_tokens, 'stream': False, 'provider': {'require_parameters': True},
                'response_format': {'type': 'json_schema', 'json_schema': {'name': 'result', 'strict': True, 'schema': schema}}}
        if self.reasoning_effort:
            body['reasoning'] = {'effort': effort}
        payload = self._post('/chat/completions', body, 'generation')
        tin, tout = self._usage(payload)
        choices = payload.get('choices')
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise InvalidOutput('missing_content', tin, tout)
        choice, message = choices[0], choices[0].get('message') or {}
        if choice.get('finish_reason') in ('length', 'content_filter') or message.get('refusal'):
            raise InvalidOutput('truncated' if choice.get('finish_reason') == 'length' else 'refusal', tin, tout)
        return Generated(_json_object(message.get('content') if isinstance(message, dict) else None, tin, tout), tin, tout,
                         str(payload.get('model') or self.generation_model), self._cost(payload))

    def embed(self, texts: list[str], input_type: str) -> Embedded:
        if not self._key or not self.embedding_model or not self.embedding_dimension:
            raise AIUnavailable('Embedding provider is not configured', not_billed=True)
        if not texts or any(not isinstance(t, str) or not t for t in texts):
            raise InvalidOutput('embedding_input')
        # Input formatting is part of embedding_space.  The current verified contract
        # is plain text; don't invent model-specific query/document prefixes.
        body = {'model': self.embedding_model, 'input': texts}
        payload = self._post('/embeddings', body, 'embedding')
        rows = payload.get('data')
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise InvalidOutput('embedding_shape')
        vectors: list[list[float] | None] = [None] * len(texts)
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('index'), int) or not 0 <= row['index'] < len(texts):
                raise InvalidOutput('embedding_index')
            vector = row.get('embedding')
            if vectors[row['index']] is not None:
                raise InvalidOutput('embedding_index')
            if not isinstance(vector, list) or len(vector) != self.embedding_dimension:
                raise InvalidOutput('embedding_shape')
            if any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector) or not any(vector):
                raise InvalidOutput('embedding_values')
            vectors[row['index']] = [float(x) for x in vector]
        if any(v is None for v in vectors):
            raise InvalidOutput('embedding_index')
        tokens, _ = self._usage(payload)
        returned_model = str(payload.get('model') or self.embedding_model)
        if returned_model != self.embedding_model:
            raise InvalidOutput('embedding_model')
        return Embedded(vectors=vectors, tokens=tokens, model=returned_model, cost=self._cost(payload))




class OperatorTier(Provider):
    """The operator's generation provider (if any) plus the operator's embedder (if any)."""

    def __init__(self, generator: Provider | None, embedder_: Provider | None):
        self.generator, self.embedder = generator, embedder_
        self.name = generator.name if generator else 'none'
        self.generation_model = generator.generation_model if generator else 'none'
        self.embed_name = embedder_.name if embedder_ else 'none'
        if embedder_:
            self.embedding_model, self.embedding_dimension = embedder_.embedding_model, embedder_.embedding_dimension

    def can_generate(self) -> bool:
        return self.generator is not None

    def generate(self, **kw) -> Generated:
        if self.generator is None:
            raise AIUnavailable('AI provider is not configured', not_billed=True)
        return self.generator.generate(**kw)

    def embed(self, texts, input_type) -> Embedded:
        if self.embedder is None:
            raise AIUnavailable('Embedding provider is not configured', not_billed=True)
        return self.embedder.embed(texts, input_type)


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

    def can_generate(self) -> bool:
        return True

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


class SyntheticKeyProvider(FakeProvider):
    """Stands in for a user's key under synthetic providers. A key containing `revoked`
    passes validation but is refused at generation time, like a key revoked later."""

    def __init__(self, name: str, key: str):
        super().__init__()
        self.name = name
        self.generation_model = 'fake-' + name
        self._revoked = 'revoked' in key

    def generate(self, **kw) -> Generated:
        if self._revoked:
            raise KeyRejected('invalid_key')
        return super().generate(**kw)


_override: Provider | None = None
_operator: Provider | None = None
_operator_stamp = None
_operator_checked = 0.0
_operator_lock = threading.Lock()
OPERATOR_REFRESH_SECONDS = 30


def provider() -> Provider:
    """The operator tier. Rebuilt when the admin page changes global settings (checked at
    most every OPERATOR_REFRESH_SECONDS; `reload_operator()` forces it)."""
    if _override is not None:
        return _override
    if _operator is None or time.monotonic() - _operator_checked > OPERATOR_REFRESH_SECONDS:
        reload_operator(force=False)
    return _operator


def reload_operator(force: bool = True) -> None:
    global _operator, _operator_stamp, _operator_checked
    from . import operator_settings
    settings = config.get()
    try:
        with db.session() as s:
            stamp = operator_settings.stamp(s)
            if force or _operator is None or stamp != _operator_stamp:
                stored = operator_settings.values(s)
                # A saved removal must suppress a lower-precedence secret file.
                stored['_removed_keys'] = [name[:-8] for name, row in operator_settings.rows(s).items()
                                           if name.endswith('_api_key') and row.value is None]
            else:
                stored = None
    except Exception as exc:  # database unavailable: keep the current tier, or fall back to the environment
        security.emit('operator_settings_unavailable', exc)
        stamp, stored = _operator_stamp, ({} if _operator is None else None)
    with _operator_lock:
        if stored is not None:
            _operator = build_operator(settings, stored)
            _operator_stamp = stamp
        _operator_checked = time.monotonic()


def build_operator(settings: config.Settings, stored: dict[str, str]) -> Provider:
    """Build one immutable effective OpenRouter snapshot.

    Database configuration overrides the optional file bootstrap.  A saved disable or
    removed key is a tombstone and can never accidentally reactivate a mounted secret.
    """
    choice = stored.get('ai_provider')
    if settings.ai_provider == 'fake' and choice is None:
        return FakeProvider(settings.embedding_dimension, settings.embedding_model)
    removed = set(stored.get('_removed_keys', ()))
    if choice is None:
        choice = settings.ai_provider if settings.ai_provider == 'openrouter' else 'none'
    if choice != 'openrouter' or stored.get('ai_enabled') == '0':
        return OperatorTier(None, None)
    key = stored.get('openrouter_api_key') or (None if 'openrouter' in removed else config.read_secret(settings.openrouter_api_key_file))
    generation = stored.get('openrouter_reasoning_model') or settings.openrouter_generation_model
    embedding = stored.get('openrouter_embedding_model') or settings.openrouter_embedding_model
    try:
        dimension = int(stored.get('openrouter_embedding_dimension') or settings.embedding_dimension)
        revision = int(stored.get('ai_revision') or 0)
    except ValueError:
        return OperatorTier(None, None)
    if key and generation and embedding and dimension > 0:
        return OpenRouterProvider(key, generation, embedding, dimension, transport=None,
                                  reasoning_effort=stored.get('openrouter_reasoning_effort') == '1',
                                  embedding_contract=stored.get('openrouter_embedding_contract') or 'v1:plain-text:document-query',
                                  revision=revision)
    return OperatorTier(None, None)


def set_provider(p: Provider | None) -> None:
    """Replace the operator tier (tests); None rebuilds it from settings on next use.
    Also clears cached user-key adapters."""
    global _override, _operator, _operator_stamp
    _override = p
    with _operator_lock:
        _operator, _operator_stamp = None, None
    with _adapters_lock:
        _adapters.clear()


def embedder() -> Provider:
    """Embeddings always use the operator tier, whoever pays for generation."""
    return provider()


def embedding_targets() -> dict[str, Provider]:
    """Active plus (while rebuilding) pending immutable embedding configurations."""
    active = embedder()
    targets = {active.embedding_space: active} if active.embedding_dimension else {}
    if not isinstance(active, OpenRouterProvider):
        return targets
    from . import operator_settings
    try:
        with db.session() as s:
            values = operator_settings.values(s)
        model = values.get(operator_settings.PENDING_EMBEDDING_MODEL)
        dimension = int(values.get(operator_settings.PENDING_EMBEDDING_DIMENSION) or 0)
        contract = values.get(operator_settings.PENDING_EMBEDDING_CONTRACT) or 'v1:plain-text:document-query'
    except (Exception, ValueError):
        return targets
    if model and dimension:
        pending = OpenRouterProvider(active._key, active.generation_model, model, dimension,
                                     reasoning_effort=active.reasoning_effort, embedding_contract=contract,
                                     revision=active.revision)
        targets[pending.embedding_space] = pending
    return targets


def embedder_for_space(space: str) -> Provider | None:
    return embedding_targets().get(space)


# ---------------- per-workspace generation tier ----------------

@dataclass
class Resolved:
    provider: Provider
    billing: str                         # user | operator | none
    connection_id: uuid.UUID | None = None


_adapters: OrderedDict[tuple, Provider] = OrderedDict()
_adapters_lock = threading.Lock()
_user_factory = None
ADAPTER_CACHE = 64


def set_user_adapter_factory(factory) -> None:
    """Tests: `factory(provider_name, key) -> Provider` replaces real user-key adapters."""
    global _user_factory
    _user_factory = factory
    with _adapters_lock:
        _adapters.clear()


def _build_user_adapter(name: str, key: str, settings: config.Settings) -> Provider:
    if _user_factory is not None:
        return _user_factory(name, key)
    if settings.synthetic_providers:
        return SyntheticKeyProvider(name, key)
    if name == 'anthropic':
        return AnthropicGenerator(key, settings.anthropic_model, settings.anthropic_fallbacks)
    return OpenAIGenerator(key, settings.openai_model, settings.openai_reasoning)


def _user_adapter(name: str, fingerprint: str, key_enc: str | None, settings: config.Settings) -> Provider | None:
    """Adapters are cached by key fingerprint, so a replaced key never reuses an old client."""
    model = settings.anthropic_model if name == 'anthropic' else settings.openai_model
    cache_key = (name, fingerprint, model)
    with _adapters_lock:
        adapter = _adapters.get(cache_key)
        if adapter is not None:
            _adapters.move_to_end(cache_key)
            return adapter
    key = security.decrypt(key_enc)
    if not key:
        return None
    adapter = _build_user_adapter(name, key, settings)
    with _adapters_lock:
        _adapters[cache_key] = adapter
        while len(_adapters) > ADAPTER_CACHE:
            _adapters.popitem(last=False)
    return adapter


def generator_for(workspace_id: uuid.UUID) -> Resolved:
    """Every production generation call uses the shared OpenRouter snapshot."""
    operator = provider()
    return Resolved(operator, 'operator' if operator.can_generate() else 'none')


# ---------------- budgeted calls ----------------

def generate(workspace_id: uuid.UUID, *, system: str, prompt: str, schema: dict, context: dict, max_tokens: int = 2000,
             effort: str = 'low', job_id: uuid.UUID | None = None) -> Generated:
    tier = generator_for(workspace_id)
    if tier.billing == 'none':
        raise AIUnavailable('AI provider is not configured', not_billed=True)
    return _generate_on(tier, workspace_id, system=system, prompt=prompt, schema=schema, context=context,
                        max_tokens=max_tokens, effort=effort, job_id=job_id)


def _generate_on(tier: Resolved, workspace_id: uuid.UUID, *, system, prompt, schema, context, max_tokens, effort,
                 job_id) -> Generated:
    p = tier.provider
    wait = p.cooling_down()
    if wait:
        raise rate_limited(wait)  # recently rate limited: do not call (or reserve) again yet
    # Reserve input + max output (+ one retry's worth of input for provider-side retries).
    estimate = 2 * estimate_tokens(system + prompt) + max_tokens
    usage = reserve(workspace_id, 'generate', p.generation_model, estimate, job_id, billing=tier.billing, provider_name=p.name)
    try:
        with _slot():
            out = p.generate(system=system, prompt=prompt, schema=schema, max_tokens=max_tokens, effort=effort, context=context)
    except InvalidOutput as exc:
        reconcile(usage, exc.input_tokens, exc.output_tokens, failed=True)
        raise
    except AIUnavailable as exc:
        if exc.not_billed:  # refused before any work (key refused, rate limited): nothing was billed
            reconcile(usage, 0, 0, failed=True)
            if str(exc) == 'rate_limited':
                p.cool_down(exc.retry_after)
        else:
            reconcile(usage, None, None, failed=True)
        raise
    except BaseException:
        reconcile(usage, None, None, failed=True)
        raise
    reconcile(usage, out.input_tokens, out.output_tokens, cost=out.cost)
    return out


def embed(workspace_id: uuid.UUID, texts: list[str], input_type: str, job_id: uuid.UUID | None = None,
          snapshot: Provider | None = None) -> Embedded:
    p = snapshot or embedder()
    if not p.embedding_dimension:
        raise AIUnavailable('Embedding provider is not configured', not_billed=True)
    usage = reserve(workspace_id, 'embed', p.embedding_model, 2 * sum(estimate_tokens(t) for t in texts), job_id,
                    provider_name=getattr(p, 'embed_name', p.name))
    try:
        with _slot():
            out = p.embed(texts, input_type)
    except InvalidOutput as exc:
        reconcile(usage, exc.input_tokens, exc.output_tokens, failed=True)
        raise
    except AIUnavailable as exc:
        reconcile(usage, *((0, 0) if exc.not_billed else (None, None)), failed=True)
        raise
    except BaseException:
        reconcile(usage, None, None, failed=True)
        raise
    reconcile(usage, out.tokens, 0, cost=out.cost)
    return out
