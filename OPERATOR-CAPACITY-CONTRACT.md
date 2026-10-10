# Operator capacity and deployment profiles

Accepted direction: 2026-10-09. Delivery and acceptance: [E3 #71](https://github.com/inkseq/h3-studio/issues/71). This extends the [generation contract](GENERATION-CONTRACT.md); it does not renew a budget or certify a deployed runtime.

## One authority

The operator console manages manual capacity intents. A start is an explicit paid-capacity decision, independent of user generation demand. It does not create a synthetic video job. Durable operator operations link to the existing instance intents, reservations, scaler actions, provider receipts and worker identities. The existing global capacity gate and cumulative budget remain authoritative across manual and automatic capacity. Unknown creation remains a reconciliation obligation. HTTP handlers never rent synchronously.

Only explicitly configured operator browser identities can mutate capacity. Ordinary browser users and generation API keys cannot rent or stop machines. Existing authentication, same-origin checks and expected-account protection apply. Provider credentials, private runtime endpoints, SSH paths and environment values never enter public DTOs.

## Hardware and deployment identity

A node is a billed provider host; a slot is an isolated GPU executor. A two-GPU node is not two independent nodes. Display whole-host price and GPU count. Each slot has one immutable deployment profile and functional mode. A change of profile requires drain and a separately prepared replacement; accepted attempts keep their identity. Multi-slot readiness cannot be inferred from one healthy GPU.

The catalog includes pruned rank8 INT8 on RTX 5090, unpruned 33B INT8 on PRO 6000, corrected unpruned BF16 on PRO 6000, and a separately identified single-PRO pruned rank8 INT8 qualification profile. The current user accepts pruning. Legacy BF16 jobs are not converted. Each profile binds source/weight revisions, component hashes, precision, model type, memory policy, kernel/QKV layout and exact control validation. Historical native experiments, adapter implementation, installed identity and live worker readiness are separate evidence.

`h3-pruned-rank8-int8-pro6000-quanto-int8-vae-int8-sdpa-p4-lowram-v1` retains the pruned profile's weights, precision and bounded controls, but has a distinct engine manifest with explicit GPU/VRAM admission. Its `qualification_cases` permit only the listed candidate requests; they are not measurements and produce no historical timing hint. A generic supplier PRO 6000 label does not establish the GPU edition: startup verifies the actual driver identity, compute capability and memory. Existing profile/mode engine digests remain unchanged.

`deployment_profile_id` is optional on new generation requests, Quick Chat settings/card revisions and saved shot generation settings. Omission retains legacy routing. A present value is explicit: unknown, unconfigured or incompatible profiles fail closed, never fall back to a different model. Public functional recipe IDs still distinguish FL2VA and Ref2VA; they are not model or deployment IDs. The compiled snapshot, policy hash and engine manifest preserve the choice through preflight, confirmation, worker claim and recovery.

An operator-owned execution-profile file can bind several profile/mode policies. Each remains individually qualified, budgeted and revocable. Publishing a catalog entry does not enable it. Normal generation clients only see safe capability and measured-time projections.

## Commands and observations

### Creator and Agent mode availability

Authenticated `GET /v1/generation-availability` is the shared, read-only observation for browser creators and Agent clients. Browser authentication or a generation key with `jobs:read` or `jobs:write` is sufficient; operator privileges are not required. It performs no provider query, rental, job creation or inference and exposes no private worker, provider, account or job identifiers. Responses are not cacheable.

The version-1 response contains `observed_at`, `expires_at`, `poll_after_seconds` (10), `advisory_only: true`, and `profiles`. Each entry identifies one exact `deployment_profile_id` and `modes.fl` / `modes.ref`, with `recipe_id`, `state`, `reason_code` and `available`. An explicit `deployment_profile_id: null` entry observes the actual legacy execution policy for drafts/plans without a profile; it never borrows readiness from the named default profile. Freshness expires after ten seconds; consumers refresh on focus, before a new generation confirmation and while showing current availability. Static capabilities and catalog qualification do not replace this observation.

| State | New creator/Agent behavior |
|---|---|
| `ready` | Select this exact mode/profile and perform normal request-specific preflight. |
| `busy` | Select this mode and attempt normal queue admission; show that matching capacity is busy. |
| `starting` | Show that the matching machine is preparing; wait and refresh, without requesting another rental. |
| `unavailable` | Block new generation confirmation for this mode and ask an administrator to start matching capacity. |
| `disabled` | Block confirmation and ask an administrator to enable/configure the mode; renting alone may not resolve policy restrictions. |
| `unknown` | Fail closed for new confirmation and refresh; stale or missing evidence is not proof of an offline or ready machine. |

`available` is true only for `ready` or `busy`. A ready observation is not a slot reservation and does not validate the prompt, input combination, budget or remaining execution window. Existing preflight and submission admission remain authoritative. Do not combine FL readiness from one profile with REF readiness from another, silently change models/precision, or convert inputs to another mode. Existing saved choices, material roles and accepted jobs remain unchanged when availability changes. Original submissions and unknown outcomes must still be reconciled under their original idempotency identities, not blocked from observation or submitted again.

Only explicitly authorized browser operators may follow the operator-console action to start capacity. The configured production browser allowlist includes `superdan` and `supervan` once its protected deployment is applied; ordinary creator/Agent availability access does not grant rental authority. Source configuration is not evidence of current deployed permissions.

The operator API uses `/v1/operator/capacity`. Start follows selection -> bounded preview -> explicit confirmation with an idempotency key -> durable operation -> controller reconciliation. Identical replay returns the original operation. Conflicting reuse is rejected. Stock and quotes are observations with timestamps, not reservations. Selection accepts only allowlisted provider/profile/hardware/filter values from trusted deployment bindings, never a browser-supplied shell command, URL or manifest.

Limits are versioned; concurrent edits require `expected_version`. Tightening limits prevents new starts without erasing existing nodes or reservations. Raising UI limits cannot raise a ledger budget, extend a deadline, or bypass the provider manifest. A start that no longer fits returns a concrete blocker. No automatic recharge.

Drain stops new claims and retains reconciliation/collection. Stop is drain-then-destroy only after original execution, collection and provider obligations are safe. Partial/unknown outcomes retain their operation and provider identities; do not retry creation with another identity. Controller health, observation age, preparation, runtime readiness and serving state are displayed separately. No stale green state.

An ambiguous bootstrap is `runtime_state: blocked` with `reason_code: bootstrap_reconciliation_required`, not indefinite preparation or proof of failure/removal. The node's optional `bootstrap` observation has a controller `observed_at`, aggregate state/reason and bounded per-slot `index`, `state`, `phase`, `failure_phase`, `error_code` and `error_type`. Only allowlisted static diagnostics are public; raw logs, exception messages, credentials and file paths are excluded. The last observation may remain visible after state changes and must be labelled historical. This projection preserves the original boot/rental journals, deadlines and reservations; it neither authorizes a restart nor converts unknown execution into safe destruction.

## Model-first stock selection

The console first selects the exact model ID and FL2VA/Ref2VA mode, then reads
`GET /v1/operator/capacity/candidates?model_id=...&mode=...&ttl_seconds=...`.
[Issue #89](https://github.com/inkseq/h3-studio/issues/89) replaces upfront
provider/GPU/count selection with ranked Lium/Targon allocations. Each row is a
complete Lium executor or a Targon resource SKU, not a promised physical host.
It contains provider/offer identity, GPU type/count, whole-allocation quote,
memory/disk/network observations, qualification blockers and its preview selection.
One exact executor choice authorizes at most one allocation; additional machines
require separate choices and confirmations.

Only hardware explicitly catalogued for the selected model is considered.
Known insufficient RAM/disk is excluded. Unknown specifications and unqualified
topologies remain visibly blocked. Qualified deployments sort first, then
whole-allocation hourly price, then known/higher download bandwidth. Multi-card
hosts retain their full price and require one execution slot per GPU. There is
no provider priority, automatic rental, weight/precision or mode substitution.
Catalog bandwidth/price guidance produces hints; protected binding limits remain
hard requirements. Historical timing applies only to its measured configuration.

Lium eligibility uses allocatable RAM: host telemetry minus the larger of 4 GiB
or 1% host reserve; displayed host RAM stays unchanged. Targon SKU RAM is already
the allocated specification. See [Lium allocation documentation](https://docs.lium.io/developers/quickstart).

Optional `offer_id` binds the preview, confirmation and durable start command to
that allocation. A fresh normalized cache is checked at preview, confirmation
and before reservation. Changed quote/specification requires a new preview;
stock count changes alone do not invalidate the quote, but full availability is
still required. Provider creation rechecks the exact executor/SKU, quoted ceiling
and protected constraints before POST, with no fallback. Upstream prices remain
preflight-only caps where atomic price enforcement is unsupported. Unknown POST
outcomes retain the existing journal and intent, never another rental identity.

Exact commands use a protocol-domain binding digest; old controllers reject it
before reservation even after a lease change or rollback. Legacy command hashes,
node binding hashes and LaunchSpec serialization are unchanged. Operational
admission also requires a fresh `operator-offers-v1-` controller heartbeat.
Roll out the API and controller together before enabling this frontend; a
downgraded controller can block pending exact commands but cannot substitute a
machine. No schema/data migration is required.

Preview separates actual whole-allocation quote from conservative deployment
ceiling and budget reservation. Changing model/mode/window/machine or an expired
observation/preview clears consent. Stock is not a reservation or runtime proof.

## Legacy stock and next-tier API compatibility

[Issue #85](https://github.com/inkseq/h3-studio/issues/85) adds advisory
Lium/Targon stock, independently of the existing binding-based start admission.
`provider` is optional in a selection; omission still means Lium and remains
absent in historical canonical selections/hashes. Explicit Targon cannot resolve
to a Lium binding, including through a custom resolver. Existing accepted jobs,
previews, binding fingerprints and cumulative ledgers are unchanged.

`GET /v1/operator/capacity/offers` accepts the full selection: profile, mode,
GPU type/count, `node_count`, `ttl_seconds`, `provider`, and JSON-encoded `filters`
(at most 4096 characters). Existing defaults remain compatible. Its additive
`market` projection contains the echoed selection/hash, effective filter basis,
per-provider observation timestamps/status, stock rows and recommendations.
The HTTP API reads a normalized DB cache; it performs no supplier calls and
needs no new credentials. The legacy top-level availability remains exact-binding
admission evidence; `market.advisory_only` is always true.

A successful observation expires after 120 seconds. Failed, malformed, partial,
future-dated, stale or unconfigured observations do not prove absence. Unknown
specs or possible GPU splitting must remain unconfirmed. When the selected
provider has no matching single 5090 allocation, the legacy API recommends a single PRO
6000 from fresh Lium/Targon stock (Lium first, then whole-node price). Never
increase GPU/node count, switch weight/precision/mode, or raise the price cap.
Each recommendation retains the original selection except provider/GPU type.
Selecting it requests a new preview; it neither reserves money nor rents.

Rows distinguish actual whole-resource quotes from approved deployment ceilings.
Targon MiB and CPU millicores are converted explicitly; unknown edition,
bandwidth and location stay unknown. Lium larger/partially available hosts are
not quoted as cheap one-GPU slices. An available resource count does not prove
independent physical hosts. Price/metadata and runtime qualification blockers
are visible even when a larger-GPU suggestion is useful.

The Targon VM adapter under [#86](https://github.com/inkseq/h3-studio/issues/86)
requires an explicit immutable manifest and protected deployment binding. A
generic registry still admits only Lium. Targon admission binds the provider,
organization, resource/image, SSH-key IDs, topology, RAM/disk floors, whole-node
price ceiling, maximum lifetime and original approval window. The price ceiling
is enforced at preflight; unsupported network/country/CPU filters cannot be
claimed as enforced. Registration, deployment and deletion retain exact remote
identity and durable journals; an ambiguous submission is reconciled, never
replayed as another rental.

Targon VM lifetime uses an independent host guardian, not a provider-native TTL.
Deployment, SSH/bootstrap and new generation require a fresh guardian proof for
the exact instance and original deadline. The controller can write requests but
cannot write guardian acknowledgements or health receipts. A completed guardian
sweep publishes `process_state: running` separately from its aggregate obligation
state. An unrelated pending/blocked cleanup keeps that aggregate state degraded;
it does not invalidate another exact UID's fresh `armed` receipt. Missing/stale
process health, unknown health states, or a missing/stale/non-armed receipt for
the requested UID close admission. Earlier guardian versions without the explicit
process-health field fail closed: update the protected guardian together with the
controller before using this proof. Original unresolved receipts, deadlines,
bounded retries and billing obligations remain retained. This mechanism does not cover loss of the guardian host,
guarantee deletion through a provider outage, or establish billing settlement.
Public inventory, adapter capability and bootstrap readiness each remain
insufficient evidence of live inference acceptance.

Pending deletion observations use a durable 60-second interval per node in the
capacity controller. Targon's independent guardian also limits its own pending
removal reads to once per minute; these remain separate safety observers rather
than a combined one-request-per-minute quota. Health sweeps and observations of
other active machines keep their existing cadence. First deadline enforcement
is immediate. Skipped checks do not refresh provider observation timestamps.
Late positive terminal receipts can be consumed without waiting. Restart and
failed reads retain the interval, original deletion obligation and reservation.
Elapsed time alone never confirms removal or billing settlement.

The operator state response adds `node.removal_confirmation` for a requested
deletion: `pending`, `overdue` after 300 seconds, or `confirmed` from the existing
ledger's terminal removal evidence. It contains the original request time,
last actual provider observation, next eligible check and only allowlisted
observation fields. A scheduling claim and a webpage refresh are not provider
observations. Overdue is an attention signal, not a lifecycle transition.
Retained slots and rates on a removal card describe historical allocation;
they do not establish current hardware activity or final billing.

### Explicit manual Targon removal review

For the beta VM API's stale exact-UID `Stopping` records, an authorized browser
operator may acknowledge an already requested teardown using
`POST /v1/operator/capacity/nodes/{node_id}/manual-review` with the original
`provider_instance_id`, current `expected_version`, both `account_absent: true`
and `no_continuing_charge: true`, and `Idempotency-Key`. These fields are explicit
human account/billing attestations; list absence or unchanged credit alone does
not trigger the action automatically. The browser first shows a review reminder
and requires both checks. Ordinary users and Agent credentials cannot mark it.

The existing global lock protects the audit and capacity exclusion. Only an
exact Targon intent already `destroying`, with its existing stop operation,
deletion-start receipt and stopped desired state, is eligible. Fresh bound
workers, current jobs, unknown/orphan device ownership, unfinished/unsafe attempts and
unconfirmed upstream stops reject the mark. Creation-unknown and active nodes
cannot use this exception. Identical replay returns the same operation;
conflicting replay rejects. Actor, time, exact instance, original intent,
deadline and stop operation are durably bound in the existing command/scaler
receipt authority, without a new schema or rental ledger.
The mark also requires a fresh `operator-offers-v1-review-v1-` controller
heartbeat. An earlier controller cannot accept the new command through an
app-only rollout; activate the updated controller before enabling review.

For the exact stopped node only, the same human-review transaction may retire
an expired unbound worker and release its remaining local devices. This requires
the original protected deployment binding and worker-spec hash to match, all
worker attempt/job history to be safely terminal, and exact device ownership
to match the worker spec. Capacity, worker/device and job/attempt locks prevent
concurrent revival; retirement increments the worker fence. Fresh/current
workers, inconsistent/cross-node ownership and unknown upstream stops remain
blocked. The durable `local_execution_release` audit records prior states,
fences, devices and terminal attempt IDs. It releases local ownership only;
it does not assert physical provider removal or settle any reservation.

The public removal observation becomes `manually_reviewed`, its next check is
null, and the node no longer counts toward operating capacity/hourly estimates.
Controller reconciliation skips the reviewed UID. The underlying instance
remains `destroying`: manual review is neither `destroyed`, provider stop proof,
nor a final bill. Original jobs, IDs, requests, deadlines and reservations remain.
Existing replacement/invoice gates requiring positive provider removal and
settlement are not waived. The final provider API/billing defect remains #86 in
backlog; no approximate charge is settled automatically.

The independent root guardian consumes the same committed audit through an
optional fixed read-only PostgreSQL bridge. Its service pins the verified full
database container ID with `--manual-review-database-container`; Docker labels
must still identify the Sixnine `db` service. Set the independently verified
application database with `--manual-review-database-name`; `postgres` remains
the compatibility default and may be only a maintenance database. The short
nonsecret database name is validated and passed as a separate `psql -d`
argument. The bridge retains the `postgres` OS/database role and read-only
transaction; it never reads or forwards application credentials.
It checks the audit/stop/node/UID
relations and absence of active execution obligations, then records its own
protected `manually_reviewed` receipt and stops polling that UID. DB failure or
invalid review never disables ordinary deadline enforcement. Persisted review
survives restart, and `removal_proof` still rejects it.

Standalone historical test UIDs without a business node use a separate explicit
root-only exception audit, enabled by `--manual-review-directory`. Each
root-owned single-link `{exact_uid}.json` must bind the retained request hash,
workload-identity hash and original deadline; it records a human auditor, review
operation/time, both account attestations, and `accepted_delete: true`. The
`intent_id` is null when no business intent exists; do not invent a ledger ID.
The
retained guardian must already have a deletion-start receipt. Writable/symlinked
paths, wrong UID/identity/deadline and invalid audits fail closed. This narrowly
authorizes stopping that historical polling obligation, never runtime, rental,
budget release, positive physical removal or invoice settlement. The service
config/window and retained request/receipt are not rewritten.

Compatible application publication may retain an active operator execution
release. The protected host checks an exact execution-to-app review receipt
bound to both manifests, the original image and preparation hash. Schema/startup
migrations and accepted work remain unchanged; a reviewed conservative readiness
tightening is explicitly recorded rather than described as unchanged admission.
Operator command semantics and host Compose/configuration files remain identical.
Publication replaces only the app with `--no-deps`, preserving controller,
guardian, provider identities, original deadlines and accounting. Admission
closure and recovery resolve the current approved compatible app independently
of the immutable execution pin. Operator account additions use separate protected
app settings, never a rewrite of execution preparation. Unknown or incompatible
changes still require reconciliation; outstanding billing alone does not force
a website outage. An app publication does not update a running controller's code.

The explicit protected `operator_handoff.py` action can replace controller code
while deletion remains pending only when there are no active tasks, unsafe
attempts, live bound workers or owned child processes. Every remaining allocation
must already be destroying under its original exact identity and stop intent.
It rejects incomplete start commands, binds the old supervisor to the exact
controller process, closes admission, and waits for both clean local shutdown
and host-supervisor exit. A one-use successor intent binds configuration and
ledger hashes before credentials are delivered. It preserves journals, original
deadlines and accounting, uses normal leadership expiry, and reopens only after
fresh successor health. Unknown outcomes retain the barrier and require
inspection, never another launch. Independent guardian code activation is
separate and must preserve its original requests, receipts and configuration.

## Historical generation hints

Use `total_seconds` consistently: task submission through local output save/validation, including component loading encountered in the task, excluding machine startup, queueing, dependency setup and prior downloads. Store process/loading context separately; no controlled warm/cold comparison was performed. Match profile, mode, resolution, native frames/fps, steps and reference roles exactly. A single measured sample is not an SLA or an estimate for another configuration.

5090 historical 48->39 reference-frame trimming cases and BF16 v1 gray-noise outputs are excluded from verified samples. The aligned 5090 video-reference case qualifies only its measured 480p/20-step combination. Accepting an audio input does not establish speech or voice fidelity. Host-shared PRO comparisons do not isolate hardware or precision as the only variable.

## Delivery gate

Verify cookie/PAT isolation, replay/conflicts, concurrent global limits, unknown rentals, restart/drain/collection and profile identity offline first. Then record the exact host/runtime/recipe and actual Quick Chat or Agent job-to-artifacts result for operational acceptance. Source merge, a working control page, provider `running`, and native experiment success each remain insufficient alone.
