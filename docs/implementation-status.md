# Implementation status

> Supersession note (2026-09-25): the legacy Anthropic/Voyage/OpenAI and personal-key
> sections below describe a previous implementation. The active implementation is
> OpenRouter-only; Admin owns one encrypted key and pinned reasoning/embedding models.
> Local migration and synthetic checks are not evidence of live API validation or deployment.

Last updated 2026-09-25. This is the resume point for any agent or operator.

Synthetic-provider tests do **not** prove live Google, WhatsApp or AI integration. The
release is **deployed** at https://1f517.com/moresocial/. Interactive provider checks remain pending.

## Production deployment — 2026-09-25

- Active application revision: `c921458b7ece3329e41ef7390dd4bf171ef062f5`.
  Source was deployed from a local Git bundle. The application and documentation are
  maintained in `https://github.com/errslima/moresocial`.
- Server: `ubuntu@54.37.204.161`, using `~/.ssh/qoc_vps_ed25519`.
  Isolated release/state under `/srv/moresocial`; migration `0002` applied.
- Both Docker images built successfully. Database and web healthy; worker running;
  connector provisioner and daily backup timer enabled and active.
- Public home, static CSS, health and readiness return 200; bare `/moresocial` redirects
  with 308. Internal management path returns 404 publicly. Existing root (200),
  EnzoSocial (303) and Quorum (200) preserved their responses.
- OAuth initiation verified: Google authorization endpoint, exact production callback,
  Gmail/Calendar read-only scopes and secure HttpOnly callback cookie. No user signed in
  during deployment; actual consent, grants and provider sync remain unverified.
- `AI_PROVIDER=none`; Anthropic and Voyage secret files are empty. AI features are disabled.
  Beta allowlist contains only the configured first tester. WhatsApp cap is two.
- First encrypted backup `moresocial-20260925T133655Z.tar.enc` restored successfully into
  an isolated container, migration head `0002`, zero workspaces/sources (fresh installation).
  Disposable restore container, volume and decrypted files removed afterward.
- Live Caddy configuration backed up at `/srv/moresocial/Caddyfile.before-20260925T133638Z`.
  Shared `../deploy/Caddyfile` includes the new route in the separate
  `https://github.com/errslima/1f517` repository for future shared deployments.
- Secrets stay outside Git. Recovery secrets and backups currently remain on this host;
  an independent private recovery-vault copy has not been configured.

## Review fixes completed

All six confirmed findings in `docs/review-findings.md` are fixed and deployed:

- Public synchronous DB/provider work runs in FastAPI's thread pool; the internal
  async handlers offload DB work too. Slow AI no longer blocks the public/internal
  event loop. These calls still occupy a request worker; this is not a job-based UI.
- Account deletion writes an atomic cleanup outbox record that survives the
  workspace cascade, including final-user deletion. A lost host acknowledgment
  leaves cleanup pending for an idempotent retry. Suspicious empty-DB protection stays.
- Gmail incremental history is processed in 100-message batches with durable
  per-page progress. The final history cursor advances only after all covered work.
- Chunk membership stores the actual attributed source segment. Retrieval and
  extraction consume these segments, including later passages of long emails.
  Pipeline `v2-segments` triggers automatic legacy chunk rebuilds.
- Restore uses a shared `deploy/restore-init.sql` file with independent SQL
  statements and asserts the restored migration revision against Alembic's real head.
- The provisioner reconciles connector image IDs on upgrades and rollbacks, checks
  replacement availability before stopping, and preserves each session mount.

Migration `0002` adds `connector_cleanups` and `chunk_sources.segment_text`.
The production release applied this migration successfully.
Old chunks are temporarily unavailable to retrieval until the worker rebuilds them.

Verification after fixes (2026-09-25):

- `.venv/bin/python -m pytest -o addopts='' -q`: **81 passed**, including browser,
  Postgres, schema comparison and a downgrade/upgrade round trip on disposable data.
