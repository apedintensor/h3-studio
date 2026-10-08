"""Explicit, isolated RTX 5090 / H3 Pruned INT8 pilot (not a production adapter).

No imports of torch/WanGP and no network, install, provider, or generation work
occur on import. `assets` prints metadata; `inspect` records the actual owned
runtime; `run` requires that reviewed environment and an absolute local cache.
Never replay a task with a non-complete receipt. Use a separately authorized
new task identity after diagnosing a failure. Completed artifacts are rehashed.
"""
from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import threading
import time

REVISION = "0e58385fbde7ff102d276e4a9e490845de76b4ea"
MODEL_REVISION = "adc81ccb71352192214d83d5fafb9487e860be39"
RECIPE = "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1"
MODEL_TYPES = {"fl": "minimax_h3_fl2va_pruned", "ref": "minimax_h3_ref2va_pruned"}
COLLECTION_RESERVE_SECONDS = 30
# Public HF tree metadata at MODEL_REVISION; no weights were fetched to author this file.
ASSETS = [
    ("MiniMax-H3-FL2VA-pruned_rank8_int8_convrot.safetensors", 21057674787, "30ff400f974b11a1ef13d216c5d9f6439a9c10322a3988b0374a39672ce286f0"),
    ("MiniMax-H3-Ref2VA-pruned_rank8_int8_convrot.safetensors", 21057674788, "e09db861c48d13560222948b4e38082bc21b1b7d4ddcaed9c865bf9c3233898c"),
    ("Qwen3-VL-32B-Instruct/Qwen3-VL-32B-Instruct-layer50_quanto_bf16_int8.safetensors", 26723791903, "4df8fc5237746b3b058745d6ec8fe1e54a9721bdc663d35d2d1806952672f301"),
    ("minimax_h3/minimax_h3_video_vae_int8_convrot.safetensors", 2811065184, "52a2c8c73583c86e4f41cdcce3a6ad0ea562987bc0bf3d60a0cef5f5c8e60c0e"),
    ("MiniMax-H3-audio_vae_fp32.safetensors", 605429308, "37dddc2f3e6d5d5139d823d5ea283bbf304dadcb885b1ccda818aa13dade5ea2"),
    ("minimax_h3/minimax_h3_latent_upscaler_3d_bf16.safetensors", 690592992, "4f57821f5837f32f7142b67d815606dbd7550f194e5c769f7d6c3f83b146a5e6"),
    ("Qwen3-VL-32B-Instruct/config.json", 1474, "29e925092dee9f14278c53e7e7d876cea8a997bc"),
    ("Qwen3-VL-32B-Instruct/preprocessor_config.json", 390, "2ea84a437d448ff71b08df68fdd949d5cc4ebb64"),
    ("Qwen3-VL-32B-Instruct/tokenizer.json", 7032403, "c6cc1014128b19d1fc46b1d30a23e3b1d35db421"),
    ("Qwen3-VL-32B-Instruct/tokenizer_config.json", 11004, "5e463a18949779b8a38a03f0a6d9089094970eed"),
    ("Qwen3-VL-32B-Instruct/vocab.json", 2776833, "4783fe10ac3adce15ac8f358ef5462739852c569"),
]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def absolute_dir(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("absolute_directory_required")
    return path.resolve(strict=True)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name == "posix":
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def command(args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def source_identity(root):
    revision = command(["git", "-C", str(root), "rev-parse", "HEAD"])
    if revision != REVISION:
        raise ValueError("upstream_revision_mismatch")
    changed = command(["git", "-C", str(root), "diff", "HEAD", "--name-only"]).splitlines()
    # Vast's published Dockerfile strips torch pins while installing requirements.
    # Executable upstream source remains immutable; record the actual requirements.
    if set(changed) - {"requirements.txt"}:
        raise ValueError("upstream_source_modified")
    extras = command(["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"]).splitlines()
    if any(Path(p).suffix in {".py", ".so", ".pyd"} for p in extras):
        raise ValueError("untracked_runtime_code")
    return {"revision": revision, "modified_install_metadata": changed,
            "requirements_sha256": sha256(root / "requirements.txt")}


def assert_no_ui(root):
    for proc in Path("/proc").glob("[0-9]*"):
        if proc.name == str(os.getpid()):
            continue
        try:
            args = (proc / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if any(value == b"wgp.py" or value.endswith(b"/wgp.py") for value in args):
            raise ValueError("wanGP_UI_running_stop_only_on_the_owned_pilot_node")
        if b"run" in args and any(value.endswith(b"h3_5090_pilot_runtime.py") for value in args):
            raise ValueError("another_pilot_runtime_is_active")


def environment(root):
    root = root.resolve(strict=True)
    assert_no_ui(root)
    source = source_identity(root)
    # MMGP is vendored in this reviewed WanGP checkout, not installed in site-packages.
    sys.path.insert(0, str(root))
    mmgp = importlib.import_module("mmgp")
    if not getattr(mmgp, "__file__", None) or not Path(mmgp.__file__).resolve().is_relative_to(root):
        raise ValueError("vendored_mmgp_import_collision")
    versions = dict(sorted((d.metadata["Name"].lower(), d.version)
                           for d in importlib.metadata.distributions() if d.metadata["Name"]))
    # Import probes are explicit here, not at module import or in `assets`.
    for module in ("torch", "torchvision", "torchaudio", "diffusers", "transformers",
                   "mmgp", "optimum.quanto", "comfy_kitchen", "triton", "soundfile"):
        importlib.import_module(module)
    torch = importlib.import_module("torch")
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise ValueError("exactly_one_visible_GPU_required")
    name = torch.cuda.get_device_name(0)
    if "5090" not in name or torch.cuda.get_device_capability(0) != (12, 0):
        raise ValueError("RTX_5090_required")
    return {"source": source, "python": platform.python_version(), "packages": versions,
            "gpu": name, "cuda": torch.version.cuda,
            "gpu_memory_bytes": torch.cuda.get_device_properties(0).total_memory,
            "driver": command(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"]),
            "gpu_uuid": command(["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"]),
            "ffmpeg": command(["ffmpeg", "-version"]).splitlines()[0],
            "ffprobe": command(["ffprobe", "-version"]).splitlines()[0]}


def asset_manifest():
    return {"repository": "DeepBeepMeep/MiniMax-H3", "revision": MODEL_REVISION,
            "source_revision": REVISION, "recipe_id": RECIPE,
            "files": [{"path": name, "size_bytes": size,
                       "sha256" if len(digest) == 64 else "git_blob_sha1": digest}
                      for name, size, digest in ASSETS]}


def verify_assets(root, modes=None):
    modes = set(MODEL_TYPES if modes is None else modes)
    if not modes or modes - MODEL_TYPES.keys():
        raise ValueError("asset_verification_modes_invalid")
    required = [ASSETS[index] for index, mode in enumerate(("fl", "ref")) if mode in modes]
    required.extend(ASSETS[2:])
    for name, size, digest in required:
        path = root / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size != size:
            raise ValueError("missing_or_wrong_size_asset:" + name)
        if not path.resolve().is_relative_to(root):
            raise ValueError("asset_path_escape")
        if len(digest) == 64:
            actual = sha256(path)
        else:
            actual = hashlib.sha1(b"blob " + str(size).encode() + b"\0" + path.read_bytes()).hexdigest()
        if actual != digest:
            raise ValueError("asset_hash_mismatch:" + name)
    return [name for name, _, _ in required]


def runtime_config(models):
    return {"transformer_quantization": "int8", "text_encoder_quantization": "int8",
            "transformer_dtype_policy": "bf16", "attention_mode": "sdpa",
            "video_profile": 4, "profile": 4, "vae_config": 3, "vae_precision": "16",
            "mixed_precision": "0", "compile": "", "boost": 1,
            "int8_kernels": "kitchen", "kernel_precision": "strict",
            "video_preload_mode": "default", "video_preload_in_VRAM": 0,
            "perc_reserved_mem_max": 20, "smart_memory_pinning": True,
            "read_ahead": False, "vram_allocator": "default", "preload_model_policy": [],
            "fit_canvas": 2, "video_output_codec": "libx264_8", "video_container": "mp4",
            # Pinned extension migration canonicalizes manual+0 to default and
            # enhancer availability 0 to 3. Every task still disables enhancement.
            "audio_output_codec": "aac_128", "enhancer_enabled": 3,
            "save_queue_if_crash": 0, "notification_sound_enabled": 0,
            "embed_source_images": False, "checkpoints_paths": [str(models)]}


def prepare_runtime_config(path, requested):
    added_defaults = []
    if path.exists():
        existing = json.loads(path.read_text())
        if not isinstance(existing, dict):
            raise ValueError("pilot_runtime_config_changed")
        changed = [key for key, value in requested.items()
                   if key not in existing or canonical(existing[key]) != canonical(value)]
        if changed:
            raise ValueError("pilot_runtime_config_changed:" + ",".join(sorted(changed)))
        added_defaults = sorted(existing.keys() - requested.keys())
    # Pinned WanGP persists defaults and last-selection fields into this file.
    # Recreate the exact authored inputs so those extras cannot influence a new process.
    write_json(path, requested)
    return {"requested_keys_preserved": sorted(requested), "upstream_added_keys_reset": added_defaults}


def runtime_audit(module, requested):
    effective = {key: module.server_config.get(key) for key in requested}
    changed = [key for key in requested if canonical(requested[key]) != canonical(effective[key])]
    if changed:
        raise ValueError("effective_pilot_config_changed:" + ",".join(sorted(changed)))
    return {"requested_config": requested, "effective_config": effective,
            "extensions_defaults_version": module.server_config.get("extensions_defaults_version"),
            "default_video_profile": module.default_profile_video,
            "loaded_profile": module.loaded_profile,
            "video_preload_mode": module.preload_mode("video"),
            "video_preload_in_VRAM": module.server_config.get("video_preload_in_VRAM"),
            "task_override_profile": 4, "task_prompt_enhancer": "",
            "migration_explanation": "At pinned 0e58385, extension migration maps manual preload with zero budget to default (same init_pipe budgets), and enhancer availability 0 to 3. Task prompt_enhancer remains empty, so enhancement is not requested. Canonical inputs now match the first pilot's effective settings; inference recipe identity is unchanged."}


def validate_task(task):
    allowed = {"id", "mode", "steps", "seed", "prompt", "resolution", "frames", "inputs", "timeout_seconds"}
    if not isinstance(task, dict) or set(task) - allowed:
        raise ValueError("invalid_task_fields")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task.get("id", "")):
        raise ValueError("invalid_task_id")
    if task.get("mode") not in MODEL_TYPES or type(task.get("steps")) is not int or task["steps"] not in (20, 50):
        raise ValueError("explicit_mode_and_20_or_50_steps_required")
    if type(task.get("seed")) is not int or not 0 <= task["seed"] <= 0xffffffffffffffff:
        raise ValueError("invalid_seed")
    if not isinstance(task.get("prompt"), str) or not task["prompt"].strip() or len(task["prompt"]) > 12000:
        raise ValueError("prompt_required")
    if (task.get("resolution") not in {"832x480", "1344x768"}
            or type(task.get("frames")) is not int or task["frames"] != 124):
        raise ValueError("pilot_requires_explicit_124_frames_and_480p_or_768p")
    timeout = task.get("timeout_seconds", 900)
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError("invalid_timeout")
    inputs = task.get("inputs", {})
    roles = {"first_frame", "last_frame"} if task["mode"] == "fl" else {"image", "video", "audio"}
    if not isinstance(inputs, dict) or set(inputs) - roles:
        raise ValueError("unsupported_input_roles")
    if task["mode"] == "ref" and (not inputs or ("audio" in inputs and not set(inputs) & {"image", "video"})):
        raise ValueError("reference_visual_required")
    for asset in inputs.values():
        if (not isinstance(asset, dict) or set(asset) != {"path", "sha256"}
                or not Path(asset["path"]).is_absolute()
                or not re.fullmatch(r"[a-f0-9]{64}", asset["sha256"])):
            raise ValueError("absolute_hash_bound_input_required")
    return task


def settings_for(task):
    validate_task(task)
    files = {role: value["path"] for role, value in task.get("inputs", {}).items()}
    settings = {"model_type": MODEL_TYPES[task["mode"]], "config": "int8,int8_convrot,lower_ram",
        "image_mode": 0, "prompt": task["prompt"], "negative_prompt": "", "alt_prompt": "",
        "resolution": task["resolution"], "video_length": task["frames"], "force_fps": "24",
        "num_inference_steps": task["steps"], "seed": task["seed"], "guidance_scale": 1.0,
        "guidance_phases": 1, "flow_shift": 12.0, "sample_solver": "euler", "denoising_strength": 1.0,
        "image_prompt_type": ("S" if "first_frame" in files else "T") + ("E" if "last_frame" in files else ""),
        "image_start": files.get("first_frame"), "image_end": files.get("last_frame"),
        "video_prompt_type": ("I" if "image" in files else "") + ("V-U" if "video" in files else ""),
        "image_refs": [files["image"]] if "image" in files else None,
        "video_guide": files.get("video"), "audio_guide": files.get("audio"),
        "audio_prompt_type": "A" if "audio" in files else "", "image_refs_relative_size": 100,
        "remove_background_images_ref": 0, "video_source": None, "audio_source": None,
        "video_guide2": None, "video_guide3": None, "audio_guide2": None, "audio_guide3": None,
        "repeat_generation": 1, "batch_size": 1, "multi_prompts_gen_type": "FG",
        "multi_images_gen_type": 0, "prompt_enhancer": "", "activated_loras": [],
        "skip_steps_cache_type": "", "override_attention": "sdpa", "override_profile": 4,
        "guidance2_scale": 1.0, "guidance3_scale": 1.0,
        "sliding_window_size": 362, "sliding_window_overlap": 18,
        "sliding_window_discard_last_frames": 0, "sliding_window_trim_first_frames": 0,
        "temporal_upsampling": "", "spatial_upsampling": "", "postprocess_audio": "",
        "custom_settings": {"audio_refinement": "none"}, "output_filename": task["id"],
        "_api": {"return_audio": True, "return_video_uint8": False, "return_side_files": False}}
    return settings


def task_digest(task):
    return hashlib.sha256(canonical({"recipe": RECIPE, "models": asset_manifest(), "task": task}).encode()).hexdigest()


def verify_input_media(task):
    for role, record in task.get("inputs", {}).items():
        path = Path(record["path"])
        if not path.is_file() or sha256(path) != record["sha256"]:
            raise ValueError("input_hash_mismatch")
        probe = json.loads(command(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]))
        streams = probe["streams"]
        if role == "audio":
            audios = [s for s in streams if s["codec_type"] == "audio"]
            if (len(audios) != 1 or int(audios[0]["sample_rate"]) != 32000
                    or audios[0]["channels"] != 2
                    or not 2 <= float(probe["format"]["duration"]) <= 5.2):
                raise ValueError("pilot_reference_audio_must_be_2_to_5_2_seconds_32k_stereo")
        else:
            visuals = [s for s in streams if s["codec_type"] == "video"]
            if len(visuals) != 1:
                raise ValueError("one_visual_stream_required")
            visual = visuals[0]
            if task["mode"] == "ref" and visual["width"] * visual["height"] > 832 * 480:
                raise ValueError("reference_pixels_exceed_qualified_input_budget")
            if role == "video" and (any(s["codec_type"] == "audio" for s in streams)
                    or not 2 <= float(probe["format"]["duration"]) <= 3.1
                    or visual["avg_frame_rate"] != "24/1"):
                raise ValueError("reference_video_must_be_silent_24fps_2_to_3_seconds")


def completed_or_refuse(receipt_path, identity):
    if not receipt_path.exists():
        return False
    previous = json.loads(receipt_path.read_text())
    if previous.get("task_digest") != identity:
        raise ValueError("task_identity_changed")
    if previous.get("state") != "complete":
        raise ValueError("prior_task_requires_reconciliation:" + previous.get("state", "unknown"))
    if not previous.get("outputs"):
        raise ValueError("completed_task_missing_outputs")
    for item in previous["outputs"]:
        path = Path(item["path"])
        if not path.is_file() or path.stat().st_size != item["size_bytes"] or sha256(path) != item["sha256"]:
            raise ValueError("completed_output_changed")
    return True


def memory_snapshot(torch=None):
    values = {}
    status = Path("/proc/self/status")
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                key, amount, _ = line.split()
                values[key[:-1] + "_bytes"] = int(amount) * 1024
    for key in ("memory.current", "memory.peak", "memory.max"):
        path = Path("/sys/fs/cgroup") / key
        if path.exists():
            value = path.read_text().strip()
            values["cgroup_" + key] = int(value) if value.isdigit() else value
    if torch is not None:
        for name in ("memory_allocated", "memory_reserved", "max_memory_allocated", "max_memory_reserved"):
            values["cuda_" + name] = getattr(torch.cuda, name)(0)
    return values


def cgroup_ram_headroom(host_available, limit, usage, fields):
    # File includes tmpfs/shmem; neither that nor dirty/writeback/pinned pages is
    # immediately reclaimable model cache. The LRU bound includes active clean files.
    file_bytes = max(0, fields.get("file", 0) - fields.get("shmem", 0))
    lru_file = max(0, fields.get("active_file", 0) + fields.get("inactive_file", 0))
    exclusions = sum(max(0, fields.get(key, 0)) for key in ("file_dirty", "file_writeback", "unevictable"))
    reclaimable = max(0, min(file_bytes, lru_file) - exclusions)
    available = host_available
    if isinstance(limit, int) and isinstance(usage, int):
        available = min(host_available, limit, max(0, limit - usage + reclaimable))
    return available, reclaimable


def resource_admission(torch, enforce=True):
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value, *_ = line.split()
        info[key.rstrip(":")] = int(value) * 1024
    snapshot = memory_snapshot(torch)
    limit = snapshot.get("cgroup_memory.max")
    usage = snapshot.get("cgroup_memory.current")
    stat = Path("/sys/fs/cgroup/memory.stat")
    fields = {}
    if stat.exists():
        fields = {key: int(value) for key, value in (line.split() for line in stat.read_text().splitlines())}
    available, reclaimable = cgroup_ram_headroom(info["MemAvailable"], limit, usage, fields)
    free_gpu, total_gpu = torch.cuda.mem_get_info(0)
    data = {"effective_available_ram_bytes": available, "host_mem_available_bytes": info["MemAvailable"],
            "cgroup_reclaimable_clean_file_bytes": reclaimable, "cgroup_memory_stat": fields,
            "gpu_free_bytes": free_gpu, "gpu_total_bytes": total_gpu, "memory": snapshot,
            "required_available_ram_gib": 96, "required_free_gpu_gib": 28}
    if enforce and (available < 96 * 1024**3 or free_gpu < 28 * 1024**3):
        raise ValueError("pilot_memory_headroom_below_96GiB_RAM_or_28GiB_VRAM:" + canonical(data))
    return data


def check_output_duration(value, task, label):
    try:
        duration = float(value)
    except (TypeError, ValueError):
        raise ValueError("output_duration_invalid:" + label) from None
    # One video frame, plus one millisecond of container timestamp rounding.
    if not math.isfinite(duration) or abs(duration - task["frames"] / 24) > 1 / 24 + 0.001:
        raise ValueError("output_duration_mismatch:" + label)


def validate_output_probe(probe, task):
    vstreams = [s for s in probe["streams"] if s["codec_type"] == "video"]
    astreams = [s for s in probe["streams"] if s["codec_type"] == "audio"]
    width, height = map(int, task["resolution"].split("x"))
    if (len(vstreams) != 1 or len(astreams) != 1 or vstreams[0]["width"] != width
            or vstreams[0]["height"] != height or int(vstreams[0].get("nb_frames", 0)) != task["frames"]):
        raise ValueError("output_media_shape_mismatch")
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            fps = Fraction(vstreams[0].get(key, "0/1"))
        except (TypeError, ValueError, ZeroDivisionError):
            raise ValueError("output_frame_rate_invalid:" + key) from None
        if fps != 24:
            raise ValueError("output_frame_rate_mismatch:" + key)
    audio = astreams[0]
    if int(audio.get("sample_rate", 0)) != 32000 or audio.get("channels") != 2:
        raise ValueError("output_audio_must_be_32k_stereo")
    check_output_duration(vstreams[0].get("duration"), task, "video")
    check_output_duration(audio.get("duration"), task, "audio")
    check_output_duration(probe.get("format", {}).get("duration"), task, "container")


def output_records(result, output_dir, task):
    if (not result.success or result.cancelled or result.errors or result.total_tasks != 1
            or result.successful_tasks != 1 or result.failed_tasks != 0 or len(result.generated_files) != 1):
        raise ValueError("generation_failed_or_result_shape_invalid")
    video = Path(result.generated_files[0]).resolve(strict=True)
    if not video.is_relative_to(output_dir) or video.suffix.lower() != ".mp4":
        raise ValueError("unexpected_output_path")
    matches = [a for a in result.artifacts if a.media_type == "video" and a.path
               and Path(a.path).resolve() == video and a.audio_tensor is not None]
    if len(matches) != 1 or matches[0].audio_sampling_rate != 32000:
        raise ValueError("independent_generated_audio_missing")
    import numpy as np
    import soundfile
    audio = np.asarray(matches[0].audio_tensor, dtype=np.float32)
    if audio.ndim != 2 or audio.shape[1] != 2 or not audio.shape[0] or not np.isfinite(audio).all():
        raise ValueError("invalid_generated_audio")
    check_output_duration(audio.shape[0] / 32000, task, "independent_audio_samples")
    wav = video.with_name(video.stem + "-generated.wav")
    soundfile.write(str(wav), audio, 32000, format="WAV", subtype="FLOAT")
    wav_info = soundfile.info(str(wav))
    if wav_info.samplerate != 32000 or wav_info.channels != 2 or wav_info.frames != audio.shape[0]:
        raise ValueError("independent_WAV_shape_mismatch")
    check_output_duration(wav_info.duration, task, "independent_WAV")
    probe = json.loads(command(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video)]))
    validate_output_probe(probe, task)
    command(["ffmpeg", "-v", "error", "-xerror", "-i", str(video), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"])
    records = []
    for kind, path in (("video", video), ("audio", wav)):
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())
        records.append({"kind": kind, "path": str(path), "size_bytes": path.stat().st_size,
                        "sha256": sha256(path)})
    if os.name == "posix":
        fd = os.open(video.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return records, probe


class PhaseRecorder:
    def __init__(self):
        self.started = time.monotonic()
        self.phases = []
        self.last_phase = None
        self.step = None
        self.lock = threading.Lock()

    def on_progress(self, progress):
        phase = str(getattr(progress, "phase", "unknown"))
        with self.lock:
            self.step = getattr(progress, "current_step", None)
            if phase != self.last_phase:
                self.phases.append({"phase": phase, "elapsed_seconds": time.monotonic()-self.started})
                self.last_phase = phase

    def snapshot(self):
        with self.lock:
            return {"phase": self.last_phase, "current_step": self.step, "phase_transitions": list(self.phases)}


def defer_for_deadline(task, run_root, deadline):
    now = time.time()
    required = task.get("timeout_seconds", 900) + COLLECTION_RESERVE_SECONDS
    if deadline - now >= required:
        return None
    state = {"state": "deferred_unstarted", "reason": "insufficient_deadline_window",
             "task_id": task["id"], "task_digest": task_digest(task), "observed_epoch": now,
             "deadline_epoch": deadline, "remaining_seconds": deadline - now,
             "required_seconds": required, "collection_reserve_seconds": COLLECTION_RESERVE_SECONDS}
    # A deferral is not a submission receipt; this task remains safe to start later.
    write_json(run_root / "deferrals" / (task["id"] + ".json"), state)
    return state


def run_tasks(session, tasks, run_root, output_dir, torch, deadline, collect=output_records, audit=None):
    previous_mode = None
    for task in tasks:
        identity = task_digest(task)
        receipt = run_root / "receipts" / (task["id"] + ".json")
        if completed_or_refuse(receipt, identity):
            continue
        deferred = defer_for_deadline(task, run_root, deadline)
        if deferred:
            return deferred
        for item in task.get("inputs", {}).values():
            if sha256(item["path"]) != item["sha256"]:
                raise ValueError("input_hash_mismatch")
        # One process, one generation. Explicitly release between FL/REF, reusing files.
        if previous_mode is not None and previous_mode != task["mode"]:
            session.close()
            torch.cuda.synchronize()
        settings = session.get_default_settings(MODEL_TYPES[task["mode"]])
        settings.update(settings_for(task))
        write_json(run_root / "settings" / (task["id"] + ".json"), settings)
        state = {"task_digest": identity, "recipe": RECIPE, "task_id": task["id"],
                 "mode": task["mode"], "steps": task["steps"], "state": "dispatch_intent",
                 "started_epoch": time.time(), "memory_before": memory_snapshot(torch)}
        deferred = defer_for_deadline(task, run_root, deadline)
        if deferred:
            return deferred
        write_json(receipt, state)  # Durable BEFORE calling the upstream submission.
        started = time.monotonic()
        phases = PhaseRecorder()
        try:
            torch.cuda.reset_peak_memory_stats()
            job = session.submit_task(settings, callbacks=phases)
            state["state"] = "running"
            write_json(receipt, state)
            limit = min(deadline - COLLECTION_RESERVE_SECONDS, time.time() + task.get("timeout_seconds", 900))
            while not job.done:
                write_json(run_root / "progress.json", {"task_id": task["id"], "state": "running",
                    "elapsed_seconds": time.monotonic() - started, "memory": memory_snapshot(torch), **phases.snapshot()})
                if time.time() >= limit:
                    job.cancel()
                    raise TimeoutError("deadline_cancel_requested_reconcile_required")
                time.sleep(1)
            join_deadline = time.monotonic() + 10
            while any(t.name == "wangp-session-worker" and t.is_alive() for t in threading.enumerate()) and time.monotonic() < join_deadline:
                time.sleep(0.1)
            if any(t.name == "wangp-session-worker" and t.is_alive() for t in threading.enumerate()):
                raise RuntimeError("upstream_worker_not_quiescent")
            torch.cuda.synchronize()
            state["submission_to_result_seconds"] = time.monotonic() - started
            state.update(phases.snapshot())
            state["state"] = "collecting"
            write_json(receipt, state)
            outputs, probe = collect(job.result(timeout=0), output_dir, task)
            if audit is not None:
                state["runtime_audit"] = audit()
            state.update(state="complete", outputs=outputs, media_probe=probe,
                         total_seconds=time.monotonic() - started, completed_epoch=time.time(),
                         memory_after=memory_snapshot(torch))
            write_json(receipt, state)
            previous_mode = task["mode"]
        except BaseException as error:
            # Unknown dispatch/runtime/collection never becomes an implicit retry.
            state.update(state="reconcile_required", error_type=type(error).__name__,
                         error=str(error)[:500], elapsed_seconds=time.monotonic() - started,
                         memory_after=memory_snapshot(torch))
            write_json(receipt, state)
            raise
    return {"state": "complete", "tasks": len(tasks)}


def run(args):
    if not math.isfinite(args.deadline_epoch) or not time.time() < args.deadline_epoch <= time.time() + 86400:
        raise ValueError("finite_future_authorization_deadline_required")
    root, models, run_root = map(absolute_dir, (args.runtime_root, args.model_root, args.run_root))
    import fcntl
    with (run_root / "pilot.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        expected = json.loads(Path(args.environment_lock).read_text())
        observed = environment(root)
        if observed != expected:
            raise ValueError("reviewed_environment_changed")
        torch = importlib.import_module("torch")
        write_json(run_root / "resource-admission.json", resource_admission(torch))
        document = json.loads(Path(args.tasks).read_text())
        tasks = document["tasks"]
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 32:
            raise ValueError("bounded_task_list_required")
        for task in tasks:
            validate_task(task)
        if len({t["id"] for t in tasks}) != len(tasks):
            raise ValueError("duplicate_task_ids")
        # Audit receipts before expensive model verification or runtime initialization.
        pending = [t for t in tasks if not completed_or_refuse(run_root / "receipts" / (t["id"] + ".json"), task_digest(t))]
        if not pending:
            print(canonical({"state": "all_complete_verified", "count": len(tasks)}))
            return
        if time.time() >= args.deadline_epoch:
            raise ValueError("authorization_deadline_elapsed")
        for task in pending:
            verify_input_media(task)
        pending_modes = sorted({task["mode"] for task in pending})
        verified_assets = verify_assets(models, modes=pending_modes)
        verified = time.monotonic()
        config = runtime_config(models)
        config_path = run_root / "wgp_config.json"
        config_preparation = prepare_runtime_config(config_path, config)
        output = run_root / "outputs"
        output.mkdir(exist_ok=True, mode=0o700)
        # All component bytes must already be present; fail closed on unplanned HF fetches.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
        sys.path.insert(0, str(root))
        api = importlib.import_module("shared.api")
        if not Path(api.__file__).resolve().is_relative_to(root):
            raise ValueError("upstream_import_collision")
        session = api.init(root=root, config_path=config_path, output_dir=output,
            cli_args=["--attention", "sdpa", "--profile", "4", "--perc-reserved-mem-max", "0.2"],
            console_output=True, console_isatty=False, webui_state=None)
        # Public metadata resolves the actual selected model definitions before submit.
        for mode, model_type in MODEL_TYPES.items():
            definition = session.get_model_def(model_type)
            wanted = ASSETS[0 if mode == "fl" else 1][0]
            if not any(str(url).endswith("/" + wanted) for url in definition["URLs"]):
                raise ValueError("pruned_int8_model_definition_mismatch")
            # At this pinned API revision this module is the engine used by submit.
            # Assert actual filename resolution, not merely presence in a URL list.
            module = session._ensure_runtime().module
            selected = module.get_model_filename(model_type, quantization="int8", dtype_policy="bf16")
            if not str(selected).endswith("/" + wanted):
                raise ValueError("INT8_transformer_selection_mismatch")
            if module.transformer_quantization != "int8" or module.text_encoder_quantization != "int8":
                raise ValueError("INT8_runtime_quantization_mismatch")
        write_json(run_root / "preflight.json", {"recipe": asset_manifest(), "environment": observed,
            "verified_modes": pending_modes, "verified_asset_paths": verified_assets,
            "config_preparation": config_preparation,
            "runtime_audit": runtime_audit(module, config),
            "config": config, "verification_seconds": verified-started,
            "runtime_initialization_seconds": time.monotonic()-verified,
            "note": "Runtime import readiness is not inference success."})
        outcome = run_tasks(session, tasks, run_root, output, torch, args.deadline_epoch,
                            audit=lambda: runtime_audit(module, config))
        session.close()
        print(canonical({**outcome, "total_seconds": time.monotonic()-started}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("assets")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--runtime-root", required=True)
    inspect.add_argument("--output", required=True)
    execute = sub.add_parser("run")
    for key in ("runtime-root", "model-root", "run-root", "tasks", "environment-lock"):
        execute.add_argument("--" + key, required=True)
    execute.add_argument("--deadline-epoch", type=float, required=True)
    args = parser.parse_args()
    if args.action == "assets":
        print(json.dumps(asset_manifest(), indent=2))
    elif args.action == "inspect":
        output = Path(args.output)
        if output.exists():
            raise ValueError("environment_receipt_already_exists")
        write_json(output, environment(absolute_dir(args.runtime_root)))
        write_json(output.with_suffix(".resources.json"), resource_admission(importlib.import_module("torch"), enforce=False))
    else:
        run(args)


if __name__ == "__main__":
    main()
