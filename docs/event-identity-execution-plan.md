# Event identity: execution plan and agent handoff

Status: implemented (M1–M5); M6 release results recorded below.
Prepared: 27 September 2026. Implemented: 27 September 2026 (branch `whatsapp-sidebar`, migration `0011`).
Repository: `/Users/enzolima/Projects/1f517/moresocial`.

## 1. Outcome and scope

Represent one real-world outing as one event, even when invitations, replies,
rescheduling, booking emails and Calendar entries describe it differently.
Retain all relevant evidence and distinguish genuinely separate plans.

Events must continue to originate from imported WhatsApp, email or Calendar data.
Do not reintroduce manual event creation. Corrections to event identity are allowed:
the user can say that detected records are the same event or should stay separate.

Implement the identity resolver, durable corrections, reversible grouping,
field-level evidence selection, review UI and a controlled backfill. Preserve the
current Events navigation, dismissal, source timeline and existing import behavior.
No provider migration, new embedding system, connector rewrite, external messaging
automation or general event-editing workflow is needed.

This plan supersedes the matching/reconciliation portions of
`docs/events-execution-plan.md` and `EVENTS-IMPLEMENTATION-PROMPT.md`. Those older
documents include obsolete manual-planning and AI-provider assumptions. Use current
code and this plan, not their branch, migration or provider instructions.

## 2. Confirmed failure and required behavior

The owner confirmed that these three cards on Sunday 27 September describe one outing:

| Detected record | Displayed time | Sources at investigation | Problem |
| --- | --- | --- | --- |
| Generic Sunday booking/invitation | 15:00 | 7 | Sparse invitation became its own event |
| Sunday reservation with a contact | 16:00 | 10 | Changed booking proposal became its own event |
| Lasergame, two rounds | 16:15–17:00 | 66 | Actual confirmed booking |

The source union contained **83 distinct source IDs** when investigated. Two of the
records shared an exact map link, an exact payment-request link and repeated outgoing
messages. The reservation conversation explicitly changed an earlier time. The sparse
invitation did not share those links; nearby messages also discussed an unrelated
Saturday birthday. Do not attach the whole conversation to the outing.

Expected result after applying the owner's correction:

- One event with the confirmed booking's activity, venue and 16:15–17:00 time range.
- All still-included relevant sources from the three records, deduplicated by source ID.
- Earlier proposed times visible as history, not competing event cards.
- The organizing role supported by invitations, even though an email supplies booking details.
- Attendance deduplicated by person and supported by the latest explicit relevant statement.
- The separate birthday, and a synthetic separate same-day dinner, remain separate.

The sparse invitation is an **uncertain** match without the owner's confirmation.
Do not tune the automated resolver to force that case to merge without evidence.
The owner has already supplied the same-event correction for these three records;
an authorized implementation may apply it through the tested correction service.
Rediscover exact IDs privately and verify them before applying. Do not hardcode names,
production IDs, private messages, payment links or booking references in tracked files.
Compare the source union immediately before/after repair; 83 is a historical baseline,
not a permanent production assertion.

## 3. Read first and establish the baseline

Read these files before editing:

| Area | Files |
| --- | --- |
| Current behavior | `docs/events.md`, `app/events.py` |
| Storage and scoping | `app/models.py`, `app/repo.py`, migration `0010_automatic_events.py` |
| Queue and imports | `app/jobs.py`, `worker/main.py`, `app/ingest.py` |
| UI | event routes in `app/pages.py`, `app/templates/events.html`, `app/templates/event.html`, `app/static/app.css` |
| Tests | `tests/test_events.py`, relevant event coverage in `tests/test_browser.py` |
| Operations | `docs/operator-setup.md`, `docs/agent-browser-login.md`, `scripts/test.sh` |

Start with `git status`, inspect local changes and identify the actual branch and
migration head. At planning time the branch was `whatsapp-sidebar` and migration head
was `0010`; neither is an instruction to overwrite a changed checkout.

