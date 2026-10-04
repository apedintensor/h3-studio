"""Publish a tested, immutable bundle through GitHub OIDC; never approve a host release."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

BUCKET = "sixnine-platform-releases-829135631045-ap-southeast-1"
REGION = "ap-southeast-1"
FILES = ("image.tar.gz", "compose.yaml", "Caddyfile", "init_database.py", "check_config.py", "release-manifest.json")


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def publish(commit, directory):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("An exact commit is required")
    directory = Path(directory)
    manifest = json.loads((directory / "release-manifest.json").read_text(encoding="utf-8"))
    if manifest["commit"] != commit or set(manifest["files"]) != set(FILES)-{"release-manifest.json"}:
        raise ValueError("Invalid release manifest")
    hashes = {}
    for name in FILES:
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4*1024**3:
            raise ValueError("Unexpected bundle file or size")
        hashes[name] = checksum(path)
        if name != "release-manifest.json" and hashes[name] != manifest["files"][name]:
            raise ValueError("Bundle hash mismatch")
    s3 = boto3.client("s3", region_name=REGION, endpoint_url="https://s3.ap-southeast-1.amazonaws.com",
        config=Config(connect_timeout=15, read_timeout=300, retries={"mode": "standard", "max_attempts": 2}))
    # Conditional writes make retries/reruns unable to replace an approved object.
    for name in FILES:
        key = "releases/" + commit + "/" + name
        try:
            with (directory / name).open("rb") as body:
                s3.put_object(Bucket=BUCKET, Key=key, Body=body, IfNoneMatch="*",
                    ServerSideEncryption="AES256", Metadata={"sha256": hashes[name]})
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise
            existing = s3.head_object(Bucket=BUCKET, Key=key)
            if existing.get("Metadata", {}).get("sha256") != hashes[name] or existing["ContentLength"] != (directory/name).stat().st_size:
                raise ValueError("Existing immutable release differs; operator investigation required") from None
    print(json.dumps({"commit": commit, "bucket": BUCKET, "manifest_sha256": hashes["release-manifest.json"],
        "status": "published_pending_independent_host_approval"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        publish(args.commit, args.directory)
    except Exception:
        print("Release publication failed; no host deployment was requested", file=sys.stderr)
        raise SystemExit(1)
