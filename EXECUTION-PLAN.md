# Moresocial first release — implementation handoff

Status: executable design plan, not an implemented or deployed application.
Prepared 2026-09-25. Work in the standalone `errslima/moresocial` repository.

## 1. Outcome and scope

Build a multi-user web application at **https://1f517.com/moresocial/** that helps
people remember their relationships and turn social intentions into manageable
actions. Enzo is the first user. The representative workflow is organizing a
lasergame gathering and getting help writing invitations and follow-ups.

Confirmed requirements:

- One Continue with Google flow requests identity, Gmail read access, and Calendar
  read access. Users never configure Google Cloud projects or enter credentials.
- Operator-owned OAuth credentials; each user's authorization and data are private.
- WhatsApp connection by QR-linked WhatsApp Web, using the same approach as EnzoSocial.
- Source-backed contact profiles and semantic retrieval, with editable memories.
- Support multiple users from the start; never special-case the owner as the data owner.
- Independent deployment under `/srv/moresocial`, serving the `/moresocial/` prefix.

Implementation defaults chosen for this first release:

- Read-only integrations. Draft text inside Moresocial with Copy and edit actions;
  users send it in their existing messaging app. Copy does not mean sent.
- Event workspace with invited people, proposed dates, reply evidence, and next steps.
- Self-profile with user-entered preferences/goals and editable, sourced observations.
- Small optional action list; no unread-task pressure, relationship scores, or streaks.
- Initially controlled beta: operator-configured email allowlist, not an invitation
  email system. Google test-user configuration is a separate operator responsibility.
- Google primary calendar only; defer calendar selection and its extra scope.

Defer: automatic sending, Gmail/Calendar writes, payments, native mobile apps,
attachments/media transcription, psychological classification, cross-user profiles,
a graph visualization, a separate graph database, and a general autonomous agent runtime.
Do not import EnzoSocial's existing user data, sessions, or provider credentials.

## 2. Repository references and working rules

Read `docs/product-definition.md` and `docs/connections.md`. If available locally,
inspect these sibling EnzoSocial files before adapting their patterns:

| Reference | Reuse as reference |
| --- | --- |
| `../enzosocial/accounts/main.py` | Google state/PKCE/nonce, verified identity, session handling |
| `../enzosocial/app/google_connection.py` | Google refresh, bounded reads, revocation |
| `../enzosocial/connector/` | QR pairing, normalization, contact identity, restart behavior, compatibility patches |
| `../enzosocial/deploy/provision_workspaces.py` | Host-managed per-user connector provisioning |
| `../enzosocial/tests/test_google_connection.py` | Synthetic provider tests |
| `../deploy/Caddyfile` | Current shared-server routing |

If these references are unavailable, implement from the contracts below and official
documentation; do not invent claims that the references were checked. Preserve
license notices for copied code. Copy only needed modules and adapt tests/imports;
do not depend on sibling files at runtime or copy hardcoded owner emails/paths.

Existing `config/google_auth.json` is private operator material. Do not print,
commit, place in images, or copy into test fixtures. `.gitignore` already excludes it.
Existing source docs may be untracked: include the intended docs in the eventual
handoff commit, but use explicit file selection rather than blindly adding everything.

Implement milestones in order. After each, update `docs/implementation-status.md`
with completed work, checks and outstanding dependencies. A missing production
credential does not block synthetic implementation or tests. Never describe a
mocked provider test as a successful real account connection.

## 3. Fixed architecture

Use a small stack consistent with the existing server:

- Python 3.12, FastAPI, SQLAlchemy, Alembic migrations, PostgreSQL with pgvector.
- Server-rendered HTML/Jinja plus small ES modules and CSS; no frontend framework
  or bundler needed for v1. Escape text and render imported content as inert text.
- Node.js connector with `whatsapp-web.js`, Chromium, LocalAuth and QR rendering.
  EnzoSocial pins 1.34.7 with a compatibility patch; start by validating that
  combination. Pin all chosen dependencies and commit lock files.
- Docker Compose services: `web`, `worker`, `db`; separate connector containers
  per connected user, created by a host-side provisioner.
- PostgreSQL job queue with leases and retry state; no Redis/Celery in v1.
  Claim jobs atomically, renew leases, recover expired leases after crashes, and
  use unique idempotency keys. Commit source updates and their jobs together.
