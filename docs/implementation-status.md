# Implementation status

Last updated 2026-09-27. This is the resume point for any agent or operator. **Current
state** points to the last recorded release; the sections after the pending list are dated
history, newest first. A revision called active or current inside a dated entry was current
only on that date. Sections about Anthropic, Voyage, OpenAI and personal keys describe
retired implementations. Local migration and synthetic checks are not evidence of live API
validation or deployment.

## Current state (recorded 2026-09-27)

- **Last verified release:** `79304e9`, schema `0012`, deployed and checked on
  2026-09-27. It follows the reviewed runtime cleanup `f6cf3eb` and removes only
  the empty manual gathering schema. Readiness passes; 18 authenticated desktop/mobile
  page checks passed after each deployment; the WhatsApp connector is `ready`.
  The prior event-identity release was
  `d5fc668` (recorded by `bbb8379`).
- **Current behavior:** see [architecture](architecture.md). Production AI uses one
  operator-owned OpenRouter configuration managed in Admin; spending limits are set on the
  OpenRouter key and token usage is still reported, with no local caps. Personal AI keys
  and the direct Anthropic, OpenAI and Voyage adapters are retired. Events are recognized
  automatically; manual gathering creation and invitation planning are retired. WhatsApp
  sends only on an explicit user action; Gmail and Calendar remain read-only.
