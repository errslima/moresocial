"""Admin-only OpenRouter configuration and operational status."""
from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select

from . import ai, config, operator_settings, security
from .auth_routes import throttle
from .models import ProviderUsage
from .web import Ctx, Problem, is_admin, redirect, render, require_mutation, require_user

router = APIRouter()
_catalog: tuple[float, dict] | None = None
CATALOG_SECONDS = 600


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
        with httpx.Client(timeout=5) as h:
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
                reasoning.append({'id': ident, 'reasoning': 'reasoning' in params})
        embedding = []
        for row in embedding_response.json().get('data') or []:
            if isinstance(row, dict) and isinstance(row.get('id'), str):
                # The dedicated endpoint is the contract. Keep a defensive modality
                # check only when the field is present, for forward-compatible data.
                outputs = set((row.get('architecture') or {}).get('output_modalities') or [])
                if not outputs or 'embeddings' in outputs:
                    ident = row['id']
                    embedding.append({'id': ident})
        data = {'reasoning': sorted(reasoning, key=lambda x: x['id']), 'embedding': sorted(embedding, key=lambda x: x['id']), 'error': None}
        _catalog = (time.monotonic(), data)
        return data
    except (httpx.HTTPError, ValueError, TypeError):
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


def admin_page(request: Request, ctx: Ctx, status: int = 200, error: str | None = None):
    settings, stored, rows = config.get(), operator_settings.values(ctx.s), operator_settings.rows(ctx.s)
    meta = catalog()
    reasoning = stored.get(operator_settings.REASONING_MODEL) or settings.openrouter_generation_model
    embedding = stored.get(operator_settings.EMBEDDING_MODEL) or settings.openrouter_embedding_model
    return render(request, 'admin.html', ctx, status=status, error=error,
                  enabled=stored.get(operator_settings.ENABLED) == '1' or (operator_settings.ENABLED not in rows and settings.ai_provider == 'openrouter'),
                  key_row=rows.get('openrouter_api_key'), reasoning_models=_with_selected(meta['reasoning'], reasoning),
                  embedding_models=_with_selected(meta['embedding'], embedding), reasoning_model=reasoning, embedding_model=embedding,
                  pending_embedding=stored.get(operator_settings.PENDING_EMBEDDING_MODEL), catalog_error=meta['error'],
                  revision=operator_settings.revision(ctx.s), last_test=stored.get(operator_settings.LAST_TEST_STATUS),
                  last_test_error=stored.get(operator_settings.LAST_TEST_ERROR), costs=_costs(ctx), limits=settings)


@router.get('/admin')
def admin_home(request: Request, ctx: Ctx = Depends(admin_only)):
    return admin_page(request, ctx)


def _valid_key(key: str) -> bool:
    return 10 <= len(key) <= 300 and all(c.isprintable() and not c.isspace() for c in key)


def _candidate(key: str, reasoning_model: str, embedding_model: str, *, reasoning_effort: bool) -> ai.OpenRouterProvider:
    """Perform tiny paid synthetic calls, learning the actual embedding dimension."""
    probe = ai.OpenRouterProvider(key, reasoning_model, embedding_model, 1, reasoning_effort=reasoning_effort)
    generated = probe.generate(system='Return the requested object.', prompt='Synthetic setup check.',
                               schema={'type': 'object', 'additionalProperties': False, 'required': ['ok'],
                                       'properties': {'ok': {'type': 'boolean'}}}, max_tokens=32, effort='low', context={})
    if generated.data.get('ok') is not True or generated.model != reasoning_model:
        raise ai.InvalidOutput('validation_schema')
    raw = probe._post('/embeddings', {'model': embedding_model, 'input': ['Synthetic setup check.']}, 'embedding')
    data = raw.get('data') or []
    vector = data[0].get('embedding') if len(data) == 1 and isinstance(data[0], dict) else None
    if not isinstance(vector, list) or not vector:
        raise ai.InvalidOutput('embedding_shape')
    candidate = ai.OpenRouterProvider(key, reasoning_model, embedding_model, len(vector), reasoning_effort=reasoning_effort)
    candidate.embed(['Synthetic setup check.'], 'query')
    return candidate


@router.post('/admin/ai/openrouter/save')
def save_openrouter(request: Request, api_key: str = Form(''), reasoning_model: str = Form(''), embedding_model: str = Form(''),
                    reasoning_effort: str = Form(''), revision: int = Form(-1), ctx: Ctx = Depends(admin_mutation)):
    throttle(request, 'admin-openrouter', limit=20, window=3600)
    submitted_key, reasoning_model, embedding_model = api_key.strip(), reasoning_model.strip(), embedding_model.strip()
    key = submitted_key or operator_settings.values(ctx.s).get('openrouter_api_key', '')
    if not _valid_key(key) or not reasoning_model or not embedding_model:
        return admin_page(request, ctx, 400, 'Enter a valid key and exact model IDs.')
    try:
        candidate = _candidate(key, reasoning_model, embedding_model, reasoning_effort=reasoning_effort == 'yes')
    except (ai.AIUnavailable, ai.InvalidOutput, ai.KeyRejected) as exc:
        code = str(exc).split(':', 1)[0]
        operator_settings.record_test(ctx.s, ok=False, error=code, by=ctx.account.email)
        ctx.s.commit()
        security.emit('admin_openrouter_test_failed', provider='openrouter', code=code)
        return admin_page(request, ctx, 400, 'Test failed (' + code + '); existing settings were kept.')
    try:
        operator_settings.set_openrouter(ctx.s, key=key, reasoning_model=reasoning_model, embedding_model=embedding_model,
                                         embedding_dimension=candidate.embedding_dimension, reasoning_effort=reasoning_effort == 'yes',
                                         by=ctx.account.email, expected_revision=revision)
        operator_settings.record_test(ctx.s, ok=True, error=None, by=ctx.account.email)
        ctx.s.commit()
    except ValueError:
        ctx.s.rollback()
        return admin_page(request, ctx, 409, 'Settings changed while the candidate was tested. Reload and try again.')
    ai.reload_operator()
    security.emit('admin_openrouter_saved', provider='openrouter')
    return redirect('/admin', notice='admin-saved')


@router.post('/admin/ai/openrouter/disable')
def disable_openrouter(ctx: Ctx = Depends(admin_mutation)):
    operator_settings.disable(ctx.s, ctx.account.email)
    ctx.s.commit()
    ai.reload_operator()
    return redirect('/admin', notice='admin-saved')


@router.post('/admin/ai/openrouter/key/remove')
def remove_openrouter_key(ctx: Ctx = Depends(admin_mutation)):
    operator_settings.remove_key(ctx.s, 'openrouter', ctx.account.email)
    operator_settings.disable(ctx.s, ctx.account.email)
    ctx.s.commit()
    ai.reload_operator()
    return redirect('/admin', notice='admin-saved')


@router.post('/admin/ai/openrouter/pending/cancel')
def cancel_pending(ctx: Ctx = Depends(admin_mutation)):
    operator_settings.cancel_pending(ctx.s, ctx.account.email)
    ctx.s.commit()
    return redirect('/admin', notice='admin-saved')
