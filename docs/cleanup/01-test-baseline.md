# Plan 1: restore the current test baseline

Status: completed locally on 2026-09-27 (uncommitted working tree on `bbb8379`). Depends on: no earlier cleanup plan.
Read [the handoff](README.md) first. Baseline: `bbb8379`, schema `0011`.

## Outcome and scope

Make the complete suite test the product that exists today and pass without
resurrecting retired features. Change tests and their supporting documentation only.
Production cleanup belongs to plan 2; schema changes belong to plan 3.

Deleting a test is appropriate when its entire contract is deliberately retired.
Preserve or relocate tests that also protect active behavior. Do not keep a broken
test under `skip`/`xfail`, install removed SDKs, or exclude entire files in CI.

## Read first

- `tests/test_ai_keys.py`, `tests/test_ai_budget.py`, `tests/test_openrouter.py`
- `tests/test_browser.py`, `tests/test_foundation.py`, `tests/test_memory.py`
- `tests/test_admin.py`, `tests/test_diagnostics.py`, `tests/conftest.py`
- `app/ai.py`: `generator_for`, OpenRouter adapter and usage accounting
- `app/config.py`, AI sections of `app/pages.py` and `app/templates/connections.html`
- `migrations/versions/0003_user_ai_keys.py` and `0006_remove_shared_allowance.py`
- `scripts/test.sh`, `.github/workflows/ci.yml`

## M1. Record the baseline

Run the full suite once and classify failures. The audited failures were:

| Location | Failing cases | Cause |
| --- | --- | --- |
| `tests/test_ai_keys.py` | 18 collected cases | Removed key-management routes, per-user generation/fallback, adapter caching; Anthropic tests import missing `httpx2` |
| `tests/test_ai_budget.py::test_adapters_log_rate_limits_without_content` | 1 | Imports retired direct-provider helpers from `test_ai_keys` |
| `tests/test_browser.py::test_ai_key_rejection_add_and_remove` | 2 (desktop/mobile) | Looks for the removed personal-key form |
| `tests/test_foundation.py::test_production_requires_secret_files_and_https` | 1 | Treats `AI_PROVIDER=anthropic` as valid |

The passing portion of `test_ai_keys.py` also includes obsolete tests. Do not remove
only the failing functions and leave the retired feature's passing adapter tests.
Capture new failures separately if the checkout has changed.

## M2. Replace the personal-key suite with current coverage

1. Move `test_migration_0003_round_trip_keeps_usage_history` into a new
   `tests/test_migrations.py`, with only its required imports/helpers. Preserve its
   actual downgrade/upgrade, usage-row count and `command.check` assertions. Its
   expectation that revision 0003 backfills `provider='anthropic'` describes historical
   migration behavior; do not rewrite that history as OpenRouter.
2. Move `test_generation_schemas_are_valid_openai_strict_schemas` into
   `tests/test_openrouter.py`, rename it to describe the active strict-schema contract,
   and remove gathering `DRAFT_SCHEMA`/`RSVP_SCHEMA` imports. Check active schemas:
   `memory.EXTRACT_SCHEMA`, `retrieval.ANSWER_SCHEMA`, `suggestions.SCHEMA`,
   `suggestions.DRAFT_SCHEMA`, `events.SCHEMA`, `event_resolver.SCHEMA`, and
   `observation_groups.SCHEMA`. Keep its recursive object/array checks. If an active
   schema violates the contract, report and fix that actual issue separately rather
   than weakening the assertion or omitting that schema.
3. Preserve the intent of `test_feature_off_hides_keys` as a current retirement
   regression in `tests/test_openrouter.py`: an authenticated Connections page has
   no personal-key input or action; valid-CSRF requests to former key save/remove/
   preference paths return 404 and create no personal-key records. Use the real
   browser helper and current authentication middleware. Test retirement with valid
   CSRF so a 403 from middleware is not mistaken for proof the route is gone.
4. Preserve useful workspace-isolation intent in a compact current-provider test:
   two synthetic workspaces use the shared provider, with separate workspace usage
   records and correct aggregate accounting. Reuse existing memory/usage coverage
   where it already asserts this; add only the missing behavior. Do not recreate
   per-user key factories or spy on `_user_adapter`.