- **Rollback for that release:** returning to `f6cf3eb` requires a coordinated
  downgrade to schema `0011` before starting old code. The live rollback guard was
  verified to refuse the incompatible image without changing the active release.
  See the [schema rollback procedure](cleanup/schema-rollout.md#failure-and-rollback).
- **Cleanup verification:** [runtime cleanup](cleanup/README.md) passed **295 Python**
  and **33 connector** tests locally; the connector suite also passed **33/33** on
  production's Node **22.23.3** image. Review fixed inventory detection of a leftover
  draft-reference column. The encrypted pre-release backup was restored successfully
  in a disposable database, then the scratch resources were removed.
- **Schema cleanup verified:** final local suite **305 Python tests passed**, including
  browser/migration/restore coverage; **33 connector tests passed**. Rehearsing
  upgrade/downgrade/re-upgrade on a restored production backup preserved every retained
  row. During the live migration, all **39 retained tables / 21,040 rows** matched
  aggregate counts and content digests before writers resumed. Production inventory
  reports G `removed`, K `ineligible`: two personal-key and two legacy operator-key
  records are retained. No stored credentials were deleted or revoked.
- **No separate entry:** the diary and observation-group release (`6c762e3`, migration
  `0007`) preceded the Suggestions release below; its verification notes are in the
  archived [diary notes](archive/development/diary-observations.md).

## Pending live checks and operator actions

Consolidated on 2026-09-27 from the dated entries below and the archived plans.

| Item | State | Who |
| --- | --- | --- |
| Dedicated Google consent check (fresh sign-in, partial consent) with bounded sync counts | **pending**: Gmail import is evidenced by the 2026-09-26 Events live check (a booking email was recognized), but no consent-flow check has been recorded | operator |
| AI quality of extraction, cited answers, Suggestions and event recognition | **pending**: live model runs succeeded on 2026-09-26/27, but quality was checked only on small samples; evaluate through use | owner |
| Two remaining Possible duplicates (same-titled records; two birthday records) | **pending** owner review in Events, left open by the event identity release | owner |
| Private recovery-vault copy of recovery secrets, and backups off this host | **pending**: not configured as of 2026-09-25; no later record | operator |
| Cleanup of inert legacy AI secrets (`anthropic_api_key`/`voyage_api_key` files, legacy `operator_settings` key rows) and old vectors | **pending**, only after the rollback window and through a separately reviewed operation ([rollout runbook](openrouter-rollout.md), step 5) | operator |
| Retired variables in production `runtime.env` (`USER_AI_KEYS`, `ANTHROPIC_MODEL` and `OPENAI_MODEL` were added on 2026-09-25) | optional: ignored by the application; no record of removal | operator |
| Log volume re-measurement after the first busy week, before extending the 14-day retention | **pending**, from the diagnostics release | operator |
| Split/undo of a real event group | not exercised live by design; covered by synthetic tests | none |
| Read-only legacy schema inventory on production (`python -m app.legacy_inventory`, [cleanup plan 3](cleanup/03-historical-schema.md)) | **pending**: needed before any retired-table or key-column removal; only local synthetic inventories exist | operator |

Resolved according to later entries: WhatsApp QR pairing, history sync and session
continuity (2026-09-26: "Linked and syncing", a 50-chat history sync, connector upgrades
and restarts keeping the session); the real-key OpenRouter **Test and save** (generation
and embeddings in use by the 2026-09-26 Suggestions and Events releases); Docker builds,
the Compose stack and the provisioner with a paired connector. Obsolete: live checks for
personal AI keys and for Anthropic/Voyage keys, whose features were retired.

## Event identity — 2026-09-27

Implemented the archived [event identity plan](archive/development/event-identity-execution-plan.md):
stable original records, reversible groups (`canonical_event_id`) from user and automatic decisions, the `resolve_events`
resolver with server-side verdict validation, field-level provenance, review UI (Possible
duplicates, Same event / Keep separate, Separate…, Undo…) and the dry-run operator tool
`scripts/reconcile-events.py`. Migration `0011` (additive). Production revision after this
release: `d5fc668` (released as `3726c54` in mode `observe`, then follow-ups `6d7fd24`, `f2dad92`
and `d5fc668`), resolver mode `apply`; previous release `90384f2`. The owner's confirmed Sunday
outing was repaired through the tested correction service (one event, 83 sources, 16:15–17:00).
Details and verification: section M6 of the archived plan. In summary:

- Tests: `tests/test_events.py tests/test_event_identity.py` 38 passed; event browser cases 4
  passed (1440/390 px). The full suite had the same 22 retired AI-key/production-secret
  failures as the pre-change baseline and no new failures.
- Pre-release backup `moresocial-20260926T230947Z.tar.enc`. Observe sample: 14 pairs
  (`identity-v1`) and 5 (`identity-v2`), one validated same verdict confirmed by the owner, no
  false merge; then mode `apply`. Backfill: 19 model calls; an unchanged rerun made none.
- Two remaining Possible duplicates were left for the owner (see the pending list). No
  WhatsApp notification was sent for this release.

## Automatic Events — 2026-09-26

Implemented the Events overview and evidence timeline described in [Events](events.md).
Manual event creation/planning routes removed; `/gatherings` redirects permanently.
Migration 0010 is additive. Source ingestion plus worker reconciliation recognize Calendar,
WhatsApp and qualifying email events, with cross-conversation matching, explicit attendance,
booking precedence, persistent dismissal and immediate source-exclusion invalidation.

Local verification: 78 focused integration/browser checks pass (Events, auth, agent sessions,
Google sync, workflow, source review fixes, diagnostics; 1440/390px browser coverage). Rendered
screenshots inspected. A broad run also exposed legacy AI-key/budget/config tests that target
features removed by the earlier OpenRouter migration; representative failures reproduced on
unchanged 7b643cc. These are separate from the Events release checks.

Live deployment and recognition verification recorded below.

Deployment completed at revision `90384f2` (migration `0010`). Readiness checks pass for
DB, migrations, Google configuration, encryption, generation and embeddings. Authenticated
live reviews pass at 1440px and 390px: Events navigation, permanent legacy redirect, absence
of manual creation, detail/timeline/source links, Home Upcoming and no browser errors or
horizontal overflow. The temporary review session was revoked.

The reference booking is automatically assembled across email and WhatsApp; the final check
found 49 supporting sources and six attendance replies. Comparing against the booking exposed
whitespace-heavy email truncation: input now compacts email whitespace and preserves the whole
booking table, recognizes consecutive sessions as one outing, and prioritizes booking email
over conversational estimates. Its full booked time range now matches the source.

Verification after live fixes: 82 combined release checks passed, followed by 12 booking/UI
checks and 74 final Events/auth/sync/exclusion/diagnostic checks. Initial recognition backfill
continues in the normal worker; new imports also schedule reviews automatically. Recognition
has the documented recent-history and ambiguity limits; source context remains available for
review. No source-message content or private screenshots are committed.

The requested review notification was sent to the owner’s linked WhatsApp self-chat with a
confirmed message receipt. It includes the Events URL and notes the ongoing initial history scan.

## WhatsApp sending and suggestion conversation links — 2026-09-26

Production revision after this release: `eaf3848` (includes `b3c41c8`), schema `0009`.
Both releases used `deploy/release.sh`; web/worker readiness and the upgraded
connector passed. Prior feature release: `4344fbb`. Migration 0009 only adds send
receipts; it does not alter existing source data.

- Suggestions have **Open conversation** buttons leading to MoreSocial's own WhatsApp
  page, with the evidence's exact account/chat and a current saved draft when available.
- The conversation composer sends text explicitly from the matching linked account.
  CSRF, workspace/account checks, an existing-chat check (or the verified self-chat),
  and durable request claims guard sending. Concurrent submissions/refreshes cannot
  repeat a send; uncertain results preserve the text and disable automatic retry.
- Sent messages enter the normal idempotent source-ingestion path. Gmail/Calendar
  remain read-only. Delivery/read status is not exposed by the UI.
- WhatsApp's current MsgKey omits `_serialized`, and a phone-based self-chat resolves
  to an `@lid` conversation. The pinned library patch preserves the exact generated
  message key; the connector returns the canonical destination and authoritative
  self-chat mapping. The app redirects to that canonical stored conversation.

Verification:

- Initial release regression selection: **118 passed**, including WhatsApp,
  suggestions, authentication, agent sessions and desktop/mobile browser checks.
  After the canonical-ID fix: **91 passed** covering WhatsApp/suggestions/browser;
  **33 connector tests passed**, including source-drift guards for the library patch.
- The unrestricted suite retains **22 unrelated legacy failures**: retired personal
  AI-key/provider behavior (including missing legacy `httpx2`), its two browser
  scenarios, and an old production configuration expectation. The prior saved run
  already reported the corresponding 20 non-browser failures.
- Authenticated live checks: all five visible suggestion links at **1440px and 390px**
  opened the correct internal conversation with an enabled composer; no document
  overflow. Self-chat selection, the success receipt and refresh without duplicates
  also passed at both sizes. Web and connector report ready.
- Exactly **one** user-approved test message was sent to the linked user's own account.
  WhatsApp acknowledged it (`ack=3`). It initially exposed the canonical-ID issue;
  after the fix, a read-only replay of that existing message through the corrected
  connector confirmed serialization/destination handling. The original uncertain
  receipt was reconciled against that single verified message. No second live message
  was sent. Future live receipt checks must not rerun the original send script.
- Temporary operator browser session revoked after verification.

## Live layout and Suggestions repairs — 2026-09-26

Production revision after this release: `4344fbbaeda16b20e23b469b75b596b91a5bd1b6` (`4344fbb`).
This includes the operator session helper and cumulative repairs in `2ec781a`,
`8002f17` and `4344fbb`. Standard `deploy/release.sh` image builds, migrations and
readiness checks passed. Schema remains `0008`; no data migration was added.

- Ordinary signed-in pages now start 24px beside the sidebar and use up to 1120px
  of width; mobile retains 16px gutters. Public pages retain the old reading width.
  Home no longer says WhatsApp suggestions require pinned people or gatherings.
- Fixed invisible document overflow below the WhatsApp list/reader: absolutely
  positioned accessibility labels now stay within their scrolling pane. Long
  identifiers on People wrap instead of overflowing mobile screens. Content-hashed
  CSS/JS URLs make browsers pick up deployed layout changes immediately.
- Real model responses were rejected because evidence contained quotations instead
  of message labels. The per-review schema now restricts citations to exact labels;
  internal source IDs and versions stay out of the prompt. Follow-up live checks
  exposed invalid due-date strings and a promise anchored to the other person's
  message. The schema now requires timezone-aware date-times and restricts reply/
  promise anchors by direction. Existing server validation and publication race
  checks remain in place.
- Failure notices count conversations whose latest review still failed, rather
  than all historical failed attempts. Failed empty inboxes explain that the review
  is incomplete instead of giving an all-clear message.

Live verification, with the user's explicit approval to retry through the configured
OpenRouter model using stored WhatsApp context:

- **36 of 36 conversations reviewed successfully; 14 active suggestions.** Latest
  jobs are all done; no pending reviews or unresolved failures at final verification.
  Earlier failed job records remain as audit history. Temporary provider rate limits
  recovered through normal retry/backoff.
- All ten signed-in routes (Home, WhatsApp, Suggestions, People, Ask, Gatherings,
  About me, Connections, Admin and Sources) returned 200 at 1440×900 and 390×844,
  with no horizontal overflow or JavaScript page errors. Desktop content starts
  beside the sidebar. WhatsApp list and open chat fit the viewport without the
  previous blank overflow; mobile back navigation works.
- Verified suggestion context/evidence and source links, Snoozed/History views,
  People search and detail, and WhatsApp source filtering/detail. Generated one
  editable live draft and checked its mobile layout and Copy message control.
  No message was sent and no suggestion was marked done, dismissed or snoozed.
- The connector restarted with the final image, completed a 50-chat history sync,
  and Connections reported **Linked and syncing**. The final Suggestions page had
  cards with no warnings or pending-review notice. Screenshots are private under
  ignored `.dev/`, including `final-live-suggestions.png`.

Automated verification:

- **31 Suggestions tests** passed after the final schema changes; **18 browser
  cases** passed (two obsolete personal-AI-key cases excluded). The browser suite
  initially caught the old 860px width expectation; it was updated for the requested
  1120px layout and both affected sidebar cases passed on rerun.
- Earlier combined Suggestions/WhatsApp/browser run: **55 passed, 2 deselected**;
  foundation/browser follow-up: **25 passed, 3 deselected**. Auth/session helper:
  **28 passed**. Connector tests: **27 passed** in an isolated, network-disabled
  production Node image; connector source was unchanged.
- Broader non-browser run before the final date/direction additions: **212 passed,
  20 failed**. These match the previously documented legacy personal-key/provider
  and production-config failures. The two legacy browser key tests are also still
  outstanding; the full suite is not green.

Code and this deployment record are committed locally on `whatsapp-sidebar`;
deployment used a Git bundle, without pushing or merging. This record is a later
documentation-only commit than the running application. Immediate rollback:
`sudo /srv/moresocial/src/deploy/rollback.sh 8002f17`; pre-repair release: `d667d92`.

## Temporary agent browser access — 2026-09-26

At the time of this helper-only check, live source and images remained at `d667d92`
(sidebar and WhatsApp release), confirmed
through `/srv/moresocial/src` and `release.env`. No application deployment, restart or
schema migration was needed for the operator-only session helper.

- Added `app/agent_session.py` and workstation wrapper `scripts/agent-session.py`.
  The wrapper executes the helper through the existing operator SSH connection in
  the web container; creates a normal session for one existing allowlisted account;
  and privately downloads Playwright storage state. Default TTL: two hours, maximum
  one day. No public authentication endpoint or Google grant changes.
- Private files live under ignored `.dev/` with mode 0600. The revoke command removes
  them and revokes only that session. See [agent browser login](agent-browser-login.md)
  for commands and a prompt future agents can use.
- Targeted verification: **28 passed** across the new session tests and existing
  Google authentication tests. No full-suite rerun for this operator-tool change.
- Live revocation verified on a separate one-minute session: Home returned 200 before
  revocation and a 303 sign-in redirect afterward. Its local files were removed; the
  main two-hour session was retained for development work.
- Live Chromium checks using the session passed on Home, WhatsApp list (50 rows),
  and an open chat at 1440×900 and 390×844. Mobile back navigation works; no horizontal
  overflow or page JavaScript errors. Mutation requests were blocked during review.
- Visual follow-up identified substantial blank space below the WhatsApp layout
  on desktop and mobile; resolved by the live layout repairs recorded above.
  Original private screenshots are under `.dev/agent-live-*.png`.

## Suggestions release — 2026-09-26

Production revision: `c89c164` (previous: `6c762e3`). Migration `0008` adds
workspace-scoped conversation reviews, suggestions and evidence references. See
[suggestions](suggestions.md) for the implementation and lifecycle.

- New `/suggestions` inbox: replies, promises and follow-ups from recent one-to-one
  WhatsApp chats; evidence/context, copyable editable drafts, Done/dismissal reasons,
  snooze, history and per-conversation pause/resume.
- Change-driven review jobs and deadline revisits use the configured OpenRouter model.
  Stale review/draft results cannot overwrite newer messages or edits; source removal
  invalidates derived text and evidence. Drafting/copying never sends a message.
- Final targeted checks: **22 passed**, including desktop/mobile Chromium flows.
  Broader suite: **187 passed, 22 failed** before the final added edge-case tests.
  The failures match the documented legacy AI-key tests: 20 non-browser failures
  reproduce on unchanged `6c762e3`; two browser failures still expect removed key fields.
  Connector tests: **27 passed** locally and in the production Node 22 image.
- Encrypted pre-release backup completed: `moresocial-20260926T133440Z.tar.enc`.
  Standard `deploy/release.sh` completed; public health/readiness passed, web and worker
  run `c89c164`, schema is at `0008`, and all three inbox views render with live data.
  Initial reconciliation discovered 36 eligible conversations. The first live model review
  completed successfully; subsequent reviews are queued/running with no terminal failures
  observed at verification.
- Rollback: `sudo /srv/moresocial/src/deploy/rollback.sh 6c762e3`; keep the additive
  tables and user data. No shared proxy changes were needed.

## Voyage response identity fix — 2026-09-26

Production revision after this release: `1585035` (previous: `1f753c0`). Operation
`876cc6ebfa91c61019f1dfe196103133` confirmed the reasoning fix: DeepSeek finished
normally with 820 output tokens. The initial embedding request then returned HTTP 200,
1024 dimensions, and native model name `voyage-4`, which exact catalog matching rejected.

- Added an embedding-only response alias scoped to selected `voyageai/voyage-4` and
  canonical `voyageai/voyage-4-20260727`, shared by setup and runtime. Requests and saved
  vector-space identity remain unchanged. Other models/revisions remain rejected.
- 37 admin/OpenRouter/diagnostics tests passed, including native/native and mixed
  response names, save/reload, 1024-dimensional vectors and negative identity cases.
  Isolated test logging quotas to prevent events being suppressed by earlier tests.
- Standard release script completed; public health/readiness and live identity checks
  passed. Disposable test database and SSH tunnel removed. No schema changes.
- Operator still needs to run Test and save with the key: neither failed attempt
  saved it. No paid provider calls were made during this deployment. (Resolved later:
  AI generation and embeddings were in use by the Suggestions release above.)

## OpenRouter setup truncation fix — 2026-09-26

Production revision after this release: `1f753c0` (previous: `41c6966`). The reasoning setup probe
now permits 4,096 output tokens and requests low effort when the catalog advertises
reasoning support. Truncation has a specific explanation; provider and setup events
record the token ceiling and finish reason. Saved runtime effort preferences are unchanged.

- 33 targeted admin, OpenRouter and diagnostics tests passed, including truncation,
  preserved settings, catalog capability handling and secret-safe metadata.
- Tests used a disposable VPS PostgreSQL container through an SSH tunnel because
  local sandbox restrictions prevent PostgreSQL startup. Container and tunnel removed.
- Deployed with `deploy/release.sh`; public health and readiness passed. Web and worker
  run the new image; the live probe constant is 4096. No schema changes in this fix.
- No valid OpenRouter key is saved, so a real DeepSeek/Voyage setup test remains for
  the operator through Admin. No paid provider requests were made during deployment.
  (Resolved later; see the Voyage response identity fix above.)

## Diagnostics release — 2026-09-26 (branch `diagnostics`)

Implements the archived [diagnostics plan](archive/development/diagnostics-plan.md), deployed in
the plan's order: `e373a49` (setup instrumentation and identity fix), `945f57d` (correlation and
boundary events), `41c6966` (persistent store, retention, alerts). Revision after this release:
`41c6966`; no schema migration.

