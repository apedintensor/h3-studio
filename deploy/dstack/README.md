# dstack capacity boundary

`studio_platform.dstack_capacity` targets dstack **0.22.3** using its REST API.
The pinned CPU server in `compose.yml` owns Vast/RunPod credentials and a separate
dstack database/user. The business database retains original capacity intents,
reservations, accepted jobs, attempts and output obligations. The dstack database
is infrastructure metadata, not another business queue or telemetry archive.
The app accesses loopback port 3000; do not publish the administrator endpoint.

The integration owner supplies `DstackStore` over the existing ledger. Its
`begin_apply` transaction commits the immutable run spec and original reservation
once before `/runs/apply`. It returns false for any previously started operation.
A lost response uses exact `/runs/get` with run UUID or stable owned name; absence
is unknown and does not permit another apply. `force:false/current_resource:null`
also rejects a concurrent existing dstack resource. `record` retains immutable
run/provider/incarnation identity. `begin_stop` locks and checks jobs, unsafe
attempts, collection holds and retired workers before committing stop intent.
Do not provide a file-based substitute ledger in production or the operations CLI.

Each new run requests exactly **one GPU**, explicit backend, image digest,
supported model/profile/mode and resource floors. A container whose allocation
reports more than one GPU cannot become ready. Additional GPUs need independently
registered native slots and proof; a whole multi-GPU price must not hide idle GPUs.
`get_plan` is advisory inventory and never rents. A failed provider does not
silently substitute another model, GPU, backend or precision. Historical test
cases are timing evidence, not an input whitelist.

The run command remains alive (`sleep infinity`) while the CPU controller uses
the existing SSH transport to upload the exact native profile bundle/manifest,
private loopback token and `native_bootstrap_config`, then invokes once:

```sh
/venv/main/bin/python /opt/sixnine/wangp-bootstrap.py \
  --config /workspace/h3-studio/profile-slot-0/wangp-runtime.json \
  --slot-key ORIGINAL_INTENT_UUID \
  --token-file /workspace/h3-studio/profile-slot-0/wangp-token
```

This reuses `wangp_profile_bootstrap`, the hash-checked model downloader and
`wangp_launcher`'s persistent `PinnedWanGPSession`. Dependencies belong in a
captured image (`Dockerfile.wangp`), not fresh apt/pip operations on each rental.
The derivative requires a digest-pinned **native** image with exact Python3.12.14
and profile core versions; the older BF16 resolver image is not interchangeable.
Pin the derivative's published digest in `CapacityRequest`. An image build alone
does not establish NVIDIA compatibility or real inference. First startup hashes
and loads public model files; cache availability depends on the backend/region.
RunPod network volumes have separate storage cost and locality restrictions.
Vast's local volume support is not automatically exposed by dstack.

dstack `running` means the command is running. Native readiness separately binds
the exact manifest, slot, provider instance and runtime incarnation. Even a native
ready Session does not prove the transformer has loaded: `model_load_state` stays
`not_observed` until trusted runtime evidence observes it. The first accepted
task may therefore incur model load. Later jobs reuse the same Session; profile
offloading can still occur inside WanGP. No extra paid warm-up test is fabricated.

The controller measures **business** idle time. It stops only after 600 seconds
without queued demand, running/unknown attempts or collection holds, subject to
configured manual hold and the original absolute deadline. `max_duration` is a
runner fallback measured from dstack running; it excludes provisioning/image
pull and is not an absolute billing cap. Deadline admission must leave time to
finish/collect before that fallback. Deadline plus outstanding work means drain
and reconcile, not erase obligations. A stop response is not a zero invoice;
dstack cost is explicitly an estimate until supplier billing settles.

The trusted CPU CLI uses a service factory over the original ledger:

```sh
python -m studio_platform.dstack_capacity spec --request /protected/request.json --ssh-public-key /protected/controller.pub
python -m studio_platform.dstack_capacity bootstrap-config --request /protected/request.json --source-bundle-sha256 HASH
python -m studio_platform.dstack_capacity plan --request /protected/request.json --ssh-public-key /protected/controller.pub --factory studio_platform.dstack_integration:create_capacity
python -m studio_platform.dstack_capacity start --request /protected/request.json --ssh-public-key /protected/controller.pub --factory studio_platform.dstack_integration:create_capacity
python -m studio_platform.dstack_capacity observe --request /protected/request.json --factory studio_platform.dstack_integration:create_capacity
python -m studio_platform.dstack_capacity stop --request /protected/request.json --factory studio_platform.dstack_integration:create_capacity
```

The integration factory is supplied by the main application integration; it must
never auto-create a replacement store. The first two commands are inert and do
not contact a provider. Remaining commands obey existing authority and journals.
Keep manifests, request files and protected tokens outside the public checkout.
No secret is accepted in a CLI argument or serialized into a GPU run spec.

Grafana Cloud receives allowlisted business stage events from the CPU telemetry
adapter. dstack's automatic HTTP/log instrumentation is disabled here because raw
request bodies, provider responses and media URLs must not enter Cloud telemetry.
Short bounded Docker logs remain operational transport buffers, not long-term
storage. The wider rollout/retirement gate is tracked in issue #132.

References: [pinned run schemas](https://github.com/dstackai/dstack/blob/0.22.3/src/dstack/_internal/server/schemas/runs.py),
[pinned configuration models](https://github.com/dstackai/dstack/blob/0.22.3/src/dstack/_internal/core/models/configurations.py),
[run API](https://dstack.ai/docs/reference/api/runs/),
[backend boundaries](https://github.com/dstackai/dstack/blob/0.22.3/contributing/BACKENDS.md).