Capture a focused test baseline before changes. Use the existing locked dependencies
and test database fixtures. Some historical AI-key/provider tests have been reported
as failing independently; reproduce any failures you encounter and report them
separately. Do not remove or weaken tests to produce a green result.

### Root causes to address

1. `match()` compares title/activity tokens, same local day and nearby times. Generic
   “booking” and “reservation” share no useful tokens with “Lasergame”. Both duplicate
   times were inside the existing 90-minute window: increasing that window is no fix.
2. The extraction prompt receives abbreviated event summaries, without enough shared
   links, participant identities or source excerpts to establish cross-chat identity.
3. `persist()` prefers the prior source-linked event before matching. Existing duplicates
   therefore remain attached to themselves even when better evidence arrives.
4. `EventScan` fingerprints the conversation's input. New evidence in another conversation
   does not invalidate an unchanged extraction window or reconsider its event identity.
5. `recompute()` takes most fields from one highest-ranked mention. Identity, current
   details and the user's role need different evidence rules.

## 4. Implementation decisions

Use the following design rather than choosing a new architecture during each milestone.

### 4.1 Preserve original records; project a canonical event

Keep `Event` rows as stable identities and keep each `EventMention.event_id` associated
with its original detected identity. Do not delete duplicate Event rows or move all
their mentions into a winner when grouping. Mention rows are recreated on rescans, so
their IDs must not be durable correction keys.

Add an explicit, reversible grouping layer:

- Add nullable `Event.canonical_event_id`, a workspace-scoped self-reference. Null means
  the row represents its group; members point directly to that row, never through chains.
- Add an `EventIdentityDecision` model/table. Store workspace, normalized pair of original
  Event IDs, verdict (`same`, `different`, `uncertain`), origin (`user`, `automatic`),
  active/superseded state, timestamps, resolver version, input fingerprint, cited source
  IDs and their content hashes, and structured reason codes. Persist no copied raw messages
  or private URLs in the audit record. Allow decision history; enforce one current decision
  per pair/origin. Add scoped foreign keys and indexes for pair/member lookups.
- Add a lightweight pair scan/cache table keyed by workspace and normalized event pair,
  with resolver version and input fingerprint. Keep the latest evaluation status so
  retries and unchanged scans do not repeat paid calls. Audit history and cache are distinct.
- Treat canonical pointers as a materialized result of active decisions, not the truth
  from which to reconstruct decisions. Compute groups deterministically from their members.

The graph is small enough to rebuild for an affected connected component. Prefer an
existing representative if it remains in the component; otherwise choose the oldest
Event, breaking ties by ID. Its display title comes from evidence, not its original title.
Only representatives appear in Home and the Events list. Detail reads aggregate member
mentions. Old member URLs resolve to the current representative with a **temporary**
redirect: permanent cached redirects would break after a split.

Retain each original Event's dismissal state. A group is dismissed if any member is
dismissed; restore/dismiss from the group UI applies to all current members. Document
and test that rule. After a split, each member retains its last explicitly applied state.

### 4.2 Corrections and reversibility

User same-event and keep-separate decisions are durable and outrank automatic decisions.
They survive unchanged rescans, model changes and fresh evidence. Use stable Event IDs
plus the existing opaque source identity tombstones to reconnect rescanned mentions.

Never blindly union pairwise matches. Before joining groups, check **every cross-group
pair** for an active keep-separate decision or a current evidence-backed different verdict.
For example, A=same B and B=same C must not override A=different C. Reject contradictory
user corrections without a partial update; explain which earlier correction must be undone.
An explicit user correction may supersede an automatic different verdict, but must be
recorded as such. It cannot silently override another user correction.

