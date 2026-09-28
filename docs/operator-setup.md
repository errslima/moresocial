# Operator setup and runbook

For the Moresocial operator (server and Google Cloud access). End users never see any
of this; they only use **Continue with Google** and the WhatsApp QR code.

Target: `https://1f517.com/moresocial/` on the shared 1f517 host, independent state in
`/srv/moresocial`. Nothing here touches EnzoSocial, Quorum or other apps' files, services,
volumes or networks.

## 1. Layout on the host

| Path | Contents | Mode |
| --- | --- | --- |
| `/srv/moresocial/releases/<sha>/src` | Tracked source of one revision (`git archive`) | 0755 |
| `/srv/moresocial/src` | Symlink to the active release | |
| `/srv/moresocial/data/postgres` | Postgres data (bind mount) | 0700 |
| `/srv/moresocial/data/whatsapp/<workspace>/` | Per-workspace WhatsApp session + connector key | 0700, uid 1000 |
| `/srv/moresocial/secrets/` | Secret files (below) | 0700 dir |
| `/srv/moresocial/runtime.env` | Non-secret configuration (`.env.example` lists names) | 0640 |
| `/srv/moresocial/release.env` | `MORESOCIAL_REVISION=<sha>` (written by `release.sh`) | 0640 |
| `/srv/moresocial/backups/` | Encrypted backups, 14 days | 0700 |

Ports (verify free first: `ss -ltnp | grep -E ':877[23]\b'`):
`127.0.0.1:8772` public app (Caddy proxies only this), `127.0.0.1:8773` private management
API for the provisioner. The Compose network uses `172.31.77.0/24`; check it is unused
(`docker network inspect $(docker network ls -q) | grep -c 172.31.77.`) or pick another subnet
and update `TRUSTED_PROXY_IPS` (its gateway) in `deploy/compose.yaml`.

## 2. Secrets (files, never pasted into chat or committed)

Create each in `/srv/moresocial/secrets/`. The directory is `root:root 0700`, so no host user
other than root can reach the files; the files themselves are `0444` because Compose file
secrets are bind mounts that keep host permissions, and the web/worker (uid 10001) and Postgres
(uid 999) containers must read the ones mounted into them. `backup_passphrase` is never mounted
into a container: make it `0400`.

| File | Content |
| --- | --- |
| `google_auth.json` | Google's downloaded **web** client JSON (already exists locally as `config/google_auth.json`; copy with `scp`, never through a chat or image) |
| `encryption_key` | `python3 -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"` (keep a copy in the private recovery vault: without it stored Google grants are unreadable) |
| `operator_key` | `openssl rand -base64 48 \| tr -d '\n=+/'` |
| `db_password`, `db_admin_password` | `openssl rand -hex 32` each |
| `backup_passphrase` | `openssl rand -base64 48` (store in the recovery vault too) |

