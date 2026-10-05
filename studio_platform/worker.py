"""Explicit single-slot worker. Disabled by default, with no import-time requests.

Mock output is CPU-generated and visibly labelled SIMULATION. Comfy transport
requires an exact operator allowlist; it never follows provider output URLs.
The pinned workflow compiler is pure; legacy server/cloud controllers are unused.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from urllib.parse import urlsplit

import httpx

from .media import ffmpeg, probe
from .queue import TaskQueue
from .repository import Conflict, LeaseLost, Scope, money
from .storage import ObjectAlreadyExists, ObjectNotFound, StorageWriteUncertain, key_belongs_to, validate_key


TAG = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
TASK = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


class BackendError(Exception):
    """Stable message only. Do not propagate URL, response body, prompt or tokens."""


class NotReady(BackendError):
    pass


class RenderCacheCapacityExceeded(BackendError):
    """Local CPU cache cannot admit another attempt; operator action is required."""


class SubmissionUncertain(BackendError):
    pass


class SubmissionRejected(BackendError):
    pass


@dataclass(frozen=True)
class Outcome:
    state: str
    task_id: str | None = None
    actual_cost_microusd: int | None = None


def _request(job):
    return job["request"].get("request", job["request"])


def _shape(job):
    if job["request"].get("recipe_id") == "chapter-roughcut-v1":
        # Pure lazy import avoids the renderer->worker Outcome dependency cycle.
        from .render_backend import validate_render_request
        shape = validate_render_request(job["request"], owner_id=job.get("owner_id"))
        return shape["width"], shape["height"], shape["duration"], shape["audio"]
    from comfy_workflow import native_output_spec
    request = _request(job)
    spec = job["request"].get("output_spec") or native_output_spec(request)
    width, height, duration = int(spec["width"]), int(spec["height"]), float(request.get("duration", 5))
    if not (256 <= width <= 1536 and 256 <= height <= 1536 and width % 32 == height % 32 == 0
            and math.isfinite(duration) and 4 <= duration <= 15):
        raise BackendError("invalid_output_requirements")
    return width, height, duration, bool(request.get("generate_audio", True))


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


class ComfyBackend:
    """One dedicated Comfy slot with bounded HTTP and attempt-tag reconciliation.

    actual_cost_resolver(job, task_id) is a trusted accounting adapter; None means
    pending billing. Never use a quote as an invoice. HTTP endpoint must be local
    or an explicitly approved HTTPS gateway, without credentials/query/fragment.
    """
    def __init__(self, *, endpoint="", enabled=False, allowed_origins=(), transport=None,
                 actual_cost_resolver=None, max_download_bytes=512*1024*1024,
                 timeout_s=30, max_transfer_seconds=180, comfy_revision=None):
        self.enabled = bool(enabled)
        self.endpoint = endpoint.rstrip("/")
        self.cost_resolver = actual_cost_resolver
        self.max_bytes = max_download_bytes
        self.max_transfer_seconds = max_transfer_seconds
        self.timeout_s = timeout_s
        self.transport = transport
        self.comfy_revision = comfy_revision
        self._confirmed_dequeued = set()
        self._client = None
        self.slot_key = "comfy-" + hashlib.sha256(self.endpoint.encode()).hexdigest()
        self.kind = "comfy-worker"
        if self.enabled:
            parsed = urlsplit(self.endpoint)
            allowed = set(allowed_origins)
            if (self.endpoint not in allowed or parsed.scheme not in {"http", "https"}
                or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path or parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}):
                raise ValueError("comfy_endpoint_not_explicitly_allowed")
            if (type(self.max_bytes) is not int or self.max_bytes <= 0 or not math.isfinite(timeout_s)
                    or timeout_s <= 0 or not math.isfinite(max_transfer_seconds) or max_transfer_seconds <= 0):
                raise ValueError("invalid_comfy_limits")

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

    def _http(self):
        if not self.enabled:
            raise NotReady("backend_disabled")
        if self._client is None:
            self._client = httpx.Client(base_url=self.endpoint, timeout=self.timeout_s,
                follow_redirects=False, trust_env=False, transport=self.transport)
        return self._client

    def _json(self, method, path, **kwargs):
        try:
            # All request paths are constants or validated task IDs, never provider URLs.
            with self._http().stream(method, path, **kwargs) as response:
                if response.status_code >= 400 or 300 <= response.status_code < 400:
                    raise BackendError("comfy_http_error")
                content = bytearray()
                started = time.monotonic()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 16*1024*1024 or time.monotonic()-started > self.max_transfer_seconds:
                        raise BackendError("comfy_response_limit")
                return json.loads(content)
        except (httpx.HTTPError, json.JSONDecodeError):
            raise BackendError("comfy_response_unavailable") from None

    def prepare(self, job, tag, store, heartbeat):
        from comfy_workflow import build_workflow
        if not TAG.fullmatch(tag):
            raise BackendError("invalid_attempt_tag")
        queue = self._json("GET", "/queue")
        if (not isinstance(queue, dict) or not all(isinstance(queue.get(k), list) for k in ("queue_running", "queue_pending"))
            or queue["queue_running"] or queue["queue_pending"]):
            raise NotReady("comfy_slot_not_idle")
        request = {**_request(job), "_job_id": tag}
        snapshots = job["request"].get("assets", {})
        metadata, filenames = {}, {}
        for asset_id, snapshot in snapshots.items():
            heartbeat()
            model = snapshot["model"]
            key = validate_key(model["key"])
            if not key_belongs_to(key, job["owner_id"]):
                raise BackendError("asset_owner_mismatch")
            extension = {"image": ".png", "video": ".mp4", "audio": ".wav"}[snapshot["metadata"]["kind"]]
            filename = "sixnine-" + hashlib.sha256(key.encode()).hexdigest() + extension
            with store.open(key) as source:
                response = self._json("POST", "/upload/image", files={"image": (filename, source, "application/octet-stream")},
                    data={"type": "input", "overwrite": "true", "subfolder": "sixnine-inputs"})
            name, folder = response.get("name"), response.get("subfolder", "")
            if (name != filename or folder != "sixnine-inputs" or response.get("type", "input") != "input"):
                raise BackendError("comfy_upload_path_invalid")
            filenames[asset_id] = folder + "/" + name
            metadata[asset_id] = snapshot["metadata"]
        return build_workflow(request, metadata, filenames)

    def submit(self, prepared, tag):
        try:
            with self._http().stream("POST", "/prompt", json={"prompt": prepared, "client_id": tag,
                    "extra_data": {"sixnine_attempt_id": tag}}) as response:
                if response.status_code in (400, 401, 403, 404, 422):
                    raise SubmissionRejected("comfy_submission_rejected")
                if response.status_code != 200:
                    raise SubmissionUncertain("comfy_submission_unknown")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 1024*1024:
                        raise SubmissionUncertain("comfy_submission_unknown")
                body = json.loads(content)
            task_id = body.get("prompt_id") if isinstance(body, dict) else None
            if not isinstance(task_id, str) or not TASK.fullmatch(task_id):
                raise SubmissionUncertain("comfy_submission_unknown")
            return task_id
        except (httpx.HTTPError, json.JSONDecodeError):
            raise SubmissionUncertain("comfy_submission_unknown") from None

    @staticmethod
    def _tagged(prompt, tag):
        if not isinstance(prompt, list) or len(prompt) < 4:
            return False
        extra, graph = prompt[3], prompt[2]
        if not isinstance(extra, dict) or extra.get("sixnine_attempt_id") != tag or not isinstance(graph, dict):
            return False
        return any(isinstance(node, dict) and node.get("class_type") == "SaveVideo"
            and node.get("inputs", {}).get("filename_prefix") == "h3-studio/" + tag for node in graph.values())

    def reconcile(self, tag, task_id=None):
        if not TAG.fullmatch(tag) or task_id is not None and not TASK.fullmatch(task_id):
            raise BackendError("invalid_attempt_identity")
        if task_id:
            return self.poll(tag, task_id)
        history = self._json("GET", "/history", params={"max_items": 200})
        queue = self._json("GET", "/queue")
        if not isinstance(history, dict) or not isinstance(queue, dict):
            raise BackendError("comfy_reconciliation_invalid")
        ids = set()
        for tid, record in history.items():
            if isinstance(record, dict) and self._tagged(record.get("prompt"), tag):
                ids.add(tid)
        for key in ("queue_running", "queue_pending"):
            if not isinstance(queue.get(key), list):
                raise BackendError("comfy_reconciliation_invalid")
            for prompt in queue[key]:
                if self._tagged(prompt, tag):
                    ids.add(prompt[1])
        if len(ids) != 1:
            # History may be evicted or the old process may still accept a POST.
            # No matching task is never treated as proof it is safe to repost.
            return Outcome("unknown")
        found = next(iter(ids))
        if not isinstance(found, str) or not TASK.fullmatch(found):
            raise BackendError("invalid_upstream_task_id")
        return self.poll(tag, found)

    def _record(self, tag, task_id):
        if not TASK.fullmatch(task_id):
            raise BackendError("invalid_upstream_task_id")
        history = self._json("GET", "/history/" + task_id)
        record = history.get(task_id) if isinstance(history, dict) else None
        if record is None:
            return None
        if not isinstance(record, dict) or not self._tagged(record.get("prompt"), tag):
            raise BackendError("comfy_task_identity_mismatch")
        return record

    def poll(self, tag, task_id):
        record = self._record(tag, task_id)
        if record is None:
            queue = self._json("GET", "/queue")
            if isinstance(queue, dict) and any(self._tagged(p, tag) and p[1] == task_id
                for k in ("queue_running", "queue_pending") for p in queue.get(k, [])):
                return Outcome("running", task_id)
            if task_id in self._confirmed_dequeued:
                return Outcome("cancelled", task_id)
            return Outcome("unknown", task_id)
        status = record.get("status", {})
        if status.get("completed") is True and status.get("status_str") not in ("error", "failed"):
            return Outcome("succeeded", task_id)
        if status.get("completed") is False and status.get("status_str") in ("error", "failed"):
            return Outcome("failed", task_id)
        return Outcome("unknown", task_id)

    def cancel(self, tag, task_id):
        if not TASK.fullmatch(task_id):
            raise BackendError("invalid_upstream_task_id")
        from comfy_workflow import COMFY_COMMIT
        if self.comfy_revision != COMFY_COMMIT:
            queue = self._json("GET", "/queue")
            if not isinstance(queue, dict) or any(not isinstance(queue.get(k), list) for k in ("queue_running", "queue_pending")):
                raise BackendError("comfy_cancel_unknown")
            if any(self._tagged(p, tag) and p[1] == task_id for p in queue["queue_running"]):
                # Unverified versions never get a global interrupt or guessed route.
                return False
            if not any(self._tagged(p, tag) and p[1] == task_id for p in queue["queue_pending"]):
                return False
            try:
                response = self._http().post("/queue", json={"delete": [task_id]})
                if response.status_code != 200:
                    raise BackendError("comfy_cancel_unknown")
            except httpx.HTTPError:
                raise BackendError("comfy_cancel_unknown") from None
            checked = self._json("GET", "/queue")
            if not isinstance(checked, dict) or any(not isinstance(checked.get(k), list) for k in ("queue_running", "queue_pending")):
                raise BackendError("comfy_cancel_unknown")
            stopped = not any(isinstance(p, list) and len(p) > 1 and p[1] == task_id
                              for k in ("queue_running", "queue_pending") for p in checked[k])
            if stopped:
                self._confirmed_dequeued.add(task_id)
            return stopped
        result = self._json("POST", "/api/jobs/" + task_id + "/cancel")
        if not isinstance(result, dict) or type(result.get("cancelled")) is not bool:
            raise BackendError("comfy_cancel_unknown")
        # This proves dispatch/dequeue only. Runner still checks actual task status.
        return result["cancelled"]

    def _outputs(self, record, tag):
        graph = record["prompt"][2]
        found = {}
        for node_id, result in record.get("outputs", {}).items():
            node = graph.get(str(node_id), {})
            kind = {"SaveVideo": "video", "SaveAudioAdvanced": "audio"}.get(node.get("class_type"))
            if kind is None:
                continue
            prefix = tag + ("_audio" if kind == "audio" else "")
            if node.get("inputs", {}).get("filename_prefix") != "h3-studio/" + prefix:
                continue
            for label in ("videos", "images", "gifs") if kind == "video" else ("audio", "audios"):
                for entry in result.get(label, []):
                    filename = entry.get("filename", "")
                    extension = ".mp4" if kind == "video" else ".flac"
                    if (entry.get("type") == "output" and entry.get("subfolder") == "h3-studio"
                        and isinstance(filename, str) and re.fullmatch(re.escape(prefix) + r"_\d+_?" + re.escape(extension), filename)):
                        found.setdefault(kind, entry)
        return found

    def fetch(self, job, tag, task_id, target_dir, heartbeat):
        record = self._record(tag, task_id)
        if not record or record.get("status", {}).get("completed") is not True:
            raise BackendError("comfy_output_not_complete")
        outputs = self._outputs(record, tag)
        required = ("video", "audio") if _shape(job)[3] else ("video",)
        if any(kind not in outputs for kind in required):
            raise BackendError("comfy_save_outputs_missing")
        paths = {}
        for kind in required:
            heartbeat()
            entry = outputs[kind]
            target = Path(target_dir) / ("raw.mp4" if kind == "video" else "raw.flac")
            query = {key: entry[key] for key in ("filename", "subfolder", "type")}
            count, started = 0, time.monotonic()
            try:
                with self._http().stream("GET", "/view", params=query) as response, target.open("wb") as destination:
                    if response.status_code != 200:
                        raise BackendError("comfy_download_failed")
                    for chunk in response.iter_bytes():
                        count += len(chunk)
                        if count > self.max_bytes or time.monotonic()-started > self.max_transfer_seconds:
                            raise BackendError("comfy_download_limit")
                        destination.write(chunk)
            except httpx.HTTPError:
                raise BackendError("comfy_download_failed") from None
            paths[kind] = target
        return paths


class WorkerRunner:
    def __init__(self, repository, store, work_dir, *, backend=None, retry_after_s=5, control=None,
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
