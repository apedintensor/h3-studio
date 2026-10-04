"""Conditionally publish a verified frontend bundle; never approve or deploy it."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.platform.frontend_bundle import ARCHIVE_NAME, MANIFEST_NAME, bundle_payloads, digest
from tools.publish_aws_release import BUCKET, REGION


def publish(commit, directory, *, client=None):
    manifest, payloads = bundle_payloads(directory, commit)
    # GitHub OIDC uses the existing SDK credential chain, never a copied key.
    s3 = client or boto3.client("s3", region_name=REGION,
        endpoint_url="https://s3.ap-southeast-1.amazonaws.com",
        config=Config(connect_timeout=15, read_timeout=300,
                      retries={"mode": "standard", "total_max_attempts": 3}))
    for name in (ARCHIVE_NAME, MANIFEST_NAME):
        raw = payloads[name]
        checksum = digest(raw)
        key = "releases/" + commit + "/frontend/" + name
        try:
            # Managed multipart transfers cannot express this create-only write.
            s3.put_object(Bucket=BUCKET, Key=key, Body=raw, IfNoneMatch="*",
                ContentLength=len(raw), ServerSideEncryption="AES256", Metadata={"sha256": checksum},
                ContentType="application/gzip" if name == ARCHIVE_NAME else "application/json")
        except ClientError as error:
            # S3 exposes no modeled PreconditionFailed exception; only this exact
            # conditional-write failure can be reconciled as an idempotent retry.
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise
            existing = s3.head_object(Bucket=BUCKET, Key=key)
            if existing.get("Metadata", {}).get("sha256") != checksum or existing.get("ContentLength") != len(raw):
                raise ValueError("Existing immutable frontend release differs; operator investigation required") from None
    return {"commit": manifest["commit"], "bucket": BUCKET,
        "manifest_sha256": digest(payloads[MANIFEST_NAME]),
        "status": "published_pending_independent_host_approval"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        result = publish(args.commit, args.directory)
    except Exception:
        print("Frontend publication failed; no host deployment was requested", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
