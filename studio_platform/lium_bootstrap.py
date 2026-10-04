"""Explicit CPU-side boot/qualification for already-reserved single-GPU pods.

This module NEVER rents, deletes, changes a budget, or copies CPU credentials to
GPU hosts. ScaleCoordinator owns those actions. The controller must run beside
the CPU fleet on one durable host; its lock and receipts live in work_dir.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import select as io_select
import shlex
import socketserver
import threading
import time

from sqlalchemy import select

from .control import WorkerSpec
from .fleet import FleetConfig, FleetSupervisor, SlotConfig
from .lium_provider import InferenceIdleProof, _uuid
from .repository import Conflict, instance_intents
from .worker import ComfyBackend, SubmissionRejected, _slot_lock

REMOTE_ROOT = "/workspace/h3-studio"
MODEL_REVISION = "e5eb578a89295337b8ff433a035929ce0279e0b6"
COMFY_REVISION = "e9027f2b30f37bb3052714eb08fcf479542f4fc0"


class BootError(Conflict):
    pass


@dataclass(frozen=True)
class BootConfig:
    work_dir: Path
    source_dir: Path
    ssh_key_file: Path
    known_hosts_file: Path
    local_port: int
    configuration_id: str
    model_id: str = "MiniMax-H3-Base-BF16"
    min_gpu_bytes: int = 90*1024**3
    enabled: bool = False
    trust_first_host_key: bool = False
    smoke_enabled: bool = False
    fleet_enabled: bool = False
    recipe_ids: tuple[str, ...] = ("h3-base-fl2va-v1",)
    minimum_remaining_s: int = 1200

    def __post_init__(self):
        for field in ("work_dir", "source_dir", "ssh_key_file", "known_hosts_file"):
            if not Path(getattr(self, field)).is_absolute():
                raise ValueError("bootstrap_paths_must_be_absolute")
            object.__setattr__(self, field, Path(getattr(self, field)))
        if type(self.local_port) is not int or not 1024 <= self.local_port <= 65535:
            raise ValueError("invalid_bootstrap_local_port")
        for field in ("enabled", "trust_first_host_key", "smoke_enabled", "fleet_enabled"):
            if type(getattr(self, field)) is not bool:
                raise ValueError("invalid_bootstrap_switch")
        if (not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", self.configuration_id)
                or self.model_id != "MiniMax-H3-Base-BF16"
                or type(self.min_gpu_bytes) is not int or self.min_gpu_bytes < 30*1024**3
                or type(self.minimum_remaining_s) is not int or not 120 <= self.minimum_remaining_s <= 7200
                or self.recipe_ids not in (("h3-base-fl2va-v1",), ("h3-base-fl2va-v1", "h3-base-ref2va-v1"))):
            raise ValueError("bootstrap_configuration_requires_explicit_fl2va_smoke_envelope")
        if self.fleet_enabled and not self.smoke_enabled:
            raise ValueError("fleet_requires_successful_smoke")


class SSHHost:
    """Paramiko private-key use stays inside the library; no secret serialization."""
    def __init__(self, config, coordinates):
        import paramiko
        self.client = paramiko.SSHClient()
        self.tunnel = None
        config.known_hosts_file.parent.mkdir(parents=True, exist_ok=True)
        if config.known_hosts_file.exists():
            self.client.load_host_keys(str(config.known_hosts_file))
        else:
            config.known_hosts_file.touch(mode=0o600)
            self.client.load_host_keys(str(config.known_hosts_file))
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy() if config.trust_first_host_key else paramiko.RejectPolicy())
        try:
            self.client.connect(coordinates["host"], port=coordinates["port"], username="root",
                key_filename=str(config.ssh_key_file), look_for_keys=False, allow_agent=False,
                timeout=15, banner_timeout=15, auth_timeout=15)
        except Exception:
            self.client.close()
            raise BootError("bootstrap_ssh_unavailable_or_host_key_untrusted") from None

    def run(self, script, *, limit=4*1024*1024, timeout=25):
        channel = None
        try:
            channel = self.client.get_transport().open_session(timeout=15)
            channel.exec_command("python3 -c "+shlex.quote(script))
            output, started = bytearray(), time.monotonic()
            while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
                if time.monotonic()-started > timeout:
                    raise BootError("bootstrap_ssh_command_timeout")
                if channel.recv_ready():
                    output.extend(channel.recv(65536))
                    if len(output) > limit:
                        raise BootError("bootstrap_report_too_large")
                if channel.recv_stderr_ready():
                    channel.recv_stderr(65536)  # Never retain upstream diagnostics.
                time.sleep(.02)
            if channel.recv_exit_status() != 0:
                raise BootError("bootstrap_remote_command_failed")
            return json.loads(output)
        except BootError:
            raise
        except Exception:
            raise BootError("bootstrap_remote_response_unconfirmed") from None
        finally:
            if channel is not None:
                channel.close()

    def upload(self, files):
        self.run("from pathlib import Path; import json; Path('/workspace/h3-studio').mkdir(parents=True,exist_ok=True); print(json.dumps({'ok':True}))")
        with self.client.open_sftp() as sftp:
            for name, content in files.items():
                if name not in {"bootstrap_cloud.py", "model_manifest.json"}:
                    raise BootError("bootstrap_upload_file_not_allowlisted")
                target = REMOTE_ROOT+"/"+name
                try:
                    with sftp.open(target, "rb") as remote:
                        existing = remote.read(len(content)+1)
                    if existing != content:
                        raise BootError("bootstrap_existing_source_mismatch")
                except FileNotFoundError:
                    with sftp.open(target, "wx") as remote:
                        remote.write(content)

    def start(self, identity):
        # Persistent marker is created before Popen. A lost launch result never
        # causes this method to be called again by BootController.
        script = '''import fcntl,json,os,subprocess,sys
from pathlib import Path
root=Path('/workspace/h3-studio')
with (root/'sixnine-bootstrap.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 marker=root/'sixnine-bootstrap-identity.json'
 expected=IDENTITY
 if marker.exists():
  if json.loads(marker.read_text())!=expected: raise RuntimeError('identity_conflict')
  print(json.dumps({'state':'already_reserved'}))
 else:
  # Provider images may expose PEP 668 managed system Python. Reuse their
  # installed CUDA/Torch via system-site-packages in our dedicated environment.
  python=root/'.venv/bin/python'
  if not python.exists():
   subprocess.check_call([sys.executable,'-m','venv','--system-site-packages',str(root/'.venv')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  with marker.open('x') as out:
   json.dump(expected,out);out.flush();os.fsync(out.fileno())
  with (root/'bootstrap-controller.log').open('ab') as log:
   proc=subprocess.Popen([str(python),'-u',str(root/'bootstrap_cloud.py'),'--cache-dir','/workspace/hf-cache'],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
  print(json.dumps({'state':'started','pid':proc.pid}))
'''.replace("IDENTITY", repr(identity))
        return self.run(script)

    def report(self):
        script = '''import json,subprocess
from pathlib import Path
r=Path('/workspace/h3-studio')
def read(name):
 p=r/name
 return json.loads(p.read_text()) if p.exists() and p.stat().st_size<4194304 else {}
s=read('setup-status.json');runtime=read('runtime-after.json')
result={'identity':read('sixnine-bootstrap-identity.json'),'state':s.get('state'),'phase':s.get('phase'),'model_revision':s.get('model_revision'),'comfyui_revision':s.get('comfyui_revision'),'files':s.get('files',{}),'runtime':runtime}
if result['state']=='ready':
 result['actual_comfy_revision']=subprocess.check_output(['git','-C',str(r/'ComfyUI'),'rev-parse','HEAD'],text=True).strip()
 rows=subprocess.check_output(['nvidia-smi','--query-gpu=uuid,memory.total,name','--format=csv,noheader,nounits'],text=True).strip().splitlines()
 result['gpus']=[{'uuid':x.split(',')[0].strip(),'memory_mib':int(x.split(',')[1].strip()),'name':','.join(x.split(',')[2:]).strip()} for x in rows]
print(json.dumps(result))
'''
        return self.run(script)

    def open_tunnel(self, port):
        if self.tunnel:
            return
        transport = self.client.get_transport()
        gate = threading.BoundedSemaphore(8)
        class Handler(socketserver.BaseRequestHandler):
            def handle(inner):
                if not gate.acquire(blocking=False):
                    return
                channel = None
                try:
                    channel = transport.open_channel("direct-tcpip", ("127.0.0.1", 8188), inner.request.getpeername(), timeout=15)
                    while transport.is_active():
                        readable, _, _ = io_select.select([inner.request, channel], [], [], 10)
                        for source, destination in ((inner.request, channel), (channel, inner.request)):
                            if source in readable:
                                chunk = source.recv(65536)
                                if not chunk:
                                    return
                                destination.sendall(chunk)
                except Exception:
                    pass
                finally:
                    if channel is not None:
                        channel.close()
                    gate.release()
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False
        try:
            self.tunnel = Server(("127.0.0.1", port), Handler)
        except OSError:
            raise BootError("bootstrap_tunnel_port_already_in_use") from None
        threading.Thread(target=self.tunnel.serve_forever, daemon=True).start()

    def close(self):
        if self.tunnel:
            self.tunnel.shutdown()
            self.tunnel.server_close()
        self.client.close()


class BootController:
    def __init__(self, repository, provider, config: BootConfig, *, ssh_factory=SSHHost,
                 backend_factory=ComfyBackend, fleet_factory=FleetSupervisor, verify_smoke=None):
        self.repo, self.provider, self.config = repository, provider, config
        self.ssh_factory, self.backend_factory, self.fleet_factory = ssh_factory, backend_factory, fleet_factory
        self.verify_smoke = verify_smoke or self._verify_smoke
        self.host, self.backend, self.fleet = None, None, None
        self.bound_intent = None
        self.bound_instance = None
        self.idle_since = None

    def _save(self, path, state):
        state["updated_at"] = self.repo.clock()
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)

    def _sources(self):
        files = {}
        for name, maximum in (("bootstrap_cloud.py", 512*1024), ("model_manifest.json", 64*1024)):
            path = self.config.source_dir/name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
                raise BootError("bootstrap_source_missing_or_untrusted")
            files[name] = path.read_bytes()
        manifest = json.loads(files["model_manifest.json"])
        if (manifest.get("revision") != MODEL_REVISION or manifest.get("comfyui_revision") != COMFY_REVISION
                or manifest.get("repository") != "Comfy-Org/MiniMax-H3" or len(manifest.get("files", [])) != 5):
            raise BootError("bootstrap_manifest_revision_mismatch")
        return files, manifest

    def tick(self, intent_id):
        if not self.config.enabled:
            return {"state": "disabled", "provider_calls_enabled": False}
        _uuid(intent_id)
        if self.bound_intent not in (None, intent_id):
            raise BootError("bootstrap_controller_is_bound_to_one_intent")
        self.bound_intent = intent_id
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        with _slot_lock(self.config.work_dir, "bootstrap-"+intent_id) as acquired:
            if not acquired:
                return {"state": "bootstrap_locked"}
            with self.repo.engine.connect() as conn:
                intent = conn.execute(select(instance_intents).where(instance_intents.c.id == intent_id)).mappings().first()
            if intent is None or intent["provider"] != "lium" or not intent["provider_instance_id"] or intent["physical_gpus"] != 1:
                raise BootError("bootstrap_requires_reserved_single_gpu_lium_intent")
            self.bound_instance = intent["provider_instance_id"]
            if intent["state"] in {"draining", "destroying", "destroyed"}:
                if self.fleet:
                    self.fleet.drain()
                return {"state": "instance_not_admitting"}
            if intent["state"] not in {"starting", "ready", "busy"}:
                return {"state": "instance_not_confirmed"}
            if intent["hard_deadline"]-self.repo.clock() < self.config.minimum_remaining_s:
                if self.fleet:
                    self.fleet.drain()
                return {"state": "bootstrap_deadline_insufficient"}
            files, manifest = self._sources()
            identity = {"intent_id": intent_id, "instance_id": intent["provider_instance_id"],
                "configuration_id": self.config.configuration_id,
                "sources": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}}
            directory = self.config.work_dir/intent_id
            directory.mkdir(exist_ok=True)
            receipt = directory/"bootstrap-state.json"
            state = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else {
                "identity": identity, "phase": "reserved", "tag": "boot-"+intent_id.replace("-", ""),
                "created_at": self.repo.clock(), "local_port": self.config.local_port}
            if state.get("identity") != identity or state.get("local_port") != self.config.local_port:
                raise BootError("bootstrap_receipt_identity_conflict")
            if not self.host:
                coordinates = self.provider.ssh_connection(intent_id, intent["provider_instance_id"])
                self.host = self.ssh_factory(self.config, coordinates)
            if state["phase"] == "reserved":
                self.host.upload(files)
                state["phase"] = "bootstrap_starting"
                self._save(receipt, state)
                try:
                    self.host.start(identity)
                    state["phase"] = "booting"
                    self._save(receipt, state)
                except Exception:
                    return {"state": "bootstrap_start_unknown"}
            report = self.host.report()
            if report.get("identity") != identity:
                return {"state": "bootstrap_start_unknown"}
            if report.get("state") == "failed":
                state["phase"] = "bootstrap_failed"
                self._save(receipt, state)
                return {"state": "bootstrap_failed"}
            if report.get("state") != "ready":
                return {"state": "booting", "phase": report.get("phase")}
            self._validate_report(report, manifest)
            state["hardware"] = {"gpu": report["gpus"][0], "runtime": report["runtime"],
                "model_revision": MODEL_REVISION, "comfy_revision": COMFY_REVISION,
                "weight_verification": "pinned_cache_revision_and_exact_sizes_not_full_rehash"}
            self.host.open_tunnel(self.config.local_port)
            if self.backend is None:
                endpoint = f"http://127.0.0.1:{self.config.local_port}"
                self.backend = self.backend_factory(endpoint=endpoint, enabled=True, allowed_origins=(endpoint,), comfy_revision=COMFY_REVISION)
            if not self.config.smoke_enabled:
                if state["phase"] not in {"qualified", "fleet_starting", "fleet_started"}:
                    state["phase"] = "ready_for_qualification"
                self._save(receipt, state)
                return {"state": "ready_for_qualification", "generation_verified": False}
            result = self._smoke(directory, receipt, state)
            if result["state"] != "qualified":
                return result
            additional = self._additional_qualification(directory, state)
            if additional["state"] != "qualified":
                return additional
            result.update({k: v for k, v in additional.items() if k != "state"})
            if not self.config.fleet_enabled:
                return result
            if self.fleet is None:
                if state["phase"] in {"fleet_starting", "fleet_started"}:
                    return {"state": "fleet_recovery_required", "generation_verified": True}
                worker_id = "lium-"+intent_id.replace("-", "")
                endpoint = f"http://127.0.0.1:{self.config.local_port}"
                spec = WorkerSpec(worker_id, intent["pool"], "lium", intent["provider_instance_id"],
                    (report["gpus"][0]["uuid"],), self.config.recipe_ids, self.config.model_id, self.config.configuration_id)
                slot = SlotConfig(spec, True, endpoint, (endpoint,), COMFY_REVISION, True)
                config = FleetConfig(directory/"fleet", (slot,), True, 1)
                cfg_path = directory/"fleet.json"
                value = {"version": 1, "work_dir": str(config.work_dir), "enabled": True, "max_children": 1,
                    "shutdown_grace_s": config.shutdown_grace_s, "slots": [{**asdict(spec), "enabled": True,
                        "endpoint": endpoint, "allowed_origins": [endpoint], "comfy_revision": COMFY_REVISION, "confirmed_idle": True}]}
                cfg_path.write_text(json.dumps(value), encoding="utf-8")
                self.fleet = self.fleet_factory(config, self.repo, cfg_path)
                state["fleet_recipe_ids"] = list(self.config.recipe_ids)
                state["phase"] = "fleet_starting"
                self._save(receipt, state)
                self.fleet.start()
                state["phase"] = "fleet_started"
                self._save(receipt, state)
            fleet_status = self.fleet.tick()
            attention = any(child.get("state") == "exited" for child in fleet_status.get("children", []))
            return {"state": "fleet_attention_required" if attention else "fleet_running", "fleet": fleet_status, "generation_verified": True,
                "qualification_scope": "single_host_fl2va_4s_480p_audio_smoke_only"}

    def _additional_qualification(self, directory, state):
        """Historical base smoke remains unchanged; production overrides this."""
        if "h3-base-ref2va-v1" not in self.config.recipe_ids:
            return {"state": "qualified"}
        if state["phase"] in {"fleet_starting", "fleet_started"}:
            if state.get("fleet_recipe_ids", ["h3-base-fl2va-v1"]) != list(self.config.recipe_ids):
                raise BootError("fleet_recipe_change_requires_explicit_drain_and_new_configuration")
            receipt = directory/"reference-smoke"/"state.json"
            if not receipt.exists() or json.loads(receipt.read_text()).get("phase") != "qualified":
                raise BootError("fleet_reference_evidence_missing_requires_reconciliation")
        from .lium_reference_smoke import ReferenceSmoke
        result = ReferenceSmoke(self.backend, self.repo.clock, self._save, self.verify_smoke).tick(directory, state)
        return {"state": "qualified", "reference_evidence": result["evidence"]} if result["state"] == "qualified" else result

    def _validate_report(self, report, manifest):
        if (report.get("model_revision") != MODEL_REVISION or report.get("comfyui_revision") != COMFY_REVISION
                or report.get("actual_comfy_revision") != COMFY_REVISION):
            raise BootError("bootstrap_runtime_revision_mismatch")
        for item in manifest["files"]:
            actual = report.get("files", {}).get(item["path"], {})
            if actual.get("state") != "verified_size" or actual.get("revision") != MODEL_REVISION or actual.get("size_bytes") != item["size_bytes"]:
                raise BootError("bootstrap_weights_not_verified")
        gpus = report.get("gpus", [])
        runtime = report.get("runtime", {})
        if (not isinstance(gpus, list) or len(gpus) != 1 or not isinstance(gpus[0], dict)
                or not re.fullmatch(r"GPU-[A-Za-z0-9-]{8,100}", str(gpus[0].get("uuid", "")))
                or type(runtime.get("gpu_total_bytes")) is not int or runtime["gpu_total_bytes"] < self.config.min_gpu_bytes):
            raise BootError("bootstrap_gpu_identity_or_memory_mismatch")

    def _smoke(self, directory, receipt, state):
        from comfy_workflow import build_workflow
        request = {"mode": "fl", "prompt": "A red ceramic teapot on a wooden table, slow cinematic camera move, gentle ambient sound.",
            "duration": 4, "resolution": "480P", "aspect_ratio": "16:9", "steps": 4, "seed": "12345", "generate_audio": True,
            "video_decode": "tiled", "encoder_device": "cpu", "_job_id": state["tag"]}
        if state["phase"] in {"qualified", "fleet_starting", "fleet_started"}:
            return {"state": "qualified", "generation_verified": True, "evidence": state["evidence"]}
        if not state.get("smoke_submission_started"):
            queue = self.backend._json("GET", "/queue")
            if queue.get("queue_running") != [] or queue.get("queue_pending") != []:
                return {"state": "qualification_upstream_busy"}
            graph = build_workflow(request, {}, {})
            state.update(smoke_submission_started=self.repo.clock(), phase="smoke_submitting")
            self._save(receipt, state)
            try:
                state["smoke_task_id"] = self.backend.submit(graph, state["tag"])
                state["phase"] = "smoke_running"
                self._save(receipt, state)
            except SubmissionRejected:
                state["phase"] = "qualification_failed"
                self._save(receipt, state)
                return {"state": "qualification_failed"}
            except Exception:
                return {"state": "smoke_submission_unknown"}
        if state["phase"] == "qualification_failed":
            return {"state": "qualification_failed"}
        task_id = state.get("smoke_task_id")
        outcome = self.backend.poll(state["tag"], task_id) if task_id else self.backend.reconcile(state["tag"])
        if outcome.task_id and not task_id:
            state["smoke_task_id"] = task_id = outcome.task_id
            self._save(receipt, state)
        if outcome.state in {"failed", "cancelled"}:
            state["phase"] = "qualification_failed"
            self._save(receipt, state)
            return {"state": "qualification_failed"}
        if outcome.state != "succeeded" or not task_id:
            return {"state": "smoke_running" if outcome.state == "running" else "smoke_submission_unknown"}
        paths = self.backend.fetch({"request": {"request": request}}, state["tag"], task_id, directory, lambda: None)
        state["evidence"] = self.verify_smoke(paths, request)
        state["evidence"]["elapsed_wall_seconds"] = self.repo.clock()-state["smoke_submission_started"]
        state["evidence"]["completed_at"] = self.repo.clock()
        state["phase"] = "qualified"
        self._save(receipt, state)
        return {"state": "qualified", "generation_verified": True, "evidence": state["evidence"]}

    @staticmethod
    def _verify_smoke(paths, request):
        from .media import inspect
        from .worker import _validate_video, _validate_audio
        from comfy_workflow import native_output_spec
        spec = native_output_spec(request)
        video, audio = inspect(paths["video"], "video"), inspect(paths["audio"], "audio")
        if (video["width"] != spec["width"] or video["height"] != spec["height"]
                or abs(video["duration"]-spec["actual_duration"]) > .1
                or abs(audio["duration"]-spec["actual_duration"]) > .1):
            raise BootError("qualification_media_shape_mismatch")
        _validate_video(paths["video"], spec["width"], spec["height"], video["duration"], True)
        _validate_audio(paths["audio"], audio["duration"], flac=True)
        evidence = {"request": {k: v for k, v in request.items() if k != "prompt"},
            "scope": "single_host_fl2va_smoke_not_ref_or_quality_benchmark", "outputs": {}}
        for kind, path in paths.items():
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024*1024), b""):
                    digest.update(chunk)
            evidence["outputs"][kind] = {"filename": path.name, "size_bytes": path.stat().st_size,
                "sha256": digest.hexdigest(), "metadata": video if kind == "video" else audio}
        return evidence

    def idle_probe(self, tag, instance_id):
        if tag != self.bound_intent or instance_id != self.bound_instance or self.backend is None:
            raise BootError("bootstrap_idle_probe_not_bound")
        now = self.repo.clock()
        queue = self.backend._json("GET", "/queue")
        idle = queue.get("queue_running") == [] and queue.get("queue_pending") == []
        self.idle_since = (self.idle_since if self.idle_since is not None else now) if idle else None
        return InferenceIdleProof(instance_id, now, self.idle_since or now, idle)

    def close(self):
        # Graceful CPU-side shutdown never terminates a GPU pod or unknown job.
        if self.fleet:
            self.fleet.shutdown()
        if self.backend:
            self.backend.close()
        if self.host:
            self.host.close()


def main(argv=None):
    """An explicit operator entrypoint; the default touches no config or DB."""
    import argparse
    import signal
    from .lium_provider import LiumProvider
    from .repository import Repository
    from .settings import Settings
    parser = argparse.ArgumentParser(description="Bootstrap already-reserved Lium pod; never rents or deletes")
    parser.add_argument("--mode", choices=("disabled", "run"), default="disabled")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--intent-id")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=10)
    args = parser.parse_args(argv)
    if args.mode == "disabled":
        print(json.dumps({"state": "disabled", "provider_calls_enabled": False}))
        return 0
    controller = repo = provider = None
    stop = threading.Event()
    previous = {}
    try:
        if not args.config or not args.config.is_absolute() or args.config.stat().st_size > 65536 or not 1 <= args.interval <= 60:
            raise ValueError("bootstrap_operator_config_required")
        raw = json.loads(args.config.read_text(encoding="utf-8"))
        if "recipe_ids" in raw:
            raw["recipe_ids"] = tuple(raw["recipe_ids"])
        config = BootConfig(**raw)
        if args.once and config.fleet_enabled:
            raise ValueError("fleet_requires_persistent_controller")
        settings = Settings.from_environment()
        repo = Repository(settings.database_url)
        # Existing ledger required: no DDL or budget/instance creation here.
        provider = LiumProvider(enabled=True)
        controller = BootController(repo, provider, config)
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: stop.set())
        last = None
        while not stop.is_set():
            value = controller.tick(args.intent_id)
            # Print a small non-secret progress state, never config/DB/SSH keys.
            public = {k: value[k] for k in ("state", "phase", "generation_verified") if k in value}
            if public != last:
                print(json.dumps(public), flush=True)
                last = public
            if args.once:
                break
            stop.wait(args.interval)
        return 0
    except Exception:
        print(json.dumps({"state": "bootstrap_configuration_or_runtime_error", "cloud_creation_enabled": False}))
        return 1
    finally:
        if controller:
            controller.close()
        if provider:
            provider.close()
        if repo:
            repo.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
