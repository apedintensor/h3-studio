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
6. Mount the source/config files read-only, work/known-hosts and existing platform state writable, and SSH credential file read-only into the controller. API and controller must share the **same database**, absolute configuration references and release. API environment: `H3_OPERATOR_RUNTIME_CONFIG`, `SIXNINE_EXECUTION_PROFILES_FILE`, and `SIXNINE_OPERATOR_CAPACITY_OWNERS=superdan`. Do not simultaneously set `H3_OPERATOR_CAPACITY_REGISTRY`. Existing authentication, generation/backend, storage, queue and budget settings remain explicit. Ordinary API keys have no rental permission.

`operator_runtime.create_registry(path)` / `create_controller(path)` validate source hashes, topology, model identities, filters, price/reservation and paths without fetching credentials or calling Lium. Existing test fixtures illustrate schemas, but contain synthetic identities and are **not deployable configuration**. Linux configuration files must be operator-owned, without group/world write or world access; linked paths are rejected.

After exact-version release and protected-host approval, run the existing controller entry point:

```text
python -m studio_platform.operator_controller --factory studio_platform.operator_runtime:create_controller --config /absolute/protected/runtime.json --enabled
```

This does not initialize global capacity or budget accounts. The existing global capacity gate must already be configured. In the console, set versioned manual limits within that authority. Then select an approved binding, refresh availability, preview, and explicitly confirm a start. Editing a hardware filter beyond a configured binding is rejected until a matching trusted binding exists; the browser cannot invent arbitrary launch commands.

## Start, dispatch and stop

The Lium provider rents integer hours. The requested TTL is an upper stopping bound, not guaranteed uptime; minimum supported request is 3,780 seconds to leave the existing provider/preview margins. The controller records the supplier's verified safe deadline and only shortens the original ledger deadline. Admission checks that deadline plus task and collection margin. Unavailable or stale lifetime proof blocks new dispatch while an already bound task can reconcile/collect. The original budget reservation remains until settlement evidence exists.

Each physical GPU gets its own immutable GPU UUID, private runtime directory, HTTP port, source manifest, worker identity, tunnel and process receipt. Shared downloads use a bounded cache lock. Bootstrap validates the exact prepared WanGP revision and dependency set before registering capacity. It does not run separate synthetic smoke videos. The first accepted user job supplies real generation evidence after output validation and persistence.

Quick Chat selects the deployment profile and FL/REF mode. Saving or revising a card preserves that choice. The API uses optional top-level `deployment_profile_id` on generation plans; omitting it preserves legacy routing. A ready worker must match profile-derived model, configuration, engine digest and recipe. Failure to find matching capacity is visible; a different ready model never substitutes silently. A manual start does not itself submit a video.

Drain stops new claims. Safe stop also waits for bound jobs, output collection and positive per-slot idle proof before removal. An uncertain rental keeps its original identity and reservation. A controller restart does not blindly spawn a replacement for an unobserved child: unknown process ownership is reported and needs recovery under [#60](https://github.com/apedintensor/h3-studio/issues/60). Do not restart a controller with live owned processes as a normal way of changing profiles. The supplier TTL still bounds remote lifetime, but it does not reconcile unpaid invoices or retrieve lost files.

SIGINT/SIGTERM initiate a graceful controller drain: no new rentals, retained original node identities and continued reconciliation/collection. The process closes its local tunnels only after proving that its owned children and bound jobs are finished; this is not a supplier-removal or billing-settlement receipt. The default 900-second grace threshold reports attention rather than forcibly killing collectors. A service manager must not impose a shorter forced-kill timeout (`TimeoutStopSec=infinity` or an explicitly reviewed recovery policy). Forced termination, host loss and adopting a previous process's unknown fleet remain #60; do not claim this orderly shutdown path proves crash recovery.

## Acceptance before calling it operational

Offline coverage establishes permissions, idempotency, accounting, slot isolation, native output delivery and profile matching. Release packaging includes the profile catalog and fingerprints it for both API/worker compatibility. A code merge does not publish the frontend or enable a rental service.

The remaining live gate for E3 is: approve the local UI, bind the actual host configuration and unchanged ledgers, release the exact API/frontend/controller versions, start one requested profile from the page, submit a Quick Chat/Agent job, retrieve validated video/audio, then observe drain/removal/settlement. Record exact receipts on #71; track broader mode/recovery/image qualification under #22, #29, #60, #61 and public proof under #16. Never mark those complete merely because the catalog lists historical native runs.
