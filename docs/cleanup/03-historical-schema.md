# Plan 3: inventory and safely retire historical schema

Status: phase A reviewed and deployed on 2026-09-27; production inventory permits phase B group G.
Group G migration `0012` is implemented and locally verified; rollout is pending.
Group K is retained because production contains two encrypted personal-key records.
Depends on: plans 1 and 2 completed and verified.
Read [the handoff](README.md) first. Baseline schema was `0011`; determine the actual
next revision before creating a migration.

## Outcome and data decision gate

The user's subsequent review/deployment request authorizes the rollout recorded in
[schema rollout](schema-rollout.md). The original local-only execution result below
is historical; the new production inventory satisfies the gate for group G only.

This plan has two phases. **Phase A is always executable locally:** implement a
read-only inventory, test it, and document the data decisions. **Phase B removes
eligible empty structures with a guarded forward migration.** It may proceed only
after the intended target's inventory establishes eligibility. A synthetic empty
database is not evidence about production. If no target inventory is available,
finish phase A and report phase B as pending; do not silently assume emptiness.

The default data policy is preservation. Nonempty retired tables, old keys and
user-authored content are retained. Deleting or transforming them requires a
separate explicit disposition decision and a corresponding plan amendment. Do not
invent a lossy conversion from manual gathering data into detected Events, or
delete old encrypted keys just because their feature is retired.

## Read first

- `app/models.py`, `app/ingest.py`, `app/accounts.py`, `app/diary.py`
- `app/operator_settings.py`, post-plan-2 `app/ai.py`
- Migrations `0001`, `0003`, `0006`, `0007`, `0010`, `0011`
- `tests/test_migrations.py`, `tests/test_diary_groups.py`, `tests/test_review_fixes.py`,
  `tests/test_backup_restore.py`, `tests/test_workflow.py`
- `migrations/env.py`, `deploy/release.sh`, `deploy/rollback.sh`, current operator runbook

## Phase A, M1. Build a read-only inventory

Add `app/legacy_inventory.py`, callable as:

```bash
python -m app.legacy_inventory
```

Use the configured database through a dedicated connection and a PostgreSQL
read-only, repeatable-read transaction. All output is aggregate JSON: schema revision,
table/column names, counts, eligibility flags and fixed reason codes. Do not call
provider configuration loaders, `operator_settings.values`, or decryption helpers.
Do not emit keys, fingerprints, hints, personal IDs, message text, URLs or database
connection strings. Errors must also avoid echoing connection credentials.

Use explicit SQL or SQLAlchemy Core/reflection, not current ORM classes, so the
inventory can inspect the pre-cleanup schema and tolerate already-removed optional
objects. `information_schema`/catalog inspection should distinguish absent objects
from empty objects. Unknown/unsupported schema or query failure is an error, not
an empty result. No `--apply`, SSH default, data mutation or migration execution.

Report at least:

| Structure | Aggregate information | Decision it supports |
| --- | --- | --- |
| `event_candidates`, `event_people`, `drafts` | Row counts for each | Whether manual gathering tables are empty |
| `answer_sources` | Total rows, non-null `draft_id`, rows with both/neither answer and draft references | Whether removing the draft FK/column is lossless; anomalous references block removal |
| `events` | Counts grouped by `origin` only | Retain legacy events; not a deletion candidate |
| `connections` | Count of `anthropic`/`openai` rows; how many contain any credential value | Old personal-key presence; never decrypt |
| `connections.key_hint`, `validated_at` | Non-null counts across all providers | Whether columns are entirely unused, including unexpected Google rows |
| `workspaces.ai_preference` | Non-null count | Whether preference metadata can be removed losslessly |
| `operator_settings` | Count of rows/value-present rows for explicit legacy key names (`anthropic_api_key`, `openai_api_key`, `voyage_api_key`) and legacy provider-choice count | Separate future secret disposition; do not dump values |
| `provider_usage` | Counts by `billing` and `status`, including reserved historical user rows | Preserve accounting compatibility and history |
| `self_profiles` | Total rows and counts with nonblank preferences/goals | Explain retention and migration dependency |

Fixed enumerations/count buckets are enough; do not turn this into a general SQL
console. Missing old objects after a cleanup should report `absent`, not crash.
Exit 0 for a successful inventory (even if cleanup is ineligible), nonzero for a
failed/unsupported inventory. Eligibility is explicit in the JSON.

Add `tests/test_legacy_inventory.py` with disposable synthetic fixtures:

