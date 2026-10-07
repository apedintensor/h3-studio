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

Quick Chat `/v1/quick-chat/sessions/...` routes reuse the authoring source preserved at `efc79c27e2bc0022149e8addabe1699b0d574cfc` (draft [PR #23](https://github.com/apedintensor/h3-studio/pull/23)). Session/turn/card/revision/submission/item/execution identities sit above the existing job ledger. `GenerationAdmission` handles the same current-engine preflight, explicit confirmation and enqueue as story/direct clients. Main's engine/manifest/delivery bindings remain authoritative; hidden project/shot projections are not separately editable sources. [G1 #20](https://github.com/apedintensor/h3-studio/issues/20) and [B1 #12](https://github.com/apedintensor/h3-studio/issues/12) retain the remaining UX/public release and explicit access/plan/read extraction criteria.

Existing PATs can author via this API without a website assistant call: use `assistant_mode=none, create_card=true` or save a card directly, then preflight and explicitly confirm. The authenticated `/v1/quick-chat/schema` and additive `quick_chat` entry in `/for-agents/guide.json` describe those steps. New-session creation requires owner-wide project create/read/write authority; scoped media and job operations retain their own permission checks. No G2 credential-exchange route is introduced by this slice. The website assistant remains disabled by default; named model options and preserved adapter source do not establish hosted-model availability or enabled generation. Frontend publication and [G2 #21](https://github.com/apedintensor/h3-studio/issues/21) onboarding acceptance remain separate.

Refreshing an inert Quick Chat plan uses the same global capacity lock and delivery-configuration identity fence as job creation/enqueue, in addition to the no-attempt/no-reservation conditions below. Backup includes Quick Chat authoring and operation identities; restore expires preflights and places unfinished authoring executions on a recovery hold, without re-enqueueing or repeating an assistant call.

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

An operator Lium manifest may set `min_download_mbps` to a positive finite number. It is absent by default; old configuration documents and fingerprints remain unchanged. The explicit floor goes to Lium's executor-list filter or both dry-run and actual rent-by-spec payloads, with no fallback that lowers it. Lium excludes missing or insufficient trusted measurements; no matching inventory retains `capacity_no_matching_gpu`, while an unconfirmed inventory check is distinct. This is a provider-measured host bandwidth constraint, not guaranteed Hugging Face transfer speed or permission to enable a new operating configuration. See the [official network-filter contract](https://docs.lium.io/developers/executor-interconnect) and [rent-by-spec parameters](https://docs.lium.io/developers/quickstart).

Provider preparation has a separate, opt-in operating timeout: `provider_preparation_timeout_s` (120–7200 seconds; absent/null keeps historical behavior and hashes). It measures from the original instance intent's creation, not the latest observation or controller restart, and is unrelated to the `cold_start_s` scheduling estimate. The existing rental receipt ledger commits `awaiting_provider` before creation and irreversibly commits `bootstrap_started` before any boot/SSH work. Only that positive unused proof, fresh exact-owned `PENDING` timeout or `FAILED`/`STOPPED` facts, and locked checks excluding workers, attempts and runtime evidence permit `retiring_unused`. The original approval closes atomically before deletion; ambiguous deletion reconciles once without replay. Service rollover preserves accepted job IDs, requests, original wait deadlines and reservations, and waits for confirmed removal and billing settlement before replacement. Provider status alone never proves idle. Legacy intents without this receipt require explicit controlled adoption; absent files do not make them eligible. Enabling this policy does not migrate an existing service fingerprint or authorize cloud work.

New Lium rentals created through the coordinator with a durable rent journal freeze an absolute TTL before the rent POST: the earlier of the original instance-intent creation plus the actual requested hours and its approved hard deadline. An exact pod observation then schedules this bound once; observed earlier deadlines are retained. The same private rent marker records schedule intent before POST and verification afterwards. An unknown POST is GET-only and may need operator recovery. Only an acknowledged, verified pending schedule permits one separately journaled shortening when the provider transitions to RUNNING and overwrites it. TTL checks gate initial lifetime/SSH entry, exposing `capacity_provider_ttl_unconfirmed` when that gate fails; they never renew the rental from readiness time. Cached ProductionBoot receipts continue enforcing the shorter database deadline. This does not continuously re-attest the provider schedule after a host is connected. Existing unbound markers retain strict read-only lifetime checks and gain no inferred scheduling authority. This source behavior is not provider-side compare-and-swap or a guarantee against independent external schedule writers.

The provider protocol is documented in [Lium scheduled termination](https://docs.lium.io/pod-users/scheduled-termination): `POST /api/pods/{id}/schedule-removal` takes UTC `removal_scheduled_at`. The [official CLI reference](https://docs.lium.io/developers/cli/reference/up) describes scheduling immediately after receiving the rental ID, before RUNNING, from CLI 0.0.42. Readiness-time deadline drift was observed in this project's protected provider evidence; its one-correction allowance is defensive behavior, not a claim that every provider always overwrites explicit schedules. These sources establish the protocol; this code's fake HTTP tests do not establish live provider acceptance.

With the timeout enabled, `provider_preparation_failure_limit` (1–5, default 2) additionally bounds consecutive failed actual provider pods: normally the initial pod and one replacement. The streak is reconstructed from original capacity-cycle/start-barrier receipts, not a resettable process counter. Inventory refusals and authoritative no-rent cycles neither count nor reset it; a committed `bootstrap_started` transition ends this provider-preparation streak, while subsequent runtime failures retain their separate repair hold. At the limit, the original task remains held under `capacity_provider_preparation_retry_limit` / `awaiting_provider_repair`; provider deletion, billing reconciliation and the original waiter deadline still apply. No further rental is created automatically. Changing configuration or clearing files is not an authorized reset. Selection still uses the approved filters and may choose the same executor; failed-executor exclusion and frontend-specific wording are separate follow-ups, not verified capabilities of this slice.

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

The WanGP queued-task policy may explicitly admit first/last image slots with
`allow_first_last: true` and an aggregate `max_reference_files` of 0–2. With that
switch off the aggregate cap must remain zero. First/last images still undergo
the same owner/project checks, immutable asset snapshot, aggregate count and
inspected image-pixel limits. Ordinary image/video/audio references, reference
video sound and guides remain disabled in this recipe; raising the aggregate
cap does not enable REF. This policy validation and owned-upload admission
coverage are not real first/last-frame fidelity evidence. The first live proof
remains text-to-video; image-guided qualification and the selected native output
contract require their own actual artifacts before a parity claim. No existing
operator policy or accepted job is changed automatically.

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

Model transfer runs only after the pinned environment/import checks, in one owned
Linux child using the already locked Hugging Face SDK. The manifest's exact file
allowlist and full revisions, official endpoint and explicit public `token=False`
remain fixed. Two file workers, a bounded SDK retry per file, parent/child elapsed
deadlines, disk/cache guards and parent-death termination bound preparation; SDK
partials never authorize a second bootstrap or job submission. Only safe completed
file/byte counts and static errors enter status. Native SDK logs are discarded,
and the child must be stopped/reaped before local preparation can be idle.
Transfer completion does not certify model bytes: the launcher acquires exclusive
journal/slot ownership, then performs full source/configuration/environment and
model size/hash verification before Session initialization or HTTP readiness.
Normal bootstrap launches that process once without a preceding duplicate
`--verify-only`; explicit standalone verification remains available. Its private,
atomic verification receipt binds manifest, slot, PID and host incarnation. The
same authenticated readiness incarnation must confirm an idle runtime. Receipts
never bypass verification in a new process; stale/mismatched evidence or startup
uncertainty cannot authorize another start. The existing launch deadline and
bootstrap marker remain binding, with verification and initialization shown as
separate preparation phases. Model-transfer speed, native
Xet interruption reuse and cross-instance model caching are not offline acceptance
claims. See [the runtime preparation guide](deploy/wangp/README.md#bounded-public-model-transfer).

Continuing single-slot operation is an explicit `service_policy`, validated
against the existing scope, absolute window, budget ceiling and idle interval.
`max_cycles: null` removes the old test-cycle count only. Each new rental remains
bounded by its own TTL and the same cumulative spent/reserved account balance;
unknown outcomes and repair holds still block unsafe rotation. Changing this
configuration does not alter previously accepted request identities, leases,
deadlines or bills, and does not itself grant spending authority.

### E1: opt-in pool member admission (offline foundation)

An operator approval may explicitly freeze `pool_members: {version: 1,
member_ids: ["a", "b"]}`: exactly two distinct stable member IDs, each one GPU
and one slot. Omitting it retains the legacy payload/hash and single-intent
cycle. The existing ledger stores the unique `(approval_id, member_id)` to
intent binding; intent creation, budget reservation and this binding commit
atomically under the shared capacity/account locks. Each member binds at most
one original intent. E1 has no replacement generation, provider call or second
task/rental ledger; removal does not release an unconfirmed bill or renew a
member binding, approval or deadline.

Cold plans freeze `capacity_binding: "pool-members-v1"` with the exact approval
ID/hash and current engine configuration. Their waiters belong to that approval,
with no single `intent_id`. The first compatible ready member may activate the
original job without rewriting its request, execution plan, budget reservation
or deadline. Readiness must match the approved tenant/pool, recipe/model,
configuration, backend/manifest/delivery and a live bound instance with enough
original lifetime. Both members may be eligible, but `WorkerControl` and the
existing queue remain the sole atomic claim/attempt authority. Generate claims
recheck membership and current approval; an unbound same-configuration worker
cannot take a pool-bound job. Existing unknown attempts retain their worker,
original attempt identity and reservations; another member may serve distinct
work, and approval revocation does not prevent original-attempt reconciliation.

This is not an enabled two-node service. The legacy cold/finite controllers
refuse these approvals. Warm admission for the same opted-in tenant/pool/config
also fails closed, including after approval revocation/expiry, because E1 does
not yet bind new warm plans to members. Legacy warm admission is unchanged.
E2 must supply demand-triggered target two, fully bound warm admission, each
member's independent preparation/recovery/holds, bounded replacement and the
600-second no-obligation idle rule in the existing controller before enabling
this mode. Offline SQLite/PostgreSQL races prove ledger behavior only; two-node
provider operation, timing and production activation need separate evidence.

## 6. Output contract

Engine success is not platform success. The worker validates expected geometry, duration, actual decodability and audio, normalizes the requested export, and publishes through the existing `ArtifactWriter` receipt. Audio-enabled generation requires an independent audio output as well as valid video; an engine returning only a muxed file needs an explicit verified extraction step.

Exports are MP4/H.264, 24 fps, and independent FLAC for audio-enabled jobs. Requested duration is distinct from padded native sampling duration. Content hash/size, ownership, job/attempt linkage, storage quota and durable publication are recorded before completion.

Delivery is versioned and immutable; it is not inferred from the current default at collection time:

| Accepted execution contract | Delivery behavior |
|---|---|
| No `output_delivery` / `delivery_spec` | Historical requested-duration export, unchanged for existing jobs, Comfy and chapter roughcuts. A five-second H3 request delivers 120 frames. |
| Explicit `output_delivery: native-frames-v1` | New explicitly configured WanGP FL or bounded REF plans freeze `delivery_spec` with policy, fps, frame count, video duration and requested duration. All native frames remain: five requested seconds means 124 frames / 24 fps = 5.1667 seconds. No end-frame crop, frame-rate conversion, retiming or silence padding. |

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

### Offline REF candidate boundary (D4 #29)

`deploy/wangp/manifest-ref2va-candidate.json` defines a separate pinned Ref2VA
transformer and `sixnine-h3-ref2va-bf16-50-smallrefs-v1` compiler. It retains Base,
BF16 transformer/text encoder, 50 steps, SDPA and MMGP profile 4. Its initial
qualification input is at most one image, one silent video and one audio, with
at least one visual reference; images/videos are 256--832 px per side, at most
832x480 pixels and aspect ratio 0.4--2.5. A video is an explicit 2--3-second
source selection normalized by the existing asset service to 24 fps and 56 or
73 frames. Audio is 2--3 seconds, PCM16 WAV, 32 kHz stereo. Output is five
requested seconds, 832x480 and 124 native frames. These conservative bounds are
for the first qualification, not claims of the model's maximum capability.

The compiler consumes immutable owned asset snapshots, preserves reference
order, and maps images/video/audio to `image_refs` / `video_guide` /
`audio_guide` with explicit `I`, `V-U`, and `A` flags. There is no soundtrack
extraction, first/last mixing, automatic aggregate trimming or extra guide.
The private resolver rechecks the actual staged content hash, image geometry,
video frame count and absence of any audio track, and audio format/duration.
Media metadata alone cannot attest silence. Typed file copies preserve exact
normalized bytes; they do not transcode or shorten inputs.

The manifest/profile, model-specific pristine Session defaults and owned
launcher are bound together; legacy FL manifests and accepted jobs stay on
their original route. Public `h3-base-ref2va-v1` requests use the strict REF
compiler. Execution requires a separate explicit singleton REF policy,
`native-frames-v1`, the bounded envelope above, and a matching manifest/worker
or approved cold capacity. The controller opt-in is
`execution_recipe_id: h3-base-ref2va-v1`; absence keeps the historical FL recipe
and hash/serialization. Boot validates that recipe against the manifest's
compiler/profile before staging. An FL policy or a legacy multi-recipe policy
cannot enable new WanGP REF requests. Preflight/card construction never rents
or starts inference; explicit job confirmation retains the existing path.

This source change does not activate a production policy. Freeze a complete
environment package and review a REF configuration before enabling its first
real qualification job; record actual output, peak memory and cost before
advertising GPU parity. Candidate construction or offline fake-Session success
is not production availability. Mixed-reference peak RAM/VRAM have not been
established for this candidate on one B200.

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
