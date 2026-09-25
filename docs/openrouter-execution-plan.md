# OpenRouter-only AI execution plan

Status: implementation handoff, updated 2026-09-25. These are instructions for work
to perform, not a claim of completed implementation or verified live behavior.

## Instructions to the implementing agent

Implement this plan in `moresocial/`. Read applicable AGENTS.md instructions and
inspect the current tree first. Preserve unrelated work: MoreSocial may be untracked
in the enclosing repository. Reuse existing adapters, settings, jobs and migrations.
Complete implementation and available checks; report unavailable live checks separately.
This handoff authorizes implementation, not production deployment, external messages,
or deletion of live credentials. Resolve routine implementation choices autonomously.

This plan supersedes the previous transitional OpenRouter plan and conflicting AI
provider guidance in older documents. The user explicitly removed the no-data-collection
and zero-retention restrictions for this release. Do not reconfirm those restrictions
or make them an activation prerequisite.

## Settled product decisions

- OpenRouter is the only production AI integration. One operator-owned key pays for
  generation and embeddings for every workspace. No personal keys, direct Anthropic,
  OpenAI or Voyage integration, or selectable legacy mode remains.
- Admin setup: **OpenRouter API key**, **Reasoning model**, **Embedding model**, and
  **Test and save**. Reasoning covers all existing text tasks: extraction, answers,
  invitation drafts, rewrites and RSVP suggestions. No per-user/task model selection.
- Include key replace/remove, an explicit disable action, separate operation health,
  usage/spend status and rebuild progress. These are status/operational controls,
  not additional required model settings.
- Pin exact model IDs. No automatic model selection, substitution or model fallback
  lists. OpenRouter may route/fail over among compatible providers of the same model.
- Remove mandatory upstream-provider allowlists, `data_collection="deny"` and
  `zdr=true`. Use normal routing with support for required parameters. Do not promise
  no training or zero retention. Account/provider policies still apply; document
  downstream processing accurately. Do not enable payload logging as part of this work.
- Preserve encrypted credentials, admin authorization, Origin/CSRF protections,
  safe logging, workspace isolation, exclusions/deletion, schema and citation validation.
- Keep per-workspace/global token allowances. Add spend visibility and a monetary
  limit on a dedicated OpenRouter key, managed in OpenRouter. No billing platform or
  mandatory budget field in the simple model setup form.
- Keep a disabled state and deterministic test provider, neither an alternative
  production provider. Browsing/importing should work when AI is unavailable.
- Keep the existing stack, hosting layout, Google/WhatsApp behavior and read-only
  integrations. No gateway service, orchestration framework or new vector database.

## Implementation inventory

Recheck actual code before editing; existing mocks do not establish live compatibility.

| Area | Foundation and remaining work |
| --- | --- |
| `app/ai.py` | OpenRouter adapter exists alongside legacy adapters/user-key fallback. Routing currently requires allowlists/ZDR; effort is accepted but not sent; embedding dimension/query-document controls are omitted. |
| `app/operator_settings.py`, `app/admin.py`, `app/templates/admin.html` | Encrypted keys and provider selector exist. Implement database-backed model selection and validated activation. |
| `app/config.py`, `.env.example`, `deploy/compose.yaml` | Remove legacy settings and required provider allowlists; keep optional bootstrap. |
| `app/ai_keys.py`, `app/pages.py`, `app/templates/connections.html` | Remove personal-key routes/preferences/UI; move shared allowance/status helpers before deleting dependencies. |
| `app/models.py`, migrations `0003` and `0004` | Usage metadata and embedding-space identity exist. Allocate the next free migration; never rewrite applied migrations. |
| `app/memory.py`, `app/retrieval.py`, `worker/main.py` | Space filtering and missing-vector jobs exist; add active/pending index lifecycle and consistent configuration snapshots. |
| Tests | Extend OpenRouter/admin/budget/memory/workflow/browser tests; replace removed personal-key behavior tests. |
| Dependencies | Remove legacy-only dependencies, including Anthropic SDK if unused, and regenerate all relevant lockfiles. |

## Phase 1 — Settings and admin experience

1. Define one effective configuration shared by web and worker: key, enabled state,
   reasoning model, active embedding configuration, optional pending embedding
   configuration and revision. Keep secrets out of representations and logs.
2. Persist model selections in operator settings. Database values override optional
   environment/file bootstrap; explicit disable/key removal must never reactivate a
   lower-priority secret. Existing OpenRouter model settings may seed initial values.
   Routine changes require neither environment edits nor restart. Incomplete setup
   leaves AI unconfigured without preventing Admin from starting.
3. Build separate selectors from current OpenRouter model catalogs. Filter by supported
   structured-output/context capabilities and verified embedding contracts. Cache
   metadata; catalog outages must not disable configured models or cause substitution.
4. Test submitted candidate settings with tiny synthetic generation and embedding
   inputs before atomic activation. Explain the small potential charge. Test the candidate,
   not the current adapter. Failure preserves working settings and does not activate
   the replacement secret. Apply revision checks so stale tests cannot overwrite newer edits.
