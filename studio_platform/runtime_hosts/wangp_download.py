"""Bounded public model fetching; inert until explicitly invoked by bootstrap.

The already locked Hugging Face SDK owns transfer retries/partial-file formats.
This helper cannot start a model, change a manifest, rent a GPU or replay a job.
Downloaded bytes remain unqualified until the existing full runtime verifier.
"""
from __future__ import annotations

import argparse
import ctypes
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time

from ..inference.wangp_contract import EngineManifest

ENDPOINT = "https://huggingface.co"
MAX_FILES = 64
MAX_TOTAL = 256 * 1024**3
CACHE_LIMIT = 1024**3
HEADROOM = 10 * 1024**3
DOWNLOAD_TIMEOUT = 7200
SAFE_ERRORS = frozenset({"model_download_manifest_invalid", "model_download_manifest_changed",
    "model_download_path_invalid", "model_download_size_mismatch", "model_download_disk_headroom",
    "model_download_failed", "model_download_timeout", "model_download_cache_limit",
    "model_download_progress_invalid", "model_download_stop_unconfirmed", "model_download_state_exists",
    "model_download_owner_unconfirmed"})


def path_checked(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("model_download_path_invalid")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError("model_download_path_invalid")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError("model_download_path_invalid")
    return path


def selected_files(document, expected_digest):
    """Build an immutable exact-file list, never snapshot an entire repository."""
    try:
        manifest = EngineManifest.from_dict(document)
        if manifest.digest != expected_digest:
            raise ValueError("model_download_manifest_changed")
        records, names = [], set()
        for name, component in manifest.document["components"].items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
                raise ValueError()
            repo, revision = component.get("repository"), component["revision"]
            if (not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
                    or any(part in {".", ".."} for part in repo.split("/"))
                    or not re.fullmatch(r"[0-9a-f]{40}", revision)):
                raise ValueError()
            files = component.get("files")
            if not isinstance(files, list) or not files:
                raise ValueError()
            for item in files:
                filename = item["path"]
                relative = PurePosixPath(filename)
                if (not isinstance(filename, str) or not filename or relative.is_absolute()
                        or str(relative) != filename or any(x in {".", "..", ".cache"} for x in relative.parts)
                        or re.search(r"[\\:*?\[\]#%\x00-\x20]", filename) or filename in names
                        or type(item["size_bytes"]) is not int or not 0 < item["size_bytes"] <= MAX_TOTAL):
                    raise ValueError()
                key, width = ("sha256", 64) if "sha256" in item else ("git_blob_sha1", 40)
                if not re.fullmatch(r"[0-9a-f]{" + str(width) + r"}", item.get(key, "")):
                    raise ValueError()
                records.append((name, repo, revision, filename, item["size_bytes"]))
                names.add(filename)
        if not 0 < len(records) <= MAX_FILES or sum(row[4] for row in records) > MAX_TOTAL:
            raise ValueError()
        return tuple(records)
    except Exception as error:
        code = str(error)
        raise ValueError(code if code == "model_download_manifest_changed" else "model_download_manifest_invalid") from None


def safe_error(error):
    code = str(error)
    return code if code in SAFE_ERRORS else "model_download_failed"


def write_progress(path, value):
    path = path_checked(path)
    temporary = path_checked(path.with_suffix(".tmp"))
    raw = json.dumps(value, sort_keys=True, allow_nan=False).encode()
    if len(raw) > 8192:
        raise ValueError("model_download_progress_invalid")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600) if hasattr(os, "fchmod") else None
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def download_environment(environment, state_root):
    # No implicit API identity, custom endpoint or inherited SDK debug settings.
    state = path_checked(state_root)
    result = {key: value for key, value in environment.items()
              if not key.startswith(("HF_", "HUGGINGFACE_", "HUGGING_FACE_"))}
    result.update(HF_ENDPOINT=ENDPOINT, HF_HOME=str(state / "cache"),
        HF_HUB_CACHE=str(state / "cache" / "hub"), HF_XET_CACHE=str(state / "cache" / "xet"),
        HF_TOKEN_PATH=str(state / "no-token"), HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
        HF_HUB_OFFLINE="0", HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_PROGRESS_BARS="1",
        HF_HUB_VERBOSITY="error", HF_DEBUG="0", HF_HUB_DOWNLOAD_TIMEOUT="60",
        HF_HUB_ETAG_TIMEOUT="30", HF_XET_HIGH_PERFORMANCE="0",
        HF_XET_CHUNK_CACHE_SIZE_BYTES="0", HF_XET_SHARD_CACHE_SIZE_LIMIT="0",
        HF_XET_LOG_DEST=os.devnull, HF_XET_LOG_FILE=os.devnull, RUST_LOG="off",
        RUST_BACKTRACE="0", DO_NOT_TRACK="1", TRANSFORMERS_OFFLINE="1")
    return result


