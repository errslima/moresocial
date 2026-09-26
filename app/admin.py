"""Admin-only OpenRouter configuration and operational status."""
from __future__ import annotations

from dataclasses import dataclass
import time

import httpx
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select

from . import ai, config, diagnostics, operator_settings, security
from .auth_routes import throttle
from .models import ProviderUsage
from .web import Ctx, Problem, is_admin, redirect, render, require_mutation, require_user

router = APIRouter()
_catalog: tuple[float, dict] | None = None
CATALOG_SECONDS = 600
# Reasoning tokens share the completion budget; 32 tokens truncated valid setup calls.
SETUP_MAX_TOKENS = 4096
_transport: httpx.BaseTransport | None = None


def set_transport(transport: httpx.BaseTransport | None) -> None:
    """Tests: route catalog fetches and setup probes through a mock transport."""
    global _transport, _catalog
    _transport, _catalog = transport, None


def admin_only(ctx: Ctx = Depends(require_user)) -> Ctx:
    if not is_admin(ctx):
        raise Problem(404, 'Not found.')
    return ctx


def admin_mutation(ctx: Ctx = Depends(require_mutation)) -> Ctx:
    return admin_only(ctx)


def catalog() -> dict:
    """Fetch non-secret model metadata; a failed refresh never changes saved choices.

    OpenRouter's generic ``/models`` listing defaults to text-output models. Embedders
    are listed separately at ``/embeddings/models``; filtering the generic listing
    therefore produces an empty selector even while the embedding catalog is healthy.
    """
    global _catalog
    if _catalog and time.monotonic() - _catalog[0] < CATALOG_SECONDS:
        return _catalog[1]
    try:
        with httpx.Client(timeout=5, transport=_transport) as h:
            models_response = h.get(ai.OpenRouterProvider.BASE_URL + '/models')
            embedding_response = h.get(ai.OpenRouterProvider.BASE_URL + '/embeddings/models')
        models_response.raise_for_status()
        embedding_response.raise_for_status()
        reasoning = []
        for row in models_response.json().get('data') or []:
            if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                continue
            ident, params = row['id'], set(row.get('supported_parameters') or [])
            if 'response_format' in params or 'structured_outputs' in params:
                canonical = row.get('canonical_slug')
                reasoning.append({'id': ident, 'reasoning': 'reasoning' in params,
                                  'canonical': canonical if isinstance(canonical, str) else ident})
        embedding = []
        for row in embedding_response.json().get('data') or []:
            if isinstance(row, dict) and isinstance(row.get('id'), str):
                # The dedicated endpoint is the contract. Keep a defensive modality
                # check only when the field is present, for forward-compatible data.
                outputs = set((row.get('architecture') or {}).get('output_modalities') or [])
                if not outputs or 'embeddings' in outputs:
                    ident = row['id']
                    canonical = row.get('canonical_slug')
                    embedding.append({'id': ident, 'canonical': canonical if isinstance(canonical, str) else ident})
        data = {'reasoning': sorted(reasoning, key=lambda x: x['id']), 'embedding': sorted(embedding, key=lambda x: x['id']), 'error': None}
        _catalog = (time.monotonic(), data)
        return data
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        security.emit('openrouter_catalog_failed', exc, level='warn', provider='openrouter',
                      http_status=getattr(getattr(exc, 'response', None), 'status_code', None))
        return {'reasoning': [], 'embedding': [], 'error': 'Model catalog is temporarily unavailable.'}


def _with_selected(items: list[dict], selected: str) -> list[dict]:
    items = [{**item, 'unverified': bool(item.get('unverified', False))} for item in items]
    if selected and not any(i['id'] == selected for i in items):
        return [{'id': selected, 'reasoning': False, 'unverified': True}] + items
    return items


def _costs(ctx: Ctx) -> dict:
    rows = ctx.s.execute(select(ProviderUsage.operation, func.count(), func.coalesce(func.sum(ProviderUsage.cost), 0),
                                 func.count().filter(ProviderUsage.cost.is_(None))).where(ProviderUsage.provider == 'openrouter')
                         .group_by(ProviderUsage.operation)).all()
    return {operation: {'calls': calls, 'known': str(known), 'unknown': unknown} for operation, calls, known, unknown in rows}


