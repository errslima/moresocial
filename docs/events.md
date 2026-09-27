# Events

Event identity (one outing = one event, reversible grouping and corrections) is implemented per the
archived [execution plan](archive/development/event-identity-execution-plan.md); see **Identity and
grouping** below.

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
email whitespace is compacted before truncation so full booking tables remain in context;
all windows of the last 30 days of imported messages are reviewed. Earlier messages already
attached to an event remain in its timeline. Import coverage and clipped source bodies limit
what can be recognized; recognition is not a guarantee that every related message is found.

Each detected mention cites source IDs. Participants must be known people from its sources;
a status must cite that person's message or the user's statement. Calendar attendee responses
are mapped directly. Silent group members are not added. Relative dates resolve against the
message timestamp in Europe/Amsterdam. Old memories and payment requests cannot create past
events. A concrete date is required; undated ideas are omitted.

Resolution is serialized per workspace (advisory lock). Booking/ticket emails outrank Calendar
and conversational booking reports; those outrank proposals. The latest explicit participant
statement wins.

## Identity and grouping

Each detected record is a stable **original identity** (`events` row) with its mentions. A rescan
keeps a record's identity when it cites the same sources (any scan window, including tombstoned
identities) and the plan context agrees (title overlap, or same time and mostly the same messages).
A message that discusses two plans can support two events. A model `match_id` is only a hint for
the resolver; it never merges records.

Records describing one outing form a **group**: a connected component of applied *same*
decisions (`event_identity_decisions`), materialized as `events.canonical_event_id` (NULL =
representative). Only representatives appear in Home and Events; the detail page aggregates all
members' sources, and an old member URL answers with a **temporary (307)** redirect so a later
split can give it its own page again. Pointers are rebuilt deterministically from active
decisions: user decisions outrank automatic ones, user joins are processed first, and a join is
refused if **any** cross-group pair is marked different (A=B and B=C never override A≠C). An
existing representative is kept when it stays in the group; otherwise the oldest record is chosen.

Decisions come from:

- **You** — *Same event* / *Keep separate* in the Possible duplicates list, *Same outing as another
  detected event* (a missed match; it groups detected records, it never creates an event),
  *Separate…* (split a group; shows which messages move) and *Undo…* in the grouping history
  (shows the actual result: another path may still keep records together). Contradicting an
  earlier correction is refused with the correction to undo first. All forms need sign-in, CSRF and
  an unchanged-state token; foreign IDs are 404.
- **Lexical match** (automatic, `via=lexical`): the previous conservative title/time/place match,
  now a reversible decision instead of an irreversible merge.
- **Resolver** (automatic, `via=resolver`): the `resolve_events` job (below).

A split supersedes crossing *same* decisions and records *keep separate* across the partition;
decisions inside each part remain. New evidence stays with its original identity through merges,
splits and undo. **Dismissal:** a group is dismissed if any member is; Dismiss/Restore on a group
applies to all current members; after a split each record keeps its last applied state.

### Field selection and provenance

A group's card is projected from all current member mentions, field by field: schedule, title and
place from the strongest confirmed fact (booking email > Calendar > chat booking > proposal); a
later explicit `reschedule` supersedes. An organizing invitation sets the organizer role even when
a ticket email carries the booking. Attendance is the latest explicit statement per canonical
person (people linked later are followed through the evidence author). Cancellation holds only
while it is the latest statement, so an explicit reinstatement is representable. Earlier proposed
times appear as history; two confirmations that disagree show *Needs checking*. `details.provenance`
records the source IDs behind each field; only included, non-deleted sources are shown.

### Resolver (`resolve_events`)

Queued after recognition changes, source invalidation and corrections, and hourly per workspace by
periodic reconciliation (backfill/recovery of upcoming and last-30-day records; explicit targets
from the operator tool may be older). Candidate retrieval is broader than merging: shared booking
references, payment/booking links, distinctive links, copied invitation text, shared messages,
extraction hints, and same day with compatible activity or shared people. Date and proximity only
retrieve candidates. URLs are normalized locally (host lowercased, tracking parameters and fragments
dropped, IDs kept) and hashed; nothing is fetched or logged. Map links and venue homepages are weak.

Per job: at most 20 pairs; per packet at most 40 messages / ~24,000 characters (shared messages,
then messages carrying shared references, bookings, your messages, newest). Short replies get the
preceding message as context (X labels, not citable). The model returns same/different/uncertain
with cited labels; server validation decides: *same* groups automatically only with a distinctive
shared reference (payment, booking, reference, copied text, shared message, distinctive link) cited
on both sides, compatible context (same day, or an explicit reschedule/proposal→confirmation) and
no hard conflict; otherwise it is stored as *uncertain*. Malformed output or forged citations create
no decision (at most two attempts per unchanged pair); provider failures follow normal job retry.

Model calls run outside the lock with lease renewal. Afterwards the pair's **fingerprint** (both
groups' member IDs, sources and content hashes, mention data, participant identity mappings, user
corrections, resolver version and prompt) is recomputed under the lock; changed input discards the
result and re-queues. An unchanged pair is never paid for twice (`event_pair_scans`). Overflow and
stale pairs continue in new jobs. Automatic decisions citing a changed or removed source are
withdrawn immediately and groups rebuilt; user corrections keep their identity link but never make
removed content visible.

**Modes** (operator setting `event_resolver_mode`, default `observe`): `off` makes no new automatic
decisions (existing groups stay); `observe` stores verdicts and offers them as Possible duplicates
without grouping; `apply` groups validated *same* verdicts. Entering `apply` promotes still-active
observed *same* verdicts without new model calls. Lexical matches apply in `observe` and `apply`.
Possible duplicates are shown only with more than a shared date (a shared reference or message, a
hint, or compatible activity/shared people within three hours).

Exclusion/deletion immediately clears dependent summaries, attendance and evidence. Unsupported
events leave the overview. Only opaque identity tombstones remain to prevent re-creation after
updates; account deletion cascades through all new tables. No imported message can trigger a
send, Calendar write or tool execution.

## Operations

Migration `0010` adds event fields, `event_mentions` and `event_scans`. Migration `0011` adds
`events.canonical_event_id`, `event_identity_decisions` and `event_pair_scans`; it is additive and
existing events start as singleton groups. Both cascade on account deletion.

Worker jobs: `detect_events` and `resolve_events`, with lease renewal during slow provider
responses; normal queue retry/defer and diagnostics apply. `events_resolved` diagnostics report
counts per run (candidates, cached, settled, evaluated, same/different/uncertain, rejected,
invalid, stale, grouped, continued), never content.

Operator repair and reporting: `scripts/reconcile-events.py` (see
[operator setup](operator-setup.md#event-identity-repair)). Dry run is the default.

Rollback: switching the mode to `off` stops new automatic decisions but keeps existing groups. To
reverse bad automatic grouping use `withdraw-automatic` (dry run first); user corrections and all
evidence are kept. Pre-identity application images ignore `canonical_event_id` and would show
duplicate cards again, and their `/ready` reports a migration mismatch; prefer the mode switch and
a forward fix. Never drop the new tables or delete evidence as an emergency rollback.

Limitations: resolver accuracy was evaluated on a bounded sample, not proven in general; ambiguous
records stay separate until you decide. There is no automatic external messaging or reminder.
