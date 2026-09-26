# Suggestions

First release: `/suggestions` helps users act on questions, promises and follow-ups in
one-to-one WhatsApp conversations. Each card explains its reason, links source evidence,
and offers on-demand drafting, editable/copyable text, Done, dismissal reasons and
1/3/7-day snoozing. Copying never sends or completes an action. Conversation preferences
pause analysis and hide suggestions for someone until resumed.

The inbox shows five items per page, with deadline ordering, Snoozed and History views.
It reports AI availability, stale WhatsApp status, pending/failed reviews and incomplete
history. The detail view shows the reviewed conversation context and original sources.
Gmail, group chats and optional reconnect/check-in suggestions are outside this
release. Existing gathering workflows are unchanged.

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

## Verification

`tests/test_suggestions.py` covers workflow, evidence validation, reminders, dismissals,
resolution, worker scheduling, source invalidation, workspace/CSRF boundaries, stale review
and draft races, bounded truncation retry, identity splitting and deadline ordering.
`tests/test_browser.py::test_suggestions_browser` exercises desktop/mobile display, drafting,
edits, clipboard behavior and dismissal through Chromium. Synthetic tests establish these
contracts, not the quality of a live model's recommendations.


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
through their normal conversation review; sending does not blindly mark them done.
