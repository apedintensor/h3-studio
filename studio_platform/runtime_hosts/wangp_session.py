"""Explicit-start facade for the pinned upstream headless Session API.

Importing this module does not import WanGP/torch, load weights, or start threads.
The HTTP host owns durable receipts; this facade owns one live runtime handle.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from ..inference.protocol import BackendError, NotReady
from ..inference.wangp_contract import (EngineManifest, RuntimeObservation,
    RuntimeOutput, UPSTREAM_REVISION)

REQUIRED_CONFIG = {
    "transformer_quantization": "bf16", "text_encoder_quantization": "bf16",
    "transformer_dtype_policy": "bf16", "attention_mode": "sdpa",
    "video_profile": 4, "profile": 4, "vae_config": 3, "vae_precision": "16",
    "mixed_precision": "0", "compile": "", "boost": 1,
    "int8_kernels": "disabled", "kernel_precision": "strict",
    "fit_canvas": 2, "video_output_codec": "libx264_8", "video_container": "mp4",
    "audio_output_codec": "aac_128", "enhancer_enabled": 0,
    "save_queue_if_crash": 0, "notification_sound_enabled": 0,
    "embed_source_images": False,
}
CORE_VERSIONS = {"torch": "2.10.0+cu128", "torchvision": "0.25.0+cu128",
    "torchaudio": "2.10.0+cu128", "diffusers": "0.36.0", "transformers": "4.54.0",
    "numpy": "2.1.2", "optimum-quanto": "0.2.7", "comfy-kitchen": "0.2.35"}


def _contained_file(root: Path, path: Path) -> Path:
    """Reject links and escaping paths; output names never come from clients."""
    root = root.resolve(strict=True)
    candidate = path if path.is_absolute() else root / path
    # resolve alone would hide a symlink that points back inside the root.
    try:
        relative = candidate.absolute().relative_to(root)
    except ValueError:
        raise BackendError("wangp_output_path_invalid") from None
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise BackendError("wangp_output_path_invalid")
    target = candidate.resolve(strict=True)
    if not target.is_relative_to(root) or not target.is_file():
        raise BackendError("wangp_output_path_invalid")
    return target


def _check_config(path, model_root=None):
    path = Path(path).resolve(strict=True)
    if path.name != "wgp_config.json":
        raise ValueError("wangp_config_filename_invalid")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or set(data) != set(REQUIRED_CONFIG) | {"checkpoints_paths"}
            or any(data.get(key) != value for key, value in REQUIRED_CONFIG.items())):
        raise ValueError("wangp_runtime_config_mismatch")
    paths = data.get("checkpoints_paths")
    if (not isinstance(paths, list) or len(paths) != 1 or not isinstance(paths[0], str)
            or not Path(paths[0]).is_absolute()):
        raise ValueError("wangp_runtime_model_root_mismatch")
    actual = Path(paths[0]).resolve(strict=True)
    if model_root is not None and actual != Path(model_root).resolve(strict=True):
        raise ValueError("wangp_runtime_model_root_mismatch")
    return data


def verify_runtime(runtime_root, config_path, manifest_path, model_root):
    """Explicit local attestation, not a model test and never an installer.

