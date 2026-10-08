"""Private host HTTP assembly; no listener or model starts on import.

Bind the explicit launcher to 127.0.0.1 only. Access is carried by the existing
protected SSH tunnel. This is not the public business API or a second job queue.
"""
from dataclasses import asdict
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
from fractions import Fraction
import subprocess

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..inference.wangp_contract import InputDescriptor, PreparedRequest
from ..storage import LocalObjectStore
from ..media_process import run_media_process


def private_token_file(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("wangp_invalid_token_file")
    from .wangp_receipts import checked_reader
    with checked_reader(path, path.parent) as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode)
                or os.name != "nt" and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
            raise ValueError("wangp_token_permissions")
        value = source.read(513).strip()
    if not 32 <= len(value) <= 512 or not value.isascii() or any(chr(c).isspace() for c in value):
        raise ValueError("wangp_invalid_token")
    return value.decode("ascii")


class StagedInputs:
    def __init__(self, root, *, max_bytes=512 * 1024**2):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise ValueError("wangp_linked_input_root")
        self.store = LocalObjectStore(self.root)
        self.max_bytes = max_bytes

    def key(self, descriptor):
        if not isinstance(descriptor, InputDescriptor) or descriptor.size_bytes > self.max_bytes:
            raise ValueError("wangp_input_limit")
        # Content-addressed objects are not user filenames or arbitrary paths.
        return "inputs/" + descriptor.sha256 + "/blob"

    def resolve(self, descriptor):
        key = self.key(descriptor)
        digest, size = hashlib.sha256(), 0
        with self.store.open(key) as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                if size > descriptor.size_bytes:
                    raise ValueError("wangp_staged_input_mismatch")
                digest.update(chunk)
        if size != descriptor.size_bytes or digest.hexdigest() != descriptor.sha256:
            raise ValueError("wangp_staged_input_mismatch")
        # LocalObjectStore deliberately hashes keys; never join a logical key
        # to the root as if it were the actual physical object location.
        return self.store._directory(key) / "blob"

    def save(self, descriptor, source):
        key = self.key(descriptor)
        from ..storage import ObjectAlreadyExists
        try:
            self.store.put(key, source, max_bytes=descriptor.size_bytes,
                           expected_sha256=descriptor.sha256)
        except ObjectAlreadyExists:
            # Replay checks the same bytes rather than overwriting a live input.
            pass
        self.resolve(descriptor)

    def image_path(self, descriptor, *, reference=False):
        """Expose inspected normalized PNG bytes under a runtime-safe extension.

        WanGP rejects extensionless inputs. The public asset service supplies
        normalized PNGs; this copy preserves their hash and never re-encodes.
        """
        from PIL import Image
        if descriptor.kind != "image":
            raise ValueError("wangp_image_input_required")
        self.resolve(descriptor)
        key = self.key(descriptor)
        with self.store.open(key) as source, Image.open(source) as picture:
            if (picture.format != "PNG" or getattr(picture, "n_frames", 1) != 1
                    or not 256 <= picture.width <= 5760 or not 256 <= picture.height <= 5760):
                raise ValueError("wangp_normalized_png_required")
            if reference:
                self._reference_dimensions(picture.width, picture.height)
            picture.verify()
        return self._typed_copy(descriptor, "images", ".png")

    @staticmethod
    def _reference_dimensions(width, height):
        if (type(width) is not int or type(height) is not int or min(width, height) < 256
                or max(width, height) > 832 or width * height > 832*480 or not .4 <= width/height <= 2.5):
            raise ValueError("wangp_ref_qualification_pixels_exceeded")

    def _probe_reference(self, descriptor, kind):
        if descriptor.kind != kind:
            raise ValueError("wangp_typed_input_kind_mismatch")
        path = self.resolve(descriptor)
        # Force a local demuxer, suppress all diagnostic/media text, and inspect
        # the actual hash-bound object rather than trusting uploaded metadata.
        args = ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
                "-f", "mov" if kind == "video" else "wav"]
        if kind == "video":
            args += ["-enable_drefs", "0", "-use_absolute_path", "0", "-count_frames"]
        args += ["-max_streams", "4", "-show_entries",
                 "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,nb_read_frames,duration,sample_rate,channels:format=duration",
                 "-of", "json", str(path)]
        try:
            result = run_media_process(args, check=True, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=20, memory_bytes=512*1024**2)
            if len(result.stdout) > 16384:
                raise ValueError()
            value = json.loads(result.stdout)
            streams = value["streams"]
            if not isinstance(streams, list) or len(streams) != 1 or streams[0]["codec_type"] != kind:
                raise ValueError()
            return streams[0], value.get("format", {})
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
            raise ValueError("wangp_ref_media_probe_rejected") from None

    def video_path(self, descriptor, *, deployment_profile_id=None):
        counts = (56, 73)
        if deployment_profile_id is not None:
            from ..runtime_catalog import get_profile
            get_profile(deployment_profile_id)  # Unknown IDs never widen admission.
            counts = (56,)
        stream, _ = self._probe_reference(descriptor, "video")
        try:
            self._reference_dimensions(stream["width"], stream["height"])
            count = int(stream["nb_read_frames"])
            duration = float(stream["duration"])
            if (stream["codec_name"] != "h264" or count not in counts
                    or Fraction(stream["avg_frame_rate"]) != 24 or Fraction(stream["r_frame_rate"]) != 24
                    or not math.isfinite(duration) or abs(duration-count/24) > .00001):
                raise ValueError()
        except (ValueError, KeyError, TypeError, ZeroDivisionError, OverflowError):
            raise ValueError("wangp_ref_normalized_silent_video_required") from None
        return self._typed_copy(descriptor, "videos", ".mp4")

    def audio_path(self, descriptor, *, deployment_profile_id=None):
        maximum = 3
        if deployment_profile_id is not None:
            from ..runtime_catalog import get_profile
            get_profile(deployment_profile_id)
            maximum = 5.2
        stream, info = self._probe_reference(descriptor, "audio")
        try:
            if (stream["codec_name"] != "pcm_s16le" or int(stream["sample_rate"]) != 32000
                    or stream["channels"] != 2 or not 2 <= float(info["duration"]) <= maximum):
                raise ValueError()
        except (ValueError, KeyError, TypeError, OverflowError):
            raise ValueError("wangp_ref_normalized_audio_required") from None
        return self._typed_copy(descriptor, "audios", ".wav")

    def _typed_copy(self, descriptor, plural, extension):
        from .wangp_receipts import checked_directory, checked_reader, sync_directory
        key = self.key(descriptor)
        folder = checked_directory(self.root / ("typed-" + plural), create=True)
        final = folder / (descriptor.sha256 + extension)
        if not final.exists():
            temporary = folder / (uuid.uuid4().hex + ".part")
            try:
                checksum, size = hashlib.sha256(), 0
                with self.store.open(key) as source, temporary.open("xb") as target:
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        if size > descriptor.size_bytes:
                            raise ValueError("wangp_staged_input_mismatch")
                        checksum.update(chunk)
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if size != descriptor.size_bytes or checksum.hexdigest() != descriptor.sha256:
                    raise ValueError("wangp_staged_input_mismatch")
                os.replace(temporary, final)
                sync_directory(folder)
            finally:
                if temporary.exists():
                    temporary.unlink()
        checksum, size = hashlib.sha256(), 0
        with checked_reader(final, folder) as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                if size > descriptor.size_bytes:
                    raise ValueError("wangp_staged_input_mismatch")
                checksum.update(chunk)
        if size != descriptor.size_bytes or checksum.hexdigest() != descriptor.sha256:
            raise ValueError("wangp_staged_input_mismatch")
        return final


