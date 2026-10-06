# Sixnine / Yingxu: Backend and API Project Plan

Baseline date: 2026-10-06 (Australia/Sydney).
This English plan is the GitHub working baseline for cross-session development.
It condenses the local Chinese architecture research and reuse/migration review.
It describes implementation direction, not a claim of deployment, current health, or renewed GPU authorization.

Start a work session with [WORKFLOW.md](WORKFLOW.md) and the work-package index in [workflow/project.json](workflow/project.json).
Use the actual GitHub issues linked there; A–H below are parent work packages, not a second issue-numbering system.

## 1. North star

A user or an AI agent can upload references, describe a video, confirm generation, and retrieve the real result through the same business API.
Quick Chat, Yingxu story production, and future advertising tools are clients of that API.
The website remains useful while GPU capacity starts, fails, recovers, or shuts down.
Every accepted job has a traceable owner, immutable request, execution history, output, and cost obligation.

The immediate priority is a reliable generation path and understandable failure recovery.
Repeated frontend redesign is not a substitute for proving that path.

## 2. Facts, limitations, and targets

The following is a source-code and historical-record baseline, not a fresh production inspection.

| Area | Existing evidence | Remaining target |
|---|---|---|
| Business backend | `platform_app.py` / `studio_platform` implement authentication, assets, plans, jobs, budgets, and recovery. | Clear application-service boundaries and explicit compatibility contracts. |
| Public generation | Historical receipts contain successful Comfy-based generation, download, and result integration. | Recheck current deployment and authorization; prove the new policy end to end. |
| Quick Chat | Local `/quick-chat` and `/v1/quick-chat/` integration exists, with sessions, turns, bindings, revisions, and submissions. | User-approved local UX and real generation integration before release. |
| Quick Chat deployment | The isolated preview is local; generation and assistant execution are disabled there. | Do not describe local integration as released or currently generating. |
| Accounts | Ownership and PAT controls exist, but the account directory is fixed to `superdan` and `supervan`. | A real user directory, explicit identity migration, and later team membership. |
| Assets | Private storage, validation, and recovery exist; staging and locks depend on one host. | Cross-host staging, operation leases, backup, and a tested object-store migration. |
| GPU control | On-demand orchestration reuses finite-controller logic with single-node and finite-cycle constraints. | Separate continuing service policy from test policy; then implement node isolation and redundancy. |
| Engines | A Comfy baseline has historical evidence. SGLang is a researched candidate. | Validate each engine, recipe, topology, and control envelope before enabling it. |

Historical budgets, service deadlines, successful jobs, and deployment receipts do not establish today's available capacity.
No plan, issue, restart, or configuration change renews an expired operating window or resets accumulated costs.

## 3. Change the existing platform in place

Keep one production business backend and one authoritative set of identities, assets, jobs, and obligations.
Preserve account ownership, API credentials, existing IDs, accepted requests, results, and recovery evidence.
Extract interfaces and replace individual implementations where there is a measurable benefit.
Do not create a parallel `backend_v2`, duplicate job database, or dual-write generation authority.

The first extraction points are:

- Application services: move generation admission and plan orchestration out of HTTP assembly closures.
- Engine adapters: extract Comfy compilation, submission, reconciliation, cancellation, and output normalization from the generic worker.
- Runtime policy: separate finite acceptance limits from continuing on-demand service rules while retaining the rental ledger.
- Storage and identity: address their specific scaling constraints without changing existing ownership semantics implicitly.

Old entry points remain compatibility or retirement candidates until callers, accepted work, assets, billing, and rollback are accounted for.
Similar names do not prove duplication: `autoscale` recommends capacity, `scaler` coordinates rental side effects, and `fleet` supervises execution processes.
`DrainSafeRunner` must continue reconciliation and collection after new work is stopped.
`production_scaler` is an on-demand dependency; `production_worker` is a separate historical acceptance entry point.

## 4. Module boundaries and data authority

| Boundary | Owns | Must not own |
|---|---|---|
| Scenario frontend | Upload interaction, prompt editing, visible references, confirmation, history, and result selection. | Provider credentials, rental decisions, or a second task state machine. |
| HTTP / agent interface | Authentication, request parsing, version checks, serialization, and trace IDs. | Direct GPU provisioning or engine-specific node graphs. |
| Scenario services | Chat sessions and story/chapter/shot documents, material bindings, and explicit adoption. | Independently editable copies of an accepted generation request. |
| Generation service | Authorization, capabilities, normalization, admission, business idempotency, and batch items. | Provider rental calls or engine-private protocol details. |
| Execution worker | Claiming, submission, reconciliation, cancellation, collection, and output validation. | Raising budgets, renting GPUs, or silently changing the requested model. |
| Inference adapter | Engine protocol, pinned recipe compilation, safe error classification, and normalized outputs. | Account permissions or user-facing billing authority. |
| Capacity controller | Selection filters, provisioning intent, readiness, node draining, destruction, and rental reconciliation. | Declaring a video successful before artifact validation. |
| Asset service | Authorized upload/download, stable asset identity, validation, derivatives, and recovery. | Treating arbitrary user URLs or local paths as trusted assets. |

