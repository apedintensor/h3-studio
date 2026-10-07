# Agent connection and shared creation API

This is the G2 contract, following [DEC-008](DECISIONS.md) and [PROJECT-PLAN.md](PROJECT-PLAN.md). Implementation, offline acceptance and frontend/production publication are separate gates tracked in [#21](https://github.com/apedintensor/h3-studio/issues/21). A discoverable website URL grants no account access or permission to spend.

## Public discovery and owner authorization

Anonymous GET/HEAD discovery is limited to `/for-agents`, `/llms.txt`, the guide JSON, Skill, reviewed three-file ZIP, `connect.py`, and its digest manifest. These static resources never read private projects or advertise live GPU availability. The ZIP contains `SKILL.md`, `scripts/sixnine.py` and `scripts/connect.py`. A digest proves consistency with the same-origin release, not independent trust in that website.

Only an authenticated browser owner can issue, inspect or revoke `/v1/account/agent-connections`. Writes require an accepted Origin; machine keys cannot mint keys through this interface. Creation requires the explicit `creator-full` profile ID/version and a stable idempotency key. The first response includes a five-minute random code. Only its digest is stored. Repeating issuance returns the same record without reconstructing the code; the owner must revoke an inaccessible pending grant before intentionally creating a replacement.

The signed instruction freezes origin, tenant, account, connection ID, profile version, eight explicit scopes, all of that owner's projects, and a 90-day expiry measured from the grant. The scopes are project create/read/write, asset read/write, job read/write and `assistant:run`. Future scopes do not automatically expand a grant or existing manual PAT. No account, budget, rental or infrastructure authority is granted.

## Client-held key and one-time exchange

The standard-library connection helper validates the exact HTTPS origin (loopback HTTP only for previews), generates a high-entropy PAT plus recovery verifier, and saves them before the first exchange. Windows uses current-user DPAPI; Linux uses an existing Secret Service through `secret-tool`. Unsupported storage, including macOS in this slice, fails before exchange. No dependency is installed automatically and no plaintext credential file is created. External users do not need our internal AI Registry.

The only anonymous write is `POST /v1/agent-connect/exchange`, bounded to an 8 KiB JSON body, with exact fields, cross-site checks and source/known-connection rate limits. It accepts the short-lived code, the token digest/prefix, verifier challenge and expected authorization fingerprint. It never accepts or returns the raw PAT. Account and connection locking atomically consume the grant and insert a normal existing `platform_personal_keys` record. PostgreSQL and SQLite use their respective locking mechanisms; there is no parallel authentication authority.

A lost exchange response is recovered with the original local token/verifier within five minutes of the first claim. Recovery returns the same key metadata; it cannot create another key or extend expiry. A response lost before the first claim permits retrying only the same saved material. Expired, revoked, changed-account, changed-origin or changed-fingerprint authority fails closed. Logout invalidates an unconsumed browser grant; an already-issued PAT retains the existing PAT lifecycle. Password/account changes and explicit revocation invalidate access. Revoke leaves drafts, accepted jobs and assets intact.

The helper has no redirect-following credential path and prints safe connection/account metadata. Codes are read through a hidden prompt or stdin, never CLI arguments. Permanent keys, recovery material and signed URLs must not be put in chat, URLs, ordinary documents or logs. The API/media helper loads a connection with `--connection ID`; it refuses mixed credential sources. Manual process-environment PAT and already-configured internal Registry use remain explicit advanced fallbacks.

## Same creation objects for Agent and browser

After connection, use current authenticated `/v1/quick-chat/schema`, capabilities and the public guide's `quick_chat` examples. A direct turn uses `assistant_mode=none`, `create_card=true`, the current session version and a complete prompt. It creates the same editable session/card seen by the owner without invoking a chat model or submitting generation. Direct cards remain available. The default assistant model ID stays `gemini-3.8-flash`, with `gemma-4-31b-it` as the alternative; catalog entries are not online verification or enablement.

Upload through the session asset route or `sixnine.py upload --session ID`; resume with the same asset/client identity. `--session` and legacy `--project` are mutually exclusive. Do not access hidden compatibility projects. Selection of references remains explicit. Preflight and confirmed submission retain the existing shared generation authority, immutable revisions, budgets, idempotency, jobs and artifact/download authorization. Connecting or authoring a card never starts a GPU. A broad key does not itself authorize a particular paid request.

## Operational and acceptance limits

- The backend can serve discovery/helper resources independently of the frontend release. The canonical Quick Chat connection UI is a separate acceptance/publication gate. Older local mock pages that still require AI Registry are historical and do not override this contract; do not silently publish them.
- Reuse the three additive connection tables with schema creation serialized on PostgreSQL. Connection, rate-limit and audit tables are authentication data excluded by the existing media/business backup policy, alongside other authentication tables. Restoring a business backup requires fresh account credentials/connections.
- Source-local and known-connection rate limits do not establish production reverse-proxy trust. Public TLS/origin/proxy configuration, real browser onboarding, actual OS credential-service availability and multi-process deployment remain explicit release checks.
- Revocation cannot cancel already accepted work by itself. Use existing job cancellation rules; never reset jobs or accounting during credential recovery.
- Fake HTTP/OS stores and isolated SQL tests do not prove a live credential exchange, model availability, paid generation or frontend publication. Their exact evidence belongs in the PR and #21, not this durable contract.