Support undoing a correction and splitting a mistaken grouping. The split operation
accepts an explicit partition of the original member identities, supersedes all crossing
same decisions, and writes durable keep-separate decisions across the partition. Internal
decisions remain intact. Show the affected source sets before submission. Undoing one edge
alone may leave another path connecting the group; the UI must show the actual result.
New evidence remains attached to its original identities and is not lost by either action.

Use one service layer for user corrections, automatic resolution and operator repair.
All writes use the existing workspace event advisory lock, scoped queries and a transaction.
No production repair through hand-written SQL updates.

### 4.3 Separate extraction from identity resolution

Retain the bounded per-conversation extraction pass and stable scan keys. Add a dedicated
`resolve_events` job after extraction changes event evidence. Also enqueue after relevant
source changes, exclusion/deletion and user corrections. Periodic reconciliation recovers
missed work and backfills existing records, including recent past events.

Extraction should retain an original identity when rescanning its own evidence, but it
must no longer use a free-form `match_id` to irreversibly coalesce unrelated identities.
Treat cross-event model matches as resolver candidate hints. For newly extracted mentions,
create a stable original record unless supported by the same source/calendar object identity.
When a source discusses multiple events, source overlap alone is not sufficient to choose
the first previous record: compare the extracted plan context and preserve distinct mentions.
It is valid for one source to support more than one event.

Resolve unchanged original records when **other events' context changes**, without paying
to re-extract all unchanged conversations. Pair fingerprints must include both sides'
sorted member IDs, evidence IDs/content hashes, extracted structured data, relevant person
identity mappings, current correction state and resolver/prompt version. Do not hash only
the winning card summary. When people are linked or unlinked, invalidate affected fingerprints
or ensure periodic recovery observes the changed identity mapping.

Keep existing extraction fingerprint behavior where possible. Bump `INPUT_VERSION` only
if extraction input/output changes require reprocessing. Do not casually change `VERSION`:
it participates in stable scan identity and can orphan old mentions.

### 4.4 Candidate retrieval and evidence packet

Candidate retrieval is deliberately broader than the merge decision. Start with:

- Same local date and compatible/unknown activity or shared participants, considering
  all-day/unknown times. Date and proximity only retrieve candidates; they prove nothing.
- Shared booking reference (scoped to issuer), distinctive booking/payment URL, quoted
  message relationship, or long non-template repeated invitation text.
- A stable Calendar object identity, or explicit rescheduling evidence linking old/new
  dates. Different dates alone are not a hard veto when a move is explicit.

Resolve against retained attached evidence even if it is older than the normal 30-day
extraction lookback. Default backfill includes upcoming and recent-past non-legacy events;
exact user-selected identities can be reconciled outside that range.

Build a deterministic evidence packet for each candidate pair, containing structured
facts, participant IDs, proposed/confirmed times, booking references and relevant excerpts
from both records. Include a few adjacent messages when needed to interpret “yes”, “paid”,
or “that time”. Context-only messages are not automatically event evidence. Keep source
labels mapped to real IDs and validate every citation against the permitted packet.

Normalize URLs conservatively: lowercase host, remove known tracking parameters/fragments,
preserve booking/payment IDs and meaningful query parameters. Never strip a token that
distinguishes two bookings. A generic venue homepage or reused map link is weak evidence;
even a payment link requires event-compatible context. Extract references locally without
fetching links. Never log payment URLs or tokens. Repeated template text is not a strong link.

Bound costs explicitly: at most 20 candidate pairs per resolver job, 40 cited/context
messages and about 24,000 characters per pair packet. Prioritize shared references and
explicit links, then date/participant candidates. Queue continuation work for overflow;
do not silently discard the rest. Reuse the configured generation provider and strict
JSON-schema conventions in `app.ai`; do not hardcode a model or add embeddings.

### 4.5 Verdict contract and merge policy

The model returns `same`, `different` or `uncertain`, evidence labels for both sides,
structured supporting/conflicting signals and a short explanation. Treat all input text,
including event summaries, as untrusted data. The model has no tools and cannot execute
instructions in messages. Malformed responses, missing support and provider failures
must not create a merge or a durable different constraint.

