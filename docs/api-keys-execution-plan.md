# User-supplied AI API keys — execution plan

Status: implemented 2026-09-25 (K0–K4, K5 docs); rollout and live checks pending. See
`docs/implementation-status.md` for verification and deviations. Prepared against the
deployed first release (migration head `0002`).

## 1. Outcome

On the Connections page, a user can add their own **Anthropic** and/or **OpenAI** API
key. The assistant's generation calls for that workspace (claim extraction, cited
answers, invitation drafts, rewrites and RSVP suggestions) then run on the user's key,
billed to them by the provider. Users without a working key fall back to the
operator's key under the existing daily budgets, or to no AI if the operator has none.

Why API keys and not subscription login: Anthropic's Claude Code terms forbid third
parties from offering Claude.ai login or routing requests through Free/Pro/Max
credentials on behalf of users. OpenAI's Codex docs direct programmatic use to API
keys. No Claude or Codex CLI is installed on the server.

## 2. Decisions fixed by this plan

These are defaults. Change them here before implementation starts, not during it.

| Topic | Decision |
| --- | --- |
| Providers | Anthropic (Messages API, existing adapter) and OpenAI (Responses API, new adapter). |
| What the user key pays for | Generation only. Embeddings stay on the operator's Voyage key for everyone, so all vectors share one model and retrieval never mixes vector spaces. Embedding cost is small. |
| Resolution order | Workspace's active key for its preferred provider → other active user key → operator key (`AI_PROVIDER`) under existing budgets → AI unavailable. |
| Models | Operator-configured: `ANTHROPIC_MODEL` (existing) and new `OPENAI_MODEL`. No per-user model picker in this iteration. |
| One key per provider | Enforced by the existing `UNIQUE(workspace_id, provider)` on `connections`. |
| Budgets on user keys | Not counted toward the operator's global budget. A separate per-workspace daily cap, `AI_DAILY_TOKENS_USER_KEY` (default 2,000,000), protects the user from runaway background jobs; it is shown on the page. |
| Rejected key (401/403, no credit, no model access) | Connection moves to `reconnect_required` with a reason, and the workspace falls back to the operator tier. The page says so plainly. |
| Rate limited (429 without quota exhaustion) | Existing `AIUnavailable('rate_limited')` retry behavior; no state change. |
| Provider base URLs | Fixed in code. Users can never supply a URL (prevents SSRF and key exfiltration). |
| Removal | Hard-deletes the encrypted key. There is no provider revocation API for keys; the page tells users to revoke the key in the provider console if they want it disabled. |

## 3. Data model — migration `0003_user_ai_keys`

Reuse `connections` for keys; it already has encryption-ready columns, state and
workspace scoping.

- `connections` rows with `provider` in (`anthropic`, `openai`):
  - `provider_account` = `fp:` + first 16 hex chars of `security.digest(key)`, a
    non-reversible fingerprint used to detect "same key re-entered".
  - `access_token_enc` = `security.encrypt(key)`. `refresh_token_enc`, `scopes`,
    `access_expires_at`, `email` are unused (NULL/empty).
  - `state`: `active` | `reconnect_required`; `detail`: `invalid_key` | `no_credit` |
    `model_unavailable` | NULL.
  - New nullable column `key_hint VARCHAR(8)`: last 4 characters for display
    (`sk-ant-…a1b2`). Never store more of the key in plaintext.
  - New nullable column `validated_at TIMESTAMPTZ`.
- `workspaces`: new nullable `ai_preference VARCHAR(20)` (`anthropic` | `openai`),
  used when both keys are active.
- `provider_usage`: new `provider VARCHAR(20) NOT NULL DEFAULT 'anthropic'` and
  `billing VARCHAR(10) NOT NULL DEFAULT 'operator'` (`operator` | `user`).
  Existing rows backfill to the defaults.
- `usage_budgets` scopes: user-key usage reserves under `userkey:<workspace_id>` only;
  operator usage keeps `workspace:<id>` + `global`.