- Verified live: catalog maps `voyageai/voyage-4` to `voyageai/voyage-4-20260727`; a
  deliberately invalid key stops at `reasoning_probe` (401) with every stage under one
  operation ID and no key or body logged; the public app ignores client request IDs; a
  connector sync's operation ID appears on web's ingest events; a 404 reference stayed
  searchable with `logs.py find` after web and worker were force-recreated; the hourly
  prune/check unit ran with no alerts; scoped DEBUG reached the worker and expired.
- Connector upgraded in place twice (image, then diagnostics mount); session kept.
- **Pending at the time (resolved; see the Voyage response identity fix and later
  releases):** no OpenRouter key is saved. Run Admin **Test and save** once
  with the real key; the page shows a reference. `logs.py find <reference>` then shows
  the actual `returned_model` of both embedding probes, which confirms the identity policy
  against real Voyage responses. If it still fails, the stage event names all three models.
- Legacy `ai_provider=openai` and Anthropic/OpenAI key rows remain in `operator_settings`
  (inert; superseded by the first successful OpenRouter save).
- Pre-existing test failures unchanged: 22 legacy user-key/Anthropic tests
  (`test_ai_keys.py`, one `test_ai_budget.py`, two browser key tests, one foundation config
  test) describe the removed pre-OpenRouter features.

