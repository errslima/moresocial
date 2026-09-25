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
    """Provider not configured or failed after bounded retries."""


class KeyRejected(AIUnavailable):
    """The provider refused a user's API key: invalid_key | no_credit | model_unavailable."""

    def __init__(self, reason: str):
        super().__init__('key_rejected:' + reason)
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
        for scope, _ in _scopes(usage.workspace_id, usage.billing):
            s.execute(text('UPDATE usage_budgets SET tokens = GREATEST(0, tokens + :d) WHERE scope = :scope AND day = :day'),
                      {'d': delta, 'scope': scope, 'day': usage.day})
        usage.input_tokens, usage.output_tokens = input_tokens, output_tokens
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

    def can_generate(self) -> bool:
        return False

    def generate(self, *, system: str, prompt: str, schema: dict, max_tokens: int, effort: str, context: dict) -> Generated:
        raise AIUnavailable('AI provider is not configured')

    def embed(self, texts: list[str], input_type: str) -> Embedded:
        raise AIUnavailable('Embedding provider is not configured')


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
            raise AIUnavailable('rate_limited') from exc
        except anthropic.APIStatusError as exc:
            reason = _anthropic_rejection(exc)
            if reason:
                security.emit('ai_key_refused', exc, provider='anthropic', status=exc.status_code, code=reason)
                raise KeyRejected(reason) from None
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
                security.emit('ai_generation_failed', exc, provider='openai', attempt=attempt)
                r = None
            if r is not None:
                if r.is_success:
                    break
                reason = _openai_rejection(r)
                if reason:
                    security.emit('ai_key_refused', provider='openai', status=r.status_code, code=reason)
                    raise KeyRejected(reason)
                if r.status_code == 429:
                    raise AIUnavailable('rate_limited')
                if r.status_code not in (500, 502, 503, 504):
                    security.emit('ai_generation_failed', provider='openai', status=r.status_code)
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
            raise AIUnavailable('AI provider is not configured')
        return self.generator.generate(**kw)

    def embed(self, texts, input_type) -> Embedded:
        if self.embedder is None:
            raise AIUnavailable('Embedding provider is not configured')
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
            stored = operator_settings.values(s) if force or _operator is None or stamp != _operator_stamp else None
    except Exception as exc:  # database unavailable: keep the current tier, or fall back to the environment
        security.emit('operator_settings_unavailable', exc)
        stamp, stored = _operator_stamp, ({} if _operator is None else None)
    with _operator_lock:
        if stored is not None:
            _operator = build_operator(settings, stored)
            _operator_stamp = stamp
        _operator_checked = time.monotonic()


def build_operator(settings: config.Settings, stored: dict[str, str]) -> Provider:
    """Admin-page settings first, then the server environment (AI_PROVIDER, secret files)."""
    choice = stored.get('ai_provider')
    if settings.ai_provider == 'fake' and choice is None:
        return FakeProvider(settings.embedding_dimension, settings.embedding_model)
    if settings.ai_provider == 'fake':
        embedder_ = FakeProvider(settings.embedding_dimension, settings.embedding_model)
    else:
        voyage = stored.get('voyage_api_key') or config.read_secret(settings.voyage_api_key_file)
        embedder_ = VoyageEmbedder(voyage, settings.embedding_model, settings.embedding_dimension) if voyage else None
    if choice is None:
        choice = 'anthropic' if settings.ai_provider == 'anthropic' else 'none'
    key = None
    if choice == 'anthropic':
        key = stored.get('anthropic_api_key') or config.read_secret(settings.anthropic_api_key_file)
    elif choice == 'openai':
        key = stored.get('openai_api_key')
    generator = None
    if key and settings.synthetic_providers:
        generator = SyntheticKeyProvider(choice, key)
    elif key and choice == 'anthropic':
        generator = AnthropicGenerator(key, settings.anthropic_model, settings.anthropic_fallbacks)
    elif key and choice == 'openai':
        generator = OpenAIGenerator(key, settings.openai_model, settings.openai_reasoning)
    return OperatorTier(generator, embedder_)


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
    """The workspace's working key (its preferred provider first), else the operator tier."""
    settings = config.get()
    if settings.user_ai_keys:
        with db.session() as s:
            ws = Scoped(s, workspace_id)
            preference = s.scalar(select(Workspace.ai_preference).where(Workspace.id == workspace_id))
            rows = s.execute(ws.q(Connection, Connection.id, Connection.provider, Connection.provider_account,
                                  Connection.access_token_enc)
                             .where(Connection.provider.in_(settings.user_ai_keys), Connection.state == 'active')).all()
        rows.sort(key=lambda r: (r.provider != preference, settings.user_ai_keys.index(r.provider)))
        for row in rows:
            adapter = _user_adapter(row.provider, row.provider_account, row.access_token_enc, settings)
            if adapter is not None:
                return Resolved(adapter, 'user', row.id)
    operator = provider()
    return Resolved(operator, 'operator' if operator.can_generate() else 'none')


# ---------------- budgeted calls ----------------

def generate(workspace_id: uuid.UUID, *, system: str, prompt: str, schema: dict, context: dict, max_tokens: int = 2000,
             effort: str = 'low', job_id: uuid.UUID | None = None) -> Generated:
    """Generate on the workspace's paying tier. A refused user key is marked for the user's
    attention and the call is retried on the next tier (another key, then the operator)."""
    for _ in range(len(config.USER_KEY_PROVIDERS) + 1):
        tier = generator_for(workspace_id)
        if tier.billing == 'none':
            raise AIUnavailable('AI provider is not configured')
        try:
            return _generate_on(tier, workspace_id, system=system, prompt=prompt, schema=schema, context=context,
                                max_tokens=max_tokens, effort=effort, job_id=job_id)
        except KeyRejected as exc:
            if tier.billing != 'user':
                raise
            from . import ai_keys
            ai_keys.mark_rejected(workspace_id, tier.connection_id, tier.provider.name, exc.reason)
    raise AIUnavailable('no usable AI provider')


def _generate_on(tier: Resolved, workspace_id: uuid.UUID, *, system, prompt, schema, context, max_tokens, effort,
                 job_id) -> Generated:
    p = tier.provider
    # Reserve input + max output (+ one retry's worth of input for provider-side retries).
    estimate = 2 * estimate_tokens(system + prompt) + max_tokens
    usage = reserve(workspace_id, 'generate', p.generation_model, estimate, job_id, billing=tier.billing, provider_name=p.name)
    try:
        with _slot():
            out = p.generate(system=system, prompt=prompt, schema=schema, max_tokens=max_tokens, effort=effort, context=context)
    except InvalidOutput as exc:
        reconcile(usage, exc.input_tokens, exc.output_tokens, failed=True)
        raise
    except KeyRejected:
        reconcile(usage, 0, 0, failed=True)  # refused before any work: nothing was billed
        raise
    except BaseException:
        reconcile(usage, None, None, failed=True)
        raise
    reconcile(usage, out.input_tokens, out.output_tokens)
    return out


def embed(workspace_id: uuid.UUID, texts: list[str], input_type: str, job_id: uuid.UUID | None = None) -> Embedded:
    p = embedder()
    usage = reserve(workspace_id, 'embed', p.embedding_model, 2 * sum(estimate_tokens(t) for t in texts), job_id,
                    provider_name=getattr(p, 'embed_name', p.name))
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