def cache_size(root):
    root = path_checked(root)
    if not root.exists():
        return 0
    size = count = 0
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = path_checked(Path(parent) / name)
            count += 1
            if count > 4096:
                raise ValueError("model_download_cache_limit")
            if path.is_file():
                size += path.stat().st_size
            if size > CACHE_LIMIT:
                raise ValueError("model_download_cache_limit")
    return size


def fetch_files(document, expected_digest, model_root, progress_path, *, workers=2,
                downloader=None, sleep=time.sleep):
    records = selected_files(document, expected_digest)
    if type(workers) is not int or not 1 <= workers <= 2:
        raise ValueError("model_download_manifest_invalid")
    root = path_checked(model_root)
    root.mkdir(parents=True, exist_ok=True)
    # Full incomplete objects may be preallocated by Xet. Do not report stat size
    # as received network bytes, or subtract it from this conservative disk gate.
    remaining = 0
    for record in records:
        target = path_checked(root / record[3])
        if target.exists():
            if not target.is_file() or target.stat().st_size != record[4]:
                raise ValueError("model_download_size_mismatch")
        else:
            remaining += record[4]
    if shutil.disk_usage(root).free < remaining + HEADROOM:
        raise ValueError("model_download_disk_headroom")
    if downloader is None:
        from huggingface_hub import hf_hub_download
        downloader = hf_hub_download
    lock = threading.Lock()
    completed = set()
    total = sum(row[4] for row in records)
    def progress(state, code=None):
        value = {"state": state, "manifest_digest": expected_digest,
                 "files_complete": len(completed), "files_total": len(records),
                 "bytes_complete": sum(row[4] for row in records if row[3] in completed),
                 "bytes_total": total, "inference_verified": False}
        if code:
            value["code"] = code
        write_progress(progress_path, value)
    progress("downloading")
    def fetch(record):
        _, repo, revision, filename, size = record
        expected = path_checked(root / filename)
        for attempt in range(2):
            try:
                returned = downloader(repo_id=repo, filename=filename, revision=revision,
                    local_dir=str(root), token=False, endpoint=ENDPOINT, force_download=False,
                    etag_timeout=30)
                path = path_checked(returned)
                if path != expected or not path.is_file():
                    raise ValueError("model_download_path_invalid")
                if path.stat().st_size != size:
                    raise ValueError("model_download_size_mismatch")
                break
            except Exception as error:
                # SDK errors may contain transient signed URLs. Never retain or
                # serialize them. Only a file transfer retries, not bootstrap.
                if str(error) in SAFE_ERRORS or attempt == 1:
                    raise ValueError(safe_error(error)) from None
                sleep(1)
        with lock:
            completed.add(filename)
            progress("downloading")
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(fetch, records))
    except Exception as error:
        with lock:
            progress("failed", safe_error(error))
        raise ValueError(safe_error(error)) from None
    progress("downloaded_unverified")


def stop_child(child):
    if child.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(child.pid, signal.SIGKILL)
            else:
                child.kill()
        except ProcessLookupError:
            pass
    try:
        child.wait(timeout=10)
    except Exception:
        raise ValueError("model_download_stop_unconfirmed") from None


def child_guard(owner_pid, timeout):
    """Linux kernel guards survive parent termination and native SDK stalls."""
    if (sys.platform != "linux" or type(owner_pid) is not int or owner_pid <= 1
            or type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= DOWNLOAD_TIMEOUT or os.getppid() != owner_pid):
        raise ValueError("model_download_owner_unconfirmed")
    libc = ctypes.CDLL(None, use_errno=True)
    # The child is a separate process group so the owner can stop/reap it. Bind
    # death to that original owner too; a session boundary must not orphan it.
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != owner_pid:
        raise ValueError("model_download_owner_unconfirmed")
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, timeout)