Server-side validation, not a confidence number alone, controls automatic grouping:

| Evidence | Default action |
| --- | --- |
| Owner's explicit same-event correction | Group through correction service |
| Same stable Calendar object through a reschedule | Preserve its identity |
| Distinctive shared booking/payment reference plus compatible event context, with no conflict | Eligible for automatic same verdict |
| Explicit quoted/copied invitation or reschedule link plus compatible activity/people | Eligible for automatic same verdict |
| Same date, nearby time, same organizer or overlapping attendees only | Uncertain; do not auto-merge |
| Unknown title, time or venue on one side | Missing information, not a contradiction |
| Different times explicitly described as proposal then confirmation | One identity; preserve history |
| Different bookings/activities with evidence of separate plans | Different; retain separate events |
| Ambiguous or conflicting evidence | Uncertain; reviewable, no automatic grouping |

Independent confirmed bookings normally support separation, but amendments/replacements
can share identity when explicitly linked. A venue change or time mismatch is not by itself
proof of a different event. Two consecutive laser-game rounds under one outing remain one
event. A same-day dinner and laser-game outing remain distinct even with the same people.

User different decisions persist until undone. Automatic different/uncertain decisions
are valid only for their evidence fingerprint and must be reconsidered when it changes.
If supporting sources are removed, deactivate affected automatic same decisions and rebuild
groups before showing unsupported derived content. User corrections can retain opaque
identity links, but cannot make deleted/excluded content visible.

### 4.6 Current facts and provenance

Refactor `recompute()` to aggregate all current member mentions and select fields separately:

- Title/activity/place/start/end: prefer explicit confirmed booking facts over proposals;
  later explicit rescheduling can supersede an earlier confirmation. Preserve session ranges.
- Role: explicit organizing invitations can establish organizer independently of a ticket email.
- Attendance: latest explicit relevant statement per known person, with source attribution;
  do not infer attendance from payment alone or group membership.
- Cancellation: distinguish cancellation of the outing from one person declining or one
  booking being replaced. A later explicit reinstatement must be representable.
- Summary/history: summarize the resolved plan and relevant changes, without implying that
  an unresolved timing conflict has been settled. Keep alternative proposals in the timeline.

Store source IDs for each selected field, replacing the current single `field_source`
assumption. Every displayed fact must remain traceable to included, non-deleted sources.
Deduplicate sources by ID and people by canonical person ID. Do not infer a final attendee
count from the number of merged records. Conflicting confirmed details should be surfaced
as needing clarification rather than silently selected by incidental iteration order.

## 5. Sequential implementation milestones

Complete these in order. Keep commits focused and update this checklist with actual results.
Do not pause for approval after every milestone.

### M1 — Regression fixtures and schema

- [x] Add synthetic fixtures in `tests/test_event_identity.py` reproducing the three records:
  generic invitation, changed reservation with shared references, confirmed two-round booking.
  Use fictional people/venue/links and a frozen clock. Include nearby unrelated birthday text.
- [x] Add the additive migration after the actual current head; implement scoped models,
  canonical membership and decision/cache storage. Existing events begin as singleton groups.
- [x] Test upgrade on existing populated data, workspace isolation, constraints and cascade deletion.

Done when the fixtures demonstrate the current duplication and the schema preserves all
existing event/source data. Do not delete production duplicates in the migration.

Result: `tests/test_event_identity.py` (synthetic Lumen Arena fixture); migration `0011_event_identity`
(additive: `events.canonical_event_id`, `event_identity_decisions` with `applied` flag and a partial unique
index on the current decision per pair/origin, `event_pair_scans`). Downgrade→upgrade on populated data,
composite-FK isolation, pair ordering and account-deletion cascade are tested.

### M2 — Deterministic grouping and corrections

