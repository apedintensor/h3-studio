# Sixnine implementation and production baseline

Source/workflow refreshed: 2026-10-08 Australia/Sydney (source/board review only; no new production observation).
Last public health/provider inventory observation: 2026-10-07, 11:23 Australia/Sydney (UTC+11).
Last container/ledger/controller observation: 2026-10-06, 22:20–22:25 Australia/Sydney; those details were not rechecked by the WanGP integration batch.
Work item: [A1 #10](https://github.com/apedintensor/h3-studio/issues/10).
This is a dated baseline, not a live status page or permission to operate GPUs.

## Source and deployment are different

| Surface | Observed identity | Evidence / limits |
|---|---|---|
| Accepted generation foundation | `f51a90567227910cc2f30c55a8cb19b953d3d7a2` (PR #17) | A1/A2/B2 merged; not production-released. Read GitHub for subsequent source-only integration commits. |
| WanGP integration source | `d8c811db79683507a959d3260cdf99b072d0022e` ([PR #25](https://github.com/apedintensor/h3-studio/pull/25)) | D1 #18 accepted offline. Durable adapter/host, configured-slot routing and partial shared admission are implemented. Full image lock, on-demand boot integration and B3 real-generation proof remain incomplete; not production-released. |
| Running production API image tag | `sixnine-platform:73ca224970ffdfae30e1bc7d99c50b2c96ce91af` | Read-only Docker inspection through official AWS MCP/SSM. |
| Running image configuration digest | `sha256:9cda5689e60064ec2f6269b469274824f704f13186707fa5c6f2dc11f1a35ac2` | Docker image ID; do not confuse this with an OCI manifest/index digest. |
| Frontend compatibility label | `sixnine-web-v1` | Fresh `/healthz`; this is not an independently verified frontend build SHA. |
| Quick Chat and Agent connection | Preserved branch `codex/quick-chat-preserved-20261006`, initial commit `fd136fbd5a9b9681c17af12131ed22997b8113e7` | G1 #20 / G2 #21 track acceptance. Draft source preservation is not a production release or acceptance of all included changes. |
| Canonical frontend and approved mock | Private `apedintensor/sixnine-design`, initial commit `2a7d0a5e16c679bbbf82a1f94f9f44fd970d59e0` | Existing `../video-studio-design/studio-app` and `quick-chat-mock` paths unchanged; `yingxu/` remains the generated release snapshot. |

Unpublished source has a remote preservation branch; it differs from both accepted main and production. Use isolated worktrees and coordinate G1/G2 before changing its API/auth/repository/admission. Source-only preservation does not back up user media or production databases. The audit-remediation batch changes no service, cloud instance, budget or production deployment.

## Last observed production facts (2026-10-06)

Read-only refresh on 2026-10-07 at 00:23 UTC confirmed public health/capabilities HTTP 200,
authentication ready, generation/backend/render still disabled, Lium inventory empty,
and the recorded previous pod absent. Official AWS MCP confirmed the known CPU host
running and SSM online. No bill settlement, database read, image inspection or service
change occurred in this refresh; the dated ledger/container details below remain historical.

Official AWS MCP confirmed the known Singapore CPU host is running and managed through SSM. App and PostgreSQL containers were healthy. The finite GPU controller `sixnine-ondemand-duration-20261005.service` was inactive/dead with successful exit status 0.

Read-only `python -m studio_platform.diagnostics` against the running container returned:

| Ledger observation | Value |
|---|---|
| Jobs | 5 total: 4 succeeded, 1 failed |
| Waiting, queued, claimed, submitting, unknown, running, collecting, cancelling, recovery hold | 0 each |
| Expired active leases | 0 |
| Jobs with billing pending | 4 |
| Workers | 3 retired; no other states |
| Instance intents | 4 destroyed; no other states |
| Pending funding reservations | 7 |
| Diagnostic alert | `billing_unsettled_keep_reservations` |

Aggregate account counters were limit **71,109,655**, reserved **5,400,000**, spent **3,690,396** micro-USD. These sum potentially overlapping account ledgers. They are **not** deduplicated supplier charges, remaining GPU authorization, or available cash. Preserve the 7 reservations until explicit settlement evidence resolves them. [C3 #28](https://github.com/apedintensor/h3-studio/issues/28) owns the fresh reconciliation and evidence-backed disposition; this workflow update does not refresh the historical counts or settle anything.

Separate GET-only verification at 11:25 UTC:

- `https://www.sixnine.art/healthz`: HTTP 200, authentication ready, generation **disabled**, backend **disabled**, render disabled.
- Authenticated `/v1/capabilities`: HTTP 200; both `h3-base-fl2va-v1` and `h3-base-ref2va-v1` execution support **disabled**; simulation false.
- Lium `/pods`: HTTP 200, 0 rows. Exact GET of the previously recorded pod: HTTP 404.
- Supplier billing statements were not freshly reconciled. Empty inventory/404 is not proof of settled billing and does not authorize repeating an ambiguous creation.

Read-only SSM receipts: `bd5adba8-2e9d-42a5-8932-f9aefd3a7541` and `4b824dee-f809-4fdb-9e8c-bd8a03656a51`, both success/exit 0. Commands read selected Docker/systemd facts and aggregate database diagnostics, not secrets, prompts, media or unrestricted logs. Sanitized GET evidence is retained locally in `.architecture-research/a1-live-public-provider-20261006.json`.

## Historical evidence and its limits

The last recorded finite operating window ended **2026-10-05 23:45 Sydney**, epoch `1791204305.3287306`. The new architecture does not renew it. Historical successful Comfy jobs, idle shutdown, artifact download/range checks and runtime acceptance are recorded under `.platform-demand-live/`; those private operator receipts are not required to understand this English baseline.

Correction to older prose: the earlier release receipt described **6 terminal-job funding reservations involving 3 jobs**, not 6 terminal jobs. The fresh count above is a later snapshot: 4 billing-pending jobs and 7 reservations. Neither should overwrite the other or be treated as rental settlements.

No fresh frontend build identity, supplier final invoice reconciliation, WanGP GPU inference, 5090/B200 capacity qualification, two-node production redundancy, or ongoing production GPU window was established by A1.

## Reuse and boundaries

| Responsibility | Current implementation to retain | Extraction / gap |
|---|---|---|
| HTTP and account authorization | `platform_app.py`, `studio_platform/api.py`, `auth.py`, `guided.py` | Preserve routes, ownership and PAT semantics. |
| Assets and snapshots | `assets.py`, `source_snapshot.py`, storage modules | GPU scratch must not be the only output copy. |
| Admission and plans | `repository.py`, `execution_policy.py`, merged `generation_admission.py` | PR #25 shares preflight/confirmation/enqueue; B1 #12 still needs explicit plan/read/access interfaces and preserved Quick Chat compatibility. |
| Durable execution | `queue.py`, `control.py`, `worker.py` | Keep submission intent, fences, unknown states, attempt identity and collection recovery. |
| Output publication | `artifact_writer.py` | Keep verified video and independent audio, receipts and storage settlement. |
| Runtime and capacity | `fleet.py`, `scaler.py`, `production_scaler.py`, `on_demand_scaler.py`, `queued_task_runner.py` | PR #25 binds configured slots and attempts to engine/manifest and preserves legacy recovery. On-demand approval/bootstrap/readiness still need the WanGP strategy in D2 #22. |
| Selected next engine | Upstream WanGP headless runtime | PR #25 adds the offline adapter, compiler, host and configured-slot routing. No real runtime installation, GPU qualification or production switch is established. |

The public generation contract is frozen in [GENERATION-CONTRACT.md](GENERATION-CONTRACT.md). It distinguishes existing routes from local Quick Chat additions and proposed engine changes.

The first live proof is one explicitly confirmed task: upload/resolve owned media → plan → submit once → matching capacity → engine execution → durable validated artifacts → authenticated download. A warm follow-up and a later cold restart test are separate observations. Use the accepted user task and deliver its output; do not duplicate it as a benchmark. Real execution awaits applicable release, operating-window and remaining-budget checks.

## Next implementation order

1. A1/A2/B2 are accepted in PR #17. A3 #19 protects source and reconciles guidance; source checks do not prove runtime readiness.
2. B1 #12 coordinates the existing shared admission extraction with preserved G1/G2; do not overwrite it from an old checkout.
3. D1 #18 is complete within its offline adapter/receipt scope. D2 #22 has partial pinned compilation/transport/routing implementation; next implement its engine-bound cold approvals/bootstrap/recovery and complete the runtime lock. Read its criterion-level evidence before claiming remaining work.
4. C1 #14 separates configuration/continuing policy from finite tests; C2 #15 exercises ambiguous start/cancel/restart/collection under the resulting contract.
5. B3 #16 depends on the above gates and current operating authority for one real public cold/warm/idle-restart-to-download slice. WanGP source integration exists; the live path is not yet qualified. C3 #28 reconciles historic supplier obligations before calculating available operating funds.
6. E broad redundancy and G production integration follow the reliable core; G1 #20 and G2 #21 may preserve/review local UX independently. F backups precede storage migration; H follows measured demand.
7. [D3 #27](https://github.com/apedintensor/h3-studio/issues/27) owns native/delivered duration and last-frame fidelity; its CPU reproduction may proceed now, while real validation waits for D2. [D4 #29](https://github.com/apedintensor/h3-studio/issues/29) owns REF capability expansion after the initial runtime/service gates. Neither is silently included in the initial text-only B3 proof.

The 2026-10-08 workflow follow-through is [A4 #26](https://github.com/apedintensor/h3-studio/issues/26). Current ownership/status is read from the Project and latest claims, not this dated baseline. The shared checkout deliberately remains on the preserved Quick Chat branch; start new work in an isolated worktree instead of switching or cleaning that checkout.

The user delegates routine PR creation, checks and merge to agents. Source preservation, product acceptance and publication remain distinct. GitHub branch enforcement is recorded in the A3 completion comment; no account upgrade/public visibility change is authorized to obtain it.
