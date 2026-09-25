"""Persisted, secret-safe OpenRouter configuration.

The settings table is intentionally used instead of process environment for normal
operator changes.  A NULL key and an explicit ``ai_enabled=0`` are tombstones: they
must win over a lower-priority file bootstrap.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DB

from . import security
from .models import OperatorSetting

PROVIDER = 'ai_provider'  # historical name; values are only openrouter|none
CHOICES = ('openrouter', 'none')
KEYS = ('openrouter',)
ENABLED = 'ai_enabled'
REASONING_MODEL = 'openrouter_reasoning_model'
REASONING_EFFORT = 'openrouter_reasoning_effort'
EMBEDDING_MODEL = 'openrouter_embedding_model'
EMBEDDING_DIMENSION = 'openrouter_embedding_dimension'
EMBEDDING_CONTRACT = 'openrouter_embedding_contract'
PENDING_EMBEDDING_MODEL = 'openrouter_pending_embedding_model'
PENDING_EMBEDDING_DIMENSION = 'openrouter_pending_embedding_dimension'
PENDING_EMBEDDING_CONTRACT = 'openrouter_pending_embedding_contract'
REVISION = 'ai_revision'
LAST_TEST_AT = 'openrouter_last_test_at'
LAST_TEST_STATUS = 'openrouter_last_test_status'
LAST_TEST_ERROR = 'openrouter_last_test_error'


def key_name(provider: str) -> str:
    return provider + '_api_key'


def stamp(s: DB) -> datetime | None:
    """Changes whenever any setting is saved or removed."""
    return s.scalar(select(func.max(OperatorSetting.updated_at)))


def values(s: DB) -> dict[str, str]:
    """Current values, secrets decrypted. Only the AI tier builder should call this."""
    out = {}
    for row in s.scalars(select(OperatorSetting).where(OperatorSetting.value.is_not(None))):
        value = security.decrypt(row.value) if row.name.endswith('_api_key') else row.value
        if value:
            out[row.name] = value
    return out


def rows(s: DB) -> dict[str, OperatorSetting]:
    """Metadata for display (hint, who, when); callers must not render `value`."""
    return {r.name: r for r in s.scalars(select(OperatorSetting))}


def _put(s: DB, name: str, value: str | None, hint: str | None, by: str) -> None:
    fields = {'value': value, 'hint': hint, 'updated_by': by, 'updated_at': func.clock_timestamp()}
    s.execute(insert(OperatorSetting).values(name=name, **fields)
              .on_conflict_do_update(index_elements=['name'], set_=fields))


def set_provider(s: DB, provider: str, by: str) -> None:
    _put(s, PROVIDER, provider, None, by)
    _put(s, ENABLED, '1' if provider == 'openrouter' else '0', None, by)


def set_key(s: DB, provider: str, key: str, by: str) -> None:
    _put(s, key_name(provider), security.encrypt(key), key[-4:], by)


def remove_key(s: DB, provider: str, by: str) -> None:
    _put(s, key_name(provider), None, None, by)


def _revision(s: DB) -> int:
    row = s.get(OperatorSetting, REVISION)
    try:
        return int(row.value) if row and row.value else 0
    except ValueError:
        return 0


def revision(s: DB) -> int:
    return _revision(s)


def set_openrouter(s: DB, *, key: str, reasoning_model: str, embedding_model: str,
                   embedding_dimension: int, reasoning_effort: bool, by: str,
                   expected_revision: int | None = None) -> tuple[int, bool]:
    """Atomically activate a tested candidate.

    An embedding change is staged.  The active index remains queryable until the
    worker has populated the pending space and cuts it over.
    """
    current = _revision(s)
    if expected_revision is not None and expected_revision != current:
        raise ValueError('stale configuration test')
    old_model = s.get(OperatorSetting, EMBEDDING_MODEL)
    old_dim = s.get(OperatorSetting, EMBEDDING_DIMENSION)
    old_model_value = old_model.value if old_model else None
    old_dim_value = old_dim.value if old_dim else None
    contract = 'v1:plain-text:document-query'
    changed_space = bool(old_model_value and (old_model_value != embedding_model or old_dim_value != str(embedding_dimension)))
    set_key(s, 'openrouter', key, by)
    _put(s, PROVIDER, 'openrouter', None, by)
    _put(s, ENABLED, '1', None, by)
    _put(s, REASONING_MODEL, reasoning_model, None, by)
    _put(s, REASONING_EFFORT, '1' if reasoning_effort else '0', None, by)
    if changed_space:
        _put(s, PENDING_EMBEDDING_MODEL, embedding_model, None, by)
        _put(s, PENDING_EMBEDDING_DIMENSION, str(embedding_dimension), None, by)
        _put(s, PENDING_EMBEDDING_CONTRACT, contract, None, by)
    else:
        _put(s, EMBEDDING_MODEL, embedding_model, None, by)
        _put(s, EMBEDDING_DIMENSION, str(embedding_dimension), None, by)
        _put(s, EMBEDDING_CONTRACT, contract, None, by)
    new_revision = current + 1
    _put(s, REVISION, str(new_revision), None, by)
    return new_revision, changed_space


def record_test(s: DB, *, ok: bool, error: str | None, by: str) -> None:
    _put(s, LAST_TEST_AT, datetime.utcnow().isoformat(timespec='seconds') + 'Z', None, by)
    _put(s, LAST_TEST_STATUS, 'ok' if ok else 'failed', None, by)
    _put(s, LAST_TEST_ERROR, error if not ok else None, None, by)


def disable(s: DB, by: str) -> None:
    set_provider(s, 'none', by)


def cancel_pending(s: DB, by: str) -> None:
    for name in (PENDING_EMBEDDING_MODEL, PENDING_EMBEDDING_DIMENSION, PENDING_EMBEDDING_CONTRACT):
        _put(s, name, None, None, by)


def promote_pending(s: DB, by: str = 'worker') -> bool:
    pending = values(s)
    model = pending.get(PENDING_EMBEDDING_MODEL)
    dimension = pending.get(PENDING_EMBEDDING_DIMENSION)
    if not model or not dimension:
        return False
    _put(s, EMBEDDING_MODEL, model, None, by)
    _put(s, EMBEDDING_DIMENSION, dimension, None, by)
    _put(s, EMBEDDING_CONTRACT, pending.get(PENDING_EMBEDDING_CONTRACT) or 'v1:plain-text:document-query', None, by)
    cancel_pending(s, by)
    _put(s, REVISION, str(_revision(s) + 1), None, by)
    return True
