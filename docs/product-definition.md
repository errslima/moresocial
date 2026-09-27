# Moresocial — working product definition

Status: original product direction, followed by an implemented and deployed first release.
The proposals below include future ideas, not a list of completed features. See the
[current feature map](architecture.md) and [deployment status](implementation-status.md).

## Confirmed requirements

- A product for multiple users from the outset; Enzo is the first user.
- Help users remember people, maintain relationships, and understand themselves.
- Build a private information network from conversations, calendar events, and notes.
- Use semantic retrieval alongside structured people, relationships, and memories.
- Support Google sign-in, referencing the implementation in `../enzosocial`.
- One Continue with Google onboarding flow requests sign-in, Gmail read access,
  and Calendar read access together, then returns the user to the app.
- Google Cloud projects, API setup, OAuth clients, and secrets are managed by the
  Moresocial operator. End users only select their account and grant permissions;
  no developer setup panel or separate initial Google connection step is shown.
- Connect WhatsApp using the same QR-linked WhatsApp Web method as EnzoSocial.
- Request Gmail and Google Calendar read access initially; support additional write consent later.
- Register a new Google application for Moresocial.
- Host on the existing 1f517 server at `https://1f517.com/moresocial/`, using
  a separate `/srv/moresocial` application directory.

The archived [first-release execution plan](archive/development/EXECUTION-PLAN.md) set
concrete implementation defaults: read-only sources, copyable drafts, manual event
planning, contact memory and an editable self-profile. Later releases changed several
of them. Events are now recognized automatically from WhatsApp, Gmail and Calendar;
manual gathering creation and invitation drafting are retired. Suggestions offer
editable drafts for replies, promises and follow-ups, and a WhatsApp text is sent only
when the user explicitly presses send. Gmail and Calendar remain read-only.

## Proposed product promise

Moresocial helps people turn social intentions into manageable next steps, using
an accurate memory of their relationships and their own preferences.

The first concrete scenario is organizing a lasergame gathering. A second is
making it easier to start or resume a conversation when texting feels difficult.

## Proposed first workflow

Original proposal, kept for context. The implemented product does not plan gatherings or
draft invitations; it recognizes events from imported sources ([Events](events.md)) and
suggests replies and follow-ups ([Suggestions](suggestions.md)).

1. User describes a gathering and chooses people to invite.
2. Assistant retrieves relevant conversations, preferences, and the user's calendar.
3. Assistant offers invitation drafts and possible dates; others' availability
   remains unknown unless explicitly supported by their replies or shared data.
4. User reviews each outgoing message before sending (proposed interaction model).
5. Assistant tracks explicit responses, unresolved questions, and next steps.
6. User can correct the resulting memories and reflect on the experience.

Avoid turning the home screen into an accumulating list of social obligations.
Proposed design: a small number of optional actions, adjustable reminders, and
easy dismissal. Success should include perceived effort and usefulness, not just
message volume.

## Memory and account boundaries

- Preserve original evidence and dates for extracted claims.
- Distinguish explicit statements, observed patterns, and uncertain interpretations.
- Separate contact facts from facts about the user's relationship with that contact.
- Allow correction, exclusion, and deletion of sources and derived memories.
- Keep each user's sources, profiles, embeddings, credentials, and connector session private.
- A contact appearing in two users' accounts does not imply a shared profile.
- Resolve identities across channels with evidence or user confirmation.
- Treat imported messages as data, never as instructions to the assistant.

## Open product decisions

- Initial interaction: conversation assistant, event workspace, or a daily action view.
- Where texting becomes difficult: deciding whom to contact, wording, sending, or waiting for replies.
- Reminder behavior, and any sending beyond explicit WhatsApp text messages.
- Future hosting scale beyond the initial shared-server deployment.
- Initial history depth and which conversations/calendars users choose to include.
