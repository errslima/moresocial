# Archived development plans and prompts

Archived 2026-09-27. These are completed execution plans and agent prompts from earlier
MoreSocial releases. They are kept for implementation rationale, dated evidence and
review history; they are **not** current behavior, runbooks or instructions to act on.
Where they conflict with current documentation or code, the current material wins.
Do not execute the prompts or reuse their deployment, messaging or live-action authorizations.

Current documentation: [README](../../../README.md), [architecture](../../architecture.md),
[implementation status](../../implementation-status.md) and
[operator runbook](../../operator-setup.md). Pending live checks and operator actions that
these plans left open are tracked in the implementation status file.

| File | Description |
| --- | --- |
| [EXECUTION-PLAN.md](EXECUTION-PLAN.md) | First-release (M0–M6) design and deployment contract; its read-only, copy-only and manual gathering defaults were superseded |
| [IMPLEMENTATION-PROMPT.md](IMPLEMENTATION-PROMPT.md) | Prompt given to the agent that implemented the first release |
| [EVENTS-IMPLEMENTATION-PROMPT.md](EVENTS-IMPLEMENTATION-PROMPT.md) | Prompt for the automatic Events milestones E1–E6; manual-planning and provider assumptions are obsolete |
| [api-keys-execution-plan.md](api-keys-execution-plan.md) | Personal Anthropic/OpenAI keys and the admin page (K0–K5); feature since retired |
| [openrouter-execution-plan.md](openrouter-execution-plan.md) | Consolidation onto one operator-owned OpenRouter key; its local token allowances were later removed |
| [diary-observations.md](diary-observations.md) | Diary entries on About me and semantic observation grouping (migration `0007`) |
| [diagnostics-plan.md](diagnostics-plan.md) | Investigation of the failed OpenRouter setup and the structured diagnostics/log-store plan |
| [whatsapp-sidebar-execution-plan.md](whatsapp-sidebar-execution-plan.md) | Left sidebar navigation and the WhatsApp conversation browser (originally read-only) |
| [events-execution-plan.md](events-execution-plan.md) | Original automatic-events plan (E0–E6), including the E0 usage-accounting and rate-limit fixes |
| [event-identity-execution-plan.md](event-identity-execution-plan.md) | Event identity resolver, reversible grouping and corrections (M1–M6), with release results |
