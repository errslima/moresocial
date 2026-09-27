# Moresocial: reliable sync status and a proactive drafting assistant

Status: approved product direction; implementation not started by this document.
Prepared: 2026-09-27. Work in the standalone `moresocial/` repository.

## 1. Assignment and non-negotiable boundary

Implement this plan in order, using the existing application architecture. Deliver tested
code, migrations, updated documentation and a deployment-ready handoff. This document is
not an instruction to deploy, change production records or contact anyone. Later explicit
user instructions can authorize deployment. Do not copy old deployment authorizations
from archived plans.

**Moresocial must never send messages unattended.** The user explicitly confirmed:

> I never want moresocial to send messages unattended. so the scope remains taking the
> initiative to prepare messages, nothing more.

The assistant may independently identify opportunities, prepare drafts, and refresh its
understanding. Only the user's existing **Send message** action may transmit the exact
displayed message to its displayed recipient. Do not add auto-send settings, delayed send,
bulk approval, background sends, or a future auto-send milestone. Persona instructions
cannot grant sending permission. Draft generation, preview, copying, opening a conversation,
snoozing, Done and background review must never send.

Use judgment for routine implementation details. Record deviations and their reasons in
this file. Do not stop after a proposal, partial scaffold or untested migration. Avoid
unrelated refactoring, new agent frameworks and provider changes. Work serially; this plan
does not require additional agents or multiple-model review loops.

## 2. Outcomes

1. A healthy WhatsApp connector no longer produces a persistent false sync warning.
2. Suggestions understand linked Events and use concrete places, dates and participants.
   Example: “Meet Mickaël at Aloha on Sunday”, with a visible link to the relevant event,
   rather than “Meet Mickaël there on Sunday”. Use the event's actual current title; the
   example “Lasergamen at Aloha” is not an instruction to rename production data.
3. One editable assistant persona controls communication style and initiative in both
   ordinary suggestion drafts and proactive drafts.
4. Before events, the assistant prepares useful messages for confirmed participants:
   reminders, a friendly anticipation message, or a request for an actually missing detail.
5. Outside events, it prepares check-ins and relevant updates about the user, based on
   relationship preferences and information explicitly marked as shareable.
6. Everything appears in the existing Suggestions workflow, with explanations, evidence,
   editable drafts, pause, dismissal and snooze. No second task inbox.

## 3. Read first and preserve

Current documentation:

- [Architecture](architecture.md), [Suggestions](suggestions.md), [Events](events.md).
- [Product definition](product-definition.md), [implementation status](implementation-status.md).
- [Operator runbook](operator-setup.md), especially migration/readiness/rollback behavior.

Historical rationale, not current implementation instructions:

- [EnzoSocial product description](../../enzosocial/PROJECT-DESCRIPTION.md), sections 4–7:
  engagements spanning channels, relationship preferences, one inbox and a stable secretary
  role supported by persistent storage and bounded model calls.
- [Moresocial original scope](archive/development/EXECUTION-PLAN.md), sections 1 and 3.
  Automatic sending and a general autonomous runtime were deferred there; the user's newer
  instruction above rules out unattended sending entirely.

Relevant code map:

| Area | Files |
| --- | --- |
| Sync warning and status | `app/suggestion_pages.py`, `app/whatsapp.py`, `app/internal.py`, `connector/index.js`, `deploy/provisioner.py` |
| Suggestion lifecycle | `app/suggestions.py`, `app/suggestion_pages.py`, `app/templates/suggestions.html`, `app/templates/suggestion.html` |
| Event facts and identity | `app/events.py`, `app/event_identity.py`, `app/event_resolver.py` |
| People, self-profile, diary | `app/pages.py`, `app/diary.py`, `app/next_steps.py`, `app/models.py` |
| Scheduling and AI | `worker/main.py`, `app/jobs.py`, `app/ai.py`, `app/synthetic.py` |
| Conversation targets and sending | `app/conversations.py`, `app/whatsapp_pages.py`, `app/whatsapp_send.py` |
| Schema and test fixtures | `migrations/versions/`, `tests/conftest.py`, `scripts/test.sh` |

Preserve workspace scoping and composite foreign keys, source exclusion/deletion,
corrections, reversible event grouping, stable suggestion identities, user edits, CSRF,
job leases, rate-limit deferral and usage accounting. Generation uses the existing shared
OpenRouter adapter. No Redis, Celery, vector service or continuously running model session.

## 4. Investigation findings to reproduce

### 4.1 False warning

`app/suggestion_pages.py` currently warns when no connector exists, `observed != 'ready'`,
or `updated_at` is older than ten minutes. `updated_at` records row changes, not a health
heartbeat. Ingestion does not refresh it; the provisioner does not continuously report
healthy connector state. Connections can refresh cached status when visited.

Read-only production observation at 2026-09-27 01:10 UTC:

- Stored state `ready`, desired state `running`, stored row age about 42 minutes.
- Live state `ready`, no error, zero pending packets.
- Last sync completed about 20 seconds earlier; 50 of 50 selected history chats synced.

This establishes a false warning at that instant, not a guarantee of complete WhatsApp
history. The connector's periodic history sweep is every 15 minutes. Its `last_sync`
currently advances even after partial failures; it must not be relabeled “last fully
successful sync” without changing that contract.

