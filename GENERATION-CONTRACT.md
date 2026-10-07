# Generation lifecycle and compatibility contract

Version: **generation-contract-v1**, frozen 2026-10-06 for A2 ([#11](https://github.com/apedintensor/h3-studio/issues/11)).
Baseline: [CURRENT-BASELINE.md](CURRENT-BASELINE.md). Direction: [PROJECT-PLAN.md](PROJECT-PLAN.md). Decision rationale: [DECISIONS.md](DECISIONS.md).
Source-status wording reviewed against PR #25 / merge `d8c811db79683507a959d3260cdf99b072d0022e` on 2026-10-08; the v1 API and recovery semantics are unchanged. This editorial review is not a production observation.
This is the binding contract for incremental B/C/D work. “Required” below is an acceptance condition, not a claim that every future engine is implemented. Existing narrower queue/worker/fleet/storage contracts remain applicable. Material semantic changes require updating this version and its issue before implementation.

## 1. One business API and ledger

Web and Agent clients use the same authenticated owner, projects, assets, plans, jobs and artifacts. Scenario services own creative documents; the generation service owns admission; the durable queue owns execution; adapters own engine protocol; the capacity controller owns provisioning. A runtime queue or MCP server is never a second business authority.

Keep current IDs and field types. Recipe IDs, model IDs, configuration IDs, engine IDs and credential profile IDs are different identities. Existing recipes must not silently acquire a different model, precision, compiler or control meaning.

## 2. Public route compatibility map

| Existing interface | Contract to preserve | Target service |
|---|---|---|
| `/api/auth/*`, account API-key routes | Browser session and scoped Bearer credentials identify the caller; request-supplied owner is not authoritative. | Identity/authentication |
| `/v1/projects` and guided entity/action routes | Optimistic document versions, owner/project permissions, existing story and shot IDs. | Scenario services |
| `POST /v1/assets`, `GET /v1/assets`, asset content/derivative routes | `client_project_id` in the multipart upload/query identifies a project subject to authorization; server inspection, stable media ID, explicit selected clips and owner-scoped access remain. | Asset service |
| `GET /v1/capabilities` | Existing capability version, recipes, typed controls and `execution_support`; upload validity is not execution qualification. | Generation capabilities |
| `POST /v1/generation-plans` and per-shot plan/draft routes | Validated source snapshot, normalized input/control request, native output specification, policy snapshot, quote and expiry. Existing source/project/shot binding remains. | Generation admission |
| `POST /v1/jobs` | Exactly `{ "plan_id": "..." }` plus `Idempotency-Key`; HTTP 202 does not mean GPU started or result completed. | Generation admission |
| `GET /v1/jobs`, `GET /v1/jobs/{job_id}`, activity summary | Same job identity, scoped visibility and truthful persisted state/error code. | Job queries |
| `POST /v1/jobs/{job_id}/cancel` | Cancellation request is distinct from proven stop; repeat calls preserve identity. | Job control |
| `GET /v1/jobs/{job_id}/artifacts`, artifact content/download | Stable artifact IDs, kind/MIME/size/hash/metadata and authenticated content routes; Range support remains. | Artifact delivery |
| Existing batch routes | Batch/item identities and common job admission; no competing engine batch queue. | Generation batches |

The preserved, not-yet-merged `/v1/quick-chat/sessions/...` routes add session/turn/card/revision/submission/item/execution identities above the same job ledger; see [G1 #20](https://github.com/apedintensor/h3-studio/issues/20), [G2 #21](https://github.com/apedintensor/h3-studio/issues/21) and draft [PR #23](https://github.com/apedintensor/h3-studio/pull/23). Shared draft preflight, confirmation and enqueue through `GenerationAdmission` are merged in [PR #25](https://github.com/apedintensor/h3-studio/pull/25). Explicit access/plan/read extraction and preserved Quick Chat parity remain [B1 #12](https://github.com/apedintensor/h3-studio/issues/12); do not create a duplicate admission authority. Hidden project/shot projections are internal compatibility mappings, not separately editable sources.

Public jobs retain `id`, `status`, `phase`, `request_hash`, `error_code`, `plan_id`, `project_id`, `client_ref`, `recipe_id`, `effective_request`, `simulation`, `result` and `artifacts`. Today `phase` mirrors status. Future progress details are additive; do not replace old strings with a new incompatible enum or silently return a different envelope.

Current errors are HTTP 401 authentication, 403 browser-origin rejection, 404 missing/inaccessible resources, 409 conflicts, 413 size limits, 422 invalid requests, 429 budget/capacity limits and 503 transient service failures. Existing `detail` responses are not yet a universal structured-error schema. Add safe codes compatibly; never expose raw provider exceptions, secrets, filesystem paths or signed URLs. Private prompts/media are only returned through existing authorized user-data responses, not operational logs.

Direct generation independent of a story is a target, not an invented available endpoint. Preserve current source mappings until a versioned contract and migration exist.

## 3. Immutable admission and replay

1. Authenticate and authorize the real principal on every write/read, including Agent calls. Preserve project scope and account-change protection.
2. Resolve media from the asset service and snapshot selected derivatives/clips, owner, source versions and content identity. Never accept a client path or arbitrary URL as a trusted model input.
3. Freeze normalized controls, canonical seed (existing decimal string), request/source hashes, requested duration and native output geometry/frame duration. When an operator-qualified native delivery policy is selected, also freeze its explicit delivery specification in the execution plan. Later user edits cannot change accepted work.
4. Plan/preflight/assistant/card creation must not provision a GPU or start video generation. Only explicit confirmed admission can start paid video inference or demand-triggered GPU provisioning. Assistant model calls and storage have separate authorization/cost semantics and are not claimed to be free. Reservations and job admission are transactional; unknown cost is not zero.
5. Legacy idempotency namespace is tenant/owner/project/actor/key. Same key with equivalent admitted data returns the existing job; conflicting data rejects. Do not globally change that namespace.
6. Quick Chat additionally uses stable submission/item/execution business identity across Agents. Its queue actor is an internal idempotency namespace, not an authenticated user. Preserve actual caller authorization separately.
7. Local inert-plan refresh is a narrow exception: only planned/blocked Quick Chat jobs, same source/recipe identity, no lease/attempt/history/reservation. It keeps job ID and does not enqueue. No accepted execution snapshot is mutable through refresh.
8. Network replay, reconciliation, collection retry, user retry and another variation are distinct actions. User retry can create a linked new job only under the existing explicit item-retry policy; the original failure remains visible.

## 4. Lifecycle and ownership

| Persisted state | Authority and required next behavior |
|---|---|
| `planned`, `blocked` | Saved intention; no generation implied. Explain admission blockers. |
| `waiting_capacity` | Accepted original job waits for matching capacity; retain identity, reservation and reason. |
| `queued` | Eligible for one fenced generation claim. |
| `claimed` | Worker lease/attempt exists; preparation remains reversible. |
| `submitting` | Durable submission intent was committed before the external start. |
| `submission_unknown` | Start may have been accepted. Reconcile the same attempt; never blindly submit again. |
| `running` | Bound upstream execution; lost status is not proof it stopped. |
| `collecting` | Upstream output exists or is complete; recover original output, never regenerate to repair storage. |
| `cancel_requested` | Preserve original phase and obligations until attempt-specific stop evidence; retain late successful output. |
| `recovery_hold` | Ambiguous identity/history or ownership; quarantine and reconcile, not an empty queue. |
| `succeeded` | Verified durable artifacts were committed, even if supplier billing is still pending. |
| `failed`, `cancelled` | Proven safe terminal execution outcome; accounting may still require reconciliation. |

The queue keeps lease fences and original attempt identity across reconcile/collect claims. An expired worker cannot update a replacement claim. Drain blocks new work while allowing protected reconciliation/collection. A global engine interrupt must not cancel another attempt.

Capacity has its own persistent instance-intent states (`reserved`, `creating`, `creation_unknown`, `starting`, `ready`, `busy`, `draining`, `destroying`, `destroyed`). Uncertain deletion remains `destroying` with evidence pending; a diagnostics counter named `destroy_unknown` is not an implemented persisted transition. Rental unknowns are not job unknowns and have their own evidence/reconciler. Empty provider inventory, failed status or an accepted DELETE does not settle costs or prove an ambiguous creation was never accepted.

Provider preparation has a separate, opt-in operating timeout: `provider_preparation_timeout_s` (120–7200 seconds; absent/null keeps historical behavior and hashes). It measures from the original instance intent's creation, not the latest observation or controller restart, and is unrelated to the `cold_start_s` scheduling estimate. The existing rental receipt ledger commits `awaiting_provider` before creation and irreversibly commits `bootstrap_started` before any boot/SSH work. Only that positive unused proof, fresh exact-owned `PENDING` timeout or `FAILED`/`STOPPED` facts, and locked checks excluding workers, attempts and runtime evidence permit `retiring_unused`. The original approval closes atomically before deletion; ambiguous deletion reconciles once without replay. Service rollover preserves accepted job IDs, requests, original wait deadlines and reservations, and waits for confirmed removal and billing settlement before replacement. Provider status alone never proves idle. Legacy intents without this receipt require explicit controlled adoption; absent files do not make them eligible. Enabling this policy does not migrate an existing service fingerprint or authorize cloud work.

New Lium rentals created through the coordinator with a durable rent journal freeze an absolute TTL before the rent POST: the earlier of the original instance-intent creation plus the actual requested hours and its approved hard deadline. An exact pod observation then schedules this bound once; existing earlier deadlines are retained. The same private rent marker records schedule intent before POST and verification afterwards. An unknown POST is GET-only and may need operator recovery. Only an acknowledged, verified pending schedule permits one separately journaled shortening when the provider transitions to RUNNING and overwrites it. Further drift blocks bootstrap with `capacity_provider_ttl_unconfirmed`; it never renews the rental from readiness time. Lifetime and SSH entry verify the bound. Existing unbound markers retain strict read-only lifetime checks and gain no inferred scheduling authority. This source behavior is not provider-side compare-and-swap or a guarantee against independent external schedule writers.

## 5. Inference adapter seam

The existing worker remains the execution owner. The adapter provides:

```text
kind, enabled, slot_key
prepare(job, attempt_tag, storage, heartbeat) -> prepared
submit(prepared, attempt_tag) -> upstream_task_id
reconcile(attempt_tag, upstream_task_id=None) -> Outcome
poll(attempt_tag, upstream_task_id) -> Outcome
cancel(attempt_tag, upstream_task_id) -> acknowledgement, NOT stop proof
fetch(job, attempt_tag, upstream_task_id, target_dir, heartbeat) -> owned output paths
is_idle() -> bool  [B2 readiness extraction; only literal True permits readiness]
optional cost resolver and close()
```

Preserve `Outcome.state`, optional `task_id` and optional integer micro-USD cost. `succeeded` requires completed output evidence; `failed`/`cancelled` require verified upstream stop because the existing runner treats those terminal values as stop proof. Transport errors, missing in-memory tasks and unconfirmed disappearance must remain `unknown`. Preserve exception identity via compatibility imports: `BackendError`, `NotReady`, `SubmissionRejected`, `SubmissionUncertain`. A definitely rejected start differs from an uncertain start. Unknown adapter response or malformed/failed idle check fails closed.

`prepare` may validate/stage inputs, but must not start inference. `submit` is called only after durable intent. `reconcile`/`poll` never trigger another inference. `fetch` only retrieves the existing attempt's artifacts. Idle confirmation is evidence for a currently empty matching engine; it is not proof of full model coverage or successful H3 inference.

B2 extracted the Comfy seam in [PR #17](https://github.com/apedintensor/h3-studio/pull/17), preserving its existing imports, defaults, controls, queue rules and CPU/mock paths. [PR #25](https://github.com/apedintensor/h3-studio/pull/25) subsequently added configured-slot/attempt engine binding and original-engine recovery with offline evidence. Engine-bound cold approvals, bootstrap/readiness and reconnect integration remain [D2 #22](https://github.com/apedintensor/h3-studio/issues/22); configured routing alone is not a production migration.

Before WanGP activation, immutable execution configuration/attempt binding must identify engine code revision, image identity, compiler/recipe version, full component model revisions/precision and slot topology. Preserve old accepted Comfy bindings for reconcile/collect after new-task routing changes. One global backend switch cannot perform this migration safely.

The WanGP cold path extends the existing controller and capacity ledger. Its
approval, waiter, worker slot and attempt bind the same backend and manifest
digest. The private client also pins the runtime incarnation; generation POSTs
carry that precondition and a replaced host refuses them before dispatch. Reads
of original receipts remain available. A transport reconnect reuses the same
instance, host key, token and local port, including during draining; it does not
upload, reinstall, restart or resubmit. A complete controller-process restart
after fleet launch still requires explicit recovery under C2; transport recovery
must not be reported as acceptance of that wider requirement.

WanGP source/dependency staging is a pollable upload-only operation within this
same controller. Its identity-bound `staging` receipt precedes remote bytes;
an OS-held per-intent lock prevents concurrent uploads on the durable controller
host. The main loop continues leader renewal, wait/deadline reconciliation and
safe progress snapshots while transfer runs. Before setup starts, it rechecks the
current leader fence, approval, original waiter deadline, remaining rental life
and confirmed demand. Upload completion is not setup permission. A process lost
before setup may resume only verified source bytes; `bootstrap_starting` and
later phases reconcile the original remote marker and never replay setup.
Preparation cancellation is cooperative and may wait for a bounded in-flight
SSH operation. Pending transfer is not child-exit or upstream-idle evidence;
staging failure retains original accepted backlog and accounting for repair.

Continuing single-slot operation is an explicit `service_policy`, validated
against the existing scope, absolute window, budget ceiling and idle interval.
`max_cycles: null` removes the old test-cycle count only. Each new rental remains
bounded by its own TTL and the same cumulative spent/reserved account balance;
unknown outcomes and repair holds still block unsafe rotation. Changing this
configuration does not alter previously accepted request identities, leases,
deadlines or bills, and does not itself grant spending authority.

## 6. Output contract

Engine success is not platform success. The worker validates expected geometry, duration, actual decodability and audio, normalizes the requested export, and publishes through the existing `ArtifactWriter` receipt. Audio-enabled generation requires an independent audio output as well as valid video; an engine returning only a muxed file needs an explicit verified extraction step.

Exports are MP4/H.264, 24 fps, and independent FLAC for audio-enabled jobs. Requested duration is distinct from padded native sampling duration. Content hash/size, ownership, job/attempt linkage, storage quota and durable publication are recorded before completion.

Delivery is versioned and immutable; it is not inferred from the current default at collection time:

| Accepted execution contract | Delivery behavior |
|---|---|
| No `output_delivery` / `delivery_spec` | Historical requested-duration export, unchanged for existing jobs, Comfy and chapter roughcuts. A five-second H3 request delivers 120 frames. |
| Explicit `output_delivery: native-frames-v1` | New qualified WanGP FL plans freeze `delivery_spec` with policy, fps, frame count, video duration and requested duration. All native frames remain: five requested seconds means 124 frames / 24 fps = 5.1667 seconds. No end-frame crop, frame-rate conversion, retiming or silence padding. |

The native policy requires exactly matching video frame count, 24 fps, zero-based timestamps and native video-stream duration before and after export. Both the MP4 audio and independent FLAC derive from the complete generated waveform; neither is cut to the integer requested duration. Their observed lengths must remain within the existing 0.1-second audio tolerance of the native video timeline, including codec/sample rounding; larger mismatch rejects collection instead of being corrected silently. Video, container and audio durations remain distinct evidence. This tolerance is not a claim of perceptual lip-sync or real H3 audio qualification.

The optional policy/capability is bound through operator policy, capacity approval, boot/config identity, worker registration and the accepted execution plan. Missing fields preserve historical hashes and legacy fleet-file fields. Native and legacy workers cannot share a recorded configuration identity; configuration IDs remain opaque. Historical registrations, immutable cold approvals and accepted jobs retain that binding even after retirement, revocation, expiry or completion. Writers check and persist it under the shared global capacity lock, so conflicting first approvals or stale-plan admissions cannot race a registration. Exact worker capability matching applies to generation and collection, and an old recovery spec cannot attach to a native worker record. Rollout requires a separately qualified configuration/registration; existing workers, accepted attempts and historical artifacts are not relabelled. No default or currently enabled operational configuration is changed by adding this implementation.

Collection receipts for native jobs additionally bind the frozen delivery specification. Publication validates native-only timing metadata against that specification; legacy metadata contracts remain unchanged. API preflight exposes the planned delivery under `execution.delivery_spec`, jobs expose the same descriptor, and downloadable artifacts retain verified timing evidence. Unknown policies or malformed descriptors fail closed before a new engine submission.

[D3 #27](https://github.com/apedintensor/h3-studio/issues/27) separates deterministic CPU export evidence from real last-image conditioning. Frame preservation does not establish model fidelity. A real first/last-image example and its actual video/audio/download evidence remain necessary before advertising qualified ending fidelity. Fixed exact-duration retiming is not part of this policy.

On a process restart, an existing collection receipt resumes the same publication. A download, transcode, storage or database failure cannot cause a new generation. GPU scratch files cannot be discarded before durable output/obligation handling permits shutdown. Unknown final supplier billing keeps its reservation independently of video delivery.

## 7. WanGP acceptance requirements and implementation boundary

User authorization obtained on 2026-10-06. Pinned research revision: `deepbeepmeep/Wan2GP@0e58385fbde7ff102d276e4a9e490845de76b4ea`. Use the upstream headless Session API behind a private adapter; keep upstream unchanged where practical. Do not adopt its UI/shared login/MCP in-memory queue as our account or job system.

[D1 #18](https://github.com/apedintensor/h3-studio/issues/18) is accepted within its offline adapter/receipt scope. PR #25 implements a bounded Base FL2VA compiler, protected host/transport and configured routing; full runtime locking, cold-start integration and real output verification remain #22 and [B3 #16](https://github.com/apedintensor/h3-studio/issues/16). REF qualification is [D4 #29](https://github.com/apedintensor/h3-studio/issues/29): keeping Comfy recovery does not imply automatic REF-to-Comfy routing for new requests. Request-duration ranges do not by themselves establish the executable or publicly enabled envelope.

- Pin dependencies and every model component independently; an upstream `resolve/main` URL is not a frozen weight version.
- Use one active inference per isolated slot initially. Two replicas differ from tensor parallelism.
- Persist a subordinate attempt receipt **before** invoking the in-memory Session start. Record request hash, pinned runtime identity, dispatch state, upstream association, stop evidence and output references. It is evidence, not another scheduler.
- Same attempt+hash replays/reconciles; a conflicting hash fails. A crash between dispatch intent and acknowledgement stays unknown. A missing in-memory Session job after restart never authorizes replay. Do not promise exactly-once inference across process/network failures.
- Preserve all requested controls or reject before starting. Current platform controls cannot silently map to upstream defaults, ignored controls, automatic trimming, quantization, or different steps/samplers. Publish a tested intersection first; unsupported controls remain explicit.
- Verify first/last-frame versus omni-reference exclusions, reference counts/selected durations and audio behavior. Source support, adapter mapping, hardware qualification and current enablement are separate facts.
- Every GPU backend must retain engine identity, capacity guards, endpoint uniqueness and original-attempt recovery routing. Adding an enum or passing configured-slot tests cannot bypass physical GPU limits or the remaining production activation gates.

## 8. Acceptance matrix

| Gate | Required proof | Evidence level |
|---|---|---|
| Existing API compatibility | Same types/statuses/IDs; owner isolation; source-version rejection; same-key replay and conflict; no paid action on plan/card. | Isolated API/repository tests |
| Adapter extraction | Old imports work; no raw Comfy queue protocol in generic fleet readiness; disabled remains inert; definite rejection vs unknown retained. | Fake HTTP/unit integration |
| Fenced execution | Intent before start; lost accepted response causes no second POST; stale lease cannot complete/interrupt another claim. | Fake upstream + database |
| Cancellation | Pending cancellation, stop confirmation, process loss and late result retain correct obligations. | Fake upstream + queue |
| Collection recovery | Failed download/publication/restart resumes same attempt; MP4 plus independent audio validate; no extra start. | Synthetic CPU media + receipts |
| Mixed engines | Old attempts stay on original config during new-route changes; new backend obeys all GPU/slot limits. | Offline routing/control tests, then isolated live gate |
| WanGP controls/runtime | Exact manifest and control mapping, rejection of unsupported combinations, restart journal and output containment. | Fake Session first; real GPU subsequently |
| PostgreSQL races | Claim, idempotency, admission and receipt contention against isolated PostgreSQL. | Separate database integration; SQLite is not equivalent |
| Public generation | One authorized task cold-starts to verified downloads; warm follow-up; idle shutdown then restart; timings and rental accounting retained. | Protected production receipt |

Existing suites to reuse: `test_platform_repository`, `test_platform_queue`, `test_platform_control`, `test_platform_worker`, `test_platform_fleet`, `test_platform_drain_safe_runner`, `test_platform_artifact_writer`, execution-policy/queued-task suites, and local Quick Chat admission/security suites when their shared entry points change. Add only tests that cover changed seams or unresolved risks; do not repeatedly run the full pipeline for intermediate edits.

## 9. Release and completion gate

A1/A2 can complete with dated baseline/specification evidence. B2 can complete locally after compatibility and fake-engine checks; that does not mean WanGP or generation is live. B/D public acceptance additionally requires a tested release, current operational authorization, exact runtime/model identity, original obligations preserved, real artifact validation and rollback.

No API/profile import, successful model load, empty queue, health endpoint, local mock, or passing CI proves a current GPU is ready. No issue or new specification renews the expired finite window. Keep the existing budget/reservations and review remaining authorization before paid acceptance.
