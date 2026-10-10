"""Optional, bounded stage telemetry. Business state never depends on export.

Only this closed projection reaches Cloud. Do not attach these providers to root
logging, HTTP auto-instrumentation or exception recording. IDs are structured
metadata, never metric labels or Loki index labels. No provider keys reach GPUs.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from urllib.parse import urlsplit
import uuid

from .runtime_catalog import PROFILE_IDS
from .generation_diagnostics import WANGP_VALIDATION_CODES, COMMON_CODES

STAGES = frozenset({"queue_wait", "capacity_provision", "image_pull", "weights_download",
    "model_load", "mode_switch", "input_transfer", "generate", "collect",
    "validate_output", "upload", "commit_result", "end_to_end", "unknown"})
OUTCOMES = frozenset({"success", "failure", "unknown", "cancelled"})
PROVIDERS = frozenset({"vast", "runpod", "lium", "targon", "local", "unknown"})
GPU_TYPES = frozenset({"rtx5090", "rtx-pro6000", "h100", "h200", "b200", "b300", "unknown"})
ERROR_CODES = WANGP_VALIDATION_CODES | COMMON_CODES | frozenset({"unknown_error",
    "stage_failed", "cancel_requested", "submission_rejected", "submission_needs_reconciliation",
    "upstream_status_unknown", "worker_preparation_not_ready", "worker_preparation_failed",
    "worker_reconciliation_needed", "worker_draining", "worker_backend_not_authorized",
    "execution_policy_unavailable_before_submission", "artifact_validation_failed",
    "wangp_generation_cuda_out_of_memory", "wangp_generation_failed", "capacity_unavailable",
    "runtime_not_ready", "collection_failed", "upload_failed"})
_LABEL_FIELDS = ("stage", "provider", "profile_id", "mode", "gpu_type", "warmth")
_UUID = re.compile(r"(?:[a-z]+-)?(?:[0-9a-f]{32}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})\Z")
_SERVICES = frozenset({"sixnine-api", "sixnine-worker", "sixnine-controller", "sixnine-acceptance"})


def _choice(value, allowed):
    return value if isinstance(value, str) and value in allowed else "unknown"


def _context(value):
    """Accept trusted identifiers/dimensions, never arbitrary text or objects."""
    source = value if type(value) is dict else {}
    result = {
        "provider": _choice(source.get("provider"), PROVIDERS),
        "profile_id": _choice(source.get("profile_id"), frozenset(PROFILE_IDS) | {"unknown"}),
        "mode": _choice(source.get("mode"), {"fl", "ref", "unknown"}),
        "gpu_type": _choice(source.get("gpu_type"), GPU_TYPES),
        "warmth": _choice(source.get("warmth"), {"cold", "warm", "mode_switch", "unknown"}),
    }
    for name in ("job_id", "attempt_id", "node_id", "hatchet_run"):
        item = source.get(name)
        if isinstance(item, str) and len(item) <= 80 and _UUID.fullmatch(item):
            result[name] = item
    run = source.get("dstack_run")
    if isinstance(run, str) and re.fullmatch(r"sixnine-[a-z0-9-]{1,55}", run):
        result["dstack_run"] = run
    for name, maximum in {"gpu_index": 7, "width": 16384, "height": 16384,
            "frames": 100000, "fps": 1000, "steps": 1000, "image_refs": 100,
            "video_refs": 100, "audio_refs": 100}.items():
        item = source.get(name)
        if type(item) is int and 0 <= item <= maximum:
            result[name] = item
    for name in ("cgroup_memory_gib", "listed_memory_gib"):
        item = source.get(name)
        if type(item) in (int, float) and math.isfinite(item) and 0 < item <= 1048576:
            result[name] = float(item)
    # These values distinguish reproducible environments without arbitrary tags.
    for name in ("runtime_revision", "image_digest"):
        item = source.get(name)
        if isinstance(item, str) and re.fullmatch(r"(?:sha256:)?[0-9a-f]{12,64}", item):
            result[name] = item
    return result


@dataclass(frozen=True)
class CloudTelemetryConfig:
    endpoint: str
    authorization: str = field(repr=False)
    service_name: str = "sixnine-worker"
    service_version: str = "unknown"
    environment: str = "staging"

    def __post_init__(self):
        try:
            parsed = urlsplit(self.endpoint)
            valid_endpoint = (parsed.scheme == "https" and parsed.port in (None, 443)
                and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                and parsed.path.rstrip("/") == "/otlp" and parsed.hostname is not None
                and re.fullmatch(r"otlp-gateway-[a-z0-9-]+\.grafana\.net", parsed.hostname))
            auth = self.authorization
            valid_auth = (isinstance(auth, str) and auth.startswith("Basic ") and len(auth) <= 8192
                and not any(c in auth for c in "\r\n\x00")
                and re.fullmatch(rb"[0-9]+:[!-~]+", base64.b64decode(auth[6:], validate=True)))
            valid_version = self.service_version == "unknown" or bool(re.fullmatch(r"[0-9a-f]{12,40}", self.service_version))
        except (TypeError, ValueError):
            raise ValueError("telemetry_config_invalid") from None
        if (not valid_endpoint or not valid_auth or not isinstance(self.service_name, str)
                or self.service_name not in _SERVICES or not valid_version
                or not isinstance(self.environment, str) or self.environment not in {"local", "staging", "production"}):
            raise ValueError("telemetry_config_invalid")


def read_config(filename):
    """Read one explicit protected mount; never reflect its contents in errors."""
    try:
        path = Path(filename)
        if not path.is_absolute():
            raise ValueError("telemetry_config_invalid")
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_mode & 0o077)):
                raise ValueError("telemetry_config_invalid")
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("telemetry_config_invalid")
        value = json.loads(raw)
        expected = {"schema_version", "enabled", "endpoint", "authorization", "service_name",
                    "service_version", "environment"}
        if (type(value) is not dict or set(value) != expected
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or type(value["enabled"]) is not bool):
            raise ValueError("telemetry_config_invalid")
        if not value["enabled"]:
            return None
        return CloudTelemetryConfig(**{key: value[key] for key in expected - {"schema_version", "enabled"}})
    except (OSError, TypeError, ValueError, UnicodeError):
        raise ValueError("telemetry_config_invalid") from None


class NullStage:
    def finish(self, outcome="success", *, error_code=None):
        return None

    def __enter__(self):
        return self

    def __exit__(self, kind, _error, _traceback):
        self.finish("failure" if kind else "success", error_code="stage_failed" if kind else None)
        return False


class NullTelemetry:
    enabled = False

    def __init__(self, reason="disabled"):
        self.reason = reason

    def start(self, stage, context=None):
        return NullStage()

    def call(self, stage, context, operation, *args, **kwargs):
        with self.start(stage, context):
            return operation(*args, **kwargs)

    def force_flush(self, timeout_millis=1000):
        return True

    def close(self):
        pass

    def status(self):
        return {"enabled": False, "code": self.reason}


class Stage:
    def __init__(self, telemetry, stage, context):
        self.telemetry, self.stage, self.context = telemetry, stage, context
        self.stage_id = uuid.uuid4().hex
        self.started = telemetry.monotonic()
        self._lock, self._finished = threading.Lock(), False
        self.span = telemetry._begin(self)

    def finish(self, outcome="success", *, error_code=None):
        with self._lock:
            if self._finished:
                return None
            self._finished = True
        outcome = _choice(outcome, OUTCOMES)
        code = error_code if isinstance(error_code, str) and error_code in ERROR_CODES else (
            "unknown_error" if error_code is not None else None)
        duration = max(0.0, self.telemetry.monotonic() - self.started)
        self.telemetry._end(self, outcome, code, duration)
        return duration

    def __enter__(self):
        return self

    def __exit__(self, kind, _error, _traceback):
        self.finish("failure" if kind else "success", error_code="stage_failed" if kind else None)
        return False


class StageTelemetry(NullTelemetry):
    enabled = True

    def __init__(self, *, logger, tracer, meter, providers=(), monotonic=time.monotonic,
                 timestamp=time.time_ns, transport_stats=None):
        self.logger, self.tracer, self.providers = logger, tracer, tuple(providers)
        self.monotonic, self.timestamp = monotonic, timestamp
        self._stats = transport_stats or {"export_attempts": 0, "export_failures": 0}
        self._lock, self._labelsets, self._closed = threading.Lock(), set(), False
        self.started_count = meter.create_counter("sixnine.stage.started")
        self.finished_count = meter.create_counter("sixnine.stage.finished")
        self.duration = meter.create_histogram("sixnine.stage.duration", unit="s")

    def start(self, stage, context=None):
        if self._closed:
            return NullStage()
        try:
            return Stage(self, _choice(stage, STAGES), _context(context))
        except Exception:
            # Optional observation must not fail admission, inference or cleanup.
            return NullStage()

    def _labels(self, stage, context, outcome=None):
        labels = {"stage": stage, **{name: context[name] for name in _LABEL_FIELDS[1:]}}
        if outcome is not None:
            labels["outcome"] = outcome
        identity = tuple(sorted(labels.items()))
        with self._lock:
            if identity not in self._labelsets:
                if len(self._labelsets) >= 128:
                    return {name: "unknown" for name in labels}
                self._labelsets.add(identity)
        return labels

    def _event(self, stage, event, *, outcome=None, error_code=None, duration=None, span=None):
        from opentelemetry.trace import set_span_in_context
        body = {"schema_version": 1, "event": event, "stage": stage.stage,
                "stage_id": stage.stage_id, "time_unix_ns": self.timestamp(), **stage.context}
        if outcome is not None:
            body["outcome"] = outcome
        if error_code is not None:
            body["error_code"] = error_code
        if duration is not None:
            body["duration_seconds"] = duration
        kwargs = {}
        if span is not None:
            trace = span.get_span_context()
            body.update(trace_id=format(trace.trace_id, "032x"), span_id=format(trace.span_id, "016x"))
            kwargs["context"] = set_span_in_context(span)
        self.logger.emit(body=json.dumps(body, separators=(",", ":"), allow_nan=False),
                         event_name="sixnine." + event, timestamp=body["time_unix_ns"], **kwargs)

    def _begin(self, stage):
        from opentelemetry.context import Context
        # Never inherit arbitrary HTTP/baggage context. Trusted job/attempt IDs
        # correlate these stage spans without automatic user-input capture.
        span = self.tracer.start_span("sixnine." + stage.stage, context=Context(),
            attributes={"stage": stage.stage, **stage.context})
        try:
            # Enqueue now, independently of whether the long-lived span ever ends.
            self._event(stage, "stage_started", span=span)
            self.started_count.add(1, self._labels(stage.stage, stage.context))
        except Exception:
            span.end()
            raise
        return span

    def _end(self, stage, outcome, error_code, duration):
        try:
            from opentelemetry.trace import Status, StatusCode
            self._event(stage, "stage_finished", outcome=outcome, error_code=error_code,
                        duration=duration, span=stage.span)
            labels = self._labels(stage.stage, stage.context, outcome)
            self.finished_count.add(1, labels)
            self.duration.record(duration, labels)
            stage.span.set_status(Status(StatusCode.ERROR if outcome == "failure" else StatusCode.UNSET,
                description=error_code if outcome == "failure" else None))
        except Exception:
            pass
        finally:
            try:
                stage.span.end()
            except Exception:
                pass

    def force_flush(self, timeout_millis=1000):
        deadline = self.monotonic() + max(0, min(timeout_millis, 5000)) / 1000
        result = True
        for provider in self.providers:
            remaining = max(1, int((deadline - self.monotonic()) * 1000))
            try:
                result = provider.force_flush(timeout_millis=remaining) is not False and result
            except Exception:
                result = False
        return result

    def close(self):
        if self._closed:
            return
        self._closed = True
        for provider in self.providers:
            try:
                provider.shutdown(timeout_millis=1000)
            except TypeError:
                # Logger/TracerProvider lack a timeout argument; the pinned
                # bounded processors below enforce it instead.
                try:
                    provider.shutdown()
                except Exception:
                    pass
            except Exception:
                pass

    def status(self):
        return {"enabled": not self._closed, "code": "telemetry_closed" if self._closed else "telemetry_enabled",
                "metric_labelsets": len(self._labelsets), **self._stats}


def create_cloud_telemetry(config):
    """Private SDK providers, fixed resource metadata and bounded HTTPS export."""
    from opentelemetry.exporter.http.transport._base import BaseHTTPResult, BaseHTTPTransport
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.metrics.view import View, ExplicitBucketHistogramAggregation
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    import urllib3

    stats = {"export_attempts": 0, "export_failures": 0}
    resource = Resource({"service.name": config.service_name, "service.namespace": "sixnine",
        "service.version": config.service_version, "deployment.environment.name": config.environment})

    class SafeResult(BaseHTTPResult):
        def content(self):
            return b""

        def headers(self):
            return {}

    class SafeTransport(BaseHTTPTransport):
        def __init__(self):
            self.pool = urllib3.PoolManager(cert_reqs="CERT_REQUIRED", retries=False)

        def request(self, method, url, *, headers=None, timeout=None, data=None):
            stats["export_attempts"] += 1
            response = None
            try:
                # No redirect, arbitrary body or exception text can escape this
                # transport into SDK logs. No response body is needed for export.
                response = self.pool.request(method, url, headers=headers, body=data,
                    timeout=urllib3.Timeout(total=min(timeout or 1, 1)), redirect=False,
                    retries=False, preload_content=False)
                status = response.status
                response.close()
                if not 200 <= status < 300:
                    stats["export_failures"] += 1
                return SafeResult(status_code=400 if 300 <= status < 400 else status,
                                  reason="telemetry_http_status")
            except Exception:
                if response is not None:
                    response.close()
                stats["export_failures"] += 1
                return SafeResult(error=RuntimeError("telemetry_transport_failed"))

        def is_connection_error(self, _error):
            return False

        def close(self):
            self.pool.clear()

    base = config.endpoint.rstrip("/")
    options = {"headers": {"Authorization": config.authorization}, "timeout": 1, "max_request_size": 65536}
    class BoundedLogProcessor(BatchLogRecordProcessor):
        def shutdown(self):
            # Pin 1.45.0: public shutdown has no timeout; avoid its 30s default.
            return self._batch_processor.shutdown(timeout_millis=1000)

    class BoundedSpanProcessor(BatchSpanProcessor):
        def shutdown(self):
            return self._batch_processor.shutdown(timeout_millis=1000)

    logs = LoggerProvider(resource=resource, shutdown_on_exit=False)
    logs.add_log_record_processor(BoundedLogProcessor(
        OTLPLogExporter(endpoint=base + "/v1/logs", _transport=SafeTransport(), **options),
        max_queue_size=256, max_export_batch_size=32, schedule_delay_millis=250, export_timeout_millis=1000))
    traces = TracerProvider(resource=resource, shutdown_on_exit=False)
    traces.add_span_processor(BoundedSpanProcessor(
        OTLPSpanExporter(endpoint=base + "/v1/traces", _transport=SafeTransport(), **options),
        max_queue_size=256, max_export_batch_size=32, schedule_delay_millis=250, export_timeout_millis=1000))
    metrics = MeterProvider(resource=resource, shutdown_on_exit=False, metric_readers=[
        PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=base + "/v1/metrics", _transport=SafeTransport(), **options),
            export_interval_millis=30000, export_timeout_millis=1000)], views=[
        View(instrument_name="sixnine.stage.duration", attribute_keys=set(_LABEL_FIELDS) | {"outcome"},
             aggregation=ExplicitBucketHistogramAggregation((1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200))),
        View(instrument_name="sixnine.stage.started", attribute_keys=set(_LABEL_FIELDS)),
        View(instrument_name="sixnine.stage.finished", attribute_keys=set(_LABEL_FIELDS) | {"outcome"})])
    return StageTelemetry(logger=logs.get_logger("sixnine.stages"),
        tracer=traces.get_tracer("sixnine.stages"), meter=metrics.get_meter("sixnine.stages"),
        providers=(logs, traces, metrics), transport_stats=stats)


def configured_telemetry(filename=None):
    """Disabled unless explicitly configured; a broken exporter never boots GPUs."""
    filename = filename or os.environ.get("SIXNINE_TELEMETRY_CONFIG_FILE")
    if not filename:
        return NullTelemetry()
    try:
        config = read_config(filename)
        return create_cloud_telemetry(config) if config else NullTelemetry()
    except Exception:
        return NullTelemetry("telemetry_config_unavailable")