### 4.2 Missing event/persona input

The suggestion snapshot currently supplies at most 80 recent chat messages, older open
anchors, relationship notes and previous suggestions. It does not supply structured Events
or an assistant persona. Its follow-up prompt intentionally excludes general check-ins.
Drafting uses a fixed “conversation's language and tone” instruction.

The live Mickaël suggestion shared source evidence with a grouped event whose representative
was “Lasergame – 2 rondes”, location “Aloha Amsterdam”. Checking only member titles would
miss this association: follow evidence through original event identities to their current
representative. The stored event start was 16:15 Amsterdam time, whereas the suggestion
mentioned a 16:00–17:00 meeting window. Do not assume an arrival window and activity start
are the same fact or overwrite one with the other.

## 5. Implementation defaults

These are concrete starting defaults, not claims that the user selected every numerical
value. Keep constants centralized and configurable where a user-facing preference exists.

### 5.1 Persona and relationship controls

Add an **Assistant** settings page reachable from the existing navigation. It edits one
workspace-owned persona, with a short preview and the clear statement “Prepares drafts;
messages are sent only when you press Send message.”

- Free-text instructions, bounded to 4,000 characters.
- Style: warmth, directness, humour, brevity, emoji use, and language (`match conversation`
  by default, or a preferred language). Use simple controls and sensible defaults.
- A few optional examples of preferred and unwanted wording, bounded in total.
- Initiative: low / balanced / high; default balanced. This changes draft selection and
  frequency, never permissions or evidence requirements.
- Separate switches for event preparation and relationship check-ins; enabled for the new
  drafting feature by default, plus a single pause control. Existing per-chat pauses win.
- Background preparation window: 08:00–23:00 in the workspace timezone. Manual requests
  may run outside it. No catch-up burst after downtime.

On People, add explicit preferences: check-ins enabled, desired contact interval (default
30 days), and preferred style/boundaries. Initially consider pinned contacts or contacts
whose check-ins the user explicitly enabled; do not turn the full imported address book
into a reminder list. Existing relationship notes remain relevant.

On Events, allow pausing message preparation for that event. Existing event dismissal also
suppresses preparation. Provide **Prepare messages** to request a bounded manual review.

Keep persona configuration separate from facts about the user in `SelfProfile`. Explicit
settings are user-authored instructions; imported chat/email/diary text is untrusted data
and cannot override application rules. A contact's boundaries override stylistic initiative.

### 5.2 Personal updates

Add an explicit way on About me to create/select a short **Update I can share**, with an
optional expiry and audience (eligible contacts or selected people). Default ordinary
diary entries and observations to private: do not automatically share the entire diary,
psychological observations, another person's private information or an inferred life event.
Store an audience decision and revision, not just a phrase hidden in a prompt.

Pass only applicable, unexpired updates into draft context. Keep a record of which update
was already proposed to which person; rewording must not create repeated suggestions.
Never equate drafting, copying or Done with delivery. Record an explicit successful send
receipt when available; unknown outcomes remain unknown.

### 5.3 Bounded behavior

- Run a cheap eligibility scan every 30 minutes during the preparation window, using the
  existing durable job queue. Call AI only for changed context or a newly due checkpoint.
- At most five newly prepared proactive drafts per workspace per day by default; this is
  a product attention limit, not a new provider token budget. Manual preparation can
  request another bounded batch and must still respect pauses and deduplication.
- At most one open proactive message per recipient. Combine an event reminder and an
  anticipation message when they serve the same purpose. Ordinary urgent replies/promises
  retain their current behavior and are not hidden by this new limit.
- Event candidates: confirmed attendance, known future event within seven days, useful
  checkpoint at roughly 24 hours before start. If first discovered closer to start, one
  current candidate is sufficient. An unresolved logistical question may justify an
  earlier candidate. Do not manufacture required replies, arrival times or obligations.
- Relationship candidates: chosen contact whose interval elapsed, considering both sent
  and received included direct messages, plus existing open work and recent dismissals.
  Silence is not evidence of feelings or neglect. A fresh reply cancels an obsolete
  check-in; it does not automatically resolve an unrelated promise.
- Missing/ambiguous recipient identity yields a visible explanation and no prepared send
  target. V1 uses existing one-to-one WhatsApp conversations only. Group/email/Calendar
  evidence may inform an event; this adds no group outreach or Gmail/Calendar writes.

## 6. Data and context contracts

### 6.1 Extend Suggestions without inventing message anchors

Use the existing `Suggestion` and `SuggestionReview` workflow for direct-recipient actions.
Keep current chat review behavior; add a distinct proactive review producer/job and shared
context/publication helpers. Do not weaken the old reply/promise direction validation to
squeeze proactive work into an existing kind.

Add workspace-owned tables for persona settings, per-person preparation preferences and
shareable updates. Include revisions and deletion cascades. Add durable proactive review
state with scope, next checkpoint, input fingerprint and last result; use the existing
`Job` table for execution rather than a second queue.

Extend suggestions with:

- A producer (`conversation` or `proactive`) and stable action key.
- Nullable `anchor_id` for proactive actions only. Preserve legacy anchors and uniqueness;
  add database checks enforcing required anchors for legacy reply/promise/follow-up items.