The database is authoritative for ownership, document versions, job/attempt state, budgets, leases, and durable receipts.
Private object storage holds media bytes; GPU scratch space is not the only copy of a deliverable.
One accepted generation decision has one admission authority, even when a browser and several agents submit concurrently.
Scenario documents remain editable; the accepted generation snapshot, recipe identity, and resulting artifacts are immutable.
Existing hidden project/shot projections in Quick Chat are a compatibility bridge, not an additional editing authority.

## 5. API direction

Keep existing public routes compatible while making their shared service contracts explicit.
Web and agent clients use the same account, asset, capability, generation, status, and download rules.
Direct generation must eventually work without inventing a story or chapter; preserve existing source mappings during that transition.
Agent discovery and one-time connection use the existing onboarding contract, not the internal AI Registry.

The contract inventory must cover identity/PATs, asset upload/finalization, capabilities, scenario commands, plans/preflight, submission, jobs, cancellation, artifacts, and downloads.
New route names in a proposal are not implemented endpoints; publish the implemented schema and supported version.
Report capability status separately as implemented, verified, and enabled, with the applicable recipe and hardware envelope.
Use structured phase, reason, retryability, and next-check information; do not present every wait as unexplained `running`.
Authorization follows stable ownership, and document edits use explicit version checks.
Clients must not infer success from HTTP acceptance, boot readiness, or an upstream response alone.

## 6. Job identity, retries, and recovery

| Action | Identity and behavior |
|---|---|
| Network replay or repeated confirmation | Recover the same business submission/item and original execution/job; no second initial generation. |
| Reconciliation or interrupted output collection | Continue the original job/attempt and retrieve its original output. |
| Platform technical retry | Only after prior execution is confirmed stopped and explicit policy permits a new attempt under the same job. |
| User retries a failed Quick Chat item | Keep the submission/item, create a new execution and job linked to the previous one; preserve failed history. |
| User requests another variation | A new generation decision/item with explicit cost and provenance. |

Unknown submission, cancellation, or rental outcomes are obligations to reconcile, not permission to repeat the external operation.
Keep owner, request, upstream identity, attempt evidence, reservations, and deadlines across restarts.
Every unknown class needs an accountable reconciler, evidence sources, recheck timing, and an escalation path.
Hard budget/lease termination remains enforceable; termination does not prove delivery or authorize regeneration.
Do not make unknown work disappear by migrating it into an empty database or resetting a controller cycle.

## 7. Engine and deployment decisions

Retain pinned ComfyUI as the behavioral baseline and compatibility route while extracting the adapter boundary.
SGLang Diffusion is the first native H3 serving candidate, not an already verified replacement.
vLLM-Omni is an alternative with its own serving limits; ordinary vLLM support is not equivalent.
Diffusers' H3 ModularPipeline is a research/custom-service option that still needs service lifecycle and recovery implementation.
External H3 APIs are explicit provider choices after validation, never silent substitutions for unknown self-hosted executions.

Before selecting a new engine, freeze source SHA, image digest, dependency versions, model revision, precision, and exact recipe.
The switching gate must demonstrate:

- Advertised text, first/last-frame, image/video/audio reference, and advanced-control coverage for the exact version.
- A joint envelope for dimensions, frames, reference encoding budget, per-slot concurrency, CPU RAM, disk, and GPU memory.
- Comparable visual/audio quality, control adherence, and separately measured cold/warm performance.
- Submission identity, restart recovery, cancellation, unknown handling, and output collection compatibility.
- Normalized video and audio outputs, including separate audio where the existing artifact contract requires it.
- New-task routing and rollback while old jobs stay bound to their original engine and recipe.

Do not silently discard references, shorten videos, change precision, or ignore unsupported controls.
Quantized or distilled variants are separate quality profiles; their speed does not prove full Base capability parity.
An execution slot may use one or several GPUs. Two single-GPU replicas are different from one tensor-parallel two-GPU slot.
A first/last-frame-only node and a reference-only node do not provide redundancy for either individual capability.

Initially keep EC2 CPU hosting, independently supervised API/worker/controller processes, PostgreSQL, and protected local media storage.
Evaluate S3 migration with verified backup/restore; R2 or Hippius require their own storage-semantics evidence.
Managed CPU/container/database services are later options when availability and scale requirements justify migration.
Runtime secrets come through the existing central/runtime loaders; no new plaintext credential copies.

## 8. Parent work packages

Use these packages to organize actual GitHub issues and specifications, not to mark architecture prose as completed work.

