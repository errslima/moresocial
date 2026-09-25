# Automatic events — execution plan

Status: E0 implemented 2026-09-25 (see `docs/implementation-status.md`); E1–E6 not
started. Prepared 2026-09-25 from a review of the code and of
the production data (aggregates plus one real upcoming event, used only to shape this plan;
no real message content belongs in the repository or tests).

## 1. Outcome

Moresocial recognizes upcoming events in the user's WhatsApp messages, emails and Google
Calendar and saves them without manual entry. Each event has a date/time, place, the
user's **role** and **participants** with a colour-coded status. Upcoming events appear at
the top of Home. "Gatherings" is renamed **Events** everywhere in the product.

Reference scenario (from production, anonymized): the user organizes lasergaming on a
Sunday. They post "who wants to come lasergaming on Sunday?" in a group, invite several
friends 1:1, collect payment, and receive a venue booking confirmation by email. Replies
arrive over several days in 1:1 chats and the group: "Yes, I'm in!", "not sure I'll make
it", "unfortunately cannot make it", "I have to work", "I'm travelling on Sunday sorry",
and one person who saw the group post later asks to join and says yes. Chat messages
mention 3pm, 4pm and 5pm; the booking email says 16:15–17:00 for 8 people. An older
payment request mentions a *past* lasergame and must not create an event.

## 2. Decisions (confirmed with the user 2026-09-25)

| Topic | Decision |
| --- | --- |
| Creation | Detected events are added directly to Upcoming with a "detected" badge; the user can edit or dismiss. A dismissed event is never re-created; later mentions attach to it silently. |
| Scope | Events the user organizes; events they are invited to or attend; tickets and bookings from email (even without chat); Google Calendar events. |
| Statuses | 🟢 confirmed, 🟠 interested / not sure, 🔴 not joining, ⚪ invited, no reply yet. Colour is never the only signal: each chip also carries a text label. |
| Updates | Statuses update automatically from new messages, with the message as evidence. Anything the user set by hand is locked and never overwritten. |

Defaults chosen in this plan (change before implementation if needed):

- Home shows events in the next 30 days (then "Show later events"); past events leave
  Home after they end and remain on the Events page.
- Group members who did not respond are **not** listed; a group post invites the group,
  not each member. People the user invited 1:1 without a reply are ⚪.
- Field precedence: user edit > booking/ticket/calendar > latest statement by the
  organizer > other statements. Newer beats older at equal rank.
- Status comes only from explicit statements by that person (or the user stating it for
  them: "Sam is in"). Silence never produces 🟢, 🟠 or 🔴.

## 3. Findings that must be fixed first (E0, E1)

1. **Embedding budget leak.** With no embedder configured, `rebuild_conversation` still
   enqueues embed jobs and `ai.embed` reserves tokens, then fails and keeps the reservation
   (unknown cost). This used up the whole operator budget on 2026-09-25 (400,000 of
   400,000), so the global tier could not run until the next UTC day. Fix: skip embed jobs
   and reservations when `embedder().embedding_dimension == 0`; release the reservation for
   failures raised before any provider request (configuration/`AIUnavailable` before send).
2. **Silent provider failures.** 17 calls on the user's OpenAI key failed with no log line
   (likely HTTP 429). Emit `ai_generation_failed` with provider and status (never content)
   for rate limits too, and cap concurrency per provider during backfills.
3. **Unnamed group senders.** Group messages carry only WhatsApp's internal `@lid` sender id,
   so a group reply cannot be tied to a person. The connector must send each group
   message's sender display name and, where the pinned `whatsapp-web.js` version allows,
   the sender's phone-number id; ingest links both to the person. Verify the available API
   in the pinned 1.34.7 before relying on it.
4. Without a Voyage key there is no semantic search; event matching below does not depend
   on embeddings.

## 4. Data model — migration `0004_events`

The `events`, `event_people` and `event_candidates` tables already back Gatherings.

- `events`: add `origin` (`manual` | `detected` | `calendar`), `starts_at`, `ends_at`,
  `all_day`, `place`, `my_role` (`organizer` | `co_organizer` | `invited` | `attending` |
  `ticket_holder`), `state` (`upcoming` | `cancelled` | `past` | `dismissed`),
  `dismissed_at`, `locked_fields` (JSON list of fields the user edited), `field_sources`
  (JSON: field → source id used), `updated_at`. Existing gatherings migrate to
  `origin='manual'`, times from their chosen candidate.
- `event_people`: keep the status column and map it onto the colours: `accepted` 🟢,
  `maybe` 🟠, `declined` 🔴, `awaiting_reply` ⚪ (`not_contacted` remains for manual
  planning). Add `status_origin` (`detected` | `user`), `status_source_id` (evidence) and
  `status_at` (time of that evidence, for newer-wins).
