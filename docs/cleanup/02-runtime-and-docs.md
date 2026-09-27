# Plan 2: remove retired runtime code and refresh documentation

Status: completed locally on 2026-09-27 (uncommitted working tree on `bbb8379`, after plan 1). Depends on: plan 1 completed with a green full suite.
Read [the handoff](README.md) first. No schema migrations in this plan.

## Outcome

Remove the inactive personal AI key/direct-provider subsystem and manual gathering
helpers. Keep active behavior and historical-data handling intact. Align current
documentation with OpenRouter, automatic Events and explicit WhatsApp sending.

## Preservation map: read before deleting

| Keep | Why |
| --- | --- |
| `OpenRouterProvider`, `Provider`, `FakeProvider`, `OperatorTier` | Current runtime and synthetic test mechanisms; `OperatorTier` also models disabled/partial providers in tests |
| `KeyRejected`, `_json_object`, `_retry_after`, `InvalidOutput`, rate-limit cooldown | Used by OpenRouter and Admin, even though some names originated in the old provider work |
| `identity_accepted`, `embedding_identity_accepted`, `_EMBEDDING_RESPONSE_ALIASES` | The scoped Voyage native-name exception is active OpenRouter compatibility, not the retired direct Voyage adapter |
| Embedding targets, pending spaces, operator refresh/revision/tombstones | Prevent mixed embedding spaces and accidental reactivation of removed credentials |
| `ProviderUsage`, `UsageBudget`, reserve/reconcile, USD cost and unknown-cost semantics | Active accounting; unlimited spending locally does not mean untracked spending |
| `gatherings.next_steps`, `hide_step`, `person_identifiers`, required clock helper | Home still uses pinned-contact catch-up steps and dismissal/snoozing |
| `/gatherings` and `/gatherings/{eid}` redirects; creation rejection | Existing bookmarks and automatic-only Events behavior |
| `Event`, legacy event display, all event identity/provenance tables | Original event records remain meaningful and can participate in historical views |
| `EventCandidate`, `EventPerson`, `Draft`, `AnswerSource.draft_id`, key metadata columns | Historical rows may exist; schema and compatibility paths belong to plan 3 |
| `ingest.invalidate`, `prune_people`, `link_people` handling old drafts/invitees | Exclusion/deletion and identity changes must still handle stored legacy data |
| `SelfProfile`, `diary.migrate_profiles`, account creation of the profile row | Migration 0007 calls the helper; current accounts still create the row |
| Synthetic providers/data, demo scripts, connector patches and operator tools | Active testing/development or operations, not dead merely because absent from user pages |

Inspect `app/ai.py`, `config.py`, `gatherings.py`, `fake_generation.py`, `pages.py`,
`ingest.py`, `diary.py`, `accounts.py`, `worker/main.py` and their tests first.

## M1. Remove personal keys and direct-provider adapters

1. Confirm `app/ai_keys.py` has no remaining runtime/migration/script imports;
   plan 1 should have removed its test consumers. Delete the module.
2. Remove these definitions from `app/ai.py`:
   `_anthropic_rejection`, `AnthropicGenerator`, `_openai_rejection`,
   `OpenAIGenerator`, `VoyageEmbedder`, `SyntheticKeyProvider`,
   `set_user_adapter_factory`, `_build_user_adapter`, `_user_adapter`.
3. Remove `_adapters`, `_adapters_lock`, `_user_factory`, `ADAPTER_CACHE`, the
   `OrderedDict` import, and cache-reset code in `set_provider`. Remove imports
   made unused, including `Connection`, `Workspace`, `Scoped` and `select` if
   rechecking shows no remaining use.
4. Keep the small `generator_for`/`Resolved` interface for current callers in this
   pass. Remove unused `Resolved.connection_id`; its billing values are now only
   `operator` or `none`. Do not invent a replacement provider registry.
5. Rewrite the module/provider docstrings and comments to describe OpenRouter,
   shared accounting and synthetic providers. No direct-provider SDK is needed.

Do not delete all strings containing `voyage`, `openai`, or `anthropic`: selected
OpenRouter model IDs, verified response aliases, historical migrations and stored
usage provider labels can legitimately contain them.

## M2. Remove obsolete configuration and daily-cap branches

