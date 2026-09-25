"""User-supplied AI provider API keys (Anthropic, OpenAI).

A key pays for its workspace's generation (see `ai.generator_for`). It is stored only
encrypted in `connections.access_token_enc`; the row also keeps a non-reversible
fingerprint and the last four characters for display. Keys are never logged, rendered,
put in URLs or job errors. Validation contacts fixed provider hosts only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

import httpx
from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DB

from . import ai, config, db, security
from .models import Connection, Job, Workspace
from .repo import Scoped

LABELS = {'anthropic': 'Anthropic (Claude)', 'openai': 'OpenAI'}
COMPANIES = {'anthropic': 'Anthropic', 'openai': 'OpenAI'}
PREFIXES = {'anthropic': 'sk-ant-', 'openai': 'sk-'}
CONSOLES = {'anthropic': 'https://console.anthropic.com/settings/keys', 'openai': 'https://platform.openai.com/api-keys'}
MODELS_URL = {'anthropic': 'https://api.anthropic.com/v1/models/{model}', 'openai': 'https://api.openai.com/v1/models/{model}'}
ANTHROPIC_VERSION = '2023-06-01'

# Messages refer to the key only by provider; the key itself is never echoed back.
REASONS = {
    'format': "That doesn't look like an {label} API key. Copy the whole key from the provider's console.",
    'wrong_provider': 'That is an Anthropic key. Add it under Anthropic instead.',
    'subscription_token': ('That is a Claude subscription sign-in token, not an API key. Moresocial can only use an '
                           'API key from the Anthropic console.'),
    'invalid_key': '{label} did not accept this key. It may be mistyped, revoked or expired.',
    'no_credit': 'The {label} account behind this key has no credit left or reached its spend limit.',
    'model_unavailable': 'This key cannot use the model Moresocial is configured for ({model}).',
    'unreachable': "Moresocial couldn't reach {label} to check the key. Nothing was saved; please try again.",
}

_transport: httpx.BaseTransport | None = None


def set_transport(transport: httpx.BaseTransport | None) -> None:
    """Tests: route validation requests through a mock transport."""
    global _transport
    _transport = transport


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enabled() -> tuple[str, ...]:
    return config.get().user_ai_keys


def model_for(provider: str) -> str:
    settings = config.get()
    return settings.anthropic_model if provider == 'anthropic' else (settings.openai_model or '')


def message(provider: str, reason: str) -> str:
    return REASONS.get(reason, REASONS['invalid_key']).format(label=LABELS[provider], model=model_for(provider))


def fingerprint(key: str) -> str:
    return 'fp:' + security.digest(key)[:16]


def check_format(provider: str, key: str) -> str | None:
    if key.startswith('sk-ant-oat'):
        # Claude.ai OAuth token (e.g. from `claude setup-token`): Anthropic does not permit third-party
        # products to collect or use these, so it is refused before any request and never stored.
        return 'subscription_token'
    if provider == 'openai' and key.startswith(PREFIXES['anthropic']):
        return 'wrong_provider'
    if not key.startswith(PREFIXES[provider]) or not 20 <= len(key) <= 300:
        return 'format'
    if any(c.isspace() or not c.isprintable() for c in key):
        return 'format'
    return None


def validate(provider: str, key: str) -> str | None:
    """One free request that proves the key works for the configured model. None = usable."""
    if _transport is None and config.get().synthetic_providers:
        # Synthetic stand-in: keys containing `reject` or `nomodel` fail, anything else passes.
        return 'invalid_key' if 'reject' in key else 'model_unavailable' if 'nomodel' in key else None
    url = MODELS_URL[provider].format(model=model_for(provider))
    headers = ({'x-api-key': key, 'anthropic-version': ANTHROPIC_VERSION} if provider == 'anthropic'
               else {'Authorization': 'Bearer ' + key})
    try:
        with httpx.Client(timeout=10, transport=_transport) as h:
            r = h.get(url, headers=headers)
    except httpx.HTTPError as exc:
        security.emit('ai_key_validation_failed', exc, provider=provider)
        return 'unreachable'
    if r.is_success or r.status_code == 429:  # rate limited means authenticated
        return None
    if r.status_code == 401:
        return 'invalid_key'
    if r.status_code == 402:
        return 'no_credit'
    if r.status_code in (403, 404):
        return 'model_unavailable'
    security.emit('ai_key_validation_failed', provider=provider, status=r.status_code)
    return 'unreachable'


def save(s: DB, workspace_id: uuid.UUID, provider: str, raw_key: str) -> str | None:
    """Validate and store a key. Returns a reason code when nothing was stored."""
    key = (raw_key or '').strip()
    reason = check_format(provider, key) or validate(provider, key)
    if reason:
        security.emit('ai_key_not_saved', provider=provider, code=reason)
        return reason
    fp = fingerprint(key)
    s.execute(insert(Connection).values(id=uuid.uuid4(), workspace_id=workspace_id, provider=provider,
                                        provider_account=fp, scopes=[], state='active')
              .on_conflict_do_nothing(index_elements=['workspace_id', 'provider']))
    ws = Scoped(s, workspace_id)
    conn = ws.first(ws.q(Connection).where(Connection.provider == provider).with_for_update())
    conn.provider_account, conn.access_token_enc, conn.key_hint = fp, security.encrypt(key), key[-4:]
    conn.state, conn.detail, conn.validated_at = 'active', None, utcnow()
    resume_paused_jobs(s, workspace_id)
    security.emit('ai_key_saved', provider=provider)
    return None


def resume_paused_jobs(s: DB, workspace_id: uuid.UUID) -> int:
    """AI jobs waiting for budget or a configured provider can run now on the new key."""
    return s.execute(update(Job).where(Job.workspace_id == workspace_id, Job.status == 'queued',
                                       Job.last_error.like('AI paused%') | Job.last_error.like('ai unavailable%'))
                     .values(run_after=func.now())).rowcount


def remove(s: DB, workspace_id: uuid.UUID, provider: str) -> None:
    Scoped(s, workspace_id).delete(Connection, Connection.provider == provider)
    s.execute(update(Workspace).where(Workspace.id == workspace_id, Workspace.ai_preference == provider)
              .values(ai_preference=None))
    security.emit('ai_key_removed', provider=provider)


def set_preference(s: DB, workspace_id: uuid.UUID, provider: str) -> None:
    s.execute(update(Workspace).where(Workspace.id == workspace_id).values(ai_preference=provider))


def mark_rejected(workspace_id: uuid.UUID, connection_id: uuid.UUID | None, provider: str, reason: str) -> None:
    """The provider refused a stored key at run time: the user must replace it."""
    with db.session() as s:
        s.execute(update(Connection).where(Connection.workspace_id == workspace_id, Connection.id == connection_id,
                                           Connection.state == 'active')
                  .values(state='reconnect_required', detail=reason))
    security.emit('ai_key_rejected', provider=provider, code=reason)


@dataclass
class KeyView:
    provider: str
    label: str
    console: str
    prefix: str
    conn: Connection | None
    problem: str | None


def status(ws: Scoped) -> dict:
    """View data for the Connections page. Contains no key material beyond the hint."""
    rows = {c.provider: c for c in ws.all(ws.q(Connection).where(Connection.provider.in_(enabled())))}
    keys = [KeyView(p, LABELS[p], CONSOLES[p], PREFIXES[p], rows.get(p),
                    message(p, rows[p].detail) if p in rows and rows[p].state != 'active' else None) for p in enabled()]
    tier = ai.generator_for(ws.wid)
    preference = ws.s.get(Workspace, ws.wid).ai_preference
    active = [k.provider for k in keys if k.conn is not None and k.conn.state == 'active']
    return {'providers': keys, 'labels': LABELS, 'company_names': COMPANIES,
            'companies': ' or '.join(COMPANIES[p] for p in enabled()), 'tier': tier.billing, 'tier_provider': tier.provider.name, 'active': active,
            'preference': preference if preference in active else (active[0] if active else None),
            'budget': ai.budget_state(ws.wid, tier.billing)}
