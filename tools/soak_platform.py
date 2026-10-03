"""Bounded real-time CPU queue/worker soak, explicitly NOT an H3 benchmark.

Creates a new isolated .platform-soak-* directory; never opens legacy user data,
cloud credentials, provider APIs or a GPU. Keeps all generated evidence locally.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from studio_platform.capabilities import compile_request
from studio_platform.control import WorkerControl
from studio_platform.repository import Repository, Scope, NotFound
from studio_platform.storage import LocalObjectStore


def save(path, data):
    temporary = path.with_suffix(".next")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def run(root, *, hours, interval, poll):
    root = root.resolve()
    if root.parent != ROOT or not root.name.startswith(".platform-soak-") or root.exists():
        raise ValueError("Use a fresh dedicated .platform-soak-* child directory")
    if not 0 < hours <= 8 or not 30 <= interval <= 3600 or not 1 <= poll <= 30:
        raise ValueError("Invalid bounded soak duration or interval")
    root.mkdir(mode=0o700)
    repo = Repository("sqlite:///" + (root / "platform.sqlite3").as_posix())
    repo.create_schema()
    repo.configure_capacity()
    control = WorkerControl(repo)
    store = LocalObjectStore(root / "objects")
    started = time.time()
    status = {"kind": "CPU_SIMULATION_NOT_H3", "started_at": started, "deadline": started+hours*3600,
              "status": "running", "cloud_calls": 0, "gpu_calls": 0, "bursts": 0,
              "submitted": 0, "verified": 0, "worker_restarts": 0, "failures": [], "samples": []}
    handles, processes, job_ids, verified_ids = [], {}, [], set()
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("SIXNINE_"):
            del env[key]
    env.update(SIXNINE_DATA=str(root), SIXNINE_AUTH_MODE="local-test", SIXNINE_EXECUTION_BACKEND="mock",
               SIXNINE_GENERATION_ENABLED="1", SIXNINE_STORAGE_PROVIDER="local", PYTHONUNBUFFERED="1")

    def start(worker_id):
        out = (root / (worker_id+".log")).open("ab")
        err = (root / (worker_id+"-error.log")).open("ab")
        handles.extend((out, err))
        command = [sys.executable, "-m", "studio_platform.worker", "--backend", "mock", "--worker-id", worker_id,
            "--pool", "mock", "--work-dir", str(root / worker_id), "--data-dir", str(root)]
        child = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        processes[worker_id] = child

    def stop(worker_id):
        child = processes[worker_id]
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=45)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)

    try:
        for worker in ("soak-cpu-one", "soak-cpu-two"):
            start(worker)
        next_burst, next_restart, index = started+5, started+3600, 0
        while time.time() < status["deadline"]:
            now = time.time()
            for worker_id, process in processes.items():
                if process.poll() is not None:
                    raise RuntimeError("CPU worker exited unexpectedly")
                try:
                    state = control.get(worker_id)
                except NotFound:
                    if now-started < 30:
                        continue
                    raise
                if now-started > 30 and (state["expires_at"] <= now or state["state"] in {"unknown", "retired"}):
                    raise RuntimeError("Registered CPU worker lost idle heartbeat")
            if now >= next_burst:
                for owner in ("superdan", "supervan"):
                    scope = Scope("soak-only", owner, "soak-story")
                    compiled, fingerprint = compile_request({"client_ref": {"project_id": "soak-story", "shot_id": "soak-shot", "shot_version": 1},
                        "recipe_id": "h3-base-fl2va-v1", "prompt": "Synthetic CPU stability sample, no user material",
                        "controls": {"duration": 4, "resolution": "480P", "generate_audio": bool(index % 2), "seed": str(index+1)}}, lambda _: None)
                    plan = repo.create_plan(scope, compiled, {"pool": "mock", "backend": "mock", "enabled": True,
                        "quote_known": True, "expected_runtime_s": 5, "fingerprint": fingerprint}, expires_at=now+900)
                    job = repo.create_job(scope, plan["id"], "soak-burst-"+str(index))
                    duplicate = repo.create_job(scope, plan["id"], "soak-burst-"+str(index))
                    if duplicate["id"] != job["id"] or duplicate["created"]:
                        raise RuntimeError("Idempotency failed during real-time soak")
                    job_ids.append((scope, job["id"], now))
                    status["submitted"] += 1
                index += 1
                status["bursts"] += 1
                next_burst = now+interval
            for scope, job_id, submitted in job_ids:
                if job_id in verified_ids:
                    continue
                job = repo.get_job(scope, job_id)
                if job["status"] in {"failed", "cancelled", "submission_unknown"}:
                    raise RuntimeError("CPU sample entered an unexpected state")
                if now-submitted > 180:
                    raise RuntimeError("CPU sample exceeded local stability timeout")
                if job["status"] == "succeeded":
                    values = repo.list_artifacts(scope, job_id)
                    if not any(value["metadata"]["kind"] == "video" for value in values):
                        raise RuntimeError("Completed CPU sample has no video")
                    for value in values:
                        metadata = value["metadata"]
                        digest, count = hashlib.sha256(), 0
                        with store.open(metadata["object_key"]) as source:
                            for chunk in iter(lambda: source.read(1024*1024), b""):
                                digest.update(chunk)
                                count += len(chunk)
                        if count != metadata["size_bytes"] or digest.hexdigest() != metadata["sha256"]:
                            raise RuntimeError("CPU artifact read-back checksum failed")
                    verified_ids.add(job_id)
                    status["verified"] += 1
            # Restart only our known, idle subprocesses. No user services touched.
            if now >= next_restart and len(verified_ids) == len(job_ids):
                worker_id = "soak-cpu-one" if status["worker_restarts"] % 2 == 0 else "soak-cpu-two"
                stop(worker_id)
                start(worker_id)
                status["worker_restarts"] += 1
                next_restart = now+3600
            status["observed_at"] = now
            status["samples"].append({"at": now, "verified": status["verified"], "submitted": status["submitted"]})
            # Bounded rolling observations; job/artifact ledger remains complete.
            status["samples"] = status["samples"][-480:]
            save(root / "status.json", status)
            time.sleep(poll)
        status["status"] = "passed" if len(verified_ids) == len(job_ids) else "incomplete"
    except Exception as error:
        status["status"] = "failed"
        status["failures"].append({"at": time.time(), "type": type(error).__name__})
        # Exceptions are deliberately not serialized: provider/DSN/media errors
        # in future versions may contain credential or user-content information.
    finally:
        for worker_id in processes:
            stop(worker_id)
        for handle in handles:
            handle.close()
        status["completed_at"] = time.time()
        save(root / "status.json", status)
        repo.close()
    return 0 if status["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--hours", type=float, default=6)
    parser.add_argument("--interval", type=float, default=1200)
    parser.add_argument("--poll", type=float, default=15)
    args = parser.parse_args()
    raise SystemExit(run(args.directory, hours=args.hours, interval=args.interval, poll=args.poll))