- Empty candidates are classified correctly; populated candidates become ineligible.
- Legacy provider secrets and text canaries never appear in stdout/stderr.
- No provider or decryption call occurs; the connection is read-only.
- Running the report leaves rows unchanged, including timestamps and values.
- Missing candidate tables/columns are handled via introspection in an isolated
  test schema/database; unknown required schema is rejected.

Do not let a temporary test schema mutation leak into other tests. Reuse the
repository's PostgreSQL fixtures or a separate disposable database.

## Phase A, M2. Make the decision explicit

Record a dated, aggregate inventory for the intended target in the execution result
or a private operator record. An executor without authorized target access should
provide the command and finish local tooling/tests; it must not guess the results.

The following groups are independent at planning time, but each selected migration
has fixed operations and identical schema results on every database:

| Group | Eligible only when | Default if ineligible |
| --- | --- | --- |
| G: manual gathering schema | All three tables empty; no draft-linked answer sources; no ambiguous both/neither references | Retain tables, models and all `ingest` compatibility handling |
| K: personal-key metadata columns | No Anthropic/OpenAI connection rows; both connection metadata columns entirely null; all `ai_preference` values null | Retain columns/models; no credential deletion |

Retain the following regardless of those groups:

- `events`, including `origin='legacy'`, and all event identity/mention tables.
- `answers`, answer citations, source data, Suggestions and its current draft fields.
- `provider_usage`, `usage_budgets`, historical provider/billing labels and user scopes.
- Old operator-settings rows and encrypted personal-key rows pending a separate
  credential-retention/revocation decision. Inventorying a key does not authorize revocation.
- `self_profiles`, its account-creation path, and `diary.migrate_profiles`. The table
  is still created/populated during account creation, and migration 0007 imports
  the helper. Removing it safely is a different migration-history project.

If neither G nor K is eligible, phase A still delivers a useful completed inventory.
Record phase B as deferred for nonempty data and retain its application handling.
Do not mark the whole schema-removal phase implemented in that case.

## Phase B, M3. Implement guarded removal for selected eligible groups

Create a new Alembic revision for the selected scope. Do not edit/squash historical
revisions. Use `op.get_bind()` and explicit SQL/Core tables; the migration must not
depend on the evolving application ORM or call AI providers.

1. At upgrade start, acquire the necessary table locks for selected candidates and
   their referring tables, then check **all** selected-group preconditions before
   any DDL. PostgreSQL transaction rollback must leave the database unchanged if a
   precondition fails. Use bounded lock/statement waits and an actionable non-secret
   error such as `legacy_gathering_data_present`; do not hang indefinitely.
2. Never use `if count: skip the drop` and stamp the same revision. Either every
   operation selected for this revision succeeds or the migration fails. The
   inventory report's earlier counts do not replace migration-time guards.
3. For group G, remove the actual draft foreign key/index/column from
   `answer_sources` in dependency order, then drop `drafts`, `event_people`, and
   `event_candidates`. Discover actual constraint names from the existing migration/
   catalog; do not guess. Do not use `DROP ... CASCADE` to hide unexpected dependencies.
4. For group K, drop only `connections.key_hint`, `connections.validated_at`, and
   `workspaces.ai_preference` after their guards. Keep OAuth credential columns,
   connection rows, operator settings and provider usage untouched.
5. Implement a structural downgrade that recreates exactly the removed empty
   tables/columns, indexes, workspace-scoped FKs and uniqueness constraints.
   Restore column nullability from the original migrations (answer_id was nullable).
   No deleted content needs recreation because populated candidates were refused.
   Do not claim this allows an uncoordinated live application rollback.

If target data changes between inventory and rollout, the migration must refuse
without erasing anything. Re-inventory and decide separately; never bypass the guard.

## Phase B, M4. Remove only the now-retired model dependencies

For group G, in the same change as its migration:

- Remove `EventCandidate`, `EventPerson`, `Draft`, and `AnswerSource.draft_id` plus
  its FK/index from `app/models.py`; preserve ordinary answer/source references.
- Remove the old draft/invitee branches from `ingest.invalidate`, `prune_people`,
  `link_people` and their imports. Preserve active source, claim, answer, person,
  Suggestion and Event behavior. Audit all callers before simplifying shared code.
- Update `AnswerSource` descriptions that refer to the removed draft table.

For group K, remove only the three matching model fields and outdated connection/
workspace comments. Account creation and Google OAuth must still work.

