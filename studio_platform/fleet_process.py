"""Local lifetime ownership for a protocol-bound CPU fleet child.

The lock covers registration, the entire runner and cleanup. A durable token
fences a Popen child delayed before acquiring that lock. No saved PID is used
for signalling, and absent legacy evidence never authorizes reconstruction.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import uuid


PROTOCOL = "fleet-process-v1"
TOKEN = re.compile(r"[0-9a-f]{32}\Z")


def _boot_identity():
    # Production is Linux. Kernel boot identity refuses copied control state or
    # a host reboot; Windows supports only the local CPU regression fixtures.
    if os.name == "nt":
        return "windows-test-" + os.environ.get("COMPUTERNAME", "local")
    value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return str(uuid.UUID(value))


def _directory(config, worker_id):
    config.slot(worker_id)
    directory = config.work_dir / worker_id
    for path in (directory, *directory.parents):
        if path.is_symlink():
            raise ValueError("fleet_process_path_untrusted")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _check_file(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or os.name != "nt" and (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
        raise ValueError("fleet_process_file_untrusted")


def _lock_identity(config, worker_id):
    info = (_directory(config, worker_id)/"process-owner.lock").stat(follow_symlinks=False)
    _check_file(info)
    return {"device": info.st_dev, "inode": info.st_ino}


@contextmanager
def _ownership(config, worker_id):
    path = _directory(config, worker_id) / "process-owner.lock"
    if path.is_symlink():
        raise ValueError("fleet_process_path_untrusted")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        _check_file(os.fstat(fd))
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        # A replaced inode cannot attest ownership of the pathname being used
        # by the other cooperating parent/child process.
        if not os.path.samestat(os.fstat(fd), path.stat(follow_symlinks=False)):
            raise ValueError("fleet_process_lock_replaced")
        yield acquired
    finally:
        if acquired:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(config, worker_id):
    path = _directory(config, worker_id) / "process-owner.json"
    if path.is_symlink():
        raise ValueError("fleet_process_path_untrusted")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as source:
        _check_file(os.fstat(source.fileno()))
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError("fleet_process_owner_untrusted")
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {"protocol", "fleet_hash", "worker_id", "token", "state", "boot_id", "lock_identity"}
            or value["protocol"] != PROTOCOL or value["fleet_hash"] != config.fingerprint()
            or value["boot_id"] != _boot_identity()
            or value["lock_identity"] != _lock_identity(config, worker_id)
            or value["worker_id"] != worker_id or not isinstance(value["token"], str)
            or not TOKEN.fullmatch(value["token"]) or value["state"] not in {"launching", "running", "exited"}):
        raise ValueError("fleet_process_owner_identity_conflict")
    return value


def _save(config, worker_id, value):
    path = _directory(config, worker_id) / "process-owner.json"
    temporary = path.with_name("process-owner-" + uuid.uuid4().hex + ".tmp")
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as target:
            json.dump(value, target, sort_keys=True)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


class ObservedProcess:
    """Observe a compatible running owner, never signal an advisory PID."""
    pid = None

    def __init__(self, config, worker_id, token):
        self.config, self.worker_id, self.token = config, worker_id, token

    def poll(self):
        with _ownership(self.config, self.worker_id) as acquired:
            value = _read(self.config, self.worker_id)
            if value["token"] != self.token or value["state"] == "launching":
                raise ValueError("fleet_process_observation_superseded")
            # Kernel ownership is stopped; the original OS exit code is not
            # available to a non-parent, so do not invent successful exit zero.
            return -1 if acquired else None

    def send_signal(self, _signal):
        return None  # The supervisor's exact per-worker drain flag is enough.

    def stopped_proof(self):
        with _ownership(self.config, self.worker_id) as acquired:
            value = _read(self.config, self.worker_id)
            if not acquired or value["token"] != self.token or value["state"] == "launching":
                return None
            return {"worker_id": self.worker_id, "protocol": PROTOCOL, "token": self.token,
                    "fleet_hash": self.config.fingerprint(), "boot_id": value["boot_id"],
                    "lock_identity": value["lock_identity"], "cpu_owner_stopped": True}


def prepare_launch(config, worker_id, *, recovering=False):
    """Return a fenced launch token, or an observed current compatible owner."""
    with _ownership(config, worker_id) as acquired:
        path = _directory(config, worker_id) / "process-owner.json"
        if not acquired:
            value = _read(config, worker_id)
            if not recovering or value["state"] != "running":
                raise ValueError("fleet_process_already_owned")
            return None, ObservedProcess(config, worker_id, value["token"])
        if recovering:
            _read(config, worker_id)  # Missing/legacy/replaced evidence is a hold.
        elif path.exists() or path.is_symlink():
            raise ValueError("fleet_process_recovery_required")
        token = uuid.uuid4().hex
        _save(config, worker_id, {"protocol": PROTOCOL, "fleet_hash": config.fingerprint(),
            "worker_id": worker_id, "token": token, "state": "launching", "boot_id": _boot_identity(),
            "lock_identity": _lock_identity(config, worker_id)})
        return token, None


@contextmanager
def owned_process(config, worker_id, token):
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ValueError("fleet_process_token_required")
    with _ownership(config, worker_id) as acquired:
        if not acquired:
            raise ValueError("fleet_process_already_owned")
        value = _read(config, worker_id)
        if value["token"] != token or value["state"] != "launching":
            raise ValueError("fleet_process_launch_superseded")
        value["state"] = "running"
        _save(config, worker_id, value)
        try:
            yield
        finally:
            value["state"] = "exited"
            _save(config, worker_id, value)
