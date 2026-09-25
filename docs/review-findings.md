# Implementation review — 2026-09-25

Follow-up: all six findings below have now been addressed in application/deployment
code. Nine permanent regressions are in `tests/test_review_fixes.py`, including
migration and retry cases. See `docs/implementation-status.md` for current verification.
The descriptions below preserve the original review findings and reproduction evidence;
they describe the pre-fix implementation, not outstanding defects.

## Verification

- Existing Python suite: 72 tests passed, including real temporary PostgreSQL and
  the Playwright browser test. Executed via `.venv/bin/python -m pytest -q`.
- Existing Node suite: 19 tests passed using Node 22.23.2.
- Six additional review probes: all six failed against the pre-fix implementation,
  reproducing the findings below. They use synthetic data and fake Docker where noted.
- Initial sandboxed runs could not create the PostgreSQL lock file or local HTTP
  listeners; rerunning with the required filesystem/listener access passed the baseline.
- Docker is unavailable here. No image build, real container rollout, live provider
  connection or server deployment was performed. No real account data was used.
- Installed Anthropic SDK 1.8.0 does accept the `fallbacks` and `output_config`
  arguments used by the adapter. Their presence is not itself a defect. Actual
  generation/embedding responses, fallback usage accounting and live quality remain
  unverified; the real adapters still need mocked transport/SDK coverage.

The original probes have been promoted into the permanent suite.
`docs/review-probes.py` forwards to those tests for compatibility. Run normally:

```bash
.venv/bin/python -m pytest tests/test_review_fixes.py -q --tb=short
```

The permanent regressions now pass. The restore regression executes the SQL file
used by the script against disposable Postgres and cleans up its test database/roles.

## R1 — P1: blocking AI calls stall both HTTP listeners

Location: `app/pages.py:230` / `app/pages.py:236`; same pattern in drafting,
rewriting and RSVP handlers. `app/serve.py` runs public and internal Uvicorn servers
on the same asyncio event loop.

The handlers are `async def` but call synchronous retrieval/provider functions
directly. Blocking HTTP requests, SDK retries and semaphore waits therefore block
the event loop itself. This is not merely one slow response or a larger-scale
connection-pool concern: one user asking a question can prevent all users, health
checks, connector ingestion and the provisioner's management requests from being
served until the provider call finishes. A batch RSVP check compounds the delay.

Reproduction: a 200ms substitute for retrieval.ask delayed an event-loop callback
scheduled for 20ms until approximately 210ms. Real provider timeouts can be much longer.
Probe: `test_slow_ask_allows_event_loop_to_progress`.

Fix: move long AI work to the job runner with a result/status flow, or make the
complete synchronous request operation run in a worker thread with safe DB-session
ownership. Audit all async handlers for blocking I/O, including connector logout
and Google revocation. Verify `/health` and internal ingestion remain responsive
while a generation request is deliberately held open.

## R2 — P1: deleting the final account leaves its WhatsApp session running

Location: `deploy/provisioner.py:200`; related account deletion in `app/accounts.py`.

Account deletion cascades away the connector's desired-state row and relies on
orphan reconciliation to remove its container and session directory. Reconciliation
returns early whenever `workspace_count` is zero. Consequently deleting the only
user (or the last remaining user) never stops/removes their connector, even after
repeated complete, successful management responses. This is particularly relevant
when Enzo is the first user.

The application rejects later ingestion because the key's row is gone, but the
linked WhatsApp session remains active and can continue collecting packets into
its local queue. The UI has already reported account/data deletion.

Reproduction: create one running connector with the existing FakeDocker, return
`complete=true, workspace_count=0, connectors=[]`, reconcile five times. The
container remains running and its directory is retained.
Probe: `test_last_account_connector_is_cleaned`.

Fix: persist explicit cleanup tasks/tombstones independently of the deleted
workspace until the provisioner acknowledges deletion. Preserve the safeguard
against wiping sessions after an accidental empty database; do not just treat any
empty listing as authorization to delete all resources. Add a final-user deletion test.

