"""Explicit bounded GPU import probe; never submits a task or loads model weights."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import importlib
import json
import os
from pathlib import Path
import socket
import sys
import time


def probe(runtime_root):
    from studio_platform.runtime_hosts.wangp_environment import (
        digest, regular_file, validate_lock, verify_environment, verify_source)
    from studio_platform.runtime_hosts.wangp_session import CORE_VERSIONS
    import importlib.metadata
    runtime_root = Path(runtime_root).resolve(strict=True)
    lock = validate_lock(json.loads(regular_file(runtime_root, ".sixnine-environment.json").read_text(encoding="utf-8")))
    verify_source(runtime_root, lock)
    evidence = verify_environment(lock)
    if any(importlib.metadata.version(name) != version for name, version in CORE_VERSIONS.items()):
        raise ValueError("core_runtime_version_mismatch")
    import torch
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise ValueError("single_cuda_gpu_required")
    device = torch.cuda.get_device_properties(0)
    original_connect, original_create = socket.socket.connect, socket.create_connection
    previous_cwd, previous_argv = os.getcwd(), sys.argv
    def refuse_network(*args, **kwargs):
        raise RuntimeError("network_forbidden_during_import_probe")
    # Import-time model/helper downloads are a failure to fix explicitly, never
    # permission to change the declared package or acquire additional weights.
    socket.socket.connect = refuse_network
    socket.create_connection = refuse_network
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    sys.path.insert(0, str(runtime_root))
    sys.argv = ["wgp.py", "--attention", "sdpa", "--profile", "4"]
    try:
        os.chdir(runtime_root)
        importlib.import_module("wgp")
        importlib.import_module("models.minimax_h3.pipeline")
        torch.cuda.synchronize()
    finally:
        socket.socket.connect, socket.create_connection = original_connect, original_create
        os.chdir(previous_cwd)
        sys.argv = previous_argv
    return {"state": "imports_verified", "recorded_unix": time.time(),
            "environment_lock_sha256": digest(lock), "source_revision": lock["source_revision"],
            "python": lock["python"], "torch": torch.__version__, "torch_cuda": torch.version.cuda,
            "gpu": {"name": device.name, "total_bytes": device.total_memory,
                    "compute_capability": [device.major, device.minor]},
            "environment": evidence, "inference_verified": False,
            "model_weights_loaded": False, "generation_submitted": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output = Path(args.output)
    if output.exists():
        raise ValueError("probe_receipt_exists")
    try:
        # Upstream stdout is not the protocol and may contain arbitrary paths.
        with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            value = probe(args.runtime_root)
    except ModuleNotFoundError as error:
        value = {"state": "import_failed", "code": "missing_module", "module": error.name,
                 "inference_verified": False}
    except Exception as error:
        value = {"state": "import_failed", "code": type(error).__name__, "inference_verified": False}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as target:
        json.dump(value, target, sort_keys=True)
    output.chmod(0o600)
    print(json.dumps(value))
    return 0 if value["state"] == "imports_verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