def admin_page(request: Request, ctx: Ctx, status: int = 200, error: str | None = None, *,
               submitted: dict | None = None, reference: str | None = None):
    """Render Admin. After a failed test the submitted choices stay selected (never the key)."""
    settings, stored, rows = config.get(), operator_settings.values(ctx.s), operator_settings.rows(ctx.s)
    meta = catalog()
    reasoning = stored.get(operator_settings.REASONING_MODEL) or settings.openrouter_generation_model
    embedding = stored.get(operator_settings.EMBEDDING_MODEL) or settings.openrouter_embedding_model
    submitted = submitted or {}
    chosen_reasoning = submitted.get('reasoning_model') or reasoning
    chosen_embedding = submitted.get('embedding_model') or embedding
    effort = submitted['reasoning_effort'] if 'reasoning_effort' in submitted else stored.get(operator_settings.REASONING_EFFORT) == '1'
    return render(request, 'admin.html', ctx, status=status, error=error, reference=reference,
                  enabled=stored.get(operator_settings.ENABLED) == '1' or (operator_settings.ENABLED not in rows and settings.ai_provider == 'openrouter'),
                  key_row=rows.get('openrouter_api_key'),
                  reasoning_models=_with_selected(_with_selected(meta['reasoning'], reasoning), chosen_reasoning),
                  embedding_models=_with_selected(_with_selected(meta['embedding'], embedding), chosen_embedding),
                  reasoning_model=reasoning, embedding_model=embedding, chosen_reasoning=chosen_reasoning,
                  chosen_embedding=chosen_embedding, chosen_effort=effort,
                  embedding_identity=stored.get(operator_settings.EMBEDDING_IDENTITY),
                  pending_embedding=stored.get(operator_settings.PENDING_EMBEDDING_MODEL), catalog_error=meta['error'],
                  revision=operator_settings.revision(ctx.s), last_test=stored.get(operator_settings.LAST_TEST_STATUS),
                  last_test_error=stored.get(operator_settings.LAST_TEST_ERROR),
                  last_test_reference=stored.get(operator_settings.LAST_TEST_REFERENCE), costs=_costs(ctx), limits=settings)


@router.get('/admin')
def admin_home(request: Request, ctx: Ctx = Depends(admin_only)):
    return admin_page(request, ctx)


def _valid_key(key: str) -> bool:
    return 10 <= len(key) <= 300 and all(c.isprintable() and not c.isspace() for c in key)


# ---------------- instrumented setup test ----------------

class SetupFailed(Exception):
    """A stage of the setup test failed. ``facts`` are sanitized diagnostic metadata."""

    def __init__(self, stage: str, code: str, **facts):
        super().__init__(f'{stage}:{code}')
        self.stage, self.code, self.facts = stage, code, facts


EXPLANATIONS = {
    ('catalog', 'catalog_unavailable'): "OpenRouter's model catalog could not be loaded, so the model identity cannot be "
                                        'verified. No paid request was made. Try again in a few minutes.',
    ('catalog', 'unknown_model'): "The selected model is not in OpenRouter's current catalog. Choose a listed model. "
                                  'No paid request was made.',
    'invalid_key': 'OpenRouter rejected this API key.',
    'no_credit': 'The key has no remaining credit or reached its OpenRouter spending limit.',
    'model_unavailable': 'OpenRouter refused the selected model for this key.',
    'rate_limited': 'OpenRouter is rate limiting this key. Try again shortly.',
    'connection': 'OpenRouter could not be reached.',
    'provider_error': 'OpenRouter returned an error.',
    'malformed_response': 'OpenRouter returned a response that is not valid JSON.',
    ('reasoning_probe', 'truncated'): 'The reasoning model reached the setup test token limit before finishing. '
                                      'The API key was accepted, but setup could not be verified.',
    ('reasoning_probe', 'validation_schema'): 'The reasoning model did not return the requested JSON object.',
    ('reasoning_probe', 'model_mismatch'): 'The reasoning request was answered by a different model than the one selected.',
    'embedding_shape': 'The embedding response did not contain one well-formed vector of the expected size.',
    'embedding_index': 'The embedding response had inconsistent row indices.',
    'embedding_values': 'The embedding response contained invalid numbers.',
    ('embedding_probe', 'embedding_model'): 'The embedding request was answered as a model that is neither the selected ID '
                                            'nor its documented canonical ID.',
    ('query_probe', 'embedding_model'): 'The second (query) embedding request was answered as a model that is neither the '
                                        'selected ID nor its documented canonical ID.',
    ('commit', 'stale_revision'): 'Settings changed while the candidate was tested. Reload and try again.',
}
STAGE_LABELS = {'catalog': 'catalog check', 'reasoning_probe': 'reasoning test', 'embedding_probe': 'embedding test',
                'query_probe': 'query embedding test', 'validation': 'validation', 'commit': 'saving'}