- Optional primary original event identity, with a scoped foreign key; resolve its current
  representative when displaying/contextualizing. Do not persist only a representative
  that can become wrong after a split.
- An exact destination account/conversation for proactive actions, resolved from included
  stored messages and provider identities. Never infer a phone number from a name. Keep
  the current `review_id` for pause/disposition coordination, and enforce its conversation
  matches the target. Where existing review scoping spans more than one provider account,
  filter input to the selected account and reject ambiguous targets rather than mix them.
- An input fingerprint covering sources, event projection/grouping, persona, relationship
  settings, audience/update revisions and destination identity.
- Draft provenance sufficient to protect edited drafts and track their generation context.
- Structured rationale for policy-based actions: enabled contact rhythm, due checkpoint or
  selected update. Policy is not evidence that another person requested a message.

Retain `SuggestionSource` for actual source citations and add typed links for settings and
shareable updates where needed. A check-in can have policy rationale without a message
anchor; validate it accordingly. Do not create synthetic chat sources to satisfy a foreign
key. Refactor callers that currently require `anchor_id` or nonempty message evidence,
particularly `evidence()`, `draft()`, detail rendering and `suggestion_target()`.

Action identity is server-created, not a free model string. Legacy actions retain their
existing identity. Proactive identity consists of recipient + purpose + event identity or
contact-cycle/update identity. An event reschedule updates the existing action; it does
not recreate a dismissed reminder. A new relationship cycle begins only after its interval
from the latest relevant contact or user disposition. Settings edits do not start cycles.
Keep terminal action identities to suppress repetition.

### 6.2 Shared context builder

Build one bounded, workspace-scoped context service used by ordinary review, proactive
review and drafting. Suggested module: `app/assistant_context.py`.

1. Resolve person and exact destination; load included direct messages, relationship notes
   and applicable user settings. Preserve support for older open anchors.
2. Retrieve event candidates through source overlap first, across all original members.
   Participant/time/activity compatibility may retrieve additional candidates; it alone
   must not prove association. Ambiguity stays unlinked and visible for correction.
3. Use the existing canonical event projection and field provenance. Respect corrections,
   conflicts, cancellations, dismissals and latest explicit attendance. Do not create a
   competing event summary or copy the old manual gathering subsystem.
4. Include relevant venue/schedule facts and their source labels, the recipient's attendance,
   unresolved logistics and applicable shareable updates. Do not expose unrelated guests'
   private conversation text in an outgoing draft.
5. Produce bounded model input and a complete fingerprint. Reuse current conversation
   bounds; cap supplemental event evidence at 40 messages / 24,000 characters and five
   candidate events. Deterministic selection must keep field provenance and cite only
   supplied labels. Indicate omitted coverage. No per-candidate model call fan-out.

Keep namespaces distinct for chat/event/update citations. Server-side schema validation
rejects invented IDs, foreign events, unavailable sources, unsupported recipients and
unshareable updates. Dates use the workspace timezone and source timestamps. Separate an
explicit factual deadline from a policy-selected reminder/checkpoint.

### 6.3 Invalidation and concurrency

Generation occurs outside database transactions. On publication, recheck the job lease,
review revision and full input fingerprint. Lock the appropriate review/action records.
Source changes, event grouping/splits, corrections, attendee changes, persona/settings
changes, update revocation, per-chat pause and user dispositions invalidate affected work.
Use targeted hooks plus periodic reconciliation to recover missed scheduling.

Preserve user-edited text on ordinary context/style changes and mark it stale; never
silently replace it. Require review before it can be transferred as a current suggestion
draft. For exclusion/deletion/revocation, remove dependent derived text and copies as the
existing privacy behavior requires; retaining an edited draft must not retain excluded
content. Update any composer-prefill freshness checks as well as the suggestion detail.

Ordinary chat review must not accidentally revive or resolve proactive rows. Audit the
current “clear stale if source versions match” path: unchanged messages alone are no longer
enough when event or persona context changed. Conversely, proactive review must preserve
legacy actions and their dispositions. A new message triggers reevaluation, not automatic
completion of every action for that person.

## 7. Milestones

Complete each milestone with focused tests and a short dated progress entry below.

### M0 — Baseline and migration design

- Read the files above; inspect `git status` and applicable repository instructions. Do
  not overwrite unrelated work. The current schema was `0012` when this plan was written;
  inspect the actual migration head before numbering new migrations.
- Record baseline test results, current revision and a concise final schema map. Use the
  specified design unless code inspection shows a concrete incompatibility; document it.
- Build synthetic fixtures for the Aloha example, including chat evidence on a grouped
  member, booking venue on another member, confirmed attendance and distinct arrival/start
  times. Use fabricated people/messages, not copied private production chat content.

Acceptance: baseline known, migration/backfill approach explicit, no production writes.

### M1 — Accurate WhatsApp health

Preferred implementation: a lightweight authenticated connector heartbeat to the internal
API every 60 seconds and after state changes. This avoids making the worker join each
connector's private network or making every Suggestions render block on a live probe.

- Add explicit receipt-time `health_checked_at` and reported health fields. Server time
  determines freshness; receipt authentication scopes the workspace. Use the connector's
  existing scoped credential and private listener; reject oversized/malformed packets.