- Provider adapter for structured generation and embeddings, configured by operator.
  Implement a real HTTP/API adapter and a deterministic fake used only in tests.
  Do not depend on a developer's interactive AI subscription or EnzoSocial workers.
- Pick and document one supported generation/embedding provider during milestone 0,
  based on available operator configuration and current official documentation.
  Keep provider keys and model IDs in server configuration. If none is available,
  implement the adapter and tests, and mark live AI validation pending.
- No LangChain dependency for v1: explicit ingestion, extraction and retrieval functions.

Postgres stores original text, structured claims, and embeddings together. Start
with exact cosine vector search over the authenticated user's filtered rows plus
Postgres full-text search; avoid premature approximate-index tuning.
Record embedding model, dimension, content hash, and pipeline version. Refuse
mixed-model comparisons and provide a reindex job when configuration changes.

Suggested tree:

```text
app/                    # web, auth, data access, providers, retrieval, templates/static
worker/                 # sync/extraction job runner
connector/              # adapted WhatsApp connector and Node tests
migrations/             # Alembic migrations
tests/                  # synthetic unit, integration and browser scenarios
scripts/                # setup, test, seed-demo commands
deploy/                 # Compose, Dockerfiles, provisioner, Caddy fragment, runbook
docs/implementation-status.md
docs/operator-setup.md
.env.example            # names and placeholders only
```

## 4. Data and isolation contracts

One account owns one private workspace in v1. Use opaque UUIDs. Every tenant-owned
row, queue item, cache key and lookup includes workspace ownership. The workspace
comes from the authenticated server session, never a trusted browser parameter.
Use composite foreign keys `(workspace_id, id)` for relationships between owned
records so a row cannot reference another workspace's object.

Minimum entities:

| Entity | Required information |
| --- | --- |
| accounts/workspaces | Verified Google `sub`, display email/name, timezone, status |
| sessions/oauth_flows | Hashed tokens/state, expiry, PKCE verifier, nonce, browser binding, flow purpose |
| connections | Workspace, provider account identifier, granted scopes, encrypted tokens, state, sync cursor |
| people/person_identifiers | Workspace-local identity, provider identifiers, confirmed cross-channel links |
| sources/source_participants | Provider ID/version, thread/chat/calendar ID, author/participants, time, inert text, inclusion/deletion state |
| claims/claim_sources | Subject, predicate/value, explicit/observed/inferred, dates, evidence references, confirmed/rejected/superseded status |
| chunks/embeddings | Source version, bounded text, model/dimension/hash, vector |
| relationship_notes | User's notes, preferences and shared history with a person |
| self_profile | User-entered preferences, intentions and confirmed observations |
| events/event_people | Title, timezone, candidate times, invitees, manually confirmed RSVP state |
| drafts/answer_sources | Editable output, referenced source IDs and source versions |
| jobs/provider_usage | Workspace, idempotency key, lease, attempts, usage reservation, outcome |
| exclusions/deletion_tombstones | Provider record or conversation identity, preventing unwanted reimport |

Use dedicated repository functions requiring workspace context; prohibit raw
unscoped data lookup in handlers. Runtime DB role is not a superuser. Test isolation
against real Postgres, including vector searches and worker jobs, not just mocks.

Google refresh tokens encrypted with an operator-held key outside the DB. Keep
private WhatsApp session directories per workspace. Never log tokens, QR payloads,
authorization codes, message bodies, or prompts. Browser errors get an opaque
reference ID with sanitized operator diagnostics.

Exclude/delete removes searchable chunks, embeddings and unsupported derived
claims, and invalidates cached answers/drafts that contain deleted evidence.
Late jobs must recheck inclusion/version before publishing results. Tombstones
prevent later sync reintroducing deleted material. Disconnect stops future jobs
and revokes/removes credentials; offer an explicit separate delete-imported-data action.
Account deletion disables sessions/jobs, disconnects providers, removes connector
storage and all owned data. Explain the backup retention window in the UI/runbook.

## 5. Milestones and acceptance checks

### M0 — foundation and runnable synthetic environment

Create the tree, pinned dependencies, Compose, migrations, configuration validation,
test scripts and health endpoints. Use separate disposable test data directories.
Provide a synthetic two-user fixture with lasergame messages, calendar events and
conflicting identities. Fixture creation must be disabled in production mode.
Record provider selection and generation/embedding configuration contract.

