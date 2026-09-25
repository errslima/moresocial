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
| `anthropic_api_key` | Anthropic API key, or an empty file until available |
| `voyage_api_key` | Voyage AI API key, or an empty file until available |
| `backup_passphrase` | `openssl rand -base64 48` (store in the recovery vault too) |

With empty AI key files set `AI_PROVIDER=none`: import, browsing, gatherings and manual
drafts work; memory extraction, semantic search, cited answers and AI drafts are disabled.

`runtime.env` minimum:

```
BETA_ALLOWLIST=errslima@gmail.com
AI_PROVIDER=none
WHATSAPP_CONNECTOR_CAP=2
```

The runtime validates at startup that `https://1f517.com/moresocial/api/auth/google/callback`
is listed in the client file's `redirect_uris` (`/ready` reports `google_client: false`
otherwise) and never logs the file's contents.

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
sudo install -m 0644 deploy/systemd/moresocial-*.service deploy/systemd/moresocial-backup.timer /etc/systemd/system/
# When installing from root-only /srv paths, expand wildcards inside sudo sh -c.
sudo systemctl daemon-reload
sudo systemctl enable --now moresocial-connectors.service moresocial-backup.timer
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
3. Ask one question and draft one gathering invitation; copy it; send nothing.
4. `sudo systemctl start moresocial-backup.service` then a scratch restore (section 6).

## 5. Operations

- Logs: `docker compose -p moresocial -f /srv/moresocial/src/deploy/compose.yaml logs --tail 100 web worker`
  and `journalctl -u moresocial-connectors`. Logs are sanitized JSON; a user-facing error shows a
  reference ID that matches the `reference` field.
- Connector memory: each linked user runs one Chromium (limit 1.5 GB, 256 PIDs). Keep
  `WHATSAPP_CONNECTOR_CAP` at 2 until `docker stats` shows headroom; raise it in `runtime.env`
  and restart `web`.
- Connector upgrades and rollbacks are automatic: the provisioner compares the actual
  image ID with the configured release, verifies that the replacement image is available,
  then stops and recreates the container with the same private session mount. No manual
  container removal is needed. Real WhatsApp session continuity still needs a live check.
- A stopped or crashed connector is restarted by Docker (`unless-stopped`) or re-created by the
  provisioner, keeping `/srv/moresocial/data/whatsapp/<id>/session`.
- AI budgets: `AI_DAILY_TOKENS_PER_WORKSPACE` / `AI_DAILY_TOKENS_GLOBAL`. When exhausted, AI work
  pauses until the next UTC day and the UI says so.
- Account deletion is self-service (Connections page). An operator-only cleanup record
  containing just the workspace ID survives the account cascade until the provisioner
  removes the connector, network and session directory and acknowledges completion.
  This works for the final user too. Failed acknowledgment is retried safely. An empty
  database without explicit cleanup records still does not authorize deleting sessions.

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

- Review-fix migration `0002` adds a durable connector-cleanup outbox and source text
  segments. The worker automatically rebuilds old chunks for pipeline `v2-segments`;
  retrieval skips old chunks until rebuilt. Source data and user-confirmed claims remain.
  Do not downgrade the database while cleanup acknowledgments are pending. Prefer a
  forward fix over reverting to pre-review code, which contains the documented defects.
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

```bash
bash scripts/test.sh                 # everything, synthetic only (needs uv, node 22)
bash scripts/dev.sh                  # http://localhost:8772/moresocial/ with synthetic Google/WhatsApp/AI
DATABASE_URL=... SYNTHETIC_PROVIDERS=1 AI_PROVIDER=fake uv run python scripts/seed_demo.py
```

Synthetic providers are refused in production mode and on `https://1f517.com`.
