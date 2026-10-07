"""Fail closed on the actual OIDC caller before protected release operations."""
from __future__ import annotations

import sys

EXPECTED_ACCOUNT = "829135631045"
REGION = "ap-southeast-1"


def verify_account(*, client=None):
    if client is None:
        import boto3
        from botocore.config import Config

        # Use the same existing OIDC environment as the following release tool.
        # Pin the official endpoint; never copy/export credentials or trust an
        # action input that the currently pinned action does not implement.
        client = boto3.client("sts", region_name=REGION,
            endpoint_url="https://sts.ap-southeast-1.amazonaws.com",
            config=Config(connect_timeout=10, read_timeout=15,
                retries={"mode": "standard", "total_max_attempts": 1}, signature_version="v4"))
    identity = client.get_caller_identity()
    if not isinstance(identity, dict) or identity.get("Account") != EXPECTED_ACCOUNT:
        raise RuntimeError("AWS release caller account mismatch or missing")


def main():
    try:
        verify_account()
    except Exception:
        # No provider diagnostics, ARN, token, credential or response dump.
        print("AWS release caller account unconfirmed; release operation blocked", file=sys.stderr)
        return 1
    print("AWS release caller account verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
