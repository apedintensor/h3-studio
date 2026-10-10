"""Actual Hatchet callback/ledger stages, fake broker, no Cloud/GPU calls."""
import json
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch
import uuid

from studio_platform.control import WorkerControl
from studio_platform.hatchet_dispatch import BrokerConfig, HatchetSlotRunner, main
from studio_platform.storage import LocalObjectStore
from studio_platform.telemetry import StageTelemetry
import test_hatchet_dispatch as dispatch_fixture
from test_platform_repository import LedgerCase
from test_platform_telemetry import RecordingLogger, RecordingMeter


class HatchetTelemetryTests(LedgerCase):
    make_job = dispatch_fixture.HatchetDispatchTests.make_job
    ready = dispatch_fixture.HatchetDispatchTests.ready
    message = dispatch_fixture.HatchetDispatchTests.message

    def setUp(self):
        super().setUp()
        self.control = WorkerControl(self.repo)
        self.directory = Path(self.temp.name)
        self.store = LocalObjectStore(self.directory / "objects")
        self.config = BrokerConfig(token_file=self.directory / "token", server_url="http://127.0.0.1:8080",
            host_port="127.0.0.1:7070", tls=False, poll_interval_s=.1)

    def sdk(self, message, context):
        outer = self
        class SDK:
            def task(self, **_options):
                def decorate(operation):
                    self.operation = operation
                    return operation
                return decorate
            def worker(self, **options):
                outer.assertEqual(options["slots"], 1)
                outer.assertEqual(options["workflows"], [self.operation])
                return SimpleNamespace(start=lambda: setattr(self, "result", self.operation(message, context)))
        return SDK()

    def invoke_callback(self, *, telemetry, context=None):
        job = self.make_job(audio=True)
        self.ready()
        backend = dispatch_fixture.CountingMock(self.directory / "mock", enabled=True)
        context = context or SimpleNamespace(is_cancelled=False, workflow_run_id=str(uuid.uuid4()))
        sdk = self.sdk(self.message(job), context)
        # The production Linux guard stays enabled. Replace only this module's
        # os reference; stdlib Path and the real filesystem keep their platform.
        fake_sdk_module = ModuleType("hatchet_sdk")
        fake_sdk_module.DesiredWorkerLabel = lambda **options: SimpleNamespace(**options)
        fake_sdk_module.TTLBasedIdempotencyConfig = lambda **options: SimpleNamespace(**options)
        runner = HatchetSlotRunner(self.repo, self.store, self.directory / "worker", backend=backend,
            control=self.control, broker_config=self.config, collection_lock_dir=self.directory / "collection-lock",
            client_factory=lambda _config: sdk, submission_guard=lambda _job: True, telemetry=telemetry,
            telemetry_context={"dstack_run": "sixnine-test-runtime", "gpu_type": "rtx5090",
                "prompt": "SECRET prompt", "authorization": "SECRET token", "url": "SECRET signed URL"})
        original_sdk = sys.modules.get("hatchet_sdk")
        sys.modules["hatchet_sdk"] = fake_sdk_module
        try:
            with patch("studio_platform.hatchet_dispatch.os", SimpleNamespace(name="posix")):
                runner.run_forever("worker", "hatchet-test")
        finally:
            # Restore only the seam we own, preserving ordinary lazy ledger
            # imports and their SQLAlchemy metadata from the real callback.
            if original_sdk is None:
                sys.modules.pop("hatchet_sdk", None)
            else:
                sys.modules["hatchet_sdk"] = original_sdk
        self.assertEqual(sdk.result, {"job_id": job["id"], "state": "succeeded"})
        self.assertEqual(backend.submits, 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)
        self.assertEqual(len(self.repo.list_artifacts(self.scope, job["id"])), 2)
        return job, context

    def test_actual_callback_emits_original_stage_boundaries_and_closed_run_context(self):
        from opentelemetry.trace import NonRecordingSpan, SpanContext
        logger = RecordingLogger()
        tracer = SimpleNamespace(start_span=lambda *_args, **_kwargs: NonRecordingSpan(SpanContext(1, 2, False)))
        telemetry = StageTelemetry(logger=logger, tracer=tracer, meter=RecordingMeter())
        job, context = self.invoke_callback(telemetry=telemetry)
        finished = [event for event in logger.events if event["event"] == "stage_finished"]
        self.assertEqual([event["stage"] for event in finished], ["queue_wait", "input_transfer", "generate",
            "collect", "validate_output", "upload", "commit_result", "end_to_end"])
        self.assertTrue(all(event["job_id"] == job["id"] and event["hatchet_run"] == context.workflow_run_id
            and event["dstack_run"] == "sixnine-test-runtime" and event["provider"] == "local"
            and event["gpu_type"] == "rtx5090" for event in finished))
        self.assertTrue(all(event["simulation"] for event in finished))
        self.assertNotIn("SECRET", json.dumps(logger.events))

    def test_broken_exporter_or_run_identity_getter_cannot_interrupt_original_job(self):
        class Context:
            is_cancelled = False
            @property
            def workflow_run_id(self):
                raise RuntimeError("SECRET SDK context detail")
        telemetry = Mock()
        telemetry.start.side_effect = RuntimeError("SECRET exporter unavailable")
        self.invoke_callback(telemetry=telemetry, context=Context())
        self.assertTrue(telemetry.start.called)

    def test_worker_entrypoint_passes_optional_exporter_and_flushes_closes_on_return_and_failure(self):
        for fails in (False, True):
            telemetry = Mock()
            fleet = SimpleNamespace(work_dir=self.directory)
            def run_slot(_fleet, _worker_id, _settings, *, runner_factory):
                runner_factory()
                if fails:
                    raise RuntimeError("original worker failure")
                return {"state": "stopped"}
            with patch("studio_platform.settings.Settings.from_environment", return_value=SimpleNamespace()), \
                    patch("studio_platform.hatchet_dispatch.read_broker_config", return_value=self.config), \
                    patch("studio_platform.hatchet_dispatch.configured_telemetry", return_value=telemetry) as load, \
                    patch("studio_platform.hatchet_dispatch.HatchetSlotRunner") as factory, \
                    patch("studio_platform.fleet.read_config", return_value=fleet), \
                    patch("studio_platform.fleet.run_slot", side_effect=run_slot), \
                    patch.dict(os.environ, {"SIXNINE_TELEMETRY_CONFIG_FILE": str(self.directory / "protected-cloud.json")}):
                args = ["worker", "--broker-config", "broker.json", "--fleet-config", "fleet.json", "--worker-id", "worker"]
                if fails:
                    with self.assertRaisesRegex(RuntimeError, "^original worker failure$"):
                        main(args)
                else:
                    self.assertEqual(main(args), {"state": "stopped"})
                load.assert_called_once_with()
                self.assertIs(factory.call_args.kwargs["telemetry"], telemetry)
            telemetry.force_flush.assert_called_once_with(timeout_millis=1000)
            telemetry.close.assert_called_once_with()

    def test_shutdown_export_failure_preserves_worker_result(self):
        telemetry = Mock()
        telemetry.force_flush.side_effect = RuntimeError("SECRET flush diagnostic")
        telemetry.close.side_effect = RuntimeError("SECRET close diagnostic")
        with patch("studio_platform.settings.Settings.from_environment", return_value=SimpleNamespace()), \
                patch("studio_platform.hatchet_dispatch.read_broker_config", return_value=self.config), \
                patch("studio_platform.hatchet_dispatch.configured_telemetry", return_value=telemetry), \
                patch("studio_platform.fleet.read_config", return_value=SimpleNamespace(work_dir=self.directory)), \
                patch("studio_platform.fleet.run_slot", return_value={"state": "stopped"}):
            self.assertEqual(main(["worker", "--broker-config", "broker.json", "--fleet-config", "fleet.json",
                "--worker-id", "worker"]), {"state": "stopped"})
        telemetry.close.assert_called_once_with()

    def test_real_optional_loader_reads_only_explicit_protected_file_at_worker_entrypoint(self):
        import base64
        document = {"schema_version": 1, "enabled": True,
            "endpoint": "https://otlp-gateway-prod-au-southeast-1.grafana.net/otlp",
            "authorization": "Basic " + base64.b64encode(b"100:offline-test-token").decode(),
            "service_name": "sixnine-worker", "service_version": "a" * 40, "environment": "local"}
        protected = self.directory / "protected-cloud.json"
        protected.write_text(json.dumps(document), encoding="utf-8")
        protected.chmod(0o600)
        telemetry = Mock()
        with patch("studio_platform.settings.Settings.from_environment", return_value=SimpleNamespace()), \
                patch("studio_platform.hatchet_dispatch.read_broker_config", return_value=self.config), \
                patch("studio_platform.telemetry.create_cloud_telemetry", return_value=telemetry) as create, \
                patch("studio_platform.fleet.read_config", return_value=SimpleNamespace(work_dir=self.directory)), \
                patch("studio_platform.fleet.run_slot", return_value={"state": "stopped"}), \
                patch.dict(os.environ, {"SIXNINE_TELEMETRY_CONFIG_FILE": str(protected)}):
            self.assertEqual(main(["worker", "--broker-config", "broker.json", "--fleet-config", "fleet.json",
                "--worker-id", "worker"]), {"state": "stopped"})
            self.assertEqual(create.call_args.args[0].service_name, "sixnine-worker")
            self.assertNotIn("offline-test-token", repr(create.call_args.args[0]))
        telemetry.force_flush.assert_called_once_with(timeout_millis=1000)
        telemetry.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
