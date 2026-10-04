"""Small authenticated Sixnine client. Requires httpx; never logs credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

import httpx


def origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise ValueError("Use an exact HTTPS origin (HTTP allowed only on loopback)")
    return value.rstrip("/")


def api_path(value):
    parsed = urlsplit(value)
    if (parsed.scheme or parsed.netloc or parsed.fragment or "\\" in value
            or any(ord(c) < 32 for c in value) or not value.startswith("/v1/")
            or "%" in parsed.path or "/../" in value or "/./" in value):
        raise ValueError("Use a relative /v1/ API path")
    if parsed.path.startswith(("/v1/api-keys", "/v1/auth")):
        raise ValueError("Manage account credentials in the website, not this client")
    return value


def credential(args):
    if args.profile:
        if not args.registry_root:
            raise ValueError("Specify the existing registry root with a profile")
        sys.path.insert(0, str(Path(args.registry_root).resolve()))
        from api_registry import load_api
        config = load_api("sixnine", profile=args.profile)
        if not config.base_url or origin(config.base_url) != args.base_url:
            raise ValueError("Registry profile endpoint does not match the chosen origin")
        value = config.api_key
    else:
        value = os.environ.get("SIXNINE_API_KEY")
    if not value or not value.isascii() or any(c.isspace() for c in value):
        raise ValueError("A valid process-only credential or existing registry profile is required")
    return value


def write_json(result, destination=None):
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if destination:
        with Path(destination).open("x", encoding="utf-8") as handle:
            handle.write(text)
        print("Response saved without overwriting an existing file")
    else:
        print(text, end="")


def check_response(response):
    if not 200 <= response.status_code < 300:
        retry = response.headers.get("retry-after", "")
        suffix = " (retry delay supplied by server)" if retry else ""
        raise ValueError(f"API returned HTTP {response.status_code}{suffix}; response details suppressed")


def run(args, transport=None):
    args.base_url = origin(args.base_url)
    token = credential(args)
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if getattr(args, "idempotency_key", None):
        headers["Idempotency-Key"] = args.idempotency_key
    with httpx.Client(base_url=args.base_url, headers=headers, follow_redirects=False,
                      timeout=httpx.Timeout(300, connect=15), trust_env=False, transport=transport) as client:
        if args.command == "request":
            path = api_path(args.path)
            body = json.loads(Path(args.json_file).read_text(encoding="utf-8")) if args.json_file else None
            response = client.request(args.method, path, json=body)
            check_response(response)
            if len(response.content) > 8 * 1024 * 1024:
                raise ValueError("JSON response exceeded the expected limit")
            write_json(response.json(), args.output)
        elif args.command == "upload":
            path = Path(args.file)
            if not path.is_file() or path.stat().st_size > 512 * 1024 * 1024:
                raise ValueError("Input must be an existing file of at most 512 MiB")
            with path.open("rb") as source:
                response = client.post("/v1/assets", data={"client_project_id": args.project,
                    "client_asset_id": args.asset_id}, files={"file": (path.name, source)})
            check_response(response)
            write_json(response.json(), args.output)
        elif args.command == "download":
            path = api_path(args.path)
            target = Path(args.output)
            # Exclusive creation prevents overwriting a previous take or receipt.
            with client.stream("GET", path) as response:
                check_response(response)
                digest, size = hashlib.sha256(), 0
                with target.open("xb") as output:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > args.max_bytes:
                            raise ValueError("Download limit exceeded; partial file retained for inspection")
                        digest.update(chunk)
                        output.write(chunk)
            actual = digest.hexdigest()
            if args.sha256 and actual != args.sha256.lower():
                raise ValueError("SHA-256 mismatch; do not adopt the downloaded file")
            write_json({"bytes": size, "sha256": actual, "verified": bool(args.sha256)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--registry-root")
    parser.add_argument("--profile")
    commands = parser.add_subparsers(dest="command", required=True)
    req = commands.add_parser("request")
    req.add_argument("method", choices=["GET", "POST", "PUT", "DELETE"])
    req.add_argument("path")
    req.add_argument("--json-file")
    req.add_argument("--idempotency-key")
    req.add_argument("--output")
    upload = commands.add_parser("upload")
    upload.add_argument("--project", required=True)
    upload.add_argument("--asset-id", required=True)
    upload.add_argument("--file", required=True)
    upload.add_argument("--output")
    download = commands.add_parser("download")
    download.add_argument("path")
    download.add_argument("--output", required=True)
    download.add_argument("--sha256")
    download.add_argument("--max-bytes", type=int, default=512 * 1024 * 1024)
    try:
        run(parser.parse_args())
    except ValueError as error:
        # JSON parser errors may contain user content; do not print those errors.
        print("Invalid JSON input or response" if isinstance(error, json.JSONDecodeError) else str(error), file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt):
        print("Operation incomplete; details suppressed. Reconcile the original request before retrying a write.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
