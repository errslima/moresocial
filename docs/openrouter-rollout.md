# OpenRouter rollout runbook

1. Record non-secret counts of operator settings, AI connections and embedding spaces;
   take and verify an encrypted database backup. Deploy the migration before web and
   worker code, then deploy web and worker together so old workers cannot use retired
   providers.
2. Create a dedicated, funded OpenRouter key and set its monetary spending limit in
   OpenRouter. In Admin select exact catalog model IDs and use **Test and save**. The
   synthetic generation and embedding probes are paid calls; record their latency,
   model IDs and returned usage without recording keys or private text. Each attempt
   has a reference (shown in Admin); `logs.py find <reference>` lists its catalog,
   reasoning, embedding, query, validation and commit stages with requested, catalog
   canonical and returned model IDs. The catalog check runs first: an outage or an
   unlisted model fails before any paid call.

   Baseline identity policy (`ai.identity_accepted`): a
   response is accepted if it names the selected catalog ID or the canonical slug
   the catalog documented for it at setup (for example `voyageai/voyage-4` ->
   `voyageai/voyage-4-20260727`). Requests always send the selected ID; the canonical
   identity is saved as `openrouter_embedding_identity` and names the vector space.
   Responses naming a different dated revision are rejected. A native response name
   does not independently attest the upstream revision; the mapping relies on the
   verified catalog relationship.
   The embedding-only compatibility policy (`ai.embedding_identity_accepted`) also
   accepts the observed native response `voyage-4` for exactly that selected ID and
   canonical revision. This was verified from production operation
   `876cc6ebfa91c61019f1dfe196103133` on 2026-09-26. No prefix stripping or
   acceptance of unverified revisions is applied; generation validation stays exact.
3. Watch pending embedding progress while the worker rebuilds. New chunks are written
   to both spaces. Verify an extraction, a cited answer, an invitation draft and an
   older source retrieval. Do not send messages as part of this verification.
4. If generation is unhealthy, disable AI; imports and keyword search remain usable.
   Roll back an embedding index only when its OpenRouter model remains callable and
   coverage is current. Never restore an old database over new writes.
5. After the documented rollback window and verification, use a separately reviewed
   cleanup operation for inert legacy AI secrets and vectors. This release does not
   revoke or delete live legacy credentials. Database backup retention is separate
   from user-data deletion.

OpenRouter and the routed downstream provider process the excerpts required for each
task according to their account and provider policies. The app does not request
`data_collection="deny"` or zero-retention routing, and it does not enable payload
logging.
