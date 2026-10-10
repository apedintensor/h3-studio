"""Protected first-boot files and API token capture; never print secrets.

This tool starts no services, does not change the business database and does not
rent GPUs. Compose service startup is a separate explicit operator action.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import uuid


def protected_write(path, contents):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
        stream.write(contents)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "token"))
    parser.add_argument("--secret-dir", required=True, type=Path)
    parser.add_argument("--project", default="sixnine-hatchet")
    parser.add_argument("--tenant-id", default="707d0855-80ab-4e1f-a156-f1c4546cbf52")
    parser.add_argument("--token-name", default="sixnine-cpu-dispatch")
    parser.add_argument("--expires-in", default="2160h")
    args = parser.parse_args()
    if os.name == "nt":
        raise ValueError("hatchet_bootstrap_requires_linux_protected_runtime")
    directory = args.secret_dir
    if not directory.is_absolute() or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,80}", args.project):
        raise ValueError("invalid_hatchet_bootstrap_path_or_project")
    if directory.is_symlink():
        raise ValueError("hatchet_secret_directory_symlink_rejected")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt" and directory.stat().st_mode & 0o077:
        raise ValueError("hatchet_secret_directory_must_be_0700")
    if args.action == "init":
        for name in ("database-password", "administrator-password"):
            destination = directory/name
            if not destination.exists():
                protected_write(destination, secrets.token_urlsafe(48))
        print("protected_initial_files_ready; existing_credentials_preserved")
        return
    destination = directory/"hatchet-token"
    if destination.exists():
        raise ValueError("hatchet_token_exists_rotation_requires_explicit_plan")
    if (not re.fullmatch(r"[0-9]{1,6}h", args.expires_in)
            or not 1 <= int(args.expires_in[:-1]) <= 8760):
        raise ValueError("invalid_hatchet_token_expiration")
    try:
        uuid.UUID(args.tenant_id)
    except ValueError:
        raise ValueError("invalid_hatchet_tenant_id") from None
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", args.token_name):
        raise ValueError("invalid_hatchet_token_name")
    # No token on command arguments/environment. CLI stdout is captured in
    # memory, verified and written directly into a new mode-0600 file.
    environment = dict(os.environ, SIXNINE_HATCHET_SECRET_DIR=str(directory))
    result = subprocess.run(["docker", "compose", "-f", str(Path(__file__).with_name("compose.yaml")),
        "-p", args.project, "exec", "-T", "hatchet", "/hatchet-admin", "token", "create",
        "--config", "/config", "--tenant-id", args.tenant_id, "--name", args.token_name,
        "--expiresIn", args.expires_in], env=environment, capture_output=True, text=True, timeout=30)
    tokens = re.findall(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", result.stdout)
    if result.returncode != 0 or len(tokens) != 1:
        raise ValueError("hatchet_protected_token_capture_failed")
    protected_write(destination, tokens[0]+"\n")
    print(json.dumps({"state": "protected_token_ready", "token_file": str(destination),
        "expires_in": args.expires_in}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # CLI process errors may contain a secret DSN or raw broker response.
        code = str(error) if isinstance(error, ValueError) and re.fullmatch(r"[a-z0-9_]+", str(error)) else "hatchet_bootstrap_failed"
        raise SystemExit(code) from None