| Package | Priority and dependency | Deliverable | Acceptance gate |
|---|---|---|---|
| A — Baseline and contracts | P0; first. | Versioned entry-point map, API/capability/state/error inventory, ownership and obligation baseline. | Every generation phase has a known authority and evidence source; local, historical, and current production facts are distinguished. |
| B — Minimum generation loop | P0; uses A. | Generation-service and adapter boundaries, correlated phase receipts, download validation. | One authorized job completes cold start to real artifact; warm follow-up and restart after idle shutdown work without a new UI. |
| C — Failure recovery | P0; follows B's boundaries; design alongside B. | Unknown/cancel/restart/collection contracts and controlled recovery tooling. | PostgreSQL races, cross-actor replay, lost responses, transfer failure, inventory/budget blockage, and recovery preserve identities and obligations. |
| D — Native engine comparison | P1; A adapter contract; C-equivalent gates before switching. | Pinned SGLang adapter, capability matrix, controlled comparison report. | All selected recipe/control, quality, resource, and recovery gates pass; unmatched controls remain explicit. |
| E — Two-node redundancy | P1; reliable single-slot B/C. | Durable pool/member state, per-node isolation, capacity and budget policy. | First healthy matching node serves; another failure does not stop it; no duplicate job dispatch; unknown capacity remains accounted for. |
| F — Storage and recovery | P1; backup design may start with A. | Independent backup, S3 migration/rollback plan, restore exercise. | Old assets/results remain verifiable and retrievable; no unique copy is deleted; restored unknown operations are held. |
| G — Scenario integration | After the core B/C loop; coordinated with A contracts. | Approved Quick Chat and usable Yingxu clients of the same API. | Real media inputs and results are visible; web/agents share objects; ordinary flows do not expose deployment parameters. |
| H — Collaboration and scale | P2; proven identity, storage, and core execution boundaries. | User directory, memberships, audit, required multi-host operation. | Ownership migration, access/revocation, conflicts, staging, admission, and load tests pass before broader access. |

E preserves the desired policy: confirmed demand, target two independent nodes, serve with one ready, no always-on GPU, and shutdown after 600 seconds with no pool obligations.
Implement member-level failure isolation and explicit reservations; do not merely change a single-node count to two.
F migration does not block the initial B proof; its backup design should not wait for a storage-provider change.
G must preserve the approved Quick Chat mock and current user decisions. Do not quietly redesign the frontend during backend extraction.

## 9. First A/B/C batch and authorization boundaries

The first batch fixes one supported Base profile and aligns capabilities, admission, bootstrap, and worker identity.
Extract B's service and Comfy adapter boundaries without changing public semantics, task identity, or the rental ledger.
Add truthful phases and reasons through the existing UI/API rather than redesigning the creation experience.

Work that can proceed without production access or paid execution:

- Static source/receipt inventory, current-contract documentation, and issue specifications.
- Application-service and adapter extraction with compatibility imports and routes.
- Fake-upstream tests for replay, unknown outcomes, cancellation, and collection recovery.
- PostgreSQL integration checks against an explicitly isolated local test database; distinguish them from SQLite checks.
- Local UX review and build validation with generation disabled.

Work that needs its applicable production authorization and controls:

- Current production inspection uses approved read-only access; do not infer fresh health from an old receipt.
- Deployment changes require the established protected release process and authorization for that batch.
- GPU provisioning, live generation, and comparison runs require a current operating window and budget, including existing obligations.
- Use an explicitly confirmed queue task once; deliver success to its owner rather than duplicating it as a test.
- User-facing Quick Chat publication requires approval of the local experience; local completion alone does not authorize release.

The batch receipt records commit/image/profile, environment, job/attempt identity, phase timing, artifact validation, and rental reconciliation.
Do not combine this batch with a story-model rewrite, wholesale storage migration, accelerated-model rollout, or hosting-platform migration.

## 10. Documentation, issue, and status authority

This plan holds architectural direction. The active issue-linked specification holds the batch's accepted behavior and acceptance criteria.
GitHub issues hold work status and current ownership; [workflow/project.json](workflow/project.json) indexes the Project, fields, packages, and initial issue links.
[WORKFLOW.md](WORKFLOW.md) defines claiming work, handoff, review, testing, and release coordination.
Code and observed evidence establish implemented behavior; disagreements with accepted requirements are defects or explicit decisions to resolve.
Versioned test/release receipts establish what was verified, where, and when; issue closure is not current production health.
Local Chinese documents retain detailed research and historical context; keep the English working baseline consistent when a decision changes.

Every handoff identifies scope, owned files, contracts, invariants, dependencies, tests, unresolved questions, and deployment status.
One integration owner coordinates shared contracts and release; independent sessions must not race on the same runtime authority.
Batch related changes, run affected checks during development, then complete risk-appropriate regression and one coordinated release.
Open decisions include ongoing production authorization, target waiting/recovery times, public-user scope, collaboration policy, and storage location.
Those decisions do not block safe contract work, but a placeholder never grants spending, migration, or production permission.