Update the `Connection.provider` comment in `app/models.py`. Existing Google queries
already filter `provider == 'google'` (`app/ingest.py:77`, `app/accounts.py:72,149`,
`app/pages.py:66,449`); re-check each after the change, and confirm the
`worker/main.py` sync scheduler cannot select key rows (it joins through
`sync_streams`, so it should not).

## 4. Code changes

### 4.1 `app/ai.py` — split provider resolution

Today a single process-wide `provider()` ([ai.py:275](../app/ai.py)) serves both
generation and embeddings. Replace it with:

- `embedder() -> Provider`: process-wide, operator-configured (current Voyage/fake
  behavior). Only `embed`, `embedding_model` and `embedding_dimension` are used.
- `generator_for(workspace_id) -> Resolved`: `Resolved(provider, billing, connection_id)`.
  Reads the workspace's key connections (one short DB query) and applies the §2
  resolution order. Adapter instances are cached in a small LRU keyed by
  `(provider_name, key fingerprint)` so a changed key never reuses an old client, and
  plaintext keys are held only inside the adapter.
- `generate(workspace_id, ...)` uses `generator_for`, then reserves against the scopes
  that match `billing` and records `provider`/`billing` on `ProviderUsage`.
- New exception `KeyRejected(provider, reason, connection_id)` raised by adapters on
  401/403, credit exhaustion or model-access errors. `generate()` reconciles the
  reservation, calls `ai_keys.mark_rejected(...)`, and re-raises.
- `set_provider()` for tests keeps working: an override applies to both embedder and
  operator generator. Add `set_user_adapter_factory()` so tests can inject fakes for
  user-key adapters.

Update the callers:

| File | Change |
| --- | --- |
| `app/retrieval.py:51` | `ai.provider()` → `ai.embedder()` for query embedding. |
| `app/memory.py:137,176` | `ai.provider()` → `ai.embedder()`. |
| `worker/main.py:125` | `reconcile_ai` uses `ai.embedder()`. |
| `app/pages.py:464` | `ai_configured` comes from `generator_for(ws.wid)`, not `provider().name`. |
| `app/pages.py:35` `ai_error` | Add messages for `KeyRejected` ("Your Anthropic key was rejected. Moresocial switched to the shared allowance; update your key under Connections.") and for a user-key cap. |

### 4.2 Adapters

