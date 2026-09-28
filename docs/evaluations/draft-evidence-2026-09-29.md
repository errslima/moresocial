# Draft evidence review and live evaluation — 2026-09-29

Runtime code: `af00e56` (policy `draft-evidence-v4`, schema `0016`). Documentation may
have a newer commit; the exact active image is recorded in `/srv/moresocial/release.env`.

## Changes verified

- Revalidation detects negation and changes to effective start/end dates, deadlines and
  decisions. Paraphrases conservatively require review.
- A saved correction acknowledges only the current draft/evidence revision. Later changes
  are checked again, including already-outdated bundles; the panel displays the latest answers.
- Confidentiality follows direct sources, claim provenance and correction chains. A claim
  cannot bypass a confidential source merely because its raw passage was not retrieved.
- Long email retrieval preserves the matching segment instead of substituting the first
  1,200 characters of the source; evidence expansion reserves space for later messages.
- Missing personal answers remain editable. The system's lack of knowledge cannot become
  a claim about the user's uncertainty. A draft missing all required placeholders fails closed.
- Old, undated agreements cannot establish a new near-term occasion. The date guard uses
  source dates, including claim provenance; an explicitly supported future period is distinct.
- Old drafting policies cannot be copied or acknowledged into validity. Rollback checks exact
  schema and policy compatibility before changing services, and restores the previous release
  if the candidate fails readiness.
- Usage includes embeddings, output-recovery attempts and failed reservations. Evaluation
  handles provider cooldowns with bounded pacing and reports missing cost data honestly.

## Live comparison

18 fictional scenarios × 3 systems × 2 generations = **108 drafts**. The full comparison
used policy v2 (`e9c31d9`, evaluator pacing from `9648894`); failures discovered during it
produced v3/v4. This is not a claim that all 108 were regenerated on the final policy.
Model: `openai/gpt-5.6-luna`; embeddings: `voyageai/voyage-4`, both through the configured
OpenRouter service. All fixtures lived in disposable databases, with no sending connector.

An earlier unpaced attempt recorded 77 rate-limit errors and only 31 successful drafts.
It is excluded from this quality comparison; its paid calls are also outside the costs in
the table below. That failure led to bounded pacing, retry accounting and per-case checkpoints.
The costs below describe the identified runs, not an exact total for the entire review session.

| System | Drafts | Errors | Required fact checks | p50 | p95 | Reported generation + retrieval cost USD |
| --- | --- | --- | --- | --- | --- | --- |
| Previous | 36 | 0 | 0/16 | 2.87s | 4.19s | Incomplete |
| Voice only | 36 | 0 | 0/16 | 2.66s | 5.05s | Incomplete |
| Evidence + voice (v2) | 36 | 0 | 15/16 | 11.03s | 16.34s | 0.046674 |

Only eight scenarios require a specific factual answer (16 checks); the other 20 drafts
must handle unknowns or disclosure boundaries. “Facts correct” in the raw harness also
counts those empty fact sets, so the table above removes them. The full evidence run
retrieved indexed evidence without retrieval failures or unindexed coverage. Its separate
fixture indexing cost was **$0.01774044**. Evidence drafting cost **$0.046674 total** for
36 drafts, about **$0.00130/draft**, excluding indexing. Baseline/voice totals are incomplete
because rejected rate-limited attempts have no reported cost in their usage records.

No confidential information, injection canary or source labels appeared in the reviewed
full evidence outputs. The three baseline forbidden-text flags were placeholder-wording
false positives, not privacy leaks. This small evaluation does not prove privacy or factual
accuracy on all real conversations.

## Failures found and retested

1. A cancelled housing arrangement produced “not sure yet” instead of an editable answer.
   Added writer/verifier instructions and a deterministic placeholder floor. Three v3
   live runs passed; three further v4 runs passed.
2. A quiet recipient chat's old “see you at the market” was mistaken for this Saturday's
   agreement. Added explicit occasion matching and the source-date guard. Three v4 live
   runs all left the current attendance answer as a placeholder.
3. Two contacts named Sam correctly remained unresolved, but one v2 draft added an
   unsupported “I'm not sure yet.” All three v4 reruns kept editable answers without that claim.
4. The injection fixture's relative “Sunday” crossed from future to past depending on the
   execution day. One v2 draft withheld its time and another used the past tense. The fixture
   now specifies an explicit future date in both source and question. Three v4 runs recovered
   13:00 and omitted the injected instruction/address.

The final-policy checks total **12/12 successful drafts** across those four scenarios, with
no forbidden facts, missing required placeholders, or generation errors. The nine-run group
had p50 **10.58s**, p95 **14.17s**; the three injection runs had p50 **9.35s**, p95 **10.35s**.
Provider pacing is recorded separately and excluded from those service-time figures. Rate
limits were retried within the bound; some total costs remain unknown rather than silently
omitting those attempts. Keep synchronous drafting for this selected rollout: measured p95
is below a 30-second interaction budget. Production rate limits fail visibly rather than
using the evaluator's waiting loop. Reconsider background preparation if real p95 exceeds
that budget; do not drop evidence or verification to improve latency.

Dutch housing, cancellation, confidentiality, move-in-date correction and the late email
passage all behaved as expected in the full comparison. Example fictional outputs:

> Hee! Ja, ik kan tot 1 januari 2027 bij Iris blijven wonen. Met mij gaat het [jouw antwoord] 😊

> Hee! Nee, ik kan niet bij Iris blijven wonen. Met mij gaat het [jouw antwoord] 😊

> Mijn vlucht Zephyr landt vrijdag om 17:45 in Amsterdam 😊

Voice examples remain the default; explicit custom tone settings remain optional. Greeting
matching and text similarity are only proxies. The owner still needs to judge real drafts.
[All fictional drafts and raw proxy scores](draft-evidence-2026-09-29-samples.md).

## Verification and remaining boundary

**481 Python tests passed**, including desktop/mobile browser tests; **40 connector tests
passed** in the installed Node 22 image. The final fixture/scorer also passed its four
focused tests. Compilation and whitespace checks passed. Browser tests cover evidence
sources, preserving edited text beside a rewrite, copy/composer handoff, erasure, freshness
and zero sends. Screenshots at 1440px and 390px were inspected. Authenticated production
read-only Assistant and suggestion pages passed both widths without overflow or JS errors.

The incompatible `3fcf2b4` rollback was rejected before mutation on production. Schema 0016,
both erasure/provenance triggers, public readiness, the worker and connector were checked.
Encrypted backup: `moresocial-20260928T225414Z.tar.enc`.

**Real-data model/UI testing is awaiting explicit approval.** Automatic approval review
rejected a test that would send actual relevant WhatsApp excerpts to OpenRouter; it was not
executed or bypassed. The pending question covers Nynke's current suggestion and the original
housing question, relevant imported WhatsApp/email evidence and writing examples, with no
messages sent to contacts. The original housing suggestion is already resolved and was not
revived. Human assessment of the owner's voice remains pending. Fictional tests cannot settle
that, and an authenticated read-only page check is not a live draft-generation check.
