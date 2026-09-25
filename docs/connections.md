# Connections — implementation and setup

Status: implemented and deployed on 2026-09-25. OAuth initiation is verified; real user
consent/sync and WhatsApp pairing are pending. See [implementation status](implementation-status.md).

## Reference implementation

- `enzosocial/accounts/main.py`: Google sign-in with `openid email profile`,
  authorization code flow, PKCE, state, nonce, and verified Google ID tokens.
- `enzosocial/app/google_connection.py`: separate Gmail/Calendar authorization,
  offline access, refresh tokens, and read-only API calls.
- `enzosocial/connector/index.js` and `connector/package.json`:
  `whatsapp-web.js` pinned to 1.34.7, QR pairing, LocalAuth, browser automation,
  persistent sessions, history synchronization, and a separately guarded send path.
- The existing connector has custom compatibility code; reuse requires reviewing
  those patches and synchronization behavior, not just copying its dependency.

## Google account model

Configure one Moresocial-owned web OAuth client per environment server-side.
Users sign in and authorize their own data; they do not supply developer credentials.
Confirmed onboarding requirement: one **Continue with Google** entry point starts
an authorization flow requesting identity plus Gmail and Calendar read permissions
together. After choosing their account and granting Google's consent, the user
returns signed in with the granted data connections ready to synchronize.
Identify sign-in accounts by verified Google subject ID.

End users never create Google Cloud projects, enable APIs, configure callbacks,
or enter client IDs/secrets. Do not reproduce EnzoSocial's developer setup panel.
Google controls the account-selection and consent screens; this requirement means
one application onboarding flow, not a guarantee of one Google screen.

Returning users with valid grants should not be forced through consent again.
If a user declines a data permission, preserve successful sign-in, clearly show
which feature is unavailable, and offer a simple authorization retry. Revoked or
expired access gets a Reconnect Google action, never developer setup instructions.

Registered and implemented unified callback path:

- `https://1f517.com/moresocial/api/auth/google/callback`

Production configuration uses `PUBLIC_ORIGIN=https://1f517.com` and
`BASE_PATH=/moresocial`. Register the exact
production callback above; choose development URLs separately. Later permission upgrades and
reconnection can use this callback with a server-validated flow purpose and state.

## Permission stages

Google API scopes below use the prefix `https://www.googleapis.com/auth/`.

| Stage | Scopes | Purpose |
| --- | --- | --- |
| Initial Google onboarding | `openid email profile` | Authenticate and display basic account information |
| Same initial onboarding | `gmail.readonly` | Read email for user-authorized memory features |
| Same initial onboarding | `calendar.events.readonly` | Read events |
| Calendar selection, if offered | `calendar.calendarlist.readonly` | List calendars the user can choose to sync |
| Later email sending | `gmail.send` | Send user-approved messages |
| Later Gmail draft management, if needed | `gmail.compose` | Manage Gmail drafts and send mail |
| Later inbox organization, if needed | `gmail.modify` | Modify messages/labels; request only for a concrete feature |
| Later Calendar writing | `calendar.events.owned` | Create/update/delete events on calendars the user owns |

Use `calendar.events` instead of `calendar.events.owned` only if editing events on
other writable calendars is required. Drafting text inside Moresocial does not
require Gmail write permission.

Request future permissions incrementally when the relevant feature is enabled.
Read consent does not permit future writes automatically. Check actual granted
scopes and support partial consent. Request offline access for background sync;
store refresh tokens server-side, isolated by user and protected at rest.

## Google Console setup — operator only

These steps are performed by the Moresocial operator, never by end users.

Setup recorded 2026-09-25: the operator created the Cloud project and web client,
with `errslima@gmail.com` as support and tester email. The screenshot and local
credential export include the exact production callback above. The local file
`config/google_auth.json` is valid Google web-client JSON, includes both credentials,
and is Git-ignored and untracked. Its contents must remain private. Load the nested
`web` object; production secret path is `/srv/moresocial/secrets/google_auth.json`,
mounted read-only as `/run/secrets/google_auth.json` via `GOOGLE_CLIENT_FILE`.

The following list remains the setup reference; project/client creation is already
done. API enablement and consent configuration were reported complete by the operator.
The deployed authorization request includes both read scopes; actual consent and sync
still need interactive verification.

1. Create a dedicated Moresocial Google Cloud project.
2. Enable Gmail API and Google Calendar API.
3. Configure Google Auth Platform branding and an External audience.
4. During Testing, add Enzo and other initial testers as test users.
5. Configure Data Access with the initial scopes above; add future scopes when implemented.
6. Create a Web application OAuth client and register exact callback URLs.
7. Store the client secret outside source control; configure it on the server.
8. Implement authorization requests with the scopes: Console configuration alone
   does not cause the application to request or receive permission.

External Testing refresh tokens generally expire after seven days when requesting
these data scopes. Public launch requires planning for Google's verification;
Gmail read access is restricted, and server storage/transmission can require a
security assessment unless an exception applies. Production status alone does
not mean the application is verified.

## WhatsApp

Use QR pairing through WhatsApp Web, matching EnzoSocial. Each user needs a separate
browser/authentication session and storage. Make connection state, reconnection,
and available history visible; do not promise complete historical access.

This is an unofficial integration. The library documents account-blocking risk,
and WhatsApp changes can break compatibility. Keep the connector replaceable and
account for per-user browser resource costs in the multi-user design.

## References

- https://developers.google.com/identity/protocols/oauth2/web-server
- https://developers.google.com/workspace/gmail/api/auth/scopes
- https://developers.google.com/workspace/calendar/api/auth
- https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification
- https://wwebjs.dev/
