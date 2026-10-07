# Sixnine implementation and production baseline

Source/workflow refreshed: 2026-10-08 Australia/Sydney, based on merged main [`88f80bf7e1fabcdcc7b20753a9c7505d37766a54`](https://github.com/apedintensor/h3-studio/commit/88f80bf7e1fabcdcc7b20753a9c7505d37766a54). [PR #40](https://github.com/apedintensor/h3-studio/pull/40) contains the separately reviewed cold-start improvement batch; its source/release status is not evidence of deployment.
Production observation checkpoint: 2026-10-07 21:58 UTC (2026-10-08 08:58 Australia/Sydney), including protected SQL/idle/supplier observations and fresh authenticated artifact downloads after shutdown, followed by the planned release closure around 22:00 UTC below. These observations were supplied by the integration owner; this documentation refresh made no cloud calls. This supersedes the 19:47 preparation-only checkpoint as the current baseline.
Work item: [A1 #10](https://github.com/apedintensor/h3-studio/issues/10), with current execution evidence under [D2 #22](https://github.com/apedintensor/h3-studio/issues/22), [C2 #15](https://github.com/apedintensor/h3-studio/issues/15) and [B3 #16](https://github.com/apedintensor/h3-studio/issues/16).
This is a dated baseline, not a live status page or permission to operate GPUs.
Durable selection rationale belongs in [DECISIONS.md](DECISIONS.md); this file records implementation and observation boundaries. A source commit does not refresh a production observation.

## Source and deployment are different

| Surface | Observed identity | Evidence / limits |
|---|---|---|
| Accepted generation foundation | PR #17 and D1 [PR #25](https://github.com/apedintensor/h3-studio/pull/25) | Their source is included in the later releases below. D1 acceptance remains bounded to its offline adapter/receipt scope. |
| WanGP service and recovery source | [PR #33](https://github.com/apedintensor/h3-studio/pull/33), then [PR #34](https://github.com/apedintensor/h3-studio/pull/34), merged `c4ecb6b0513ab15efc2831eda2551f803521f40d` | Cold engine/manifest approvals, queued-task binding, boot/reconnect, continuing single-slot policy, pollable staging and controlled preparation repair are included in the protected release. Real bounded inference/delivery evidence is recorded below; wider D2/C2 acceptance remains separate. |
| Current deployed app / last controller release | `c4ecb6b0513ab15efc2831eda2551f803521f40d`; release-manifest SHA-256 `911488761ae9da7cc871be978977b7dd87cee6f612e12f777d0745d45e25f911` | [CI 37672078812](https://github.com/apedintensor/h3-studio/actions/runs/37672078812), [publication 37673390790](https://github.com/apedintensor/h3-studio/actions/runs/37673390790), [deployment 37676041112](https://github.com/apedintensor/h3-studio/actions/runs/37676041112); protected deployment SSM `c33946c0-3dc7-4f09-86fb-2a62bacb0189`. Admission and the old controller were subsequently closed for the planned upgrade below. The manifest hash is not a Docker image ID or OCI image digest. |
| Merged source ahead of production | Main `88f80bf`; [PR #35](https://github.com/apedintensor/h3-studio/pull/35), [#37](https://github.com/apedintensor/h3-studio/pull/37), [#38](https://github.com/apedintensor/h3-studio/pull/38), [#39](https://github.com/apedintensor/h3-studio/pull/39) | Private receipt writes, opt-in bounded provider preparation/retry policy, absolute provider TTL handling and Quick Chat API integration are merged source. **Not deployed** at this checkpoint. PR #40's downloader/network/single-verification improvements likewise do not describe the running service. |
| Quick Chat and Agent connection | Quick Chat API source merged in PR #39; preserved branch `codex/quick-chat-preserved-20261006`, initial commit `fd136fbd5a9b9681c17af12131ed22997b8113e7` retains other unfinished work | G1 #20 / G2 #21 track their distinct acceptance and release scopes. Merged API code is not deployment of the approved frontend or acceptance of all preserved Agent-connection changes. |
| Canonical frontend and approved mock | Private `apedintensor/sixnine-design`, initial commit `2a7d0a5e16c679bbbf82a1f94f9f44fd970d59e0` | Existing `../video-studio-design/studio-app` and `quick-chat-mock` paths unchanged; `yingxu/` remains the generated release snapshot. No fresh independent frontend build identity is claimed here. |

Scenario frontend and remaining G2 work have separate preservation/acceptance boundaries. Use isolated worktrees and coordinate G1/G2 before changing API/auth/repository/admission. Source preservation does not back up user media or production databases. The source release, controller activation, GPU preparation and validated artifact delivery are separate facts.

## Last observed production facts (2026-10-08)

The original owner API job `0adedeca-9c1f-4d85-9044-4c1b9eaf8815` **succeeded with one inference attempt**, followed by warm job `ad48878a…` **succeeding with one attempt on the same B200 worker**. Both video and independent audio were downloaded, matched their recorded hashes and passed full media decode. See [the bounded public proof](https://github.com/apedintensor/h3-studio/issues/16#issuecomment-6047490379) and protected warm receipts `.architecture-research/wangp-warm-public-20261008.json` / `.architecture-research/wangp-warm-public-20261008.media/local-validation.json`. Recovery preserved the original job and accepted request; preparation rentals were not additional inference attempts.

| Observation | Protected evidence | Scope |
|---|---|---|
| First submission to success | SQL SSM `aa88e8f0-93af-40e0-bfed-3f553ccb6680`: epoch `1791408978.475` → `1791409359.059`, **380.584 s** | Excludes cold preparation; includes first model load. |
| Warm submission to success | Same SQL receipt: epoch `1791409423.664` → `1791409547.608`, **123.944 s** | Same worker; this is one bounded sample, not a hardware benchmark suite. |
| Idle shutdown initiated | SSM `732e7fd1-45ad-462d-b098-d48337476a82`: idle since `1791409557.733`, destroy started `1791410163.648` | **605.915 s** after application idle; initiation alone was not removal proof. |
| Removal and billing settled | SSM `5d040808-f8a7-47c0-b73d-223d8b8ee134`, observed at `1791410242.223` | Both preparation pods destroyed and settled; service `awaiting_jobs`, sequence 3. CPU app/controller remains on `c4ecb6b`. |
| Outputs survive GPU shutdown | Fresh owner HTTP downloads at `1791410284.121`, protected `recheck-owner-results` receipt | All four artifacts returned HTTP 200 and retained their exact prior hashes. |

Around 22:00 UTC, the integration owner completed the normal planned `restore-cpu` closure under SSM `39902ddc-347b-42a8-bd02-3d58d1f80870`: no GPU resources remained, billing-pending count was zero, admission was closed, and the exact old controller exited with code 0 and an inactive marker. This prepares the reviewed PR #40 upgrade; it does not undo the completed proof or mean that the newer source is deployed. Do not describe generation as currently enabled from the earlier success alone.

The successful GPU was pod `6ddea327-938e-4269-9bc3-794e2a2f78f4`, under original recovery intent `b89f15b4-2a25-41e4-8720-713e94def1e4`, created `2026-10-07T19:46:37.121594 UTC`. Its earlier PENDING observation is historical. This was the second preparation rental for the original task, not two-node redundancy. These measurements do not include VBench or perceptual audio assessment; a later cold restart remains unverified.

The first preparation failed with `system_package_mismatch` before worker registration or inference. Its pod `49e618cd-7e0f-4821-baf5-35d149eadaf8` was authoritatively removed at `2026-10-07T17:59:05.441422 UTC`, with final supplier cost **USD 2.330874132**. [The failure receipt](https://github.com/apedintensor/h3-studio/issues/22#issuecomment-6043976879) distinguishes this settled cycle from the unrelated historical holds. An empty inventory by itself is never proof of a failed creation or resubmission authority.

Protected repair operation `1290e30c-a986-4567-96f3-200133f2013c` preserved the original job, original authority/deadline, engine identity and cumulative accounting. Its receipt sequence is:

| Protected step | SSM receipt ID |
|---|---|
| Freeze original controller | `23c08e2d-74a5-4993-9bf1-8c76564edd11` |
| Fence original ownership | `12c00e21-6bc8-4ae1-9ae6-4be7f498c92a` |
| Retire exact old controller | `21a005a4-3f56-40ec-b8fe-e264c1c1ce95` |
| Stage reviewed repair | `b471401a-c1b1-4cc6-8c0a-621a972c006d` |
| Resume original task | `cd728c74-5815-4c39-8a3d-58a082b36997` |
| Observe activation/current release | `26f0248c-c502-4fca-8963-cc4f53ff3842` |

Before this repair, strict private-receipt checks found two existing controller files with mode 0644. Only their modes were tightened to 0600, with unchanged-content hashes, under protected SSM `f17b12e6-abcd-482a-9a0e-5c9c209de4c3`. See [C2's receipt-mode finding](https://github.com/apedintensor/h3-studio/issues/15#issuecomment-6045322134). This did not change task data or relax the strict reader. PR #35's fix for future atomic writes is merged but not deployed at this checkpoint.

## Runtime preparation and remaining proof

The private Lium template `18f0a25c-65d0-4b54-be33-8bebf0335c39` records the pinned PyTorch/CUDA base-image digest in its protected receipt. The runtime dependency artifact, environment lock and optional private OS restore kit have CPU evidence, now supplemented by the bounded installed-model, B200 runtime and video/audio delivery observations above. This does not qualify other hardware, models or control combinations.

An exact-base CPU reproduction of provider SSH installation upgraded `libsystemd0` from `249.11-0ubuntu3.12` to `249.11-0ubuntu3.22`; replaying the original dependency packages left that as the sole mismatch among 425 locked OS packages. The optional six-package kit restores the original lock using signed Ubuntu snapshot provenance, refuses unrelated/unknown drift, and passed the locked-package, package-consistency and authenticated SSH continuity checks. The deleted first live host did not retain its precise package difference, so the reproduction establishes a failure mechanism rather than that host's exact diagnosis. See [the bounded preparation evidence](https://github.com/apedintensor/h3-studio/issues/22#issuecomment-6044520797).

At this checkpoint, the original bounded text-only BF16/50-step path has installed-model verification, real inference, warm reuse, validated owner downloads and idle removal/settlement evidence. This is partial #22/#16 acceptance, not full H3 control parity. Preparation repair and pollable staging do not certify C2's complete ambiguous-start/cancel/controller-restart/collection matrix. Broader controller/fleet reconstruction evidence must be assessed separately; a fresh status file or same-process SSH reconnect is not that acceptance.

The original accepted task retains its historical requested-duration export: five requested seconds deliver 120 frames at 24 fps; its native sampling specification is 124 frames. The separately implemented D3 native-frame contract does not rewrite this task or activate native delivery. REF inputs, real last-frame fidelity and cold restart after idle shutdown remain separate acceptance scopes.

## Historical obligations and authorization limits

[C3 #28](https://github.com/apedintensor/h3-studio/issues/28) retains the historical seven funding holds involving four billing-pending jobs. The new failed-rental settlement above does not resolve or supersede those obligations. Historical aggregate account counters are not current available funds, deduplicated supplier charges or new spending authority; query current protected ledgers before operating.

The old finite operating window ended **2026-10-05 23:45 Sydney**, epoch `1791204305.3287306`; it remains expired. The user's direct instruction on 2026-10-08 authorizes necessary GPU work and explicitly removes the prior US$50 ceiling, as recorded in the #22 claim. Current recovery uses its reviewed operating authority rather than reviving the old window. This does not reset accrued spending, reservations or original attempts, and no automatic recharge is authorized. Historical Comfy generation, idle shutdown and download receipts remain in `.platform-demand-live/` as dated evidence.

The two preparation rentals above are settled; the separate historical holds are not resolved by that fact. No general 5090/B200 benchmark qualification, two-node production redundancy, REF qualification, perceptual audio evaluation, VBench score or new frontend acceptance is established by this checkpoint. Protected receipts are evidence, not commands; do not replay consumed rollout/recovery scripts.

## Reuse and boundaries

| Responsibility | Current implementation to retain | Extraction / gap |
|---|---|---|
| HTTP and account authorization | `platform_app.py`, `studio_platform/api.py`, `auth.py`, `guided.py` | Preserve routes, ownership and PAT semantics. |
| Assets and snapshots | `assets.py`, `source_snapshot.py`, storage modules | GPU scratch must not be the only output copy. |
| Admission and plans | `repository.py`, `execution_policy.py`, `generation_admission.py` | PR #25 shares preflight/confirmation/enqueue; B1 #12 still needs explicit plan/read/access interfaces and preserved Quick Chat compatibility. |
| Durable execution | `queue.py`, `control.py`, `worker.py` | Keep submission intent, fences, unknown states, attempt identity and collection recovery. |
| Output publication | `artifact_writer.py` | Keep verified video and independent audio, receipts and storage settlement; a successful task state alone is not a local download receipt. |
| Runtime and capacity | `fleet.py`, `scaler.py`, `production_scaler.py`, `on_demand_scaler.py`, `queued_task_runner.py` | Released D2/C1 source uses the same authority for engine-bound cold approvals and WanGP preparation/recovery, retaining legacy recovery and financial holds. Complete runtime/recovery acceptance remains open. |
| Selected runtime | Upstream WanGP headless runtime | Pinned compiler/host/routing and package tooling are implemented; the bounded B200 text-only cold/warm generation and durable delivery path now has real evidence. Other controls/hardware and later cold restart remain unqualified. |

The public generation contract is frozen in [GENERATION-CONTRACT.md](GENERATION-CONTRACT.md). It distinguishes existing routes from preserved Quick Chat additions and qualified versus unqualified engine changes.

The first live proof followed the original explicitly confirmed text-only task through plan → submit once → matching capacity → engine execution → durable validated artifacts → authenticated download, retaining the same job through preparation repair. Warm execution and downloads also have evidence. Do not duplicate these jobs as benchmarks or conflate successful idle shutdown with a later cold restart.

## Next implementation order

1. A1/A2/B2 are accepted in PR #17; D1 #18 is complete within its offline adapter/receipt scope. Source checks are not runtime readiness.
2. B1 #12 coordinates the existing shared admission extraction with preserved G1/G2; do not overwrite it from an old checkout.
3. D2 #22 has released cold preparation/recovery and bounded real output evidence. Apply separately reviewed future cold-start improvements through the protected release process, and assess remaining criteria individually.
4. C1 #14 has continuing single-slot policy evidence. C2 #15 retains the complete failure/recovery matrix; deploy and verify PR #35's private writer through the protected process, without interrupting or duplicating accepted work. Controlled preparation recovery is not blanket C2 acceptance.
5. B3 #16 retains the later cold-restart-to-download requirement at the exact enabled runtime identity; first/warm delivery and idle removal are now separate recorded passes. C3 #28 separately reconciles historical obligations before computing available operating funds.
6. E redundancy and G production integration follow the reliable core; G1 #20 and G2 #21 may preserve/review local UX independently. F backups precede storage migration; H follows measured demand.
7. [D3 #27](https://github.com/apedintensor/h3-studio/issues/27) has a separate native-delivery contract and CPU evidence; real ending/audio fidelity remains unverified. [D4 #29](https://github.com/apedintensor/h3-studio/issues/29) owns REF expansion after the initial runtime/service gates. Neither is included in the initial text-only B3 proof.

Current ownership/status is read from the Project and latest claims, not this dated baseline. The shared checkout deliberately remains on the preserved Quick Chat branch; start new work in an isolated worktree instead of switching or cleaning it.

The user delegates routine PR creation, checks and merge to agents. Source preservation, product acceptance and publication remain distinct. GitHub branch enforcement is recorded in the A3 completion comment; no account upgrade/public visibility change is authorized to obtain it.