def explain(failure: SetupFailed) -> str:
    text = EXPLANATIONS.get((failure.stage, failure.code)) or EXPLANATIONS.get(failure.code) or 'The test failed.'
    facts = failure.facts
    if failure.code in ('embedding_model', 'model_mismatch') and facts.get('returned_model'):
        text += (f" Requested {facts.get('requested_model')}; catalog canonical {facts.get('catalog_model') or 'none'};"
                 f" returned {facts['returned_model']}.")
    elif failure.code == 'embedding_model':
        text += ' The response did not name a model.'
    return f'Test failed during the {STAGE_LABELS.get(failure.stage, failure.stage)} ({failure.code}). {text}'


def _lookup(rows: list[dict], ident: str) -> dict | None:
    return next((row for row in rows if row['id'] == ident), None)


@dataclass
class Tested:
    provider: ai.OpenRouterProvider
    embedding_identity: str


class _Stage:
    """Times one stage and emits exactly one event for it, carrying the operation ID."""

    def __init__(self, name: str, **facts):
        self.name, self.facts = name, facts

    def __enter__(self):
        self.started = time.monotonic()
        return self

    def __exit__(self, kind, exc, tb):
        duration = int((time.monotonic() - self.started) * 1000)
        if exc is None:
            security.emit('admin_openrouter_stage', stage=self.name, outcome='ok', duration_ms=duration, **self.facts)
            return False
        if isinstance(exc, SetupFailed):
            failure = exc
        elif isinstance(exc, (ai.AIUnavailable, ai.InvalidOutput)):
            code = exc.reason if isinstance(exc, ai.KeyRejected) else str(exc)
            failure = SetupFailed(self.name, code if diagnostics.TOKEN.match(code or '') else 'not_configured',
                                  http_status=getattr(exc, 'http_status', None))
        else:
            return False
        facts = {**self.facts, **{k: v for k, v in failure.facts.items() if v is not None}}
        failure.facts = facts
        security.emit('admin_openrouter_stage', level='warn', stage=self.name, outcome='failed', code=failure.code,
                      duration_ms=duration, **facts)
        if failure is not exc:
            raise failure from None
        return False


def _probe_facts(probe: ai.OpenRouterProvider) -> dict:
    call = probe.last_call
    return {k: call.get(k) for k in ('http_status', 'attempt', 'provider_request_id', 'returned_model', 'max_tokens', 'finish_reason') if call.get(k) is not None}


