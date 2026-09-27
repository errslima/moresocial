# Diary entries and semantic observation groups

> **Archived 2026-09-27.** Historical development document, kept for its rationale and
> evidence. Current docs and code take precedence wherever they conflict; its
> instructions are not current tasks. See the [archive index](README.md).
>
> Implemented in `6c762e3` (migration `0007`), the production revision before the
> 2026-09-26 Suggestions release. Invitation drafting mentioned below has since
> been retired.

The About me page accepts one dated diary entry at a time. Original text is stored as
`Source(provider='diary', from_me=True)` and each entry has its own conversation/chunks.
It uses the existing embedding, extraction, citation and source invalidation pipeline.
Thoughts remain searchable even when no durable observation is extracted. Submission
IDs prevent duplicate form retries; fresh edit versions also handle reverting to old text.
The page includes indexing status, paginated history, editing and deletion.

Migration `0007` preserves nonempty legacy preferences/goals as dated diary sources,
without clearing the old profile fields. Rebuild jobs are queued in the same transaction.
No historical messages are deleted or automatically reinterpreted by the migration.

## Grouping

`app/observation_groups.py` embeds observation text in the active embedding space and
shortlists up to five groups belonging to the same subject and workspace. The reasoning
model distinguishes equivalent, related, contradictory and uncertain observations using
all member wordings and dated evidence excerpts. Only equivalent observations are grouped;
no cosine threshold can independently authorize a merge. Categories may differ.
Historical/conflicting lifecycles stay separate. Groups are capped at 40 members.

Original claims and their source links remain intact. A group displays a deterministic
representative (confirmed/user wording first) and the union of unique, current sources.
Grouped originals can be expanded. Confirm/reject apply to the whole group; edits reject
the old group and preserve the user's corrected claim. Rejected groups participate in
matching to suppress equivalent paraphrases. Conflicting user confirmations/rejections
are not silently resolved. Ungroup keeps the originals separate and disables automatic
regrouping for those records. Source deletion prunes only that source's support; confirmed
memories continue to follow the existing explicit-user-endorsement retention rule.

Generation occurs outside database transactions. Publication locks/rechecks source
versions and group membership, and serializes membership changes within a workspace.
Stale decisions retry rather than overriding edits/deletions. Only one candidate group
is joined per job, avoiding unchecked transitive bridges between candidates. Both people
pages and invitation drafting consume the shared grouped observation view.

New extractions enqueue grouping; the worker's ten-minute reconciliation backfills
existing observations in batches of 200 and reindexes after embedding-space changes.
Each job is idempotent and uses existing retry/billing controls. Grouping is intentionally
conservative, so uncertain or historical duplicates may remain separate. Changing
`VERSION` permits a deliberate future recheck. No user text is logged.

## Verification and rollout

- `tests/test_diary_groups.py`: source/embedding lifecycle, retry deduplication, text
  reversion, old profile migration, grouping/evidence union, confirmed text, rejection,
  ungrouping, workspace isolation, invalid output and deletion/edit races.
- `tests/test_browser.py::test_diary_flow`: desktop and mobile submission/edit/delete.
- Focused suite: 44 passed; connector suite: 27 passed. Full suite retained the 22
  documented pre-existing retired personal-key/Anthropic failures.
- Live-model synthetic probe: equivalent paraphrase grouped; related, contradictory
  and temporary statements remained separate. This is a small quality check, not a
  guarantee of perfect semantic classification.

Before rollout, take and scratch-restore an encrypted backup. Stop the old worker before
migration so it cannot claim newly introduced `group_claim` jobs. Run the standard
release script, then check `/ready`, schema `0007`, diary migration counts and grouping
queue/results using counts only. The worker starts the existing-observation backfill
without requiring a web request. Existing user data never needs to be copied locally.
