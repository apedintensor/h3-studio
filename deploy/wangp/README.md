# Pinned WanGP H3 FL2VA execution recipe

This is the first deliberately bounded **BF16 Base, 50-step** path, not full H3
control parity or a measured performance claim. The business ledger, leases,
owner isolation, attempts, budgets, media validation and publication remain in
the existing platform. The upstream Session is private to a one-slot host.

## Reproduction and qualification status

- Source: `deepbeepmeep/Wan2GP@0e58385fbde7ff102d276e4a9e490845de76b4ea`.
- Components: `manifest.json` pins the HF repository revision, exact filenames,
  precision, byte sizes and SHA-256 (LFS), or Git blob SHA-1 (small JSON files).
  Metadata was read from the pinned [HF model API](https://huggingface.co/api/models/DeepBeepMeep/MiniMax-H3/revision/adc81ccb71352192214d83d5fafb9487e860be39?blobs=true)
  on 2026-10-07. This is source/metadata evidence, not proof of installed weights.
- Model files total **124,300,443,428 bytes**. Transformer: 66,280,486,944;
  Qwen layer-50 BF16: 51,506,305,568; video VAE FP16: 5,207,806,512;
  audio VAE FP32: 605,429,308; latent upscaler BF16: 690,592,992;
  the balance is tokenizer/configuration files. Disk size is not a VRAM estimate.
- `runtime-recipe.json` is a candidate installation recipe, **not a complete
  dependency lock or a qualified container image**. `runtime_digest` identifies
  the exact upstream requirements source, not a fabricated installed-image hash.
  Transitive wheels, OS packages, driver compatibility, image digest and measured
  memory/cold-start/inference results must be recorded on the authorized host.
- The candidate uses Python 3.11.14 and PyTorch 2.10.0/CUDA 12.8 wheels. The
  [pinned Dockerfile lines 1–43](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/Dockerfile#L1-L43)
  specifies these Torch wheels; the [manual guide lines 5–25](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/docs/INSTALLATION.md#L5-L25)
  recommends Python 3.11.14 and a different CUDA 13.0 recipe for modern RTX.
  These are documented alternatives, not evidence of a universal CUDA minimum.
  This first recipe selects dense SDPA and does not require building optional
  Sage/Sol kernels. Do not substitute CUDA wheels silently to make a host pass.

## Explicit startup interfaces

The launcher owns the listener, loopback authentication token, process lifetime,
journal, immutable input storage and Worker transport. These helpers do not
start anything merely by being imported:

1. `config_for_model_root(model_root)` produces a non-secret `wgp_config.json`.
   Write that exact filename in a protected configuration directory. Its single
   absolute `checkpoints_paths` entry is the same directory verified below.
   Do not add a `.` fallback, UI plugins, other accounts or saved GUI presets.
2. `verify_runtime(runtime_root, config_path, manifest_path, model_root)` checks
   source HEAD, tracked modifications, untracked executable source, core package
   versions and every model file's size/hash. It returns dated-run evidence for
   the caller to save and explicitly reports `inference_verified=False`.
   The source/config/weight tree must remain private and immutable after this
   check. A full dependency lock and container attestation remain separate gates.
3. `create_session(runtime_root, config_path, output_dir)` explicitly imports
   `shared.api.init(root=..., config_path=..., output_dir=..., cli_args=["--attention",
   "sdpa", "--profile", "4"], console_output=False, webui_state=None)`.
   This call initializes the runtime; it is not an offline test. The directory
   must already exist. Model loading/readiness does not qualify inference.
4. Wrap the facade in `WanGPHost` with durable receipts. Inputs are immutable
   `InputDescriptor` values. The host resolver replaces `image_start`/`image_end`
   opaque handles only with its own hash-verified private files. No client path,
   URL, arbitrary workflow or Python code is accepted.
5. Construct `H3FL2VACompiler(manifest, transport.stage_input)`. Its callback
   receives `(descriptor, source_stream, heartbeat=...)`, must verify both digest
   and size, and return the same descriptor. The worker retains the same job ID,
   request hash, attempt tag, manifest binding and source asset ownership.

Upstream `Session.close()` waits for its generation lock and unloads the model;
it is not a cancellation acknowledgement. Do not release host ownership while
a live GPU thread can still execute. Host/process replacement must preserve and
quarantine nonterminal receipts rather than infer the old attempt stopped.

## Controls and exact mapping

The public schema comes from `wangp_compiler.control_schema()`, and the machine
envelope is also recorded in `manifest.json`. New requests are normalized before
admission; old Comfy snapshots are never rewritten. A rejected control produces
a stable error before staging/submission.

| Platform intent | Upstream setting / behavior | First recipe |
| --- | --- | --- |
| BF16 Base | `model_type=minimax_h3_fl2va`, config `bf16,bf16`, startup transformer/text quantization `bf16` | Fixed; no pruned, turbo, INT8, FP8, LoRA or VDN substitution |
| Steps | `num_inference_steps=50` | Fixed 50; handler's default 20 is explicitly overridden |
| Prompt | `prompt`, `multi_prompts_gen_type=FG`, enhancer disabled | One prompt including all lines; exactly one task/output |
| Seed | Decimal uint64 converted to Python integer | Exact value; public admission generates an omitted seed once |
| Resolution | Exact `WIDTHxHEIGHT` from the accepted native output spec | Existing 480P/576P/768P/custom area and aspect rules |
| Duration | Integer `video_length` on native `17*k+5` grid, `force_fps="24"` | 107–362 native frames; preserve both native and requested durations |
| First/last image | `image_start`, `image_end`; explicit `S`, `TE`, `SE` flags | Zero, one or two image assets; no references/video/audio inputs |
| Reference sizing | Startup `fit_canvas=2` | Explicit center crop to the requested canvas; source images cannot silently alter resolution |
| Sampling | `sample_solver=euler`, `flow_shift=12`, `guidance_phases=1` | Native scheduler only; audio sigma shift is fixed 3; guidance fixed 1 |
| Decode | Video tiled 256, overlap 64; native temporal clip length 17; audio normal | No normal video decode, adjustable temporal tiles or forced CPU encoder |
| Sound | Native joint generation; `_api.return_audio=True` | `generate_audio=True` only for this recipe; false is rejected, not ignored |
| Export | Native MP4 + exact generated audio tensor, then existing platform export | Raw H.264 CRF10/AAC; delivered H.264 CRF18 + separate FLAC |

### Why some familiar options are rejected

- Default quantization is INT8 in [wgp.py lines 2647–2675](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/wgp.py#L2647-L2675).
  The BF16 transformer choice is explicit in [model selection lines 3009–3071](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/wgp.py#L3009-L3071).
  The Qwen and video VAE configuration groups are [handler lines 669–689](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/models/minimax_h3/minimax_h3_handler.py#L669-L689).
  The video VAE menu calls its option BF16, but the actual selected file is FP16;
  this manifest records its real precision.
- [Handler lines 1204–1220](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/models/minimax_h3/minimax_h3_handler.py#L1204-L1220)
  defaults to 20 steps. The recipe overrides it. [Pipeline lines 743–775](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/models/minimax_h3/pipeline.py#L743-L775)
  consumes solver, steps, shift and frames, but does not consume the generic
  `guide_scale` keyword forwarded by the outer UI. Guidance is not advertised as
  an adjustable control. [Lines 1148–1151](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/models/minimax_h3/pipeline.py#L1148-L1151)
  show the independent fixed audio shift of 3.
- [Pipeline lines 499–500](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/models/minimax_h3/pipeline.py#L499-L500)
  ignores the requested VAE tile size and always enables 256-pixel tiles.
  [Video autoencoder lines 691, 748–779](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/models/minimax_h3/components/video_autoencoder.py#L691-L779)
  sets clip length 17 and overlap 64. Passing Comfy's temporal tile controls would
  therefore be misleading; they are rejected rather than discarded.
- Resolution follows input images by default in [wgp.py lines 7380–7390 and
  7739–7767](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/wgp.py#L7739-L7767).
  Explicit crop-to-canvas configuration preserves the accepted resolution.
- The pinned [video codec table](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/shared/utils/video_codecs.py#L46-L77)
  maps `libx264_8` to CRF10; `libx264_18` is not an actual selectable CRF18 codec.
  The existing platform worker, not a guessed upstream option, applies the
  requested final export CRF. Native frames are retained in the raw receipt;
  the platform's existing explicit requested-duration export can shorten delivery.

## Result and cancellation semantics

`SessionJob.cancel()` only sets cancellation intent. `done`/`result()` are the
completion evidence ([shared/api.py lines 511–544](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/shared/api.py#L511-L544)).
The headless runner normally completes its generation worker, clears `active_job`,
then sets the result in [shared/api_cli.py](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/shared/api_cli.py).
Its outer exception path can publish a result before that daemon worker stops.
The facade therefore also checks the pinned `wangp-session-worker` thread and
requires successful CUDA synchronization before reporting terminal stop proof.
Acknowledgement, timeout and an empty queue are insufficient. A runtime exception
is sanitized; partial files are not success.

Success requires one successful task, no error, exactly one MP4, and the matching
audio artifact. H3 returns sample-major stereo float32 NumPy samples at 32kHz
([pipeline lines 1695–1721](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/models/minimax_h3/pipeline.py#L1695-L1721)).
`_api.return_audio=True` retains those samples in `GeneratedArtifact`
([shared/api.py lines 319–339](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/shared/api.py#L319-L339);
[wgp.py lines 8671–8678](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/wgp.py#L8671-L8678)).
The facade writes IEEE float32 WAV without reconstructing it from the lossy AAC
track, preserves the native MP4, and hands both to the host for checksum sealing.
Failure during packaging retries the same completed result; it never regenerates.
A crash before durable sealing remains unknown for reconciliation, not safe retry.

The current worker exports the requested integer duration (for example 120 frames
for 5 seconds), while the native H3 sample can have 124 frames. A last-image
condition applies to that native ending and may be cut from the delivered export.
The mapping tests do not prove the last frame remains visible in the delivered
video. B3 should first prove text-to-video; accepting last-frame fidelity requires
an explicit export/endpoint contract and a real last-frame test.

## Checks and remaining gates

`test_platform_wangp_compiler.py` and `test_platform_wangp_session.py` use isolated
fake Session objects/files only. They cover control rejection, immutable identities,
role mapping, uint64 seeds, cancellation intent, both outputs, collection retry,
lookup-root binding and escaped output rejection. They do not import WanGP/torch,
download weights or assert real video/audio quality. Real media validation is the
Worker's existing responsibility.

Before enabling this recipe publicly: verify the installed runtime and files,
qualify a real accepted job through the complete API-to-download path, record the
actual native/delivered media specs and audio stream, then reconcile cost/lease
evidence. Adding further controls or variants requires a distinct declared and
tested recipe; it must not weaken the immutable manifest binding of existing jobs.