5. First retarget the `test_ai_budget.py` helper import as described in M3 below;
   otherwise deleting this file breaks test collection. Delete the remaining
   personal-key tests and helpers, then remove
   `tests/test_ai_keys.py`. This includes its passing direct OpenAI adapter tests,
   key-format validation, fallback, quota and cache tests. Those are not current
   product contracts. Existing generic account-deletion tests remain.

There must be no imports from `test_ai_keys` after this milestone. Runtime
`app/ai_keys.py` stays in place until plan 2.

## M3. Repair the three other failure sources

### Provider logging/rate limits

Replace `test_adapters_log_rate_limits_without_content` in `test_ai_budget.py` with
OpenRouter coverage, or consolidate it into the existing OpenRouter tests if every
assertion below is covered. Use `httpx.MockTransport` and synthetic key/prompt canaries.

- An OpenRouter 429 preserves the `retry-after` delay and `not_billed=True`.
- The log uses the current `provider_call` event with provider/status/category/delay
  metadata. Assert the actual OpenRouter log schema, not removed adapter event names.
- Key, prompt and response-body canaries do not appear in logs.
- Cover generation as well as the existing embedding rejection case.
- Preserve `test_rate_limit_releases_reservation_pauses_provider_and_defers_jobs`,
  the unknown-outcome reservation test, no-embedder tests and migration 0006 test.

### Browser tests

Replace the retired key-entry scenario with a short Connections AI-status scenario
at both existing viewport sizes. Sign in through the existing synthetic flow, verify
the shared-AI explanation and absence of personal-key controls, then verify the
disabled-provider explanation using the existing test provider override mechanism.
Restore overrides/context resources in fixture teardown or `finally` blocks.
Do not add real keys, paid calls, or browser waits for deleted selectors.

Keep existing Admin OpenRouter, Suggestions draft/send, Events, identity grouping,
mobile layout and no-JavaScript scenarios. Do not remove the whole browser suite.

### Production config test

Change the successful case in `test_production_requires_secret_files_and_https`
to a supported mode (`AI_PROVIDER=none` is sufficient). Preserve HTTPS, secret-file
presence, callback and cookie checks. Add a small parameterized rejection assertion
for obsolete provider choices such as `anthropic`, `openai`, and `voyage`.
OpenRouter bootstrap coverage already lives in `test_openrouter.py`.

## M4. Verify and hand off

Focused command (new migration test file must exist):

```bash
uv run --frozen pytest -o addopts='' -q tests/test_openrouter.py tests/test_ai_budget.py tests/test_foundation.py tests/test_migrations.py tests/test_admin.py tests/test_memory.py
```

Run the updated browser scenarios, then `bash scripts/test.sh`. Check:

```bash
rg -n 'test_ai_keys|httpx2|set_user_adapter_factory' tests
git diff --check
git diff --stat
```

The first search should have no matches after the retired tests are removed.
Do not use that zero-match rule on historical documentation or migration provider names.

## Acceptance criteria

- [x] Full Python and connector suites pass; no new exclusions or expected failures.
- [x] Historical migration 0003 coverage and current schema checks remain executable.
- [x] Current strict schemas, shared-AI accounting, 429 handling and secret redaction remain covered.
- [x] Desktop/mobile tests exercise current Connections behavior and clean up overrides.
- [x] Runtime code, database schema, dependency locks and CI collection were not changed
      merely to accommodate obsolete tests.
- [x] Test removals are mapped to retired contracts in the result below.

Do not target a particular test count. Fewer tests are expected after removing the
retired subsystem, but active regression coverage must remain.

## Execution result

Executed 2026-09-27 on the workstation (macOS, Python 3.12 via `uv`/`.venv`, private
`pgserver` PostgreSQL + pgvector, Playwright Chromium).

- Starting/ending revision or local diff: start `bbb8379`, Alembic head `0011`. Ending
  state is an uncommitted local diff on top of `bbb8379` (tests only; plus the
  pre-existing README link to `docs/cleanup/`). Not deployed.