5. Refresh settings in both processes within the existing bounded interval (roughly
   30 seconds). Each request/job captures one immutable configuration snapshot.
6. Key replacement alone does not rebuild vectors. Disable/removal stops new paid work;
   document that in-flight requests may complete. Show active versus pending models clearly.

Acceptance: both models can be configured entirely in Admin; failed validation keeps
working settings intact; normal users cannot view secrets or change global settings.

## Phase 2 — OpenRouter-only runtime and model contracts

1. Route all production generation/embedding work through the shared configuration.
   Remove direct-provider adapters, personal-key resolution/caches/preferences/routes,
   legacy configuration and UI. Update shared helpers/callers before removing modules.
2. Keep the fixed `https://openrouter.ai/api/v1` host. Remove mandatory `only`,
   `data_collection` and `zdr` request fields and their startup requirements. Require
   essential parameter support; verify endpoint-specific routing contracts. Same-model
   provider failover is allowed; different-model fallback is not.
3. Generation must handle all existing strict JSON schemas, context sizes and language
   requirements. Map reasoning effort only when supported. Define bounded internal
   defaults per operation, allowing room for reasoning and useful output; no extra
   required admin knob. Validate extraction, answers and drafts beyond a trivial probe.
4. For every selectable embedding model, establish dimension, input limits, supported
   dimension parameter, and document/query formatting. Derive/persist dimension
   internally. Do not assume 1024 dimensions or identical optional parameters. The
   current vector column is dimension-flexible; inspect indexes for constraints.
5. Validate vector counts, unique/complete indices, dimensions, finite/nonzero values
   and returned model identity. Never pad/slice vectors or silently truncate text.
   Split/batch within model limits; include preprocessing in embedding-space identity.
   Only accept documented canonical model-ID equivalences, not silent substitution.
6. Bound timeouts/retries. Distinguish invalid key, credit/spend limit, unavailable model,
   throttling, malformed output and temporary failures. Honor Retry-After and defer jobs
   appropriately. No unbounded retry or paid schema-repair loop.
7. Preserve local schema/citation validation. Never log credentials, private text,
   prompts, completions, reasoning traces or unsanitized upstream errors.

Acceptance: missing OpenRouter setup or saved legacy keys cannot trigger direct-provider
requests. Both operations use one key and safe, validated model contracts.

## Phase 3 — Safe embedding-model changes

1. Reuse/extend space identity: exact model, dimension, preprocessing/query-document
   contract version, and gateway namespace where needed. Apply it to vectors, caches,
   retrieval, uniqueness, job keys and reconciliation. Equal dimensions are not compatibility.
   Key changes and generation settings must not change embedding identity.
2. Keep active and pending embedding configurations. A changed embedding selection
   stages a background rebuild; queries continue using the active model/index. Show
   requested versus active model and progress. A reasoning-only change affects future
   generation, without regenerating old answers, drafts or claims.
3. Index newly created/changed eligible chunks into both spaces during rebuild. Jobs
   carry the target space/configuration and cannot switch models midway. Query embedding
   and vector filtering must share the same immutable active snapshot.
4. Make rebuilds restart-safe, idempotent, budgeted and fair across workspaces. Track
   current/missing chunks and sanitized errors. Check source/chunk versions before
   persistence: edits, exclusions and deletion must win over late provider responses.
5. Transactionally recheck coverage before atomic cutover, coordinating with ingestion
   so concurrent commits cannot be missed. In-flight queries may finish on their old
   snapshot. Never compare or merge similarity scores from incompatible spaces.
6. Support cancelling/replacing a pending rebuild. Obsolete jobs exit safely; failed
   rebuilds leave the active configuration intact. Generation can change independently.
7. Initial setup or a legacy index that cannot be queried compatibly through OpenRouter
   uses keyword search with visible reduced coverage while rebuilding. Do not preserve
   a direct-provider adapter just for transition. Query-embedding outages should fall
   back to keyword retrieval where feasible; unavailable generation cannot fabricate answers.
8. Retain previous vectors/configuration through rollout verification. Rollback requires
   an OpenRouter-callable old model and current coverage; reconcile missing changes
   before switching back. Otherwise use keyword search/disabled AI. Schedule obsolete
   vector cleanup after the documented rollback window, with safe reference checks.

Acceptance: same-dimension different models never mix; interrupted rebuilds resume;
concurrent ingestion/deletion, cancellation, cutover and rollback preserve correctness.

## Phase 4 — Shared allowances and spend visibility

1. Generation, document/query embeddings, retries and rebuilds consume operator-paid
   global/per-workspace allowances. Preserve historical provider/billing metadata;
   do not relabel old usage or break reconciliation of existing reserved rows when
   removing live user-key budget behavior.
2. Preserve atomic reservations/idempotent reconciliation. Do not double-count reasoning
   subfields already included in completion tokens. Missing usage is unknown, not zero.
   Keep conservative reservations for ambiguous billed failures; account for potentially
   billed retry attempts, not just the last response.
