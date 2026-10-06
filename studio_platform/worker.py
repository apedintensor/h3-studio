"""Explicit single-slot worker. Disabled by default, with no import-time requests.

Mock output is CPU-generated and visibly labelled SIMULATION. Comfy transport
requires an exact operator allowlist; it never follows provider output URLs.
The pinned workflow compiler is pure; legacy server/cloud controllers are unused.
"""
from __future__ import annotations

from contextlib import contextmanager
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from .media import ffmpeg, probe
from .queue import TaskQueue
from .repository import Conflict, LeaseLost, Scope, money
from .storage import ObjectAlreadyExists, ObjectNotFound, StorageWriteUncertain
# Compatibility exports retain the same objects for existing worker callers.
from .inference.comfy import ComfyBackend
from .inference.outputs import _request, _shape
from .inference.protocol import (BackendError, InferenceBackend, NotReady, Outcome,
                                 RenderCacheCapacityExceeded, SubmissionRejected,
                                 SubmissionUncertain, TAG, TASK)


class DisabledBackend:
    enabled = False
    slot_key = "disabled"


class MockBackend:
    """Local CPU demonstration, not an AI model or video benchmark."""
    def __init__(self, state_dir, *, enabled=False):
        self.enabled = bool(enabled)
        self.state_dir = Path(state_dir).resolve()
        self.slot_key = "mock-" + hashlib.sha256(str(self.state_dir).encode()).hexdigest()
        self.kind = "mock"

    def prepare(self, job, tag, store, heartbeat):
        if not self.enabled:
            raise NotReady("backend_disabled")
        if not TAG.fullmatch(tag):
            raise BackendError("invalid_attempt_tag")
        return dict(shape=_shape(job), tag=tag)

    def submit(self, prepared, tag):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        receipt = self.state_dir / (tag + ".json")
        if receipt.exists():
            raise SubmissionUncertain("mock_submission_already_recorded")
        width, height, duration, audio = prepared["shape"]
        video = self.state_dir / (tag + ".mp4")
        # Pillow bundles its default font. Do not depend on a host Fontconfig or
        # silently omit the watermark when an OS font cannot be found.
        from PIL import Image, ImageDraw, ImageFont
        frame = Image.new("RGB", (width, height), (23, 32, 66))
        draw = ImageDraw.Draw(frame)
        for label, size, y, color in (("SIMULATION", max(18, width//14), height//2, "yellow"),
                                     ("CPU demo - NOT H3", max(12, width//24), height-40, "white")):
            font = ImageFont.load_default(size=size)
            box = draw.textbbox((0, 0), label, font=font)
            draw.text(((width-(box[2]-box[0]))/2, y), label, fill=color, font=font)
        image = self.state_dir / (tag + "-simulation.png")
        frame.save(image)
        command = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-loop", "1", "-framerate", "24", "-i", str(image)]
        if audio:
            command += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=32000:duration={duration}"]
        command += ["-t", str(duration), "-c:v", "libx264", "-threads", "2",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
        command += ["-c:a", "aac", "-ar", "32000", "-ac", "2"] if audio else ["-an"]
        command += ["-movflags", "+faststart", str(video)]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=180)
            if audio:
                ffmpeg(["-i", video, "-vn", "-c:a", "flac", self.state_dir / (tag + ".flac")])
            task_id = "mock-" + tag
            payload = {"task_id": task_id, "tag": tag, "simulation": True, "audio": audio}
            with receipt.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle)
            return task_id
        except (OSError, subprocess.SubprocessError, ValueError):
            raise SubmissionRejected("mock_cpu_generation_failed") from None

    def reconcile(self, tag, task_id=None):
        if not TAG.fullmatch(tag):
            raise BackendError("invalid_attempt_tag")
        if task_id is not None and task_id != "mock-" + tag:
            raise BackendError("mock_task_identity_mismatch")
        receipt = self.state_dir / (tag + ".json")
        if not receipt.exists():
            return Outcome("unknown")
        try:
            result = json.loads(receipt.read_text(encoding="utf-8"))
            if result["task_id"] != "mock-" + tag or result["simulation"] is not True:
                raise ValueError
        except (ValueError, KeyError):
            raise BackendError("invalid_mock_receipt") from None
        return Outcome("succeeded", result["task_id"], 0)

    def poll(self, tag, task_id):
        return self.reconcile(tag, task_id)

    def cancel(self, tag, task_id):
        # CPU submit finishes synchronously: already completed results are retained.
        return False

    def fetch(self, job, tag, task_id, target_dir, heartbeat):
        outcome = self.reconcile(tag, task_id)
        if outcome.state != "succeeded":
            raise BackendError("mock_output_unknown")
        files = {"video": self.state_dir / (tag + ".mp4")}
        if _shape(job)[3]:
            files["audio"] = self.state_dir / (tag + ".flac")
        return files


class WorkerRunner:
    def __init__(self, repository, store, work_dir, *, backend: InferenceBackend | None = None, retry_after_s=5, control=None,
                 submission_guard=None, stop_requested=None):
        self.repo, self.store = repository, store
        self.queue = TaskQueue(repository)
        self.work_dir = Path(work_dir).resolve()
        self.backend = backend or DisabledBackend()
        self.retry_after_s = retry_after_s
        self.control = control
        self.submission_guard = submission_guard
        self.stop_requested = stop_requested
        self._drain = threading.Event()

    def drain(self):
        self._drain.set()

    def _check_external_stop(self):
        if self.stop_requested is not None and self.stop_requested() is True:
            self.drain()

    def _submission_allowed(self, job):
        if isinstance(self.backend, MockBackend):
            return True
        try:
            return (callable(self.submission_guard) and self.submission_guard(job) is True
                and (self.control is None or self.control.submission_allowed(job)))
        except Exception:
            return False

    @staticmethod
    def _scope(job):
        return Scope(job["tenant_id"], job["owner_id"], job["project_id"], job["actor_id"])

    def _summary(self, job, state=None):
        return {"job_id": job["id"], "state": state or job["status"],
                "simulation": isinstance(self.backend, MockBackend) or (self.backend.kind == "cpu-render"
                    and any(source.get("simulation") is True for source in job["request"].get("sources", {}).values()))}

    def _observe(self, worker_id, job_id):
        """Profile-specific observation runs while the physical slot lock is held."""
        return self.control.observe(worker_id, job_id)

    def run_once(self, worker_id, pool):
        if not self.backend.enabled:
            return {"state": "disabled", "simulation": False}
        self._check_external_stop()
        if self._drain.is_set():
            return {"state": "draining", "simulation": isinstance(self.backend, MockBackend)}
        self.work_dir.mkdir(parents=True, exist_ok=True)
        with _slot_lock(self.work_dir, self.backend.slot_key) as acquired:
            if not acquired:
                return {"state": "slot_busy", "simulation": isinstance(self.backend, MockBackend)}
            if self.control:
                # Keep an already valid registration alive even when the queue
                # is empty. Never renew/revive an expired or UNKNOWN slot merely
                # because this process is running; it needs reconciliation/proof.
                worker = self.control.get(worker_id)
                if (worker["expires_at"] > self.repo.clock()
                    and worker["state"] not in ("unknown", "retired")):
                    try:
                        self.control.heartbeat(worker_id, worker["fence"])
                    except Conflict:
                        return {"state": "slot_reconciliation_required", "simulation": isinstance(self.backend, MockBackend)}
                self.control.recover_expired()
            result = self._run_once(worker_id, pool)
            if self.control and result.get("job_id"):
                self._observe(worker_id, result["job_id"])
            return result

    def _claim(self, worker_id, pool, *, purpose):
        return (self.control or self.queue).claim(worker_id, pool, lease_seconds=900, purpose=purpose)

    def _run_once(self, worker_id, pool):
        self.queue.recover_expired(summary=True)
        claim, purpose = None, None
        for phase in ("collect", "reconcile", "generate"):
            claim = self._claim(worker_id, pool, purpose=phase)
            if claim:
                purpose = phase
                break
        if claim is None:
            return {"state": "idle", "simulation": isinstance(self.backend, MockBackend)}
        job, lease = claim.job, claim.lease
        tag = lease.attempt_id
        slot_fence = self.control.get(worker_id)["fence"] if self.control else None
        def heartbeat():
            self._check_external_stop()
            self.queue.heartbeat(lease, lease_seconds=900)
            if self.control:
                self.control.heartbeat(worker_id, slot_fence, lease_seconds=900)
            if self.backend.kind == "cpu-render":
                current = self.repo.get_job(self._scope(job), job["id"])
                if current["status"] == "cancel_requested":
                    self.backend.cancel(tag, "cpu-render-"+tag)
        task_id = None
        try:
            if (job["execution_plan"].get("backend") != self.backend.kind
                    or job["execution_plan"].get("enabled") is not True):
                return self._summary(self.queue.defer_unsubmitted(lease, error_code="worker_backend_not_authorized"))
            if purpose == "generate":
                if not self._submission_allowed(job):
                    return self._summary(self.queue.fail(lease, "execution_policy_unavailable_before_submission",
                        actual_cost_microusd=0, upstream_stopped=True))
                try:
                    prepared = self.backend.prepare(job, tag, self.store, heartbeat)
                except RenderCacheCapacityExceeded:
                    # No submission intent/POST exists. Retained input/cache
                    # evidence stays untouched; do not grow attempts forever
                    # against an operator-managed cache with no automatic GC.
                    if self.backend.kind == "cpu-render":
                        return self._summary(self.queue.fail(lease, "render_cache_capacity_exhausted",
                            actual_cost_microusd=0, upstream_stopped=True))
                    return self._summary(self.queue.defer_unsubmitted(lease, retry_after_s=30,
                        error_code="worker_preparation_not_ready"))
                except (BackendError, OSError, ValueError):
                    job = self.queue.defer_unsubmitted(lease, retry_after_s=30, error_code="worker_preparation_not_ready")
                    return self._summary(job)
                self._check_external_stop()
                if self._drain.is_set():
                    return self._summary(self.queue.defer_unsubmitted(lease, error_code="worker_draining"))
                if not self._submission_allowed(job):
                    return self._summary(self.queue.fail(lease, "execution_policy_unavailable_before_submission",
                        actual_cost_microusd=0, upstream_stopped=True))
                self.queue.begin_submission(lease)
                try:
                    task_id = self.backend.submit(prepared, tag)
                except SubmissionRejected:
                    return self._summary(self.queue.fail(lease, "submission_rejected", actual_cost_microusd=0, upstream_stopped=True))
                except Exception:
                    return self._summary(self.queue.mark_submission_unknown(lease))
                job = self.queue.record_submitted(lease, task_id)
            else:
                attempt = self.queue.get_attempt(self._scope(job), job["id"])
                task_id = attempt["upstream_task_id"]
                if not task_id:
                    outcome = self.backend.reconcile(tag)
                    if not outcome.task_id:
                        return self._summary(self.queue.release(lease, retry_after_s=30, error_code="submission_needs_reconciliation"))
                    task_id = outcome.task_id
                    job = self.queue.record_submitted(lease, task_id)
            self._check_external_stop()
            if self._drain.is_set():
                return self._summary(self.queue.release(lease, retry_after_s=self.retry_after_s))
            if job["status"] == "cancel_requested" and job["cancel_from_status"] != "collecting":
                self.backend.cancel(tag, task_id)
            if job["status"] == "collecting" or job.get("cancel_from_status") == "collecting":
                return self._collect(job, lease, tag, task_id, heartbeat)
            outcome = self.backend.poll(tag, task_id)
            if outcome.state == "succeeded":
                job = self.queue.begin_collection(lease)
                return self._collect(job, lease, tag, task_id, heartbeat)
            if outcome.state in ("failed", "cancelled"):
                cost = self._cost(job, task_id, outcome)
                if job["status"] == "cancel_requested":
                    return self._summary(self.queue.confirm_cancel(lease, upstream_stopped=True, actual_cost_microusd=cost))
                return self._summary(self.queue.fail(lease, "upstream_generation_failed", actual_cost_microusd=cost, upstream_stopped=True))
            return self._summary(self.queue.release(lease, retry_after_s=self.retry_after_s,
                error_code="upstream_status_unknown" if outcome.state == "unknown" else None))
        except LeaseLost:
            return self._summary(job, "lease_lost_reconcile_required")
        except Exception:
            # No raw exception response, URL, request or prompt escapes this worker.
            fresh = self.repo.get_job(self._scope(job), job["id"])
            try:
                if fresh["status"] == "claimed":
                    fresh = self.queue.defer_unsubmitted(lease, error_code="worker_preparation_failed")
                elif fresh["status"] == "submitting":
                    fresh = self.queue.mark_submission_unknown(lease)
                elif fresh["status"] == "collecting" or fresh.get("cancel_from_status") == "collecting":
                    fresh = self.queue.collection_failed(lease)
                else:
                    fresh = self.queue.release(lease, retry_after_s=30, error_code="worker_reconciliation_needed")
            except (LeaseLost, Conflict):
                return self._summary(fresh, "lease_lost_reconcile_required")
            return self._summary(fresh)

    def _cost(self, job, task_id, outcome=None):
        if isinstance(self.backend, MockBackend):
            return 0
        # Billing availability is independent of verified output availability.
        # A timeout or malformed invoice is unknown cost, never a free task and
        # never a reason to repeat collection/submission. The ledger retains its
        # reservation until an explicit, validated settlement arrives later.
        try:
            if outcome and outcome.actual_cost_microusd is not None:
                cost = outcome.actual_cost_microusd
            else:
                resolver = getattr(self.backend, "cost_resolver", None)
                cost = resolver(job, task_id) if resolver is not None else None
            return None if cost is None else money(cost)
        except Exception:
            return None

    def _collect(self, job, lease, tag, task_id, heartbeat):
        from .artifact_writer import ArtifactWriter
        writer = ArtifactWriter(self.repo.engine, self.store, self.work_dir, tenant=job["tenant_id"])
        receipt = writer.get(job, lease.attempt_id, tag)
        if receipt is not None and receipt["phase"] != "staging":
            with _lease_keepalive(heartbeat):
                specs = writer.write(receipt, heartbeat)
            return self._summary(self.queue.complete(lease, specs, actual_cost_microusd=self._cost(job, task_id),
                                                     settlement=writer.settlement(receipt)))
        width, height, duration, audio = _shape(job)
        writer.begin_staging(job, lease.attempt_id, tag, kinds=("video", "audio") if audio else ("video",))
        directory = self.work_dir / tag
        directory.mkdir(parents=True, exist_ok=True)
        heartbeat()
        paths = self.backend.fetch(job, tag, task_id, directory, heartbeat)
        raw = probe(paths["video"])
        stream = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), {})
        if (stream.get("width"), stream.get("height")) != (width, height):
            raise BackendError("output_dimensions_mismatch")
        try:
            if float(raw["format"]["duration"]) < duration - .1:
                raise BackendError("output_too_short")
        except (ValueError, KeyError):
            raise BackendError("output_duration_unknown") from None
        final = directory / "verified.mp4"
        request = _request(job)
        media_timeout = 1800 if job["request"].get("recipe_id") == "chapter-roughcut-v1" else 180
        heartbeat()
        args = ["-i", paths["video"], "-t", duration, "-frames:v", round(duration*24),
            "-vf", "fps=24", "-c:v", "libx264", "-preset", "veryfast", "-crf", request.get("export_crf", 18),
            "-pix_fmt", "yuv420p"]
        args += ["-c:a", "aac", "-ar", "32000", "-ac", "2"] if audio else ["-an"]
        with _lease_keepalive(heartbeat):
            ffmpeg([*args, "-fs", 512*1024*1024+1, "-movflags", "+faststart", final], timeout=media_timeout)
            video_evidence = _validate_video(final, width, height, duration, audio, timeout=media_timeout)
        files = [("video", final, "video/mp4", video_evidence)]
        if audio:
            if "audio" not in paths:
                raise BackendError("independent_audio_missing")
            output = directory / "verified.flac"
            heartbeat()
            with _lease_keepalive(heartbeat):
                ffmpeg(["-i", paths["audio"], "-t", duration, "-c:a", "flac", "-ar", "32000", "-ac", "2",
                        "-fs", 512*1024*1024+1, output], timeout=media_timeout)
                actual_audio_duration = _validate_audio(output, duration, flac=True, timeout=media_timeout)
            files.append(("audio", output, "audio/flac", {"duration_s": actual_audio_duration, "has_audio": True}))
        with _lease_keepalive(heartbeat):
            receipt = writer.prepare(job, lease.attempt_id, tag, files)
            specs = writer.write(receipt, heartbeat)
        result = self.queue.complete(lease, specs, actual_cost_microusd=self._cost(job, task_id),
                                     settlement=writer.settlement(receipt))
        return self._summary(result)

    def run_forever(self, worker_id, pool, *, poll_interval_s=1):
        """Explicit CLI lifecycle; SIGTERM drains without globally interrupting Comfy."""
        if not math.isfinite(poll_interval_s) or not .01 <= poll_interval_s <= 60:
            raise ValueError("invalid_poll_interval")
        prior = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                prior[signum] = signal.getsignal(signum)
                signal.signal(signum, lambda *_: self.drain())
        try:
            while not self._drain.is_set():
                self.run_once(worker_id, pool)
                self._drain.wait(poll_interval_s)
        finally:
            try:
                if self.control:
                    self.control.drain(worker_id)
            finally:
                # Database loss during shutdown must not leave process handlers
                # replaced or a provider client open. Durable leases expire to
                # UNKNOWN; inability to persist drain is never an idle claim.
                for signum, handler in prior.items():
                    signal.signal(signum, handler)
                if hasattr(self.backend, "close"):
                    self.backend.close()


@contextmanager
def _lease_keepalive(heartbeat, *, interval_s=30):
    """Bounded background lease renewals during blocking local media stages."""
    if not math.isfinite(interval_s) or not 0 < interval_s <= 60:
        raise ValueError("invalid_media_heartbeat_interval")
    stopped, lost = threading.Event(), []
    def renew():
        while not stopped.wait(interval_s):
            try:
                heartbeat()
            except Exception:
                lost.append(True)
                return
    thread = threading.Thread(target=renew, name="sixnine-media-lease", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=5)
    if lost:
        raise LeaseLost("lease_lost_during_media_stage")


def _validate_video(path, width, height, duration, audio, *, timeout=180):
    data = probe(path)
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    try:
        fps = float(Fraction(stream["avg_frame_rate"]))
        actual = float(data["format"]["duration"])
        if ((stream["width"], stream["height"]) != (width, height) or abs(fps-24) > .001
            or abs(actual-duration) > .1 or stream.get("codec_name") != "h264"
            or int(stream.get("nb_frames", 0)) != round(duration*24)):
            raise ValueError
    except (ValueError, KeyError, ZeroDivisionError):
        raise BackendError("output_video_verification_failed") from None
    if audio:
        _validate_audio(path, duration, timeout=timeout)
    elif any(s.get("codec_type") == "audio" for s in data.get("streams", [])):
        raise BackendError("unexpected_output_audio")
    ffmpeg(["-xerror", "-i", path, "-map", "0:v:0", "-map", "0:a:0?", "-f", "null", "-"], timeout=timeout)
    return {"width": width, "height": height, "duration_s": duration, "fps": 24, "has_audio": audio}


def _validate_audio(path, duration, *, flac=False, timeout=180):
    """Verify the requested tolerance and return the file's observed duration."""
    data = probe(path)
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    try:
        actual = float(stream.get("duration", data["format"].get("duration")))
        if (int(stream["sample_rate"]) != 32000 or stream["channels"] != 2
            or not math.isfinite(actual) or abs(actual-duration) > .1
            or flac and stream.get("codec_name") != "flac"):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise BackendError("output_audio_verification_failed") from None
    ffmpeg(["-xerror", "-i", path, "-map", "0:a:0", "-f", "null", "-"], timeout=timeout)
    return actual


@contextmanager
def _slot_lock(directory, key):
    """OS-held local lock; all runners for an endpoint must share this directory."""
    path = Path(directory) / (hashlib.sha256(key.encode()).hexdigest() + ".slot")
    with path.open("a+b") as handle:
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    pass
            yield acquired
        finally:
            if acquired:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def main(argv=None):
    """Explicit local CLI. No cloud provisioning or automatic GPU discovery."""
    import argparse
    from dataclasses import replace
    from .control import WorkerControl, WorkerSpec
    from .repository import Repository
    from .settings import Settings
    from .storage import LocalObjectStore
    parser = argparse.ArgumentParser(description="Sixnine worker; disabled unless explicitly selected")
    parser.add_argument("--backend", choices=("disabled", "mock", "comfy-worker"), default="disabled")
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--endpoint", default="")
    parser.add_argument("--allowed-origin", action="append", default=[])
    parser.add_argument("--provider", default="")
    parser.add_argument("--instance-id", default="")
    parser.add_argument("--gpu-id", action="append", default=[])
    parser.add_argument("--recipe-id", action="append", default=[])
    parser.add_argument("--model-id", default="")
    parser.add_argument("--configuration-id", default="")
    parser.add_argument("--comfy-revision", default="")
    parser.add_argument("--confirmed-idle", action="store_true")
    args = parser.parse_args(argv)
    if args.backend == "disabled":
        print(json.dumps({"state": "disabled", "simulation": False}))
        return 0
    repo, backend = None, None
    try:
        settings = Settings.from_environment()
        if args.data_dir:
            # An explicit data-dir changes the derived SQLite DB only; an explicitly
            # configured database URL is retained. Never print that URL.
            configured_url = settings.database_url if (os.environ.get("SIXNINE_DATABASE_URL")
                or os.environ.get("SIXNINE_DATABASE_URL_FILE")) else ""
            settings = replace(settings, data_dir=args.data_dir, database_url=configured_url)
        if settings.storage_provider != "local":
            raise ValueError("remote_worker_store_requires_runtime_adapter")
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        repo = Repository(settings.database_url)
        repo.create_schema()
        control = WorkerControl(repo)
        if args.backend == "mock":
            backend = MockBackend(args.work_dir / "simulation", enabled=True)
            spec = WorkerSpec(args.worker_id, args.pool, "mock", "local-"+args.worker_id,
                ("cpu",), tuple(args.recipe_id), "SIMULATION", "simulation-v1", backend="mock")
            # Mock-only registration consumes no GPUs/instances and grants no cloud permission.
            # Do not overwrite an existing approved real global ceiling.
            with repo.engine.connect() as connection:
                from .repository import capacity_gate
                present = connection.execute(__import__("sqlalchemy").select(capacity_gate)).first()
            if present is None:
                repo.configure_capacity()
        else:
            if not all((args.provider, args.instance_id, args.gpu_id, args.recipe_id,
                        args.model_id, args.configuration_id, args.comfy_revision)):
                raise ValueError("real_worker_requires_explicit_slot_and_model_manifest")
            backend = ComfyBackend(endpoint=args.endpoint, enabled=True,
                allowed_origins=args.allowed_origin, comfy_revision=args.comfy_revision)
            spec = WorkerSpec(args.worker_id, args.pool, args.provider, args.instance_id,
                tuple(args.gpu_id), tuple(args.recipe_id), args.model_id, args.configuration_id)
        worker = control.register(spec)
        if worker["state"] in ("registered", "ready"):
            control.mark_ready(args.worker_id, upstream_idle_confirmed=args.backend == "mock" or args.confirmed_idle)
        elif worker["state"] in ("unknown", "draining") and worker["current_job_id"] is None:
            control.mark_ready(args.worker_id, upstream_idle_confirmed=args.backend == "mock" or args.confirmed_idle)
        store = LocalObjectStore(settings.data_dir / "objects")
        from .execution_policy import ExecutionPolicies
        runner = WorkerRunner(repo, store, args.work_dir, backend=backend, control=control,
            submission_guard=ExecutionPolicies(settings, repo).submission_allowed)
        if args.once:
            print(json.dumps(runner.run_once(args.worker_id, args.pool)))
        else:
            runner.run_forever(args.worker_id, args.pool)
        return 0
    except Exception:
        print(json.dumps({"state": "worker_configuration_or_runtime_error", "detail": "Check explicit worker configuration; no secrets are logged"}))
        return 1
    finally:
        if backend is not None and hasattr(backend, "close"):
            backend.close()
        if repo:
            repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
