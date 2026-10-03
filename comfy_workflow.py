"""Build native ComfyUI H3 API prompts without importing ComfyUI or loading models.

Verified upstream: Comfy-Org/ComfyUI e9027f2b30f37bb3052714eb08fcf479542f4fc0,
comfy_extras/nodes_minimax_h3.py, nodes_video.py and nodes_audio.py.
R2V template: Comfy-Org/workflow_templates e7cd011d4ded3411c2f481200544f0be6fdc962e.
Source URLs are recorded in SOURCE_URLS. Video uploads must already be decoded,
resampled to 24fps, and explicitly padded to the native 17k+5 frame grid.
This deterministic prompt formatting is not MiniMax's hosted Context-IR.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

COMFY_COMMIT = "e9027f2b30f37bb3052714eb08fcf479542f4fc0"
TEMPLATE_COMMIT = "e7cd011d4ded3411c2f481200544f0be6fdc962e"
SOURCE_URLS = {
    "h3": f"https://raw.githubusercontent.com/Comfy-Org/ComfyUI/{COMFY_COMMIT}/comfy_extras/nodes_minimax_h3.py",
    "video": f"https://raw.githubusercontent.com/Comfy-Org/ComfyUI/{COMFY_COMMIT}/comfy_extras/nodes_video.py",
    "audio": f"https://raw.githubusercontent.com/Comfy-Org/ComfyUI/{COMFY_COMMIT}/comfy_extras/nodes_audio.py",
    "template": f"https://raw.githubusercontent.com/Comfy-Org/workflow_templates/{TEMPLATE_COMMIT}/templates/video_minimax_h3_r2v.json",
}

DIFFUSION_REF = "minimax_h3_ref2va_bf16.safetensors"
DIFFUSION_FL = "minimax_h3_fl2va_bf16.safetensors"
CLIP_NAME = "qwen3vl_32b_minimax_h3_bf16.safetensors"
VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"
DEFAULT_STEPS = 20
FPS = 24
ASPECT_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
RESOLUTIONS = ("480P", "576P", "768P", "custom")
SAMPLER_NAMES = (
    "euler", "euler_cfg_pp", "euler_ancestral", "euler_ancestral_cfg_pp", "heun", "heunpp2",
    "exp_heun_2_x0", "exp_heun_2_x0_sde", "dpm_2", "dpm_2_ancestral", "lms", "dpm_fast",
    "dpm_adaptive", "dpmpp_2s_ancestral", "dpmpp_2s_ancestral_cfg_pp", "dpmpp_sde",
    "dpmpp_sde_gpu", "dpmpp_2m", "dpmpp_2m_cfg_pp", "dpmpp_2m_sde", "dpmpp_2m_sde_gpu",
    "dpmpp_2m_sde_heun", "dpmpp_2m_sde_heun_gpu", "dpmpp_3m_sde", "dpmpp_3m_sde_gpu",
    "ddpm", "lcm", "ipndm", "ipndm_v", "deis", "cfgpp_ud10_ab", "res_multistep",
    "res_multistep_cfg_pp", "res_multistep_ancestral", "res_multistep_ancestral_cfg_pp",
    "gradient_estimation", "gradient_estimation_cfg_pp", "er_sde", "seeds_2", "seeds_3",
    "sa_solver", "sa_solver_pece", "ddim", "uni_pc", "uni_pc_bh2",
)
SCHEDULER_NAMES = ("simple", "sgm_uniform", "karras", "exponential", "ddim_uniform",
                   "beta", "normal", "linear_quadratic", "kl_optimal")
CONTROL_FIELDS = frozenset({
    "steps", "seed", "sampler_name", "scheduler", "denoise", "ref_image_size", "video_audio",
    "shift_video", "shift_audio", "guides", "video_decode", "audio_decode", "video_tile_size",
    "video_overlap", "video_temporal_size", "video_temporal_overlap", "audio_tile_size",
    "audio_overlap", "encoder_device", "export_crf",
})
REQUEST_FIELDS = CONTROL_FIELDS | frozenset({
    "backend", "model", "mode", "prompt", "duration", "resolution", "aspect_ratio", "width",
    "height", "inputs", "generate_audio", "_job_id",
})
MODEL_FILES = {
    "diffusion_models": [DIFFUSION_REF, DIFFUSION_FL],
    "text_encoders": [CLIP_NAME],
    "vae": [VIDEO_VAE, AUDIO_VAE],
}
REQUIRED_NODE_TYPES = frozenset({
    "UNETLoader", "CLIPLoader", "VAELoader", "LoadImage", "LoadVideo",
    "GetVideoComponents", "LoadAudio", "MiniMaxH3ReferenceToVideo",
    "MiniMaxH3ImageToVideo", "RandomNoise", "BasicGuider", "KSamplerSelect",
    "BasicScheduler", "SamplerCustomAdvanced", "VAEDecode", "VAEDecodeAudio",
    "CreateVideo", "SaveVideo", "SaveAudioAdvanced",
    "MiniMaxH3AddGuide", "MiniMaxH3SigmaShift", "VAEDecodeTiled",
})


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def native_output_spec(request: Mapping[str, Any]) -> dict[str, int | float]:
    """Expose native frame snapping so the API/UI can report the actual duration."""
    duration = _number(request.get("duration", 5), "duration")
    if not 4 <= duration <= 15:
        raise ValueError("duration must be between 4 and 15 seconds")
    resolution = request.get("resolution", "768P")
    if resolution not in RESOLUTIONS:
        raise ValueError("resolution must be 480P, 576P, 768P or custom; hosted 2K regeneration is unavailable")
    aspect = request.get("aspect_ratio", "16:9")
    if resolution == "custom":
        width = _integer(request.get("width"), "width", 256, 1536)
        height = _integer(request.get("height"), "height", 256, 1536)
        if width % 32 or height % 32:
            raise ValueError("Custom width and height must be multiples of 32")
        if width * height > 768 * 1344:
            raise ValueError("Custom canvas must not exceed the 768*1344 native pixel area")
        if not .4 <= width / height <= 2.5:
            raise ValueError("Custom canvas aspect ratio must be between 0.4 and 2.5")
    else:
        if aspect not in ASPECT_RATIOS:
            raise ValueError(f"aspect_ratio must be one of {', '.join(ASPECT_RATIOS)}")
        left, right = map(int, aspect.split(":"))
        ratio = left / right
        short_edge = int(resolution[:-1])
        area_cap = short_edge * short_edge * 1.75
        # Keep the pinned native adapt_canvas() algorithm and exact old 768P canvases.
        width, height = (short_edge * ratio, short_edge) if ratio >= 1 else (short_edge, short_edge / ratio)
        if width * height > area_cap:
            scale = math.sqrt(area_cap / (width * height))
            width, height = width * scale, height * scale
        width, height = max(32, round(width / 32) * 32), max(32, round(height / 32) * 32)
    frames = max(5, round(duration * FPS))
    frames += (5 - frames % 17) % 17
    return {"width": width, "height": height, "frames": frames,
            "actual_duration": frames / FPS}


def _choice(value: Any, name: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(choices)}")
    return value


def _stepped_int(value: Any, name: str, minimum: int, maximum: int, step: int) -> int:
    value = _integer(value, name, minimum, maximum)
    if value % step:
        raise ValueError(f"{name} must be a multiple of {step}")
    return value


def validate_controls(request: Mapping[str, Any], uploads: Mapping[str, Mapping[str, Any]],
                      spec: Mapping[str, Any]) -> dict[str, Any]:
    """Pure control/media validation, reusable by the API before creating a GPU job.

    Sampler enums reflect the saved deployed schema, not a claim that every
    combination has been quality-tested. Guides use the requested export end as
    well as the native frame limit so neither the native node nor export silently
    crops the guide. No file, model, environment or network access occurs here.
    """
    if not isinstance(request, Mapping) or not isinstance(uploads, Mapping):
        raise ValueError("request and uploads must be objects")
    unknown = set(request) - REQUEST_FIELDS
    if unknown:
        raise ValueError(f"Unsupported request fields: {', '.join(sorted(unknown))}")
    mode = _choice(request.get("mode", "ref"), "mode", ("ref", "fl"))
    seed = request.get("seed", 0)
    if isinstance(seed, str):
        if not re.fullmatch(r"[0-9]{1,20}", seed):
            raise ValueError("seed must be an integer or unsigned decimal string")
        seed = int(seed)
    controls = {
        "steps": _integer(request.get("steps", DEFAULT_STEPS), "steps", 1, 100),
        "seed": _integer(seed, "seed", 0, 0xffffffffffffffff),
        "sampler_name": _choice(request.get("sampler_name", "res_multistep"), "sampler_name", SAMPLER_NAMES),
        "scheduler": _choice(request.get("scheduler", "auto"), "scheduler", ("auto",) + SCHEDULER_NAMES),
        "ref_image_size": _choice(request.get("ref_image_size", "max"), "ref_image_size", ("match", "max")),
        "video_decode": _choice(request.get("video_decode", "normal"), "video_decode", ("normal", "tiled")),
        "audio_decode": _choice(request.get("audio_decode", "normal"), "audio_decode", ("normal", "tiled")),
        "encoder_device": _choice(request.get("encoder_device", "default"), "encoder_device", ("default", "cpu")),
        "export_crf": _integer(request.get("export_crf", 18), "export_crf", 0, 51),
    }
    if controls["audio_decode"] == "tiled":
        raise ValueError("H3 音频分块解码暂不可用：当前通用 VAEDecodeAudioTiled 与 H3 音频 VAE 张量维度不兼容；请用 audio_decode=normal。")
    denoise = _number(request.get("denoise", 1), "denoise")
    if not .01 <= denoise <= 1:
        raise ValueError("denoise must be between 0.01 and 1")
    controls["denoise"] = denoise
    for name in ("shift_video", "shift_audio"):
        value = request.get(name)
        if value is not None:
            value = _number(value, name)
            if not .01 <= value <= 100:
                raise ValueError(f"{name} must be between 0.01 and 100")
        controls[name] = value
    for name, default, minimum, maximum, step in (
        ("video_tile_size", 512, 64, 4096, 32), ("video_overlap", 64, 0, 4096, 32),
        ("video_temporal_size", 64, 8, 4096, 4), ("video_temporal_overlap", 8, 4, 4096, 4),
        ("audio_tile_size", 512, 32, 8192, 8), ("audio_overlap", 64, 0, 1024, 8),
    ):
        controls[name] = _stepped_int(request.get(name, default), name, minimum, maximum, step)
    for size, overlap in (("video_tile_size", "video_overlap"),
                          ("video_temporal_size", "video_temporal_overlap"),
                          ("audio_tile_size", "audio_overlap")):
        if controls[overlap] >= controls[size]:
            raise ValueError(f"{overlap} must be less than {size}")
    raw_inputs = request.get("inputs", {})
    if not isinstance(raw_inputs, Mapping):
        raise ValueError("inputs must be an object")
    videos = raw_inputs.get("videos", [])
    if not isinstance(videos, list) or any(not isinstance(x, str) for x in videos):
        raise ValueError("inputs.videos must be a list of upload IDs")
    video_audio = request.get("video_audio", {})
    if not isinstance(video_audio, Mapping) or any(not isinstance(k, str) or not isinstance(v, bool) for k, v in video_audio.items()):
        raise ValueError("video_audio must map video upload IDs to booleans")
    if set(video_audio) - set(videos):
        raise ValueError("video_audio contains a video not present in inputs.videos")
    if mode == "fl" and video_audio:
        raise ValueError("video_audio is only available in reference mode")
    controls["video_audio"] = dict(video_audio)
    guides = request.get("guides", [])
    if not isinstance(guides, list) or len(guides) > 8:
        raise ValueError("guides must be an array of at most 8 time anchors")
    normalized_guides = []
    export_duration = _number(request.get("duration", 5), "duration")
    for index, guide in enumerate(guides):
        label = f"guides[{index}]"
        if not isinstance(guide, Mapping) or set(guide) - {"media_id", "time_seconds", "use_audio"}:
            raise ValueError(f"{label} must contain only media_id, time_seconds and use_audio")
        media_id = guide.get("media_id")
        if not isinstance(media_id, str) or not media_id:
            raise ValueError(f"{label}.media_id must be an upload ID")
        meta = uploads.get(media_id)
        if not isinstance(meta, Mapping) or meta.get("kind") not in ("image", "video", "audio"):
            raise ValueError(f"{label} must reference a decoded image, video or audio")
        time_seconds = _number(guide.get("time_seconds", 0), f"{label}.time_seconds")
        frame_idx = round(time_seconds * FPS)
        if time_seconds < 0 or time_seconds >= export_duration or frame_idx >= export_duration * FPS or frame_idx >= spec["frames"]:
            raise ValueError(f"{label} time anchor is outside the requested output duration")
        use_audio = guide.get("use_audio", False)
        if not isinstance(use_audio, bool):
            raise ValueError(f"{label}.use_audio must be a boolean")
        kind = meta["kind"]
        remaining = export_duration - frame_idx / FPS
        if kind == "video":
            source_duration = _number(meta.get("source_duration", meta.get("duration")), f"{label} source duration")
            duration = _number(meta.get("duration"), f"{label} normalized duration")
            frames = _integer(meta.get("frame_count"), f"{label} frame_count", 5, 362)
            if not 2 <= source_duration <= 15 or meta.get("fps") != FPS or frames % 17 != 5 or not math.isclose(duration, frames / FPS, abs_tol=.002):
                raise ValueError(f"{label} video must be normalized to 24fps and the 17k+5 frame grid")
            if not isinstance(meta.get("has_audio"), bool):
                raise ValueError(f"{label} video metadata must specify has_audio")
            if use_audio and not meta["has_audio"]:
                raise ValueError(f"{label} video has no soundtrack to anchor")
            if frame_idx + frames > spec["frames"] or duration > remaining + 1e-6:
                raise ValueError(f"{label} video exceeds the remaining output; trim it or move the anchor earlier")
        elif kind == "audio":
            duration = _number(meta.get("duration"), f"{label} audio duration")
            if not 2 <= duration <= 15 or duration > remaining + 1e-6:
                raise ValueError(f"{label} audio exceeds the remaining output; trim it or move the anchor earlier")
        elif use_audio:
            raise ValueError(f"{label} images have no soundtrack")
        normalized_guides.append({"media_id": media_id, "time_seconds": time_seconds, "use_audio": use_audio})
    controls["guides"] = normalized_guides
    return controls


def controls_metadata() -> dict[str, Any]:
    """Public, secret-free capability metadata from the pinned deployed nodes.

    Practical service ranges intentionally bound engine maxima; unavailable
    official features and generic-node options are not advertised as H3 support.
    """
    return {
        "source": {"comfyui_revision": COMFY_COMMIT, "schema": "comfy-object-info.json"},
        "samplers": list(SAMPLER_NAMES), "schedulers": ["auto", *SCHEDULER_NAMES],
        "resolutions": list(RESOLUTIONS), "aspect_ratios": list(ASPECT_RATIOS),
        "ref_image_sizes": ["match", "max"], "decoder_modes": ["normal", "tiled"],
        "video_decoder_modes": ["normal", "tiled"], "audio_decoder_modes": ["normal"],
        "audio_decode": ["normal"],
        "encoder_devices": ["default", "cpu"],
        "ranges": {
            "duration": {"min": 4, "max": 15, "step": 1, "default": 5},
            "steps": {"min": 1, "max": 100, "step": 1, "default": 20},
            "seed": {"min": "0", "max": "18446744073709551615", "decimal_string": True},
            "width": {"min": 256, "max": 1536, "step": 32},
            "height": {"min": 256, "max": 1536, "step": 32},
            "custom_area": {"max": 768 * 1344},
            "custom_aspect_ratio": {"min": .4, "max": 2.5},
            "denoise": {"min": .01, "max": 1, "step": .01, "default": 1},
            "shift_video": {"min": .01, "max": 100, "step": .01, "default": 12},
            "shift_audio": {"min": .01, "max": 100, "step": .01, "default": 3},
            "video_tile_size": {"min": 64, "max": 4096, "step": 32, "default": 512},
            "video_overlap": {"min": 0, "max": 4096, "step": 32, "default": 64},
            "video_temporal_size": {"min": 8, "max": 4096, "step": 4, "default": 64},
            "video_temporal_overlap": {"min": 4, "max": 4096, "step": 4, "default": 8},
            "audio_tile_size": {"min": 32, "max": 8192, "step": 8, "default": 512},
            "audio_overlap": {"min": 0, "max": 1024, "step": 8, "default": 64},
            "export_crf": {"min": 0, "max": 51, "step": 1, "default": 18},
            "guides": {"max": 8, "min_time_seconds": 0, "time_step": 1 / FPS},
        },
        "defaults": {"sampler_name": "res_multistep", "scheduler": "auto", "ref_image_size": "max",
                     "video_decode": "normal", "audio_decode": "normal", "encoder_device": "default",
                     "denoise": 1, "shift_video": None, "shift_audio": None, "steps": 20,
                     "video_audio": {}, "guides": [], "export_crf": 18},
        "native_fps": FPS, "native_audio_sample_rate": 32000, "native_audio_channels": 2,
        "unavailable": ["official-context-ir", "official-regenerate-2k", "fun-controlnet-weights"],
        "unsupported": {
            "audio_tiled": "当前通用 VAEDecodeAudioTiled 与 H3 音频 VAE 张量维度不兼容；2026-10-03 真正 GPU 推理已确认失败，使用普通音频解码。",
        },
        "notes": {
            "resolution": "480P/576P/custom change actual sampling dimensions; only 768P has prior live acceptance. These are not the official 2K regeneration model.",
            "sampler": "All deployed engine enums are available for experiments; prior acceptance used res_multistep, beta/simple, 20 steps.",
            "denoise": "This changes the sigma schedule; it is not reference/guide strength for the empty-latent H3 pipeline.",
            "guide": "Images, clips and audio anchor conditioning at a 24fps frame; guide tails must fit the requested export, without silent cropping.",
            "generate_audio": "Silent export skips audio decoding/output, not joint audio-latent generation.",
            "export_crf": "Compression quality only; does not add model-generated detail.",
        },
    }


def _filename(filenames: Mapping[str, str], upload_id: str) -> str:
    name = filenames.get(upload_id)
    if not isinstance(name, str) or not name or any(c in name for c in "\r\n\x00:"):
        raise ValueError(f"Missing or invalid Comfy filename for upload {upload_id}")
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or normalized.endswith("/"):
        raise ValueError("Comfy filenames must be relative paths within its input directory")
    return normalized


def _validate_inputs(request: Mapping[str, Any], uploads: Mapping[str, Mapping[str, Any]],
                     filenames: Mapping[str, str], spec: Mapping[str, Any]) -> dict[str, Any]:
    mode = request.get("mode", "ref")
    if mode not in ("ref", "fl"):
        raise ValueError("mode must be 'ref' or 'fl'")
    raw = request.get("inputs", {})
    if not isinstance(raw, Mapping):
        raise ValueError("inputs must be an object")
    unknown = set(raw) - {"images", "videos", "audios", "first_frame", "last_frame"}
    if unknown:
        raise ValueError(f"Unsupported input fields: {', '.join(sorted(unknown))}")
    result: dict[str, Any] = {}
    for key, maximum in (("images", 9), ("videos", 3), ("audios", 3)):
        value = raw.get(key, [])
        if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
            raise ValueError(f"inputs.{key} must be a list of upload IDs")
        if len(value) > maximum:
            raise ValueError(f"inputs.{key} accepts at most {maximum} files")
        result[key] = list(value)
    for key in ("first_frame", "last_frame"):
        value = raw.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"inputs.{key} must be an upload ID or null")
        result[key] = value
    count = sum(len(result[key]) for key in ("images", "videos", "audios"))
    if count > 12:
        raise ValueError("Mixed reference inputs accept at most 12 files in total")
    if mode == "ref" and (result["first_frame"] or result["last_frame"]):
        raise ValueError("Reference mode cannot include first/last frames")
    if mode == "fl" and count:
        raise ValueError("First/last-frame mode cannot include reference images, videos or audio")
    if mode == "ref" and not count:
        raise ValueError("Reference mode needs at least one reference; use 'fl' for text-to-video")
    for key, kind in (("images", "image"), ("videos", "video"), ("audios", "audio"),
                      ("first_frame", "image"), ("last_frame", "image")):
        ids = result[key] if isinstance(result[key], list) else [result[key]] if result[key] else []
        total = 0.0
        for upload_id in ids:
            meta = uploads.get(upload_id)
            if not isinstance(meta, Mapping) or meta.get("kind") != kind:
                raise ValueError(f"Upload {upload_id} must be a decoded {kind}")
            _filename(filenames, upload_id)
            if kind in ("video", "audio"):
                duration = _number(meta.get("duration"), f"{upload_id} duration")
                source_duration = _number(meta.get("source_duration", duration), f"{upload_id} source_duration")
                if not 2 <= source_duration <= 15:
                    raise ValueError(f"{kind} reference {upload_id} must be 2–15 seconds")
                total += source_duration
                if kind == "video":
                    if _number(meta.get("fps"), f"{upload_id} fps") != FPS:
                        raise ValueError("Reference videos must be normalized to 24fps before submission")
                    frames = _integer(meta.get("frame_count"), f"{upload_id} frame_count", 5, 362)
                    if frames % 17 != 5 or not math.isclose(duration, frames / FPS, abs_tol=0.002):
                        raise ValueError("Reference videos must explicitly match the native 17k+5 frame grid")
                    if frames > spec["frames"]:
                        raise ValueError("Reference video exceeds the output length; increase duration or explicitly trim the reference")
                    if not isinstance(meta.get("has_audio"), bool):
                        raise ValueError("Decoded video metadata must specify has_audio")
                elif not 2 <= duration <= 15:
                    raise ValueError("Standalone audio references must be 2–15 seconds")
        if kind in ("video", "audio") and total > 15 + 1e-6:
            raise ValueError(f"Total source {kind} reference duration must not exceed 15 seconds")
    return result


def format_prompt(prompt: str, inputs: Mapping[str, Any], uploads: Mapping[str, Mapping[str, Any]],
                  video_audio: Mapping[str, bool] | None = None) -> str:
    """Resolve UI aliases without rewriting the user's scene or inferring missing content."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    video_audio = video_audio or {}
    soundtrack_count = sum(bool(uploads[x].get("has_audio")) and video_audio.get(x, True) for x in inputs["videos"])
    picture_count = len(inputs["images"]) or sum(bool(inputs.get(key)) for key in ("first_frame", "last_frame"))
    limits = {"image": picture_count, "picture": picture_count,
              "video": len(inputs["videos"]), "audio": len(inputs["audios"])}
    def replace(match: re.Match[str]) -> str:
        kind, index = match.group(1).lower(), int(match.group(2))
        if not 1 <= index <= limits[kind]:
            raise ValueError(f"Prompt references unavailable @{kind}{index}")
        label = {"image": "Picture", "picture": "Picture", "video": "Video", "audio": "Audio"}[kind]
        return f"<{label} {index + soundtrack_count if kind == 'audio' else index}>"
    text = re.sub(r"@(image|picture|video|audio)\s*(\d+)(?![A-Za-z0-9_])", replace, prompt.strip(), flags=re.IGNORECASE)
    # The workbench labels standalone audio with its native ordinal, including
    # preceding video soundtracks. Chinese labels follow those displayed numbers.
    def replace_chinese(match: re.Match[str]) -> str:
        label = {"图片": "Picture", "图像": "Picture", "视频": "Video", "音频": "Audio"}[match.group(1)]
        index = int(match.group(2))
        maximum = {"Picture": picture_count, "Video": len(inputs["videos"]),
                   "Audio": soundtrack_count + len(inputs["audios"])}[label]
        if not 1 <= index <= maximum:
            raise ValueError(f"Prompt references unavailable {match.group(0)}")
        return f"<{label} {index}>"
    text = re.sub(r"(?<![<@])(图片|图像|视频|音频)\s*(\d+)(?!\d|\s*>)", replace_chinese, text)
    # Validate native ordinals too: excluding a video's audio changes their range.
    native_limits = {"Picture": picture_count, "Video": len(inputs["videos"]),
                     "Audio": soundtrack_count + len(inputs["audios"])}
    for match in re.finditer(r"<(Picture|Video|Audio)\s+(\d+)>", text):
        if not 1 <= int(match.group(2)) <= native_limits[match.group(1)]:
            raise ValueError(f"Prompt references unavailable {match.group(0)}")
    if not text.startswith("integrated_multimodal_description:"):
        text = "integrated_multimodal_description: " + text
    return text