Done when: a fresh database migrates successfully; web/worker start; tests run
without Google, WhatsApp or paid AI credentials; synthetic mode cannot activate
on the production URL; no secret is tracked or copied into a container image.

### M1 — combined Google onboarding and accounts

Operator supplies a web OAuth client. One POST start endpoint initiates authorization
with `openid email profile`, `gmail.readonly`, `calendar.events.readonly` (API scopes
use `https://www.googleapis.com/auth/`). Request offline access. Callback verifies
state, expiry, browser binding, PKCE exchange, ID-token issuer/audience/expiry/nonce,
and verified email before establishing identity using `sub`.

Store OAuth flows independently so two simultaneous users/tabs do not overwrite
each other. Issue a new opaque session at login; Secure, HttpOnly, SameSite=Lax,
host-only cookie named `moresocial_session`, Path=/moresocial/. Protect mutations
with CSRF tokens and Origin checks. No state-changing GET except validated OAuth
callback semantics. Avoid open redirects; strip callback codes by immediate redirect.

Persist actual granted scopes and refresh token. Do not erase a stored refresh
token when Google omits it on a later login. Reconnection must verify the same
Google subject in v1; do not silently replace the connected account. Missing API
consent leaves the user signed in with a clear disabled feature and Retry action.
Do not use `prompt=consent` for every returning sign-in; use it when needed to
establish/recover offline access. Expired/revoked grants become reconnect-required.
Validate beta access before storing API credentials; no hardcoded owner bypass.

Done when: synthetic full/partial/cancelled consent, expired/replayed state,
bad nonce, missing refresh token, concurrent flows, reconnect, logout and two-user
isolation pass. Browser flow contains no Cloud Console steps or credential fields.
Redirects and cookies work through the actual /moresocial/ proxy arrangement.

### M2 — Google synchronization and source browsing

Background sync starts after successful consent. Initial bounds: Gmail last 90 days,
at most 500 messages excluding Spam/Trash; primary Calendar past 90/next 90 days,
at most 1,000 event instances. Show these limits and whether results were truncated.
Fetch plain text with safe HTML-to-text fallback, cap stored body at 32 KiB and
mark truncation. No attachments or remote image loading. Retain participants and
provider IDs. Treat calendar invitations as scheduled events, not proof of attendance.

Persist paginated progress. Use Gmail history cursor with bounded resync on invalid
cursor; Calendar event sync tokens with full bounded refresh when invalid. Use the
documented compatible parameter sets for Calendar incremental sync (do not blindly
combine syncToken with timeMin/timeMax); refresh the rolling window separately.
Honor updates, cancellations and provider deletions. Upsert by workspace + provider
account + object ID. Poll initially every 5 minutes; back off on throttling/outages.
No new paid model extraction on every unchanged poll. Distinguish stale from empty.
Persist a new provider sync cursor only after all pages it covers are processed.
If the initial cap prevents reaching a final Calendar page, keep the connection
marked truncated and use bounded refreshes until a valid cursor can be obtained;
never invent a cursor or infer deletion from absence in a truncated result set.

Done when: paginated/retried reads are idempotent; worker restart resumes jobs;
revocation stops requests; deletion/cancellation invalidates stale derived memory;
user can inspect source text and sync status; partial consent runs only granted syncs.

### M3 — per-user WhatsApp pairing and ingestion

Adapt EnzoSocial's QR/LocalAuth connector and normalization tests. Each workspace
gets its own connector container, network, authentication key and private session
mount under `/srv/moresocial/data/whatsapp/<workspace_uuid>/`.

Web records desired connector state; a host-side provisioner reconciles it through
an authenticated internal management interface. No Docker socket in web, worker
or connector containers. Provisioner uses fixed templates/image references and
validated opaque IDs, never user-supplied paths or shell fragments. Create a distinct
systemd unit `moresocial-connectors.service`; manage only Moresocial-owned resources.

Connector communicates with web over a private per-workspace network. Scope its
credential to one workspace and ingest/status actions. Do not trust an ingest
payload's workspace ID. Connectors have no Postgres/model credentials, no host ports,
and no access to other connector networks or session mounts. Web joins these
networks to route status/QR requests after checking the signed-in workspace.

QR available only to its owner, no-store responses; clear it after pairing/expiry.
Surface pairing/loading/ready/disconnected/error/capacity states. Set a configurable
active connector cap, initially two, until server memory measurements justify more.
One Chromium instance per user has material resource cost; set memory/PID limits.

