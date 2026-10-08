# Operating the GPU capacity console

Delivery: [E3 #71](https://github.com/apedintensor/h3-studio/issues/71). See [the contract](OPERATOR-CAPACITY-CONTRACT.md) for authority and compatibility. This guide describes implemented configuration; it is not a production release or a new spending authorization.

## What is available

The canonical frontend route is `/operator`. An explicitly allowlisted operator uses a normal authenticated browser session; ordinary accounts and generation API keys cannot access the rental controls. The page manages a Lium host count, GPUs per host, a deployment profile and FL2VA/Ref2VA mode, approved hardware filters, a latest stopping time, global capacity limits, drain and safe stop. An accepted start is asynchronous. The page shows provider observations, runtime stages, controller freshness, immutable operation IDs and actionable blockers.

The initial catalog contains:

| Profile | Tested host topology | Component strategy | Generation evidence |
|---|---|---|---|
| Pruned Rank8 INT8 | 1 × RTX 5090, 32 GB VRAM | INT8 backbone / INT8 text encoder / INT8 VAE, SDPA, low RAM | 8 matched FL/REF cases |
| Unpruned 33B INT8 | 2 × RTX PRO 6000 Server, 96 GB each | INT8 backbone / BF16 text encoder / FP16 VAE, SDPA | 7 matched FL/REF cases |
| Unpruned 33B BF16 v2 | Same two-card PRO host | BF16 backbone / BF16 text encoder / FP16 VAE, split QKV | 7 matched FL/REF cases |

One PRO host currently launches two independent GPU slots of the **same** selected mode/profile. It is not tensor parallelism, and it is not two independent machines. Create separate hosts for different modes/profiles. The experimental mixed-precision comparison on one host does not establish a mixed-profile production topology.

All measured videos use 124 native frames at 24 fps (about 5.17 s). The catalog records 480p/768p and 20/50-step **joint cases**, not every possible combination. Reference cases accept only the tested combinations and bounds: at most one image, one silent 56-frame/24-fps video and one independent 2–5.2 s audio reference. PRO FL measurements require both endpoint images. Additional controls or combinations need separate qualification; this does not claim full H3 control parity.

Example historical task totals, excluding queue/VM start/download: 5090 FL first+last at 480p/20 steps 96.791 s; 5090 REF image+video+audio at 480p/20 steps 161.568 s; PRO INT8 same REF case 151.950 s; PRO BF16 same REF case 193.664 s. These are observations, not completion guarantees. Full samples, source revisions and input roles are in `deploy/wangp/profiles/*.json` and the linked measurement evidence.

### RTX 5090 rental filter policy

The catalog defaults are **96 GiB RAM, 128 GiB free disk and 200 Mbps advertised download bandwidth**. The disk and bandwidth values relax the original 250 GiB / 500 Mbps pilot filters to widen eligible inventory. They are derived operating policy, not newly measured hardware minima. PRO 6000 filters remain unchanged.

The [original 5090 proposal](https://github.com/apedintensor/h3-studio/pull/68) used conservative admission; the [completed 5090 pilot](https://github.com/apedintensor/h3-studio/pull/69) ran on 105 GiB actual cgroup RAM. Its runtime still requires 96 GiB effective available RAM, accounting for reclaimable clean file cache, and 28 GiB free VRAM. A 64 GiB host has not been qualified. The [PRO comparison](https://github.com/apedintensor/h3-studio/pull/70) used a separate 279 GiB shared host and does not establish a smaller 5090 RAM requirement.

Pinned 5090 files total **51,898,376,278 bytes (48.33 GiB)** for one mode and **72,956,051,066 bytes (67.95 GiB)** for both modes. Current operator nodes prepare one selected mode and reuse the pinned image's installed environment. For a split rental, [Lium's allocation rules](https://docs.lium.io/developers/quickstart) divide the pod's disk share between the `/root` volume (two thirds) and container storage (one third). A 128 GiB pod share therefore provides about 85 GiB for `/root/sixnine-cache/models`, leaving about 37 GiB beyond one mode's weights, before temporary files and outputs. It is not 128 GiB of model-cache space. The downloader checks missing-file bytes plus 10 GiB free headroom on the actual cache filesystem and limits its separate cache to 1 GiB. This is a sizing calculation, not a successful run on a 128 GiB allocation; a different image, both-mode cache or environment installation needs its own sizing check.

At a sustained 200 Mbps, the one-mode payload alone would take about **34.6 minutes**; both modes would take **48.6 minutes**, before setup and verification. Advertised provider bandwidth is not measured Hugging Face throughput or an SLA. The lower filter accepts a cold-start latency tradeoff; bootstrap, task-admission and supplier-deadline checks remain in force. Operators must allow sufficient authorized startup time.

Catalog changes do not update protected live provider manifests or immutable deployment bindings automatically. Prepare a matching replacement configuration through the approved release process before using these filters; existing attempts retain their original configuration. These metadata changes do not alter model files, runtime settings or engine-manifest digests.

For this filter-only activation, retain each old binding and its provider manifest; disable the old binding only for new starts. Give the successor a new `binding_id` and immutable provider-manifest path, setting `minimum_disk_gib: 128` / `min_disk_gib: 128` and `min_download_mbps: 200` consistently in the manifest and binding. Preserve `configuration_id`, pool, model/engine/recipe, source hashes, RAM/VRAM, GPU count, price, budget accounts and original expiry/TTL. No generation-policy rewrite or source-package rebuild is necessary solely for these hardware filters. The old binding fingerprint remains unchanged because only its `enabled` flag changes. Do not reload API/controller configuration until the existing protected release barrier proves no owned processes or in-flight obligations will be lost; unresolved restart recovery remains #60. No live configuration is changed by publishing this guide.

Use RAM/disk requirements for the rented pod's proportional share, not the whole host's advertised totals. An eight-GPU host with enough total RAM may still fail the single-GPU requirement. Provider minimum rental counts also apply. Looser disk/network filters do not guarantee that a qualifying one-GPU pod is currently available, and must never trigger a silent switch to four/eight GPUs or a lower RAM allocation.

## Local product review

From this backend checkout, using the existing Python environment:

```powershell
python tools/run_operator_preview.py --frontend C:/absolute/path/to/frontend/build --port 8897
```

This preview uses an isolated SQLite database and loopback-only test authentication. `superdan` can review the console; `supervan` remains an ordinary creator. It exposes real routes and the real catalog, but no provider bindings, inventory credentials, paid controller or generation. A blocked start here is intentional and is never reported as a running GPU.

## Provision the controller configuration once

Use the existing platform database and budget/instance ledgers on the chosen controller host. Do not copy the preview database into production. Do not reset a budget to make the page green, and do not run two competing legacy/manual controllers for the same pool. Existing accepted jobs and legacy pools retain their route until their obligations are reconciled.

1. Build immutable small source sets on the release host with `python tools/build_operator_sources.py --output /absolute/new/sources`. This creates ten slot directories plus `index.json`; it installs nothing and does not download models. A repeated destination is rejected. The index supplies exact hashes, profile IDs, manifest digests, mode and slot topology.
2. For each approved profile/mode, prepare a protected `LiumManifest` JSON. Use the existing provider configuration schema, a current explicit template, server-side selection and the tested GPU count. Bind approved VRAM/RAM/disk/download/price/country filters and the existing public SSH key. No private key or API key belongs in this JSON. CPU-core filtering is not supported by the current provider adapter and is rejected instead of pretending to enforce it.
3. Create a protected registry `{ "schema_version": 1, "bindings": [...] }`. Each binding follows `studio_platform.operator_capacity.DeploymentBinding`: exact profile, model, engine digest, pool/configuration, functional recipe, existing scope/budget account IDs, full-host hourly cap, full-window reservation, expiry, supported TTL range and `enabled`. Its `boot` has exactly `provider_manifest_file`, `source_dirs` and `source_sha256`; arrays contain one distinct directory/hash set per GPU from the source index. Keep disabled retired bindings until their instances and invoices are reconciled. A changed binding is a replacement, not an edit to a running attempt.
4. Create a protected runtime file with `schema_version: 1`, absolute `registry_file`, existing `work_dir`, existing `ssh_key_file`, `known_hosts_file`, and `port_start` (reserve 1,024 unused local ports). The exact runtime Python is `/venv/main/bin/python`. Host-key trust defaults to false. `credential_source: central_registry` reuses `lium/lium--rig-root`; a Linux service instead uses `credential_source: aws_runtime` plus the existing explicit `secret_arn` and `secret_version_id` references. No credential values, new dotenv file or copied loader.
5. Configure an execution-profile file `{ "schema_version": 1, "policies": [...] }` using `studio_platform.execution_policy.validate_policy`. Each entry binds one profile and one mode to the same pool/config/model/engine digest. Use `execution_profiles.tested_envelope(profile_id, mode)` as a conservative starting scope, `output_delivery: native-frames-v1`, and `qualification.profile: queued-task-first-v1` with `status: runtime_required`. Set reservation amounts, expected runtime, account IDs and expiry from the actual authorized operating policy. An experiment timing or catalog entry does not authorize a policy. Keep the legacy policy separate.
6. Mount the source/config files read-only into API and controller at the same absolute paths. Only the controller receives writable work/known-hosts and existing platform state, plus the existing SSH credential file read-only. The API does not need the private SSH key, controller work directory, cloud role or provider network access. API and controller must share the **same database**, absolute configuration references and release. API environment: `H3_OPERATOR_RUNTIME_CONFIG`, `SIXNINE_EXECUTION_PROFILES_FILE`, and `SIXNINE_OPERATOR_CAPACITY_OWNERS=superdan`. Do not simultaneously set `H3_OPERATOR_CAPACITY_REGISTRY`. Existing authentication, generation/backend, storage, queue and budget settings remain explicit. `superdan` is the existing platform account/session owner; this code does not map a Cognito subject to an account. Ordinary API keys have no rental permission.

`operator_runtime.create_registry(path, repository=repo)` validates source hashes, topology, model identities, filters and price/reservation using protected metadata. It never constructs a provider or requires private path mounts. `create_controller(path)` additionally validates controller-private paths, but does not fetch credentials or call Lium during construction. Existing test fixtures illustrate schemas, but contain synthetic identities and are **not deployable configuration**. Linux configuration files must be operator-owned, without group/world write or world access; linked paths are rejected.

After exact-version release and protected-host approval, run the existing controller entry point:

```text
python -m studio_platform.operator_controller --factory studio_platform.operator_runtime:create_controller --config /absolute/protected/runtime.json --enabled
```

This does not initialize global capacity or budget accounts. The existing global capacity gate must already be configured. In the console, set versioned manual limits within that authority. Then select an approved binding, refresh availability, preview, and explicitly confirm a start. Editing a hardware filter beyond a configured binding is rejected until a matching trusted binding exists; the browser cannot invent arbitrary launch commands.

For the isolated AWS controller container, use the trusted factory `studio_platform.operator_runtime:create_controller_from_stdin` instead. The existing protected host helper loads one approved Secrets Manager version and supplies the existing bounded `{secret_arn, version_id, payload}` envelope over private stdin, then closes the pipe. The factory reuses `production_scaler.stdin_loader`; it does not fetch AWS credentials or fall back to the workstation vault. Do not paste or put this envelope in a command, file, log or environment variable. Keep the API on its internal networks and the controller on its approved database/egress networks; neither needs EC2 metadata access.

Availability is an observation in the existing database, not a second rental ledger. The controller performs one read-only provider inventory probe at a time outside its lifecycle loop. The API accepts a result only for the exact immutable binding fingerprint, a matching current controller heartbeat no older than 30 seconds, and an inventory observation no older than 120 seconds. Missing/stale/unavailable inventory blocks previews and new confirmations; an exact idempotent replay still returns its original operation. Prices remain approved ceilings, not live quotes. A slow inventory request cannot delay provider lifetime checks or collection.

Before enabling a new binding, verify the existing global `capacity_gate`, its pool limits, and referenced budget scopes. Initialize only genuinely new pools under the approved policy. Changing global/operator maxima does not reset spent/reserved money or settle earlier work. Rental reservations use the binding's infrastructure scope; generation reservations must also match each creator's owner/project scope. An owner-scoped `superdan` budget cannot fund `supervan` jobs merely because both use the same GPU. Keep unresolved historical reservations until settlement evidence exists.

The controller writes a small atomic `controller-status.json` in its private work directory after successful ticks and before a clean shutdown return. It binds `runtime_config_sha256` to `repository.request_hash` of the raw parsed runtime JSON, plus controller identity, observation time and lifecycle state. `shutdown_complete` and `local_connections_released` describe local collectors/tunnels only; `cloud_removal_confirmed` and `billing_settled` remain false. A host release barrier must combine this receipt with the exact container/image/configuration/exit status and a fresh database obligation check. A receipt alone does not authorize service replacement.

## Start, dispatch and stop

The Lium provider rents integer hours. The requested TTL is an upper stopping bound, not guaranteed uptime; minimum supported request is 3,780 seconds to leave the existing provider/preview margins. The controller records the supplier's verified safe deadline and only shortens the original ledger deadline. Admission checks that deadline plus task and collection margin. Unavailable or stale lifetime proof blocks new dispatch while an already bound task can reconcile/collect. The original budget reservation remains until settlement evidence exists.

Each physical GPU gets its own immutable GPU UUID, private runtime directory, HTTP port, source manifest, worker identity, tunnel and process receipt. Shared downloads use a bounded cache lock. Bootstrap validates the exact prepared WanGP revision and dependency set before registering capacity. It does not run separate synthetic smoke videos. The first accepted user job supplies real generation evidence after output validation and persistence.

Quick Chat selects the deployment profile and FL/REF mode. Saving or revising a card preserves that choice. The API uses optional top-level `deployment_profile_id` on generation plans; omitting it preserves legacy routing. A ready worker must match profile-derived model, configuration, engine digest and recipe. Failure to find matching capacity is visible; a different ready model never substitutes silently. A manual start does not itself submit a video.

Drain stops new claims. Safe stop also waits for bound jobs, output collection and positive per-slot idle proof before removal. An uncertain rental keeps its original identity and reservation. A controller restart does not blindly spawn a replacement for an unobserved child: unknown process ownership is reported and needs recovery under [#60](https://github.com/apedintensor/h3-studio/issues/60). Do not restart a controller with live owned processes as a normal way of changing profiles. The supplier TTL still bounds remote lifetime, but it does not reconcile unpaid invoices or retrieve lost files.

SIGINT/SIGTERM initiate a graceful controller drain: no new rentals, retained original node identities and continued reconciliation/collection. The process closes its local tunnels only after proving that its owned children and bound jobs are finished; this is not a supplier-removal or billing-settlement receipt. The default 900-second grace threshold reports attention rather than forcibly killing collectors. A service manager must not impose a shorter forced-kill timeout (`TimeoutStopSec=infinity` or an explicitly reviewed recovery policy). Forced termination, host loss and adopting a previous process's unknown fleet remain #60; do not claim this orderly shutdown path proves crash recovery.

## Acceptance before calling it operational

Offline coverage establishes permissions, idempotency, accounting, slot isolation, native output delivery and profile matching. Release packaging includes the profile catalog and fingerprints it for both API/worker compatibility. A code merge does not publish the frontend or enable a rental service.

The remaining live gate for E3 is: approve the local UI, bind the actual host configuration and unchanged ledgers, release the exact API/frontend/controller versions, start one requested profile from the page, submit a Quick Chat/Agent job, retrieve validated video/audio, then observe drain/removal/settlement. Record exact receipts on #71; track broader mode/recovery/image qualification under #22, #29, #60, #61 and public proof under #16. Never mark those complete merely because the catalog lists historical native runs.