## R3 — P1: Gmail advances history past unprocessed messages

Location: `app/google_sync.py:362` and `app/google_sync.py:377`.

Incremental sync collects all added message IDs but slices them to
`gmail_max_messages` (500). It then advances the saved cursor to the latest history
ID after processing that slice. If more than 500 messages arrive between polls,
including during downtime, the remaining messages disappear from subsequent
incremental syncs. No truncation state or resumable backlog records the omission.

Reproduction: return 501 additions in the history response. Only 500 messages are
fetched, while the cursor advances to the new history ID.
Probe: `test_incremental_does_not_advance_over_dropped_messages`.

Fix: process all covered IDs in bounded, durable batches before committing the
new cursor, or persist a backlog/page position and resume it. Merely displaying a
truncation warning still leaves an avoidable permanent gap. Add a multi-page burst
and crash/retry case exceeding the configured limit.

## R4 — P2: long-message retrieval discards the passage that matched

Location: `app/retrieval.py:99`; related extraction prefix truncation at
`app/memory.py:249`.

Ingestion splits a long email into chunks and embeds each chunk. But evidence_for
does not use the selected chunk's text: it reloads the original source and takes
only its first 1,500 characters. A match in the later part of an email therefore
feeds unrelated opening text to the answer model. Deduplicating by source ID also
prevents multiple useful passages from the same source being retained.

Extraction has the analogous problem: every chunk of a long source is processed
using the source's first 5,000 characters rather than the chunk's actual segment.
It repeats work on the prefix and misses later facts.

Reproduction: create a 7,200+ character email with a unique fact at its end. The
matching chunk contains the fact, but evidence_for on that exact chunk does not.
Probe: `test_retrieval_preserves_matched_tail`.

Fix: preserve segment offsets/text and per-source attribution in chunk membership,
and use the selected segments for both answer evidence and claim extraction.
Keep citations tied to the correct original source version. Add long-email tests
with facts in several segments, not only short synthetic messages.

## R5 — P2: the provided restore script cannot create its target database

Location: `deploy/restore.sh:26`.

The script sends two CREATE ROLE statements and CREATE DATABASE in one `psql -c`
string. PostgreSQL executes that string as one transaction, and CREATE DATABASE
is forbidden there. With `set -e`, every restore exits before pg_restore runs.
The current backup test separately creates the database in autocommit mode, so it
does not exercise this faulty script step.

Reproduction: run the same statement shape, with unique temporary names, against
the disposable PostgreSQL instance. It fails with:
`ERROR: CREATE DATABASE cannot run inside a transaction block`.
Probe: `test_restore_command_can_create_database`.

Fix: use separate `-c` invocations (or createdb), then run the actual shell restore
path in a disposable Docker integration test. Also make the reported migration
head comparison an assertion; printing both values is not verification.
Reference: [PostgreSQL psql command processing](https://www.postgresql.org/docs/18/app-psql.html).

## R6 — P2: existing WhatsApp connectors never change release image

Location: `deploy/provisioner.py:118` through its return at line 135.

ensure_running only checks container existence and running status. It uses the
requested image when first creating a container, but never compares that image
to the existing container. release.sh/rollback.sh restart the provisioner expecting
it to reconcile the configured revision; running connectors instead remain on
their old code indefinitely. New users can get the new connector while existing
users retain old bugs or an incompatible protocol.

Reproduction: reconcile a connector at revision A, change the management response
to revision B, reconcile again. FakeDocker records no replacement using B.
Probe: `test_connector_revision_is_reconciled`.

Fix: inspect the running image identity/configuration, gracefully recreate when
the desired revision changes, and preserve the workspace's LocalAuth/session
mount. Cover both forward release and rollback, followed by a real Docker test.

## Remaining verification gaps

The six findings above were confirmed and are now fixed. Separately, production
readiness still requires the pending real Google consent, WhatsApp pairing/restart,
generation/embedding adapter checks, Docker builds and rollout, and actual backup
restore. The handover's GET side effects, identity-unlink heuristic and concurrent
token-refresh handling also merit follow-up.