def setup_test(key: str, reasoning_model: str, embedding_model: str, *, reasoning_effort: bool,
               saved: dict[str, str]) -> Tested:
    """Tiny paid synthetic calls that verify both models and learn the embedding dimension.

    Stages: catalog -> reasoning_probe -> embedding_probe -> query_probe -> validation.
    Identity policy: `ai.identity_accepted` (selected catalog ID or its documented
    canonical ID; nothing else). A missing catalog is reported as such, never as an
    identity mismatch.
    """
    saved_embedding = saved.get(operator_settings.EMBEDDING_MODEL)
    saved_identity = saved.get(operator_settings.EMBEDDING_IDENTITY) or saved_embedding
    with _Stage('catalog', requested_model=embedding_model, reasoning_model=reasoning_model) as stage:
        meta = catalog()
        stage.facts['catalog_status'] = 'unavailable' if meta['error'] else 'ok'
        if meta['error']:
            # Only an unchanged, previously verified configuration can be re-tested without the catalog.
            if embedding_model != saved_embedding or reasoning_model != saved.get(operator_settings.REASONING_MODEL):
                raise SetupFailed('catalog', 'catalog_unavailable')
            reasoning_entry = {'id': reasoning_model, 'canonical': reasoning_model}
            embedding_entry = {'id': embedding_model, 'canonical': saved_identity}
        else:
            reasoning_entry = _lookup(meta['reasoning'], reasoning_model)
            embedding_entry = _lookup(meta['embedding'], embedding_model)
            if reasoning_entry is None and reasoning_model == saved.get(operator_settings.REASONING_MODEL):
                reasoning_entry = {'id': reasoning_model, 'canonical': reasoning_model}
            if embedding_entry is None and embedding_model == saved_embedding:
                embedding_entry = {'id': embedding_model, 'canonical': saved_identity}
            stage.facts['listed'] = embedding_entry is not None and _lookup(meta['embedding'], embedding_model) is not None
            if reasoning_entry is None:
                raise SetupFailed('catalog', 'unknown_model', requested_model=reasoning_model)
            if embedding_entry is None:
                raise SetupFailed('catalog', 'unknown_model')
        identity = embedding_entry.get('canonical') or embedding_model
        stage.facts['catalog_model'] = identity

    probe = ai.OpenRouterProvider(key, reasoning_model, embedding_model, 1, transport=_transport,
                                  reasoning_effort=reasoning_effort or bool(reasoning_entry.get('reasoning')))
    with _Stage('reasoning_probe', requested_model=reasoning_model,
                catalog_model=reasoning_entry.get('canonical') or reasoning_model) as stage:
        try:
            generated = probe.generate(system='Return the requested object.', prompt='Synthetic setup check.',
                                       schema={'type': 'object', 'additionalProperties': False, 'required': ['ok'],
                                               'properties': {'ok': {'type': 'boolean'}}},
                                       max_tokens=SETUP_MAX_TOKENS, effort='low', context={})
        finally:
            stage.facts.update(_probe_facts(probe))
        stage.facts['returned_model'] = generated.model
        if generated.data.get('ok') is not True:
            raise SetupFailed('reasoning_probe', 'validation_schema')
        if not ai.identity_accepted(generated.model, reasoning_model, reasoning_entry.get('canonical')):
            raise SetupFailed('reasoning_probe', 'model_mismatch')

    with _Stage('embedding_probe', requested_model=embedding_model, catalog_model=identity) as stage:
        try:
            raw = probe._post('/embeddings', {'model': embedding_model, 'input': ['Synthetic setup check.']}, 'embedding')
        finally:
            stage.facts.update(_probe_facts(probe))
        data = raw.get('data') or []
        vector = data[0].get('embedding') if len(data) == 1 and isinstance(data[0], dict) else None
        if not isinstance(vector, list) or not vector:
            raise SetupFailed('embedding_probe', 'embedding_shape')
        stage.facts['actual_dimension'] = len(vector)
        returned = raw.get('model')
        stage.facts['returned_model'] = returned
        if returned is not None and not ai.identity_accepted(returned, embedding_model, identity):
            raise SetupFailed('embedding_probe', 'embedding_model')

    candidate = ai.OpenRouterProvider(key, reasoning_model, embedding_model, len(vector), transport=_transport,
                                      reasoning_effort=reasoning_effort, embedding_identity=identity)
    with _Stage('query_probe', requested_model=embedding_model, catalog_model=identity,
                expected_dimension=len(vector)) as stage:
        try:
            result = candidate.embed(['Synthetic setup check.'], 'query')
        finally:
            stage.facts.update(_probe_facts(candidate))
            if candidate.last_call.get('actual_dimension'):
                stage.facts['actual_dimension'] = candidate.last_call['actual_dimension']
        stage.facts['returned_model'] = result.model
        stage.facts['actual_dimension'] = len(result.vectors[0])

    with _Stage('validation', requested_model=embedding_model, identity=identity, expected_dimension=len(vector),
                space=candidate.embedding_space):
        if candidate.embedding_dimension <= 0 or not candidate.accepts_embedding_model(result.model):
            raise SetupFailed('validation', 'embedding_model')
    return Tested(candidate, identity)