- [x] Create `app/event_identity.py` for group lookup, decision application, split/undo and
  canonical recomputation. Keep detection-specific code in `app/events.py`.
- [x] Implement group-level conflict checks, deterministic representatives and idempotent writes.
- [x] Integrate group-aware cards, detail lookup and source aggregation before enabling merges.
- [x] Preserve original identities through rescans, dismissal, splitting and later source arrival.
- [x] Test A–B–C conflicts, cycles/redirect behavior, concurrent corrections and exact source unions.

Done when grouping, separating and undoing work without any model calls and do not lose evidence.

Result: `app/event_identity.py`. `persist()` no longer coalesces across conversations; the previous
lexical match is now a reversible automatic decision (`via=lexical`). Corrections write decisions
between every selected pair so a later split keeps the rest together. Wall-clock `created_at` keeps
the oldest-representative rule deterministic within one transaction.

### M3 — Cross-conversation resolver and queue

- [x] Implement candidate retrieval, normalized signals, bounded evidence packets and verdict schema.
- [x] Add `resolve_events` handling to `worker/main.py`, enqueue triggers and periodic recovery.
- [x] Add pair fingerprints, continuation jobs, cache reuse and diagnostics with counts/reason codes.
- [x] Keep model calls outside DB locks; renew job leases during slow calls using existing patterns.
- [x] After the model returns, reacquire the workspace lock and recompute the input fingerprint.
  Discard stale results and enqueue current work if evidence, identities or corrections changed.
- [x] Provide resolver modes `off`, `observe`, `apply`. Observe stores decisions/review candidates
  without changing group membership. User corrections remain explicit service operations.

Done when new booking evidence reconciles unchanged prior conversations, retries are
idempotent, uncertain cases remain separate and stale calls cannot override user corrections.

Result: `app/event_resolver.py` (`RESOLVER_VERSION = identity-v1`); mode is the operator setting
`event_resolver_mode` (default `observe`). Automatic triggers are coalesced to one job per workspace
per minute; periodic recovery runs hourly per workspace. A grouping change within a run re-derives
the remaining candidates (still at most 20 evaluations per job).

### M4 — Field reconciliation and invalidation

- [x] Implement field-level selection/provenance and history behavior in section 4.6.
- [x] Extend source invalidation to resolver decisions/caches and affected groups immediately.
- [x] Ensure privacy deletion removes derived text/URLs and account deletion cascades new tables.
- [x] Ensure backfill and recovery include recent past events and do not resurrect unsupported cards.

Done when the regression's confirmed time and organizer role coexist, and exclusion removes
all dependent display content and invalid automatic groupings without waiting for an AI call.

Result: `events.project()` with `details.provenance`, `details.history`, `needs_clarification`;
new extraction kind `reschedule` (schema only; `INPUT_VERSION`/`VERSION` unchanged, no re-extraction).

### M5 — Review UI and operator repair

- [x] Add a small “Possible duplicate” section with the two candidates, evidence-backed reason
  and actions **Same event** / **Keep separate**. Do not label every same-day pair a duplicate.
- [x] Allow selecting an existing detected event to correct a missed match, even if it was not
  suggested; this is identity correction, not manual event creation.
- [x] In event detail, show grouping history, source counts and reversible correction/split controls.
  Use readable source links rather than internal IDs or model scores in the product UI.
- [x] Protect all mutations with authentication, workspace scoping, CSRF and current-state checks.
- [x] Add `scripts/reconcile-events.py` with explicit workspace/event selection, dry-run default,
  an apply option, validated correction operations and a machine-readable before/after report.
  Dry-run may evaluate in memory but must not change grouping. Do not print private source bodies.
- [x] Add desktop/mobile browser coverage for review, grouping, keep-separate, undo/split and dismissal.

Done when the owner can correct both missed and mistaken groupings and a rescan respects the result.