### Configuration

Remove these dataclass fields, construction arguments and now-unused constants/
parsers from `app/config.py`:

- `anthropic_api_key_file`, `anthropic_model`, `anthropic_fallbacks`,
  `voyage_api_key_file`, `openai_model`, `openai_reasoning`, `user_ai_keys`;
- `DEFAULT_ANTHROPIC_MODEL`, `DEFAULT_OPENAI_MODEL`, `USER_KEY_PROVIDERS`;
- `ai_daily_tokens_workspace`, `ai_daily_tokens_global`, `ai_daily_tokens_user_key`;
- `openrouter_generation_providers`, `openrouter_embedding_providers`, and the
  local `providers()` parser if it has no remaining use.

Leave OpenRouter key/model settings, fake embedding settings and `ai_concurrency`.
Unknown retired environment variables should simply remain ignored by `load`;
do not introduce a deployment-breaking error for an old unused environment value.
`AI_PROVIDER=anthropic` itself remains invalid, as established in plan 1.

### Usage accounting

Simplify the existing implementation rather than replacing its storage:

1. New reservations have no local token limit and use operator accounting. Make
   provider names explicit at reservation call sites (including the direct test call)
   so new usage cannot silently default to `anthropic`.
2. Remove the capped `TAKE_SQL` predicate, cap-exhaustion exception path,
   `BudgetExhausted`, and production `tomorrow_start` if no active caller remains.
   Keep atomic increments and reconciliation in database transactions.
3. Preserve historical reconciliation: an existing usage row with `billing='user'`
   must still adjust only its `userkey:<workspace>` scope, without any cap. Operator
   rows still adjust workspace and global scopes. This small historical mapping is
   intentional; removing it would misattribute an outstanding old reservation.
4. Preserve at-most-once reconciliation, unknown-outcome reservations,
   known-not-billed zero usage, day attribution, `cost=None` versus zero, and USD costs.
   Do not rewrite old usage rows or drop usage tables/columns.
5. Rename `budget_state` to `usage_state` and its page/template context from `budget`
   to `usage` where appropriate. Return today's `billing` and `used`; remove
   obsolete `limit`/`paused` presentation. The Connections page must still show
   tokens used and explain OpenRouter-managed spending limits.
6. Remove cap-related catches/messages from `pages.py`, `suggestion_pages.py`,
   `templates/home.html` and `worker/main.py`. Keep provider failures, missing-AI
   configuration deferral, 429 pacing, concurrency controls and retry behavior.
   Keep `jobs.defer`; it is still used for provider waits.

### Tests that must change with those interfaces

- `tests/test_memory.py`: remove `dataclasses.replace` of deleted cap fields.
  Retain the assertions that large usage does not stop processing and that
  reconciliation adjusts workspace/global totals. Assert `usage_state()['used']`.
- Add or adapt one accounting regression with a synthetic historical user-billed
  reserved row and scope; reconciling it must not touch operator/global totals.
- `tests/test_ai_budget.py`: calculate the migration 0006 test's future timestamp
  locally after removing `ai.tomorrow_start`; keep the historical reason strings
  and exact selective-resumption assertions. The filename can remain to limit churn.
- `tests/test_diagnostics.py::test_job_retry_terminal_and_deferral_events`: replace
  its injected `BudgetExhausted` branch with `ai.rate_limited(7)` (or missing-provider
  deferral). Assert the current event code/delay, queued state and unchanged attempt
  allowance. Keep its retry and terminal-failure checks.
- Search the entire suite for removed configuration fields and helper names.

Do not weaken provider accounting/rate-limit coverage merely because “budgets” was
an old label. No spending-limit system should be reintroduced.

## M3. Extract active catch-up steps; delete manual gathering helpers

Create `app/next_steps.py` containing the existing behavior of `next_steps`,
`hide_step`, `person_identifiers`, and their required imports/clock helper.
Update the two Home handlers in `pages.py` to use the new module. Update the
`test_unexpected_errors_show_the_request_reference` monkeypatch in
`tests/test_diagnostics.py` to patch the new module.

