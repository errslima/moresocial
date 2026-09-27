# moresocial
A vectorized approach to social life

Private relationship memory, automatically recognized events, and conversation assistance.
Deployment target: **https://1f517.com/moresocial/** on the existing 1f517 server,
with independent application files and state under `/srv/moresocial`.

The first release (milestones M0–M6) was deployed at the URL above on 2026-09-25. Later
releases added the shared OpenRouter AI configuration, the Suggestions inbox, explicit
WhatsApp sending, automatically recognized Events and event identity grouping. The social
assistant (event-aware suggestions, an editable persona, prepared event reminders,
check-ins and shareable updates, heartbeat-based WhatsApp health) is implemented locally;
see the [execution plan and log](docs/social-assistant-execution-plan.md). It prepares
drafts only: messages are sent solely when the user presses **Send message**. See
[implementation status](docs/implementation-status.md) for the last recorded release and
the live checks that remain open.

- [Current architecture and feature map](docs/architecture.md)
- [Events: automatic recognition and source context](docs/events.md)
- [Suggestions: behavior and background reviews](docs/suggestions.md)
- [Implementation status and pending live checks](docs/implementation-status.md)
- [Codebase cleanup execution plans](docs/cleanup/README.md)
- [Operator setup, deployment and runbook](docs/operator-setup.md)
- [Temporary agent browser login for live UI checks](docs/agent-browser-login.md)
- [OpenRouter deployment runbook](docs/openrouter-rollout.md)
- [Product definition](docs/product-definition.md)
- [Google and WhatsApp connection design](docs/connections.md)
- [Archived development plans and prompts (historical)](docs/archive/development/README.md)

```bash
bash scripts/test.sh   # all checks, synthetic providers only (uv + node 22)
bash scripts/dev.sh    # local synthetic app at http://localhost:8772/moresocial/
```

Stack: Python 3.12, FastAPI, SQLAlchemy/Alembic, PostgreSQL + pgvector, server-rendered
Jinja; a Node `whatsapp-web.js` connector per user; OpenRouter for the operator-paid
generation and embedding models. Model settings are configured by an administrator, not
users; spending limits are set on the OpenRouter key, and the app reports token usage.
WhatsApp text messages are sent only when the user presses send in a conversation. Gmail
and Calendar remain read-only.

Google credentials and other private configuration must remain outside Git.
Parts of `connector/` and `app/google_sync.py` are adapted from EnzoSocial (GPL-3.0).