Synthetic-provider tests do **not** prove live Google, WhatsApp or AI integration. The
release is **deployed** at https://1f517.com/moresocial/. Remaining interactive checks are
tracked in [Pending live checks and operator actions](#pending-live-checks-and-operator-actions).

## Production deployment — 2026-09-25

- Application revision deployed: `c921458b7ece3329e41ef7390dd4bf171ef062f5`.
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

## Review fixes completed — 2026-09-25

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

The original failing probes were promoted into these permanent regressions. Run them with
`uv run --frozen pytest -o addopts='' -q tests/test_review_fixes.py` (the former
`docs/review-probes.py` forwarding module was removed on 2026-09-27). The fix verification
itself used no live credentials; the subsequent deployment is recorded above.

## Events plan E0 — budget leak, failure logging, pacing (2026-09-25)

From the archived [events plan](archive/development/events-execution-plan.md) §3, found in the
production review. The local token caps involved here were removed later (`81634c4`,
migration `0006`); the reservation release, failure logging and rate-limit pacing remain:

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

## Production deployment — AI keys and admin page, 2026-09-25 (personal keys since retired)

- Revision deployed: `a66135449b826d2a43a0f07721c974a6cebae07a` (previous `c921458`), deployed
  from a local Git bundle with `release.sh`; migration head `0003`. Web healthy, worker
  running, no errors in logs after start.
- `runtime.env` additions (backup at `/srv/moresocial/runtime.env.before-a661354`):
  `USER_AI_KEYS=anthropic,openai`, `ANTHROPIC_MODEL=claude-sonnet-5`,
  `OPENAI_MODEL=gpt-5.6-terra`, `ADMIN_EMAILS=errslima@gmail.com`. `AI_PROVIDER=none` is
  unchanged: no global provider or key is set yet, so `/ready` reports
  `ai_generation: false, ai_embeddings: false` until the admin page is used.
- Public checks: `/` 200, `/enzosocial/` 303, `/quorum-of-clones/` 200 (unchanged),
  `/moresocial/` 200, `/moresocial/health` 200, `/moresocial/admin` 303 when signed out.
- Pending at the time, now obsolete: sign in as the admin, set the global provider and keys;
  real-key live checks. The OpenRouter Admin configuration replaced this provider/key model.

## User AI API keys — implemented 2026-09-25 (retired)

Implements the archived [API-key execution plan](archive/development/api-keys-execution-plan.md)
milestones K0–K5 (deployed above). Personal keys and the direct providers were later retired,
so its real-key live checks are obsolete.

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

Pending live checks at the time (obsolete since the feature was retired):

1. Admin page: sign in as `errslima@gmail.com`, save the OpenAI (and optionally Voyage)
   key, select the global provider; `/ready` should then report `ai_generation: true`.
2. User keys: add a real Anthropic key, ask a question, see a cited answer and the usage
   in that account's console; add a revoked key and see the refusal; remove the key and
   confirm fallback. Repeat for OpenAI.

## Milestones (first release, 2026-09-25)

Historical evidence. Several of these tests have since changed with the retired manual
gathering workflow, personal keys and direct providers.

| Milestone | State | Evidence |
| --- | --- | --- |
| M0 foundation | done | Tree, pinned deps (`uv.lock`, `deploy/requirements.lock` with hashes, `connector/package-lock.json`), Alembic `0001` (migrate/check/downgrade/upgrade verified), config validation, `/health`, `/ready`, `.dockerignore`, synthetic two-user fixture (`app/synthetic_data.py`), `tests/test_foundation.py` |
| M1 Google onboarding | done (synthetic) | `tests/test_auth.py`: full/partial/cancelled consent, replayed + expired state, browser binding, bad nonce/audience/unverified email, allowlist before storing credentials, missing refresh token (kept vs first-time), concurrent flows/tabs, reconnect with same subject only, revoked grant, logout, CSRF + Origin, two-user isolation |
| M2 Google sync | done (synthetic) | `tests/test_google_sync.py`: bounds and inert HTML, idempotent repeat polls (no reprocessing), resume from saved page after failure, lease recovery after a crashed worker, Gmail history add/delete, invalid cursor → bounded resync, Calendar incremental + 410, truncation without invented cursor, partial consent, revocation stops requests, throttling backoff, disconnect/revoke/delete data, source browsing |
| M3 WhatsApp | done (synthetic) | `tests/test_whatsapp.py` (scoped keys, payload workspace ignored, owner-only no-store QR, dedupe/edit/revoke, bounds, group authors, no name merges, exclusions, cap, disconnect/delete); provisioner with fake Docker (separate mounts/networks/keys, no ports or Docker socket, restart keeps session, no deletion when the API fails or reports an empty DB); Node `connector/*.test.js` (19 tests: auth, no send route, history bounds, durable queue, normalization) |
| M4 memory/answers | done (synthetic) | `tests/test_memory.py`: sourced claims, conflicting locations kept distinguishable, workspace-scoped retrieval and citations, prompt-injection fixture (no tools, no cross-user evidence), server-side citation validation, deletion wins extraction race, exclusion invalidates answers and survives resync, rejections/edits authoritative, per-workspace + global budgets with reconciliation, bounded failure on invalid output, reindex on embedding-model change |
| M5 workflow UI | done (synthetic) | `tests/test_workflow.py` (all screens, gathering flow with calendar conflicts, drafts, rewrite, manual edit, RSVP suggestion needing confirmation, next steps, About me, manual link/unlink, account deletion); `tests/test_browser.py` Playwright at 1280px and 390px through the prefix proxy: sign-in, QR pairing, evidence, claim fix, gathering, draft + real clipboard copy, RSVP, cited answer, no horizontal scroll, no provider writes |
| M6 deployment packaging | deployed; interactive provider checks pending | `deploy/` (Dockerfile, compose, db init, Caddy fragment, provisioner + systemd, backup/restore, release/rollback), `docs/operator-setup.md`, `.github/workflows/ci.yml`, `scripts/test.sh`, `scripts/dev.sh`, `scripts/seed_demo.py`; `tests/test_backup_restore.py` (dump → encrypt → restore into a disposable DB → head, counts, grants decrypt, vector query) |

## Original implementation verification (before review fixes, 2026-09-25)

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

## Decisions and deviations (2026-09-25, partly superseded)

The AI-provider decisions below describe retired implementations; current AI is
OpenRouter-only (see [architecture](architecture.md)).

User API keys (deviations from the archived `api-keys-execution-plan.md`):

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
  (Superseded 2026-09-26: explicit WhatsApp sending added a pinned `connector/patch-library.cjs`.)
- Static assets are served by a small whitelisted route instead of Starlette `StaticFiles`,
  which resolves paths wrongly when `root_path` is set and the proxy has already stripped the prefix.
- Calendar events first seen already cancelled are never imported. Later cancellations delete
  the source's text and derived memory.
- Rebuild/embedding/extraction jobs are reconciled every 10 minutes (`reconcile_ai`), which also
  performs reindexing after an embedding-model change.

## External dependencies and pending live checks (2026-09-25, superseded)

First-release snapshot. The current list is
[Pending live checks and operator actions](#pending-live-checks-and-operator-actions).

| Check | State | Who |
| --- | --- | --- |
| Google APIs enabled, consent scopes/audience, test user | Reported done by the operator; not independently verified | operator |
| Real Google consent + bounded first sync | **pending** | operator after deploy |
| Real WhatsApp QR pairing, history sync, restart keeps session | **pending** | user pairs phone |
| Anthropic + Voyage keys; live cited-answer and extraction quality evaluation | **pending**: no keys available (obsolete: replaced by OpenRouter) | operator |
| Docker image builds, Compose stack, provisioner with real Docker | **passed** build/start; actual paired connector lifecycle pending | deployed |
| VPS rollout (ports, subnet, Caddy route, existing apps unaffected) | **passed** | deployed |
| First backup + scratch restore on the host | **passed**; scratch resources removed | deployed |

Deployed revision: `c921458b7ece3329e41ef7390dd4bf171ef062f5`. Rollback target: none (first deployment); removal procedure is in the operator runbook.
