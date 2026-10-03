#!/usr/bin/python3
"""Root-owned release controller, installed once under /opt/sixnine-release.

Only accepts a commit ID. Never execute an incoming release script through sudo.
No AWS, DNS, GPU provisioning or credential generation occurs here.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tarfile
import time

from check_config import validate

ROOT = Path("/srv/sixnine")
DOCKER = "/usr/bin/docker"
FILES = {"image.tar.gz", "compose.yaml", "Caddyfile", "init_database.py", "check_config.py"}
ENV_FIELDS = {"SIXNINE_IMAGE", "SIXNINE_POSTGRES_IMAGE", "SIXNINE_CADDY_IMAGE",
              "SIXNINE_DB_ADMIN_SECRET_FILE", "SIXNINE_APP_DSN_SECRET_FILE"}
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class ReleaseError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise ReleaseError(code)


def regular(path, *, root_owned=False, maximum=None):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "release_file_not_regular")
    if root_owned and os.name != "nt":
        require(info.st_uid == 0 and not info.st_mode & 0o022, "release_file_not_root_controlled")
    if maximum is not None:
        require(info.st_size <= maximum, "release_file_size_exceeded")
    return info


def checksum(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024*1024), b""):
            result.update(chunk)
    return result.hexdigest()


def manifest(directory, commit):
    require(bool(SHA.fullmatch(commit)), "invalid_commit")
    regular(directory / "release-manifest.json", maximum=16384)
    try:
        value = json.loads((directory / "release-manifest.json").read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise ReleaseError("invalid_release_manifest") from None
    require(isinstance(value, dict) and set(value) == {"commit", "image", "image_id", "files"}
            and value["commit"] == commit and value["image"] == "sixnine-platform:"+commit
            and isinstance(value["image_id"], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value["image_id"])
            and isinstance(value["files"], dict) and set(value["files"]) == FILES, "invalid_release_manifest")
    for name, expected in value["files"].items():
        require(isinstance(expected, str) and bool(DIGEST.fullmatch(expected)), "invalid_release_checksum")
        regular(directory / name, maximum=2*1024**3 if name == "image.tar.gz" else 1024**2)
        require(checksum(directory / name) == expected, "release_checksum_mismatch")
    return value


def approved_manifest(root, directory, commit):
    """Independent host approval, never written by the incoming/deploy identity.

    A self-declared checksum/revision is integrity metadata, not provenance.
    The operator approves the manifest hash from the reviewed CI artifact via
    a separate root channel. Private GitHub attestation support is not assumed.
    """
    path = root / "approved-releases" / (commit+".sha256")
    try:
        parent = path.parent.lstat()
        regular(path, root_owned=True, maximum=128)
    except FileNotFoundError:
        raise ReleaseError("independent_release_approval_missing") from None
    require(stat.S_ISDIR(parent.st_mode) and (os.name == "nt" or parent.st_uid == 0 and not parent.st_mode & 0o022),
            "release_approval_directory_not_protected")
    digest = path.read_text(encoding="ascii").strip()
    require(bool(DIGEST.fullmatch(digest)) and digest == checksum(directory / "release-manifest.json"),
            "release_has_no_matching_independent_approval")


def validate_image_archive(path, expected):
    """Inspect without extracting; reject extra image tags before docker load."""
    entries, headers, total = set(), {}, 0
    with tarfile.open(path, mode="r|gz") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            require(name and name not in entries and "\\" not in name and not name.startswith("/")
                    and all(part not in {"", ".", ".."} for part in name.split("/")), "unsafe_image_archive_path")
            entries.add(name)
            require(member.isfile() or member.isdir(), "image_archive_links_forbidden")
            total += member.size
            require(len(entries) <= 10000 and total <= 4*1024**3 and member.size <= 2*1024**3,
                    "image_archive_expansion_limit")
            if name in {"manifest.json", "index.json"}:
                require(member.isfile() and member.size <= 1024**2, "invalid_image_archive_metadata")
                headers[name] = json.load(archive.extractfile(member))
    manifests = headers.get("manifest.json")
    require(isinstance(manifests, list) and len(manifests) == 1 and isinstance(manifests[0], dict),
            "image_archive_requires_one_image")
    value = manifests[0]
    require(value.get("RepoTags") == [expected["image"]]
            and isinstance(value.get("Layers"), list) and all(x in entries for x in value["Layers"])
            and value.get("Config") in entries, "image_archive_identity_mismatch")
    if "index.json" in headers:
        index = headers["index.json"]
        require(isinstance(index, dict) and isinstance(index.get("manifests"), list) and len(index["manifests"]) == 1,
                "image_archive_requires_one_oci_reference")
        reference = index["manifests"][0]
        annotations = reference.get("annotations", {})
        require(reference.get("digest") == expected["image_id"]
                and annotations.get("io.containerd.image.name") in {expected["image"], "docker.io/library/"+expected["image"]}
                and annotations.get("org.opencontainers.image.ref.name") == expected["commit"],
                "oci_image_reference_mismatch")
    else:
        require(value["Config"] == expected["image_id"].removeprefix("sha256:")+".json",
                "legacy_image_id_mismatch")


def load_approved_image(root, directory, commit, environment):
    expected = manifest(directory, commit)
    approved_manifest(root, directory, commit)
    validate_image_archive(directory / "image.tar.gz", expected)
    command(["load", "--input", str(directory / "image.tar.gz")], environment=environment, timeout=300)
    image = json.loads(command(["image", "inspect", expected["image"]], environment=environment))[0]
    require(image.get("Id") == expected["image_id"]
            and image.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision") == commit,
            "loaded_image_does_not_match_approved_release")
    return expected


def deployment_environment(path, commit):
    regular(path, root_owned=True, maximum=16384)
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        require(separator == "=" and key in ENV_FIELDS and key not in values, "invalid_nonsecret_site_config")
        require(value == value.strip() and "\x00" not in value, "invalid_nonsecret_site_config")
        values[key] = value
    require(set(values) == ENV_FIELDS, "incomplete_site_config")
    for key in ("SIXNINE_POSTGRES_IMAGE", "SIXNINE_CADDY_IMAGE"):
        require(bool(re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}", values[key])), "unapproved_dependency_image")
    for key, name in (("SIXNINE_DB_ADMIN_SECRET_FILE", "db_admin_password"), ("SIXNINE_APP_DSN_SECRET_FILE", "app_database_url")):
        require(values[key] == "/run/sixnine-secrets/"+name, "unexpected_runtime_secret_path")
    values["SIXNINE_IMAGE"] = "sixnine-platform:"+commit
    # No shell expansion/dotenv inheritance and no cloud credentials in children.
    return {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
            "DOCKER_CONFIG": "/opt/sixnine-release/docker-config", **values}


def command(arguments, *, environment, timeout=180, input_data=None):
    try:
        result = subprocess.run([DOCKER, "--host", "unix:///var/run/docker.sock", *arguments],
            input=input_data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, timeout=timeout, check=True)
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        raise ReleaseError("container_operation_failed_no_details_logged") from None


def compose(directory, environment, *arguments, timeout=180):
    return command(["compose", "--project-directory", str(directory), "-f", str(directory / "compose.yaml"), *arguments],
                   environment=environment, timeout=timeout)


def approved_configuration(directory, environment):
    config = json.loads(compose(directory, environment, "config", "--format", "json"))
    validate(config, deployment_directory=directory)
    require(config["services"]["app"]["image"] == environment["SIXNINE_IMAGE"]
            and config["services"]["db"]["image"] == environment["SIXNINE_POSTGRES_IMAGE"]
            and config["services"]["caddy"]["image"] == environment["SIXNINE_CADDY_IMAGE"],
            "release_images_differ_from_trusted_site_config")
    return config


def sync_directory(path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def prepare_bundle(root, commit):
    incoming, target = root / "incoming" / commit, root / "releases" / commit
    require(not incoming.is_symlink() and incoming.is_dir(), "incoming_release_missing")
    expected = manifest(incoming, commit)
    if target.exists():
        require(not target.is_symlink() and target.is_dir(), "invalid_existing_release")
        require(manifest(target, commit) == expected, "existing_release_differs")
        for filename in FILES | {"release-manifest.json"}:
            regular(target / filename, root_owned=True)
        return target
    # Private staging prevents a interrupted/tampered incoming bundle from
    # publishing partial files or poisoning the final commit directory.
    staging = Path(tempfile.mkdtemp(prefix=".release-", dir=root / "releases"))
    try:
        for name in FILES | {"release-manifest.json"}:
            descriptor = os.open(incoming / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as source, (staging / name).open("xb") as destination:
                metadata = os.fstat(source.fileno())
                maximum = 2*1024**3 if name == "image.tar.gz" else 1024**2
                require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
                        and metadata.st_size <= maximum, "incoming_file_changed")
                copied = 0
                while chunk := source.read(1024*1024):
                    copied += len(chunk)
                    require(copied <= maximum, "incoming_file_grew")
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            (staging / name).chmod(0o644)
        require(manifest(staging, commit) == expected, "copied_release_differs")
        staging.rename(target)
        target.chmod(0o755)
        sync_directory(target.parent)
    finally:
        # Only our fresh, flat temporary directory; never traverse a release,
        # persistent data, incoming upload or a symbolic link for cleanup.
        if staging.exists():
            for name in FILES | {"release-manifest.json"}:
                (staging / name).unlink(missing_ok=True)
            staging.rmdir()
    return target


def check_host(root):
    require(os.name == "posix" and os.geteuid() == 0, "root_owned_host_controller_required")
    require(root == ROOT and not root.is_symlink(), "unexpected_deployment_root")
    executable = regular(Path(DOCKER), root_owned=True)
    require(bool(executable.st_mode & 0o111), "trusted_docker_executable_missing")
    from preflight_host import check_host as preflight, PreflightError
    try:
        preflight()
    except PreflightError as error:
        raise ReleaseError(str(error)) from None
    for path in (root, root / "incoming", root / "releases", root / "approved-releases"):
        info = path.lstat()
        forbidden = 0o002 if path.name == "incoming" else 0o022
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & forbidden, "deployment_parent_not_protected")
    for name in ("platform-data", "upload-spool", "postgres"):
        path = root / name
        require(path.is_dir() and not path.is_symlink(), "persistent_directory_missing")
    for name in ("db_admin_password", "app_database_url"):
        path = Path("/run/sixnine-secrets") / name
        info = regular(path, root_owned=True, maximum=16384)
        require(not info.st_mode & 0o007, "runtime_secret_world_accessible")
    require(shutil.disk_usage(root).free >= 3*1024**3, "insufficient_release_disk_headroom")


def wait_ready(directory, environment, seconds=120):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        raw = compose(directory, environment, "ps", "--format", "json", "app", timeout=20)
        try:
            parsed = json.loads(raw)
            entries = parsed if isinstance(parsed, list) else [parsed]
        except ValueError:
            entries = [json.loads(line) for line in raw.splitlines() if line]
        if any(value.get("Service") == "app" and value.get("Health") == "healthy" for value in entries):
            return
        time.sleep(3)
    raise ReleaseError("application_not_ready_existing_data_preserved")


def inspect_service(directory, environment, service):
    raw = compose(directory, environment, "ps", "--all", "--quiet", service, timeout=20).decode().strip()
    require(bool(re.fullmatch(r"[0-9a-f]{12,64}", raw)), "service_container_not_unique")
    values = json.loads(command(["inspect", raw], environment=environment, timeout=20))
    require(isinstance(values, list) and len(values) == 1, "service_container_not_unique")
    value = values[0]
    labels = value.get("Config", {}).get("Labels", {})
    require(labels.get("com.docker.compose.project") == "sixnine-platform"
            and labels.get("com.docker.compose.service") == service, "service_container_identity_mismatch")
    return value


def verify_running_app(directory, environment, expected):
    value = inspect_service(directory, environment, "app")
    require(value.get("Image") == expected["image_id"]
            and value.get("State", {}).get("Running") is True
            and value.get("State", {}).get("Health", {}).get("Status") == "healthy",
            "running_app_does_not_match_release")


def wait_proxy_stable(directory, environment, *, observations=4):
    expected_id = json.loads(command(["image", "inspect", environment["SIXNINE_CADDY_IMAGE"]], environment=environment))[0]["Id"]
    previous = None
    for index in range(observations):
        current = inspect_service(directory, environment, "caddy")
        require(current.get("Image") == expected_id and current.get("State", {}).get("Running") is True
                and current.get("State", {}).get("Restarting") is not True, "proxy_not_stably_running")
        identity = (current.get("Id"), current.get("RestartCount"))
        require(previous is None or identity == previous, "proxy_restarted_during_release")
        previous = identity
        if index+1 < observations:
            time.sleep(3)


def apply_locked(root, commit):
    # Avoid filling root-owned releases with unapproved multi-gigabyte bundles.
    approved_manifest(root, root / "incoming" / commit, commit)
    directory = prepare_bundle(root, commit)
    approved_manifest(root, directory, commit)
    environment = deployment_environment(root / "site.env", commit)
    approved_configuration(directory, environment)
    state, previous = {}, None
    state_file = root / "release-state.json"
    if state_file.exists():
        regular(state_file, root_owned=True, maximum=16384)
        state = json.loads(state_file.read_text(encoding="utf-8"))
        previous = state.get("current")
        require(previous is None or isinstance(previous, str) and SHA.fullmatch(previous), "invalid_previous_release")
        pending = state.get("pending")
        require(pending in (None, commit), "another_release_requires_reconciliation")
    dependencies = pinned_dependencies(environment)
    if previous or state.get("dependencies") is not None:
        require(state.get("dependencies") == dependencies, "dependency_change_requires_separate_maintenance")
    state = {**state, "dependencies": dependencies}
    expected = manifest(directory, commit)
    if previous == commit and state.get("status") in {"app_ready", "rolled_back_app_only"}:
        # A lost SSH response must not turn previous into a self-reference.
        verify_running_app(directory, environment, expected)
        wait_proxy_stable(directory, environment)
        return
    fallback = state.get("previous") if previous == commit else previous
    require(fallback is None or isinstance(fallback, str) and SHA.fullmatch(fallback), "invalid_fallback_release")
    # 'current' remains last CONFIRMED version while pending names the attempt.
    # A process/host crash can therefore be resumed, never silently called ready.
    write_state(root, {**state, "current": previous, "pending": commit,
                      "status": "deploying", "updated_at": time.time()})
    try:
        load_approved_image(root, directory, commit, environment)
        compose(directory, environment, "up", "-d", "db")
        compose(directory, environment, "run", "--rm", "db-init")
        compose(directory, environment, "up", "-d", "--no-deps", "app")
        wait_ready(directory, environment)
        verify_running_app(directory, environment, expected)
        compose(directory, environment, "up", "-d", "--no-deps", "caddy")
        wait_proxy_stable(directory, environment)
        write_state(root, {"current": commit, "previous": fallback, "dependencies": dependencies,
                          "updated_at": time.time(), "status": "app_ready"})
    except Exception:
        if fallback and fallback != commit:
            try:
                old_directory = root / "releases" / fallback
                old_environment = deployment_environment(root / "site.env", fallback)
                old_expected = load_approved_image(root, old_directory, fallback, old_environment)
                approved_configuration(old_directory, old_environment)
                compose(old_directory, old_environment, "up", "-d", "--no-deps", "app")
                wait_ready(old_directory, old_environment)
                verify_running_app(old_directory, old_environment, old_expected)
                compose(old_directory, old_environment, "up", "-d", "--no-deps", "caddy")
                wait_proxy_stable(old_directory, old_environment)
                write_state(root, {"current": fallback, "previous": state.get("previous"), "failed_release": commit, "dependencies": dependencies,
                                  "updated_at": time.time(), "status": "rolled_back_app_only"})
            except Exception:
                write_state(root, {**state, "current": previous, "pending": commit,
                                  "updated_at": time.time(), "status": "rollback_failed_needs_reconciliation"})
                raise ReleaseError("release_and_rollback_failed_no_ready_claim") from None
        else:
            write_state(root, {**state, "current": previous, "pending": commit,
                              "updated_at": time.time(), "status": "failed_needs_reconciliation"})
        raise ReleaseError("release_failed_check_readiness_and_previous_release") from None


def pinned_dependencies(environment):
    return {name: environment[name] for name in ("SIXNINE_POSTGRES_IMAGE", "SIXNINE_CADDY_IMAGE")}


def write_state(root, state):
    temporary = root / "release-state.next"
    with temporary.open("w", encoding="utf-8") as destination:
        json.dump(state, destination, indent=2)
        destination.flush()
        os.fsync(destination.fileno())
    temporary.replace(root / "release-state.json")
    sync_directory(root)


def apply(commit):
    import fcntl
    require(bool(SHA.fullmatch(commit)), "invalid_commit")
    check_host(ROOT)
    with (ROOT / "release.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReleaseError("another_release_is_in_progress") from None
        apply_locked(ROOT, commit)


def main():
    try:
        require(len(sys.argv) == 2, "one_commit_argument_required")
        apply(sys.argv[1])
        print("Sixnine application release is healthy; DNS, public TLS and real inference require separate verification")
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, ReleaseError) else "release_failed_details_suppressed"
        print(code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