3. Persist reported cost with decimal storage and verified currency/unit semantics.
   Show known spend separately from unknown/estimated amounts, with generation versus
   embedding breakdown and identifiable reindex cost. Label estimates clearly.
4. Document a dedicated OpenRouter key with a monetary spending limit set in OpenRouter.
   Show available key-limit/credit status if supported; do not require a privileged
   management key in the app. Distinguish local token quotas, monetary limits and the
   provider's documented enforcement behavior. No claim that token limits cap exact cost.
5. Preserve concurrency/queue pacing. Large imports and reindexing must yield to
   interactive work and respect workspace allowances. Credit/quota exhaustion needs
   clear status, bounded retries and recovery after credit recovery or allowance reset.

Acceptance: isolated workspace accounting with one shared payer, race-safe local
reservations, visible unknown costs, and recoverable background jobs.

## Phase 5 — Migration and cleanup

- Use additive migrations for settings, lifecycle and cost metadata. Inventory actual
  settings/keys/vector spaces with non-secret counts; do not assume production is empty.
- Legacy credentials become inert immediately. Provide a narrowly scoped explicit
  cleanup command/runbook for old AI connections/operator secrets/preferences after
  rollout verification. Preserve historical usage, source data and non-AI credentials.
  Do not delete live keys in this implementation task or revoke them automatically.
  Document backup retention separately from deletion in the active database.
- Remove legacy secret mounts/environment variables. OpenRouter file-secret setup must
  remain optional when Admin storage is used; validate startup without a nonexistent
  secret file. Keep dependency manifests and lockfiles consistent.
- Update README, architecture, connections, operator setup and implementation status.
  Explain shared billing, downstream processing, future-only reasoning-model changes,
  embedding rebuild costs and model retirement. Remove duplicate plan links and stale
  legacy-mode/ZDR requirements; distinguish implemented, configured and live-tested.
- Readiness checks must not make paid calls. Distinguish configuration, last successful
  test, current failures and rebuilding; key presence is not verified availability.

## Phase 6 — Verification and rollout handoff

Required automated coverage uses mocked HTTP and synthetic content:

1. One-key/two-endpoint requests, required parameters, absent mandatory privacy controls,
   normal same-model routing, and no alternate-model/direct-provider fallback.
2. Candidate validation/save, failed save, catalog outage, authorization/CSRF, concurrent
   edits, disable/removal, bootstrap precedence and consistent web/worker snapshots.
3. Output schemas/refusals/truncation, reasoning limits, malformed/HTTP-200 errors,
   401/402/403/404/429/5xx/timeouts, bounded retries, safe logging and recovery.
4. Embedding contract validation and same-dimension isolation; active/pending rebuild,
   edits/deletion during requests, restart, cancellation, cutover and rollback.
5. Token/cost reconciliation, unknown usage, billed retries, quota races, workspace
   isolation, queue pacing and keyword fallback.
6. Legacy-data migration, disabled personal-key routes, admin/shared-allowance browser
   flows under `/moresocial/`, and existing workflow/citation/isolation checks.

Run targeted checks during implementation, then from `moresocial/` run
`bash scripts/test.sh` (Python/PostgreSQL/pgvector, browser and Node checks). Validate
migrations/schema and relevant Compose configuration; build the image when Docker
is available. Report actual failures/skips and prerequisites. Mocks are not live proof.

Prepare a concrete deployment runbook covering:

1. Release/settings inventory, verified encrypted backup, migration order and old-code
   schema compatibility. Coordinate web/worker deployment to avoid mixed legacy/new workers.
2. Dedicated funded key with spend limit and exact model IDs verified using synthetic
   live calls to both endpoints; record contract, latency and usage without private text.
   Live checks need credentials and rollout authorization. Missing credentials block
   live verification, not completion of local implementation.
3. Bounded rebuild/initial indexing and progress monitoring; verify extraction, cited
   answer, invitation draft and retrieval of an older source. Send no messages.
4. Recovery via disabled AI/keyword search or a verified previous OpenRouter index.
   Do not restore an old database over newer writes or resurrect personal-key billing.
   Leave unrelated apps, connectors and hosting routes intact.
5. Explicit post-verification legacy-secret cleanup and old-vector retention schedule.

Definition of done: one key and two Admin model selections, all production AI uses
OpenRouter, safe configuration/index changes, preserved isolation/citations/budgets,
visible spend/failure status, legacy runtime removed and available required checks
passing. Report separately whether migrations, live API validation, deployment and
post-rollout cleanup actually occurred.

## Official references to recheck during implementation

Catalogs, capabilities, pricing and API fields change. Verify current official docs
and tested contracts rather than guessing model IDs or claiming unsupported capabilities.

- [Embeddings API](https://openrouter.ai/docs/api_reference/embeddings)
- [Structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs)
- [Provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)
- [Documentation index](https://openrouter.ai/docs/llms.txt): locate current reasoning,
  model catalogs, usage/cost accounting and API-key spending-limit documentation.
