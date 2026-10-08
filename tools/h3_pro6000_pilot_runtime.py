"""Isolated unpruned H3 BF16/INT8 comparison on one visible PRO 6000 Blackwell.

Import and `assets --arm ...` are metadata-only. No torch, network, installation,
provider or inference work runs on import. Reuses a PRIVATE module instance of
the reviewed 5090 receipt/collection helpers; never edits or globally patches it.

Both arms use profile 3, BF16 Qwen, FP16 video VAE, FP32 audio VAE and SDPA.
BF16 v2 uses the upstream interleaved-QKV split path. INT8 keeps its validated
grouped-ConvRot Lower RAM path and original v1 receipt identity. These are working
deployment recipes, not a precision-only performance experiment. At pinned
WanGP 0e58385, wgp.py:3940-3976 gives profile 3 an 80% VRAM
per-model budget; mmgp/offload.py:5116-5220 pins only the transformer. This allows
a fitting DiT to remain resident during denoising while other stages are offloaded.
It does not promise that every reference envelope fits. No profile fallback.

Two arms may share immutable model files but MUST use separate run roots and
CUDA_VISIBLE_DEVICES. A physical GPU UUID lock prevents two owned workers using
the same GPU. Shared host RAM remains subject to the same explicit free-RAM gate;
if unavailable, stage arms sequentially instead of silently lowering the gate.
Downloader contract: assets_for(arm) -> (path, bytes, digest) tuples;
asset_manifest(arm) -> public metadata. There is deliberately no default arm.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import re
import sys
import time

REVISION = "0e58385fbde7ff102d276e4a9e490845de76b4ea"
MODEL_REVISION = "adc81ccb71352192214d83d5fafb9487e860be39"
MODEL_TYPES = {"fl": "minimax_h3_fl2va", "ref": "minimax_h3_ref2va"}
PROFILE = 3
# Original BF16 weights are head-interleaved. At pinned transformer.py:289-296,
# the unsplit path expects grouped Q/K/V rows, as used by ConvRot INT8. Selecting
# lower_ram for plain BF16 bypasses the interleaved-aware loader split and corrupts
# attention without necessarily raising an exception. Never reuse its v1 recipe.
ARM_CONFIGS = {"bf16": "bf16,bf16", "int8": "bf16,bf16,lower_ram"}
MIN_AVAILABLE_RAM_GIB = 128
MIN_FREE_GPU_GIB = 88
MODEL_FILES = {
    "bf16": (
        ("MiniMax-H3-FL2VA_bf16.safetensors", 66280486944, "f10704606e376815298cb6bd9d94ea721346b62cd840177cd3723971edd83367"),
        ("MiniMax-H3-Ref2VA_bf16.safetensors", 66280486944, "ca877ed2b1bf72cfda76fe38117832544d6c0530abcfb9463e114cd767a39516"),
    ),
    "int8": (
        ("MiniMax-H3-FL2VA_int8_convrot.safetensors", 34038903007, "83a36b67776962f44087f2f7c12d95791393f3cce1ef898efc90405216e7b0c0"),
        ("MiniMax-H3-Ref2VA_int8_convrot.safetensors", 34038903008, "d9ba725b2920bb5ffb4ab8e2514f77264df59a5f05513613513f6fb5d9d6f749"),
    ),
}
SHARED_ASSETS = (
    ("Qwen3-VL-32B-Instruct/Qwen3-VL-32B-Instruct-layer50_bf16.safetensors", 51506305568, "a91ca0cc41bead2e62d8fc044a3ffdbd9487daa7c8997f697049990c7178e0a6"),
    ("MiniMax-H3-video_vae_fp16.safetensors", 5207806512, "455010492bb59a9cc7b8f1ee23905b22a10079f89490adbf820a1728efcaea6b"),
    ("MiniMax-H3-audio_vae_fp32.safetensors", 605429308, "37dddc2f3e6d5d5139d823d5ea283bbf304dadcb885b1ccda818aa13dade5ea2"),
    ("minimax_h3/minimax_h3_latent_upscaler_3d_bf16.safetensors", 690592992, "4f57821f5837f32f7142b67d815606dbd7550f194e5c769f7d6c3f83b146a5e6"),
    ("Qwen3-VL-32B-Instruct/config.json", 1474, "29e925092dee9f14278c53e7e7d876cea8a997bc"),
    ("Qwen3-VL-32B-Instruct/preprocessor_config.json", 390, "2ea84a437d448ff71b08df68fdd949d5cc4ebb64"),
    ("Qwen3-VL-32B-Instruct/tokenizer.json", 7032403, "c6cc1014128b19d1fc46b1d30a23e3b1d35db421"),
    ("Qwen3-VL-32B-Instruct/tokenizer_config.json", 11004, "5e463a18949779b8a38a03f0a6d9089094970eed"),
    ("Qwen3-VL-32B-Instruct/vocab.json", 2776833, "4783fe10ac3adce15ac8f358ef5462739852c569"),
)


def checked_arm(arm):
    if arm not in MODEL_FILES:
        raise ValueError("explicit_bf16_or_int8_arm_required")
    return arm


def recipe_id(arm):
    checked_arm(arm)
    suffix = "splitqkv-v2" if arm == "bf16" else "lowram-v1"
    return f"h3-unpruned33b-{arm}-qwenbf16-vaefp16-sdpa-p3-{suffix}"


def config_for(arm):
    return ARM_CONFIGS[checked_arm(arm)]


def assets_for(arm):
    return list(MODEL_FILES[checked_arm(arm)] + SHARED_ASSETS)


def asset_manifest(arm):
    return {"repository": "DeepBeepMeep/MiniMax-H3", "revision": MODEL_REVISION,
            "source_revision": REVISION, "recipe_id": recipe_id(arm), "arm": arm,
            "files": [{"path": name, "size_bytes": size,
                       "sha256" if len(digest) == 64 else "git_blob_sha1": digest}
                      for name, size, digest in assets_for(arm)]}


def validate_device(torch):
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise ValueError("exactly_one_visible_GPU_required")
    name = torch.cuda.get_device_name(0)
    properties = torch.cuda.get_device_properties(0)
    if (not re.search(r"RTX\s+PRO\s+6000\s+Blackwell", name, re.I)
            or torch.cuda.get_device_capability(0) != (12, 0)
            or not 90 * 1024**3 <= properties.total_memory <= 100 * 1024**3):
        raise ValueError("full_RTX_PRO_6000_Blackwell_96GB_required")
    uuid = str(getattr(properties, "uuid", ""))
    if not re.fullmatch(r"(?:GPU-)?[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", uuid):
        raise ValueError("visible_GPU_UUID_required_for_device_lock")
    return name, properties.total_memory, uuid if uuid.startswith("GPU-") else "GPU-" + uuid


class ProPilot:
    """Per-arm adapter, with isolated helper globals for receipt identity."""

    def __init__(self, arm):
        self.arm = checked_arm(arm)
        path = Path(__file__).with_name("h3_5090_pilot_runtime.py")
        spec = importlib.util.spec_from_file_location(f"_pro6000_helpers_{arm}_{id(self)}", path)
        self.base = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.base)
        self.base.MODEL_TYPES = dict(MODEL_TYPES)
        self.base.ASSETS = assets_for(arm)
        self.base.RECIPE = recipe_id(arm)
        self.base.asset_manifest = lambda: asset_manifest(self.arm)
        inherited_settings = self.base.settings_for

        def settings(task):
            value = inherited_settings(task)
            value.update(config=config_for(self.arm), override_profile=PROFILE)
            return value

        self.base.settings_for = settings

    def runtime_config(self, models):
        config = self.base.runtime_config(models)
        config.update(transformer_quantization=self.arm, text_encoder_quantization="bf16",
                      video_profile=PROFILE, profile=PROFILE,
                      int8_kernels="kitchen" if self.arm == "int8" else "disabled")
        return config

    def environment(self, root):
        b = self.base
        root = root.resolve(strict=True)
        b.assert_no_ui(root)  # Does not reject a sibling PRO arm on another GPU.
        source = b.source_identity(root)
        sys.path.insert(0, str(root))
        mmgp = importlib.import_module("mmgp")
        if not getattr(mmgp, "__file__", None) or not Path(mmgp.__file__).resolve().is_relative_to(root):
            raise ValueError("vendored_mmgp_import_collision")
        for name in ("torch", "torchvision", "torchaudio", "diffusers", "transformers",
                     "optimum.quanto", "comfy_kitchen", "triton", "soundfile"):
            importlib.import_module(name)
        torch = importlib.import_module("torch")
        name, memory, uuid = validate_device(torch)
        versions = dict(sorted((d.metadata["Name"].lower(), d.version)
                               for d in importlib.metadata.distributions() if d.metadata["Name"]))
        return {"source": source, "python": platform.python_version(), "packages": versions,
                "gpu": name, "gpu_memory_bytes": memory, "gpu_uuid": uuid,
                "cuda": torch.version.cuda,
                "driver": b.command(["nvidia-smi", "--id=" + uuid, "--query-gpu=driver_version", "--format=csv,noheader"]),
                "ffmpeg": b.command(["ffmpeg", "-version"]).splitlines()[0],
                "ffprobe": b.command(["ffprobe", "-version"]).splitlines()[0]}

    def resource_admission(self, torch, enforce=True):
        data = self.base.resource_admission(torch, enforce=False)
        data.update(required_available_ram_gib=MIN_AVAILABLE_RAM_GIB,
                    required_free_gpu_gib=MIN_FREE_GPU_GIB,
                    shared_host_policy="defer_or_run_arms_sequentially_without_lowering_gate")
        if enforce and (data["effective_available_ram_bytes"] < MIN_AVAILABLE_RAM_GIB * 1024**3
                        or data["gpu_free_bytes"] < MIN_FREE_GPU_GIB * 1024**3):
            raise ValueError("pro6000_memory_headroom_insufficient:" + self.base.canonical(data))
        return data

    def verify_input_media(self, task):
        self.base.verify_input_media(task)
        record = task.get("inputs", {}).get("video")
        if record is None:
            return
        # This comparison qualifies the aligned 56-frame fixture, not arbitrary
        # references. Count decoded frames; container duration alone admitted the
        # old 48-frame fixture which upstream silently shortened to 39 frames.
        probe = json.loads(self.base.command([
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=nb_read_frames", "-of", "json", record["path"]]))
        streams = probe.get("streams", [])
        value = streams[0].get("nb_read_frames") if len(streams) == 1 else None
        if not isinstance(value, (str, int)) or str(value) != "56" or (int(value) - 5) % 17:
            raise ValueError("comparison_reference_video_requires_56_decoded_frames_17n_plus_5")

    def audit_definitions(self, session, models=None, modes=None):
        module = session._ensure_runtime().module
        if module.transformer_quantization != self.arm or module.text_encoder_quantization != "bf16":
            raise ValueError("comparison_quantization_changed")
        observed = {}
        for mode, model_type in MODEL_TYPES.items():
            definition = session.get_model_def(model_type)
            if definition is None or definition.get("architecture") != model_type:
                raise ValueError("unpruned_model_architecture_mismatch")
            groups = module.get_model_config_groups(model_type, definition)
            effective = definition.copy()
            for _, _, values in module.model_config_groups.selected_model_configs(groups, config_for(self.arm)):
                effective.update(values)
            wanted = MODEL_FILES[self.arm][0 if mode == "fl" else 1][0]
            selected = module.get_model_filename(model_type, quantization=self.arm,
                                                 dtype_policy="bf16", model_def=effective)
            encoder = module.get_model_filename(model_type, quantization="bf16", dtype_policy="bf16",
                                                URLs=effective.get("text_encoder_URLs", []))
            if not str(selected).endswith("/" + wanted):
                raise ValueError("unpruned_transformer_selection_mismatch")
            if not str(encoder).endswith("/" + SHARED_ASSETS[0][0]):
                raise ValueError("BF16_encoder_selection_mismatch")
            if effective.get("video_vae_file") != SHARED_ASSETS[1][0]:
                raise ValueError("FP16_video_VAE_selection_mismatch")
            if effective.get("audio_vae_file", SHARED_ASSETS[2][0]) != SHARED_ASSETS[2][0]:
                raise ValueError("FP32_audio_VAE_selection_mismatch")
            if (effective.get("qkv_splitting") is not (self.arm == "bf16")
                    or any(effective.get(k) for k in ("pdd", "vdn", "auto_quantize"))):
                raise ValueError("comparison_model_options_changed")
            observed[mode] = {"model_type": model_type, "transformer": wanted,
                              "text_encoder": SHARED_ASSETS[0][0],
                              "video_vae": effective["video_vae_file"], "qkv_splitting": self.arm == "bf16"}
            if models is not None and mode in (set(MODEL_TYPES) if modes is None else set(modes)):
                # Attest the files the real loader will resolve, not another cache
                # with the same basename. Prior verify_assets fully hashed these.
                resolved = {
                    wanted: module.fl.get_local_model_filename(selected),
                    SHARED_ASSETS[0][0]: module.fl.get_local_model_filename(
                        encoder, extra_paths="Qwen3-VL-32B-Instruct"),
                }
                for name, _, _ in SHARED_ASSETS[1:]:
                    resolved[name] = module.fl.locate_file(name)
                for name, selected_path in resolved.items():
                    if selected_path is None or Path(selected_path).resolve(strict=True) != (models / name).resolve(strict=True):
                        raise ValueError("effective_asset_path_mismatch:" + name)
                observed[mode]["resolved_asset_paths"] = {k: str(Path(v).resolve()) for k, v in resolved.items()}
        return observed

    def runtime_audit(self, module, requested, *, loaded=False):
        result = self.base.runtime_audit(module, requested)
        # Upstream can silently resolve Kitchen to Triton/PyTorch if its probe
        # fails. The requested config string alone does not attest the backend.
        expected_backend = "kitchen" if self.arm == "int8" else "pytorch"
        effective_backend = getattr(getattr(module, "int8_backend", None), "_backend", None)
        if effective_backend != expected_backend:
            raise ValueError("comparison_effective_kernel_backend_changed")
        result["effective_int8_backend"] = effective_backend
        result.update(task_override_profile=PROFILE, arm=self.arm, task_config=config_for(self.arm),
                      migration_explanation="BF16 v2 enables the interleaved-aware QKV split; INT8 retains the validated grouped-ConvRot v1 path. Original companion files and profile 3 remain fixed.",
                      memory_profile_policy="profile3: per-model80%VRAM budget, transformer-only RAM pinning; one active DiT")
        if module.default_profile_video != PROFILE:
            raise ValueError("comparison_default_profile_changed")
        if loaded:
            if module.loaded_profile != PROFILE or module.loaded_config != config_for(self.arm):
                raise ValueError("comparison_loaded_settings_changed")
            pipeline = module.wan_model
            checkpoint = pipeline.transformer.h3_checkpoint_info
            if checkpoint.get("compressed_modulation") is not False or checkpoint.get("time_embed_dim") != 2688:
                raise ValueError("loaded_transformer_is_not_unpruned")
            split = self.arm == "bf16"
            attention = pipeline.transformer.blocks[0].attn
            has_split = all(hasattr(attention, name) for name in ("q_proj", "k_proj", "v_proj"))
            if (bool(pipeline.transformer.split_linear_modules_map) != split
                    or has_split != split or hasattr(attention, "qkv_proj") == split):
                raise ValueError("loaded_transformer_QKV_layout_mismatch")
            result["loaded_qkv_path"] = "interleaved_split" if split else "grouped_convrot_fused"
            result["loaded_checkpoint"] = {"compressed_modulation": False, "time_embed_dim": 2688}
            result["loaded_model_type"] = module.transformer_type
            if module.transformer_type not in MODEL_TYPES.values():
                raise ValueError("loaded_model_type_changed")
        return result

    def run(self, args):
        b = self.base
        if not math.isfinite(args.deadline_epoch) or not time.time() < args.deadline_epoch <= time.time() + 86400:
            raise ValueError("finite_future_authorization_deadline_required")
        root, models, run_root = map(b.absolute_dir, (args.runtime_root, args.model_root, args.run_root))
        if run_root == models or run_root.is_relative_to(models) or models.is_relative_to(run_root):
            raise ValueError("separate_model_cache_and_arm_run_roots_required")
        import fcntl
        with (run_root / "pilot.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            expected = json.loads(Path(args.environment_lock).read_text())
            observed = self.environment(root)
            if observed != expected:
                raise ValueError("reviewed_environment_changed")
            # CUDA_VISIBLE_DEVICES maps ordinals; lock the actual physical UUID.
            gpu_lock = Path("/tmp") / ("sixnine-pro6000-" + observed["gpu_uuid"] + ".lock")
            with gpu_lock.open("a+") as device_lock:
                fcntl.flock(device_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._run_locked(args, root, models, run_root, observed, started)

    def _run_locked(self, args, root, models, run_root, observed, started):
        b = self.base
        torch = importlib.import_module("torch")
        b.write_json(run_root / "resource-admission.json", self.resource_admission(torch))
        tasks = json.loads(Path(args.tasks).read_text())["tasks"]
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 32:
            raise ValueError("bounded_task_list_required")
        for task in tasks:
            b.validate_task(task)
        if len({t["id"] for t in tasks}) != len(tasks):
            raise ValueError("duplicate_task_ids")
        pending = [t for t in tasks if not b.completed_or_refuse(run_root / "receipts" / (t["id"] + ".json"), b.task_digest(t))]
        if not pending:
            print(b.canonical({"state": "all_complete_verified", "arm": self.arm, "count": len(tasks)}))
            return
        for task in pending:
            self.verify_input_media(task)
        modes = sorted({task["mode"] for task in pending})
        verified_assets = b.verify_assets(models, modes=modes)  # Full hashes, no cache-name attestation.
        verified = time.monotonic()
        if time.time() >= args.deadline_epoch:
            raise ValueError("authorization_deadline_elapsed")
        config = self.runtime_config(models)
        config_path = run_root / "wgp_config.json"
        preparation = b.prepare_runtime_config(config_path, config)
        output = run_root / "outputs"
        output.mkdir(exist_ok=True, mode=0o700)
        os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
        sys.path.insert(0, str(root))
        api = importlib.import_module("shared.api")
        if not Path(api.__file__).resolve().is_relative_to(root):
            raise ValueError("upstream_import_collision")
        session = api.init(root=root, config_path=config_path, output_dir=output,
            cli_args=["--attention", "sdpa", "--profile", str(PROFILE), "--perc-reserved-mem-max", "0.2"],
            console_output=True, console_isatty=False, webui_state=None)
        definitions = self.audit_definitions(session, models=models, modes=modes)
        module = session._ensure_runtime().module
        b.write_json(run_root / "preflight.json", {"recipe": asset_manifest(self.arm), "environment": observed,
            "verified_modes": modes, "verified_asset_paths": verified_assets, "effective_definitions": definitions,
            "config_preparation": preparation, "runtime_audit": self.runtime_audit(module, config),
            "config": config, "verification_seconds": verified-started,
            "runtime_initialization_seconds": time.monotonic()-verified,
            "note": "Runtime import readiness is not inference success."})
        outcome = b.run_tasks(session, tasks, run_root, output, torch, args.deadline_epoch,
                              audit=lambda: self.runtime_audit(module, config, loaded=True))
        session.close()
        print(b.canonical({**outcome, "arm": self.arm, "total_seconds": time.monotonic()-started}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    assets = sub.add_parser("assets")
    assets.add_argument("--arm", choices=tuple(MODEL_FILES), required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--arm", choices=tuple(MODEL_FILES), required=True)
    inspect.add_argument("--runtime-root", required=True)
    inspect.add_argument("--output", required=True)
    execute = sub.add_parser("run")
    execute.add_argument("--arm", choices=tuple(MODEL_FILES), required=True)
    for key in ("runtime-root", "model-root", "run-root", "tasks", "environment-lock"):
        execute.add_argument("--" + key, required=True)
    execute.add_argument("--deadline-epoch", type=float, required=True)
    args = parser.parse_args()
    if args.action == "assets":
        print(json.dumps(asset_manifest(args.arm), indent=2))
        return
    pilot = ProPilot(args.arm)
    if args.action == "inspect":
        output = Path(args.output)
        if output.exists():
            raise ValueError("environment_receipt_already_exists")
        pilot.base.write_json(output, pilot.environment(pilot.base.absolute_dir(args.runtime_root)))
        pilot.base.write_json(output.with_suffix(".resources.json"), pilot.resource_admission(importlib.import_module("torch"), enforce=False))
    else:
        pilot.run(args)


if __name__ == "__main__":
    main()
