"""Explicit preparation of an owned Ubuntu 24.04 Targon VM; inert on import.

Installs only through explicit --apply or the hash-bound operator bootstrap CLI;
inert on import/default CLI. It does not rent a VM,
download model weights, start inference, or alter a GPU driver. The caller must
already have independent cleanup armed for the supplied original deadline.
Native dependency constraints follow the approved pruned INT8 runtime profile;
the resulting full package inventory is recorded, not claimed to be a prior
image's complete dependency lock.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import tarfile
import time
import urllib.request
import uuid
import zipfile

REVISION = "0e58385fbde7ff102d276e4a9e490845de76b4ea"
PYTHON = "3.12.14"
UPSTREAM_SHA = "a9c4b97e100095e17302d27a2b8e35e5ec4b476a322ab2ed366cd30de97cc970"
REQUIREMENTS_SHA = "09ac07c3dece0260c19399907b0128d8d355c5422e7061cc757b335a80a9489e"
UV_VERSION = "0.12.24"
UV_WHEEL = "https://files.pythonhosted.org/packages/89/f8/2ef356d9d066686c98854be9e3e41d0ba9d69f7b2495512b6394115fa40c/uv-0.12.24-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
UV_SHA = "e47005957dca320272ad68960b591b854b6bf0c3288368efb096d9ccd06b8404"
CORE = {"torch":"2.7.1+cu128", "torchvision":"0.22.1+cu128", "torchaudio":"2.7.1+cu128",
    "diffusers":"0.36.0", "transformers":"4.54.0", "numpy":"2.1.2", "optimum-quanto":"0.2.7",
    "comfy-kitchen":"0.2.35", "triton":"3.3.1", "soundfile":"0.14.0"}
SOURCE = Path("/opt/workspace-internal/Wan2GP")
VENV = Path("/venv/main")
STATE = Path("/var/lib/sixnine-targon-prepare")
BOOT_ROOT = Path("/workspace/h3-studio/profile-slot-0")
PYTORCH_INDEX = "https://download.pytorch.org/whl/cu128"
SYSTEM_PACKAGES = ("ca-certificates", "git", "ffmpeg", "build-essential", "cmake", "ninja-build",
    "pkg-config", "libgl1", "libglib2.0-0", "libsndfile1", "libportaudio2", "libasound2-dev", "openssh-sftp-server")
PROFILE_ID = "h3-pruned-rank8-int8-pro6000-quanto-int8-vae-int8-sdpa-p4-lowram-v1"


@contextmanager
def _wall_timeout(seconds):
    """The standalone Linux main thread must leave time for original cleanup."""
    if seconds <= 0:
        raise ValueError("targon_prepare_deadline_reached")
    def expired(*_):
        raise TimeoutError("targon_prepare_download_timeout")
    if signal.getitimer(signal.ITIMER_REAL)[0]:
        raise ValueError("targon_prepare_timer_conflict")
    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def transformed_requirements(raw):
    if hashlib.sha256(raw).hexdigest() != UPSTREAM_SHA:
        raise ValueError("targon_prepare_upstream_requirements_mismatch")
    # Exact published Vast Wan2GP Dockerfile transformation. In this revision
    # only torchcodec's dead dependency is removed; torchdiffeq is retained.
    value = b"".join(line for line in raw.splitlines(keepends=True)
        if not re.match(rb"^(torch|torchvision|torchaudio|torchcodec)([\s<>=!])", line))
    if hashlib.sha256(value).hexdigest() != REQUIREMENTS_SHA:
        raise ValueError("targon_prepare_requirements_transform_mismatch")
    return value


def validate_profile(profile):
    runtime = profile.get("runtime", {})
    if (profile.get("source_revision") != REVISION or runtime.get("core_versions") != CORE
            or runtime.get("requirements_sha256") != REQUIREMENTS_SHA
            or profile.get("id") != PROFILE_ID):
        raise ValueError("targon_prepare_profile_mismatch")


def plan():
    return {"source_revision":REVISION, "python":PYTHON, "uv":UV_VERSION,
        "requirements_sha256":REQUIREMENTS_SHA, "core_versions":CORE, "source_directory":str(SOURCE),
        "venv_directory":str(VENV), "system_packages":list(SYSTEM_PACKAGES),
        "models_downloaded":False, "generation_started":False}


def _write(path, value):
    if path.is_symlink():
        raise ValueError("targon_prepare_symlink_forbidden")
    temporary = path.with_suffix(path.suffix + ".next")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class Preparer:
    def __init__(self, instance_id, deadline):
        if (not isinstance(instance_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", instance_id)
                or type(deadline) not in (int, float) or not time.time() < deadline <= time.time()+14400):
            raise ValueError("targon_prepare_authority_invalid")
        self.instance_id, self.deadline = instance_id, deadline
        self.env = {"PATH":"/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME":"/root",
            "LANG":"C.UTF-8", "DEBIAN_FRONTEND":"noninteractive", "PIP_CONFIG_FILE":"/dev/null",
            "PIP_DISABLE_PIP_VERSION_CHECK":"1", "UV_NO_CONFIG":"1", "UV_NO_PROGRESS":"1",
            "UV_PYTHON_INSTALL_DIR":str(STATE/"python"), "UV_CACHE_DIR":str(STATE/"cache"),
            "GIT_CONFIG_NOSYSTEM":"1", "GIT_CONFIG_GLOBAL":"/dev/null", "GIT_TERMINAL_PROMPT":"0",
            "PYTHONDONTWRITEBYTECODE":"1"}

    def run(self, command, *, cwd=None, timeout=2400):
        remaining = min(timeout, self.deadline-time.time()-30)
        if remaining <= 0:
            raise ValueError("targon_prepare_deadline_reached")
        process = subprocess.Popen([str(value) for value in command], cwd=cwd, env=self.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, _ = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise ValueError("targon_prepare_command_timeout") from None
        if process.returncode:
            # Child output is not an API response and may contain arbitrary
            # package installer messages. Keep public failures static.
            raise ValueError("targon_prepare_command_failed")
        return stdout.decode("utf-8").strip()

    def system(self):
        if platform.system() != "Linux" or platform.machine() != "x86_64" or os.geteuid() != 0:
            raise ValueError("targon_prepare_requires_owned_linux_root")
        release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
        if release.get("ID", "").strip('"') != "ubuntu" or release.get("VERSION_ID", "").strip('"') != "24.04":
            raise ValueError("targon_prepare_ubuntu_2404_required")
        # Root execution must come from the expected passwordless-sudo path;
        # no sudoers edit or broad filesystem permission relaxation is made.
        if self.run(["sudo", "-n", "-u", "ubuntu", "sudo", "-n", "id", "-u"], timeout=30) != "0":
            raise ValueError("targon_prepare_passwordless_sudo_required")
        gpus = self.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], timeout=30).splitlines()
        if len(gpus) != 1 or "RTX PRO 6000" not in gpus[0] or "Blackwell" not in gpus[0]:
            raise ValueError("targon_prepare_pro6000_required")
        for target in (STATE, SOURCE.parent, VENV.parent, Path("/workspace"), Path("/root/sixnine-cache/models")):
            if target.is_symlink() or target.resolve() != target:
                raise ValueError("targon_prepare_symlink_forbidden")
            target.mkdir(parents=True, exist_ok=True, mode=0o700 if target == STATE else 0o755)
        identity_path = STATE/"identity.json"
        identity = {"instance_id":self.instance_id, "deadline":self.deadline, "source_revision":REVISION,
                    "requirements_sha256":REQUIREMENTS_SHA}
        if identity_path.exists():
            if json.loads(identity_path.read_text()) != identity:
                raise ValueError("targon_prepare_original_identity_changed")
        else:
            _write(identity_path, identity)
        self.run(["apt-get", "update"], timeout=180)
        self.run(["apt-get", "install", "--no-install-recommends", "-y", *SYSTEM_PACKAGES], timeout=600)
        if not Path("/usr/lib/openssh/sftp-server").is_file():
            raise ValueError("targon_prepare_sftp_server_missing")
        return gpus[0]

    def installer(self):
        target = STATE/"uv"
        if not target.exists():
            with _wall_timeout(min(30, self.deadline-time.time()-30)):
                with urllib.request.urlopen(UV_WHEEL, timeout=30) as response:
                    raw = response.read(32*1024*1024+1)
            if len(raw) > 32*1024*1024 or hashlib.sha256(raw).hexdigest() != UV_SHA:
                raise ValueError("targon_prepare_installer_hash_mismatch")
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                candidates = [item for item in archive.infolist() if item.filename.endswith(".data/scripts/uv")]
                if len(candidates) != 1 or candidates[0].file_size > 64*1024*1024:
                    raise ValueError("targon_prepare_installer_layout_mismatch")
                data = archive.read(candidates[0])
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o700)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _write(STATE/"uv.json", {"version":UV_VERSION,"wheel_sha256":UV_SHA,
                                   "binary_sha256":hashlib.sha256(data).hexdigest()})
        receipt = json.loads((STATE/"uv.json").read_text())
        if (target.is_symlink() or receipt.get("wheel_sha256") != UV_SHA
                or hashlib.sha256(target.read_bytes()).hexdigest() != receipt.get("binary_sha256")
                or self.run([target, "--version"], timeout=30).split()[:2] != ["uv", UV_VERSION]):
            raise ValueError("targon_prepare_installer_identity_mismatch")
        return target

    def source(self):
        if SOURCE.is_symlink() or SOURCE.resolve() != SOURCE:
            raise ValueError("targon_prepare_symlink_forbidden")
        if not SOURCE.exists():
            SOURCE.mkdir()
            self.run(["git", "init", SOURCE], timeout=30)
            self.run(["git", "-C", SOURCE, "remote", "add", "origin", "https://github.com/deepbeepmeep/Wan2GP.git"], timeout=30)
            self.run(["git", "-C", SOURCE, "fetch", "--depth", "1", "origin", REVISION], timeout=300)
            self.run(["git", "-C", SOURCE, "checkout", "--detach", "FETCH_HEAD"], timeout=60)
        if self.run(["git", "-C", SOURCE, "rev-parse", "HEAD"], timeout=30) != REVISION:
            raise ValueError("targon_prepare_upstream_revision_mismatch")
        changed = self.run(["git", "-C", SOURCE, "diff", "HEAD", "--name-only"], timeout=30).splitlines()
        extra = self.run(["git", "-C", SOURCE, "ls-files", "--others", "--exclude-standard"], timeout=30).splitlines()
        if set(changed)-{"requirements.txt"} or any(Path(value).suffix in {".py", ".so", ".pyd"} for value in extra):
            raise ValueError("targon_prepare_upstream_modified")
        path = SOURCE/"requirements.txt"
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() == UPSTREAM_SHA:
            path.write_bytes(transformed_requirements(raw))
        elif hashlib.sha256(raw).hexdigest() != REQUIREMENTS_SHA:
            raise ValueError("targon_prepare_requirements_mismatch")

    def dependencies(self, uv):
        self.run([uv, "python", "install", PYTHON], timeout=300)
        python = VENV/"bin/python"
        if VENV.is_symlink() or VENV.resolve() != VENV:
            raise ValueError("targon_prepare_symlink_forbidden")
        if not VENV.exists():
            self.run([uv, "venv", "--python", PYTHON, VENV], timeout=60)
        if self.run([python, "-c", "import platform; print(platform.python_version())"], timeout=30) != PYTHON:
            raise ValueError("targon_prepare_python_version_mismatch")
        constraints = STATE/"constraints.txt"
        constraints.write_text("".join(name+"=="+version+"\n" for name,version in sorted(CORE.items())), encoding="utf-8")
        torch = [name+"=="+CORE[name] for name in ("torch", "torchvision", "torchaudio")]
        self.run([uv, "pip", "install", "--python", python, "--index-url", PYTORCH_INDEX, *torch], timeout=1200)
        self.run([uv, "pip", "install", "--python", python, "--index-url", "https://pypi.org/simple",
            "--extra-index-url", PYTORCH_INDEX, "--index-strategy", "unsafe-best-match",
            "--constraint", constraints, "--requirement", SOURCE/"requirements.txt",
            *(name+"=="+version for name,version in CORE.items())], cwd=SOURCE, timeout=2400)
        probe = """import importlib, importlib.metadata, json, platform, sys