def create_app(host, inputs, *, token):
    if not isinstance(token, str) or not 32 <= len(token) <= 512 or not token.isascii():
        raise ValueError("wangp_private_token_required")
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def authenticate(request, call_next):
        provided = request.headers.get("authorization", "")
        if not hmac.compare_digest(provided.encode(), ("Bearer " + token).encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            return await call_next(request)
        except Exception:
            # Never return raw upstream exception/event/prompt text.
            return JSONResponse({"error": "wangp_private_operation_unknown"}, status_code=503)

    @app.get("/v1/readiness")
    def readiness():
        return asdict(host.readiness())

    @app.post("/v1/operations")
    async def submit(request: Request):
        incarnation = request.headers.get("x-wangp-incarnation")
        if incarnation is not None and (not re.fullmatch(r"[0-9a-f]{32}", incarnation)
                or incarnation != getattr(host, "incarnation", None)):
            # WanGPHost's incarnation is immutable for its process lifetime.
            # This admission precondition closes the readiness/POST race;
            # reads of old durable receipts remain available after restart.
            return JSONResponse({"error": "wangp_runtime_incarnation_mismatch"}, status_code=409)
        try:
            length = int(request.headers.get("content-length", "0"))
            if not 0 < length <= 2 * 1024**2:
                raise ValueError()
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > length:
                    raise ValueError()
            value = json.loads(body)
            value["inputs"] = tuple(InputDescriptor(**v) for v in value.get("inputs", ()))
            prepared = PreparedRequest(**value)
            for descriptor in prepared.inputs:
                inputs.resolve(descriptor)
        except Exception:
            # This status is only used before host.submit can dispatch.
            return JSONResponse({"error": "wangp_request_rejected"}, status_code=422)
        return host.submit(prepared).to_dict()

    @app.get("/v1/operations/{operation_id}")
    def inspect(operation_id: str):
        from ..inference.wangp_http import _operation
        receipt = host.inspect(_operation(operation_id))
        if receipt is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return receipt.to_dict()

    @app.post("/v1/operations/{operation_id}/cancel")
    def cancel(operation_id: str):
        from ..inference.wangp_http import _operation
        return {"acknowledged": host.cancel(_operation(operation_id)) is True}

    @app.get("/v1/operations/{operation_id}/artifacts/{kind}")
    def artifact(operation_id: str, kind: str):
        from ..inference.wangp_http import _operation
        if kind not in {"video", "audio"}:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return StreamingResponse(host.read_artifact(_operation(operation_id), kind),
                                 media_type="application/octet-stream")

    @app.put("/v1/inputs/{handle}")
    async def stage(handle: str, request: Request):
        try:
            header = request.headers.get("x-wangp-input", "")
            if len(header) > 4096:
                raise ValueError()
            descriptor = InputDescriptor(**json.loads(header))
            if handle != descriptor.handle or int(request.headers.get("content-length", "0")) != descriptor.size_bytes:
                raise ValueError()
            inputs.key(descriptor)
            # Bounded spool does not accumulate complete user videos in RAM.
            with tempfile.TemporaryFile() as spool:
                size, checksum = 0, hashlib.sha256()
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > descriptor.size_bytes:
                        raise ValueError()
                    spool.write(chunk)
                    checksum.update(chunk)
                if size != descriptor.size_bytes or checksum.hexdigest() != descriptor.sha256:
                    raise ValueError()
                spool.seek(0)
                inputs.save(descriptor, spool)
            return asdict(descriptor)
        except Exception:
            return JSONResponse({"error": "wangp_input_rejected"}, status_code=422)

    return app
