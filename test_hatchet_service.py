"""Opt-in authenticated local-engine qualification; CPU SIMULATION media only.

HATCHET_PROOF_CONFIG must point to a protected broker JSON with loopback URLs.
The engine/database are operator-owned isolated test services, not production.
Use HATCHET_PROOF_LONG_SECONDS=610 to additionally qualify a >10 minute task.
No test prints credentials or uses provider/GPU endpoints.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from sqlalchemy import select, update

from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.hatchet_dispatch import (HatchetSlotRunner, OutboxDispatcher,
    SDKPublisher, create_client, read_broker_config)
from studio_platform.repository import Repository, Scope, attempts, dispatch_receipts
from studio_platform.storage import LocalObjectStore
from studio_platform.worker import MockBackend, Outcome, SubmissionUncertain


class ProofBackend(MockBackend):
    def __init__(self, root, delay):
        super().__init__(root/"backend", enabled=True)
        self.root, self.delay = root, delay

    def record(self, operation, tag):
        with (self.root/"operations.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"operation": operation, "tag": tag, "at": time.time(),
                "monotonic_at": time.monotonic()})+"\n")

    def submit(self, prepared, tag):
        self.record("submit", tag)
        super().submit(prepared, tag)
        # Simulate GPU accepting the POST while its HTTP response is lost.
        # The durable original attempt must subsequently be reconciled.
        raise SubmissionUncertain("proof_response_lost")

    def reconcile(self, tag, task_id=None):
        outcome = super().reconcile(tag, task_id)
        records = [json.loads(s) for s in (self.root/"operations.jsonl").read_text().splitlines()]
        started = next(x["monotonic_at"] for x in records if x["operation"] == "submit" and x["tag"] == tag)
        if time.monotonic()-started < self.delay:
            return Outcome("running", outcome.task_id)
        return outcome

    def fetch(self, job, tag, task_id, target_dir, heartbeat):
        self.record("fetch", tag)
        return super().fetch(job, tag, task_id, target_dir, heartbeat)


def worker(config_path, root, delay):
    repo = Repository("sqlite:///"+(root/"ledger.sqlite3").as_posix())
    control = WorkerControl(repo)
    runner = HatchetSlotRunner(repo, LocalObjectStore(root/"objects"), root/"worker",
        backend=ProofBackend(root, delay), control=control,
        broker_config=read_broker_config(config_path), collection_lock_dir=root/"collection-lock")
    runner.run_forever("hatchet-service-proof", "proof-pool")


class LoseAcceptedResponse:
    def __init__(self, sdk):
        self.sdk, self.calls = sdk, []
    def publish(self, message, job):
        run_id = self.sdk.publish(message, job)
        self.calls.append((message.event_id, run_id))
        if len(self.calls) == 1:
            raise TimeoutError("proof_broker_response_lost")
        return run_id
    def status(self, run_id):
        return self.sdk.status(run_id)


@unittest.skipUnless(os.environ.get("HATCHET_PROOF_CONFIG"), "explicit isolated authenticated engine required")
class RealHatchetTests(unittest.TestCase):
    def test_lost_responses_exact_job_and_long_execution(self):
        config_path = Path(os.environ["HATCHET_PROOF_CONFIG"]).resolve()
        config = read_broker_config(config_path)
        if not config.server_url.startswith("http://127.0.0.1:") or config.tls:
            self.fail("proof requires explicit loopback-only isolated engine")
        delay = int(os.environ.get("HATCHET_PROOF_LONG_SECONDS", "0"))
        self.assertTrue(0 <= delay <= 900)
        with tempfile.TemporaryDirectory(prefix="sixnine-real-hatchet-proof-") as temporary:
            root = Path(temporary)
            repo = Repository("sqlite:///"+(root/"ledger.sqlite3").as_posix())
            repo.create_schema()
            repo.configure_capacity(max_instances=2, max_physical_gpus=2)
            scope = Scope("proof-tenant", "proof-owner", "proof-project")
            repo.configure_budget("proof-budget", tenant_id=scope.tenant_id,
                owner_id=scope.owner_id, limit_microusd=1_000_000)
            request = {"recipe_id": "proof-recipe", "request": {"model": "SIMULATION",
                "prompt": "CPU protocol qualification", "duration": 4, "resolution": "custom",
                "width": 256, "height": 256, "generate_audio": True, "export_crf": 18},
                "output_spec": {"width": 256, "height": 256}, "assets": {}}
            configuration_id = "proof-config-"+uuid.uuid4().hex
            execution = {"pool": "proof-pool", "backend": "mock", "enabled": True,
                "configuration_id": configuration_id, "dispatch_backend": "hatchet-v1"}
            plan = repo.create_plan(scope, request, execution, expires_at=time.time()+2000,
                estimated_cost_microusd=100_000)
            older = repo.create_job(scope, plan["id"], "older-not-dispatched", budget_account_ids=["proof-budget"])
            # Keep the older *eligible* original job's broker retry pending.
            # A generic queue scan would steal it; exact-job execution must not.
            with repo.transaction() as connection:
                connection.execute(update(dispatch_receipts).where(dispatch_receipts.c.job_id == older["id"])
                    .values(not_before=time.time()+2000))
            job = repo.create_job(scope, plan["id"], "exact-dispatched", budget_account_ids=["proof-budget"])
            control = WorkerControl(repo)
            control.register(WorkerSpec("hatchet-service-proof", "proof-pool", "mock",
                "proof-instance", ("cpu-protocol-slot",), ("proof-recipe",), "SIMULATION",
                configuration_id, "mock", dispatch_backend="hatchet-v1"))
            control.mark_ready("hatchet-service-proof", upstream_idle_confirmed=True)
            worker_log = (root/"protected-worker.log").open("w", encoding="utf-8")
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker",
                str(config_path), str(root), str(delay)], stdout=worker_log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            sdk = SDKPublisher(create_client(config))
            publisher = LoseAcceptedResponse(sdk)
            dispatcher = OutboxDispatcher(repo, publisher, publisher_id="proof-dispatcher", retry_seconds=1)
            start = time.monotonic()
            receipt = None
            try:
                # Worker registration is asynchronous; the bridge safely holds
                # unknown publishing outcomes on this same original event.
                deadline = start+delay+180
                while time.monotonic() < deadline:
                    self.assertIsNone(process.poll(), "worker exited; inspect protected local diagnostic")
                    if receipt is None or receipt["state"] != "published":
                        receipt = dispatcher.publish_once()
                    current = repo.get_job(scope, job["id"])
                    if current["status"] == "succeeded" and receipt and receipt["state"] == "published":
                        break
                    time.sleep(1)
                self.assertEqual(current["status"], "succeeded")
                self.assertEqual(repo.get_job(scope, older["id"])["status"], "queued")
                self.assertGreaterEqual(len(publisher.calls), 2)
                self.assertEqual(len({event for event, run in publisher.calls}), 1)
                # At-least-once broker deliveries may have different run IDs.
                # Business execution, not a broker TTL/cache, owns deduplication.
                unique_broker_runs = len({run for event, run in publisher.calls})
                # Redelivery after success is a harmless same-run acknowledgement.
                with repo.engine.connect() as connection:
                    delivery = connection.execute(select(dispatch_receipts).where(
                        dispatch_receipts.c.job_id == job["id"])).mappings().one()
                    attempt_rows = list(connection.execute(select(attempts)).mappings())
                operations = [json.loads(s) for s in (root/"operations.jsonl").read_text().splitlines()]
                self.assertEqual(len(attempt_rows), 1)
                self.assertEqual(sum(x["operation"] == "submit" for x in operations), 1)
                self.assertEqual(sum(x["operation"] == "fetch" for x in operations), 1)
                artifacts = repo.list_artifacts(scope, job["id"])
                self.assertEqual({a["metadata"]["kind"] for a in artifacts}, {"video", "audio"})
                self.assertTrue(all(a["metadata"]["size_bytes"] > 0 for a in artifacts))
                elapsed = time.monotonic()-start
                self.assertGreaterEqual(elapsed, delay)
                result = {"engine": "v0.110.5", "sdk": "1.42.1", "state": "passed",
                    "elapsed_s": round(elapsed, 3), "broker_delivery_calls": len(publisher.calls),
                    "broker_unique_run_ids": unique_broker_runs,
                    "broker_same_run_id": unique_broker_runs == 1,
                    "original_attempts": 1, "remote_submissions": 1, "artifact_fetches": 1,
                    "video_and_audio": True, "older_job_untouched": True,
                    "event_id": delivery["event_id"], "run_id": delivery["external_run_id"],
                    "job_id": job["id"], "paid_gpu_operations": 0}
                destination = os.environ.get("HATCHET_PROOF_RECEIPT")
                if destination:
                    Path(destination).write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
                print(json.dumps(result), flush=True)
            except Exception:
                print(json.dumps({"safe_broker_call_identities": publisher.calls}), flush=True)
                worker_log.flush()
                diagnostic = (root/"protected-worker.log").read_text(encoding="utf-8", errors="replace")
                diagnostic = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED_JWT]", diagnostic)
                # SDK validation may include the protected token as a value.
                diagnostic = diagnostic.replace(Path(config.token_file).read_text().strip(), "[REDACTED_TOKEN]")
                (config_path.parent/"protected-proof-worker-diagnostic.log").write_text(diagnostic, encoding="utf-8")
                print("proof_worker_failed; sanitized diagnostic retained beside protected configuration", flush=True)
                raise
            finally:
                # Only this test's child process tree, never another GPU/service.
                if process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                    else:
                        process.terminate()
                    process.wait(timeout=20)
                worker_log.close()
                repo.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        worker(Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4]))
    else:
        unittest.main()