Initial history target: up to 50 recent chats, 200 messages per chat, 90-day age bound,
subject to WhatsApp Web availability. Live messages use the same idempotent ingestion
path as history. Keep group authors separate from group IDs. Do not merge people
by display name; unresolved WhatsApp identifiers remain unresolved. Respect source
exclusions and handle edit/revocation events supported by the connector.
No send endpoint in v1; remove or disable inherited sending routes.

Done when: two synthetic connectors cannot swap credentials, ingest into each
other's workspace, obtain each other's QR or read another session mount. Container
restart preserves its own session. Repeated history/live packets do not duplicate
records. Disconnect removes the session as requested and stops reconnect loops.
Record a real QR pairing smoke test separately when the user pairs their phone.

### M4 — memory, embeddings and cited answers

Chunk sources within a single conversation with preserved authors, dates and
source references. Bound chunks to the embedding model's token limit. Extract a
small typed schema of interests, explicit life updates, preferences and commitments.
Require valid local source IDs for every extracted claim; reject invalid JSON and
unsupported evidence references. Keep user corrections authoritative. Conflicting
and stale claims remain distinguishable; do not overwrite a changed job/location
without preserving when the old claim applied.

Resolve exact provider identifiers first. Offer manual link/unlink for identities
across email and WhatsApp; no automatic merges from similar names. User-owned
contact profiles never become a global directory shared with other users.

Retrieval combines workspace/person/time filters, full-text and vector results,
deduplicated to a bounded evidence set (initially 12 chunks). Generate answers and
drafts from that evidence; validate returned citation IDs server-side. Show source
excerpts/dates on click. When evidence is insufficient, say so. Treat all imported
content as untrusted text; generation has no shell, network tools or send capabilities.

Provider calls use timeouts, limited retries, concurrency limits and per-user daily
token budgets plus a global budget. Reserve estimated input + max-output allowance
atomically before dispatch, reconcile actual usage, and account for embedding and
retry costs. Cache unchanged extraction/embeddings by content hash and version.
Budget exhaustion pauses AI work visibly; never loop indefinitely.

Done when: deterministic retrieval fixtures return the right user's evidence,
citations resolve only within that workspace, conflicting facts are not silently
collapsed, deletion wins a race with extraction, and a prompt-injection fixture
cannot trigger tools or cause cross-user retrieval. Add a small documented live
quality evaluation when provider configuration is available; schema tests alone
do not establish factual quality.

### M5 — complete usable first workflow

Build responsive screens with loading/empty/error states, keyboard navigation,
labels and readable focus styles:

| Screen | Required behavior |
| --- | --- |
| Welcome | Continue with Google and concise description of requested access |
| Home | Up to three optional next steps from user-selected events/contacts; dismiss/snooze |
| People | Search, profile, evidence timeline, edit/reject claim, manual identity links |
| Ask | Question, cited answer, explicit insufficient-evidence state |
| Gatherings | Create lasergame event, choose contacts, dates, notes, editable drafts and reply evidence |
| About me | Editable preferences/goals and confirmed observations |
| Connections/settings | Google state/reconnect, WhatsApp QR, history coverage, exclusions and deletion |

For gatherings, track `not_contacted`, `awaiting_reply`, `accepted`, `declined`,
`maybe`, with manual user changes. AI may suggest a transition citing an explicit
reply; require user confirmation in v1. A copied draft does not advance invitation
state. Calendar checks describe conflicts in the user's imported calendar only;
do not infer invitees' availability or assume missing events mean free time.

Drafts can be rewritten shorter/more casual and manually edited; show exactly
what Copy copies. No fake Send button. Follow-up suggestions are optional and
never infer that a person is upset because they did not reply.

Done when: a Playwright scenario signs in with synthetic Google, views imported
evidence, fixes a contact claim, creates a gathering, drafts/copies an invitation,
records an RSVP and asks a cited question. Test desktop and 390px mobile widths.
Assert no provider write/send call occurs anywhere in this flow.

### M6 — deployment packaging and production validation

Implement the deployment contract below, operator runbook, backup/restore scripts,
CI and recovery checks. CI runs Python, Node, Postgres integration and browser
checks without production secrets. Deliver one documented local verification
entry point (`bash scripts/test.sh`) and a demo seed command for isolated development.