sys.path.insert(0, '/opt/workspace-internal/Wan2GP')
for name in ('torch','torchvision','torchaudio','diffusers','transformers','mmgp','optimum.quanto','comfy_kitchen','triton','soundfile'):
    importlib.import_module(name)
import mmgp, torch
assert mmgp.__file__.startswith('/opt/workspace-internal/Wan2GP/')
assert torch.cuda.is_available() and torch.cuda.device_count() == 1
print(json.dumps({'python':platform.python_version(),'cuda':torch.version.cuda,
    'packages':{d.metadata['Name'].lower():d.version for d in importlib.metadata.distributions() if d.metadata['Name']}}))
"""
        observed = json.loads(self.run([python, "-c", probe], cwd=SOURCE, timeout=180))
        if observed["python"] != PYTHON or any(observed["packages"].get(name) != version for name,version in CORE.items()):
            raise ValueError("targon_prepare_installed_versions_mismatch")
        return observed

    def apply(self):
        gpu = self.system()
        uv = self.installer()
        self.source()
        observed = self.dependencies(uv)
        receipt = {**plan(), "instance_id":self.instance_id, "deadline":self.deadline,
            "prepared_at":time.time(), "gpu":gpu, "environment":observed,
            "script_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "status":"prepared"}
        _write(STATE/"prepared.json", receipt)
        return {"status":"prepared", "instance_id":self.instance_id, "receipt_path":str(STATE/"prepared.json")}


def _checked(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError("targon_prepare_path_invalid")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("targon_prepare_symlink_forbidden")
    return path


def _document(path):
    path = _checked(path)
    if not path.is_file() or path.stat().st_size > 512*1024:
        raise ValueError("targon_prepare_document_invalid")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("targon_prepare_document_invalid")
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def bootstrap_context(config_path, slot_key, token_file):
    """Validate the CPU-authorized source identity without reading its token."""
    root = BOOT_ROOT
    config_path, token_file = _checked(config_path), _checked(token_file)
    if config_path != root/"wangp-runtime.json" or token_file != root/"wangp-token":
        raise ValueError("targon_prepare_boot_paths_invalid")
    if str(uuid.UUID(slot_key)) != slot_key:
        raise ValueError("targon_prepare_slot_invalid")
    config = _document(config_path)
    identity = _document(root/"sixnine-bootstrap-identity.json")
    if (identity.get("intent_id") != slot_key or identity.get("provider") != "targon"
            or identity.get("deployment_profile_id") != PROFILE_ID or identity.get("runtime_python") != str(VENV/"bin/python")
            or config.get("deployment_profile_id") != PROFILE_ID or config.get("profile_slot_index") != 0
            or config.get("expected_host_gpus") != 1 or config.get("prepared_root") != str(SOURCE)
            or config.get("install_root") != "/root/sixnine-cache/operator/profile-slot-0"
            or config.get("model_root") != "/root/sixnine-cache/models"
            or config.get("status_path") != str(root/"setup-status.json")
            or config.get("manifest_path") != str(root/"wangp-manifest.json")
            or config.get("source_bundle_path") != str(root/"wangp-package.tar.gz")
            or config.get("dependency_artifact_url") or config.get("dependency_artifact_path")):
        raise ValueError("targon_prepare_boot_identity_mismatch")
    sources = identity.get("sources")
    if not isinstance(sources, dict):
        raise ValueError("targon_prepare_source_identity_missing")
    for name in ("wangp-runtime.json", "wangp-manifest.json", "wangp-bootstrap.py", "wangp-package.tar.gz"):
        path = _checked(root/name)
        maximum = 16*1024*1024 if name.endswith('.gz') else 512*1024
        if (not path.is_file() or path.stat().st_size > maximum
                or hashlib.sha256(path.read_bytes()).hexdigest() != sources.get(name)):
            raise ValueError("targon_prepare_source_identity_mismatch")
    manifest = _document(root/"wangp-manifest.json")
    manifest_digest = _digest(manifest)
    if (manifest_digest != identity.get("engine_manifest_digest") or manifest.get("source_revision") != REVISION
            or manifest.get("deployment_profile_id") != PROFILE_ID or manifest.get("synthetic") is not False):
        raise ValueError("targon_prepare_manifest_identity_mismatch")
    member_name = "deploy/wangp/profiles/"+PROFILE_ID+".json"
    with tarfile.open(root/"wangp-package.tar.gz", 'r:gz') as archive:
        members = [item for item in archive if item.name == member_name]
        if len(members) != 1 or not members[0].isfile() or members[0].size > 512*1024:
            raise ValueError("targon_prepare_profile_bundle_invalid")
        with archive.extractfile(members[0]) as stream:
            profile = json.load(stream)
    validate_profile(profile)
    if manifest.get("runtime_profile") != profile["runtime"]:
        raise ValueError("targon_prepare_manifest_profile_mismatch")
    preparer = Preparer(identity.get("instance_id"), identity.get("hard_deadline"))
    return root, manifest_digest, preparer


def prepare_then_bootstrap(config_path, slot_key, token_file):
    root, manifest_digest, preparer = bootstrap_context(config_path, slot_key, token_file)
    status_path = root/"setup-status.json"
    status = {"state":"booting", "phase":"runtime_imports", "manifest_digest":manifest_digest,
        "engine_manifest_digest":manifest_digest, "slot_key":slot_key, "inference_verified":False,
        "updated_unix":time.time()}
    _write(status_path, status)
    try:
        preparer.apply()
    except Exception:
        _write(status_path, {**status, "state":"failed", "phase":"setup_failed", "failed_phase":"runtime_imports",
            "failure_phase":"runtime_imports", "error_code":"targon_prepare_failed", "updated_unix":time.time()})
        raise ValueError("targon_prepare_failed") from None
    os.execv(str(VENV/"bin/python"), [str(VENV/"bin/python"), "-u", str(root/"wangp-bootstrap.py"),
        "--config", str(config_path), "--slot-key", slot_key, "--token-file", str(token_file)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--instance-id")
    parser.add_argument("--deadline", type=float)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--slot-key")
    parser.add_argument("--token-file", type=Path)
    args = parser.parse_args()
    if args.config is not None:
        if args.slot_key is None or args.token_file is None or args.apply:
            parser.error("--config requires --slot-key and --token-file, without --apply")
        prepare_then_bootstrap(args.config, args.slot_key, args.token_file)
        return
    if not args.apply:
        print(json.dumps(plan(), sort_keys=True))
        return
    if args.profile is None or args.instance_id is None or args.deadline is None:
        parser.error("--apply requires --profile, --instance-id and original --deadline")
    validate_profile(json.loads(args.profile.read_text()))
    print(json.dumps(Preparer(args.instance_id,args.deadline).apply(), sort_keys=True))


if __name__ == "__main__":
    main()