def build_workflow(request: Mapping[str, Any], uploads: Mapping[str, Mapping[str, Any]],
                   filenames: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Return the real ComfyUI /prompt 'prompt' object for Ref2VA or FL2VA."""
    if not isinstance(request, Mapping) or not isinstance(uploads, Mapping) or not isinstance(filenames, Mapping):
        raise ValueError("request, uploads and filenames must be objects")
    spec = native_output_spec(request)
    controls = validate_controls(request, uploads, spec)
    inputs = _validate_inputs(request, uploads, filenames, spec)
    generate_audio = request.get("generate_audio", True)
    if not isinstance(generate_audio, bool):
        raise ValueError("generate_audio must be a boolean")
    steps, seed = controls["steps"], controls["seed"]
    job_id = request.get("_job_id") or uuid.uuid4().hex
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id):
        raise ValueError("_job_id must be a simple filename component")
    graph: dict[str, dict[str, Any]] = {}
    def add(class_type: str, **node_inputs: Any) -> str:
        node_id = str(len(graph) + 1)
        graph[node_id] = {"class_type": class_type, "inputs": node_inputs}
        return node_id
    def link(node_id: str, index: int = 0) -> list[Any]:
        return [node_id, index]
    mode = request.get("mode", "ref")
    model = add("UNETLoader", unet_name=DIFFUSION_REF if mode == "ref" else DIFFUSION_FL, weight_dtype="default")
    shift_video = controls["shift_video"] if controls["shift_video"] is not None else 12.0
    shift_audio = controls["shift_audio"] if controls["shift_audio"] is not None else 3.0
    if shift_video != 12.0 or shift_audio != 3.0:
        model = add("MiniMaxH3SigmaShift", model=link(model), shift_video=shift_video, shift_audio=shift_audio)
    clip = add("CLIPLoader", clip_name=CLIP_NAME, type="minimax", device=controls["encoder_device"])
    video_vae = add("VAELoader", vae_name=VIDEO_VAE)
    audio_vae = add("VAELoader", vae_name=AUDIO_VAE)
    conditioning_inputs = {"clip": link(clip), "vae": link(video_vae),
                           "prompt": format_prompt(request.get("prompt"), inputs, uploads, controls["video_audio"]),
                           "width": spec["width"], "height": spec["height"], "length": spec["frames"]}
    if mode == "ref":
        conditioning_inputs.update(audio_vae=link(audio_vae), ref_image_size=controls["ref_image_size"])
        for index, upload_id in enumerate(inputs["images"]):
            loader = add("LoadImage", image=_filename(filenames, upload_id))
            conditioning_inputs[f"ref_images.ref_image_{index}"] = link(loader)
        for index, upload_id in enumerate(inputs["videos"]):
            loader = add("LoadVideo", file=_filename(filenames, upload_id))
            components = add("GetVideoComponents", video=link(loader))
            conditioning_inputs[f"ref_videos.ref_video_{index}"] = link(components)
            if uploads[upload_id]["has_audio"] and controls["video_audio"].get(upload_id, True):
                conditioning_inputs[f"ref_video_audios.ref_video_audio_{index}"] = link(components, 1)
        for index, upload_id in enumerate(inputs["audios"]):
            loader = add("LoadAudio", audio=_filename(filenames, upload_id))
            conditioning_inputs[f"ref_audios.ref_audio_{index}"] = link(loader)
        condition = add("MiniMaxH3ReferenceToVideo", **conditioning_inputs)
    else:
        for key in ("first_frame", "last_frame"):
            if inputs[key]:
                loader = add("LoadImage", image=_filename(filenames, inputs[key]))
                conditioning_inputs[key] = link(loader)
        condition = add("MiniMaxH3ImageToVideo", **conditioning_inputs)
    latent_source = condition
    for guide in controls["guides"]:
        media_id = guide["media_id"]
        kind = uploads[media_id]["kind"]
        guide_inputs = {"positive": link(condition), "latent": link(latent_source, 1),
                        "frame_idx": round(guide["time_seconds"] * FPS)}
        if kind == "image":
            loader = add("LoadImage", image=_filename(filenames, media_id))
            guide_inputs.update(image=link(loader), vae=link(video_vae))
        elif kind == "video":
            loader = add("LoadVideo", file=_filename(filenames, media_id))
            components = add("GetVideoComponents", video=link(loader))
            guide_inputs.update(image=link(components), vae=link(video_vae))
            if guide["use_audio"]:
                guide_inputs.update(audio=link(components, 1), audio_vae=link(audio_vae))
        else:
            loader = add("LoadAudio", audio=_filename(filenames, media_id))
            guide_inputs.update(audio=link(loader), audio_vae=link(audio_vae))
        condition = add("MiniMaxH3AddGuide", **guide_inputs)
    noise = add("RandomNoise", noise_seed=seed)
    guider = add("BasicGuider", model=link(model), conditioning=link(condition))
    sampler = add("KSamplerSelect", sampler_name=controls["sampler_name"])
    # Official R2V template notes recommend beta/normal for reference-heavy
    # prompts; FL2VA retains the native template's simple schedule.
    scheduler_name = controls["scheduler"]
    if scheduler_name == "auto":
        scheduler_name = "beta" if mode == "ref" else "simple"
    scheduler = add("BasicScheduler", model=link(model), scheduler=scheduler_name, steps=steps, denoise=controls["denoise"])
    sampled = add("SamplerCustomAdvanced", noise=link(noise), guider=link(guider),
                  sampler=link(sampler), sigmas=link(scheduler), latent_image=link(latent_source, 1))
    if controls["video_decode"] == "tiled":
        decoded_video = add("VAEDecodeTiled", samples=link(sampled), vae=link(video_vae),
                            tile_size=controls["video_tile_size"], overlap=controls["video_overlap"],
                            temporal_size=controls["video_temporal_size"], temporal_overlap=controls["video_temporal_overlap"])
    else:
        decoded_video = add("VAEDecode", samples=link(sampled), vae=link(video_vae))
    create_inputs = {"images": link(decoded_video), "fps": FPS, "bit_depth": 8}
    if generate_audio:
        decoded_audio = add("VAEDecodeAudio", samples=link(sampled), vae=link(audio_vae))
        create_inputs["audio"] = link(decoded_audio)
        add("SaveAudioAdvanced", audio=link(decoded_audio), filename_prefix=f"h3-studio/{job_id}_audio", format="flac")
    created_video = add("CreateVideo", **create_inputs)
    saved = add("SaveVideo", video=link(created_video), filename_prefix=f"h3-studio/{job_id}", format="mp4")
    graph[saved]["inputs"]["format.codec"] = "h264"
    if controls["export_crf"] < 18:
        # Avoid first losing detail to the raw save node's default CRF18 before
        # the Windows exporter applies a higher-quality choice. Exact default
        # and more-compressed exports retain the historical raw graph.
        graph[saved]["inputs"]["format.codec.encoding"] = "re-encode"
        graph[saved]["inputs"]["format.codec.encoding.crf"] = controls["export_crf"]
    return graph
