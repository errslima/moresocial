# Suggestions

`/suggestions` is the one inbox for two producers:

- **Conversation reviews** (`producer='conversation'`): questions, promises and follow-ups
  in one-to-one WhatsApp conversations, anchored to a message.
- **Prepared messages** (`producer='proactive'`, see below): an *Event reminder*, *Ask
  about a detail*, *Check in* or *Share an update* that the assistant prepared from the
  user's own settings, with an editable draft already written.

Each card explains its reason, links source evidence and any related event, and offers
editable/copyable text, **Open conversation**, Done, dismissal reasons and 1/3/7-day
snoozing. Copying, opening, drafting, previewing and Done never send anything and never
count as delivery. Conversation preferences pause analysis, preparation and display for
someone until resumed. **The assistant never sends messages**: only the user's explicit
**Send message** in the WhatsApp composer transmits the text shown there.

The inbox shows five items per page, with deadline ordering, Snoozed and History views.
It reports AI availability, WhatsApp sync health from connector heartbeats
([Connections](connections.md#sync-health)), pending/failed reviews and incomplete history.
Gmail and group chats are not reviewed or prepared for. Manual gathering workflows have
since been retired; events are recognized automatically ([Events](events.md)).

## Review and lifecycle

- Migration `0008` adds `suggestion_reviews`, `suggestions` and `suggestion_sources`.
  All are workspace scoped with composite foreign keys; migration is additive.
- Ingestion marks changed conversations for review after a two-minute debounce. The
  existing worker schedules eligible reviews, serially with other jobs. Startup AI
  reconciliation discovers previously imported one-to-one chats active in the last 30 days.
- Each request includes at most 80 recent messages (last 30 days), up to 40 older open
  anchors, 200 previous suggestions and 10 relationship notes. Text fields are bounded;
  source links provide the full available originals. Unsupported media stays labeled.
- The model identifies actions, not merely unanswered last messages. It must cite actual
  source labels and resolve old actions explicitly; omission never means resolution.
  JSON, identity, direction, dates and citations are validated before publication.
  The generation schema restricts anchors and evidence to this snapshot's message
  labels. Internal source IDs/versions stay out of the model prompt; quoted text is
  not accepted as a citation. Optional due dates must be full RFC 3339 timestamps with
  a timezone offset, enforced in the model schema and server validation.
  Kind-specific schema branches restrict reply anchors to incoming messages and promise
  anchors to the user's own messages; server-side direction validation remains mandatory.
  Failure notices count conversations whose latest review
  is still failed, rather than historical attempts that have since been retried.
- Review input carries a conversation revision and fingerprint. Provider calls happen
  outside database transactions; publication rechecks both and the worker lease. A newer
  message, source change, preference or user disposition invalidates an in-flight result.
- One `(workspace, conversation, anchor message, kind)` identity persists through reruns.
  Done, dismissed and resolved actions do not reopen through rewording. Snoozing also persists.
- Open actions are checked daily or at the next explicit deadline. A conversation with no
  actions is checked once more the next day if its last message was within 24 hours, then
  waits for changes. Manual review retries are limited to chats not checked in 15 minutes.
- Source edits/exclusions erase derived text, drafts and evidence for that conversation;
  deletions cascade through anchored suggestions. Reanalysis can restore supported open
  items, while remaining terminal identities suppress repetition.
- Existing drafts are never regenerated over an edit. Draft saves check revisions; after
  new context, an old draft must be reviewed and saved before its copy button returns.

Per-chat pause and relationship notes can suppress suggestions. Models receive source text
as untrusted data, have no tools and cannot perform external actions. A correct citation
validates provenance, not the model's interpretation; real recommendation quality should
be evaluated through use and dismissals.

## Event context

Reviews and drafts use one bounded context builder (`app/assistant_context.py`). Related
Events are found through **source overlap** with the conversation's messages across every
grouped original record; events where the person is only a participant are offered as a
labelled possibility ("not confirmed as related"). Dismissed, legacy and unsupported events
are excluded; at most five events and 40 evidence messages / 24,000 characters are
supplied, with omissions counted. Facts come from the existing Event projection with
field provenance (E labels, or M labels already in the chat input). Other guests' messages
are never supplied; email/Calendar text only where it establishes schedule, place, title or
status. A booked start and a meeting time mentioned in chat stay separate facts.

A review may name an event label; the server links deterministically: exactly one event
group sharing the action's own evidence, or the model's choice among several that do.
The link stores the **original** record whose evidence overlaps, so it follows the right
record after a split; the card shows its current group title, date and place. Several
overlapping groups stay unlinked and are shown as "Possibly related". The detail page lets
the user choose among eligible events or "Not related"; that correction is never
overridden, including by background re-preparation of a prepared message. Event regrouping,
splits, reschedules, cancellations, dismissal and source exclusion re-link affected actions
without a model call; a changed link or event
projection marks the action stale and schedules one fresh review. Unchanged messages alone
no longer clear `stale` when the linked event changed.

## Drafts, persona and freshness

The **Assistant** page defaults to **Match how I write to this person** (`style_mode =
conversation`): drafts follow representative examples of the user's own messages to that
recipient (see [voice](#voice-from-conversation-history)). **Use my custom tone settings**
(`custom`) applies warmth, directness, humour, length and emoji; those controls sit in a
secondary section and are kept in both modes. Both modes keep instructions (≤4,000
characters), language (empty = match the conversation), optional wording examples to
prefer or avoid, initiative, the two preparation switches and a pause control. People have
check-in preferences and a free-text style/boundaries field; a contact's boundaries override
the persona. Settings are user-authored instructions about wording only; imported text never
becomes a setting, and nothing can grant sending.

Every draft records a fingerprint of its context (messages, linked event, persona,
person preferences, updates, destination, and the workspace-local date, so a draft saying
"tomorrow" stops being current at local midnight). Copy and composer prefill require the saved
draft to match the current context; after a change the text stays exactly as written and
shows "The context changed since this draft was written" until the user checks and saves
it. Saves submit the fingerprint shown on the page and fail with 409 if it moved.
**Preview my preferences** makes one accounted model call on a fictional scenario with no
contact data, so it shows explicit preferences (and custom tone) only, never a per-person voice.

Each action also records `context_sources`: every source whose text or facts reached its
title, reason or draft, including the sources behind every event in its context (so a
venue from a booking email in another conversation counts). When any of them is edited,
excluded or deleted, that derived text is erased, edited drafts included, the action is
taken out of view and refreshed from what remains (a chat review, or re-preparation).
Migration `0015` adds the column and backfills event-linked actions with their group's
sources; text written earlier from unlinked event candidates is refreshed on review.

## Evidence-grounded drafts

When someone asks something whose answer lives elsewhere in the user's WhatsApp or Gmail
history (for example where they will stay, settled in a chat with the host), an explicit
**Draft a reply** or **Rewrite with current information** runs one bounded pipeline
(`app/draft_evidence.py`, operator rollout switch, default off):

1. **Plan** up to four information needs from the incoming message deterministically; a
   question about current feelings or health is its own need and is never answered from
   history. Named people are matched only through stored names and identifier labels; two
   contacts with one name stay separate (passages label them "Sam (contact A/B)").
2. **Retrieve** with one batched query-embedding call and hybrid search over included
   WhatsApp and Gmail chunks (Calendar facts come from Event projections; diary entries are
   never retrieved). Workspace, provider, inclusion and embedding space are filtered inside
   both queries; at most four purely semantic hits per question and 12 chunks overall.
   Without embeddings, keyword search runs and the reduced coverage is recorded, never
   treated as "nothing found".
3. **Expand** hits with neighbouring turns and later messages of the matched conversations
   (so "yes, let's do that" attaches to the right proposal and a later cancellation is seen),
   within 40 passages / 24,000 characters, with room reserved for context.
4. **Corrections and claim lifecycle** are loaded independently of vector ranking:
   user-written or confirmed claims sharing the question's words, claims supporting the
   passages in any state, and correction links (`claims.corrects_id`, recorded for edits from
   `0016` on; earlier corrections have no guessed link). Rejected claims are shown as
   rejected and never become answers; inferences, health and diary-only claims are excluded.
5. **Resolve** in one structured call: per question `supported`, `uncertain`,
   `conflicting`, `missing` or `withheld`, the decision state (possibility, offer, accepted,
   obligation, completed, cancelled), statement date, effective period and deadline, the
   minimal first-person answer and whether it may be shared. The server rejects unknown
   labels or malformed dates and applies deterministic guards: passages with an explicit
   confidentiality request are never shared, rejected claims cannot support an answer,
   current-mood questions stay missing. Only supported and shareable answers become
   **permitted facts** (P labels).
6. **Write** from the recipient conversation, its events, the permitted facts, unresolved
   points (as placeholders only where the reply needs the user's answer), explicit
   preferences and style-only examples (W labels). The writer never receives passages from
   other conversations and returns the P labels it used; unknown labels are rejected and
   internal labels are stripped from the text.
7. **Verify** once when other sources were consulted: one call checks factual grounding,
   disclosure, invented commitments and mood; it may remove unsupported parts once (issues
   are stored as kinds only). A draft that cannot be verified is not published.
8. **Publish** atomically after rechecking the review revision, the action's own context,
   the draft revision (a concurrent edit wins), every considered passage and style example,
   claim fingerprints and the rollout switch.

The bundle (`draft_evidence_bundles`, one per suggestion) stores references, not copied
passages: questions, considered source IDs/versions and roles, claim fingerprints, answers
with source links, permitted and used facts, style example IDs, coverage, verification
outcome, versions and per-draft token/latency metrics. Every influential source (including
evidence that led to withholding) joins `context_sources`, so excluding or deleting any of
them erases the draft, edited or not, and the bundle.

The detail page shows **What this draft uses**, private to the user and never copied or
prefilled: each question with its answer, deadline, reason when not answered and links to
the supporting messages with dates; the voice basis; coverage limits (keyword-only search,
unindexed messages, shortened evidence, history searched). A rewrite never overwrites an
edited draft: it appears beside it with **Use this version** / **Keep my text**. Drafts from
before this release are kept and labelled as written with the previous context.

**Freshness.** Material evidence changes (sources, exclusions, claims and corrections,
identity links, new chunks and embeddings) bump a per-workspace evidence revision once per
committing transaction; usage accounting, bundle publication and unchanged re-extraction do
not. A newer revision makes an evidence draft "being checked" (copy and prefill wait) until a
deduplicated `revalidate_draft` job reruns the deterministic retrieval with the cached query
vectors: an unchanged evidence set simply restamps the bundle; a changed one costs one
resolution call, and unchanged answers refresh metadata without touching the user's text,
while a changed answer marks the draft outdated ("Newer information changes an answer").
The user can rewrite, or correct and save their own text, which makes that reviewed text
usable again. Revalidation never revives a terminal suggestion and never sends. GET, copy
checks and composer prefill only read persisted state. An import burst elsewhere during
drafting is absorbed by rerunning the deterministic retrieval at publication.

## Voice from conversation history

`app/voice.py` selects 6–12 examples (at most 8,000 characters) of the user's own included,
non-deleted outgoing messages in the exact destination conversation (one account, one chat),
with no 30-day cutoff, spread over time, preferring substantive messages and, for replies,
messages that answered the other person; links, media placeholders, quotes and emoji-only
messages are skipped and very short ones kept only when typical. Messages sent through the
composer and text equal to any prepared draft are excluded so the assistant does not learn
from itself; other outgoing text is assumed to be the user's. The other person's messages are
never examples. With fewer than three usable examples the writer stays plain and concise and
the page says so. Examples are untrusted, style-only input: their dates, names and plans must
not appear in the new message. Proactive drafts use the same selector and voice rules while
keeping their stricter disclosure policy (personal news only from approved updates; no
cross-conversation retrieval).

## Prepared messages (proactive)

A deterministic scan (`proactive_scan` job, every 30 minutes, 08:00–23:00 in the
workspace timezone, no catch-up after downtime; manual **Prepare messages** may run
outside the window) finds candidates, and each due candidate with changed input gets one
`prepare_message` job and one model call for one recipient:

- **Event reminder / Ask about a detail**: confirmed (`accepted`) participants of planned
  events 1 hour to 7 days ahead, about 24 hours before start. Conflicting confirmed times
  (`needs_checking`) make a question due earlier unless initiative is low. Paused or
  dismissed events, paused chats and non-confirmed participants are skipped; the event page
  explains missing or ambiguous targets.
- **Check in**: pinned contacts or contacts with check-ins turned on (never the whole
  address book), due after their interval (default 30 days; ×1.5 at low, ×0.75 at high
  initiative) since the later of the last direct message either way (included, not deleted,
  not group, same account) and the user's last check-in disposition. An open reply or
  promise for the person and an upcoming event take precedence; a fresh message closes an
  obsolete check-in. The context includes older direct messages, so quiet contacts keep it.
- **Share an update**: people in the audience of an update written on About me (not at low
  initiative); a due check-in may carry it instead. Each update × person is proposed once;
  only a confirmed send receipt from the composer records delivery.

At most five new prepared drafts per workspace per local day (three at low, seven at high
initiative); manual preparation adds one bounded batch of five. One open prepared message
per recipient; an event may replace an unedited generic one. Identities are
server-created from recipient + purpose + original event, contact cycle or update, so
reruns, restarts, rescheduling and grouping never recreate a dismissed action. A snoozed
prepared message is left alone until its snooze ends; after that it is open again and is
refreshed when its context changed. If the user corrects its event (or marks it as not
about an event), preparation uses that choice and never resets it. Publication
rechecks the lease, candidate revision, full fingerprint, pauses and terminal identities;
edited drafts are never replaced. Housekeeping closes actions for cancelled, past, paused
or dismissed events, declined participants, duplicates and expired updates. Policy
rationale ("You asked to keep in touch every 30 days…") is shown as the user's own
preference, not as a request from the other person. Operators can stop preparation with
`python -m app.proactive mode off` (see the [runbook](operator-setup.md)).

## Verification

`tests/test_suggestions.py` covers workflow, evidence validation, reminders, dismissals,
resolution, worker scheduling, source invalidation, workspace/CSRF boundaries, stale review
and draft races, bounded truncation retry, identity splitting and deadline ordering.
`tests/test_assistant_context.py` covers event association (grouped members, same-day
events, ambiguity, corrections, split, reschedule, cancellation, conflicts, exclusion,
bounds). `tests/test_social_assistant.py` covers persona, preferences, updates, proactive
scheduling, races, privacy and zero send calls; `tests/test_assistant_eval.py` is the small
synthetic evaluation set. `tests/test_draft_evidence.py` covers the evidence pipeline with
fictional fixtures (cross-chat and Gmail-only answers, accepted versus earlier alternatives,
cancellation after publication, short assents, namesakes and other accounts, corrections
without embeddings, confidentiality, diary and mood, style-example facts, prompt injection,
verification repair, budgets and coverage, rewrite protection, erasure, publication races,
revalidation, rollback triggers and zero provider calls on reads); `tests/test_draft_eval.py`
runs the evaluation harness (`app/draft_eval.py`) synthetically. Browser cases exercise desktop/mobile display, drafting, edits,
clipboard, dismissal, settings, prepared drafts, event pause and composer handoff.
Synthetic tests establish these contracts, not the quality of a live model's wording.


## Open the conversation and send

Each suggestion with available evidence has an **Open conversation** button leading to
MoreSocial's own WhatsApp page, using the evidence's exact account and conversation.
A current saved draft is prefilled for editing; stale drafts are not carried over.
Nothing is sent by opening the page. Press **Send message** to send the displayed text
from the linked account. Existing direct chats (including LID identifiers) and groups
support text up to 4,000 characters. **Message yourself** can start a conversation
with the currently linked account even if it has no imported history. Attachments
are not supported.

Sends require authentication, CSRF protection, a visible stored conversation and a
matching ready connector account. The connector independently checks the current
account and chat. Send attempts are claimed durably before contacting WhatsApp, so
refreshes and repeat submissions do not resend. A timeout is shown as an uncertain
result and is never automatically retried. Confirmed sends use the normal idempotent
message ingestion path. Delivery/read receipts are not tracked. Suggestions update
through their normal conversation review; sending does not blindly mark them done. A
confirmed send of a prepared *Share an update* message records that update as delivered to
that person; an uncertain or rejected outcome records nothing.
