"""Admin page: the global AI provider and the operator's API keys.

Only accounts listed in ADMIN_EMAILS (verified Google identities) can open it; everyone
else gets a plain 404. Keys are validated with the provider before they are stored,
stored encrypted, and never rendered. The web process applies a change immediately; the
worker picks it up within `ai.OPERATOR_REFRESH_SECONDS`.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select

from . import ai, ai_keys, config, operator_settings, security
from .auth_routes import throttle
from .models import Connection, UsageBudget
from .web import Ctx, Problem, is_admin, redirect, render, require_mutation, require_user

router = APIRouter()
LABELS = {'anthropic': 'Anthropic (Claude)', 'openai': 'OpenAI', 'voyage': 'Voyage AI (embeddings)'}
CHOICE_LABELS = {'anthropic': 'Anthropic (Claude)', 'openai': 'OpenAI', 'none': 'Off: only users with their own key get AI',
                 'fake': 'Synthetic (development)'}


def admin_only(ctx: Ctx = Depends(require_user)) -> Ctx:
    if not is_admin(ctx):
        raise Problem(404, 'Not found.')
    return ctx


def admin_mutation(ctx: Ctx = Depends(require_mutation)) -> Ctx:
    return admin_only(ctx)


def model_for(provider: str) -> str:
    settings = config.get()
    return {'anthropic': settings.anthropic_model, 'openai': settings.openai_model,
            'voyage': settings.embedding_model}[provider]


def file_key(provider: str) -> bool:
    settings = config.get()
    path = {'anthropic': settings.anthropic_api_key_file, 'voyage': settings.voyage_api_key_file}.get(provider)
    return bool(config.read_secret(path))


def validate_voyage(key: str) -> str | None:
    settings = config.get()
    if ai_keys._transport is None and settings.synthetic_providers:
        return 'invalid_key' if 'reject' in key else None
    body = {'input': ['ok'], 'model': settings.embedding_model, 'input_type': 'query',
            'output_dimension': settings.embedding_dimension}
    try:
        with httpx.Client(timeout=15, transport=ai_keys._transport) as h:
            r = h.post(ai.VoyageEmbedder.VOYAGE_URL, json=body, headers={'Authorization': 'Bearer ' + key})
    except httpx.HTTPError as exc:
        security.emit('admin_key_validation_failed', exc, provider='voyage')
        return 'unreachable'
    if r.is_success or r.status_code == 429:
        return None
    if r.status_code in (401, 403):
        return 'invalid_key'
    security.emit('admin_key_validation_failed', provider='voyage', status=r.status_code)
    return 'model_unavailable' if r.status_code in (400, 404) else 'unreachable'


def check_key(provider: str, key: str) -> str | None:
    if provider == 'voyage':
        if not 10 <= len(key) <= 300 or any(c.isspace() or not c.isprintable() for c in key):
            return 'format'
        return validate_voyage(key)
    return ai_keys.check_format(provider, key) or ai_keys.validate(provider, key)


def message(provider: str, reason: str) -> str:
    return ai_keys.REASONS.get(reason, ai_keys.REASONS['invalid_key']).format(label=LABELS[provider],
                                                                              model=model_for(provider))


def admin_page(request: Request, ctx: Ctx, status: int = 200, error: dict | None = None):
    settings = config.get()
    stored = operator_settings.rows(ctx.s)
    choice_row = stored.get(operator_settings.PROVIDER)
    choice = choice_row.value if choice_row else settings.ai_provider
    keys = []
    for p in operator_settings.KEYS:
        row = stored.get(operator_settings.key_name(p))
        keys.append({'provider': p, 'label': LABELS[p], 'model': model_for(p),
                     'row': row if row is not None and row.value else None,
                     'removed': row if row is not None and not row.value else None,
                     'from_file': file_key(p) and not (row is not None and row.value)})
    operator = ai.provider()
    used = ctx.s.scalar(select(UsageBudget.tokens).where(UsageBudget.scope == 'global', UsageBudget.day == ai.today())) or 0
    own_keys = ctx.s.scalar(select(func.count(func.distinct(Connection.workspace_id))).where(
        Connection.provider.in_(config.USER_KEY_PROVIDERS), Connection.state == 'active'))
    return render(request, 'admin.html', ctx, status=status, error=error, choice=choice, choice_row=choice_row,
                  choices=[c for c in operator_settings.CHOICES] + (['fake'] if choice == 'fake' else []),
                  choice_labels=CHOICE_LABELS, keys=keys, operator=operator, used=used, own_keys=own_keys,
                  limits=settings)


@router.get('/admin')
def admin_home(request: Request, ctx: Ctx = Depends(admin_only)):
    return admin_page(request, ctx)


@router.post('/admin/ai/provider')
def admin_set_provider(request: Request, provider: str = Form(''), ctx: Ctx = Depends(admin_mutation)):
    if provider not in operator_settings.CHOICES:
        raise Problem(400, 'Unknown provider.')
    stored = operator_settings.values(ctx.s)
    if provider != 'none' and not (stored.get(operator_settings.key_name(provider)) or file_key(provider)):
        return admin_page(request, ctx, status=400, error={'section': 'provider',
                          'message': f'Add a working {LABELS[provider]} key below before selecting it.'})
    operator_settings.set_provider(ctx.s, provider, ctx.account.email)
    ctx.s.commit()
    ai.reload_operator()
    security.emit('admin_ai_provider_set', provider=provider)
    return redirect('/admin', notice='admin-saved')


def key_provider(provider: str) -> str:
    if provider not in operator_settings.KEYS:
        raise Problem(404, 'Not found.')
    return provider


@router.post('/admin/ai/keys/{provider}')
def admin_set_key(request: Request, provider: str, api_key: str = Form(''), ctx: Ctx = Depends(admin_mutation)):
    provider = key_provider(provider)
    throttle(request, 'admin-key', limit=20, window=3600)
    key = (api_key or '').strip()
    reason = check_key(provider, key)
    if reason:
        security.emit('admin_ai_key_not_saved', provider=provider, code=reason)
        return admin_page(request, ctx, status=400, error={'section': provider, 'message': message(provider, reason)})
    operator_settings.set_key(ctx.s, provider, key, ctx.account.email)
    ctx.s.commit()
    ai.reload_operator()
    security.emit('admin_ai_key_saved', provider=provider)
    return redirect('/admin', notice='admin-saved')


@router.post('/admin/ai/keys/{provider}/remove')
def admin_remove_key(provider: str, ctx: Ctx = Depends(admin_mutation)):
    provider = key_provider(provider)
    operator_settings.remove_key(ctx.s, provider, ctx.account.email)
    ctx.s.commit()
    ai.reload_operator()
    security.emit('admin_ai_key_removed', provider=provider)
    return redirect('/admin', notice='admin-saved')