- Connector Node 22 suite: **19 passed**.
- Nine permanent regressions in `tests/test_review_fixes.py`: concurrent public/internal
  responsiveness, final-account cleanup with lost acknowledgment, a 702-message Gmail
  burst with interruption/replay, long-message retrieval/extraction, old-pipeline rebuild,
  actual restore bootstrap SQL, migration compatibility, connector upgrade/rollback,
  and unavailable-image preservation.
- Python compilation and shell syntax checks passed. Docker is unavailable locally:
  connector image reconciliation is tested with FakeDocker, and restore bootstrap SQL
  with real temporary Postgres. Full container restore/rollout subsequently passed on the VPS (above).

`docs/review-probes.py` now forwards to the permanent regressions rather than retaining
obsolete failing probes. The fix verification itself used no live credentials; the subsequent deployment is recorded above.

## Events plan E0 — budget leak, failure logging, pacing (2026-09-25)

From [the events plan](events-execution-plan.md) §3, found in the production review:

- No embed jobs are queued, and `ai.embed` reserves nothing, while no embedder is
  configured; an embed job queued earlier completes as a no-op. Before this, every such
  call kept its reservation and the operator budget was exhausted (400,000/400,000) on
  2026-09-25.
- `AIUnavailable.not_billed`: failures where the provider certainly charged nothing (not
  configured, rate limited, key refused) release their reservation; unknown outcomes
  (connection errors) still keep it.