- Record state, pending queue count, sync-in-progress, last completed sweep and error/
  partial outcome. A heartbeat must not generate AI work or clear ingestion errors.
- A failed heartbeat must be handled without crashing the connector or blocking message
  ingestion. No new secrets in logs, payload echoes, or QR/account data in diagnostics.
- Use a shared pure health classifier in Suggestions and Connections: fresh ready/healthy
  is normal; explicit disconnect/auth failure is actionable; stale/missing heartbeat means
  “status unavailable”; pending/error/partial ingestion means “sync delayed/incomplete”.
- Default freshness grace: three minutes for a 60-second heartbeat. A quiet chat is not
  stale sync. Do not apply this three-minute rule to the 15-minute history sweep timestamp.
- Handle an ongoing initial history sweep separately. Show last check and useful detail;
  do not claim that ready means all historical messages are available.
- Old connectors without heartbeats show unknown until upgraded, not a fabricated
  disconnect. Synthetic connectors/test fixtures must support the new health contract.

Acceptance tests: unchanged old `updated_at` + fresh ready heartbeat has no warning;
expired heartbeat reports unknown; disconnect reports disconnected; pending/error reports
delay; healthy idle and 15-minute sweep timing do not falsely warn; foreign/invalid
heartbeat refused; heartbeat failure does not stop ingest. Run connector tests too.

### M2 — Event-aware existing suggestions

- Implement the event context/linking subset, schema changes and shared context builder.
- Feed structured event context into existing suggestion review and drafting. Require
  concrete place names when evidence supports them; keep uncertainty when it does not.
- Show a linked event title on list/detail cards and event evidence in “Why this appeared”.
  Allow correcting a wrong association among eligible events; remember the correction.
- Preserve distinct arrival and activity times. Prefer existing field provenance when
  facts conflict; show “needs checking” rather than invent a reconciliation.
- Refresh affected actions on event updates and source invalidation. Deduplicate actions
  across event grouping and ensure splitting/undo re-evaluates associations.

Acceptance: the synthetic Mickaël card names Aloha and links the correct event; another
same-day event is not guessed; grouped-member evidence works; merge/split, cancellation,
reschedule, conflict, user correction and source exclusion preserve correct behavior.

### M3 — Persona, preferences and shareable updates

- Implement settings tables, forms, validation, revisions and pause controls from §5.1.
- Implement shareable update creation/selection, audience, expiry and revocation from §5.2.
- Apply persona to existing on-demand drafts as well as the new pipeline. Offer a small
  explicit preview action using the real adapter and ordinary usage accounting; saving or
  opening settings need not call AI. Existing edited drafts remain intact and visibly stale.
- Persona can affect wording and initiative; it cannot override facts, contact boundaries,
  source exclusion, workspace isolation or the explicit-send requirement.

Acceptance: different styles produce different validated prompt input and synthetic draft
results; edits survive settings changes; another workspace cannot read/update settings;
private/expired/wrong-audience updates do not enter the model payload; revocation clears
dependent content. Imported instructions cannot become persona settings.

### M4 — Proactive event preparation

- Add the proactive producer and durable checkpoint state. Reuse shared context, AI
  generation, validation and publication. The event/person eligibility scan is deterministic;
  AI chooses useful wording/actions from supplied eligible candidates.
- Prepare an editable draft as part of proactive generation, rather than requiring a
  separate “Draft a reply” click. Keep on-demand drafting for ordinary legacy suggestions.
- Implement bounded scheduled and manual event preparation. For confirmed participants,
  propose a useful reminder/anticipation message or evidenced logistical question.
- Suppress obsolete/cancelled/past events, paused chats/events, declined participants,
  existing equivalent actions and uncertain destinations. Limit preparation frequency.
- Show event context, recipient, proposed draft, why now and preparation status. Unchanged
  snapshots skip AI. Failed/deferred work stays visible and retryable using existing jobs.

Acceptance: confirmed attendee gets one prepared draft; tentative/declined/silent group
member is not treated as confirmed; no duplicate after rerun/restart/merge; reschedule
updates context; edited draft protected; concurrent reply/cancel/pause defeats stale
publication; scheduled runs, retries and manual preparation make **zero send calls**.

### M5 — Relationship check-ins and user updates

- Extend the same producer to eligible contacts and shareable updates. Include older direct
  context where relevant: inactive contacts cannot disappear merely because the legacy
  suggestion snapshot only considers the last 30 days.
- Consider outgoing as well as incoming messages and exclude group traffic, deleted or
  excluded sources and unrelated account history from contact-recency calculations.
- Use user intentions, notes and shared interests for natural drafts. Personal updates are
  optional; do not force an update into every check-in or invent news when none is supplied.
- Implement stable contact cycles, cooldown/disposition handling and update-per-person
  deduplication. Avoid simultaneous event and generic catch-up drafts for one recipient.
- Replace or coordinate Home's deterministic catch-up reminders so the same person does
  not generate competing reminders. Retain the small, optional nature of the Home list.

Acceptance: due chosen contact gets a draft; recent contact/space preference/pause suppresses
it; a new message during generation discards stale output; user dispositions prevent
reworded duplicates; approved update reaches only its audience; unapproved diary text is
absent; no remembered private assessment appears in a draft; zero background send calls.

