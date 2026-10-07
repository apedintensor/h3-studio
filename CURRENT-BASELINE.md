# Sixnine implementation and production baseline

Source/workflow refreshed: 2026-10-08 Australia/Sydney.
Production observation checkpoint: 2026-10-08, 03:30 Australia/Sydney (UTC+11), including the protected retirement receipt below.
Working integration source: `codex/runtime-service-20261008`, [`21fcc191fcaf9ac3f4718b07e5498088a8e3877e`](https://github.com/apedintensor/h3-studio/commit/21fcc191fcaf9ac3f4718b07e5498088a8e3877e); **D2/C1 PR pending — attach its final revision and CI to the acceptance receipt**.
Work item: [A1 #10](https://github.com/apedintensor/h3-studio/issues/10).
This is a dated baseline, not a live status page or permission to operate GPUs.
Durable selection rationale belongs in [DECISIONS.md](DECISIONS.md); this file records implementation and observation boundaries. A source commit does not refresh a production observation.

## Source and deployment are different

| Surface | Observed identity | Evidence / limits |
|---|---|---|
| Accepted generation foundation | `f51a90567227910cc2f30c55a8cb19b953d3d7a2` (PR #17) | A1/A2/B2 merged; not production-released. Read GitHub for subsequent source-only integration commits. |
| WanGP integration source | D1 accepted offline in [PR #25](https://github.com/apedintensor/h3-studio/pull/25); current D2/C1 working reference above | Cold engine/manifest approvals, queued-task binding, a WanGP strategy in the existing BootController, incarnation-bound transport and continuing single-slot policy are implemented in the working branch. Actual target dependency/model lock qualification, complete controller-process restart recovery and B3 real-generation proof remain incomplete; no new application release. |
| Running production API image tag | `sixnine-platform:73ca224970ffdfae30e1bc7d99c50b2c96ce91af` | Read-only Docker inspection through official AWS MCP/SSM. |
| Running image configuration digest | `sha256:9cda5689e60064ec2f6269b469274824f704f13186707fa5c6f2dc11f1a35ac2` | Docker image ID; do not confuse this with an OCI manifest/index digest. |
| Frontend compatibility label | `sixnine-web-v1` | Fresh `/healthz`; this is not an independently verified frontend build SHA. |
| Quick Chat and Agent connection | Preserved branch `codex/quick-chat-preserved-20261006`, initial commit `fd136fbd5a9b9681c17af12131ed22997b8113e7` | G1 #20 / G2 #21 track acceptance. Draft source preservation is not a production release or acceptance of all included changes. |
| Canonical frontend and approved mock | Private `apedintensor/sixnine-design`, initial commit `2a7d0a5e16c679bbbf82a1f94f9f44fd970d59e0` | Existing `../video-studio-design/studio-app` and `quick-chat-mock` paths unchanged; `yingxu/` remains the generated release snapshot. |

Unpublished scenario source has a remote preservation branch; it differs from both accepted main and production. Use isolated worktrees and coordinate G1/G2 before changing its API/auth/repository/admission. Source-only preservation does not back up user media or production databases. This batch's protected operational preparation is listed separately below; it did not deploy the new application or prove WanGP inference.

## Last observed production facts (2026-10-08)

Official AWS MCP/SSM and selected read-only SQL/health checks confirmed the Singapore CPU host, unchanged production application identity above, healthy app/database, and disabled generation/backend/render. Authenticated public capabilities remained available with execution disabled. Official Lium GET inventory returned zero pods. These observations do not prove inference or settled supplier billing.

The old controller unit `sixnine-ondemand-duration-20261005.service` was inactive/dead with exit 0. The integration owner then archived the exact completed generation's `control`, `operator` and `public-source` directories and selected inactive marker/container metadata before removing only its naturally exited container `sixnine-finite-82878de271c3a3946af8`. Protected apply receipt: SSM `12eaa07d-8bd8-4c63-8cb7-e10b2b093930`. It confirmed an unchanged ledger; application containers, SSH identity, media and database were preserved. The archive is retained on the same host, not an independently verified backup. No new controller was enabled by retirement.

Fresh bounded database observations returned:

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

Aggregate account counters remained limit **71,109,655**, reserved **5,400,000**, spent **3,690,396** micro-USD. These sum potentially overlapping account ledgers. They are **not** deduplicated supplier charges, remaining GPU authorization, or available cash. Preserve the 7 reserved entries until explicit settlement evidence resolves them. [C3 #28](https://github.com/apedintensor/h3-studio/issues/28) owns evidence-backed disposition; retirement changed no account or reservation.

Fresh read-only supplier statements for the three known charged pods agree with the existing GPU account's **3,690,396 micro-USD** spending after per-statement rounding. That comparison does not allocate costs to the four billing-pending jobs or settle their seven holds. Empty inventory does not prove the outcome of an unknown creation and is not resubmission authority.

The selected private Lium template `18f0a25c-65d0-4b54-be33-8bebf0335c39` records a pinned base-image digest in its protected receipt. This is base-template preparation, not a qualified WanGP image: actual Python/system/wheel capture, installed model verification and GPU runtime checks are still required. No pod or new WanGP inference existed at this checkpoint.

Selected read-only receipts include SSM `66394e74-5621-48b4-aaca-850f43c9c077` (exact old container identity) and `0247fc19-92be-4a95-b6fe-18776cc5fa06` (corrected SQL/health probe). They return allowlisted metadata, not credentials, prompts, user media or broad logs. Protected local receipts remain historical evidence; do not replay consumed operational scripts.

## Historical evidence and its limits

The old finite operating window ended **2026-10-05 23:45 Sydney**, epoch `1791204305.3287306`; it remains expired. The user's new direct instruction on 2026-10-08 authorizes necessary GPU work and explicitly removes the prior US$50 ceiling, as recorded in the current #22 claim. This does not reset accrued spending, reservations or original attempts. New operating configuration still requires a reviewed finite safety ceiling/window and exact runtime identity; no automatic recharge is authorized. Historical successful Comfy jobs, idle shutdown, downloads and acceptance remain in `.platform-demand-live/` as dated evidence.

Correction to older prose: the earlier release receipt described **6 terminal-job funding reservations involving 3 jobs**, not 6 terminal jobs. The fresh count above is a later snapshot: 4 billing-pending jobs and 7 reservations. Neither should overwrite the other or be treated as rental settlements.

No fresh frontend build identity, completed job-billing settlement, WanGP GPU inference, 5090/B200 capacity qualification, two-node production redundancy, or enabled new production operating window is established by this checkpoint.

## Reuse and boundaries

| Responsibility | Current implementation to retain | Extraction / gap |
|---|---|---|
| HTTP and account authorization | `platform_app.py`, `studio_platform/api.py`, `auth.py`, `guided.py` | Preserve routes, ownership and PAT semantics. |
| Assets and snapshots | `assets.py`, `source_snapshot.py`, storage modules | GPU scratch must not be the only output copy. |
| Admission and plans | `repository.py`, `execution_policy.py`, merged `generation_admission.py` | PR #25 shares preflight/confirmation/enqueue; B1 #12 still needs explicit plan/read/access interfaces and preserved Quick Chat compatibility. |
| Durable execution | `queue.py`, `control.py`, `worker.py` | Keep submission intent, fences, unknown states, attempt identity and collection recovery. |
| Output publication | `artifact_writer.py` | Keep verified video and independent audio, receipts and storage settlement. |
| Runtime and capacity | `fleet.py`, `scaler.py`, `production_scaler.py`, `on_demand_scaler.py`, `queued_task_runner.py` | Current D2/C1 source extends the same authority to engine-bound cold approvals and WanGP boot/reconnect, retaining legacy recovery and financial holds. Actual target qualification and full controller-process restart acceptance remain open. |
| Selected next engine | Upstream WanGP headless runtime | Offline compiler/host/routing and target package tooling are implemented. No real runtime installation, GPU qualification or production switch is established. |

The public generation contract is frozen in [GENERATION-CONTRACT.md](GENERATION-CONTRACT.md). It distinguishes existing routes from local Quick Chat additions and proposed engine changes.

The first live proof is one explicitly confirmed task: upload/resolve owned media → plan → submit once → matching capacity → engine execution → durable validated artifacts → authenticated download. A warm follow-up and a later cold restart test are separate observations. Use the accepted user task and deliver its output; do not duplicate it as a benchmark. Real execution awaits applicable release, operating-window and remaining-budget checks.

## Next implementation order

1. A1/A2/B2 are accepted in PR #17. A3 #19 protects source and reconciles guidance; source checks do not prove runtime readiness.
2. B1 #12 coordinates the existing shared admission extraction with preserved G1/G2; do not overwrite it from an old checkout.
3. D1 #18 is complete within its offline adapter/receipt scope. Current D2 source implements cold approvals/bootstrap/reconnect; next freeze and verify the actual target environment/models, qualify the runtime and map remaining recovery cases. Read #22 criterion-level evidence; do not treat implemented tooling as an installed lock or real output.
4. C1 #14 now has an explicit continuing single-slot policy implementation with offline and isolated PostgreSQL evidence. C2 #15 still owns the combined ambiguous start/cancel/restart/collection matrix. Same-process SSH recovery is not full controller-process restart acceptance.
5. B3 #16 depends on the above gates and current operating authority for one real public cold/warm/idle-restart-to-download slice. WanGP source integration exists; the live path is not yet qualified. C3 #28 reconciles historic supplier obligations before calculating available operating funds.
6. E broad redundancy and G production integration follow the reliable core; G1 #20 and G2 #21 may preserve/review local UX independently. F backups precede storage migration; H follows measured demand.
7. [D3 #27](https://github.com/apedintensor/h3-studio/issues/27) owns native/delivered duration and last-frame fidelity; its CPU reproduction may proceed now, while real validation waits for D2. [D4 #29](https://github.com/apedintensor/h3-studio/issues/29) owns REF capability expansion after the initial runtime/service gates. Neither is silently included in the initial text-only B3 proof.

The 2026-10-08 workflow follow-through is [A4 #26](https://github.com/apedintensor/h3-studio/issues/26). Current ownership/status is read from the Project and latest claims, not this dated baseline. The shared checkout deliberately remains on the preserved Quick Chat branch; start new work in an isolated worktree instead of switching or cleaning that checkout.

The user delegates routine PR creation, checks and merge to agents. Source preservation, product acceptance and publication remain distinct. GitHub branch enforcement is recorded in the A3 completion comment; no account upgrade/public visibility change is authorized to obtain it.
