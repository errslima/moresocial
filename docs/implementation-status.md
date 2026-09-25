# Implementation status

Last updated 2026-09-25. This is the resume point for any agent or operator.

Synthetic-provider tests do **not** prove live Google, WhatsApp or AI integration. The
release is **not deployed**. Live checks are listed separately at the end.

## Review fixes completed

All six confirmed findings in `docs/review-findings.md` are fixed locally:

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
Normal release migration runs it automatically; no production migration has run here.
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
  with real temporary Postgres. Full container restore/rollout still needs a Docker check.

`docs/review-probes.py` now forwards to the permanent regressions rather than retaining
obsolete failing probes. No credentials were changed, and nothing was pushed or deployed.

## Milestones

| Milestone | State | Evidence |
| --- | --- | --- |
| M0 foundation | done | Tree, pinned deps (`uv.lock`, `deploy/requirements.lock` with hashes, `connector/package-lock.json`), Alembic `0001` (migrate/check/downgrade/upgrade verified), config validation, `/health`, `/ready`, `.dockerignore`, synthetic two-user fixture (`app/synthetic_data.py`), `tests/test_foundation.py` |
| M1 Google onboarding | done (synthetic) | `tests/test_auth.py`: full/partial/cancelled consent, replayed + expired state, browser binding, bad nonce/audience/unverified email, allowlist before storing credentials, missing refresh token (kept vs first-time), concurrent flows/tabs, reconnect with same subject only, revoked grant, logout, CSRF + Origin, two-user isolation |
| M2 Google sync | done (synthetic) | `tests/test_google_sync.py`: bounds and inert HTML, idempotent repeat polls (no reprocessing), resume from saved page after failure, lease recovery after a crashed worker, Gmail history add/delete, invalid cursor → bounded resync, Calendar incremental + 410, truncation without invented cursor, partial consent, revocation stops requests, throttling backoff, disconnect/revoke/delete data, source browsing |
| M3 WhatsApp | done (synthetic) | `tests/test_whatsapp.py` (scoped keys, payload workspace ignored, owner-only no-store QR, dedupe/edit/revoke, bounds, group authors, no name merges, exclusions, cap, disconnect/delete); provisioner with fake Docker (separate mounts/networks/keys, no ports or Docker socket, restart keeps session, no deletion when the API fails or reports an empty DB); Node `connector/*.test.js` (19 tests: auth, no send route, history bounds, durable queue, normalization) |
| M4 memory/answers | done (synthetic) | `tests/test_memory.py`: sourced claims, conflicting locations kept distinguishable, workspace-scoped retrieval and citations, prompt-injection fixture (no tools, no cross-user evidence), server-side citation validation, deletion wins extraction race, exclusion invalidates answers and survives resync, rejections/edits authoritative, per-workspace + global budgets with reconciliation, bounded failure on invalid output, reindex on embedding-model change |
| M5 workflow UI | done (synthetic) | `tests/test_workflow.py` (all screens, gathering flow with calendar conflicts, drafts, rewrite, manual edit, RSVP suggestion needing confirmation, next steps, About me, manual link/unlink, account deletion); `tests/test_browser.py` Playwright at 1280px and 390px through the prefix proxy: sign-in, QR pairing, evidence, claim fix, gathering, draft + real clipboard copy, RSVP, cited answer, no horizontal scroll, no provider writes |
| M6 deployment packaging | done locally; live pending | `deploy/` (Dockerfile, compose, db init, Caddy fragment, provisioner + systemd, backup/restore, release/rollback), `docs/operator-setup.md`, `.github/workflows/ci.yml`, `scripts/test.sh`, `scripts/dev.sh`, `scripts/seed_demo.py`; `tests/test_backup_restore.py` (dump → encrypt → restore into a disposable DB → head, counts, grants decrypt, vector query) |

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
| Docker image builds, Compose stack, provisioner with real Docker | **pending** (CI builds images) | operator/CI |
| VPS rollout (ports, subnet, Caddy route, existing apps unaffected) | **pending**: no server access used | operator |
| First backup + scratch restore on the host | **pending** | operator |

Deployed revision: none. Rollback target: none (first deployment).
