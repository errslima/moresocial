# Current architecture and feature map

Updated 2026-09-26. This describes the implemented first release. For the active
revision, deployment evidence and outstanding checks, read
[implementation status](implementation-status.md). The execution plan and original
[review handover](handover-review.md) are historical inputs, not current status.

## User-facing features

| Area | Implemented behavior | Main code |
| --- | --- | --- |
| Onboarding | One Google flow for identity, Gmail read and Calendar read; partial consent, reconnect, beta allowlist | `app/auth_routes.py`, `app/accounts.py`, `app/google.py` |
| Connections | Google connection status; WhatsApp QR linking, status and disconnect; shared-AI status; account deletion | `app/pages.py`, `app/whatsapp.py`, `app/templates/connections.html` |
| People and sources | Contact profiles, explicit identity linking/unlinking, source browsing, exclusions, relationship notes and claim corrections | `app/ingest.py`, `app/pages.py` |
| Personal profile | Editable self-profile and evidence-backed memory | `app/pages.py`, `app/models.py` |
| Gatherings | Invitees, candidate dates, conflicts with the user's calendar, manual drafts, RSVP tracking and optional next steps | `app/gatherings.py`, `app/pages.py` |
| Admin | `ADMIN_EMAILS` only: encrypted OpenRouter key, pinned reasoning/embedding models, test/save, disable and rebuild status | `app/admin.py`, `app/operator_settings.py`, `app/templates/admin.html` |
| AI assistance | Claim extraction, embeddings, hybrid retrieval, cited answers, invitation drafts, rewrites and RSVP suggestions | `app/memory.py`, `app/retrieval.py`, `app/ai.py`, `app/gatherings.py` |
| Suggestions | Evidence-backed replies, promises and follow-ups from one-to-one WhatsApp; drafts, snooze, dismissal, resolution and per-chat pause | `app/suggestions.py`, `app/suggestion_pages.py`, [feature details](suggestions.md) |
| WhatsApp | Read-only conversation browser over imported WhatsApp history: newest-first list (50 per page), name search, full chat history (100 messages per page with an older-messages cursor), media placeholders and links to each source | `app/conversations.py`, `app/whatsapp_pages.py`, `app/templates/whatsapp.html` |

AI generation and embeddings are **enabled in production**, verified by the live readiness
check on 2026-09-26. Synthetic tests establish workflow behavior; recommendation quality
still needs evaluation through use.
All generation and embeddings use the shared operator-owned OpenRouter key, with spending limits managed in OpenRouter
and no local workspace or global token allowance. The encrypted admin configuration overrides an optional bootstrap
file and refreshes in each process within 30 seconds. Embedding-space identity includes the
exact model, returned dimension and preprocessing contract; a changed model is staged and
rebuilt before cutover, so vectors never mix spaces. Manual
workflows remain available. Gmail and Calendar remain read-only. WhatsApp supports explicit text sends from
the conversation page, with durable duplicate protection and no automatic retries.
It does not know invitees' calendars merely because it can read the user's calendar.

Authenticated pages share a left sidebar (`app/templates/base.html`); below 900px it collapses
into a native `<details>` menu, so navigation needs no JavaScript. Ordinary signed-in pages
start 24px beside the sidebar in a column up to 1120px wide (16px gutters on mobile);
a template sets `layout = 'wide'` to fill the content area instead. Public pages retain
their centered 860px reading column.

### WhatsApp conversation browser

`GET /whatsapp` renders stored `sources` only: it never calls the connector, and it has no
attachment download. A text composer and `POST /whatsapp/send` support explicit sends.
`whatsapp_sends` records a durable claim before the external call and keeps the outcome
without storing another copy of the message text. Pairing and connection management stay on
Connections. A conversation is one `(provider_account, conversation_id)` pair inside the
workspace, so chats from two linked numbers stay separate. Eligible rows are WhatsApp
messages that are not deleted, are included and have a conversation ID. One SQL
statement ranks each conversation's latest message with a window function
(`coalesce(occurred_at, created_at)`, then source ID) and joins workspace-scoped
`person_identifiers`/`people` to show corrected contact and sender names. Search matches
conversation titles and contact names case-insensitively, with bounded input and escaped
wildcards, before pagination. Opaque WhatsApp IDs are never shown as names: phone-based
IDs fall back to `+number`, and `@lid` IDs to "Unnamed conversation". Selection, search,
list page and history cursor (`before=<source id>`, validated inside the selected
conversation) are query parameters, so refresh, Back and direct links work without
client-side routing. Unknown or foreign selections return the generic 404 page.

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
   keys support retries; provider rate limits defer AI jobs.
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

Migration `0008` adds the Suggestions inbox, conversation review state and evidence references.
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
The validated local suite has 126 Python tests and 19 Node tests. Synthetic tests
do not establish that real Google sync, WhatsApp pairing or AI quality work.