Remove `app/gatherings.py` after removing its remaining obsolete consumers:
`person_facts`, `candidate_text`, `create_draft`, `_store_draft`, `rewrite_draft`,
`reply_evidence`, `suggest_rsvp`, `set_status`, `calendar_conflicts`, `valid_timezone`,
and their prompts/schemas/status constants. Plan 1 removed the schema-test imports.

In `fake_generation.py`, remove only the retired `draft`, `rewrite`, and `rsvp`
responders and their dispatcher entries. Keep `suggestion_draft`, extraction,
answers, observation groups and event-related behavior.

Keep the current Event routes/templates and status labels used by automatic Events.
Do not remove the `gatherings` icon key just because it has an old name: the Events
navigation and templates still use it. Suggestion drafts do not use the old `Draft`
table and must continue to work. Do not remove clipboard JavaScript.

Run current Home step/dismissal, diagnostics, workflow, Suggestions, Events and
identity tests after this move. Preserve snooze/dismiss behavior and URLs.

## M4. Small verified cleanup

- Remove `jobs.still_leased` if a fresh repository-wide search still finds no caller.
  Keep `jobs.renew` and the worker heartbeat implementation.
- Remove unused imports from affected files and confirmed leftovers in
  `google_sync.py`, `ingest.py`, `web.py`, `suggestions.py`, `whatsapp.py`,
  `retrieval.py`, `observation_groups.py`, `dev_routes.py`, and `test_workflow.py`.
  An import with intentional side effects or test re-export is not automatically dead.
- Remove the obsolete `.suggest` CSS rule only if no template/JS use remains.
  Keep dynamically generated `rsvp-*`, `status-*`, `av-*`, `layout-*`, and `main-*`
  classes; a literal-string scan alone does not establish they are unused.
- Update misleading test/module descriptions such as the old manual “lasergame
  gathering workflow” description after the relevant code has been removed.

No new lint framework, dependency refresh or broad style conversion is required.

## M5. Make current docs reliable

Update `README.md`, `docs/architecture.md`, `docs/operator-setup.md`,
`docs/product-definition.md`, `docs/openrouter-rollout.md`, and the current summary
in `docs/implementation-status.md`:

- Describe automatic Events and current Suggestions drafts; remove manual planning,
  invitation creation, direct-provider and personal-key activation instructions.
- Replace retired provider-secret entries in the operator setup table with the
  actual OpenRouter bootstrap/Admin options. Check `compose.yaml` and `.env.example`;
  do not claim a secret is automatically mounted if the configuration does not do so.
- Describe WhatsApp's explicit sending; Gmail/Calendar remain read-only.
- Describe token usage reporting without local caps and preserve embedding identity/
  rebuild, rate-limit, backup, restore and rollback instructions.
- Remove stale test counts from current architectural claims or replace them with
  dated actual results. Preserve historical release evidence as historical evidence.
- The status file currently has several “active revision” statements from different
  releases. Add a clear current pointer and label older entries by date. Use the last
  recorded release as recorded evidence, not as a new live deployment verification.

Archive completed development plans/prompts under `docs/archive/development/`.
Use these exact candidate paths, preserving each basename at its destination:

- Root: `EXECUTION-PLAN.md`, `IMPLEMENTATION-PROMPT.md`, `EVENTS-IMPLEMENTATION-PROMPT.md`.
- Under `docs/`: `api-keys-execution-plan.md`, `openrouter-execution-plan.md`,
  `diary-observations.md`, `diagnostics-plan.md`, `whatsapp-sidebar-execution-plan.md`,
  `events-execution-plan.md`, `event-identity-execution-plan.md`.

Inspect status first: if a document still contains
an unresolved operational item, move that item into the current status/runbook before
archiving. Keep live feature docs, runbooks and `docs/cleanup/` outside the archive.

Create an archive index explaining that the old files are historical, then update
all repository Markdown links, including links inside moved files. Preserve dated
release evidence and useful implementation rationale. Do not execute instructions
inside old prompts. If exact archive paths already exist, integrate without overwriting.

`docs/review-probes.py` is an old compatibility entry point to permanent tests. It
can be removed after replacing its documentation references with commands naming
the actual regression tests; do not remove those regressions.

## M6. Verification

Run focused checks for changed behavior:

