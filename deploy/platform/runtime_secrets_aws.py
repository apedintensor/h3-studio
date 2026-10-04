#!/usr/bin/python3
"""Host-only Secrets Manager initialization/hydration. Never emit secret values."""
import argparse
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import uuid
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = "ap-southeast-1"
DATABASE = "/sixnine/platform/database"
ACCOUNTS = "/sixnine/platform/bootstrap-accounts"
ROOT = Path("/run/sixnine-secrets")


def client():
    return boto3.client("secretsmanager", region_name=REGION,
        endpoint_url="https://secretsmanager.ap-southeast-1.amazonaws.com",
        config=Config(connect_timeout=5, read_timeout=20, retries={"mode": "standard", "max_attempts": 3}))


def value(api, name, initialize=False):
    try:
        result = json.loads(api.get_secret_value(SecretId=name)["SecretString"])
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException" or not initialize:
            raise RuntimeError("Required managed secret is unavailable") from None
        # The operator creates the named secret container first. Missing versions
        # may be initialized only before any production database was created.
        api.describe_secret(SecretId=name)
        if Path("/srv/sixnine/postgres/pgdata/PG_VERSION").exists():
            raise RuntimeError("Existing database requires recovery of original secrets")
        if name == DATABASE:
            result = {"db_admin_password": secrets.token_urlsafe(48),
                "app_database_url": "postgresql+psycopg://sixnine_app:"+secrets.token_urlsafe(48)+"@db:5432/sixnine"}
        elif name == ACCOUNTS:
            result = {user: secrets.token_urlsafe(30) for user in ("superdan", "supervan")}
        else:
            raise RuntimeError("Unknown secret source")
        api.put_secret_value(SecretId=name, ClientRequestToken=str(uuid.uuid4()),
            SecretString=json.dumps(result))
    expected = {"db_admin_password", "app_database_url"} if name == DATABASE else {"superdan", "supervan"}
    if not isinstance(result, dict) or set(result) != expected or any(
            not isinstance(v, str) or not 32 <= len(v) <= 512 or any(c in v for c in "\n\r\0") for v in result.values()):
        raise RuntimeError("Managed secret structure is invalid")
    if name == DATABASE:
        parsed = urlsplit(result['app_database_url'])
        if (parsed.scheme != 'postgresql+psycopg' or parsed.hostname != 'db'
                or parsed.port != 5432 or parsed.path != '/sixnine'
                or parsed.username != 'sixnine_app' or not parsed.password
                or parsed.query or parsed.fragment):
            raise RuntimeError('Managed database endpoint is invalid')
    return result


def write_runtime(name, content):
    if name not in {"db_admin_password", "app_database_url"}:
        raise RuntimeError("Unexpected runtime secret")
    ROOT.mkdir(mode=0o700, exist_ok=True)
    info = ROOT.lstat()
    if (not stat.S_ISDIR(info.st_mode) or (info.st_uid, info.st_gid) != (0, 0)
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError("Runtime secret directory is not protected")
    path = ROOT / name
    # Preserve Docker's already-bound inode. Repeated hydration compares exact
    # bytes without truncating; an unexpected rotation requires separate work.
    mode, group = (0o400, 0) if name == 'db_admin_password' else (0o440, 10001)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, mode)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0:
            raise RuntimeError("Runtime secret file is invalid")
        os.fchmod(fd, mode)
        os.fchown(fd, 0, group)
        if info.st_size:
            if info.st_size > 16384 or os.read(fd, 16385) != content.encode('utf-8'):
                raise RuntimeError('Runtime secret changed; explicit rotation required')
            return
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "w", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def hydrate(initialize=False):
    if os.geteuid() != 0:
        raise RuntimeError("Host operator privileges required")
    import fcntl
    # Single host initializer; never mint independent competing secret versions.
    with Path('/opt/sixnine-release/secret-hydration.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        api = client()
        database = value(api, DATABASE, initialize)
        if initialize:
            value(api, ACCOUNTS, True)
        for name, content in database.items():
            write_runtime(name, content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args()
    try:
        hydrate(args.initialize)
        print("Runtime secret files hydrated; no values emitted")
    except Exception:
        print("Managed secret hydration failed; details suppressed", file=sys.stderr)
        raise SystemExit(1)