Result: routes in `app/pages.py` (`/events/identity/same|separate`, `/events/{id}/split`, `/events/{id}/undo`
with GET previews), templates `events.html`, `event.html`, `event_split.html`, `event_undo.html`;
`app/event_reconcile.py` + `scripts/reconcile-events.py`; browser test
`test_event_identity_review_group_split_undo` at 1440 and 390 px.

### M6 — Evaluate, backfill and release

- [ ] Run the acceptance matrix below, focused regression tests and the repository's applicable
  broader checks. Record exact commands, results and any independently reproduced baseline failures.
- [ ] Update `docs/events.md` and operator documentation with implemented behavior, flags,
  job recovery, correction semantics and rollback limitations.
- [ ] In observe mode, evaluate a bounded sample containing the confirmed duplicate and distinct
  same-day plans. Inspect evidence privately; record aggregate outcomes, not message contents.
- [ ] Commit the reviewed implementation before building a release; deploy scripts use tracked code.
- [ ] Follow the authorized release procedure and production verification in section 7.

## 6. Required acceptance matrix

Use fake deterministic model responses for routine tests. A live provider evaluation is
supplementary and must not be required for CI. Include tests of packet construction and
server-side verdict validation, not only mocked final decisions.

| Case | Required result |
| --- | --- |
| Three-record regression with owner correction | One group; exact current source union; confirmed time/venue; organizer retained |
| Same regression without owner correction | Strongly linked reservation can join; sparse invitation remains uncertain |
| Different titles with shared distinctive booking/payment evidence | Eligible merge without lexical title overlap |
| Only same date/time/organizer | No automatic merge |
| Reused venue/map URL; different independent bookings | Stay separate |
| Invitation with unrelated birthday in adjacent context | Birthday evidence/attendance does not leak into outing |
| Two rounds of one booking | One event spanning both rounds |
| Same people at dinner later that day | Separate event |
| Explicit change from proposal to confirmation, including another day | Same identity; latest supported schedule; history retained |
| Two independently confirmed overlapping activities | Separate or uncertain; never merge from time proximity |
| A=same B, B=same C, A=keep-separate C | No contradictory/transitive merge |
| Repeat scan, retry, process restart, unchanged backfill | Stable grouping; no duplicated sources or repeated unchanged model call |
| New evidence in another conversation | Existing pair reconsidered without re-extracting unchanged windows |
| User correction during slow model call | Stale result discarded; correction preserved |
| Two simultaneous resolver jobs | One consistent group/decision result |
| Source changed, excluded, deleted or conversation disabled | Invalid automatic decisions withdrawn; no stale private content |
| Group undo/split after new evidence arrived | Evidence preserved; no immediate remerge against keep-separate |
| Dismiss then merge/rescan; restore then split | Documented dismissal behavior retained |
| Person identities linked | Attendance deduplicated and resolver input invalidated |
| Foreign workspace IDs, forged citation or missing CSRF | Rejected with no mutation or evidence disclosure |
| Malformed model response, provider timeout, oversized candidate set | Safe retry/continuation; no speculative grouping |
| Old duplicate detail URL then split | Resolves to correct current event; no permanent redirect trap |
| Populated migration and account deletion | Data retained on upgrade; scoped new data removed on deletion |

Suggested initial test command from the repository root:

```bash
.venv/bin/python -m pytest tests/test_events.py tests/test_event_identity.py
```

Run relevant browser coverage and authentication/source-invalidation checks as changes
land, then the applicable full checks once. If the environment prevents a check, report
that explicitly; do not claim the whole suite passed. Do not repeatedly run unrelated
checks after documentation-only changes.

## 7. Deployment and production verification

This document itself does not perform a deployment. When implementing under the owner's
existing instruction to implement, deploy, test and notify, carry that authorization
forward. If this file is used in a new assignment that only authorizes local work, finish
the tested, reviewable implementation before seeking any missing deployment authorization.

1. Re-read `docs/operator-setup.md`; use its release, backup and readiness procedures.
   Verify current host paths/services and the exact committed SHA. Do not alter other
   applications on the host or perform Docker pruning.
