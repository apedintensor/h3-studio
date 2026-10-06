#!/usr/bin/env python3
"""Public Sixnine connection helper; standard library, no private AI Registry.

Read this file before running it.  A one-time code is supplied through a hidden
prompt or stdin, never a command argument.  A PAT is generated and saved in the
current user's OS credential store *before* only its digest is registered.
This module does not perform network or filesystem operations on import.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

PROTOCOL = "client-held-pat-v1"
MAX_RECORD_BYTES = 32768
MAX_RESPONSE_BYTES = 1024 * 1024
ID = re.compile(r"^connection-[0-9a-f]{32}$")
HEX = re.compile(r"^[0-9a-f]{64}$")
CODE = re.compile(r"^sxc_[A-Za-z0-9_-]{43}$")


class HelperError(Exception):
    """Only static codes, never exception bodies, credentials or HTTP headers."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def checked_origin(value):
    if not isinstance(value, str):
        raise HelperError("invalid_origin")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment
            or parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}):
        raise HelperError("https_or_loopback_origin_required")
    try:
        parsed.port
    except ValueError:
        raise HelperError("invalid_origin") from None
    return parsed.scheme + "://" + parsed.netloc.lower()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise HelperError("redirect_rejected")


class JsonHTTP:
    def __init__(self, origin):
        self.origin = checked_origin(origin)
        self.opener = build_opener(_NoRedirect())

    def request(self, path, *, method="GET", body=None, token=None, idempotency_key=None):
        if not isinstance(path, str) or not path.startswith(("/v1/", "/api/auth/me")) or path.startswith("//"):
            raise HelperError("invalid_api_path")
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment or any(c in path for c in ("\r", "\n", "\\")):
            raise HelperError("invalid_api_path")
        if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            raise HelperError("invalid_method")
        raw = canonical(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", idempotency_key):
                raise HelperError("invalid_idempotency_key")
            headers["Idempotency-Key"] = idempotency_key
        request = Request(self.origin + path, data=raw, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise HelperError("response_too_large")
            value = json.loads(data)
            if not isinstance(value, dict):
                raise HelperError("invalid_response")
            return value
        except HTTPError as error:
            # Parse only our allowlisted code; do not echo server body/URL.
            try:
                value = json.loads(error.read(8192))
                code = value.get("code") if isinstance(value, dict) else None
            except (ValueError, UnicodeError):
                code = None
            allowed = {"connection_not_exchanged", "connection_expired", "connection_consumed",
                "connection_unavailable", "connection_authority_changed", "connection_exchange_limited",
                "connection_authorization_mismatch", "connection_key_conflict", "too_many_active_keys"}
            raise HelperError(code if code in allowed else "http_request_failed") from None
        except (URLError, TimeoutError, OSError):
            raise HelperError("network_result_unknown") from None
        except (ValueError, UnicodeError):
            raise HelperError("invalid_response") from None


def _dpapi(raw, *, protect):
    """User-scoped Windows DPAPI, UI forbidden; no machine-wide protection."""
    if os.name != "nt":
        raise HelperError("windows_dpapi_unavailable")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]
    incoming = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(incoming, ctypes.POINTER(ctypes.c_byte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    action = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    action.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                      ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    action.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not action(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise HelperError("os_credential_storage_failed")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


class SecureStore:
    """Encrypted DPAPI blobs or existing Linux Secret Service; never plain JSON."""
    def __init__(self):
        self.platform = "windows" if os.name == "nt" else "linux" if sys.platform.startswith("linux") else "unsupported"
        if self.platform == "windows":
            location = os.environ.get("LOCALAPPDATA")
            if not location or not Path(location).is_absolute():
                raise HelperError("os_credential_storage_unavailable")
            self.directory = Path(location) / "Sixnine" / "agent-credentials"
        elif self.platform == "linux":
            self.command = shutil.which("secret-tool")
            if not self.command or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
                raise HelperError("os_credential_storage_unavailable")
            self.lock_directory = Path(tempfile.gettempdir()) / ("sixnine-agent-locks-" + str(os.getuid()))
        else:
            raise HelperError("os_credential_storage_unavailable")

    @staticmethod
    def reference(origin, connection_id):
        origin = checked_origin(origin)
        if not isinstance(connection_id, str) or not ID.fullmatch(connection_id):
            raise HelperError("invalid_connection_id")
        return digest(origin + "|" + connection_id)

    def _directory(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        for path in [self.directory, self.directory.parent]:
            info = path.lstat()
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise HelperError("credential_storage_link_rejected")

    def _secret_service(self, action, reference, payload=None):
        args = [self.command, action]
        if action == "store":
            args += ["--label=Sixnine Agent credential"]
        args += ["application", "sixnine-agent", "reference", reference]
        try:
            result = subprocess.run(args, input=payload, capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise HelperError("os_credential_storage_failed") from None
        if result.returncode != 0:
            if action == "lookup" and result.returncode == 1:
                return None
            raise HelperError("os_credential_storage_failed")
        if len(result.stdout) > MAX_RECORD_BYTES:
            raise HelperError("invalid_credential_record")
        return result.stdout

    @contextmanager
    def _linux_lock(self, reference):
        import fcntl
        self.lock_directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        metadata = self.lock_directory.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or self.lock_directory.is_symlink()
                or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077):
            raise HelperError("credential_storage_link_rejected")
        descriptor = os.open(self.lock_directory / (reference + ".lock"),
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise HelperError("credential_storage_link_rejected")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def _preserve_record(existing, incoming):
        if existing and (existing.get("expected") != incoming.get("expected")
                or existing.get("api_key") != incoming.get("api_key")):
            raise HelperError("saved_authorization_mismatch")
        return existing if existing and existing.get("status") == "connected" and incoming.get("status") != "connected" else incoming

    def save(self, origin, connection_id, record, *, replace=False):
        reference = self.reference(origin, connection_id)
        raw = canonical(record).encode("utf-8")
        if len(raw) > MAX_RECORD_BYTES:
            raise HelperError("invalid_credential_record")
        if self.platform == "linux":
            # Secret Service's store operation itself is not create-if-absent.
            # Serialize local writers without putting credentials in lockfiles.
            with self._linux_lock(reference):
                existing = self.load(origin, connection_id)
                if not replace and existing is not None:
                    raise HelperError("connection_already_saved")
                record = self._preserve_record(existing, record)
                raw = canonical(record).encode("utf-8")
                self._secret_service("store", reference, raw)
                if self.load(origin, connection_id) != record:
                    raise HelperError("os_credential_storage_failed")
            return
        self._directory()
        target = self.directory / (reference + ".dpapi")
        if not replace and target.exists():
            raise HelperError("connection_already_saved")
        if target.exists() and (target.is_symlink() or getattr(target.lstat(), "st_file_attributes", 0) & 0x400):
            raise HelperError("credential_storage_link_rejected")
        if replace:
            record = self._preserve_record(self.load(origin, connection_id), record)
            raw = canonical(record).encode("utf-8")
        encrypted = _dpapi(raw, protect=True)
        temporary = self.directory / (reference + "." + secrets.token_hex(8) + ".tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, target)
            else:
                # Windows rename refuses an existing target, preserving an
                # earlier process's token if two agents connect concurrently.
                os.rename(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        if self.load(origin, connection_id) != record:
            raise HelperError("os_credential_storage_failed")

    def load(self, origin, connection_id):
        reference = self.reference(origin, connection_id)
        if self.platform == "linux":
            raw = self._secret_service("lookup", reference)
            if raw is None:
                return None
        else:
            target = self.directory / (reference + ".dpapi")
            if not target.exists():
                return None
            self._directory()
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or target.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise HelperError("credential_storage_link_rejected")
            with target.open("rb") as stream:
                encrypted = stream.read(MAX_RECORD_BYTES + 1)
            if len(encrypted) > MAX_RECORD_BYTES:
                raise HelperError("invalid_credential_record")
            raw = _dpapi(encrypted, protect=False)
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, UnicodeError):
            raise HelperError("invalid_credential_record") from None


class AgentClient:
    def __init__(self, origin, *, store=None, http=None):
        self.origin = checked_origin(origin)
        self.store = store if store is not None else SecureStore()
        self.http = http if http is not None else JsonHTTP(self.origin)

    def connect(self, *, code, connection_id, owner, tenant, fingerprint,
                profile_id="creator-full", profile_version=1):
        if not isinstance(code, str) or not CODE.fullmatch(code) or not HEX.fullmatch(fingerprint or ""):
            raise HelperError("invalid_connection_instruction")
        if not ID.fullmatch(connection_id or "") or not isinstance(owner, str) or not isinstance(tenant, str):
            raise HelperError("invalid_connection_instruction")
        existing = self.store.load(self.origin, connection_id)
        expected = {"origin": self.origin, "connection_id": connection_id, "owner": owner, "tenant": tenant,
                    "profile_id": profile_id, "profile_version": profile_version, "fingerprint": fingerprint}
        if existing:
            if existing.get("expected") != expected:
                raise HelperError("saved_authorization_mismatch")
            return self.resume(connection_id)
        state = {"protocol": PROTOCOL, "status": "pending", "expected": expected,
            "code": code, "api_key": "sxp_" + secrets.token_urlsafe(32), "verifier": secrets.token_urlsafe(32)}
        self.store.save(self.origin, connection_id, state)
        return self._exchange(state, recovery=False)

    def _exchange(self, state, *, recovery):
        expected = state["expected"]
        body = {"code": state["code"], "client_challenge": digest(state["verifier"]),
            "token_hash": digest(state["api_key"]), "key_prefix": state["api_key"][:12],
            "expected_authorization_fingerprint": expected["fingerprint"]}
        if recovery:
            body["recovery_verifier"] = state["verifier"]
        try:
            result = self.http.request("/v1/agent-connect/exchange", method="POST", body=body)
        except HelperError:
            state["status"] = "unknown"
            self.store.save(self.origin, expected["connection_id"], state, replace=True)
            raise
        connection = result.get("connection", {})
        authorization = connection.get("authorization", {})
        key = result.get("key", {})
        if (not isinstance(authorization, dict) or digest(canonical(authorization)) != expected["fingerprint"]
                or connection.get("authorization_fingerprint") != expected["fingerprint"]
                or any(authorization.get(k) != v for k, v in expected.items() if k != "fingerprint")
                or connection.get("id") != expected["connection_id"] or key.get("id") != connection.get("key_id")
                or key.get("prefix") != state["api_key"][:12] or "api_key" in result
                or key.get("scopes") != authorization.get("scopes")
                or key.get("expires_at") != authorization.get("key_expires_at")
                or connection.get("status") != "connected"):
            state["status"] = "unknown"
            self.store.save(self.origin, expected["connection_id"], state, replace=True)
            raise HelperError("exchange_authorization_mismatch")
        state.update(status="connected", key_id=key["id"], expires_at=key["expires_at"])
        # Keep encrypted recovery material until local activation is safely
        # stored.  No raw key reaches the public result or stdout.
        self.store.save(self.origin, expected["connection_id"], state, replace=True)
        return {"status": "connected", "connection_id": connection["id"], "owner": authorization["owner"],
            "origin": self.origin, "profile_id": authorization["profile_id"], "profile_version": authorization["profile_version"],
            "key_id": key["id"], "expires_at": key["expires_at"], "recovered": bool(result.get("recovered"))}

    def resume(self, connection_id):
        state = self.store.load(self.origin, connection_id)
        if not state or state.get("protocol") != PROTOCOL or state.get("expected", {}).get("origin") != self.origin:
            raise HelperError("saved_connection_unavailable")
        if state.get("status") == "connected":
            # This is historical local activation, not a live health check.
            return {"status": "connected_locally", "connection_id": connection_id, "owner": state["expected"]["owner"],
                "origin": self.origin, "key_id": state["key_id"], "online_verified": False}
        try:
            return self._exchange(state, recovery=True)
        except HelperError as error:
            if str(error) != "connection_not_exchanged":
                raise
            # Definite unclaimed state: retry the exact saved original material.
            return self._exchange(state, recovery=False)

    def request(self, connection_id, path, *, method="GET", body=None, idempotency_key=None):
        state = self.store.load(self.origin, connection_id)
        if (not state or state.get("status") != "connected" or state.get("protocol") != PROTOCOL
                or state.get("expected", {}).get("origin") != self.origin):
            raise HelperError("saved_connection_not_active")
        return self.http.request(path, method=method, body=body, token=state["api_key"], idempotency_key=idempotency_key)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Connect Sixnine without printing or exporting API credentials")
    parser.add_argument("command", choices=("connect", "resume", "whoami", "request"))
    parser.add_argument("--server", required=True, help="Exact HTTPS origin; loopback HTTP only for local preview")
    parser.add_argument("--connection", required=True, help="Public connection ID from the website")
    parser.add_argument("--account", help="Expected account from the website")
    parser.add_argument("--tenant", default="sixnine")
    parser.add_argument("--fingerprint", help="Public authorization fingerprint from the website")
    parser.add_argument("--profile", default="creator-full")
    parser.add_argument("--profile-version", type=int, default=1)
    parser.add_argument("--code-stdin", action="store_true", help="Read the one-time code from stdin, never a CLI argument")
    parser.add_argument("--method", choices=("GET", "POST", "PATCH", "PUT", "DELETE"), default="GET")
    parser.add_argument("--path", help="Same-origin API path, never an absolute URL")
    parser.add_argument("--input", help="JSON business request file; '-' reads stdin. Do not include credentials")
    parser.add_argument("--idempotency-key", help="Stable public operation identifier; keep it after an uncertain response")
    args = parser.parse_args(argv)
    try:
        client = AgentClient(args.server)
        if args.command == "connect":
            if not args.account or not args.fingerprint:
                raise HelperError("connection_instruction_required")
            code = sys.stdin.read(256).strip() if args.code_stdin else getpass.getpass("One-time connection code: ")
            result = client.connect(code=code, connection_id=args.connection, owner=args.account, tenant=args.tenant,
                fingerprint=args.fingerprint, profile_id=args.profile, profile_version=args.profile_version)
        elif args.command == "resume":
            result = client.resume(args.connection)
        elif args.command == "whoami":
            value = client.request(args.connection, "/api/auth/me")
            result = {k: value.get(k) for k in ("username", "machine", "authentication", "scopes", "all_projects")}
        else:
            if not args.path:
                raise HelperError("api_path_required")
            value = None
            if args.input:
                if args.input == "-":
                    raw = sys.stdin.buffer.read(MAX_RESPONSE_BYTES + 1)
                else:
                    with Path(args.input).open("rb") as source:
                        raw = source.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise HelperError("business_request_too_large")
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise HelperError("invalid_business_json") from None
                if not isinstance(value, dict):
                    raise HelperError("invalid_business_json")
            result = client.request(args.connection, args.path, method=args.method, body=value,
                idempotency_key=args.idempotency_key)
        print(canonical(result))
        return 0
    except (HelperError, OSError):
        error = sys.exc_info()[1]
        print(canonical({"status": "not_connected", "code": str(error) if isinstance(error, HelperError) else "local_storage_failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