### M6 — Integration, verification and handoff

- Keep one Suggestions inbox and its current pagination, snooze/history, explanations and
  conversation handoff. New kinds need plain labels such as “Event reminder”, “Check in”
  and “Share an update”. Show event links on cards; elaborate grouped inbox redesign is
  not required for this release.
- Ensure the exact account/conversation and current draft reach the composer. Only its
  explicit Send action sends; retain CSRF, duplicate protection and no automatic resend
  after an uncertain outcome. A successful message is not proof an event task is complete.
- Add targeted browser checks at desktop/mobile widths for settings, event-linked cards,
  prepared draft editing, pause and composer handoff. Avoid tests that only mirror helpers.
- Update architecture, Suggestions, Events, product definition, operator guidance and
  implementation status to distinguish delivered behavior from future ideas.
- Run the full verification entry point once after focused suites pass. Fix regressions;
  record genuine unrelated baseline failures or unavailable runtimes accurately. Never
  call skipped checks passed.
- Document migration order, connector/web compatibility and rollback. Because new
  suggestions may have nullable anchors/new kinds, old application code is not necessarily
  rollback-compatible. Prefer a forward fix or disable proactive scheduling; do not drop
  new user data or restore an old database over newer messages to roll back.

Acceptance: all required behaviors below verified and handoff complete. Local code/tests
are not evidence of deployment or live recommendation quality. No production sending test.

## 8. Verification matrix and commands

| Contract | Minimum meaningful verification |
| --- | --- |
| Explicit sending only | Spy/mock actual connector send boundary across scheduled/manual review, draft generation/editing, settings preview, copy/open, retries and restart recovery: zero calls. Separate existing explicit-send tests still pass. |
| Event association | Shared evidence across members, distinct same-day events, wrong-link correction, grouping/splitting, field conflicts and changed attendance. |
| Evidence/privacy | Forged labels rejected; foreign workspace rejected; source exclusion, update revocation, private diary content and removed identities handled in input, output and saved drafts. |
| Publication races | New message, event correction, persona change, audience change, user edit/disposition or lease loss during a provider call cannot publish stale results. |
| Lifecycle | Existing replies/promises unchanged; no cross-producer reopening; terminal identities persist; no repeated event action after reschedule; new contact cycle only after interval. |
| Scheduling/cost | Frozen-clock boundary tests, DST/local window, downtime recovery, no-change skips, daily draft limit, bounded candidate continuation, 429/configuration deferral and job deduplication. |
| Migration | Upgrade from current head preserves all existing suggestions/drafts/dispositions; foreign keys/checks enforce new invariants; rollback compatibility documented honestly. |
| UI | Desktop/mobile controls, event links, clear uncertainty, editable drafts, stale review, normal inbox pagination and explicit composer sending. |

Run commands from the `moresocial/` repository, adjusting new test filenames as implemented:

```bash
uv run --frozen pytest tests/test_whatsapp.py tests/test_suggestions.py tests/test_whatsapp_send.py
uv run --frozen pytest tests/test_events.py tests/test_event_identity.py tests/test_migrations.py
uv run --frozen pytest tests/test_assistant_context.py tests/test_social_assistant.py
(cd connector && node --test)
bash scripts/test.sh
```

Tests use synthetic providers and the existing PostgreSQL fixture. Do not use real
contacts, credentials or paid generation merely to make synthetic checks pass. For model
quality, add a small synthetic evaluation set: warm/concise versus restrained/direct style;
Aloha event context; an unresolved detail; a quiet contact; shareable versus private news.
Automated tests validate structure, context and constraints; report real-language quality
as pending unless it was actually evaluated. Any later live evaluation must prepare drafts
only and must never send a test message to a real person.

## 9. Completion checklist

- [x] M0 baseline and schema map recorded.
- [x] M1 accurate sync health, compatible heartbeat rollout and tests.
- [x] M2 event-aware suggestions/drafts, source-backed links and invalidation.
- [x] M3 editable persona, preferences, shareable updates and revision checks.
- [x] M4 proactive event drafts with bounded scheduling and deduplication.
- [x] M5 proactive relationship/update drafts with privacy and cadence controls.
- [x] M6 integrated UI, full verification, documentation and rollout/rollback handoff
  (implemented and verified locally; **not deployed**).
- [x] No unattended-send code path, setting, queue or permission introduced.
- [x] Final report lists changed files, migrations, test results, limitations and whether
  anything was deployed (see the final log entry).

## 10. Implementation log

Append a concise dated entry after each milestone: changes, tests/results, remaining work
and justified deviations. Keep this as the resume point for subsequent implementation turns.

2026-09-27: Execution plan prepared from repository inspection and read-only production
diagnostics. No implementation changes, migrations, deployments or message sends performed.

### M0 — 2026-09-27: baseline and migration design

Baseline: branch `whatsapp-sidebar` at `c5c91f9`, clean apart from this untracked plan.
Migration head `0012`. `bash scripts/test.sh` (Node v24 fallback on PATH): **305 Python
passed, 33 connector passed**, compile check passed. No production reads or writes.

Schema map (new migrations; both additive):

- `0013` connector health: `whatsapp_connectors.health_checked_at` (server receipt time)
  and `health` JSONB (validated connector report). Old rows start NULL = "unknown".
