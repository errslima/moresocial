"""Global AI settings chosen on the admin page: the operator tier's generation provider and
the operator's API keys. Stored values override the server environment (`AI_PROVIDER`,
secret files). Keys are encrypted like users' keys and never returned to a page.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DB

from . import security
from .models import OperatorSetting

PROVIDER = 'ai_provider'
CHOICES = ('anthropic', 'openai', 'none')       # generation provider of the operator tier
KEYS = ('anthropic', 'openai', 'voyage')        # operator API keys (voyage = embeddings)


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


def set_key(s: DB, provider: str, key: str, by: str) -> None:
    _put(s, key_name(provider), security.encrypt(key), key[-4:], by)


def remove_key(s: DB, provider: str, by: str) -> None:
    _put(s, key_name(provider), None, None, by)
