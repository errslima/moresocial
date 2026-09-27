# Gathering schema cleanup: release and rollback

Prepared 2026-09-27. Revision `0012` removes only group G: the empty manual gathering
tables and `answer_sources.draft_id`. Group K remains: production inventory found
one Anthropic and one OpenAI connection, both with encrypted keys/metadata. Legacy
operator keys, usage history, profile conversion and all automatic Events are retained.

The 2026-09-27T00:14:55Z read-only inventory found zero `drafts`, `event_candidates`,
`event_people` and draft/ambiguous answer references. It found 57 automatic Events,
zero legacy Events, and no outstanding user-billed reservations. Those counts are
dated evidence, not substitutes for migration-time guards.

## Release procedure

The tested schema-compatible cleanup is `f6cf3eb` at schema `0011`. Migration 0012 is
non-additive: old web/worker images cannot run after its tables are removed.
Use the following order with operator SSH access. All commands run on the MoreSocial
host and refer only to `/srv/moresocial`, the `moresocial` Compose project and managed
MoreSocial connector containers. Do not touch other projects.

1. Transfer a clean Git checkout/bundle of the tested release. Record its full SHA.
   Build both images **before** stopping anything:

   ```bash
   docker build -t moresocial-web:$sha -f deploy/Dockerfile .
   docker build -t moresocial-connector:$sha connector
   ```

2. Verify the encrypted backup with `deploy/restore.sh --scratch` using its documented
   positional arguments. On the disposable restored database, run the new image's
   `alembic upgrade head`, `alembic downgrade 0011`, then `alembic upgrade head`.
   Compare aggregate counts/digests of retained rows before/after; do not expose
   credential values or private content. Remove only the disposable restore resources.

3. Record the current source path and release environment for recovery. Set up shell
   arrays using the actual source checkout and `/srv/moresocial/release.env`:

   ```bash
   previous=$(readlink /srv/moresocial/src)
   old=(docker compose -p moresocial -f "$previous/deploy/compose.yaml" --env-file /srv/moresocial/release.env)
   systemctl stop moresocial-connectors.service
   mapfile -t connectors < <(docker ps -q --filter label=moresocial.managed=1)
   ((${#connectors[@]} == 0)) || docker stop -t 30 "${connectors[@]}"
   "${old[@]}" stop -t 180 worker
   "${old[@]}" stop -t 30 web
   ```

   Keep the database running. Stopped connectors retain their session mounts and
   durable queue. Gracefully stopped jobs finish; interrupted work recovers by leases.
   Do not remove volumes, connector containers, sessions or queues.

4. Take a fresh encrypted backup while writers are stopped. The backup script will
   leave already-stopped connectors stopped. Record retained table counts/digests
   immediately before the migration for comparison before restarting writers.

5. From the new checkout, invoke the migration using a separate release env file
   containing the candidate SHA. Use the candidate Compose file and its `migrate`
   profile. Do not change the active release pointer yet:

   ```bash
   printf 'MORESOCIAL_REVISION=%s\n' "$sha" > /srv/moresocial/schema-release.env
   candidate=(docker compose -p moresocial -f "$checkout/deploy/compose.yaml" --env-file /srv/moresocial/schema-release.env)
   "${candidate[@]}" --profile migrate run --rm --no-deps migrate
   ```

   The migration takes bounded table locks and refuses all nonempty or ambiguous
   legacy candidates atomically. It performs no key deletion, provider calls or data
   conversion. Compare retained table counts/digests while writers remain stopped.

6. Run `deploy/release.sh "$sha"` from the clean candidate checkout. Images were
   already built; the second migration call is a no-op. This activates the new
   web/worker and restarts the connector provisioner. Explicitly start the previously
   running managed connectors if the provisioner has not already replaced/restarted
   them; verify their readiness and preserve their sessions.

7. Check `/health`, `/ready`, schema head, web/worker status, provisioner, and connector
   observed readiness. Run the aggregate inventory: G must be `removed`, K remains
   `ineligible`. Check authenticated Home, Connections, People, WhatsApp, Suggestions,
   Events, About me, Ask and Admin at desktop/mobile widths without mutations.
   Revoke the temporary review session. Record the deployed revision and actual results.

## Failure and rollback

- If the guarded migration refuses, schema remains `0011`. Restore the old web/worker
  using the recorded old Compose/env, restart only connectors that were running,
  and restart the provisioner. Investigate the fixed reason code; never bypass guards.
- If migration succeeds but activation or validation fails, keep writers stopped.
  Use the **new** image's migration service to `alembic downgrade 0011` (it knows the
  downgrade), restore the old release env/pointer, then start the old services and
  provisioner. Confirm readiness. Do not start an old image on schema `0012`.
- The downgrade restores the removed empty structures and leaves newer active data
  intact. Do not restore an old database backup over user writes as an image rollback.
- The updated `deploy/rollback.sh` refuses an image without migration 0012 when it
  detects the removed gathering schema, before modifying release state. Older copies
  of that script do not contain the guard: use this coordinated procedure instead.

The operator must provide the actual candidate SHA/checkout to these commands; this
document does not identify an untested working tree as deployable.

## Execution evidence — 2026-09-27

- Runtime cleanup `f6cf3eb` reviewed and deployed first; one inventory bug fixed:
  all gathering tables absent with `answer_sources.draft_id` still present is an
  inconsistent schema, not a completed removal. Regression coverage added.
- Final schema release: `79304e9` (`0012`). Full local suite: **305 passed**, no
  failures/skips; connector **33 passed**. Connector tests also passed on the
  deployed Node **22.23.3** image before schema rollout (connector code unchanged).
- Pre-release encrypted backup: `moresocial-20260927T001535Z.tar.enc`, restored to
  an isolated database. Upgrade/downgrade/re-upgrade matched all 39 retained tables
  and 21,035 restored rows. Disposable containers and unpacked private files removed.
- Quiesced pre-schema backup: `moresocial-20260927T002420Z.tar.enc`. The live
  migration matched all 39 retained tables and 21,040 rows before web/worker resumed.
- Readiness checks pass for database, migration head, Google configuration,
  encryption, generation configuration and embedding configuration. These checks
  do not make paid provider calls. The WhatsApp connector's live status returned
  `ready` after restart.
- Authenticated live review passed nine pages at 1440px and 390px (18 checks per
  release), with no document overflow or browser errors. No messages were sent,
  no drafts were generated, and the temporary review session was revoked.
- Invoking the new rollback guard against `f6cf3eb` returned exit 2 and preserved
  the active `79304e9` release and schema `0012`.
- Post-migration inventory: G `removed`, K `ineligible`. The two personal-key
  records, two legacy operator-key records and all provider usage history remain.
  Their disposition was asked separately; absent a decision they remain preserved.