def run_download(python, source, manifest_path, expected_digest, model_root, state_root,
                 environment, progress, *, timeout=DOWNLOAD_TIMEOUT, workers=2,
                 popen=subprocess.Popen, clock=time.monotonic, sleep=time.sleep,
                 stop=stop_child):
    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= DOWNLOAD_TIMEOUT
            or type(workers) is not int or not 1 <= workers <= 2):
        raise ValueError("model_download_manifest_invalid")
    manifest_path, root, state = map(path_checked, (manifest_path, model_root, state_root))
    if manifest_path.stat().st_size > 1024**2:
        raise ValueError("model_download_manifest_invalid")
    records = selected_files(json.loads(manifest_path.read_text()), expected_digest)
    root.mkdir(parents=True, exist_ok=True)
    try:
        state.mkdir(mode=0o700, parents=False, exist_ok=False)
    except FileExistsError:
        raise ValueError("model_download_state_exists") from None
    receipt = state / "progress.json"
    command = [str(python), "-m", "studio_platform.runtime_hosts.wangp_download",
               "--manifest", str(manifest_path), "--expected-digest", expected_digest,
               "--model-root", str(root), "--progress", str(receipt), "--workers", str(workers),
               "--owner-pid", str(os.getpid()), "--timeout", str(timeout)]
    deadline = clock() + timeout
    child = None
    last = None
    try:
        child = popen(command, cwd=str(path_checked(source)), env=download_environment(environment, state),
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      start_new_session=True)
        while True:
            if clock() >= deadline:
                raise ValueError("model_download_timeout")
            cache_size(state / "cache")
            if shutil.disk_usage(root).free < HEADROOM:
                raise ValueError("model_download_disk_headroom")
            value = None
            if receipt.exists():
                path_checked(receipt)
                if receipt.stat().st_size > 8192:
                    raise ValueError("model_download_progress_invalid")
                value = json.loads(receipt.read_text())
                if (set(value) - {"state", "manifest_digest", "files_complete", "files_total", "bytes_complete",
                                  "bytes_total", "inference_verified", "code"}
                        or value.get("state") not in {"downloading", "downloaded_unverified", "failed"}
                        or value.get("manifest_digest") != expected_digest or value.get("inference_verified") is not False
                        or value.get("files_total") != len(records) or value.get("bytes_total") != sum(r[4] for r in records)
                        or type(value.get("files_complete")) is not int or not 0 <= value["files_complete"] <= len(records)
                        or type(value.get("bytes_complete")) is not int or not 0 <= value["bytes_complete"] <= value["bytes_total"]
                        or value.get("code", "model_download_failed") not in SAFE_ERRORS):
                    raise ValueError("model_download_progress_invalid")
                if value != last:
                    progress("model_download", files_complete=value["files_complete"], files_total=value["files_total"],
                             bytes_complete=value["bytes_complete"], bytes_total=value["bytes_total"])
                    last = value
            result = child.poll()
            if result is not None:
                child.wait(timeout=10)
                if (result != 0 or value is None or value["state"] != "downloaded_unverified"
                        or value["files_complete"] != len(records) or value["bytes_complete"] != value["bytes_total"]):
                    raise ValueError(value.get("code", "model_download_failed") if value else "model_download_failed")
                # The existing verify-only and owned-launch checks still hash
                # every model. A downloader receipt never certifies model bytes.
                return value
            sleep(.25)
    except BaseException:
        if child is not None:
            stop(child)
        raise


def main(argv=None, *, guard=child_guard):
    parser = argparse.ArgumentParser(description="Pinned public model preparation only")
    for name in ("manifest", "model-root", "progress"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=DOWNLOAD_TIMEOUT)
    args = parser.parse_args(argv)
    prior_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        guard(args.owner_pid, args.timeout)
        manifest = path_checked(args.manifest)
        if manifest.stat().st_size > 1024**2:
            raise ValueError("model_download_manifest_invalid")
        # Production also redirects file descriptors, covering native SDK logs.
        with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            fetch_files(json.loads(manifest.read_text()), args.expected_digest,
                        args.model_root, args.progress, workers=args.workers)
        return 0
    except Exception as error:
        # Preserve only static codes. No raw exception, URLs or credentials.
        try:
            records = selected_files(json.loads(args.manifest.read_text()), args.expected_digest)
            write_progress(args.progress, {"state": "failed", "manifest_digest": args.expected_digest,
                "files_complete": 0, "files_total": len(records), "bytes_complete": 0,
                "bytes_total": sum(r[4] for r in records), "inference_verified": False, "code": safe_error(error)})
        except Exception:
            pass
        return 1
    finally:
        logging.disable(prior_logging)


if __name__ == "__main__":
    raise SystemExit(main())