- `0014` assistant:
  - `suggestions`: `producer` (`conversation`|`proactive`, backfilled `conversation`),
    `action_key` (NOT NULL, unique per workspace; backfilled in SQL from the legacy
    `(review, anchor, kind)` identity), nullable `anchor_id` with checks that
    reply/promise/follow-up rows and all `conversation` rows keep an anchor,
    `event_id` + `event_link` (`auto`|`user`) primary original event identity (composite
    FK), `target_account`/`target_chat` (required for proactive rows; composite FK
    `(workspace, review_id, target_chat)` → `suggestion_reviews(workspace, id,
    conversation_id)` enforces the review matches the target), `update_id` (composite FK),
    `rationale` JSONB, `input_fingerprint`, `draft_origin`, `draft_fingerprint`.
  - `suggestion_reviews`: extra unique `(workspace_id, id, conversation_id)` for that FK.
  - `events.preparation_paused` (group semantics like dismissal).
  - New workspace-owned tables: `assistant_settings` (one row; persona, style, initiative,
    switches, pause, revision), `person_preferences` (check-ins tri-state, interval,
    style/boundaries, revision), `shareable_updates` + `shareable_update_people`
    (text, audience, expiry, state, revision), `update_proposals` (update × person,
    suggestion, delivery receipt), `proactive_reviews` (durable proactive state per
    candidate identity: scope key, revision, fingerprint, result, next checkpoint).
    All cascade with the workspace and use composite `(workspace_id, id)` foreign keys.

Design decisions from code inspection:

- Events are never deleted by application code (tombstoned as `unsupported`), so
  composite FKs to `events` use CASCADE, which only fires on account deletion.
  Legacy suggestions keep their own event association columns rather than a cascade that
  could delete a reply when an event disappears.
- Draft freshness is computed, not hook-maintained: a draft is current only if its stored
  `draft_fingerprint` equals the fingerprint of the current draft context (messages,
  linked event projection, persona, person preferences, update revisions, destination)
  and the legacy review revision still matches. Pre-upgrade drafts have no fingerprint
  and are therefore shown as needing review once; their text is preserved.
- The proactive producer uses per-candidate `prepare_message` jobs (one recipient per
  model call, so one person's context never enters another person's draft) scheduled by
  a deterministic `proactive_scan` job per workspace and half-hour slot.
- Heartbeats may update `observed` (as the existing Connections probe does) but never
  `account_wid`, `detail` or ingestion state; only heartbeats set `health_checked_at`.
- Synthetic Aloha fixture: fictional Mickaël chat evidence on one grouped member, booking
  email with venue on another, confirmed attendance, 16:00 meeting vs 16:15 booked start,
  plus a same-day dinner with the same person and no source overlap.

### M1 — 2026-09-27: accurate WhatsApp health

- Connector: `connector/health.js` posts a bounded report (state, pending count and age,
  delivery failure, sweep outcome/failed chats/last complete sweep, history counts, error
  code) to `POST /internal/connector/health` every 60 s and ~2 s after a state change, on
  its own request with a 10 s timeout. Failures are recorded (`heartbeat_failed`) and never
  touch the ingest queue; nothing is posted after unlinking. `last_sync` keeps its old
  meaning; `sweep.last_complete_at` is the new "fully successful" timestamp. `queue.js`
  now reports pending age and delivery failure. Dockerfile copies `health.js`.
- Web: migration `0013`; `whatsapp.record_health` validates strictly
  (`app/whatsapp_health.validate`), stamps server receipt time, may update `observed`, and
  never changes `account_wid`, `detail`, sources or jobs. The endpoint reuses the ingest
  key authentication (workspace from the key only), 4 KB limit, 400 on malformed input.
- `whatsapp_health.classify` (pure) drives Suggestions and Connections: fresh ready = no
  warning; >3 min or never reported = "status unavailable"; disconnected/auth failure/
  pairing = actionable; pending ≥60 s, delivery failure or connector error = delayed;
  partial/failed sweep = incomplete; initial sweep = informational; sweep overdue only after
  60 min. The synthetic connector writes a heartbeat when it reports ready.
- Tests: `tests/test_whatsapp_health.py` (21) plus WhatsApp/Suggestions suites: all pass;
  connector suite 40 passed (7 new). Deviation: none. Rollout note: web (0013) first; old
  connectors show "status unavailable" until the new connector image runs.

### M2 — 2026-09-27: event-aware existing suggestions

- Migration `0014` (full assistant schema from M0) and `app/assistant_context.py`: settings
  and preferences readers, exact destination resolution, bounded direct messages, event
  candidates (source overlap across every grouped member first, participation only as a
  labelled possibility, dismissed/legacy/unsupported excluded, max 5), and event context
  with field provenance (E labels, or M labels already in the chat input), max 40 messages /
  24,000 characters, and counted omissions. Other guests' messages are never supplied;
  email/Calendar text only where it establishes schedule, place, title or status.
