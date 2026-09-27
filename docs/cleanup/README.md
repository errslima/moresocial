# Codebase cleanup: execution handoff

Prepared: 2026-09-27. Status: plans 1 and 2 reviewed/deployed at `f6cf3eb`; plan 3
group G deployed at `79304e9`, schema `0012`. Group K is retained because the
production inventory found stored credentials. See [rollout evidence](schema-rollout.md).

Repository: `/Users/enzolima/Projects/1f517/moresocial` (its own Git repository,
inside the separate `1f517` checkout). Run all commands from the MoreSocial root.
Audit baseline: `bbb8379`; Alembic head: `0011`. Verify both before starting.

## Execution order

| Order | Plan | Outcome | Database change |
| --- | --- | --- | --- |
| 1 | [Restore the test baseline](01-test-baseline.md) | Tests describe OpenRouter and current product behavior; full suite passes | None |
| 2 | [Remove retired runtime code and refresh documentation](02-runtime-and-docs.md) | Remove personal-key/direct-provider code, manual gathering helpers, dead configuration and obsolete budget-limit handling | None |
| 3 | [Inventory and safely retire historical schema](03-historical-schema.md) | Read-only inventory first; guarded schema removal only for eligible empty data structures | Separate, conditional migration |

Execute sequentially. Plans 1 and 2 are useful and releasable without plan 3.
Do not interleave schema removal into either of them. Plan 3 has an explicit data
decision gate because the audit did not inspect production data.

## Known baseline and authoritative behavior

On the audited checkout, the full local synthetic suite produced:

- Python, including desktop/mobile browser tests: **289 passed, 22 failed**.
- Python without browser tests: **265 passed, 20 failed**.
- Connector: **33 passed**.
- All 22 Python failures concern removed personal AI keys, direct Anthropic adapters,
  or the obsolete `AI_PROVIDER=anthropic` production configuration.

These are historical observations, not counts to force the new suite to match.
Reproduce and classify additional failures; do not assume they are also obsolete.

Current intended behavior:

- Production AI uses one operator-owned OpenRouter configuration. Fake AI is for
  synthetic tests/development; `none` disables AI. Personal AI keys are retired.
- OpenRouter spending limits replace local daily token caps. Usage accounting,
  reservation reconciliation, concurrency limits and provider rate-limit pacing remain.
- Events are recognized automatically, with evidence, corrections and reversible
  identity grouping. Manual gathering creation and invitation planning are retired.
- Suggestions and their drafts, explicit WhatsApp sends, pinned-contact catch-up
  steps, Google imports, people, memory, diary, authentication and diagnostics remain.
- Gmail/Calendar are read-only. WhatsApp sending requires an explicit user action.

Current code and the specific preservation requirements in these plans take
precedence over superseded development plans. Historical documents must not be
used to reintroduce removed features just to make old tests pass.

## Common execution rules

1. Read the chosen plan completely, inspect `git status --short`, current revision,
   applicable `AGENTS.md` files and migration heads. Preserve unrelated changes.
2. Use existing locked dependencies and test fixtures. Do not add back `anthropic`
   or `httpx2`, upgrade dependencies, or change product behavior to satisfy retired tests.
3. Search production code, tests, scripts and migrations before deleting a symbol.
   Decorators, template strings, model metadata and migration imports count as callers.
4. Work locally on synthetic/disposable data. These plans do not request deployment,
   live paid AI calls, messages, credential revocation, or deletion of production data.
   Do not reuse a historical execution plan's live-action authorization.
5. Keep each milestone reviewable. Do not add broad refactors, a new architecture,
   formatter-only churn, or unrelated features. A smaller supported cleanup is preferable
   to silently discarding historical data.
6. Run the focused checks after each affected milestone and the full suite once at
   the end. Fix new failures before proceeding. No `skip`, `xfail`, collection filters
   or relaxed assertions to hide failures. Report unavailable runtimes explicitly.
7. Record actual outcomes in the result section of the executed plan: revision,
   files/behavior changed, tests, compatibility decisions and anything deferred.
   Do not claim a local change is deployed. Do not fabricate missing evidence.

## Verification commands

Preferred full verification (existing project entry point):

```bash
bash scripts/test.sh
```

For focused runs with dependencies already installed:

```bash
uv run --frozen pytest -o addopts='' -q tests/test_openrouter.py
```

The suite uses real disposable PostgreSQL plus pgvector, synthetic providers and
Playwright Chromium. Node 22 is the connector's documented test runtime. Some
sandboxes require permission for the temporary PostgreSQL cache and local listeners;
this is an environment limitation, not proof that application tests failed.
Never point test fixtures at a production database.

Local audit fallbacks, if normal executable discovery fails (verify availability;
these paths are machine-specific): `.venv/bin/python` for Python, and
`/Users/enzolima/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node`
for Node. Record the actual runtime used. Do not modify the user's shell globally.

## Prompt to give the executing model

> Implement `docs/cleanup/01-test-baseline.md` in the MoreSocial repository. Read
> `docs/cleanup/README.md` first. Follow its scope, preservation rules and acceptance
> criteria. Finish the implementation and verification, then record actual results
> in the plan. Do not deploy or work on the next plan in the same task.

After reviewing each result, substitute `02-runtime-and-docs.md`, then
`03-historical-schema.md`. For plan 3, start with its inventory phase; its data
decision gate determines whether schema removal can follow.
