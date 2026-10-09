# Operator capacity and deployment profiles

Accepted direction: 2026-10-09. Delivery and acceptance: [E3 #71](https://github.com/apedintensor/h3-studio/issues/71). This extends the [generation contract](GENERATION-CONTRACT.md); it does not renew a budget or certify a deployed runtime.

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

The operator API uses `/v1/operator/capacity`. Start follows selection -> bounded preview -> explicit confirmation with an idempotency key -> durable operation -> controller reconciliation. Identical replay returns the original operation. Conflicting reuse is rejected. Stock and quotes are observations with timestamps, not reservations. Selection accepts only allowlisted provider/profile/hardware/filter values from trusted deployment bindings, never a browser-supplied shell command, URL or manifest.

Limits are versioned; concurrent edits require `expected_version`. Tightening limits prevents new starts without erasing existing nodes or reservations. Raising UI limits cannot raise a ledger budget, extend a deadline, or bypass the provider manifest. A start that no longer fits returns a concrete blocker. No automatic recharge.

Drain stops new claims and retains reconciliation/collection. Stop is drain-then-destroy only after original execution, collection and provider obligations are safe. Partial/unknown outcomes retain their operation and provider identities; do not retry creation with another identity. Controller health, observation age, preparation, runtime readiness and serving state are displayed separately. No stale green state.

An ambiguous bootstrap is `runtime_state: blocked` with `reason_code: bootstrap_reconciliation_required`, not indefinite preparation or proof of failure/removal. The node's optional `bootstrap` observation has a controller `observed_at`, aggregate state/reason and bounded per-slot `index`, `state`, `phase`, `failure_phase`, `error_code` and `error_type`. Only allowlisted static diagnostics are public; raw logs, exception messages, credentials and file paths are excluded. The last observation may remain visible after state changes and must be labelled historical. This projection preserves the original boot/rental journals, deadlines and reservations; it neither authorizes a restart nor converts unknown execution into safe destruction.

## Model-first stock selection

The console first selects the exact model ID and FL2VA/Ref2VA mode, then reads
`GET /v1/operator/capacity/candidates?model_id=...&mode=...&ttl_seconds=...`.
[Issue #89](https://github.com/apedintensor/h3-studio/issues/89) replaces upfront
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

[Issue #85](https://github.com/apedintensor/h3-studio/issues/85) adds advisory
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

The Targon VM adapter under [#86](https://github.com/apedintensor/h3-studio/issues/86)
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
cannot write guardian acknowledgements or health receipts. Missing, stale or
degraded proof closes new admission while existing reconciliation and collection
remain obligations. This mechanism does not cover loss of the guardian host,
guarantee deletion through a provider outage, or establish billing settlement.
Public inventory, adapter capability and bootstrap readiness each remain
insufficient evidence of live inference acceptance.

## Historical generation hints

Use `total_seconds` consistently: task submission through local output save/validation, including component loading encountered in the task, excluding machine startup, queueing, dependency setup and prior downloads. Store process/loading context separately; no controlled warm/cold comparison was performed. Match profile, mode, resolution, native frames/fps, steps and reference roles exactly. A single measured sample is not an SLA or an estimate for another configuration.

5090 historical 48->39 reference-frame trimming cases and BF16 v1 gray-noise outputs are excluded from verified samples. The aligned 5090 video-reference case qualifies only its measured 480p/20-step combination. Accepting an audio input does not establish speech or voice fidelity. Host-shared PRO comparisons do not isolate hardware or precision as the only variable.

## Delivery gate

Verify cookie/PAT isolation, replay/conflicts, concurrent global limits, unknown rentals, restart/drain/collection and profile identity offline first. Then record the exact host/runtime/recipe and actual Quick Chat or Agent job-to-artifacts result for operational acceptance. Source merge, a working control page, provider `running`, and native experiment success each remain insufficient alone.