Hashing weights occurs only when this function is explicitly called on the GPU
host. No historical manifest/filename/size is treated as current verification.
The returned evidence contains no runtime environment or configuration values.
"""
    root = Path(runtime_root).resolve(strict=True)
    models = Path(model_root).resolve(strict=True)
    config = _check_config(config_path, models)
    manifest = EngineManifest.from_dict(json.loads(Path(manifest_path).read_text(encoding="utf-8")))
    environment_evidence = {}
    if manifest.document.get("runtime_digest_kind") == "sixnine-environment-lock-sha256":
        from .wangp_environment import verify_bound_environment
        environment_evidence = verify_bound_environment(root, manifest.document)
        revision = UPSTREAM_REVISION
    else:
        # Historical candidate manifests retain their exact verification path;
        # new cold bootstrap requires the fully bound package above.
        revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                  check=True, capture_output=True, text=True).stdout.strip()
        if revision != UPSTREAM_REVISION:
            raise ValueError("wangp_runtime_source_mismatch")
        for args in (["diff", "--quiet", "HEAD", "--"], ["diff", "--cached", "--quiet", "HEAD", "--"]):
            if subprocess.run(["git", "-C", str(root), *args], capture_output=True).returncode:
                raise ValueError("wangp_runtime_source_modified")
        untracked = subprocess.run(["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"],
                                  check=True, capture_output=True).stdout.decode().split("\0")
        if any(Path(name).suffix.lower() in {".py", ".pyd", ".so"} for name in untracked if name):
            raise ValueError("wangp_runtime_untracked_code")
    versions = {}
    for package, required in CORE_VERSIONS.items():
        version = importlib.metadata.version(package)
        if version != required:
            raise ValueError("wangp_runtime_dependency_mismatch")
        versions[package] = version
    checked = []
    for component in manifest.document["components"].values():
        for record in component["files"]:
            path = _contained_file(models, Path(record["path"]))
            if path.stat().st_size != record["size_bytes"]:
                raise ValueError("wangp_component_size_mismatch")
            sha = hashlib.sha256()
            git_blob = hashlib.sha1(("blob " + str(record["size_bytes"]) + "\0").encode())
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    sha.update(chunk)
                    git_blob.update(chunk)
            expected, observed = (record["sha256"], sha.hexdigest()) if "sha256" in record else (record["git_blob_sha1"], git_blob.hexdigest())
            if expected != observed:
                raise ValueError("wangp_component_hash_mismatch")
            checked.append({"path": record["path"], "sha256": sha.hexdigest(), "size_bytes": record["size_bytes"]})
    return {"manifest_digest": manifest.digest, "source_revision": revision,
            "core_versions": versions, "verified_files": checked,
            "config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "inference_verified": False, **environment_evidence}


def config_for_model_root(model_root):
    """Non-secret explicit startup configuration; caller writes wgp_config.json."""
    return {**REQUIRED_CONFIG, "checkpoints_paths": [str(Path(model_root).resolve(strict=True))]}


def _write_float_wav(path, samples, rate):
    # Imported only after a real upstream result. Preserve generated float samples;
    # the muxed MP4's AAC stream is not decoded/re-encoded to fabricate a raw WAV.
    import numpy as np
    import soundfile
    value = np.asarray(samples, dtype=np.float32)
    if value.ndim != 2 or value.shape[1] != 2 or not value.shape[0] or not np.isfinite(value).all() or rate != 32000:
        raise BackendError("wangp_audio_payload_invalid")
    soundfile.write(str(path), value, rate, format="WAV", subtype="FLOAT")


def _upstream_worker_alive():
    return any(thread.name == "wangp-session-worker" and thread.is_alive()
               for thread in threading.enumerate())


class _SessionHandle:
    def __init__(self, job, output_root, audio_writer, worker_alive, quiesce):
        self.job, self.root, self.audio_writer = job, output_root, audio_writer
        self.worker_alive, self.quiesce = worker_alive, quiesce
        self._stop_verified = False
        self._observation = None
        self._lock = threading.Lock()

    def cancel(self):
        # Upstream returns None: accepting intent is not evidence of GPU stop.
        self.job.cancel()
        return True

    def observe(self):
        with self._lock:
            if self._observation is not None:
                return self._observation
            if self.job.done is not True:
                return RuntimeObservation("running", stopped=False)
            # Upstream's outer exception handler can publish a result while its
            # daemon generation thread remains alive. Done alone is insufficient.
            if self.worker_alive() is not False:
                return RuntimeObservation("unknown", stopped=False)
            if not self._stop_verified:
                self.quiesce()  # Actual factory supplies CUDA synchronize.
                self._stop_verified = True
            result = self.job.result(timeout=0)
            if result.cancelled:
                self._observation = RuntimeObservation("cancelled", stopped=True)
                return self._observation
            if result.success is not True:
                self._observation = RuntimeObservation("failed", stopped=True)
                return self._observation
            if (result.total_tasks != 1 or result.successful_tasks != 1 or result.failed_tasks != 0
                    or result.errors or len(result.generated_files) != 1):
                raise BackendError("wangp_result_shape_invalid")
            video = _contained_file(self.root, Path(result.generated_files[0]))
            if video.suffix.lower() != ".mp4" or video.stat().st_size <= 0:
                raise BackendError("wangp_video_output_missing")
            artifacts = [item for item in result.artifacts if item.media_type == "video"
                         and item.path and Path(item.path).resolve() == video]
            if len(artifacts) != 1 or artifacts[0].audio_tensor is None or artifacts[0].audio_sampling_rate != 32000:
                raise BackendError("wangp_audio_output_missing")
            # Repeat observation retries packaging the SAME completed result only.
            # A collection failure never calls submit_task again.
            wav = video.with_name(video.stem + "-generated.wav")
            temp = wav.with_suffix(".pending.wav")
            if wav.is_symlink() or temp.is_symlink():
                raise BackendError("wangp_output_path_invalid")
            self.audio_writer(temp, artifacts[0].audio_tensor, artifacts[0].audio_sampling_rate)
            if not temp.is_file() or temp.stat().st_size <= 0:
                raise BackendError("wangp_audio_output_missing")
            with temp.open("r+b") as stream:
                os.fsync(stream.fileno())
            os.replace(temp, wav)
            self._observation = RuntimeObservation("succeeded", stopped=True, outputs={
                "video": RuntimeOutput(video, "video/mp4"), "audio": RuntimeOutput(wav, "audio/wav")})
            return self._observation


class PinnedWanGPSession:
    def __init__(self, session, output_root, *, quiesce,
                 audio_writer=_write_float_wav, worker_alive=_upstream_worker_alive):
        if not callable(quiesce) or not callable(worker_alive):
            raise ValueError("wangp_runtime_stop_probe_required")
        self.session = session
        self.output_root = Path(output_root).resolve(strict=True)
        self.audio_writer = audio_writer
        self.worker_alive, self.quiesce = worker_alive, quiesce
        self._lifecycle_lock = threading.RLock()
        self._closed = False

    def is_idle(self):
        return not self._closed and self.session.active_job is None and self.worker_alive() is False

    def submit_task(self, settings):
        with self._lifecycle_lock:
            if not self.is_idle():
                raise NotReady("wangp_session_busy")
            # Pristine computed defaults, not mutable GUI defaults/previous-job state.
            model = settings.get("model_type", "minimax_h3_fl2va")
            if model not in {"minimax_h3_fl2va", "minimax_h3_ref2va"}:
                raise ValueError("wangp_session_model_unsupported")
            values = self.session.get_default_settings(model)
            values.update(copy.deepcopy(dict(settings)))
            job = self.session.submit_task(values)
            return _SessionHandle(job, self.output_root, self.audio_writer, self.worker_alive, self.quiesce)

    def close_when_idle(self):
        """Unload only an idle runtime; never manufacture cancellation/stop proof."""
        with self._lifecycle_lock:
            if self._closed:
                return
            if not self.is_idle():
                raise NotReady("wangp_session_active_shutdown_refused")
            self.quiesce()
            self.session.close()
            self._closed = True


def create_session(runtime_root, config_path, output_dir):
    """Call only after verify_runtime on an isolated, authorized engine process."""
    root = Path(runtime_root).resolve(strict=True)
    _check_config(config_path)
    output = Path(output_dir).resolve(strict=True)
    existing = sys.modules.get("shared")
    if existing is not None and not Path(existing.__file__).resolve().is_relative_to(root):
        raise ValueError("wangp_runtime_import_collision")
    sys.path.insert(0, str(root))
    module = importlib.import_module("shared.api")
    if not Path(module.__file__).resolve().is_relative_to(root):
        raise ValueError("wangp_runtime_import_collision")
    session = module.init(root=root, config_path=config_path, output_dir=output,
        cli_args=["--attention", "sdpa", "--profile", "4"], console_output=False,
        console_isatty=False, webui_state=None)
    torch = importlib.import_module("torch")
    return PinnedWanGPSession(session, output, quiesce=torch.cuda.synchronize)