Done when: build from a clean checkout, restore into a disposable environment,
proxy-prefix browser tests, session persistence, connector recovery and rollback
procedure are verified. Actual Google consent, real QR pairing, live AI quality
and VPS rollout are distinct checks in the status file; leave them pending if
credentials or server access are unavailable. Do not label the release deployed
based on packaging or local success alone.

## 6. Exact deployment contract

Local repository is currently nested at `1f517/moresocial`, but production is a
separate application directory. Existing local deployment documentation identifies
the shared host as `ubuntu@54.37.204.161`; verify the operator's current host/SSH
configuration before connecting. No live server inspection was performed for this plan.

| Setting | Value |
| --- | --- |
| Public URL | `https://1f517.com/moresocial/` |
| PUBLIC_ORIGIN | `https://1f517.com` |
| BASE_PATH | `/moresocial` |
| OAuth redirect | `https://1f517.com/moresocial/api/auth/google/callback` |
| Compose project | `moresocial` |
| Host web listener | `127.0.0.1:8772` (verify free before first deployment) |
| Internal web port | `8000` |
| Host management listener | `127.0.0.1:8773` to a separate private API on container port `8001`; verify free |
| Source releases | `/srv/moresocial/releases/<git-sha>/src` |
| Active source | `/srv/moresocial/src` symlink to active release |
| Private state | `/srv/moresocial/data` |
| Operator secrets | `/srv/moresocial/secrets` |
| Runtime configuration | `/srv/moresocial/runtime.env` |
| Backups | `/srv/moresocial/backups` |

Proposed Caddy additions inside the existing `1f517.com` route, before fallback:

```caddyfile
redir /moresocial /moresocial/ 308
handle_path /moresocial/* {
    reverse_proxy 127.0.0.1:8772
}
```

`handle_path` strips `/moresocial` before forwarding. Backend routes therefore
start at `/api/...`, `/static/...`, `/health` and `/ready`, with FastAPI root_path
set to `/moresocial`. Generate browser URLs using a single base-path helper. Do
not register every backend route with the prefix as well. Every fetch, asset,
navigation link, login redirect and cookie must preserve the public prefix.
Use configured PUBLIC_ORIGIN for OAuth URLs rather than untrusted Host headers.
Trust forwarded headers only from the actual proxy path, not arbitrary clients.

Internal health: `http://127.0.0.1:8772/health`; public health:
`https://1f517.com/moresocial/health`. Health returns no private account details.
Readiness covers DB/migrations; external Google/WhatsApp outages appear separately.
Do not expose Postgres, connector ports, or provisioner APIs publicly. Public web
routes must reject `/internal/*`; use a separate private listener for internal APIs.
The provisioner uses the loopback management listener with an operator key;
connectors use web:8001 on their private networks with their scoped keys. Validate
roles at each internal endpoint. Caddy forwards only to public port 8772.

Create an additive Caddy fragment and explicit installation instructions. Inspect
and back up the live Caddyfile, preserve other routes, validate before reload, then
verify `/`, `/enzosocial/` and `/quorum-of-clones/` still respond as before. The
local shared config is `../deploy/Caddyfile`; update its managed counterpart during
rollout so a later shared-server deploy cannot erase the Moresocial route. Never
replace the whole live Caddyfile with a stale repository copy.

Initial rollout order:

1. Verify server free memory/disk, ports, Docker/Compose and current Caddy configuration.
2. Create Moresocial-only directories, secrets/config and fixed container/network names.
3. Build images for an exact revision, install release under that revision directory.
4. Start DB, run migrations once, then web/worker; verify internal readiness.
5. Install the connector provisioner service and limits; do not reuse EnzoSocial's service.
6. Validate/reload the additive proxy change, verify prefix and existing apps.
7. Operator performs first real Google consent and user pairs WhatsApp; verify bounded sync.
8. Run one real cited-answer/gathering check without sending any messages.
9. Enable Moresocial backup timer; document active revision and rollback target.

Use private directory/file permissions and container UIDs that can access only
their own mounts. No secrets in Docker build contexts; add `.dockerignore` in M0.
No volume pruning, broad Docker cleanup, or edits to existing EnzoSocial state.

Back up Postgres consistently with pg_dump plus encrypted private configuration
and stopped/quiesced connector session copies. Restore-test into disposable names
and directories. Keep 14 days initially; document that on-host backups do not cover
loss of the server and retain deleted content until expiration. Preserve encryption
keys in the private recovery procedure. Never upload these backups to GitHub.