2. Take the documented recoverable backup. Record the old image/source revision and
   feature mode. Deploy with automatic grouping in observe mode initially; verify migrations,
   web/worker readiness and resolver job dispatch.
3. Run the operator tool in dry-run mode for the owner's target records, scoped to the
   correct workspace. Verify date, source context and the selected three IDs. Capture their
   source union and current dismissal/attendance data privately.
4. Apply the already-confirmed same-event correction through the tested service. Verify
   one canonical event, exact source union, 16:15–17:00 schedule and correct provenance.
   Do not apply blanket same-day merges or force the sparse invitation through the model.
5. Review the bounded observe-mode sample. Enable automatic grouping only after no false
   merge in the reviewed sample and the automated negative-case tests pass. Record the
   sample size and limits: this is not proof of universal matching accuracy.
6. Queue a bounded, resumable backfill of eligible existing records. Monitor failures,
   pending/continued pairs, model-call counts, uncertain verdicts and grouping counts.
   A second unchanged run must not repeat paid work or alter the source union.
7. Use `docs/agent-browser-login.md` to issue a short-lived session and inspect live Home,
   Events and the corrected event at desktop/mobile sizes. Keep credentials and screenshots
   containing private data in ignored `.dev/` only. Revoke the session when finished.
8. Verify grouping survives a resolver rerun and ordinary source refresh. Exercise destructive
   correction/split paths in tests or synthetic staging data, not by disrupting the owner's
   repaired event. Confirm unrelated same-day events remain visible separately.
9. Report release SHA, migration, verification results, correction applied, unresolved uncertain
   cases and rollback approach. If the implementation assignment authorizes the requested
   WhatsApp notification, send the owner a brief ready-for-review message with the Events link
   only after live verification; report delivery failure honestly. Do not notify other people.

### Rollback

Turning resolver mode off stops new automatic decisions; it does **not** undo existing
groups. To reverse bad automatic grouping, deactivate the affected automatic decisions
through the service and rebuild groups while retaining user corrections and source data.
Use a dry-run report first.

An additive schema alone does not make an old application semantically compatible:
pre-resolver code may ignore canonical pointers and show duplicate cards again. Prefer
feature disablement and a forward fix. If an image rollback is necessary, follow the
operator runbook, document the temporary duplicate-display limitation, and retain the
new tables for a later forward deployment. Do not drop the schema or delete evidence
as an emergency rollback. Validate this recovery path locally before release.

## 8. Handoff prompt

Copy the following into the implementation agent's conversation:

```text
Implement docs/event-identity-execution-plan.md in
/Users/enzolima/Projects/1f517/moresocial.

Read the plan and the listed current code first. Follow milestones M1–M6 in order.
The approved design is automatic evidence-backed Events, cross-conversation identity
resolution, durable Same event / Keep separate corrections, reversible grouping,
field-level provenance, and the September 27 regression described in the plan.

Work autonomously through the authorized scope; do not ask me to choose architecture
again or approve every milestone. Preserve unrelated changes. Use the configured AI
provider, current migration head and existing deployment process. Do not resurrect
manual event creation or follow obsolete provider assumptions in older Events plans.

Keep private production data out of tracked fixtures and logs. Use synthetic tests.
Do not fix duplication by merging every nearby same-day event or deleting records.
Complete local implementation and tests before any production repair. Use the tested
correction service and dry-run tooling for repair, never ad hoc SQL updates.

For an implementation assignment that includes deployment, complete the release and
live verification in section 7 using docs/agent-browser-login.md. Carry forward any
deployment/WhatsApp authorization in this conversation; if it is absent, ask only for
the missing authorization after the changes are concrete and tested.

Update the milestone checklist with results. Finish with changed behavior, tests,
release status/SHA if deployed, unresolved limitations, and links to relevant files.
```