- Baseline reproduced (`uv run --frozen pytest -o addopts=''`, browser included):
  **289 passed, 22 failed**. The 22 failures were exactly the audited set: 18
  `test_ai_keys.py` cases, `test_ai_budget.py::test_adapters_log_rate_limits_without_content`,
  two `test_browser.py::test_ai_key_rejection_add_and_remove` viewports and
  `test_foundation.py::test_production_requires_secret_files_and_https`. No new failures.
- Removed tests and the retired contracts they asserted (`tests/test_ai_keys.py` deleted):
  - Key save/encryption/hint display, malformed/rejected key messages, replace/remove/
    preference, key-route CSRF/throttling/provider enablement, account deletion of key
    rows: the personal-key management UI and routes (retired; routes already removed).
  - Per-workspace key billing, operator fallback, user-key daily cap, run-time key
    refusal/fallback, key turning AI on, key resuming budget-paused jobs, second key
    taking over, adapter cache by fingerprint: per-user generation tiers (retired).
  - Anthropic/OpenAI key validation hosts and status mapping, OpenAI Responses adapter,
    Anthropic SDK adapter (needed the absent `httpx2`): direct-provider adapters (retired).
  - `test_ai_budget.py::test_adapters_log_rate_limits_without_content`: removed
    `ai_generation_failed` events of the direct adapters.
  - `test_browser.py::test_ai_key_rejection_add_and_remove`: removed key-entry form.
- Preserved/moved/replacement coverage:
  - `tests/test_migrations.py::test_migration_0003_round_trip_keeps_usage_history`
    (moved unchanged in substance; still expects the historical `provider='anthropic'`
    backfill, downgrade/upgrade, row count and `command.check`).
  - `tests/test_openrouter.py::test_active_generation_schemas_are_valid_strict_json_schemas`
    now checks the seven active schemas (extract, answer, suggestions, suggestion draft,
    events, event resolver, observation groups). All already satisfied the contract.
  - `tests/test_openrouter.py::test_personal_ai_keys_are_retired`: Connections has no
    key input/action; valid-CSRF posts to the four former key/remove/preference paths
    return 404, echo no canary and create no non-Google connection rows.
  - `tests/test_ai_budget.py::test_openrouter_rate_limits_are_not_billed_and_logged_without_content`:
    OpenRouter generation and embedding 429s keep `retry-after` (7 s) and `not_billed`;
    `provider_call` events carry provider/operation/model/status/category/delay; key,
    system prompt, prompt and response-body canaries are absent.
  - `tests/test_ai_budget.py::test_workspaces_share_the_provider_with_separate_usage_accounting`:
    two synthetic workspaces on the shared provider; per-workspace scopes equal each
    workspace's reconciled usage and `global` equals their sum.
  - `tests/test_browser.py::test_connections_ai_status[desktop|mobile]`: shared-AI
    explanation, no password input/key forms, no horizontal scroll, then the
    disabled-provider explanation via `ai.set_provider(OperatorTier(None, None))`;
    override and browser context restored in `finally`.
  - `tests/test_foundation.py`: production success case uses `AI_PROVIDER=none` (HTTPS,
    secret files, callback, cookie checks kept); new
    `test_retired_ai_providers_are_rejected[anthropic|openai|voyage]`.
- Commands and actual pass/fail/skip counts:
  - Focused: `uv run --frozen pytest -o addopts='' -q tests/test_openrouter.py tests/test_ai_budget.py tests/test_foundation.py tests/test_migrations.py tests/test_admin.py tests/test_memory.py`: **61 passed**.
  - Browser: `-k "connections_ai_status or admin_openrouter"`: **4 passed**.
  - `bash scripts/test.sh`: Python **289 passed, 0 failed, 0 skipped**; connector
    `node --test` **33 passed, 0 failed, 0 skipped**; `compileall` OK; "all checks passed".
  - `rg -n 'test_ai_keys|httpx2|set_user_adapter_factory' tests`: no matches.
    `git diff --check`: clean.
- Runtime/environment limitations: `node` is not on the workstation `PATH`; the connector
  suite ran with the documented fallback
  `/Users/enzolima/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node`
  (**v24.19.0**, not the documented Node 22), added to `PATH` for that command only.
- Unexpected failures or deviations and their resolution: none. Runtime code, schema,
  dependency locks and CI collection were not changed. `app/ai_keys.py` remains for plan 2.
- Ready for plan 2: yes.