- Ordinary reviews receive `events`, `event_messages`, `event_coverage` and a language
  preference; the schema allows an optional `event` label restricted to supplied events.
  Publication links deterministically: exactly one group sharing the action's own evidence,
  or the model's choice among several that do. The stored link is the original member whose
  evidence overlaps (follows splits); user corrections (`event_link='user'`, including "not
  related") are never overridden. Reviews publish and resolve only `conversation` rows.
- `input_fingerprint` (evidence versions + linked-event projection) replaces "unchanged
  source versions" as the condition for clearing `stale`; stale open items are returned to
  the model as `needs_update`. `event_identity.refresh` and dismissal/pause call
  `suggestions.events_changed`, which re-links without a model call and schedules one review
  only when a link or projection changed.
- Drafts use one `draft_context` (messages, linked event first, persona, recipient style,
  destination) with a fingerprint. Copy/prefill require `draft_fingerprint` to match; edits
  are never replaced; saving requires the fingerprint shown on the page (409 otherwise).
- UI: event title/date/place on cards and detail, provenance links under "Why this
  appeared", "Possibly related" when ambiguous, and a correction form limited to eligible
  events. Suggestions copy states that nothing is sent until Send message.
- Tests: `tests/test_assistant_context.py` (13, Aloha fixture in
  `tests/assistant_fixtures.py`); full Python suite green. Adjusted existing tests: draft
  save posts the page's context fingerprint; the 0012 retirement test compares values at
  0012 and pre-existing values at head; the legacy inventory creates data at head before
  downgrading and expects code head `0014`.
- Deviation: legacy suggestions link events via their own columns (no separate link table);
  events are never deleted by application code, so CASCADE only applies on account deletion.

### M3 — 2026-09-27: persona, preferences and shareable updates

- `app/assistant.py` + `app/assistant_pages.py`: **Assistant** page (sidebar) with the
  statement "Prepares drafts; messages are sent only when you press Send message.",
  instructions (≤4,000), warmth/directness/humour/length/emoji, language (empty = match the
  conversation), wording examples (≤2,000 total), initiative, event-preparation and
  check-in switches, one pause control, status and an explicit **Preview my style** (one
  accounted call on a fictional scenario, no contact data). Saving or opening calls no model.
- People: check-ins default/on/off (default = pinned), interval (default 30, 1–365),
  style and boundaries (≤1,000), and an explanation when no one-to-one target exists.
- Events: pause/resume preparation for the whole group (like dismissal) and **Prepare
  messages** (bounded manual batch; refused while paused or dismissed).
- About me: **Updates I can share** (≤300 characters, audience "people with check-ins on"
  or selected people, optional expiry, edit, stop sharing). Diary entries and observations
  are never supplied. Revoking or narrowing the audience erases dependent derived text,
  including edited drafts, and takes the action out of view.
- Every settings/preference/update change bumps a revision in draft and proactive
  fingerprints, so drafts become visibly stale and in-flight preparation is discarded;
  edited text is never replaced. Only these CSRF forms write settings.
- Tests: persona/validation/isolation/preview/privacy/revocation cases in
  `tests/test_social_assistant.py`.

### M4 — 2026-09-27: proactive event preparation

- `app/proactive.py`: `proactive_scan` jobs (one per workspace per half-hour slot, local
  08:00–23:00, no replay of missed slots; manual and change-triggered scans have separate
  keys) and one `prepare_message` job per due candidate. Confirmed (`accepted`)
  participants of planned, non-dismissed, non-paused events 1 h–7 days ahead are due 24 h
  before start; a conflicting time (`needs_checking`) makes an `event_question` due earlier
  unless initiative is low. Identity = recipient + purpose + original event; an existing or
  terminal action on any group member keeps its identity (no duplicates after grouping,
  no recreated dismissed reminder after a reschedule).
- The model returns prepare/skip, title, reason, draft and cited M/E labels, the candidate's
  own EV label and optional U label; validation rejects anything else. Publication rechecks
  the lease, state revision, full fingerprint, pauses, terminal identity and one open
  proactive message per recipient (an event may replace an unedited generic draft).
  Unchanged fingerprints skip the model. Deterministic housekeeping closes actions for
  cancelled/past/dismissed/paused events, declined participants and duplicates.
- Suggestions show "Event reminder" / "Ask about a detail", the event link, a draft
  preview and the policy rationale ("your own preference or schedule, not a request").
  Open conversation carries the exact account/chat and the current draft; a confirmed send
  never completes the action. Operator switch: `python -m app.proactive mode on|off|status`.
- Tests: checkpoint timing, confirmed vs tentative/declined, missing destination
  explanation, pauses, reschedule and edit protection, dismissal after merge, cancellation/
  decline, six publication races (reply, cancel, event pause, chat pause, persona, lease),
  DST window/slots/no catch-up/operator off, 429 deferral, invalid output, composer handoff.
  All background paths assert **zero** calls at the connector `/send`, synthetic send and
  send service boundaries.

### M5 — 2026-09-27: relationship check-ins and user updates

- Check-ins for pinned or explicitly enabled contacts with a one-to-one chat, due at
  interval × initiative factor after the later of the last direct message either way
  (included, not deleted, not group, same account) and the last check-in disposition.
  Settings edits never start a cycle. An open reply/promise for the person comes first;
  an upcoming event takes precedence. The proactive context uses the latest 40 direct
  messages regardless of age, so quiet contacts keep their context.
- A fresh message cancels an obsolete check-in (`resolved/contacted`) and discards
  in-flight output. Dismissal starts a new cycle only after the interval.
- Share-update candidates for the update's audience (not at low initiative); a due check-in
  carries applicable updates itself. `update_proposals` records each update × person once;
  only a confirmed send receipt from the composer sets `delivered_at`.
- Home shows prepared check-ins and keeps the simple catch-up only for pinned people the
  assistant cannot prepare for, so one person never gets two competing reminders.
- Tests: due contact with 41-day-old context, six suppression cases, group traffic,
  fresh reply, disposition cycles, audience-only updates, delivery only on confirmed send,
  daily limit (5) plus manual batch, one open message per recipient, producer separation.
  Python suite: **391 passed**; connector 40 passed.
- Deviation: proactive rows carry no `due_at` (the checkpoint is policy, not a deadline);
  they are ordered after dated actions and show the event date through the event link.

### M6 — 2026-09-27: integration, verification and handoff

- UI: one Suggestions inbox with plain labels (Event reminder, Ask about a detail, Check
  in, Share an update), event links and prepared-draft previews on cards; existing
  pagination, snooze, history, explanations and composer handoff unchanged. Composer
  prefill uses the fingerprint check; its Send message form carries the suggestion ID only
  to record a confirmed update delivery. Sidebar gains **Assistant**.
- Browser: `test_assistant_settings_prepared_draft_pause_and_composer` at 1440 and 390 px
  (settings save, event-linked prepared card, draft edit and copy, composer handoff with
  the edited draft, event pause, no overflow, no page errors, zero send calls).
  Screenshots were inspected; none are committed.
- Migration test: legacy suggestions, drafts and dispositions survive a 0013→head round
  trip; backfilled action keys equal `legacy_action_key`; SQL rejects a proactive row
  without target, a wrong producer/kind pair, a target chat that is not the review's, a
  duplicate action key, an automatic event link without event and an unknown producer; the
  downgrade refuses while proactive suggestions or assistant data exist. `rollback.sh`
  refuses pre-0014 images while the 0014 schema is present (tested with stubs).
- Synthetic evaluation set: `tests/test_assistant_eval.py` (style contrast, Aloha context,
  unresolved detail, quiet contact, shareable vs private news);
  `MORESOCIAL_EVAL_DUMP=<dir>` writes each case's system prompt and model input for a later
  draft-only live evaluation. Review found internal event/update UUIDs in the proactive
  prompt's policy facts; they are now fingerprint-only.
- Lock order was normalized to review → suggestion → proactive state in event hooks and
  proactive publication, matching chat-review publication and user dispositions.
- Docs updated: architecture, suggestions, events, connections, product definition,
  operator runbook (release order, operator switch, rollback), implementation status,
  README.

## Final report — 2026-09-27

**Deployed: nothing.** No production reads or writes after plan preparation, no model call
on production data, no message sent. Work is on branch `whatsapp-sidebar` (base `c5c91f9`).

Verification: `bash scripts/test.sh` with the Node v24 fallback on PATH: **401 Python
passed** (baseline 305; includes 28 Chromium browser cases), **40 connector passed**
(baseline 33), compile check passed, no runtime skipped. The 24 warnings are the existing
`httpx` deprecation warnings also present in the baseline.

Migrations: `0013_connector_health.py`, `0014_assistant.py` (head `0014`).

New files: `app/assistant.py`, `app/assistant_context.py`, `app/assistant_pages.py`,
`app/proactive.py`, `app/whatsapp_health.py`, `app/templates/assistant.html`,
`connector/health.js`, `connector/health.test.js`, the two migrations,
`tests/assistant_fixtures.py`, `tests/test_assistant_context.py`,
`tests/test_assistant_eval.py`, `tests/test_social_assistant.py`,
`tests/test_whatsapp_health.py`.

Changed: `app/{event_identity,events,fake_generation,ingest,internal,models,next_steps,pages,
suggestion_pages,suggestions,web,whatsapp,whatsapp_pages,whatsapp_send}.py`, templates
(`base`, `connections`, `event`, `icons`, `me`, `person`, `suggestion`, `suggestions`,
`whatsapp`), `connector/{Dockerfile,index.js,queue.js,queue.test.js}`, `deploy/rollback.sh`,
`worker/main.py`, docs listed above, and tests adjusted for deliberate contract changes
(`test_browser` sidebar list, `test_gathering_retirement` and `test_legacy_inventory` for
the new head, `test_migrations`, `test_rollback`, `test_suggestions` draft save fingerprint).

Limitations and open items:

- Real-language quality of event-aware titles, persona styling and prepared drafts is
  **not evaluated**; synthetic tests prove structure, context, constraints and races only.
- Heartbeat behaviour is verified with unit/integration tests and a Node test harness, not
  with a live WhatsApp Web session; the connector image has not been built.
- Drafts saved before the release show "needs a review" once (no stored fingerprint).
- Pausing an event closes its open prepared drafts on the next scan (kept in History);
  per-chat pause only hides them.
- Rollback to pre-0014 images requires a coordinated downgrade that refuses while new data
  exists; the documented path is `python -m app.proactive mode off` plus a forward fix.
- Release: stop the old worker before migrating (see the runbook), then connectors pick up
  the heartbeat image via the provisioner.