@router.post('/admin/ai/openrouter/save')
def save_openrouter(request: Request, api_key: str = Form(''), reasoning_model: str = Form(''), embedding_model: str = Form(''),
                    reasoning_effort: str = Form(''), revision: int = Form(-1), ctx: Ctx = Depends(admin_mutation)):
    throttle(request, 'admin-openrouter', limit=20, window=3600)
    submitted_key, reasoning_model, embedding_model = api_key.strip(), reasoning_model.strip(), embedding_model.strip()
    submitted = {'reasoning_model': reasoning_model, 'embedding_model': embedding_model,
                 'reasoning_effort': reasoning_effort == 'yes'}
    saved = operator_settings.values(ctx.s)
    key = submitted_key or saved.get('openrouter_api_key', '')
    if not _valid_key(key) or not reasoning_model or not embedding_model:
        return admin_page(request, ctx, 400, 'Enter a valid key and exact model IDs.', submitted=submitted)
    with diagnostics.operation() as op:
        reference = op['operation_id']
        started = time.monotonic()
        security.emit('admin_openrouter_test_started', reasoning_model=reasoning_model, requested_model=embedding_model,
                      outcome='key_submitted' if submitted_key else 'key_saved')
        try:
            tested = setup_test(key, reasoning_model, embedding_model, reasoning_effort=reasoning_effort == 'yes', saved=saved)
            candidate = tested.provider
            with _Stage('commit', requested_model=embedding_model, identity=tested.embedding_identity,
                        expected_dimension=candidate.embedding_dimension):
                try:
                    new_revision, staged = operator_settings.set_openrouter(
                        ctx.s, key=key, reasoning_model=reasoning_model, embedding_model=embedding_model,
                        embedding_identity=tested.embedding_identity, embedding_dimension=candidate.embedding_dimension,
                        reasoning_effort=reasoning_effort == 'yes', by=ctx.account.email, expected_revision=revision)
                    operator_settings.record_test(ctx.s, ok=True, error=None, by=ctx.account.email, reference=reference)
                    ctx.s.commit()
                except ValueError:
                    ctx.s.rollback()
                    raise SetupFailed('commit', 'stale_revision') from None
        except SetupFailed as failure:
            ctx.s.rollback()
            operator_settings.record_test(ctx.s, ok=False, error=f'{failure.stage}: {failure.code}', by=ctx.account.email,
                                          reference=reference)
            ctx.s.commit()
            security.emit('admin_openrouter_test_failed', level='warn', provider='openrouter', stage=failure.stage,
                          code=failure.code, duration_ms=int((time.monotonic() - started) * 1000), **failure.facts)
            status = 409 if failure.code == 'stale_revision' else 400
            return admin_page(request, ctx, status, explain(failure) + ' Existing settings were kept.',
                              submitted=submitted, reference=reference)
        security.emit('admin_openrouter_saved', audit=True, provider='openrouter', revision=new_revision,
                      reasoning_model=reasoning_model, requested_model=embedding_model, identity=tested.embedding_identity,
                      expected_dimension=candidate.embedding_dimension, outcome='staged' if staged else 'active',
                      duration_ms=int((time.monotonic() - started) * 1000))
    ai.reload_operator()
    return redirect('/admin', notice='admin-saved')


@router.post('/admin/ai/openrouter/disable')
def disable_openrouter(ctx: Ctx = Depends(admin_mutation)):
    operator_settings.disable(ctx.s, ctx.account.email)
    ctx.s.commit()
    ai.reload_operator()
    security.emit('admin_openrouter_disabled', audit=True, provider='openrouter', revision=operator_settings.revision(ctx.s))
    return redirect('/admin', notice='admin-saved')


@router.post('/admin/ai/openrouter/key/remove')
def remove_openrouter_key(ctx: Ctx = Depends(admin_mutation)):
    operator_settings.remove_key(ctx.s, 'openrouter', ctx.account.email)
    operator_settings.disable(ctx.s, ctx.account.email)
    ctx.s.commit()
    ai.reload_operator()
    security.emit('admin_openrouter_key_removed', audit=True, provider='openrouter')
    return redirect('/admin', notice='admin-saved')


@router.post('/admin/ai/openrouter/pending/cancel')
def cancel_pending(ctx: Ctx = Depends(admin_mutation)):
    operator_settings.cancel_pending(ctx.s, ctx.account.email)
    ctx.s.commit()
    security.emit('admin_embedding_rebuild_cancelled', audit=True, provider='openrouter')
    return redirect('/admin', notice='admin-saved')
