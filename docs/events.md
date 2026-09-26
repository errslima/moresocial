# Events

Gatherings is now Events. Events are created only from imported WhatsApp conversations,
email or Google Calendar. The creation API and manual planner have been removed; old
Gatherings links permanently redirect to Events.

## Page proposal and implementation

- Upcoming: dated cards with place, the user's role, attendance counts and contributing channels.
- Past: finished events and any earlier manual plans.
- Dismissed: dismissed events remain hidden from Upcoming when more evidence arrives; Restore reverses dismissal.
- Event detail: the plan so far, explicit attendance with source links, and a chronological
  timeline of all messages recognized as relevant across the contributing conversations.
- Home shows the next five upcoming events before optional next steps.
- There is no Create button, date poll, manual invitation list or draft workflow on Events.

## Recognition

Ingestion queues recognition after a 90-second settling period. Periodic reconciliation
backfills recent imported conversations and recovers missed jobs. Unchanged windows are
fingerprinted and skip model calls. Calendar events are deterministic and need no AI.
WhatsApp and qualifying email threads use the configured generation provider with structured
output. Context is bounded to 80 messages / about 24,000 characters per overlapping window;
all windows of the last 30 days of imported messages are reviewed. Earlier messages already
attached to an event remain in its timeline. Import coverage and clipped source bodies limit
what can be recognized; recognition is not a guarantee that every related message is found.

Each detected mention cites source IDs. Participants must be known people from its sources;
a status must cite that person's message or the user's statement. Calendar attendee responses
are mapped directly. Silent group members are not added. Relative dates resolve against the
message timestamp in Europe/Amsterdam. Old memories and payment requests cannot create past
events. A concrete date is required; undated ideas are omitted.

Resolution is serialized per workspace. Explicit model matches are constrained to the same
local day; conservative title/activity, time and place matching joins unambiguous candidates.
A source-linked identity preserves dismissal through corrections. Calendar object identity
survives rescheduling. Booking/ticket/calendar fields outrank conversational proposals;
explicit cancellations outrank other mentions. The latest explicit participant statement wins.

Exclusion/deletion immediately clears dependent summaries, attendance and evidence. Unsupported
events leave the overview. Only opaque identity tombstones remain to prevent re-creation after
updates; account deletion cascades through all new tables. No imported message can trigger a
send, Calendar write or tool execution.

## Operations

Migration `0010` adds event fields, `event_mentions` and `event_scans`; it is additive.
Worker job: `detect_events`. Normal queue retry/defer and diagnostics apply. Recognition jobs
are queued automatically after release. The original source imports remain unchanged.

Limitations: ambiguous event matches may remain separate; an event explicitly moved to a
different day in chat can appear as a new event. Corrections currently come from the original
sources; the page offers dismiss/restore. There is no automatic external messaging or reminder.