No AI secret file is needed. The OpenRouter key is normally saved in Admin (see
[OpenRouter AI configuration](#openrouter-ai-configuration) below) and stored encrypted with
`encryption_key` in the database, so it is also inside the encrypted backups.
`deploy/compose.yaml` mounts only `db_password`, `google_auth.json`, `encryption_key` and
`operator_key` into web and worker (and the two database passwords into `db`); it does not
mount an OpenRouter key file.

Until AI is configured (or while it is disabled in Admin), import, browsing, keyword search
and Calendar-based Events work; AI jobs (extraction, embeddings, suggestion reviews, conversation
and email event recognition) wait as "AI waiting: provider configuration", and cited
answers and AI drafts are unavailable.

`runtime.env` minimum:

```
BETA_ALLOWLIST=errslima@gmail.com
ADMIN_EMAILS=errslima@gmail.com
AI_PROVIDER=none
WHATSAPP_CONNECTOR_CAP=2
```

`AI_PROVIDER=none` only disables the optional file bootstrap; a configuration saved in
Admin takes precedence over it.

The runtime validates at startup that `https://1f517.com/moresocial/api/auth/google/callback`
is listed in the client file's `redirect_uris` (`/ready` reports `google_client: false`
otherwise) and never logs the file's contents.

## OpenRouter AI configuration

Production AI uses one operator-owned OpenRouter key for generation and embeddings in every
workspace; users never add AI keys. Accounts listed in `ADMIN_EMAILS` (production:
`errslima@gmail.com`) see an **Admin** link and can open `/moresocial/admin`; everyone else
gets 404 there. An admin must also be on `BETA_ALLOWLIST` to sign in.

In Admin enter the key, choose exact catalog IDs for the reasoning and embedding models, then
use **Test and save**. It checks the catalog first, then makes tiny synthetic paid calls, learns
the embedding identity and dimension, and only then activates the configuration. A failed test
preserves the current configuration and shows a reference for `logs.py find`. The web process
applies saved changes at once; the worker within 30 seconds. **Disable AI** and **Remove key and
disable AI** are saved as tombstones: they win over any bootstrap file, so a mounted file can
never reactivate a removed key or disabled AI.

Changing the embedding model stages a pending space and a background rebuild; search remains on
the active space until the new one covers current chunks, and **Cancel pending rebuild** drops
it. A reasoning-model change affects future generation only. There are no provider allowlists,
`data_collection="deny"` or ZDR fields in app requests; OpenRouter can fail over among providers
for the same pinned model.

Spending: set a monetary limit on the dedicated key in OpenRouter. Moresocial imposes no local
workspace or global token cap. It still reserves and reconciles token usage per workspace and
per UTC day; Connections shows today's tokens and Admin shows known USD spend separately from
calls whose cost OpenRouter did not report. Rate limits: a 429 pauses calls for `retry-after`
(at most 300 seconds) and defers background jobs as "AI waiting: provider rate limit" without
using an attempt; `AI_CONCURRENCY` caps simultaneous calls in each web and worker process.

Optional environment bootstrap (in `runtime.env`); saved Admin settings override it:

| Variable | Meaning |
| --- | --- |
| `AI_PROVIDER` | `openrouter` enables the file bootstrap; `none` (default) disables it. `fake` is for tests/development and refused in production; other values (`anthropic`, `openai`, `voyage`) are rejected at startup |
| `OPENROUTER_API_KEY_FILE` | Container path of a key file. Compose does not mount one: add a secret mount to web and worker yourself before relying on it |
| `OPENROUTER_GENERATION_MODEL`, `OPENROUTER_EMBEDDING_MODEL` | Exact model IDs; set both or neither |
| `EMBEDDING_DIMENSION` | Embedding dimension for the bootstrap (must be above 0 to activate it); Admin learns it by testing |
| `AI_CONCURRENCY` | Simultaneous AI calls per process (default 2) |

Retired variables are ignored if an old `runtime.env` still sets them (no startup error):
`ANTHROPIC_MODEL`, `ANTHROPIC_REFUSAL_FALLBACKS`, `OPENAI_MODEL`, `OPENAI_REASONING`,
`AI_DAILY_TOKENS_PER_WORKSPACE`, `AI_DAILY_TOKENS_GLOBAL`, `AI_DAILY_TOKENS_USER_KEY`,
`OPENROUTER_GENERATION_PROVIDERS`, `OPENROUTER_EMBEDDING_PROVIDERS` and `USER_AI_KEYS`.
Remove them from `runtime.env` when convenient. Old `anthropic_api_key`/`voyage_api_key`
secret files and legacy key rows in `operator_settings` are inert; delete them only through
the separately reviewed cleanup in [the rollout runbook](openrouter-rollout.md), which also
covers migration and verification.

## 3. Google Cloud (operator only)

Already done: project, web OAuth client, production callback, `https://1f517.com` origin,
`errslima@gmail.com` as support contact and test user; Gmail API and Calendar API enabled;
consent scopes configured (per the operator, 2026-09-25; not independently verified here).

Keep these settings exactly:
- Scopes: `openid`, `email`, `profile`, `.../auth/gmail.readonly`, `.../auth/calendar.events.readonly`.
  Nothing broader. Future write features need new incremental consent (`gmail.send`,
  `calendar.events.owned`), not pre-reserved scopes.
- Authorized redirect URI: only `https://1f517.com/moresocial/api/auth/google/callback`
  (plus a localhost callback only if you run a real-Google dev server; never wildcards).
- Audience External, Testing: add each beta user as a Google test user **and** to
  `BETA_ALLOWLIST`. Both are needed.
- In Testing, refresh tokens for these scopes generally expire after 7 days. Users then see
  "needs reconnecting" and use **Reconnect Google**; imported data is kept.
- Public launch requires Google verification; `gmail.readonly` is a restricted scope and
  server-side storage can require a security assessment. Publishing alone is not verification.

## 4. First deployment (additive)

On a workstation: `git push` the release commit; on the server:

```bash
ssh -i ~/.ssh/qoc_vps_ed25519 ubuntu@54.37.204.161  # current workstation key
free -m; df -h /srv; docker version; docker compose version
ss -ltnp | grep -E ':877[23]\b' || echo "ports free"
sudo cp /etc/caddy/Caddyfile /srv/moresocial-caddy-backup-$(date +%F).Caddyfile  # back up live config
git clone https://github.com/errslima/moresocial.git /tmp/moresocial && cd /tmp/moresocial
git checkout <sha>
# secrets and runtime.env as in section 2, then:
sudo deploy/release.sh <sha>       # builds images, migrates once, starts db/web/worker, checks /ready
sudo install -m 0644 deploy/systemd/moresocial-*.service deploy/systemd/moresocial-*.timer /etc/systemd/system/
# When installing from root-only /srv paths, expand wildcards inside sudo sh -c.
sudo systemctl daemon-reload
sudo systemctl enable --now moresocial-connectors.service moresocial-backup.timer moresocial-logs.timer
curl -fsS http://127.0.0.1:8772/health && curl -fsS http://127.0.0.1:8772/ready
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8772/internal/manage/connectors   # expect 404
```

### Proxy

Edit the **live** Caddyfile (not the stale repository copy). Paste the contents of
`deploy/Caddyfile.moresocial` inside the `1f517.com { route { ... } }` block, before the final
`handle { respond "Not found" 404 }`. Then:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy
for p in / /enzosocial/ /quorum-of-clones/ /moresocial/ /moresocial/health; do
  printf '%s %s\n' "$(curl -s -o /dev/null -w '%{http_code}' https://1f517.com$p)" "$p"; done
curl -sI https://1f517.com/moresocial | grep -i '^location: /moresocial/'
```

Existing routes must answer as before. Update the managed shared copy `../deploy/Caddyfile`
in the 1f517 repository with the same lines so a later shared deploy keeps the route.

### Live checks (record results in `docs/implementation-status.md`)

1. Real Google consent: sign in as a test user; expect Home, then Connections showing Gmail
   and Calendar "Up to date" with bounded counts. Also try unticking Calendar once.
2. WhatsApp: **Link WhatsApp**, scan the QR from the phone, wait for "Linked and syncing".
   Check `docker ps --filter label=moresocial.managed=1` shows one `ms-wa-<id>` container, no ports.
3. Ask one question and generate one Suggestions draft; send nothing.
4. `sudo systemctl start moresocial-backup.service` then a scratch restore (section 6).

## 5. Operations

- Logs: see **Diagnostics** below. A user-facing error shows a reference; look it up with
  `sudo python3 /srv/moresocial/src/deploy/logs.py find <reference>`.
- Connector memory: each linked user runs one Chromium (limit 1.5 GB, 256 PIDs). Keep
  `WHATSAPP_CONNECTOR_CAP` at 2 until `docker stats` shows headroom; raise it in `runtime.env`
  and restart `web`.
- Connector upgrades and rollbacks are automatic: the provisioner compares the actual
  image ID with the configured release, verifies that the replacement image is available,
  then stops and recreates the container with the same private session mount. No manual
  container removal is needed. Real WhatsApp session continuity still needs a live check.
- A stopped or crashed connector is restarted by Docker (`unless-stopped`) or re-created by the
  provisioner, keeping `/srv/moresocial/data/whatsapp/<id>/session`.
- AI spending limits are managed on the dedicated key in OpenRouter. Legacy
  `AI_DAILY_TOKENS_PER_WORKSPACE` / `AI_DAILY_TOKENS_GLOBAL` values are ignored.
  Daily token usage is still tracked. Migration 0006 resumes jobs deferred by the old shared caps.
- WhatsApp messages are sent only when a signed-in user presses **Send message** in a
  conversation. Gmail and Calendar access stays read-only.
- Account deletion is self-service (Connections page). An operator-only cleanup record
  containing just the workspace ID survives the account cascade until the provisioner
  removes the connector, network and session directory and acknowledges completion.
  This works for the final user too. Failed acknowledgment is retried safely. An empty
  database without explicit cleanup records still does not authorize deleting sessions.

## Event identity repair

`scripts/reconcile-events.py` runs `python -m app.event_reconcile` in `moresocial-web-1` over
operator SSH (`--local` runs it against `DATABASE_URL`). Every command is a **rolled-back dry run**
unless `--apply` is given; changes go through `app.event_identity` (the same service as the web
UI), never hand-written SQL. Output is JSON with IDs, counts, times and digests, never source text.

```bash
R="python3 scripts/reconcile-events.py"
$R report                                            # mode, decisions, pair scans, resolver jobs
$R list --email you@example.com --date 2026-09-27     # originals, representatives, source counts
$R same --email you@example.com --events A,B,C        # dry run: before/after groups and source union
$R same --email you@example.com --events A,B,C --apply
$R separate --email you@example.com --events A,B --apply
$R split --email you@example.com --parts "A|B,C" --apply
$R undo --email you@example.com --decision D --apply
$R candidates --email you@example.com                # resolver candidates and signals, no model call
$R resolve --email you@example.com [--events A] --apply   # queue a (targeted) resolver run
$R recompute --email you@example.com [--apply]      # re-project cards after a projection fix
$R mode --value observe|apply|off --apply
$R withdraw-automatic --email you@example.com [--events A,B] [--apply]   # rollback of automatic groups
```

Release with mode `observe`; review the Possible duplicates and `report` counts on a bounded sample
before `mode --value apply`. Mode `off` stops new automatic decisions but keeps existing groups.

## Social assistant release (migrations 0013–0014)

Not yet deployed as of 2026-09-27. Order and compatibility:

1. Encrypted backup first: `sudo systemctl start moresocial-backup.service`.
2. Stop the old worker before running `deploy/release.sh`
   (`sudo docker compose -p moresocial -f /srv/moresocial/src/deploy/compose.yaml --env-file
   /srv/moresocial/release.env stop worker`). Migration `0014` makes
   `suggestions.action_key` NOT NULL; an old worker publishing a chat review during the
   migration window would fail that job (it retries later, so this is a precaution, not a
   data risk). The migration takes a `SHARE ROW EXCLUSIVE` lock on `suggestions` with a
   5-second lock timeout; rerun the release if it times out.
3. `deploy/release.sh <sha>` migrates (`0013` connector health, `0014` assistant), starts
   the new web and worker, checks readiness, then restarts the provisioner, which recreates
   connectors with the new image. Until a connector runs the new image it sends no
   heartbeat and Suggestions/Connections show "WhatsApp status unavailable" (not a
   disconnect). A new connector against an old web app gets 404 on its heartbeat, logs
   `heartbeat_failed` and keeps ingesting.
4. Existing suggestions keep their data, dispositions and drafts; drafts saved before the
   release show "needs a review" once, because they carry no context fingerprint.

Migration `0015` (review follow-up) only adds `suggestions.context_sources` with a default
and backfills event-linked rows; it needs no worker stop. Images from `901bc09` keep
working on it but report a migration mismatch on `/ready`.

Proactive preparation starts automatically in the local 08:00–23:00 window (defaults:
event preparation and check-ins on, balanced initiative, pinned contacts only). Operator
switch, effective within one scan and without a restart:

```bash
sudo docker compose -p moresocial -f /srv/moresocial/src/deploy/compose.yaml \
  --env-file /srv/moresocial/release.env exec -T web python -m app.proactive mode off   # or on / status
```

`off` stops new scans and preparation; prepared drafts and all settings stay. Users can
also pause the assistant on its page. Diagnostics: job events for `proactive_scan` and
`prepare_message` (counts and outcomes only). Suggested live checks after release, using
the [agent browser login](agent-browser-login.md) and browsing only: Connections shows a
fresh heartbeat; Suggestions has no false sync warning; the Assistant page renders at
desktop/mobile; a linked event appears on an event-related suggestion. **Never send a test
message to a real person**; evaluate prepared drafts by reading them only.

## Evidence-grounded drafts (migration 0016)

Feature details: [Suggestions › Evidence-grounded drafts](suggestions.md#evidence-grounded-drafts).
Migration `0016` is additive (two new tables, `assistant_settings.style_mode`,
`claims.corrects_id` and two triggers on `suggestions`); it takes brief locks with a
5-second lock timeout and needs no worker stop, but stopping the worker during `release.sh`
remains the standard precaution. Settings whose tone values are all defaults switch to the
conversation voice; any non-default tone value keeps the user in custom mode. Existing
drafts are kept and labelled as written with the previous context; they need one review
(save or rewrite) before they can be copied again.

Rollout switch (operator setting, effective on the next request, no restart). The default is
`off`: drafts use the clearly labelled limited context (this conversation, its events and the
voice examples). Start with one workspace:

```bash
C="sudo docker compose -p moresocial -f /srv/moresocial/src/deploy/compose.yaml --env-file /srv/moresocial/release.env exec -T web"
$C python -m app.draft_evidence enable owner@example.com   # adds that account's workspace to the selection
$C python -m app.draft_evidence mode selected              # or: all | off | status
```

`off` stops new evidence preparation and bundle use for new drafts; existing bundles keep
guarding their drafts (stale or erased drafts stay blocked) and all user settings stay.
Normal cost per explicit draft: one batched query-embedding call, one resolution call, one writing
call and at most one verification/repair call. Retries and output-length recovery can add
calls; the evaluation accounts for every reserved provider attempt, including embeddings
and failures, and reports unknown cost as unknown. Background `revalidate_draft` jobs run when
the workspace evidence revision moves; they rerun retrieval without a model call and pay
one resolution call only when the considered evidence changed. Diagnostics: `draft_prepared`
(counts, durations, coverage code, model; never text) and job events for `revalidate_draft`.

Live language evaluation (fictional scenarios, three systems, bounded): run it in a
disposable database on the production Postgres server, never the live one. The harness
reads the OpenRouter configuration from the live database, then works only in the
evaluation database, and it has no WhatsApp connector, so it cannot send.

```bash
sudo docker exec moresocial-db-1 psql -U moresocial_admin -d postgres -c "CREATE DATABASE moresocial_eval_1 OWNER moresocial"
sudo docker exec moresocial-db-1 psql -U moresocial_admin -d moresocial_eval_1 -c "CREATE EXTENSION vector"
sudo install -d -m 0700 /tmp/ms-eval
sudo chown 10001:10001 /tmp/ms-eval
sudo docker compose -p moresocial -f /srv/moresocial/src/deploy/compose.yaml --env-file /srv/moresocial/release.env \
  run --rm --no-deps -v /tmp/ms-eval:/eval web python -m app.draft_eval --database moresocial_eval_1 --generations 2 --out /eval
sudo docker exec moresocial-db-1 psql -U moresocial_admin -d postgres -c "DROP DATABASE moresocial_eval_1 WITH (FORCE)"
```

The 18 fictional cases include Dutch housing replies, cancellations, effective-date changes,
confidential sources, corrections and long email passages. `/tmp/ms-eval/draft-eval.md` lists
every draft with its proxy scores; the JSON has usage and latency per system and separate
indexing costs. Automatic scores cannot establish "sounds like me": have the owner read
representative drafts before `mode all`.

## Diagnostics

Every service writes one sanitized JSON event per line (`app/diagnostics.py`; the connector and
provisioner use the same schema): `schema`, `ts` (UTC), `level`, `service`, `release`, `event`,
`reference`, correlation IDs (`request_id`, `operation_id`, `parent_operation_id`, `job_id`,
opaque `workspace_id`) and field-validated metadata. Keys, cookies, OAuth codes, authorization
headers, URLs, prompts, messages, embeddings and provider bodies are never recorded; exceptions
contribute their class and `file:function:line` frames only. `audit: true` marks administrative
and lifecycle events, which are never sampled or suppressed.

Correlation: the public app assigns every request an ID (returned as `X-Request-ID` and shown as
the reference on error pages and failed AI actions). Jobs carry the initiating ID and the worker
logs `parent_operation_id`; the internal API adopts a connector's ID, and web passes its ID to
connectors. Admin **Test and save** shows its operation ID; its stage events record the requested,
catalog-canonical and returned model, dimensions, HTTP status and OpenRouter request ID.

Store: `/srv/moresocial/logs` (root 0750) outlives containers. `app/` (web, worker, migrate,
uid 10001), `connectors/<workspace>/` (mounted into each connector, uid 1000) and `host/`
(provisioner, backup, release, alerts). `docker logs` still shows stderr, but is lost when a
container is replaced; the store is the durable copy. `moresocial-logs.timer` runs hourly:
`logs.py prune` (14 days, 1 GiB cap, oldest first) then `logs.py check`.

```bash
L="sudo python3 /srv/moresocial/src/deploy/logs.py"
$L find 6b41cd8b25b94cbda19266902d2fbe52       # reference/request/operation/job ID, plus related events
$L tail --service web --level warn -n 50        # recent warnings from web
$L tail --event admin_openrouter_stage          # every Admin setup stage (requested/returned models)
$L tail --event alert --hours 48                # alerts raised by the hourly check
$L status                                       # size, newest event per service, dropped events
$L debug --service web --events admin_,provider_ --minutes 30   # scoped DEBUG, max 2 h
systemctl status moresocial-logs.timer; systemctl --failed      # a failed check unit = active alert
```

Alert rules (one `alert` event per rule per six hours, then `alert_resolved`): error-level events
(20/h), terminal job failures, queue lag over 30 minutes, stalled Google syncs, 5 failed provider
calls/h, a failed or missing daily backup, failed readiness, dropped diagnostic events, log disk
pressure, and no worker `queue_stats` reaching the store (collector or worker down). Alerts are
recorded in the store and the unit's exit status; nothing is pushed off the server.

Levels: `LOG_LEVEL=info` in production. DEBUG is enabled only through `logs.py debug`, scoped by
service and event prefix and expiring within two hours; it never relaxes field restrictions.
Healthy requests are sampled (`HTTP_LOG_SAMPLE`, default 10%); health probes and the provisioner
poll are logged only when they fail. Repeated events are capped at 50 per minute per event and
code, and the next admitted event carries a `suppressed` count. Logging never blocks a request
or a job: write failures are counted (`dropped`) and reported on the next event.

## 6. Backups and restore

`moresocial-backup.timer` runs `deploy/backup.sh` daily: `pg_dump -Fc`, each WhatsApp session
copied while its connector is briefly stopped, `secrets/`, `runtime.env`, `release.env`,
encrypted into one `moresocial-<UTC>.tar.enc` with `backup_passphrase`. Retention 14 days.

Limits users are told about: backups stay on this server (a lost server loses them), and
deleted data remains inside backups until they expire (at most 14 days).

Restore test into disposable names (never the live project):

```bash
sudo /srv/moresocial/src/deploy/restore.sh /srv/moresocial/backups/moresocial-<stamp>.tar.enc --scratch /tmp/ms-restore
# prints the restored revision and counts, then the cleanup command
```

Full disaster recovery onto a new host: restore `secrets/` from the vault copy, run
`release.sh <sha>` with an empty data directory, stop `web worker`, `pg_restore` the dump into
the new `db` as `moresocial_admin` with `--no-owner --role=moresocial`, untar session
directories into `data/whatsapp/`, start the stack. Never restore an old dump over a database
that has newer user writes.

## 7. Rollback

Schema `0012` removes the retired manual gathering tables. Images older than `79304e9`
require a coordinated structural downgrade before startup; use the
[schema cleanup rollback procedure](cleanup/schema-rollout.md#failure-and-rollback).
The current rollback script refuses incompatible images before changing release state.
An older copy of the script does not contain that protection.

- Review-fix migration `0002` adds a durable connector-cleanup outbox and source text
  segments. The worker automatically rebuilds old chunks for pipeline `v2-segments`;
  retrieval skips old chunks until rebuilt. Source data and user-confirmed claims remain.
  Do not downgrade the database while cleanup acknowledgments are pending. Prefer a
  forward fix over reverting to pre-review code, which contains the documented defects.
- Migration `0003` (AI keys, admin settings) is additive: the previous release runs on it
  unchanged and simply ignores stored keys and admin settings.
- Migration `0011` (event identity) is additive. Pre-identity images ignore group pointers and show
  duplicate event cards; their `/ready` reports a migration mismatch although pages are served.
  Prefer `reconcile-events.py mode --value off` / `withdraw-automatic` and a forward fix. Keep the
  new tables; never drop them or delete evidence as an emergency rollback.
- Migrations `0013` (connector health) and `0014` (assistant) are additive for existing
  data but **not rollback-compatible**: pre-0014 images cannot display proactive
  suggestions (no anchor, new kinds) and report a migration mismatch. `rollback.sh`
  refuses such images while the `0014` schema is present. Prefer
  `python -m app.proactive mode off` (above) and a forward fix. The `0014` downgrade refuses
  while any proactive suggestion or assistant data exists; never delete that user data or
  restore an old database over newer messages to roll back.
- Migration `0016` is additive but **does not support an operational rollback to `3fcf2b4`**:
  that image expects head `0015` and returns 503 on `/ready`. The rollback preflight now
  checks the image's exact migration head before changing services or `release.env`.
  It also rejects images that cannot validate the versions of stored draft bundles.
  The source-erasure triggers protect dependencies; they do not make an older drafting
  policy safe. Prefer `python -m app.draft_evidence mode off` and a forward fix. A rollback
  target must pass both preflights; never downgrade the schema or discard user data just
  to make an old image start. The `0016` downgrade refuses while correction links exist.
- Application: `sudo /srv/moresocial/src/deploy/rollback.sh <previous-sha>` switches images and
  source back, keeping all data (first-release migrations are additive).
- If a future migration is not backward compatible, the previous release cannot run on the new
  schema: fix forward, or restore a backup into a *new* database and reconcile manually. Do not
  overwrite live data automatically.
- Remove the first deployment entirely (keeps private state for recovery): remove the Caddy lines
  and reload; `sudo systemctl disable --now moresocial-connectors.service moresocial-backup.timer`;
  `docker compose -p moresocial -f /srv/moresocial/src/deploy/compose.yaml down` (no `-v`);
  `docker rm -f $(docker ps -aq --filter label=moresocial.managed=1)`. Leave `/srv/moresocial`.
- Never run `docker system prune`, volume prune, or anything that touches other apps.

## 8. Local development and tests

For signed-in browser checks against the live site without Google interaction, see
[temporary agent browser login](agent-browser-login.md). The operator SSH helper issues
a private, expiring Playwright session file for one existing account; no public login
endpoint or application redeployment is needed.

```bash
bash scripts/test.sh                 # everything, synthetic only (needs uv, node 22)
bash scripts/dev.sh                  # http://localhost:8772/moresocial/ with synthetic Google/WhatsApp/AI
DATABASE_URL=... SYNTHETIC_PROVIDERS=1 AI_PROVIDER=fake uv run python scripts/seed_demo.py
```

Synthetic providers are refused in production mode and on `https://1f517.com`.

### Trace one imported message

Run this read-only command inside the app container:

```bash
sudo docker exec moresocial-web-1 python -m app.trace_message \
  --workspace <workspace-uuid> --source <source-uuid>
```

The workspace-scoped JSON shows inclusion/deletion, current chunks, extraction timestamps,
embedding models, job attempts/errors, and claims linked to the message. Add `--include-text`
only when private message/claim wording is needed; text is omitted by default.

Missing AI configuration defers jobs for ten minutes without consuming attempts.
Once generation is configured, reconciliation also revives legacy terminal extraction
jobs with the exact missing-configuration error, only for current unextracted chunks
in active workspaces. Cancelled jobs and invalid-output failures remain untouched.
