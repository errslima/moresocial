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
overridden. Event regrouping, splits, reschedules, cancellations, dismissal and source
exclusion re-link affected actions without a model call; a changed link or event
projection marks the action stale and schedules one fresh review. Unchanged messages alone
no longer clear `stale` when the linked event changed.

## Drafts, persona and freshness

The **Assistant** page holds one persona: instructions (≤4,000 characters), warmth,
directness, humour, length, emoji, language (empty = match the conversation), optional
wording examples, initiative, the two preparation switches and a pause control. People
have check-in preferences and a free-text style/boundaries field; a contact's boundaries
override the persona's initiative. Settings are user-authored instructions about wording
only; imported text never becomes a setting, and nothing can grant sending.

Every draft records a fingerprint of its context (messages, linked event, persona,
person preferences, updates, destination). Copy and composer prefill require the saved
draft to match the current context; after a change the text stays exactly as written and
shows "The context changed since this draft was written" until the user checks and saves
it. Saves submit the fingerprint shown on the page and fail with 409 if it moved.
**Preview my style** makes one accounted model call on a fictional scenario.

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
reruns, restarts, rescheduling and grouping never recreate a dismissed action. Publication
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
synthetic evaluation set. Browser cases exercise desktop/mobile display, drafting, edits,
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
