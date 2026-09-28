# Evidence-grounded drafts in the user's own voice

Status: implemented locally on 2026-09-29 (M1–M5 complete; M6 verification and rollout in
progress, see the [implementation log](#11-implementation-log)).
Date: 2026-09-28.
Scope: Suggestions reply drafts and shared drafting infrastructure, with compatible
style improvements for proactive prepared messages.

## 1. Outcome and product decisions

When someone asks a question whose answer exists in the user's imported WhatsApp or
email history, MoreSocial should retrieve the relevant evidence, understand the latest
supported decision, and prepare an answer in the way the user normally writes to that
person. The user reviews and explicitly sends the result.

Implement four changes together:

1. Retrieve evidence across the user's sources before drafting.
2. Resolve dates, decisions, contradictions and user corrections before stating facts.
3. Supply representative examples of the user's own writing.
4. Separate what the assistant can consult from what belongs in the outgoing message.

### Recommended style direction

Make **“Match how I write to this person”** the default. The user's proposal is the
right default for this product: warmth, humour, message length and punctuation depend
on the relationship and situation. A fixed global persona should not override that
evidence by default.

Use an instruction along these lines:

> Write as the user would write to this person. Use the supplied examples of the
> user's own messages as the main guide to language, phrasing, warmth, punctuation,
> emoji and length. Adapt to the current situation. Do not imitate the other person's
> voice. Examples demonstrate style only; they do not establish current facts. Follow
> explicit user wording preferences where supplied, without adding facts or commitments.

Changing the prompt alone is insufficient: the current context sometimes contains no
outgoing examples. Example selection is a required part of this change.

Retain optional free-text instructions, preferred/avoided wording examples, language
overrides and contact-specific boundaries. Keep existing tone controls available as
an optional custom mode for users who deliberately configured them; remove their
defaults from the normal drafting prompt. Initiative, check-in intervals, pauses and
event preparation are separate product controls and continue to work as before.

No fine-tuning, additional vector database, autonomous messaging, or unbounded agent
search loop is required. Reuse PostgreSQL/pgvector, existing provider accounting,
source records, jobs and workspace isolation.

## 2. Verified baseline and motivating failure

Read-only investigation on 2026-09-28 compared the relevant deployed code with the
local files. The suggestion-generation, context, retrieval and memory files matched.

The motivating case showed two independent gaps:

- The answer was present in another imported WhatsApp conversation. Its messages
  were chunked, embedded and processed by claim extraction.
- The recipient's draft context contained only the newest incoming message. Older
  outgoing messages were stored but outside the 30-day window. Persona fields were
  defaults, with no wording examples or relationship notes.

Do not copy private production messages, identifiers or housing details into checked-in
fixtures. Use a fictional equivalent throughout implementation and evaluation.

| Existing component | Current behavior | Required change |
| --- | --- | --- |
| `app/memory.py` | Chunks sources, embeds chunks, extracts sourced claims | Preserve pipeline; integrate claim lifecycle and corrections into draft evidence |
| `app/retrieval.py` | Hybrid chunk search used by Ask | Reuse search primitives for bounded draft retrieval; retain Ask behavior |
| `app/suggestions.py:snapshot` | Up to 80 messages from 30 days, plus older open anchors | Keep action detection bounded; build richer draft context separately |
| `app/suggestions.py:draft_context` | Recipient chat, notes, persona and event context | Consume a persisted, validated evidence bundle and selected writing examples |
| `app/assistant_context.py` | Shared settings, destinations and event context | Add shared voice/context contracts without provider calls in read paths |
| `app/proactive.py` | Prepared messages; approved updates control personal news | Share voice improvements; preserve stricter proactive disclosure policy |
| `app/assistant.py`, `assistant_pages.py`, templates | User-authored tone parameters and fictional style preview | Default to conversation voice; migrate preferences and update preview semantics |
| `app/ingest.py`, suggestion invalidation | Erases text derived from changed/excluded sources | Extend dependencies to retrieved sources, claims, examples and new evidence |

The draft model currently has no search tools. Its system prompt requires placeholders
for unknown answers. Supplying insufficient context therefore predictably produces
placeholders, even when searchable evidence exists elsewhere.

Current claim conflict handling is limited: `memory.resolve_conflicts` treats location
and job as single-valued attributes. This does not establish a general understanding of
offers, accepted arrangements, alternatives, cancellations or deadlines. Also,
`valid_from` is derived from supporting message timestamps; it must not be mistaken
for the effective start of an arrangement.

## 3. End-to-end drafting contract

Implement the following bounded pipeline on an explicit draft/rewrite action or an
eligible background preparation job:

1. Snapshot the destination, incoming action, nearby conversation, current settings,
   source/claim revisions and workspace-local date.
2. Identify the questions to answer and named entities from the action and conversation.
3. Retrieve candidate evidence using hybrid search and structured identity/date filters.
4. Expand relevant hits with surrounding turns and check for later updates and corrections.
5. Resolve supported answers and determine which minimal facts may appear in this reply.
6. Select representative outgoing examples and compose the permitted drafting input.
7. Generate the draft plus internal fact references; validate it and its dependencies.
8. Recheck revisions and publish the draft and user-visible evidence explanation atomically.

Provider calls must occur outside long-running database transactions. GET requests,
`draft_current`, copy checks and composer prefill must inspect persisted state without
generating query embeddings or running models. Do not put live retrieval inside the
existing `draft_context` function where every page render would trigger it.

### Evidence bundle

Introduce a versioned, workspace-scoped `DraftEvidenceBundle` representation. Prefer a
dedicated table with a suggestion reference and structured payload over scattering
mutable JSON across existing fields. Set the exact migration number from the current
Alembic head at implementation time (currently `0015`).

The bundle records:

- Suggestion, recipient and exact provider account/conversation; purpose and revision.
- Questions, resolved entity IDs, bounded search scope, retrieval policy/version and coverage.
- Source IDs, versions, source dates, speaker/direction and passage/turn references.
- Relevant claim IDs, statuses and content/lifecycle fingerprints, including corrections.
- Resolved answers: `supported`, `uncertain`, `conflicting`, `missing` or `withheld`.
- Separate statement time, effective interval, deadline and decision state when supported.
- Minimal permitted facts, supporting labels and disclosure decisions/reasons.
- Style example IDs/versions, selected mode, settings revisions and selection coverage.
- Generation/policy versions, dependency revisions, creation time and freshness state.

Keep three concepts distinct: evidence considered, facts allowed in the reply, and facts
actually used. All influential sources count as dependencies, including evidence that
caused a conflicting answer to be withheld. Use separate label namespaces for factual
evidence, permitted facts and style examples. Never make style labels valid factual citations.

Avoid persistent duplicate raw conversation dumps. Prefer source/passage references and
the minimum derived answer text needed for review; apply the same erasure rules to the
bundle as to the draft. Existing `context_sources` remains populated for compatibility.

## 4. Retrieval and factual resolution

### Bounded evidence retrieval

- Start with the incoming questions and conversation context, not just the suggestion's
  generic title. Detect named people through the user's existing identifiers and aliases;
  never merge contacts solely because a name matches.
- Search across included WhatsApp and Gmail sources in this workspace. Reuse Calendar
  and event projections for relevant schedule facts. Do not restrict every search to the
  recipient: the answer may live in a chat with someone else.
- Combine vector similarity, keyword hits, entity matches and recency. Similarity is
  a candidate ranking signal, not proof of truth or permission to disclose.
- Split multi-part messages into at most four focused information needs. Begin with a
  deterministic planner; allow one bounded structured planning call only if evaluation
  shows it is needed. Never accept model-generated SQL or unrestricted query tools.
- Initial limits: at most four query embeddings (batch where supported), 12 unique
  candidate chunks overall, and 40 evidence passages/24,000 characters after expansion.
  Reserve room for surrounding turns, later updates and corrections rather than filling
  the budget with the first semantic hits. Tune against the evaluation set.
- Preserve exact tenant/account/source scoping, source inclusion, current versions and
  embedding-space identity. Extend retrieval's filtering interface as necessary; do not
  filter forbidden providers only after top-k results have already crowded out eligible hits.
- Report available history, truncation, missing embeddings and retrieval failure separately
  from “the user has not said.” Keyword fallback may be used when embeddings are unavailable,
  but record reduced coverage. Never treat a provider error as evidence of absence.

### Dates, decisions and corrections

- Fetch neighbouring turns around relevant statements so that “yes,” “let's do that,”
  offers and refusals are attributed to the correct proposal and speaker.
- Search for subsequent changes in the matched conversation and related user evidence.
  A later timestamp alone does not override an earlier fact about a different subject or period.
- Distinguish a possibility, offer, accepted decision, obligation and completed action.
  A promise to find accommodation by a date does not mean accommodation has been found.
- Resolve relative dates using the source timestamp and relevant timezone. Keep unknown
  years/times uncertain; retain source wording where a calendar interpretation is ambiguous.
- Load relevant user-authored/confirmed corrections independently of vector ranking so
  a short or unembedded correction cannot be missed. Apply rejected, superseded and
  conflicting claim states. Do not reintroduce rejected wording through raw-source retrieval.
- Preserve the relationship between an edited/rejected claim and its replacement. Add
  explicit correction/supersession links if the current schema cannot represent it; the
  current `edit_claim` path does not always retain a direct replacement link. Backfill only
  deterministic relationships, and treat ambiguous historical corrections conservatively.
- Treat user corrections as authoritative within their stated scope; surface credible later
  contradictory evidence for review rather than silently overriding a correction forever
  or silently discarding it. Grouped paraphrases must not become independent votes.
- Use a bounded structured resolution call for evidence interpretation, followed by
  server validation of labels, ownership, versions, allowable statuses and date formats.
  Valid references establish provenance, not semantic correctness; evaluation remains necessary.

For unresolved questions, preserve the known portions of the reply and show the specific
missing/conflicting point beside the composer. Use a clear placeholder only where the
message needs the user's answer. Do not ask for facts already supported by retrieved evidence,
and do not infer today's mood from an old message about being ill or feeling well.

## 5. Voice selection and Assistant page

### Example selection

Select the user's own included, non-deleted outgoing messages from the exact destination
conversation first. Remove the 30-day cutoff for this purpose: older relationship-specific
writing is more useful than an empty set. This does not expand the action review window.

Start with 6–12 examples, at most 8,000 characters total. Prefer representative messages
with substance, a spread of dates and similar communicative situations. Avoid a sample
dominated by “OK,” emoji-only acknowledgements, forwarded text, quoted email, URLs or
media placeholders. Keep short messages where they genuinely represent the user's style;
avoid reproducing accidental typos as a goal. Use available metadata to exclude known
generated/sent draft text from automatically teaching the system its own output; identify
remaining provenance uncertainty rather than claiming all outgoing text is human-authored.

Precedence for wording:

1. Explicit recipient boundaries and applicable user wording/language overrides.
2. User-authored preferred/avoided wording examples and any explicitly selected custom mode.
3. Representative messages the user wrote to this recipient.
4. A minimal general style fallback when recipient examples are insufficient.

Do not import unrelated raw chats into the final writer merely to demonstrate style. For
the first release, fall back to explicit examples or plain, concise wording. A later
general style summary derived from other outgoing messages may be added only if evaluated
and stripped of personal facts, names and third-party content. Sparse-history behavior
must be transparent; don't claim the system has learned a voice it has not observed.

Style examples are untrusted data, never instructions, and are not evidence for current
facts. Even same-recipient examples can contain obsolete plans. Test that their dates,
names, promises and circumstances do not leak into the new draft.

### Settings and migration

- Add `style_mode` with `conversation` as the default and `custom` as an optional mode.
- New users and existing settings containing only default tone values use `conversation`.
  Existing non-default tone values migrate to `custom` to preserve deliberate choices.
  Retain all saved text, language preferences and wording examples in both modes.
- Existing storage cannot distinguish someone deliberately selecting all default tone
  values from leaving them untouched. Document this migration limitation; retain legacy
  values so the user can opt back into custom mode. Do not destroy old columns in this release.
- In conversation mode, omit legacy warmth/directness/humour/length/emoji defaults from
  the writer prompt. Put optional customization behind a secondary UI section.
- Explain the default in ordinary language: “Uses examples of how you write to this person.”
  Keep contact boundaries independent of inferred style; do not invent relationship labels.
- Update preview: a fictional scenario with no contact data previews explicit preferences
  only. Add a recipient-based preview using the same example selector if exposing a true
  “my voice” preview. Clearly label which is being previewed; neither sends anything.

## 6. Knowledge and disclosure

Retrieval permission does not automatically make a fact appropriate to repeat. Separate
the resolver's evidence from the writer's input. The final writer receives minimal
permitted facts, necessary recipient conversation, style-only examples and relevant
event facts—not raw messages from unrelated conversations.

| Information | Policy for an ordinary reply |
| --- | --- |
| Explicit, current fact about the user's own situation answering a direct question | May become a minimal permitted answer, with evidence visible privately to the user |
| Another person's message confirming an arrangement involving the user | May support the user's own arrangement; omit unnecessary quotation and unrelated third-party details |
| Earlier message to this same recipient | May establish already shared context, but check freshness and current boundaries |
| Diary, psychological interpretation, inferred feelings, unrelated third-party news | Excluded from automatic factual retrieval/disclosure in this release |
| Explicit confidentiality, recipient exclusion, sensitive or ambiguous disclosure | Withhold by default and identify the issue to the user; require their deliberate inclusion |
| User-approved shareable update | Respect its audience, expiry and revocation |

Apply deterministic provider/source/audience exclusions before retrieval where possible.
Use a structured disclosure assessment for subtler relevance/confidentiality questions;
it is fallible, so test adversarial examples and default ambiguous cases to withheld.
Being a close contact does not imply unrestricted disclosure permission.

The fictional housing case should yield “I can stay there until the agreed deadline,”
without adding the host's unrelated personal plans. Permission to consult that exchange
and permission to repeat every detail are different decisions.

Preserve the stricter existing proactive policy: unsolicited check-ins/share-update drafts
may introduce personal news only from applicable approved updates. They gain conversation
voice, not automatic access to every newly retrieved life update. Event drafts retain
their existing event-specific evidence constraints. Share the infrastructure with a
purpose-specific policy rather than using one permissive policy for every producer.

Return internal used-fact labels alongside draft text. Reject unknown or disallowed labels.
Check substantive factual assertions for grounding and disclosure before publication;
add one bounded verification pass where needed, with at most one repair attempt. Do not
claim label validation alone proves that free-form text contains no unsupported assertions.
Store uncertainty separately and never append internal evidence labels to the WhatsApp text.

## 7. Freshness, corrections and deletion

- Fingerprints include source versions, claim lifecycle/content, identity mappings,
  disclosure/voice policy versions, settings, destination and workspace-local date.
- New messages may change an answer without editing any previously cited source. Track
  a workspace evidence revision (bumped on relevant ingestion/claim/identity changes),
  alongside precise dependencies. Use conservative invalidation initially: a changed
  evidence revision makes a bundle need revalidation before it can be called current.
  Do not falsely mark every unrelated change as a proven factual contradiction.
- Revalidate asynchronously/on explicit draft actions; never make GET/copy/composer checks
  call providers. A valid recheck with unchanged results may refresh metadata without
  rewriting the user's saved text. Deduplicate work during import bursts.
  Bump evidence revisions only on material evidence changes, not usage accounting,
  bundle publication or an unchanged re-extraction; prevent self-invalidating job loops.
- Include original supporting sources, correction records and style examples in dependency
  tracking. Source exclusion/deletion and permission revocation erase affected derived
  bundle text and drafts, including edits, under the existing source-erasure contract.
- Ordinary new context preserves edited drafts and marks them for review. Regeneration is
  explicit and must not overwrite an edit. Keep a user-controlled rewrite action available
  for old generated drafts; today's `draft()` returns early when a draft already exists.
- Check source/claim/settings/workspace revisions immediately before publication and again
  for copy/prefill/send handoff. Respect leases, concurrent edits, recipient pauses and
  terminal suggestion identities. No draft refresh may reopen a dismissed suggestion.
- After hard erasure, no deleted excerpt may survive in UI metadata, cached bundles,
  verification results or routine logs. Dependency references must be workspace scoped.

## 8. Execution milestones

Work sequentially; finish each milestone's acceptance criteria before marking it complete.
This file plans implementation, not deployment authorization. Preserve unrelated work.

### M1 — Contracts and regression fixtures

- [x] Introduce fictional scenarios for cross-chat answers and older recipient writing.
- [x] Capture current failure: information indexed elsewhere, but absent from draft input.
- [x] Define bundle, question/answer, permitted-fact, style-example and coverage schemas.
- [x] Define purpose-specific disclosure rules and baseline quality rubric.

Acceptance: tests reproduce the context omission without production data or live credentials;
the expected answer distinguishes an accepted temporary arrangement from an earlier alternative.

### M2 — Evidence retrieval and resolution

- [x] Extend `retrieval.py` or add a focused `draft_evidence.py`; keep Ask compatible.
- [x] Implement bounded planning, entity resolution, hybrid search and turn expansion.
- [x] Integrate later-message checks, corrections and claim lifecycle semantics.
- [x] Add bundle persistence, correction lineage where necessary and revision tracking.
- [x] Account for every provider call through existing AI usage/cooldown mechanisms.

Acceptance: fictional facts in another chat and Gmail reach the resolver, correct identities
are preserved, later accepted decisions win over earlier alternatives, corrections work
without embeddings, and incomplete retrieval is represented honestly.

### M3 — Voice from conversation history

- [x] Implement a shared outgoing-example selector and style-only labels.
- [x] Add/migrate `style_mode`; update persona serialization and prompt precedence.
- [x] Simplify Assistant settings and correct preview behavior/copy.
- [x] Apply the same voice selection to ordinary and proactive drafts.

Acceptance: an inactive chat receives representative older outgoing examples; the recipient's
voice is not copied; default tone parameters are absent in conversation mode; custom settings
survive migration; old example facts do not enter the answer.

### M4 — Disclosure-controlled writing and evidence UI

- [x] Build the minimal permitted-fact view and output grounding checks.
- [x] Integrate the pipeline into explicit draft/rewrite actions and applicable preparation jobs.
- [x] Add a compact “What this draft uses” section with source links/dates and unresolved points.
- [x] Keep private evidence out of copied text and composer prefill.
- [x] Preserve proactive approved-update audiences and explicit-send-only behavior.

Acceptance: the fictional reply answers the user's housing question with the current
arrangement, leaves genuinely unknown mood unanswered, and does not expose unrelated host
details. The user can inspect evidence and request a rewrite without losing an edited draft.

### M5 — Lifecycle, race handling and failure behavior

- [x] Extend ingestion, claim edits/rejections, identity changes and source-erasure hooks.
- [x] Implement read-only freshness checks and deduplicated revalidation jobs.
- [x] Cover generation/publication races, exclusions, update revocation and deleted examples.
- [x] Define timeout/cooldown/failure UI; never publish a partially validated bundle as current.
- [x] Add diagnostics limited to counts, durations, coverage codes and policy/model versions.

Acceptance: new contradictory evidence invalidates an old answer; deleted evidence erases
derived content; no GET initiates model calls; concurrent edits survive; retries cannot send
messages, duplicate preparations or revive terminal suggestions.

### M6 — Evaluation, rollout and documentation

- [x] Run focused tests, then the repository's required checks, migrations and browser flows.
- [ ] Compare baseline and new drafts on a bounded fictional live-model evaluation set.
- [ ] Record quality, latency, token cost, retrieval coverage and failures separately.
- [x] Add a rollout switch for evidence-grounded drafting; retain a clearly labelled limited
  context fallback only if its output remains safe and its limitations are visible.
- [x] Update `architecture.md`, `suggestions.md`, implementation status and operator guidance.
- [ ] Prepare migration, release and rollback instructions; perform deployment only when
  authorized by the implementation session's scope.

Acceptance: all deterministic safety/lifecycle gates pass; human review supports better
factual grounding and recipient-specific voice; rollout can be disabled without restoring
invalid/deleted draft text or losing user settings.

## 9. Evaluation and release gates

| Scenario | Required result |
| --- | --- |
| Question answered in a different WhatsApp chat | Correct current answer with inspectable source support |
| Answer exists only in Gmail | Relevant email found; its unrelated contents are not disclosed |
| Recipient chat quiet for over 30 days | Older user-authored examples selected |
| No useful outgoing history | Honest fallback; no invented intimacy or learned-voice claim |
| Earlier option followed by accepted arrangement | Accepted arrangement used; deadline retained |
| Agreement later cancelled or changed | Old answer withheld/replaced after revalidation |
| Short assent following a proposal | Correctly connected to surrounding turns |
| Similar names or multiple provider accounts | No identity/account conflation |
| User correction/rejection versus raw source | Correction respected; rejected claim not resurrected |
| Old style example contains a personal fact | Fact does not become current answer evidence |
| Host mentions an unrelated private matter | Relevant user arrangement used; private matter omitted |
| Confidentiality request or sensitive ambiguity | Withheld or raised for deliberate user inclusion |
| Diary/inferred mood could seem relevant | No automatic disclosure or current-mood inference |
| Proactive check-in after a new personal event | No unapproved personal news added |
| Source deletion, style-example exclusion, claim correction during generation | Publication rejected or safely refreshed; stale text handled correctly |
| New contradictory source not in old dependency list | Evidence revision catches it |
| Rate limit, missing embedding, partial import, truncated evidence | Explicit reduced coverage/failure; no fabricated certainty |
| Prompt injection in evidence or a style example | Treated as data; cannot alter policy or send |
| Ordinary page load/copy/prefill | Zero provider calls |

Use real PostgreSQL/pgvector integration tests for retrieval, isolation and invalidation;
fake providers for deterministic orchestration/schema/race tests; browser tests for
settings, evidence display, rewrite protection and composer handoff. Add targeted cases
to the existing suggestions, assistant context, memory, social assistant and evaluation
suites rather than duplicating implementation logic in tests.

Live language evaluation must compare the existing system, a simple conversation-matching
prompt with examples, and the full evidence-grounded pipeline on identical scenarios and
the same model configuration. Score answer correctness, temporal accuracy, unsupported
claims, unnecessary disclosure, voice match, edit effort and unnecessary placeholders.
Use a small fixed set (initially 12–20 fictional cases, two generations each), with human
review of tone; synthetic providers or a model judge alone cannot establish “sounds like me.”

Release blockers: any cross-workspace evidence, prohibited disclosure, fabricated commitment,
ignored explicit correction, deleted-content retention, automatic send, or silent overwrite
of an edited draft. All answerable factual cases must retrieve their necessary evidence and
retain the right decision/deadline. Require human acceptance of representative voice samples;
report remaining uncertain cases rather than declaring quality solved from aggregate scores.

Initial call budget: up to four batched query embeddings, one resolution/disclosure call,
one writing call, and at most one verification/repair cycle. Reuse valid bundles and selected
examples. Measure p50/p95 latency and per-draft token/cost totals before enabling broadly;
if the request exceeds the existing interactive timeout budget, use the current job system
and show preparation progress. Do not solve latency by silently dropping contradiction checks.

## 10. Rollout and completion record

Use additive migrations. Preserve legacy tone values and existing drafts. Mark old drafts
as using the previous context policy; offer explicit rewriting instead of replacing edits
on deployment. Start with a selected workspace before broader rollout. Do not silently
increase WhatsApp/Gmail history-import limits as part of this work.

Rollback disables new preparation and bundle use, retains migration-compatible data,
preserves exclusions/corrections and continues to block stale or erased drafts. Test that
rolling application code back does not bypass a newer disclosure or deletion restriction;
if that compatibility is not possible, disable drafting during rollback.

Implementation handoff should record completed milestone checkboxes, migration revision,
test/evaluation results, concrete before/after fictional examples, measured costs/latency,
deployment status and remaining limitations. This plan is complete only when its intended
draft behavior and lifecycle guarantees have been verified, not when the prompt is changed.

## 11. Implementation log

### 2026-09-29 — M1–M5 implemented locally

- **Code.** `app/draft_evidence.py` (deterministic planner, identity-safe entity matching,
  bounded hybrid retrieval with provider/space filters inside the queries, turn expansion and
  later-message checks, claim lifecycle and correction loading independent of vector ranking,
  one structured resolution call with server validation and deterministic disclosure guards,
  permitted facts, writer and one verification/repair pass, versioned bundles, read-only
  freshness states, deduplicated revalidation, rollout switch and operator CLI);
  `app/voice.py` (shared outgoing-example selector); `app/draft_eval.py` (live evaluation
  harness). Integrated into `suggestions.py` (bundle-aware `draft_context`/`draft_current`,
  erasure), `proactive.py` (conversation voice with the unchanged approved-update policy),
  `assistant.py`/Assistant page (`style_mode`, secondary custom tone, preview semantics),
  `suggestion_pages.py`/detail page (rewrite, rewrite-beside-edit, "What this draft uses"),
  ingestion, memory, identity and claim hooks for the workspace evidence revision, and the
  worker (`revalidate_draft`).
- **Migration `0016`** (additive): `draft_evidence_bundles`, `evidence_revisions`,
  `assistant_settings.style_mode` (backfilled: default-only tone → `conversation`, any
  deliberate tone → `custom`), `claims.corrects_id` (FK nulls only that column), and two
  triggers that keep the source-erasure contract for bundles even under the previous image.
  Downgrade refuses while correction links exist.
- **Decisions.** Explicit drafts stay synchronous (the budget is one batched embedding call
  plus at most three generation calls); measure live latency before broad rollout. A rewrite
  over an edited draft is held beside it (**Use this version** / **Keep my text**). Saving
  one's own text after an answer went out of date makes that reviewed text usable again.
  Revalidation compares answers structurally and tolerates paraphrase but not a changed
  name, place, number, status, decision, date or disclosure. At most four purely semantic
  candidates per question keep weak neighbours out of the budget. No recipient-based voice
  preview was added: the fictional preview is labelled as showing explicit preferences only.
  Proactive drafts use `proactive-v2`, so open unedited prepared drafts are re-prepared once
  with the new voice after release; edited ones are never replaced.
- **Migration limitation.** Storage cannot tell "chose every default tone" from "never
  changed them"; such users move to conversation mode and keep their tone values for opting
  back into custom mode.
- **Tests.** New `tests/test_draft_evidence.py` (fictional housing, flight, correction,
  namesake, confidentiality, diary/mood, injection, cancellation, budget, race, erasure,
  revalidation and rollback-trigger cases), `tests/test_draft_eval.py`, a `0016` migration
  test, an evidence-draft browser flow at 1440/390 px, and the strict-schema check extended to
  the new schemas. Existing tone tests now select custom mode explicitly; proactive tests use
  a clock inside the preparation window so they no longer fail when the suite runs after
  23:00 (12 such failures reproduced on the untouched baseline at 23:37 local time).
