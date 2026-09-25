# Current architecture and feature map

Updated 2026-09-25. This describes the implemented first release. For the active
revision, deployment evidence and outstanding checks, read
[implementation status](implementation-status.md). The execution plan and original
[review handover](handover-review.md) are historical inputs, not current status.

## User-facing features

| Area | Implemented behavior | Main code |
| --- | --- | --- |
| Onboarding | One Google flow for identity, Gmail read and Calendar read; partial consent, reconnect, beta allowlist | `app/auth_routes.py`, `app/accounts.py`, `app/google.py` |
| Connections | Google connection status; WhatsApp QR linking, status and disconnect; users' own Anthropic/OpenAI API keys; account deletion | `app/pages.py`, `app/whatsapp.py`, `app/ai_keys.py`, `app/templates/connections.html` |
| People and sources | Contact profiles, explicit identity linking/unlinking, source browsing, exclusions, relationship notes and claim corrections | `app/ingest.py`, `app/pages.py` |
| Personal profile | Editable self-profile and evidence-backed memory | `app/pages.py`, `app/models.py` |
| Gatherings | Invitees, candidate dates, conflicts with the user's calendar, manual drafts, RSVP tracking and optional next steps | `app/gatherings.py`, `app/pages.py` |
| Admin | `ADMIN_EMAILS` only: global AI provider (Anthropic/OpenAI/off) and operator API keys, overriding the environment | `app/admin.py`, `app/operator_settings.py`, `app/templates/admin.html` |
| AI assistance | Claim extraction, embeddings, hybrid retrieval, cited answers, invitation drafts, rewrites and RSVP suggestions | `app/memory.py`, `app/retrieval.py`, `app/ai.py`, `app/gatherings.py` |

AI assistance is implemented but **disabled in the deployed configuration**
(`AI_PROVIDER=none`); live provider behavior and quality remain unverified.
Generation runs on one of two tiers, resolved per call from the job's or request's
workspace (`ai.generator_for`): the workspace's own API key when `USER_AI_KEYS` enables
it and a key works (Anthropic Messages API or OpenAI Responses API, billed to the user,
capped by `AI_DAILY_TOKENS_USER_KEY`), otherwise the operator tier under the workspace and
global budgets. The operator tier is built from the admin page's settings
(`operator_settings`), falling back to `AI_PROVIDER` and the secret files; every process
rebuilds it within 30 seconds of a change. A key the provider refuses is flagged on the Connections page and the call
moves to the next tier. Embeddings always use the operator's embedder, so vectors never
mix models or accounts. Manual
workflows remain available. Integrations are read-only: drafts are copied by the
user, and the app does not send messages, write Gmail drafts or change calendars.
It does not know invitees' calendars merely because it can read the user's calendar.

## Data flow and storage

1. Google authorization establishes a private workspace and encrypted grants.
   `app/google_sync.py` imports bounded Gmail and primary-calendar history;
   subsequent Gmail history pages are checkpointed without discarding large bursts.
2. Each linked WhatsApp workspace gets a separate Node/Chromium connector and
   persistent session directory. It sends authenticated data to the internal API.
3. `app/ingest.py` stores normalized sources and participants, resolves provider
   identifiers and invalidates derived data when sources change or are excluded.
4. PostgreSQL jobs (`app/jobs.py`, `worker/main.py`) rebuild conversation chunks,
   generate embeddings and extract claims when AI is enabled. Leases and idempotency
   keys support retries; budget exhaustion defers AI jobs.
5. Retrieval combines structured ownership filters with memory search. Citations
   refer to the actual source segments in the matching chunk, including later
   passages of long messages. Claims and answers retain source references.

PostgreSQL with pgvector holds both relational records and vectors; there is no
separate vector database service. `app/models.py` defines accounts, workspaces,
connections, sources, people, chunks, embeddings, claims, gatherings, drafts,
answers, jobs and usage budgets. Users' API keys are `connections` rows (provider
`anthropic`/`openai`) holding only the encrypted key, a fingerprint and its last four
characters; `provider_usage` records which provider and tier (`operator`/`user`) paid. Workspace scoping and composite foreign keys
separate users' data; matching contacts do not create shared cross-user profiles.

Migration `0001` creates the initial schema. `0002` adds source segments and the
connector cleanup outbox. `0003` adds API-key metadata, the provider preference and usage
billing columns and the `operator_settings` table (additive; downgrade removes stored keys). Pipeline `v2-segments` rebuilds legacy chunks, which are
excluded from retrieval until rebuilt. Account deletion leaves only an opaque
cleanup identifier until the host removes connector resources and acknowledges it.

## Runtime and operations

- FastAPI/Jinja serves the UI. Blocking page work runs in the thread pool; internal
  async endpoints offload database work. AI requests still occupy request threads
  and database resources, so this remains a small-beta architecture.
- Docker Compose runs Postgres, web and worker. Caddy strips `/moresocial/` and
  proxies only host port 8772. Port 8773 is loopback-only management; connector
  ingestion uses the separate internal listener and workspace-specific credentials.
- The host systemd provisioner manages per-workspace connectors, preserves session
  mounts during image upgrades/rollbacks, and retries durable account cleanup.
- Secrets are mounted from `/srv/moresocial/secrets`, outside Git. Daily encrypted
  backups include the database, configuration and WhatsApp sessions, with 14-day
  retention. Backups currently remain on the same server.

See the [operator runbook](operator-setup.md) for deployment, secret configuration,
backup/restore and rollback; [connections](connections.md) for OAuth/WhatsApp design;
and [review findings](review-findings.md) for the six fixes and regression coverage.
The validated local suite has 121 Python tests and 19 Node tests. Synthetic tests
do not establish that real Google sync, WhatsApp pairing or AI quality work.