Do not remove handling for a group that the chosen migration does not retire.
Run `alembic check` on the disposable migrated database through the existing test
configuration, not against an ambient or production `DATABASE_URL`.

## Phase B, M5. Migration and behavior tests

Use an isolated database migrated to the revision immediately before the new one.
Seed pre-cleanup rows with explicit SQL/Core tables; new ORM metadata no longer
contains selected legacy models. Cover:

1. Fresh `base -> head` with final metadata matching the database.
2. Previous head with empty selected candidates upgrades successfully; active
   accounts, Google grants, sources, answers/citations, Suggestions, current Events,
   event identity decisions and usage history remain intact.
3. Each independently relevant nonempty condition rejects upgrade and leaves the
   old revision/schema/data intact. Include ambiguous answer references and non-null
   key metadata on a non-retired connection, not only the obvious legacy rows.
4. Guard failure preserves synthetic secret/text canaries without printing them.
5. Upgrade -> downgrade -> upgrade of eligible data restores the old structure and
   preserves active data. Assert FK/index/uniqueness structure, not just table names.
6. Existing migration 0003 usage-history and migration 0007 nonempty-profile tests
   still work through head. Do not remove their historical imports to make upgrades pass.
7. Source exclusion/deletion, person linking, account deletion, Suggestions drafts,
   old event redirects and automatic Events remain correct after model cleanup.

Run the focused migration/inventory suites while developing, then the full
`bash scripts/test.sh`, including backup/restore and browser checks.

## Phase B, M6. Write the deployment/rollback procedure; do not deploy

This is a non-additive schema change. The existing `deploy/release.sh` migrates
before replacing web/worker, while old services may still use the removed columns.
`deploy/rollback.sh` assumes additive compatibility. Neither is safe to use blindly.

Add a specific runbook section or `docs/cleanup/schema-rollout.md` covering:

1. Record selected groups, target revision, aggregate inventory and the last schema-
   compatible application revision; take and verify an encrypted restorable backup.
2. Build/validate images before downtime. Quiesce jobs and writers, including web,
   worker and connector ingestion, and account for the connector provisioner so it
   cannot recreate traffic unexpectedly. Preserve connector sessions and queued data.
3. Run the guarded migration while old application processes are stopped; if it
   refuses, keep the old schema and recover the previous services, then report why.
4. Start matching new code, validate readiness, then restore ingestion/workers.
   Record read-only functional checks; no test messages or paid provider probes.
5. For rollback, quiesce again and restore the previous schema structure with the
   tested downgrade before starting schema-dependent older code. Do not overwrite
   newer user writes with an old backup. A backup restore is a separate recovery action.

The runbook must explicitly say that ordinary image-only rollback is incompatible
after the drops. Document commands only after checking actual Compose service names,
deployment paths and migration image invocation. Do not modify live services as part
of writing/testing this implementation.

## Acceptance criteria

Phase A:

- [x] Inventory runs locally with aggregate, secret-safe, read-only output and tests.
- [x] Every candidate has an explicit retained/eligible/unknown decision with evidence.
- [x] Missing production access is stated; synthetic counts are not presented as live counts.

Phase B (only if the decision gate is satisfied; **deferred** on 2026-09-27, see result):

- [ ] Fixed-scope guarded migration and matching model cleanup; no historical migration edits.
- [ ] Nonempty selected candidates fail atomically without data loss.
- [ ] Active product data, accounting and old event/profile compatibility are preserved.
- [ ] Fresh upgrade, populated refusal, structural downgrade and full suite pass.
- [ ] Deployment/rollback procedure addresses old running code and connector traffic.
- [x] No live migration, data deletion, credential revocation or deployment was performed.

## Execution result

Executed 2026-09-27 on the workstation, after plans 1 and 2, as an uncommitted local
diff on top of `bbb8379`. Nothing was deployed and no production system was contacted.

- Starting revision and migration head: `bbb8379` plus the plan 1–2 working tree;
  Alembic head `0011` (unchanged: no migration was created).
