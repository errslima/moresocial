# Current architecture and feature map

Updated 2026-09-27. This describes the implemented application. For the last recorded
release, deployment evidence and outstanding checks, read
[implementation status](implementation-status.md). The
[archived development plans](archive/development/README.md) and original
[review handover](handover-review.md) are historical inputs, not current status.

## User-facing features

| Area | Implemented behavior | Main code |
| --- | --- | --- |
| Onboarding | One Google flow for identity, Gmail read and Calendar read; partial consent, reconnect, beta allowlist | `app/auth_routes.py`, `app/accounts.py`, `app/google.py` |
| Home | Upcoming detected events and up to three optional catch-up steps for pinned contacts, each with snooze/dismiss | `app/pages.py`, `app/next_steps.py`, `app/templates/home.html` |
| Connections | Google connection status; WhatsApp QR linking, status, heartbeat-based sync health and disconnect; shared-AI availability and today's token usage; account deletion | `app/pages.py`, `app/whatsapp.py`, `app/whatsapp_health.py`, `app/templates/connections.html` |
| People and sources | Contact profiles, explicit identity linking/unlinking, source browsing, exclusions, relationship notes, claim corrections, and check-in preferences (on/off/default, interval, style and boundaries) | `app/ingest.py`, `app/pages.py`, `app/assistant.py` |
| About me | Dated diary entries (searchable sources feeding extraction, always private to the assistant's drafts), evidence-backed observations conservatively grouped when equivalent, and **Updates I can share** with an audience and optional expiry | `app/diary.py`, `app/observation_groups.py`, `app/pages.py`, `app/assistant.py` |
| Events | Recognized automatically from WhatsApp, Gmail and Calendar: evidence timeline, explicit attendance, dismissal, user corrections and reversible identity grouping. No manual creation; `/gatherings` redirects to `/events` and legacy events stay viewable | `app/events.py`, `app/event_identity.py`, `app/event_resolver.py`, [feature details](events.md) |
| Admin | `ADMIN_EMAILS` only: encrypted OpenRouter key, pinned reasoning/embedding models, test/save, disable, key removal, rebuild status and known spend | `app/admin.py`, `app/operator_settings.py`, `app/templates/admin.html` |
| AI assistance | Claim extraction, embeddings, hybrid retrieval, cited answers (Ask), suggestion drafts, event recognition/identity resolution and observation grouping | `app/memory.py`, `app/retrieval.py`, `app/ai.py` |
| Suggestions | Evidence-backed replies, promises and follow-ups from one-to-one WhatsApp, linked to related Events; prepared event reminders, detail questions, check-ins and shared updates; editable drafts in the user's style, **Open conversation** into the WhatsApp composer, snooze, dismissal, resolution and per-chat pause | `app/suggestions.py`, `app/assistant_context.py`, `app/proactive.py`, `app/suggestion_pages.py`, [feature details](suggestions.md) |
| Assistant | One persona (instructions, style, language, examples, initiative), event-preparation and check-in switches, pause, status and a style preview. Prepares drafts only; never sends | `app/assistant.py`, `app/assistant_pages.py`, `app/templates/assistant.html` |
| WhatsApp | Conversation browser over imported WhatsApp history: newest-first list (50 per page), name search, full chat history (100 messages per page with an older-messages cursor), media placeholders and links to each source; a text composer that sends only when the user presses **Send message** | `app/conversations.py`, `app/whatsapp_pages.py`, `app/whatsapp_send.py`, `app/templates/whatsapp.html` |

AI generation and embeddings are **enabled in production**, verified by the live readiness
check on 2026-09-26. Synthetic tests establish workflow behavior; recommendation quality
still needs evaluation through use.
All generation and embeddings use the shared operator-owned OpenRouter key, with spending limits managed in OpenRouter
and no local workspace or global token allowance. The encrypted admin configuration overrides an optional bootstrap
file and refreshes in each process within 30 seconds. Embedding-space identity includes the
exact model, returned dimension and preprocessing contract; a changed model is staged and
rebuilt before cutover, so vectors never mix spaces.

Usage is still accounted: each call reserves an estimate and is reconciled to the actual
tokens per workspace and globally per UTC day (the historically named `usage_budgets`
table), and `provider_usage` records USD cost when OpenRouter reports it; unknown cost stays
unknown. Connections shows today's tokens. An OpenRouter 429 starts a provider cooldown
(honouring `retry-after`, at most 300 seconds) and background jobs are deferred without
spending an attempt; `AI_CONCURRENCY` limits simultaneous calls. Without an AI configuration,
imports, browsing and keyword search continue and AI jobs wait.

Gmail and Calendar remain read-only. WhatsApp supports explicit text sends from the
conversation page, with durable duplicate protection and no automatic retries.

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
   generate embeddings, extract claims, group observations, review conversations for
   Suggestions, detect/resolve events, and scan for and prepare proactive drafts
   (`proactive_scan`, `prepare_message`) when AI is enabled. Leases and idempotency
   keys support retries; provider rate limits and missing AI configuration defer AI jobs.
5. Retrieval combines structured ownership filters with memory search. Citations
   refer to the actual source segments in the matching chunk, including later
   passages of long messages. Claims and answers retain source references.

PostgreSQL with pgvector holds both relational records and vectors; there is no
separate vector database service. `app/models.py` defines accounts, workspaces,
connections, sources, people, chunks, embeddings, claims and observation groups,
suggestions, events (with mentions, identity decisions and pair scans), WhatsApp send
receipts, answers, jobs, operator settings, usage accounting (`usage_budgets`,
`provider_usage`), and the assistant's `assistant_settings`, `person_preferences`,
`shareable_updates` (+ audience), `update_proposals` and `proactive_reviews`. Migration `0012` removed the empty manual-planner `drafts`,
`event_people`, `event_candidates` tables and the unused draft-reference column on
answer citations. Personal-key metadata and encrypted legacy keys remain because the
production inventory found nonempty records; old `provider_usage` rows retain their
historical provider/billing labels. Workspace scoping
and composite foreign keys separate users' data; matching contacts do not create shared
cross-user profiles.

Migrations: `0001` initial schema; `0002` source segments and the connector cleanup
outbox; `0003` personal-key metadata, usage billing columns and `operator_settings`
(the key feature is retired); `0004` embedding-space identity; `0005` OpenRouter
accounting fields; `0006` resumes jobs deferred by the retired shared token allowance;
`0007` diary sources and observation groups; `0008` Suggestions; `0009` WhatsApp send
receipts; `0010` automatic events; `0011` event identity; `0012` retires the empty manual
gathering tables; `0013` connector heartbeat health; `0014` assistant settings, preferences,
shareable updates, proactive state and suggestion producers/event links/targets; `0015`
suggestion context sources (erasing derived text when any contributing source is removed). Pipeline `v2-segments`
rebuilds legacy chunks, which are excluded from retrieval until rebuilt. Account deletion
leaves only an opaque cleanup identifier until the host removes connector resources and
acknowledges it.

## Runtime and operations

- FastAPI/Jinja serves the UI. Blocking page work runs in the thread pool; internal
  async endpoints offload database work. AI requests still occupy request threads
  and database resources, so this remains a small-beta architecture.
- Docker Compose runs Postgres, web and worker. Caddy strips `/moresocial/` and
  proxies only host port 8772. Port 8773 is loopback-only management; connector
  ingestion uses the separate internal listener and workspace-specific credentials.
- The host systemd provisioner manages per-workspace connectors, preserves session
  mounts during image upgrades/rollbacks, and retries durable account cleanup.
- Secret files are mounted from `/srv/moresocial/secrets`, outside Git; the OpenRouter
  key saved in Admin is stored encrypted in the database. Daily encrypted
  backups include the database, configuration and WhatsApp sessions, with 14-day
  retention. Backups currently remain on the same server.

See the [operator runbook](operator-setup.md) for deployment, secret configuration,
backup/restore and rollback; [connections](connections.md) for OAuth/WhatsApp design;
and [review findings](review-findings.md) for the six fixes and regression coverage.
Dated test results are recorded in [implementation status](implementation-status.md).
Synthetic tests do not establish that real Google sync, WhatsApp pairing or AI quality work.