- `AnthropicProvider` (rename of `AnthropicVoyageProvider`'s generation half):
  constructed with an explicit key instead of reading the operator secret file. Map
  `AuthenticationError`/`PermissionDeniedError` → `KeyRejected('invalid_key')`,
  the "credit balance too low" 400 → `KeyRejected('no_credit')`, `NotFoundError` on
  the model → `KeyRejected('model_unavailable')`. Keep the refusal-fallback option.
  Match on error type and status first; use message text only where the API gives no
  distinct type, and cover each mapping with a test.
- `VoyageEmbedder`: the embedding half, unchanged behavior.
- `OpenAIProvider` (new): Responses API with `text.format = {type: 'json_schema',
  strict: true, schema}`; map `effort` to `reasoning.effort` where the configured model
  supports it. Map 401 → `invalid_key`, 429 with `insufficient_quota` →
  `no_credit`, 404 model → `model_unavailable`, other 429 → `AIUnavailable('rate_limited')`.
  Refusal or incomplete output → `InvalidOutput`, same as Anthropic. Token usage comes
  from `usage.input_tokens`/`output_tokens`.
- Strict-mode check: OpenAI strict schemas need every property listed in `required`
  and `additionalProperties: false` at every object level. `ANSWER_SCHEMA`,
  `DRAFT_SCHEMA`, `RSVP_SCHEMA` (`app/retrieval.py`, `app/gatherings.py`) and
  `EXTRACT_SCHEMA` (`app/memory.py`) appear compliant; add a unit test that walks
  every schema and asserts it.
- Dependency: add `openai` to `pyproject.toml` and regenerate `deploy/requirements.lock`
  with hashes. Both SDKs accept an injected `httpx.Client` for tests.
- Neither adapter offers tools. Keep the "imported content is untrusted data" framing
  unchanged for both providers.

### 4.3 New module `app/ai_keys.py`

- `save(ws, provider, raw_key) -> SaveResult`: strip whitespace; check the prefix
  (`sk-ant-` for Anthropic, `sk-` for OpenAI) and a length bound; call `validate`;
  on success upsert the connection (`state='active'`, fingerprint, hint, `validated_at`);
  on failure return a reason without storing anything. After saving, re-queue this
  workspace's jobs deferred with `AI paused%` so they run now on the new key.
- `validate(provider, key) -> None | reason`: one free request with a 10s timeout.
  Anthropic `GET /v1/models/{ANTHROPIC_MODEL}`; OpenAI `GET /v1/models/{OPENAI_MODEL}`.
  401/403 → `invalid_key`, 404 → `model_unavailable`, network error → `unreachable`
  (not stored; ask the user to retry). Credit exhaustion only shows up on a real
  call; it is handled at runtime.
- `remove(ws, provider)`: delete the row; clear `ai_preference` if it pointed there.
- `set_preference(ws, provider)`.
- `mark_rejected(workspace_id, connection_id, reason)`: set `reconnect_required` and
  `detail` in its own short transaction, and emit `ai_key_rejected` with only
  provider and reason.
- `status(ws)`: view data for the page (provider, state, detail, hint, validated_at,
  which tier is in use, today's usage vs cap).
- In synthetic mode, `validate` uses an injectable transport; the dev fake accepts keys
  containing `valid` and rejects everything else, so the UI is exercisable locally.

### 4.4 Worker (`worker/main.py`)

- `except ai.KeyRejected`: the connection is already marked; fail the job with
  `retry=True, delay=0` so it re-resolves to the fallback tier on the next claim.
- `BudgetExhausted` with scope `userkey`: defer to tomorrow, message
  `AI paused: daily limit for your API key reached`.
- Nothing else changes. Jobs already carry `workspace_id`, and resolution happens per
  call from that id; that per-call resolution is what keeps user A's key off user B's
  jobs.

### 4.5 Routes (`app/pages.py`) and UI (`app/templates/connections.html`)

All mutations use `require_mutation` (session + CSRF). Provider is a path parameter
validated against the enabled set.

- `POST /connections/ai/{provider}/key`, form field `api_key`.
- `POST /connections/ai/{provider}/remove`.
- `POST /connections/ai/preference`, form field `provider`.
- Validation attempts limited to 10 per workspace per hour (count via a small
  in-DB counter or the existing job/usage tables; do not add Redis).

Replace the "Memory processing" card with an **AI assistant** card:

- One row per provider: not connected / connected `sk-ant-…a1b2` (validated time) /
  needs attention with the reason. Actions: Add key, Replace key, Remove.
- Key input: `type="password"`, `autocomplete="off"`, `spellcheck="false"`. The key is
  never rendered back, not even on a failed save; errors refer to it only by provider.
- A "Use for the assistant" choice when both keys are active.
- A line stating who pays and who processes the data: "Using your Anthropic key:
  your data is sent to Anthropic under your API account", or "Using Moresocial's
  shared allowance (N of M tokens today)", or "AI is off".
- Keep the existing processing/paused status below it.
- A short note: removing the key here does not revoke it; revoke it in the
  Anthropic Console or OpenAI dashboard. Server backups keep an encrypted copy for up
  to `backup_retention_days`.
- Links to where users create keys: console.anthropic.com and
  platform.openai.com/api-keys.

### 4.6 Configuration (`app/config.py`, `.env.example`, `deploy/compose.yaml`)

- `USER_AI_KEYS` (comma list, default empty = feature off; production target
  `anthropic,openai`). The routes 404 and the card hides key entry for disabled providers.
- `OPENAI_MODEL` (required when `openai` is enabled; `load()` raises `ConfigError`
  otherwise).
- `AI_DAILY_TOKENS_USER_KEY` (default 2,000,000).
- `AI_PROVIDER` keeps its meaning: the operator tier. `none` means "no fallback";
  user keys still work.
- `AI_CONCURRENCY` still caps simultaneous calls across all tiers; raise it
  deliberately if many users bring keys (web and worker have 768 MB each).

## 5. Security requirements (all tested)

1. The plaintext key appears only in the POST body, in memory inside the adapter and
   validation call, and encrypted in `connections.access_token_enc`.
2. Never in HTML, JSON responses, redirects/query strings, logs, `security.emit`
   fields, exception messages persisted to `jobs.last_error`, or `provider_usage`.
   Confirm `security.emit` never serializes `str(exc)` from SDK errors that could echo
   request headers.
3. Keys are workspace-scoped like every other row; `generator_for` must query through
   the workspace-scoped helper, not a raw global select.
4. Only fixed provider hosts are contacted.
5. Account deletion removes key rows through the existing cascade; add a test.
6. The encryption key and backups already protect `access_token_enc` at rest; no new
   secret material is introduced.

## 6. Milestones and acceptance checks

Work in order. Update `docs/implementation-status.md` after each milestone. Synthetic
tests do not prove a live provider works.

### K0 — split embedder from generator (no behavior change)
- `embedder()`, `generator_for()` returning the operator tier only; callers updated.
- All 81 Python tests pass unchanged, including
  `test_reindex_on_embedding_model_change_refuses_mixed_vectors`.

### K1 — storage and key management
- Migration `0003` (upgrade and downgrade); model updates; `app/ai_keys.py`.
- Tests: save valid key → active row with fingerprint/hint and encrypted value;
  invalid key → nothing stored; same key re-entered updates `validated_at` only;
  remove deletes; plaintext never in the DB outside `access_token_enc` (scan all text
  columns); two workspaces can hold keys independently.

### K2 — Anthropic user keys end to end
- Resolution, billing scopes, `KeyRejected` mapping, worker handling, re-queue on save.
- Tests with injected transports / fake adapters:
  - two users, only A has a key: A's jobs bill `user`, B's bill `operator`, and B's
    calls never construct an adapter with A's key;
  - user-key usage does not touch the `global` budget; the user cap defers with the
    right message;
  - a 401 mid-extraction marks `reconnect_required`, and the retried job completes on
    the operator tier;
  - operator `AI_PROVIDER=none` and no key → AI unavailable, manual flows work;
  - adding a key resumes jobs paused by the operator budget.

### K3 — OpenAI adapter
- Adapter, error mapping, strict-schema test over all four schemas, preference choice.
- Tests: structured output parsed; refusal/incomplete → `InvalidOutput`;
  `insufficient_quota` → `no_credit`; preference picks OpenAI when both are active and
  falls back to Anthropic if OpenAI is rejected.

### K4 — Connections UI and messaging
- Card, routes, rate limit, `ai_error` messages; browser test (`tests/test_browser.py`
  style) for add → connected → replace → remove, plus the error state.
- Tests: rendered page and every redirect never contain the key; CSRF enforced;
  disabled providers return 404; attempt limit returns a clear message.

### K5 — docs, packaging, deployment
- Update `docs/architecture.md` (AI section and data flow), `docs/connections.md`
  (new "AI provider keys" section, including why subscription login is not offered),
  `docs/operator-setup.md` (new env vars), `README.md` stack line, and
  `docs/product-definition.md` if it describes who processes data.
- Regenerate the lock file; build images; run `bash scripts/test.sh`.
- Deploy with the documented additive rollout (migration `0003` first; it is additive
  and backward compatible). Set `USER_AI_KEYS`, `OPENAI_MODEL`.
- Live checks by the operator, recorded in `implementation-status.md`: add a real
  Anthropic key, ask a question and see a cited answer billed to that key in the
  Anthropic Console; the same for OpenAI; paste a revoked key and see the rejection;
  remove the key and confirm fallback.

## 7. Out of scope

- Claude/ChatGPT subscription login or any CLI-based provider.
- User-selected models, custom base URLs, Bedrock/Vertex/Azure credentials.
- User-paid embeddings or per-workspace embedding models.
- Moving AI requests out of web request threads (tracked separately in the
  architecture notes).

## 8. References

- https://code.claude.com/docs/en/legal-and-compliance — authentication and credential use
- https://learn.chatgpt.com/docs/auth — Codex authentication; API keys for programmatic use
- https://docs.anthropic.com/en/api/errors
- https://platform.openai.com/docs/guides/structured-outputs
- https://platform.openai.com/docs/guides/error-codes
