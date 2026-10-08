# Operator capacity and deployment profiles

Accepted direction: 2026-10-09. Delivery and acceptance: [E3 #71](https://github.com/apedintensor/h3-studio/issues/71). This extends the [generation contract](GENERATION-CONTRACT.md); it does not renew a budget or certify a deployed runtime.

## One authority

The operator console manages manual capacity intents. A start is an explicit paid-capacity decision, independent of user generation demand. It does not create a synthetic video job. Durable operator operations link to the existing instance intents, reservations, scaler actions, provider receipts and worker identities. The existing global capacity gate and cumulative budget remain authoritative across manual and automatic capacity. Unknown creation remains a reconciliation obligation. HTTP handlers never rent synchronously.

Only explicitly configured operator browser identities can mutate capacity. Ordinary browser users and generation API keys cannot rent or stop machines. Existing authentication, same-origin checks and expected-account protection apply. Provider credentials, private runtime endpoints, SSH paths and environment values never enter public DTOs.

## Hardware and deployment identity

A node is a billed provider host; a slot is an isolated GPU executor. A two-GPU node is not two independent nodes. Display whole-host price and GPU count. Each slot has one immutable deployment profile and functional mode. A change of profile requires drain and a separately prepared replacement; accepted attempts keep their identity. Multi-slot readiness cannot be inferred from one healthy GPU.

Three explicit profiles are catalogued initially: pruned rank8 INT8 on RTX 5090, unpruned 33B INT8 on PRO 6000, and corrected unpruned BF16 on PRO 6000. The current user accepts pruning. Legacy BF16 jobs are not converted. Each profile binds source/weight revisions, component hashes, precision, model type, memory policy, kernel/QKV layout and exact control validation. Historical native experiments, adapter implementation, installed identity and live worker readiness are separate evidence.

`deployment_profile_id` is optional on new generation requests, Quick Chat settings/card revisions and saved shot generation settings. Omission retains legacy routing. A present value is explicit: unknown, unconfigured or incompatible profiles fail closed, never fall back to a different model. Public functional recipe IDs still distinguish FL2VA and Ref2VA; they are not model or deployment IDs. The compiled snapshot, policy hash and engine manifest preserve the choice through preflight, confirmation, worker claim and recovery.

An operator-owned execution-profile file can bind several profile/mode policies. Each remains individually qualified, budgeted and revocable. Publishing a catalog entry does not enable it. Normal generation clients only see safe capability and measured-time projections.

## Commands and observations

The operator API uses `/v1/operator/capacity`. Start follows selection -> bounded preview -> explicit confirmation with an idempotency key -> durable operation -> controller reconciliation. Identical replay returns the original operation. Conflicting reuse is rejected. Stock and quotes are observations with timestamps, not reservations. Selection accepts only allowlisted provider/profile/hardware/filter values from trusted deployment bindings, never a browser-supplied shell command, URL or manifest.

Limits are versioned; concurrent edits require `expected_version`. Tightening limits prevents new starts without erasing existing nodes or reservations. Raising UI limits cannot raise a ledger budget, extend a deadline, or bypass the provider manifest. A start that no longer fits returns a concrete blocker. No automatic recharge.

Drain stops new claims and retains reconciliation/collection. Stop is drain-then-destroy only after original execution, collection and provider obligations are safe. Partial/unknown outcomes retain their operation and provider identities; do not retry creation with another identity. Controller health, observation age, preparation, runtime readiness and serving state are displayed separately. No stale green state.

An ambiguous bootstrap is `runtime_state: blocked` with `reason_code: bootstrap_reconciliation_required`, not indefinite preparation or proof of failure/removal. The node's optional `bootstrap` observation has a controller `observed_at`, aggregate state/reason and bounded per-slot `index`, `state`, `phase`, `failure_phase`, `error_code` and `error_type`. Only allowlisted static diagnostics are public; raw logs, exception messages, credentials and file paths are excluded. The last observation may remain visible after state changes and must be labelled historical. This projection preserves the original boot/rental journals, deadlines and reservations; it neither authorizes a restart nor converts unknown execution into safe destruction.

## Historical generation hints

Use `total_seconds` consistently: task submission through local output save/validation, including component loading encountered in the task, excluding machine startup, queueing, dependency setup and prior downloads. Store process/loading context separately; no controlled warm/cold comparison was performed. Match profile, mode, resolution, native frames/fps, steps and reference roles exactly. A single measured sample is not an SLA or an estimate for another configuration.

5090 historical 48->39 reference-frame trimming cases and BF16 v1 gray-noise outputs are excluded from verified samples. The aligned 5090 video-reference case qualifies only its measured 480p/20-step combination. Accepting an audio input does not establish speech or voice fidelity. Host-shared PRO comparisons do not isolate hardware or precision as the only variable.

## Delivery gate

Verify cookie/PAT isolation, replay/conflicts, concurrent global limits, unknown rentals, restart/drain/collection and profile identity offline first. Then record the exact host/runtime/recipe and actual Quick Chat or Agent job-to-artifacts result for operational acceptance. Source merge, a working control page, provider `running`, and native experiment success each remain insufficient alone.