- New `event_mentions` (derived data, like claims): workspace, `conversation_key`,
  `event_id` (nullable until resolved), `kind` (`plan` | `invitation` | `booking` |
  `ticket` | `reminder` | `cancellation`), `activity`, `title`, `starts_at`, `ends_at`,
  `date_precision` (`exact` | `day` | `approximate`), `place`, `my_role`, `participants`
  (JSON: person id, status, evidence source ids), `evidence` (source ids), `input_hash`.
  Mentions are replaced when their conversation window changes and removed with their
  sources (exclusion and deletion rules as for claims).

## 5. Detection pipeline

### 5.1 Calendar (deterministic, no model)
Each synced calendar event becomes or updates an `origin='calendar'` event: title, time,
place, attendees mapped by email; `my_role` organizer/attending from the calendar data.
Attendee response status, where Google provides it, maps to the colours.

### 5.2 Conversations (model)
New job `detect_events` per conversation (WhatsApp chat or Gmail thread), debounced about
two minutes after the last change and skipped when the input hash is unchanged. Input:
that conversation's messages from the last 21 days plus any message that references a
future date, bounded (about 80 messages / 12,000 characters), with timestamps, speaker
labels (ME, P1, P2, …) and message labels. The prompt keeps the existing rules: untrusted
data, no tools, cite labels.

Output (strict JSON schema, valid for Anthropic and OpenAI): a list of event mentions with
activity/title, date and time resolved against the *message* timestamp plus the original
phrase, place, the user's role, whether the event is past, and per-participant status with
cited message labels. Server-side validation drops mentions whose dates fall outside
[message − 1 day, message + 365 days], statuses without a citation from that person (or
from ME about them), and participants that are not speakers or named contacts.

Email pre-filter to control cost: threads from people the user corresponds with, or bulk
mail matching booking/ticket/reservation patterns (multi-language, e.g. "booking",
"reservation", "ticket", "reservering", "bevestiging") or containing a date and time.

### 5.3 Resolution (merge into events)
Job `resolve_events` per workspace after detection:
1. Candidate match: same local day (or overlapping time range), plus activity/title
   similarity or shared participants, excluding past events. Calendar and booking
   mentions anchor a cluster.
2. Ambiguous cases (two same-day events, or no date on one side) go to a small model
   call comparing the two summaries. Never merge across different days.
3. Build or update the event using the §2 precedence; record `field_sources`; never
   change `locked_fields`.
4. Participants: per person, the newest explicit statement wins unless the user set the
   status. 1:1 invitation with no reply yields ⚪. The user is shown by role, not as a
   participant chip.
5. Lifecycle: after `ends_at` (or the end of the day) → `past`; a cancellation mention
   (for example a booking cancellation email) → `cancelled`, shown struck through;
   dismissed events stay dismissed.

## 6. Product changes

- Rename Gatherings → Events: nav, headings, copy, routes `/events…` with permanent
  redirects from `/gatherings…`. Manual creation, date polls and drafts keep working.
- Home: an **Upcoming** section first: date and time, title, place, role badge, a
  "detected" badge, participant chips (colour + label + count per colour), Dismiss.
- Event page: each field shows its evidence ("from the booking email", "from your message
  on Thursday") and is editable (edit = locked). Each participant shows the message behind
  their status; the user can change it (locks it) or remove the person.
- Next steps on Home use the new statuses (for example "2 people haven't answered").

## 7. Milestones and acceptance

| Milestone | Content | Acceptance |
| --- | --- | --- |
| E0 | Budget leak, failure logging, per-provider pacing | Tests: no reservation without embedder; pre-send failure releases; 429 is logged. Deployed promptly. |
| E1 | Group sender names and phone ids | Connector + ingest tests; live: a group reply shows a person's name. |
| E2 | Migration `0004`, rename to Events, calendar events on Home | Existing gathering tests pass under `/events`; redirects; calendar event appears on Home. |
| E3 | `detect_events` job, schema, validation | Synthetic fixture of the §1 scenario yields correct mentions; the past payment request yields none; injection text yields none. |
| E4 | `resolve_events`, statuses, lifecycle | Fixture resolves to one event at the booking time with 🟢🟢🟠🟠🔴🔴🔴 as in §1; a later "can't make it after all" turns 🟢→🔴; a user-set status survives. |
| E5 | Home Upcoming, event page evidence, dismiss/edit | Browser tests at desktop and 390px; colour is never the only signal. |
| E6 | Production backfill and live check | Upcoming shows the real lasergame on its booking time with participants matching the user's own reading; cost and false positives recorded in `implementation-status.md`. |

Cost estimate: one detection call per active conversation change (roughly 3–8k input
tokens), plus a one-off backfill of about 50 chats and the filtered email threads, which
is a few dollars on `claude-sonnet-5`. It runs on the user's key when present.

## 8. Out of scope

Writing events to Google Calendar, sending messages or reminders, reading WhatsApp status
updates or voice notes, and inferring attendance from silence or payments alone.
