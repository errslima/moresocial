"""Read-only, aggregate inventory of retired schema (docs/cleanup/03-historical-schema.md).

    python -m app.legacy_inventory

Prints one JSON document: the schema revision, which retired tables/columns exist, fixed
count buckets and whether each removal group is eligible. It never prints keys, hints,
fingerprints, IDs, message text, URLs or the connection string, and it never decrypts
anything or loads provider configuration. It runs in one READ ONLY, REPEATABLE READ
transaction on its own connection and uses catalog introspection plus explicit SQL (not
the ORM), so it works on schemas from before and after the cleanup. Exit 0 means the
inventory completed (even if nothing is eligible); any other exit means it did not, and
nothing was inferred.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from sqlalchemy import create_engine, pool, text

FORMAT = 1
# Always present in every supported revision; their absence means an unsupported schema.
REQUIRED = {
    'answer_sources': ('id', 'answer_id'),
    'events': ('id', 'origin'),
    'connections': ('id', 'provider', 'access_token_enc', 'refresh_token_enc'),
    'workspaces': ('id',),
    'operator_settings': ('name', 'value'),
    'provider_usage': ('id', 'billing', 'status'),
    'self_profiles': ('workspace_id', 'preferences', 'goals'),
}
GATHERING_TABLES = ('event_candidates', 'event_people', 'drafts')
KEY_COLUMNS = (('connections', 'key_hint'), ('connections', 'validated_at'), ('workspaces', 'ai_preference'))
PERSONAL_KEY_PROVIDERS = ('anthropic', 'openai')
LEGACY_OPERATOR_KEYS = ('anthropic_api_key', 'openai_api_key', 'voyage_api_key')
EVENT_ORIGINS = ('legacy', 'detected', 'calendar')
BILLING = ('operator', 'user')
USAGE_STATUS = ('reserved', 'reconciled', 'failed')
RETAINED = ('events (including origin=legacy) and all event identity/mention tables',
            'answers, answer citations, sources and suggestions',
            'provider_usage, usage_budgets and historical provider/billing labels',
            'operator_settings rows and encrypted personal-key rows (separate credential decision)',
            'self_profiles (account creation and migration 0007 depend on it)')


class InventoryError(Exception):
    """A fixed, secret-free reason code."""


def _bucket(rows, known) -> dict[str, int]:
    out = {k: 0 for k in known}
    for value, count in rows:
        key = value if value in out else 'other'
        out[key] = out.get(key, 0) + int(count)
    return out


def _scalar(c, sql: str) -> int:
    return int(c.execute(text(sql)).scalar() or 0)


def _known_revisions() -> tuple[set[str], str | None]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    import app
    cfg = Config(str(Path(app.__file__).resolve().parents[1] / 'alembic.ini'))
    scripts = ScriptDirectory.from_config(cfg)
    return {r.revision for r in scripts.walk_revisions()}, scripts.get_current_head()


def collect(c) -> dict:
    """Inventory through connection `c`, already inside a read-only transaction."""
    columns: dict[str, set[str]] = {}
    for table, column in c.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema()")):
        columns.setdefault(table, set()).add(column)

    def has(table: str, column: str | None = None) -> bool:
        return table in columns and (column is None or column in columns[table])

    if not has('alembic_version', 'version_num'):
        raise InventoryError('schema_unversioned')
    versions = [r[0] for r in c.execute(text('SELECT version_num FROM alembic_version'))]
    known, head = _known_revisions()
    if len(versions) != 1 or versions[0] not in known:
        raise InventoryError('schema_revision_unsupported')
    for table, required in REQUIRED.items():
        if not all(has(table, col) for col in required):
            raise InventoryError('schema_required_object_missing')

    report: dict = {'revision': versions[0], 'code_head': head}
    groups: dict = {}

    # G: manual gathering schema.
    present = [t for t in GATHERING_TABLES if has(t)]
    draft_column = has('answer_sources', 'draft_id')
    if (present or draft_column) and (len(present) != len(GATHERING_TABLES) or not draft_column):
        raise InventoryError('schema_partial_gathering_removal')
    answers = {'rows': _scalar(c, 'SELECT count(*) FROM answer_sources'),
               'draft_id_column': 'present' if draft_column else 'absent'}
    if present:
        tables = {t: {'state': 'present', 'rows': _scalar(c, f'SELECT count(*) FROM {t}')} for t in GATHERING_TABLES}
        answers.update(
            with_draft=_scalar(c, 'SELECT count(*) FROM answer_sources WHERE draft_id IS NOT NULL'),
            with_answer_and_draft=_scalar(c, 'SELECT count(*) FROM answer_sources WHERE answer_id IS NOT NULL AND draft_id IS NOT NULL'),
            with_neither=_scalar(c, 'SELECT count(*) FROM answer_sources WHERE answer_id IS NULL AND draft_id IS NULL'))
        reasons = [f'{t}_nonempty' for t in GATHERING_TABLES if tables[t]['rows']]
        reasons += [code for code, key in (('draft_linked_answer_sources', 'with_draft'),
                                           ('ambiguous_answer_source_both', 'with_answer_and_draft'),
                                           ('ambiguous_answer_source_neither', 'with_neither')) if answers[key]]
        groups['G'] = {'status': 'ineligible' if reasons else 'eligible', 'reasons': reasons}
    else:
        tables = {t: {'state': 'absent'} for t in GATHERING_TABLES}
        answers['with_neither'] = _scalar(c, 'SELECT count(*) FROM answer_sources WHERE answer_id IS NULL')
        groups['G'] = {'status': 'removed', 'reasons': []}
    report.update(tables)
    report['answer_sources'] = answers
    report['events'] = {'by_origin': _bucket(c.execute(text('SELECT origin, count(*) FROM events GROUP BY origin')),
                                             EVENT_ORIGINS)}

    # K: personal-key metadata columns.
    personal = dict(c.execute(text(
        'SELECT provider, count(*) FROM connections WHERE provider IN (:a, :b) GROUP BY provider'),
        {'a': PERSONAL_KEY_PROVIDERS[0], 'b': PERSONAL_KEY_PROVIDERS[1]}).all())
    connections = {
        'personal_key_rows': {p: int(personal.get(p, 0)) for p in PERSONAL_KEY_PROVIDERS},
        'personal_key_rows_with_credential': _scalar(c, (
            "SELECT count(*) FROM connections WHERE provider IN ('anthropic', 'openai') "
            'AND (access_token_enc IS NOT NULL OR refresh_token_enc IS NOT NULL)')),
    }
    key_present = [has(t, col) for t, col in KEY_COLUMNS]
    if any(key_present) and not all(key_present):
        raise InventoryError('schema_partial_key_metadata_removal')
    reasons = [f'{p}_connection_rows' for p in PERSONAL_KEY_PROVIDERS if connections['personal_key_rows'][p]]
    workspaces: dict = {}
    for table, column in KEY_COLUMNS:
        target = connections if table == 'connections' else workspaces
        if not all(key_present):
            target[column] = {'state': 'absent'}
            continue
        entry = {'state': 'present', 'non_null': _scalar(c, f'SELECT count(*) FROM {table} WHERE {column} IS NOT NULL')}
        if table == 'connections':
            entry['non_null_outside_personal_keys'] = _scalar(c, (
                f"SELECT count(*) FROM connections WHERE {column} IS NOT NULL AND provider NOT IN ('anthropic', 'openai')"))
        target[column] = entry
        if entry['non_null']:
            reasons.append(f'{table}_{column}_not_null')
    report['connections'] = connections
    report['workspaces'] = workspaces
    groups['K'] = ({'status': 'removed', 'reasons': []} if not all(key_present) else
                   {'status': 'ineligible' if reasons else 'eligible', 'reasons': reasons})

    # Retained regardless of the groups; counted to explain the retention decisions.
    legacy = {name: {'rows': 0, 'with_value': 0} for name in LEGACY_OPERATOR_KEYS}
    for name, rows, with_value in c.execute(text(
            'SELECT name, count(*), count(value) FROM operator_settings WHERE name IN (:a, :b, :c) GROUP BY name'),
            dict(zip('abc', LEGACY_OPERATOR_KEYS))):
        legacy[name] = {'rows': int(rows), 'with_value': int(with_value)}
    report['operator_settings'] = {
        'legacy_keys': legacy,
        'legacy_provider_choice': _scalar(c, (
            "SELECT count(*) FROM operator_settings WHERE name = 'ai_provider' "
            "AND value IS NOT NULL AND value NOT IN ('openrouter', 'none')")),
    }
    usage = {b: {s: 0 for s in USAGE_STATUS} for b in BILLING}
    for billing, status, count in c.execute(text(
            'SELECT billing, status, count(*) FROM provider_usage GROUP BY billing, status')):
        row = usage.setdefault(billing if billing in BILLING else 'other', {})
        key = status if status in USAGE_STATUS else 'other'
        row[key] = row.get(key, 0) + int(count)
    report['provider_usage'] = {'by_billing_status': usage}
    report['self_profiles'] = {
        'rows': _scalar(c, 'SELECT count(*) FROM self_profiles'),
        'with_preferences': _scalar(c, "SELECT count(*) FROM self_profiles WHERE btrim(preferences) <> ''"),
        'with_goals': _scalar(c, "SELECT count(*) FROM self_profiles WHERE btrim(goals) <> ''"),
    }
    return {'schema': report, 'groups': groups, 'retained': list(RETAINED)}


def run(database_url: str) -> dict:
    from app.db import normalize_url
    engine = create_engine(normalize_url(database_url), poolclass=pool.NullPool)
    try:
        with engine.connect() as c:
            c = c.execution_options(isolation_level='REPEATABLE READ', postgresql_readonly=True)
            with c.begin():
                mode = c.execute(text('SELECT current_setting(\'transaction_read_only\'), '
                                      'current_setting(\'transaction_isolation\')')).one()
                if mode[0] != 'on' or mode[1] != 'repeatable read':
                    raise InventoryError('transaction_not_read_only')
                c.execute(text("SET LOCAL statement_timeout = '60s'"))
                c.execute(text("SET LOCAL lock_timeout = '5s'"))
                body = collect(c)
    finally:
        engine.dispose()
    return {'inventory': 'moresocial-legacy-schema', 'format': FORMAT,
            'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
            'transaction': {'read_only': True, 'isolation': 'repeatable read'}, **body}


def main(argv: list[str] | None = None) -> int:
    if argv:
        print(json.dumps({'status': 'error', 'error': 'usage: python -m app.legacy_inventory'}), file=sys.stderr)
        return 2
    try:
        from app import config
        report = run(config.get().database_url)
    except InventoryError as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}), file=sys.stderr)
        return 1
    except Exception as exc:  # never echo driver messages: they can name hosts, users or queries
        error = {'status': 'error', 'error': 'inventory_failed', 'type': type(exc).__name__}
        sqlstate = getattr(getattr(exc, 'orig', None), 'sqlstate', None)
        if isinstance(sqlstate, str) and sqlstate.isalnum() and len(sqlstate) == 5:
            error['sqlstate'] = sqlstate
        print(json.dumps(error), file=sys.stderr)
        return 1
    print(json.dumps({'status': 'ok', **report}, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