```bash
uv run --frozen pytest -o addopts='' -q tests/test_openrouter.py tests/test_admin.py tests/test_memory.py tests/test_ai_budget.py tests/test_diagnostics.py tests/test_workflow.py tests/test_suggestions.py tests/test_events.py tests/test_event_identity.py tests/test_diary_groups.py tests/test_migrations.py
```

Then run `bash scripts/test.sh`. This includes fresh migration execution and browser
coverage; the profile migration and backup/restore tests must remain green. Verify:

```bash
rg -n 'ai_keys|AnthropicGenerator|OpenAIGenerator|VoyageEmbedder|SyntheticKeyProvider|_user_adapter|BudgetExhausted|budget_state|from app.gatherings|from \.gatherings' app worker tests scripts
git diff --check
```

Review matches rather than applying blind search-and-replace. There should be no
executable references to deleted definitions. Audit and archived docs can retain
historical names. Validate local Markdown file links after the moves.

## Acceptance criteria

- [x] No personal-key runtime module, direct-provider adapters/cache or manual gathering helpers.
- [x] Active OpenRouter identity handling, Admin tombstones, indexing, rate limits,
      accounting and historical user-scope reconciliation still work.
- [x] Home catch-up steps, Suggestions drafts/sends and automatic Events are unchanged.
- [x] No schema change; old-record deletion/invalidation/merge handling is preserved.
- [x] All tests pass, including migration 0007 with nonempty profiles, browser and connector tests.
- [x] Current docs describe current behavior; archived plans have working links and clear status.
- [x] No production data, credentials or deployments were changed.

## Execution result

Executed 2026-09-27 on the workstation after plan 1 was green. Code milestones M1–M4 were
done directly; M5 (documentation) was done in parallel by a delegated agent restricted to
Markdown/`.env.example` and then reviewed. Nothing was deployed; no production data,
credentials or services were touched.

- Starting/ending revision or local diff: start `bbb8379` plus the plan 1 diff (head
  `0011`); ending state is an uncommitted local diff. No migration, no dependency change.
- Removed modules/definitions:
  - `app/ai_keys.py` (no importers left).
  - `app/ai.py`: `_anthropic_rejection`, `AnthropicGenerator`, `_openai_rejection`,
    `OpenAIGenerator`, `VoyageEmbedder`, `SyntheticKeyProvider`,
    `set_user_adapter_factory`, `_build_user_adapter`, `_user_adapter`, `_adapters`,
    `_adapters_lock`, `_user_factory`, `ADAPTER_CACHE`, the cache reset in `set_provider`,
    `Resolved.connection_id`, `BudgetExhausted`, `tomorrow_start`, the capped `TAKE_SQL`,
    `budget_state`; unused `OrderedDict`, `timedelta`, `select`, `Connection`, `Workspace`,
    `Scoped` imports. Also the never-sent `generation_providers`/`embedding_providers`
    constructor parameters of `OpenRouterProvider` (no caller passed them; they belonged to
    the removed allowlist configuration). Module docstring now describes OpenRouter,
    shared accounting and synthetic providers.
  - `app/config.py`: the ten retired fields listed in M2 plus `USER_KEY_PROVIDERS`,
    `DEFAULT_ANTHROPIC_MODEL`, `DEFAULT_OPENAI_MODEL` and the local `providers()` parser.
    Old environment values are ignored; `AI_PROVIDER=anthropic` stays a startup error.
  - Cap handling in `pages.py` (`ai_error`, `ai_failure`, Ask), `suggestion_pages.py`,
    `templates/home.html` (paused banner) and `worker/main.py` (the `BudgetExhausted`
    deferral). Dead personal-key notices `ai-key-saved`/`ai-key-removed` in `web.py`.
  - `app/gatherings.py`; `app/next_steps.py` holds the unchanged `next_steps`,
    `hide_step`, `person_identifiers` and clock helper used by Home. Retired `draft`,
    `rewrite`, `rsvp` fake responders.
  - `jobs.still_leased` (no caller); the obsolete `.suggest` CSS rule (no template/JS use,
    no dynamic class producing it); confirmed-unused imports in `google_sync.py`,
    `ingest.py`, `web.py`, `suggestions.py`, `whatsapp.py`, `retrieval.py`,
    `observation_groups.py`, `dev_routes.py`, `jobs.py`, `pages.py` and the touched tests.
