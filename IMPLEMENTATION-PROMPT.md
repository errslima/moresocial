# Prompt for the implementing agent

Implement Moresocial v1 in this repository following `EXECUTION-PLAN.md`.
Read that file first, then `docs/product-definition.md` and `docs/connections.md`.
The execution plan supplies v1 defaults for previously open product choices.

The public deployment target is `https://1f517.com/moresocial/`, with independent
state and source under `/srv/moresocial` on the existing 1f517 server. All routes,
assets, cookies and OAuth redirects must work under that prefix.

Work through milestones M0–M6 in order. Complete each milestone's implementation
and meaningful acceptance checks before moving on. Record progress and external
dependencies in `docs/implementation-status.md` so another agent can resume.
Use the fixed stack and scoped v1 behavior; avoid architecture redesign or extra features.

Google login must request Gmail/Calendar read access in the same onboarding flow.
End users must never see Google Cloud setup or client credential fields. WhatsApp
must use the EnzoSocial QR-linked WhatsApp Web approach with separate user sessions.
Keep all records, retrieval and background jobs scoped to the signed-in workspace.

Use sibling EnzoSocial code as a reference if available, but make this repository
self-contained. Preserve existing private files, especially `config/google_auth.json`;
never print or commit credentials, real messages or connector sessions.
The operator has already created the Google project/client and supplied valid
Google web-client JSON in that private file, with the production callback included.
Use it through the secret-file configuration in the plan; do not ask for a new
client or for credentials to be pasted into chat. Live API/consent checks remain pending.

Build with synthetic Google/WhatsApp/AI providers and two-user fixtures in tests.
Missing production secrets must not stop implementation, test execution, or
deployment packaging. Implement real adapters too; clearly report which live
checks are pending. Do not claim that fake-provider tests prove live integration.

This handoff requests implementation and deployment preparation. If asked to
deploy as well, use the documented additive rollout, verify the live host first,
and preserve other applications and all existing data. Do not send real messages
or use real inboxes as test fixtures.

Finish with a concise report: milestones completed, verification results,
remaining operator setup, and exact commands to run/test/deploy. Do not claim the
application is finished if any required milestone remains incomplete.
