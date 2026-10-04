#!/usr/bin/env python3
"""Prepare the authorized Linux H3 worker. No cloud-provider control calls.

Run beside model_manifest.json on the cloud host. Weights stay in the selected
Hugging Face cache; ComfyUI receives symlinks. Public downloads pass token=False.
The local --check-manifest command is offline and creates no deployment files.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as dt
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

GIB = 1024 ** 3
LOCK = threading.RLock()


class SetupError(RuntimeError):
    """A diagnosis code generated locally, never an upstream exception body."""
    def __init__(self, code):
        if not re.fullmatch(r"[A-Z][A-Za-z0-9]+", code):
            code = "InternalSetupFailure"
        self.code = code
        super().__init__(code)


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@contextlib.contextmanager
def suppress_native_download_diagnostics():
    """Rust download diagnostics can include signed URLs; retain our safe events."""
    saved = os.dup(2)
    sink = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(sink, 2)
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        os.close(sink)


def read_manifest(path):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    files = manifest["files"]
    if len(files) != 5 or sum(item["size_bytes"] for item in files) != manifest["total_weight_bytes"]:
        raise ValueError("ManifestSizeMismatch")
    if manifest["repository"] != "Comfy-Org/MiniMax-H3" or manifest["download_workers"] != 2:
        raise ValueError("UnexpectedManifestRepository")
    if manifest["revision"] != "e5eb578a89295337b8ff433a035929ce0279e0b6" or manifest["comfyui_revision"] != "e9027f2b30f37bb3052714eb08fcf479542f4fc0":
        raise ValueError("UnexpectedPinnedRevision")
    for item in files:
        rel = Path(item["path"])
        if rel.is_absolute() or ".." in rel.parts or rel.parts[0] not in {"diffusion_models", "text_encoders", "vae"}:
            raise ValueError("UnsafeManifestPath")
    return manifest


def json_write(path, value):
    with LOCK:
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)


def redact_public_output(value):
    # Subprocess diagnostics must never retain signed URL queries or URL auth.
    def clean(match):
        try:
            url = urllib.parse.urlsplit(match.group(0))
            host = url.hostname or "redacted-host"
            if url.port:
                host += ":" + str(url.port)
            return urllib.parse.urlunsplit((url.scheme, host, url.path, "", ""))
        except ValueError:
            return "[redacted URL]"
    value = re.sub(r'https?://[^\s<>"\']+', clean, value)
    value = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|authorization|password|secret)\s*[:=]\s*)[^\s,;]+', r'\1[redacted]', value)
    value = re.sub(r'(?i)\bBearer\s+[^\s,;]+', 'Bearer [redacted]', value)
    return value


class Setup:
    def __init__(self, root, manifest, cache_dir):
        self.root = root
        self.comfy = root / "ComfyUI"
        self.manifest = manifest
        self.cache_dir = cache_dir
        self.status = {
            "started_at": utc_now(), "updated_at": utc_now(), "state": "preparing",
            "phase": "preflight", "repository": manifest["repository"],
            "model_revision": manifest["revision"],
            "comfyui_revision": manifest["comfyui_revision"],
            "total_weight_bytes": manifest["total_weight_bytes"],
            "files": {}, "events": [], "generation_verified": False,
        }
        root.mkdir(parents=True, exist_ok=True)

    def event(self, phase, **details):
        record = {"at": utc_now(), "phase": phase, **details}
        with LOCK:
            self.status["phase"] = phase
            self.status["updated_at"] = record["at"]
            self.status["events"].append(record)
            with (self.root / "setup.log").open("a", encoding="utf-8") as log:
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
            json_write(self.root / "setup-status.json", self.status)
            print(json.dumps(record, ensure_ascii=False), flush=True)

    def run(self, command, phase, cwd=None, timeout=1800):
        # Commands are argument arrays; no shell interpolation or env dumps.
        self.event(phase, state="running")
        env = os.environ.copy()
        env["PIP_NO_INPUT"] = "1"
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, errors="replace")
        def copy_output():
            with (self.root / "setup.log").open("a", encoding="utf-8") as log:
                for line in process.stdout:
                    with LOCK:
                        log.write(redact_public_output(line))
                        log.flush()
        thread = threading.Thread(target=copy_output, daemon=True)
        thread.start()
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise SetupError("SubprocessTimeout") from None
        finally:
            thread.join(timeout=10)
        self.event(phase, state="completed" if code == 0 else "failed", exit_code=code)
        if code:
            raise SetupError("SubprocessFailed")

    def snapshot_runtime(self, filename):
        import torch
        if not torch.cuda.is_available():
            raise SetupError("CUDAUnavailable")
        props = torch.cuda.get_device_properties(0)
        runtime = {
            "at": utc_now(), "python": sys.version.split()[0],
            "python_executable": sys.executable, "torch": torch.__version__,
            "torch_cuda": torch.version.cuda, "gpu": props.name,
            "gpu_total_bytes": props.total_memory,
            "compute_capability": [props.major, props.minor],
        }
        try:
            import psutil
            ram = psutil.virtual_memory()
            runtime["host_ram_total_bytes"] = ram.total
            runtime["host_ram_available_bytes"] = ram.available
        except ImportError:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith(("MemTotal:", "MemAvailable:")):
                    runtime[line.split(":")[0]] = int(line.split()[1]) * 1024
        json_write(self.root / filename, runtime)
        return runtime

    def prepare_code(self):
        if not self.comfy.exists():
            self.run(["git", "clone", "--filter=blob:none", self.manifest["comfyui_repository"], str(self.comfy)], "clone_comfy")
        elif not (self.comfy / ".git").is_dir():
            raise SetupError("ExistingComfyDirectoryIsNotCheckout")
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=self.comfy,
                               capture_output=True, text=True, check=True).stdout.strip()
        if dirty:
            raise SetupError("ExistingComfyCheckoutModified")
        revision = self.manifest["comfyui_revision"]
        known = subprocess.run(["git", "cat-file", "-e", revision + "^{commit}"], cwd=self.comfy,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if known.returncode:
            self.run(["git", "fetch", "origin", revision], "fetch_comfy", cwd=self.comfy)
        self.run(["git", "checkout", "--detach", revision], "pin_comfy", cwd=self.comfy)
        actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.comfy,
                                capture_output=True, text=True, check=True).stdout.strip()
        if actual != revision:
            raise SetupError("ComfyRevisionMismatch")
        cli = (self.comfy / "comfy" / "cli_args.py").read_text(encoding="utf-8")
        for flag in ("--listen", "--port", "--disable-auto-launch", "--cache-none", "--disable-partner-nodes"):
            if flag not in cli:
                raise SetupError("RequiredComfyFlagMissing")

    def install_dependencies(self):
        before = self.snapshot_runtime("runtime-before.json")
        frozen = []
        for package in ("torch", "torchvision", "torchaudio"):
            try:
                frozen.append(package + "==" + importlib.metadata.version(package))
            except importlib.metadata.PackageNotFoundError:
                if package == "torch":
                    raise SetupError("ImageTorchMissing") from None
        constraints = self.root / "image-torch-constraints.txt"
        constraints.write_text("\n".join(frozen) + "\n", encoding="utf-8")
        self.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                  "--constraint", str(constraints), "--requirement", str(self.comfy / "requirements.txt"),
                  "huggingface_hub"], "install_dependencies", timeout=1800)
        after = self.snapshot_runtime("runtime-after.json")
        if (before["torch"], before["torch_cuda"]) != (after["torch"], after["torch_cuda"]):
            raise SetupError("ImageTorchChanged")
        packages = sorted(({"name": d.metadata.get("Name", "unknown"), "version": d.version}
                           for d in importlib.metadata.distributions()), key=lambda x: x["name"].lower())
        json_write(self.root / "packages.json", {"at": utc_now(), "packages": packages})

    def download_models(self):
        os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        # Hub >=1.33 refuses ordinary HTTP downloads above 50GB. The unpruned
        # transformers and BF16 encoder require Xet, with public token=False.
        os.environ["HF_HUB_DISABLE_XET"] = "0"
        os.environ["HF_HUB_VERBOSITY"] = "error"
        os.environ["HF_DEBUG"] = "0"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["HF_XET_HIGH_PERFORMANCE"] = "0"
        os.environ["HF_XET_FIXED_DOWNLOAD_CONCURRENCY"] = "8"
        os.environ["HF_XET_DATA_MAX_CONCURRENT_FILE_DOWNLOADS"] = "2"
        os.environ["HF_XET_LOG_DEST"] = os.devnull
        os.environ["HF_XET_LOG_FILE"] = os.devnull
        os.environ["RUST_LOG"] = "off"
        # Suppress downloader diagnostics that can contain signed redirect URLs.
        logging.disable(logging.CRITICAL)
        from huggingface_hub import hf_hub_download
        try:
            importlib.metadata.version("hf_xet")
        except importlib.metadata.PackageNotFoundError:
            raise SetupError("XetRequiredForLargeWeights") from None
        options = {"repo_id": self.manifest["repository"], "revision": self.manifest["revision"], "token": False}
        if self.cache_dir:
            options["cache_dir"] = str(self.cache_dir)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        cached = {}
        for item in self.manifest["files"]:
            try:
                path = Path(hf_hub_download(filename=item["path"], local_files_only=True, **options))
                if path.stat().st_size == item["size_bytes"]:
                    cached[item["path"]] = path
            except Exception:
                pass
        cache_base = self.cache_dir
        if cache_base is None:
            from huggingface_hub.constants import HF_HUB_CACHE
            cache_base = Path(HF_HUB_CACHE)
            cache_base.mkdir(parents=True, exist_ok=True)
        remaining = sum(x["size_bytes"] for x in self.manifest["files"] if x["path"] not in cached)
        free = shutil.disk_usage(cache_base).free
        self.event("download_preflight", cached_files=len(cached), remaining_bytes=remaining,
                   cache_free_bytes=free, download_workers=2, xet_download_concurrency=8,
                   transport="xet_public_no_api_token", cache_directory=str(cache_base))
        if free < remaining + 10 * GIB:
            raise SetupError("InsufficientCacheDiskSpace")

        def download(item):
            rel = item["path"]
            self.event("download_file", file=rel, state="cached" if rel in cached else "downloading", expected_bytes=item["size_bytes"])
            try:
                source = cached.get(rel)
                if source is None:
                    source = Path(hf_hub_download(filename=rel, **options))
                actual_size = source.stat().st_size
                if actual_size != item["size_bytes"]:
                    raise SetupError("DownloadedFileSizeMismatch")
                target = self.comfy / "models" / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                actual_source = source.resolve(strict=True)
                if target.exists() or target.is_symlink():
                    if not target.is_symlink() or target.resolve(strict=True) != actual_source:
                        raise SetupError("ExistingModelPathConflict")
                else:
                    target.symlink_to(actual_source)
                with LOCK:
                    self.status["files"][rel] = {
                        "state": "verified_size", "size_bytes": actual_size,
                        "cache_snapshot_path": str(source), "cache_actual_path": str(actual_source),
                        "comfy_model_symlink": str(target), "revision": self.manifest["revision"],
                        "sha256_recomputed": False,
                    }
                self.event("download_file", file=rel, state="complete", size_bytes=actual_size)
            except Exception as error:
                with LOCK:
                    self.status["files"][rel] = {"state": "failed", "error_type": type(error).__name__}
                diagnosis = {"error_type": type(error).__name__}
                if isinstance(error, SetupError):
                    diagnosis["error_code"] = error.code
                self.event("download_file", file=rel, state="failed", **diagnosis)
                raise SetupError("ModelDownloadFailed") from None
        failed = []
        with suppress_native_download_diagnostics():
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(download, item) for item in self.manifest["files"]]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception as error:
                        failed.append(type(error).__name__)
        if failed:
            raise SetupError("ModelDownloadFailed")

    @staticmethod
    def node_ready():
        try:
            with urllib.request.urlopen("http://127.0.0.1:8188/object_info/MiniMaxH3ReferenceToVideo", timeout=5) as response:
                info = json.loads(response.read(2 * 1024 * 1024))
            return "MiniMaxH3ReferenceToVideo" in info
        except Exception:
            return False

    def start_comfy(self, timeout):
        pidfile = self.root / "comfy.pid"
        if pidfile.exists():
            try:
                pid = int(pidfile.read_text().strip())
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                owned = str(self.comfy / "main.py").encode() in cmdline
                if owned and self.node_ready():
                    self.status.update(state="ready", comfy_pid=pid, reused_process=True)
                    self.event("comfy_ready", pid=pid, reused_process=True)
                    return
                if owned:
                    raise SetupError("ExistingOwnedComfyNotReady")
            except (ValueError, FileNotFoundError):
                pass
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", 8188)) == 0:
                raise SetupError("Port8188AlreadyInUse")
        command = [sys.executable, "-u", str(self.comfy / "main.py"), "--listen", "127.0.0.1", "--port", "8188",
                   "--disable-auto-launch", "--cache-none", "--disable-partner-nodes"]
        json_write(self.root / "comfy-command.json", {"argv": command, "working_directory": str(self.comfy)})
        env = os.environ.copy()
        env["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        with (self.root / "comfy.log").open("a", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=self.comfy, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        pidfile.write_text(str(process.pid) + "\n", encoding="ascii")
        self.status["comfy_pid"] = process.pid
        self.event("start_comfy", pid=process.pid, state="starting", listen="127.0.0.1:8188")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code = process.poll()
            if code is not None:
                self.event("start_comfy", state="failed", exit_code=code)
                raise SetupError("ComfyExitedBeforeReady")
            if self.node_ready():
                self.status.update(state="ready", reused_process=False)
                self.event("comfy_ready", pid=process.pid, generation_verified=False)
                return
            time.sleep(2)
        raise SetupError("ComfyStartupTimeout")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/workspace/h3-studio"))
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("model_manifest.json"))
    parser.add_argument("--cache-dir", type=Path, default=None, help="Optional existing/shared HF cache; otherwise use the host's current HF cache.")
    parser.add_argument("--skip-install", action="store_true", help="Retry after dependencies have already been installed.")
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--start-timeout", type=int, default=240)
    parser.add_argument("--check-manifest", action="store_true", help="Offline validation only, with no writes/downloads/startup.")
    args = parser.parse_args()
    manifest = read_manifest(args.manifest)
    if args.check_manifest:
        print(json.dumps({"valid": True, "file_count": len(manifest["files"]), "total_weight_bytes": manifest["total_weight_bytes"],
                          "model_revision": manifest["revision"], "comfyui_revision": manifest["comfyui_revision"]}))
        return 0
    if os.name != "posix" or not Path("/proc/meminfo").is_file():
        raise RuntimeError("LinuxCloudWorkerRequired")
    root = args.root.resolve()
    if root != Path("/workspace/h3-studio"):
        raise ValueError("UnexpectedDeploymentRoot")
    setup = Setup(root, manifest, args.cache_dir.resolve() if args.cache_dir else None)
    try:
        setup.event("preflight", state="running")
        setup.prepare_code()
        if args.skip_install:
            setup.snapshot_runtime("runtime-after.json")
        else:
            setup.install_dependencies()
        setup.download_models()
        if args.no_start:
            setup.status["state"] = "weights_ready"
            setup.event("weights_ready", generation_verified=False)
        else:
            setup.start_comfy(args.start_timeout)
        return 0
    except Exception as error:
        diagnosis = {"error_type": type(error).__name__}
        if isinstance(error, SetupError):
            diagnosis["error_code"] = error.code
        setup.status.update(state="failed", **diagnosis)
        setup.event("failed", **diagnosis)
        # Intentionally omit exception text/traceback: HF failures may include signed URLs.
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"state": "failed", "error_type": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