- Retained compatibility code: `OpenRouterProvider`, `Provider`, `FakeProvider`,
  `OperatorTier`, `KeyRejected`, `_json_object`, `_retry_after`, `InvalidOutput`, cooldowns,
  identity/alias acceptance, embedding targets and tombstones; `generator_for`/`Resolved`
  (billing `operator` | `none`). Accounting keeps `RESERVE_SQL` plus an uncapped atomic
  increment, at-most-once reconciliation, unknown-outcome reservations, known-not-billed
  zero usage, day attribution, `cost=None` vs zero and USD. `reserve()` now takes a required
  keyword `provider_name` and always records `billing='operator'`; a historical
  `billing='user'` row still reconciles only its `userkey:<workspace>` scope, uncapped.
  `usage_state()` returns `{'billing', 'used'}`; Connections still shows today's tokens and
  the OpenRouter spending-limit sentence. `jobs.defer`, `jobs.renew`, the `/gatherings`
  redirects and creation rejection, legacy models, `ingest` draft/invitee handling,
  `SelfProfile`/`diary.migrate_profiles` and the `gatherings` icon key are unchanged.
- Tests changed with the interfaces: `test_memory.py` (no cap overrides; a 3,000,000-token
  reservation before processing does not pause work; reconciliation adjusts workspace/
  global once; `usage_state()['used']`), new
  `test_memory.py::test_historical_user_key_reservation_reconciles_only_its_own_scope`,
  `test_ai_budget.py` (migration 0006 test computes its own next-day timestamp; historical
  reason strings kept), `test_diagnostics.py` (Home monkeypatch targets `next_steps`;
  the deferral branch now injects `ai.rate_limited(7)` and asserts `job_deferred`
  `rate_limited`/7000 ms, queued, `attempts == 0`, `max_attempts == 2`, provider-wait
  reason), `test_workflow.py` description and imports.
- Current behavior verified: Home steps/dismiss/snooze, Suggestions drafts and sends,
  automatic Events and identity grouping, Admin/OpenRouter identity and tombstones, 429
  pacing, accounting, migrations 0003/0006/0007 (nonempty profiles) and backup/restore —
  all through the existing and updated tests below.
- Commands and actual test results:
  - Focused (M6 command): **154 passed**.
  - `bash scripts/test.sh` after M1–M4: Python **290 passed, 0 failed, 0 skipped**;
    connector **33 passed**; `compileall` OK.
  - M6 search (`rg ... app worker tests scripts`): only
    `tests/test_openrouter.py::test_personal_ai_keys_are_retired` matches (`ai_keys` in the
    regression's name). `git diff --check`: clean.
- Documentation moves and link validation: ten completed plans/prompts moved to
  `docs/archive/development/` (basenames kept, each with an "Archived 2026-09-27" note)
  with a new index; `README.md`, `docs/architecture.md`, `docs/operator-setup.md`,
  `docs/product-definition.md`, `docs/openrouter-rollout.md`,
  `docs/implementation-status.md` (current-state pointer, consolidated pending table,
  dated history), `docs/connections.md`, `docs/suggestions.md`, `docs/events.md`,
  `docs/handover-review.md`, `docs/review-findings.md` and a comment in `.env.example`
  updated. The operator docs state that Compose does not mount an OpenRouter key file.
  Unresolved items from archived plans were moved into the status pending table.
  `docs/review-probes.py` removed; references now name `tests/test_review_fixes.py`
  tests. Local Markdown link check (files and anchors, all `*.md` outside `.venv`/
  `node_modules`): **120 links, 0 problems**.
- Deferred cleanup and why: legacy models (`EventCandidate`, `EventPerson`, `Draft`,
  `AnswerSource.draft_id`, key metadata columns), their comments and `ingest`
  compatibility, and the ORM default `provider='anthropic'` on `ProviderUsage` belong to
  plan 3's data-gated schema work (all new rows now pass the provider explicitly). The
  Event detail handler keeps its historical Python name `gathering` (renaming is churn).
  Pre-existing unused imports in untouched tests (`test_agent_session`,
  `test_backup_restore`, `test_diary_groups`, `test_events`, `test_google_sync`) were
  left alone as outside the listed scope.
- Ready for plan 3 inventory: yes.