- Phase A commands/tests and resulting tool:
  - `app/legacy_inventory.py`, run as `python -m app.legacy_inventory` (also runnable
    as `python - < app/legacy_inventory.py` inside an image that predates it). Own
    `NullPool` connection, one `READ ONLY, REPEATABLE READ` transaction (verified from
    the server before querying, with `statement_timeout` 60 s and `lock_timeout` 5 s),
    `information_schema` introspection and explicit SQL only. Output is aggregate JSON:
    revision and code head, per-structure `present`/`absent` state, fixed count
    buckets, `G`/`K` status (`eligible` | `ineligible` | `removed`) with fixed reason
    codes, and the always-retained list. Unknown revision, missing required objects,
    partial removals, or any query/connection failure exit 1 with a fixed error code
    (driver messages are never printed; only the exception type and SQLSTATE); extra
    arguments exit 2. No config provider loaders, `operator_settings.values`, or
    decryption; no `--apply`, no SSH default.
  - `tests/test_legacy_inventory.py` (5 tests): empty candidates eligible; populated
    candidates (manual gathering rows, draft-linked, both and neither answer
    references, Anthropic connection with a credential, Google `key_hint`,
    `ai_preference`, legacy operator key/provider rows, historical user-billed usage,
    nonblank profile) ineligible with exact reasons and no key, hint, fingerprint, ID,
    text, role, password or ciphertext canary in the output; decryption/provider/
    secret loaders patched to fail and the report still succeeds; all application
    tables byte-identical before/after; a write inside the inventory transaction fails
    with SQLSTATE `25006`; removed tables/columns report `absent`/`removed`, partial
    removals, an unknown revision, a missing required table and a missing
    `alembic_version` are rejected (in a separate disposable database, dropped
    afterwards); the module and stdin CLI forms work in a subprocess, and a failed
    connection prints neither the database name, role, password nor URL.
  - `uv run --frozen pytest -o addopts='' -q tests/test_legacy_inventory.py`: **5 passed**.
- Target inventory source/date: **local synthetic disposable databases only
  (2026-09-27)**. No production inventory was taken: the cleanup handoff limits this
  work to local synthetic data and grants no production database access, so the
  production counts are **unknown**. The synthetic counts in the tests are not
  production evidence. Indications from the recorded history (not counts): manual
  gathering planning was part of earlier production releases, and personal keys were
  enabled in production on 2026-09-25 (`USER_AI_KEYS=anthropic,openai`, see
  `docs/implementation-status.md`), so either group may hold rows.
- Group G decision and evidence: **unknown for production → retain** tables, models and
  all `ingest` compatibility handling. Locally: `eligible` on empty data, `ineligible`
  with every expected reason when seeded.
- Group K decision and evidence: **unknown for production → retain** the three columns
  and model fields; no credential deletion. Locally: as for G.
- Retained data and any separate disposition needed: retained regardless of the groups:
  `events` (including `origin='legacy'`) and all event identity/mention tables;
  answers, citations, sources and Suggestions; `provider_usage`, `usage_budgets` and
  historical labels/user scopes; old `operator_settings` rows (legacy Anthropic/OpenAI/
  Voyage keys and provider choice) and any encrypted personal-key rows, pending a
  separate credential-retention/revocation decision (already listed as a pending
  operator item in `docs/implementation-status.md`); `self_profiles`, its account
  creation path and `diary.migrate_profiles`.
- Phase B: **deferred** — the decision gate needs an aggregate inventory of the intended
  target. Neither G nor K is implemented; no migration, model or `ingest` change was made,
  and M6's schema rollout runbook was not written because there is no migration to roll out.
- New revision and fixed selected scope, if implemented: none.
- Migration/full-suite results: `bash scripts/test.sh` after all three plans: Python
  **295 passed, 0 failed, 0 skipped** (browser included; fresh `base → head`, migration
  0003/0006/0007 and backup/restore tests included); connector `node --test`
  **33 passed** (Node v24.19.0 fallback runtime, see plan 1); `compileall` OK.
- Runbook path and remaining release prerequisites: no schema runbook yet. To reach the
  gate, an authorized operator runs the read-only inventory on the VPS and records the
  JSON (it is aggregate-only) with its date:

  ```bash
  # After a release that contains app/legacy_inventory.py:
  sudo docker compose -p moresocial -f /srv/moresocial/src/deploy/compose.yaml \
    --env-file /srv/moresocial/release.env exec -T web python -m app.legacy_inventory
  # Or, before such a release, from a checkout that contains the file:
  sudo docker compose -p moresocial -f /srv/moresocial/src/deploy/compose.yaml \
    --env-file /srv/moresocial/release.env exec -T web python - < app/legacy_inventory.py
  ```

  Only a group reported `eligible` there may proceed to phase B (guarded migration,
  model cleanup, tests and the M6 runbook); the migration must re-check at upgrade time.