- Rate limits and provider errors are logged as `ai_generation_failed` with provider,
  status and code only. After a 429 the adapter is not called again for `retry-after`
  (default 30 s, max 300 s), and the worker defers the job ("AI waiting: provider rate
  limit") without using one of its attempts, so backfills no longer fail jobs.
- `tests/test_ai_budget.py` (5 tests); full suite **126 passed**.

## Production deployment — AI keys and admin page, 2026-09-25

- Active revision `a66135449b826d2a43a0f07721c974a6cebae07a` (previous `c921458`), deployed
  from a local Git bundle with `release.sh`; migration head `0003`. Web healthy, worker
  running, no errors in logs after start.
- `runtime.env` additions (backup at `/srv/moresocial/runtime.env.before-a661354`):
  `USER_AI_KEYS=anthropic,openai`, `ANTHROPIC_MODEL=claude-sonnet-5`,
  `OPENAI_MODEL=gpt-5.6-terra`, `ADMIN_EMAILS=errslima@gmail.com`. `AI_PROVIDER=none` is
  unchanged: no global provider or key is set yet, so `/ready` reports
  `ai_generation: false, ai_embeddings: false` until the admin page is used.
- Public checks: `/` 200, `/enzosocial/` 303, `/quorum-of-clones/` 200 (unchanged),
  `/moresocial/` 200, `/moresocial/health` 200, `/moresocial/admin` 303 when signed out.
- Pending: sign in as the admin, set the global provider and keys; real-key live checks.

## User AI API keys — implemented 2026-09-25

Implements [the API-key execution plan](api-keys-execution-plan.md) milestones K0–K5
(deployed above); live checks with real keys are pending.

- `app/ai.py`: per-workspace tier resolution (`generator_for`), `embedder()` split from
  generation, billing-aware budgets (`userkey:<workspace>` scope, excluded from `global`),
  `AnthropicGenerator` for any key, new `OpenAIGenerator` (Responses API, strict JSON
  schema, `store: false`), `KeyRejected` mapping, automatic retry on the next tier.
- `app/ai_keys.py`: format check, validation request, encrypted storage, removal,
  preference, run-time rejection marking, resumption of AI-paused jobs on save.
- Connections page "AI assistant" card, routes under `/connections/ai/…`, notices,
  home/ask messages for the user-key cap. Feature is off unless `USER_AI_KEYS` is set.
- Migration `0003`: `connections.key_hint`, `connections.validated_at`,
  `workspaces.ai_preference`, `provider_usage.provider`, `provider_usage.billing`,
  `operator_settings`.
- Admin page (`/admin`, `ADMIN_EMAILS` only): global provider Anthropic/OpenAI/off and
  operator Anthropic/OpenAI/Voyage keys, stored encrypted, overriding the environment.
- Default models: `claude-sonnet-5` and `gpt-5.6-terra`.

Verification (local, synthetic providers and mock transports only):

- `uv run pytest -o addopts='' -q`: **121 passed** (81 existing + 40 new in
  `tests/test_ai_keys.py`, `tests/test_admin.py` and Playwright runs of the key flow and
  the admin page at desktop and 390px),
  including `alembic check` after a `0003` downgrade/upgrade with existing usage rows.
- Connector Node suite not run in this session (Node unavailable locally); `connector/`
  is unchanged.
- No real Anthropic or OpenAI key was used. The OpenAI adapter is tested against the
  documented Responses API shape only; its first live call is part of the live checks.

Pending live checks (deployment is recorded above):

1. Admin page: sign in as `errslima@gmail.com`, save the OpenAI (and optionally Voyage)
   key, select the global provider; `/ready` should then report `ai_generation: true`.
2. User keys: add a real Anthropic key, ask a question, see a cited answer and the usage
   in that account's console; add a revoked key and see the refusal; remove the key and
   confirm fallback. Repeat for OpenAI.

## Milestones

| Milestone | State | Evidence |
| --- | --- | --- |
| M0 foundation | done | Tree, pinned deps (`uv.lock`, `deploy/requirements.lock` with hashes, `connector/package-lock.json`), Alembic `0001` (migrate/check/downgrade/upgrade verified), config validation, `/health`, `/ready`, `.dockerignore`, synthetic two-user fixture (`app/synthetic_data.py`), `tests/test_foundation.py` |
| M1 Google onboarding | done (synthetic) | `tests/test_auth.py`: full/partial/cancelled consent, replayed + expired state, browser binding, bad nonce/audience/unverified email, allowlist before storing credentials, missing refresh token (kept vs first-time), concurrent flows/tabs, reconnect with same subject only, revoked grant, logout, CSRF + Origin, two-user isolation |
| M2 Google sync | done (synthetic) | `tests/test_google_sync.py`: bounds and inert HTML, idempotent repeat polls (no reprocessing), resume from saved page after failure, lease recovery after a crashed worker, Gmail history add/delete, invalid cursor → bounded resync, Calendar incremental + 410, truncation without invented cursor, partial consent, revocation stops requests, throttling backoff, disconnect/revoke/delete data, source browsing |
| M3 WhatsApp | done (synthetic) | `tests/test_whatsapp.py` (scoped keys, payload workspace ignored, owner-only no-store QR, dedupe/edit/revoke, bounds, group authors, no name merges, exclusions, cap, disconnect/delete); provisioner with fake Docker (separate mounts/networks/keys, no ports or Docker socket, restart keeps session, no deletion when the API fails or reports an empty DB); Node `connector/*.test.js` (19 tests: auth, no send route, history bounds, durable queue, normalization) |
| M4 memory/answers | done (synthetic) | `tests/test_memory.py`: sourced claims, conflicting locations kept distinguishable, workspace-scoped retrieval and citations, prompt-injection fixture (no tools, no cross-user evidence), server-side citation validation, deletion wins extraction race, exclusion invalidates answers and survives resync, rejections/edits authoritative, per-workspace + global budgets with reconciliation, bounded failure on invalid output, reindex on embedding-model change |
| M5 workflow UI | done (synthetic) | `tests/test_workflow.py` (all screens, gathering flow with calendar conflicts, drafts, rewrite, manual edit, RSVP suggestion needing confirmation, next steps, About me, manual link/unlink, account deletion); `tests/test_browser.py` Playwright at 1280px and 390px through the prefix proxy: sign-in, QR pairing, evidence, claim fix, gathering, draft + real clipboard copy, RSVP, cited answer, no horizontal scroll, no provider writes |
| M6 deployment packaging | deployed; interactive provider checks pending | `deploy/` (Dockerfile, compose, db init, Caddy fragment, provisioner + systemd, backup/restore, release/rollback), `docs/operator-setup.md`, `.github/workflows/ci.yml`, `scripts/test.sh`, `scripts/dev.sh`, `scripts/seed_demo.py`; `tests/test_backup_restore.py` (dump → encrypt → restore into a disposable DB → head, counts, grants decrypt, vector query) |

## Original implementation verification (before review fixes)

- `bash scripts/test.sh`: 72 Python tests (real PostgreSQL 16 + pgvector 0.6 via pgserver, non-superuser
  runtime role, Playwright Chromium) and 19 Node tests passed. It also passed from a clean copy of
  exactly the Git-visible file set (109 files, no private or state files).
- The demo seed runs and refuses to run on the production URL.
- Production-mode config loads the real `config/google_auth.json` (web client, production
  callback allowed) without exposing the secret. The file stays Git-ignored and untracked.
- `connector/package-lock.json` installs with `npm ci`; `whatsapp-web.js` 1.34.7 and `qrcode` 1.5.4 import.

Not verified locally (Docker unavailable on the development machine): building the two images,
the Compose stack, `backup.sh`/`restore.sh` container steps, the provisioner against real Docker,
and connector recovery with a real Chromium. CI builds both images; the rest is covered by the
live checks below.

## Decisions and deviations

User API keys (deviations from `docs/api-keys-execution-plan.md`):

- The OpenAI adapter uses `httpx` directly (like the Voyage adapter) instead of the
  `openai` SDK, so no dependency or lock-file change was needed.
- A key refused at run time is retried on the next tier inside `ai.generate`, so both
  interactive requests and worker jobs continue without a failed attempt; the worker needs
  no `KeyRejected` handler (it is an `AIUnavailable` and keeps that handling).
- Validation attempts are limited with the existing in-process `throttle` (10 per hour per
  workspace and client IP), not a database counter.
- `ai.provider()` remains the operator tier and still serves embeddings through
  `ai.embedder()`, so existing tests and their fakes are unchanged.

- Provider (M0): generation via the official `anthropic` SDK (1.8.0), model `claude-opus-5`
  (`ANTHROPIC_MODEL`), structured JSON output, low/medium effort, server-side refusal fallback
  (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`; turn off with
  `ANTHROPIC_REFUSAL_FALLBACKS=0`). No tools are ever passed. Embeddings via Voyage AI
  `voyage-3.5`, 1024 dimensions (Anthropic offers no embedding endpoint). The Voyage request
  shape comes from its public API and has not been exercised live.
- EnzoSocial's `patch-library.cjs` was not copied: it only patches the send path, and Moresocial has none.
- Static assets are served by a small whitelisted route instead of Starlette `StaticFiles`,
  which resolves paths wrongly when `root_path` is set and the proxy has already stripped the prefix.
- Calendar events first seen already cancelled are never imported. Later cancellations delete
  the source's text and derived memory.
- Rebuild/embedding/extraction jobs are reconciled every 10 minutes (`reconcile_ai`), which also
  performs reindexing after an embedding-model change.

## External dependencies and pending live checks

| Check | State | Who |
| --- | --- | --- |
| Google APIs enabled, consent scopes/audience, test user | Reported done by the operator; not independently verified | operator |
| Real Google consent + bounded first sync | **pending** | operator after deploy |
| Real WhatsApp QR pairing, history sync, restart keeps session | **pending** | user pairs phone |
| Anthropic + Voyage keys; live cited-answer and extraction quality evaluation | **pending**: no keys available | operator |
| Docker image builds, Compose stack, provisioner with real Docker | **passed** build/start; actual paired connector lifecycle pending | deployed |
| VPS rollout (ports, subnet, Caddy route, existing apps unaffected) | **passed** | deployed |
| First backup + scratch restore on the host | **passed**; scratch resources removed | deployed |

Deployed revision: `c921458b7ece3329e41ef7390dd4bf171ef062f5`. Rollback target: none (first deployment); removal procedure is in the operator runbook.
