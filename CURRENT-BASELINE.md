# Sixnine implementation and production baseline

Observed: 2026-10-06, 22:20–22:25 Australia/Sydney (UTC+11).
Work item: [A1 #10](https://github.com/apedintensor/h3-studio/issues/10).
This is a dated baseline, not a live status page or permission to operate GPUs.

## Source and deployment are different

| Surface | Observed identity | Evidence / limits |
|---|---|---|
| Repository HEAD and origin/main | `99755e60d0f1ce1e3fe09295a3de3dafbab4360b` | Local Git and remote tracking state; workflow baseline. |
| Running production API image tag | `sixnine-platform:73ca224970ffdfae30e1bc7d99c50b2c96ce91af` | Read-only Docker inspection through official AWS MCP/SSM. |
| Running image configuration digest | `sha256:9cda5689e60064ec2f6269b469274824f704f13186707fa5c6f2dc11f1a35ac2` | Docker image ID; do not confuse this with an OCI manifest/index digest. |
| Frontend compatibility label | `sixnine-web-v1` | Fresh `/healthz`; this is not an independently verified frontend build SHA. |
| Local Quick Chat and Agent connection | Unpublished working changes | Includes new session/turn/card/admission/assistant/connect modules and changes to API/auth/repository. Preserve them; not part of the deployed image evidence. |
| Canonical frontend | `../video-studio-design/studio-app` | `yingxu/` is the generated release snapshot. Approved UX is the Quick Chat mock; publication remains separately gated. |

The checkout was already dirty before this batch. Its API source includes changes absent from HEAD and production. Do not use an unqualified “current API” to describe all three. No source reset, migration, service start/stop, rental, or deployment was performed for this inventory.

## Fresh production facts

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

Aggregate account counters were limit **71,109,655**, reserved **5,400,000**, spent **3,690,396** micro-USD. These sum potentially overlapping account ledgers. They are **not** deduplicated supplier charges, remaining GPU authorization, or available cash. Preserve the 7 reservations until explicit settlement evidence resolves them.

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
| Admission and plans | `repository.py`, `execution_policy.py`; local `generation_admission.py` | Coordinate unpublished extraction; do not introduce another admission authority. |
| Durable execution | `queue.py`, `control.py`, `worker.py` | Keep submission intent, fences, unknown states, attempt identity and collection recovery. |
| Output publication | `artifact_writer.py` | Keep verified video and independent audio, receipts and storage settlement. |
| Runtime and capacity | `fleet.py`, `scaler.py`, `production_scaler.py`, `on_demand_scaler.py`, `queued_task_runner.py` | Comfy-specific readiness/global backend selection still obstruct safe engine coexistence. |
| Selected next engine | Upstream WanGP headless runtime | User authorization confirmed. Not installed, routed or GPU-qualified by this inventory. |

The public generation contract is frozen in [GENERATION-CONTRACT.md](GENERATION-CONTRACT.md). It distinguishes existing routes from local Quick Chat additions and proposed engine changes.

The first live proof is one explicitly confirmed task: upload/resolve owned media → plan → submit once → matching capacity → engine execution → durable validated artifacts → authenticated download. A warm follow-up and a later cold restart test are separate observations. Use the accepted user task and deliver its output; do not duplicate it as a benchmark. Real execution awaits applicable release, operating-window and remaining-budget checks.

## Next implementation order

1. A2: freeze compatibility, recovery and acceptance invariants.
2. B2: extract the existing inference adapter and readiness seam without changing routing; retain old imports and historical jobs.
3. B1/D: complete shared admission seams, per-attempt engine/config binding and private WanGP adapter/receipt. Review all GPU capacity gates before adding any backend value.
4. C: exercise ambiguous start, cancellation, process loss and collection recovery; separate continuing service policy from finite acceptance policy.
5. B3/D: authorized real-GPU qualification, public download proof and rollback before switching new jobs.
6. E/G: redundancy and approved scenario integration; F backups start before any storage migration; H follows measured demand.