Use additive migrations for the first release. Roll back application images/source
to the previous compatible revision while retaining current data. Never restore an
old DB automatically over new user writes. Document recovery if a migration cannot
be used by the previous version. First deployment rollback removes only the new
route/services and preserves private state for recovery.

## 7. Operator inputs and remaining external steps

Google setup update (2026-09-25): the operator has created the Moresocial Cloud
project and web OAuth client, with `errslima@gmail.com` as support and test user.
The supplied screenshot shows the planned production callback and
`https://1f517.com` JavaScript origin. Local `config/google_auth.json` was validated
without printing credentials: it contains a Google `web` object with client ID,
client secret and the expected production redirect URI. It is ignored and untracked
by Git. Do not ask the operator to create another project/client or paste secrets.

The implementation must load Google's downloaded `web` JSON format. In production,
install this file privately at `/srv/moresocial/secrets/google_auth.json` and mount
it read-only at `/run/secrets/google_auth.json`; configure `GOOGLE_CLIENT_FILE`
to that container path. Never bake it into an image. Runtime must validate the
configured callback against the file's allowed redirect URIs without logging secrets.

API enablement, consent-screen scopes/audience, saved live Console settings, and
successful real OAuth consent have not been independently verified. These remain
operator/live checks; the JSON file alone cannot prove them.

Implementation and synthetic tests can finish before these are available:

- Verify Gmail/Calendar APIs are enabled and the External audience/data-access
  settings match this plan. The project/client and initial tester are already supplied.
- Register localhost callback only for the chosen development server, e.g.
  `http://localhost:8772/moresocial/api/auth/google/callback` when using a local
  proxy that faithfully reproduces prefix stripping. Never add wildcard redirects.
- Initial scopes are identity + Gmail read + Calendar events read. No calendar-list
  or write permission in v1. Console setup is operator-only and not in the UI.
- Generation and embedding provider credentials/models, token budgets, encryption
  key, database password, internal management key, beta allowlist and SSH access.
- Google production verification planning: Testing tokens generally expire after
  seven days for these scopes; restricted Gmail access and server-side processing
  introduce verification/security-assessment requirements subject to exceptions.
  Publishing the consent screen alone is not verification.

Future write features must obtain additional incremental consent. Use `gmail.send`
for sending, `gmail.compose` only if managing Gmail drafts, and
`calendar.events.owned` for editing owned calendars. Do not request broad mail or
calendar management scopes merely to reserve future capabilities.

## 8. Release acceptance checklist

- [ ] One Google onboarding flow; no end-user developer configuration.
- [ ] Two accounts have isolated sources, claims, embeddings, jobs, QR and sessions.
- [ ] Google and WhatsApp sync are bounded, resumable, idempotent and visibly incomplete when applicable.
- [ ] Contact profile facts and assistant answers expose valid local evidence.
- [ ] Corrections/exclusions/deletions survive resync and in-flight jobs.
- [ ] A lasergame event can be organized with drafts and manually confirmed replies.
- [ ] Self-profile is editable; observations are distinguished from explicit facts.
- [ ] No outgoing messages or Google writes; missing evidence is not invented.
- [ ] Prefix routing works for assets, fetches, redirects, callbacks and mobile navigation.
- [ ] CI passes, restore tested, exact deployed revision and rollback documented.
- [ ] Actual provider and server checks reported separately from synthetic tests.

## 9. Official references

- Google server OAuth and offline access: https://developers.google.com/identity/protocols/oauth2/web-server
- Google identity verification: https://developers.google.com/identity/openid-connect/openid-connect
- Gmail scopes: https://developers.google.com/workspace/gmail/api/auth/scopes
- Gmail synchronization: https://developers.google.com/workspace/gmail/api/guides/sync
- Calendar scopes: https://developers.google.com/workspace/calendar/api/auth
- Calendar synchronization: https://developers.google.com/workspace/calendar/api/guides/sync
- WhatsApp library: https://wwebjs.dev/
- Vector storage/search: https://github.com/pgvector/pgvector
- FastAPI proxy prefixes: https://fastapi.tiangolo.com/advanced/behind-a-proxy/
- Caddy prefix stripping: https://caddyserver.com/docs/caddyfile/directives/handle_path
