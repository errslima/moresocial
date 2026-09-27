# Diagnostics investigation and implementation plan

> **Archived 2026-09-27.** Historical development document, kept for its rationale and
> evidence. Current docs and code take precedence wherever they conflict; its
> instructions are not current tasks. See the [archive index](README.md).
>
> Implemented and deployed 2026-09-26. Its open live identity test was completed
> (see [implementation status](../../implementation-status.md)); the runbook is
> [operator setup: Diagnostics](../../operator-setup.md#diagnostics).

## VPS evidence (investigated September 26, 2026)

- Running web and worker image: `moresocial-web:46a30fd`. Web reports healthy.
- Web container created September 25 at 22:25:43 UTC. Its available log contains
  `admin_openrouter_test_failed` at **22:27:06.050 UTC**, code `embedding_model`,
  reference `98c86c087982b9c2`. No requested/returned models or probe stage recorded.
- Deployed `app/admin.py` includes canonical-ID handling from commit `46a30fd`.
  The observed failure therefore occurred with that change already installed.
- Public catalog fetched from the VPS maps `voyageai/voyage-4` to canonical slug
  `voyageai/voyage-4-20260727`. These are catalog facts, not captured response values.
- `embedding_model` can arise in the initial embedding probe's identity check or
  the subsequent query probe's strict identity check. Both happen after the
  reasoning probe passed. Existing logs cannot distinguish these cases or prove
  the exact names submitted in this attempt. The key is not implicated by this code.
- Separate confirmed code defect: `_candidate()` returns a provider using the
  resolved embedding identity, but `save_openrouter()` persists the submitted
  `embedding_model`, discarding that identity. This can make runtime validation
  fail after a successful setup; it does not explain the current failed setup alone.
- Failed form rendering uses saved choices, causing unsaved dropdown selections to
  reset to the first catalog entries. The screenshot therefore does not identify
  the models actually tested.

Investigation was read-only on the VPS. No paid probes, settings changes, deployments,
or credential exports were performed. Historical container logs were not recovered.

## Existing diagnostics and gaps

- `app/security.py`: JSON events with timestamp, random per-event reference, small
  field allowlist, exception class. No shared request context, severity, service,
  release, model identities, or stack locations. Current string validator rejects
  slashes, so adding model IDs requires field-specific validation.
- `app/web.py`: unexpected errors show a reference. Expected admin failures discard
  the reference returned by `emit()`. No normal request completion events;
  `app/serve.py` deliberately disables Uvicorn access logs.
- `app/ai.py`: some transport failures logged; many HTTP/validation paths and
  successful calls have no diagnostic events. Usage accounting is separate and
  does not provide a trace for direct setup probes.
- `worker/main.py`: startup and unexpected failures logged. Most expected failures,
  deferrals, job completion, and queue timing lack correlated events.
- `connector/diagnostics.js`: already has request context, safe stack locations,
  rate limiting, and rotating persisted JSON files. Reuse these conventions.
- `deploy/provisioner.py`: separate JSON events. Python, Node, and provisioner
  schemas are inconsistent.
- Compose uses Docker JSON rotation (3 files of 10 MB per service), not durable
  cross-release storage. Container replacement can remove incident history.

## Implementation sequence

### 1. Make this failure diagnosable and correct model handling

Instrument catalog fetch, reasoning probe, initial embedding probe, query probe,
validation, and settings commit with one operation ID. Record stage, requested
model, catalog canonical model, returned model, expected/actual dimension, HTTP
status, duration, attempt, safe provider request ID when available, and error code.
Record catalog fetch failure explicitly instead of confusing missing metadata with
an actual identity mismatch. Keep errors visible even when debug mode is off.

Show the operation reference and useful stage-specific explanation in Admin.
Preserve submitted model selections on failure while leaving the key input blank.
Capture an instrumented synthetic attempt to establish actual response identities
before choosing a normalization rule. Never accept arbitrary returned models or
strip provider prefixes heuristically. Define one verified identity policy shared
by setup, persistence, runtime validation, and embedding-space construction; save
the tested identity/contract and verify reload behavior.

Acceptance: mocked tests cover selected ID, documented canonical ID, unknown ID,
catalog outage, second-probe mismatch, unchanged settings after failure, and
save/reload/embedding identity consistency. Existing OpenRouter tests contain
expectations from the older provider policy and need reconciliation with the
current intended contract. A subsequent explicit synthetic live test confirms the
observed Voyage response behavior; retain only metadata.

### 2. Establish a shared structured event contract

Extend the existing logger with schema version, UTC timestamp, level, service,
release SHA, event, request/operation ID, job ID, parent operation ID, duration,
outcome, and typed event-specific fields. Use Python contextvars and Node's existing
AsyncLocalStorage; explicitly propagate context into jobs and connector requests.
Generate trusted IDs at ingress and return `X-Request-ID`. Keep an error reference
linked to the operation for every user-visible failure.

Use field-specific allowlists and length/type limits. Never record keys, cookies,
OAuth codes, authorization headers, full URLs/query strings, prompts, messages,
embeddings, or raw provider response bodies. Unexpected exceptions may include
bounded filename/function/line frames without source text, locals, or exception
messages. Use opaque workspace IDs only where needed, not email addresses.

Acceptance: secret-canary tests across Python and Node, concurrency/context-isolation
tests, cross-service correlation tests, and graceful handling of log write failures.

### 3. Cover the app's operational boundaries

- Web/internal APIs: route templates, method, status, duration, error category;
  sample healthy high-volume requests, exclude routine health noise.
- Provider calls: operation, model, attempt, HTTP status, latency, timeout/rate-limit
  category, retry delay, validation result, usage and cost when returned. No bodies.
- Jobs: enqueue, claim, completion, retry, defer, lease loss and terminal failure;
  queue age and execution duration. Link to initiating requests where applicable.
- Google/WhatsApp sync: start/end, page/item counts, lag, scope/auth state changes,
  ingestion and checkpoint outcomes. Never cursor/token values or source content.
- Retrieval/generation: keyword/vector route, result counts, index/space identity,
  budget decisions, schema/citation validation, embedding rebuild progress/cutover.
- Administration/lifecycle: settings revision, disable/remove operations, account
  deletion stages, connector lifecycle, migration, backup/restore, readiness and
  release outcomes. Keep audit events distinct from sampled diagnostic detail.
- Browser: failed app actions can report operation references and enumerated error
  codes; avoid DOM text, form contents, full URLs, and third-party payload capture.

Acceptance: each major workflow can be followed through a single operation/job chain;
expected errors and retries are observable without relying on generic exceptions.

### 4. Preserve logs across deployments and make them usable

Collect sanitized stdout/stderr and provisioner journal events into a persistent
host store outside container lifetimes. Start with restricted rotating JSON files
and a reference-search command; introduce a log service only if usage warrants it.
Set both time retention (proposed 14 days) and a measured total disk cap; monitor
collector health, dropped events, disk pressure, and retention enforcement.
Standardize connector collection to avoid duplicate persisted events.

Default production level INFO with WARN/ERROR retained. Enable scoped DEBUG by
service/operation for a short expiry; DEBUG must not relax content restrictions.
Add low-cardinality alerts for error rates, terminal jobs, queue lag, sync stalls,
provider failures, backup failures, and failed readiness. Deduplicate repeated events
with suppression counts. Update the operator runbook with reference lookup examples.

Acceptance: a request reference remains searchable after web/worker recreation;
rotation, disk limits, expiration, and collector outage behavior are tested.

### 5. Roll out and verify

Ship the targeted diagnostics and model fix first, then shared correlation and
boundary coverage, then persistent collection/alerts. Deploy web and worker together
when changing the embedding identity contract. Validate with synthetic inputs,
failure injection, and existing workflow tests. Confirm the admin failure reference
finds the exact stage and model comparison, and verify log volume against the VPS
disk budget before extending retention. Logging must never be a prerequisite for
serving a request or committing a completed job.

## Implementation status (September 26, 2026)

Implemented and deployed on branch `diagnostics`; see `docs/implementation-status.md` for
live verification and `docs/operator-setup.md` ("Diagnostics") for the runbook.

1. Setup test stages, identity policy (`ai.identity_accepted`), persisted
   `openrouter_embedding_identity` (names the vector space), preserved form, reference in
   Admin. Unknown IDs and catalog outages fail before paid calls; an unchanged verified
   configuration can be re-tested during a catalog outage. **Open:** the live synthetic
   test needs the operator's key; the chosen rule (selected ID or documented canonical ID,
   nothing else) is confirmed or refuted by that attempt's `returned_model`.
2. `app/diagnostics.py` contract (schema 1, level, service, release, correlation IDs from
   contextvars, typed field allowlist with secret-shape refusal, bounded frames, suppression
   counts, never raises). Connector and provisioner share the schema.
3. Request, provider, job, sync, ingest, connector, retrieval/answer, budget, rebuild,
   admin and account-deletion events; browser shows server references only (no client
   telemetry endpoint was added).
4. `/srv/moresocial/logs` store, `deploy/logs.py` (find, tail, status, prune, check, debug,
   emit), hourly `moresocial-logs.timer`, 14-day retention and 1 GiB cap. Alerts are
   recorded in the store and the unit's exit status; no off-host notification channel
   exists yet. Measured volume after deployment was low (see implementation status);
   re-measure after the first busy week before extending retention.
