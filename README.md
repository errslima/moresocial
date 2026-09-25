# moresocial
A vectorized approach to social life

Private relationship memory and assistance with planning gatherings and conversations.
Deployment target: **https://1f517.com/moresocial/** on the existing 1f517 server,
with independent application files and state under `/srv/moresocial`.

The first release (milestones M0–M6) is deployed at the URL above. Database, web, worker,
OAuth initiation, and encrypted backup restoration have passed deployment checks.
Interactive Google consent/sync and WhatsApp pairing remain unverified. AI features are
disabled until provider keys are configured. See [implementation status](docs/implementation-status.md).

- [Current architecture and feature map](docs/architecture.md)
- [Implementation status and pending live checks](docs/implementation-status.md)
- [Operator setup, deployment and runbook](docs/operator-setup.md)
- [Implementation execution plan](EXECUTION-PLAN.md)
- [OpenRouter consolidation execution plan](docs/openrouter-execution-plan.md)
- [OpenRouter deployment runbook](docs/openrouter-rollout.md)
- [Prompt to hand to an implementing agent](IMPLEMENTATION-PROMPT.md)
- [Product definition](docs/product-definition.md)
- [Google and WhatsApp connection design](docs/connections.md)

```bash
bash scripts/test.sh   # all checks, synthetic providers only (uv + node 22)
bash scripts/dev.sh    # local synthetic app at http://localhost:8772/moresocial/
```

Stack: Python 3.12, FastAPI, SQLAlchemy/Alembic, PostgreSQL + pgvector, server-rendered
Jinja; a Node `whatsapp-web.js` connector per user; OpenRouter for the operator-paid
generation and embedding models. Model settings are configured by an administrator, not
users. Read-only integrations: Moresocial never sends messages or writes to Gmail or Calendar.

Google credentials and other private configuration must remain outside Git.
Parts of `connector/` and `app/google_sync.py` are adapted from EnzoSocial (GPL-3.0).
