"""Rental-bound SSH pins, with explicit first trust and no provider operations.

Historical allocations retain their original global file. New allocations share
one immutable host key across slots and endpoint aliases. The identity receipt
consumes first trust before authentication: lost bytes require review, not TOFU.
"""
from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid


class HostKeyError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code='bootstrap_ssh_pin_invalid'):
    if not condition:
        raise HostKeyError(code)


def validate_identity(identity):
    require(type(identity) is tuple and len(identity) == 3)
    provider, intent_id, instance_id = identity
    try:
        require(provider in {'lium', 'targon'} and str(uuid.UUID(intent_id)) == intent_id)
        if provider == 'lium':
            require(str(uuid.UUID(instance_id)) == instance_id)
        else:
            require(isinstance(instance_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', instance_id))
    except (ValueError, TypeError, AttributeError):
        raise HostKeyError('bootstrap_ssh_pin_identity_invalid') from None
    return {'provider': provider, 'intent_id': intent_id, 'instance_id': instance_id}


def _path(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts)
    for part in (path, *path.parents):
        require(not part.is_symlink() and not (hasattr(part, 'is_junction') and part.is_junction()))
        if part.exists():
            require(not getattr(part.lstat(), 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))
    return path


def _private(info, *, protected=True):
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    if os.name != 'nt':
        require(info.st_uid in (0, os.geteuid())
            and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH | (stat.S_IRWXO if protected else 0)))


def _directory(path):
    path = _path(path)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode))
    if os.name != 'nt':
        require(info.st_uid in (0, os.geteuid()) and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


def _read(path, *, protected=True):
    path = _path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as source:
            info = os.fstat(source.fileno())
            _private(info, protected=protected)
            require(0 < info.st_size <= 16384)
            raw = source.read(16385)
            require(len(raw) <= 16384)
            return raw
    except OSError:
        raise HostKeyError('bootstrap_ssh_pin_file_unavailable') from None


def _sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_once(path, raw):
    path = _path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'wb') as target:
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())
    _sync_directory(path.parent)


@contextmanager
def _lock(directory):
    directory = _path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _directory(directory)
    path = _path(directory/'ssh-host-identity.lock')
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'r+b') as handle:
        _private(os.fstat(handle.fileno()))
        acquired = False
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b'0'); handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    pass
            require(acquired, 'bootstrap_ssh_pin_busy')
            yield
        finally:
            if acquired:
                if os.name == 'nt':
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def _record(path, identity):
    pending = path.with_suffix('.next')
    require(not pending.exists() and not pending.is_symlink(), 'bootstrap_ssh_pin_migration_unconfirmed')
    try:
        value = json.loads(_read(path))
    except (ValueError, UnicodeError):
        raise HostKeyError('bootstrap_ssh_pin_invalid') from None
    require(isinstance(value, dict) and value.get('version') == 1 and type(value.get('version')) is int
        and value.get('identity') == identity, 'bootstrap_ssh_pin_identity_changed')
    if value.get('mode') == 'legacy':
        require(set(value) == {'version', 'identity', 'mode', 'known_hosts_file'})
        _path(value['known_hosts_file'])
    else:
        require(value.get('mode') == 'instance' and set(value) == {'version', 'identity', 'mode', 'pin'})
        pin = value['pin']
        if pin is not None:
            require(isinstance(pin, dict) and set(pin) == {'hostname', 'key_type', 'public_key', 'file_sha256'}
                and isinstance(pin['hostname'], str) and re.fullmatch(r'[A-Za-z0-9_.:%\[\]-]{1,300}', pin['hostname'])
                and isinstance(pin['key_type'], str) and re.fullmatch(r'[A-Za-z0-9@._+-]{1,80}', pin['key_type'])
                and isinstance(pin['public_key'], str) and len(pin['public_key']) <= 8192
                and isinstance(pin['file_sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', pin['file_sha256']))
            try:
                require(bool(base64.b64decode(pin['public_key'], validate=True)))
            except ValueError:
                raise HostKeyError('bootstrap_ssh_pin_invalid') from None
    return value


def _selection(path, identity):
    try:
        value = json.loads(_read(path))
    except (ValueError, UnicodeError):
        raise HostKeyError('bootstrap_ssh_pin_migration_unconfirmed') from None
    require(isinstance(value, dict) and set(value) == {'version', 'identity', 'mode', 'known_hosts_file'}
        and type(value.get('version')) is int and value['version'] == 1
        and value['identity'] == identity and value['mode'] in {'instance', 'legacy'},
        'bootstrap_ssh_pin_identity_changed')
    _path(value['known_hosts_file'])
    return value


def known_hosts_for(work_dir, identity, legacy_file):
    """Choose once; a control-level anchor survives loss of the boot subtree."""
    expected = validate_identity(identity)
    root, legacy = _path(work_dir), _path(legacy_file)
    directory = _path(root/'boot'/expected['intent_id'])
    selections = _path(root/'ssh-host-selections')
    # This order never reverses: selection lock -> boot pin lock. SSH only takes
    # the latter and reads the immutable selection; it does not create anchors.
    with _lock(selections):
        anchor = selections/(expected['intent_id']+'.json')
        receipt, scoped = directory/'ssh-host-identity.json', directory/'ssh'/'known_hosts'
        selected = _selection(anchor, expected) if anchor.exists() else None
        if selected is not None:
            require(directory.is_dir() and receipt.is_file(), 'bootstrap_ssh_pin_migration_unconfirmed')
        else:
            require(not receipt.exists(), 'bootstrap_ssh_pin_migration_unconfirmed')
        existed = directory.exists()
        with _lock(directory):
            if receipt.exists():
                value = _record(receipt, expected)
            else:
                entries = {item.name for item in directory.iterdir()} - {'ssh-host-identity.lock'}
                # A failed/partial scoped selection is not permission to downgrade.
                require('ssh' not in entries and 'ssh-host-identity.next' not in entries,
                    'bootstrap_ssh_pin_migration_unconfirmed')
                slots = [item for item in directory.iterdir() if item.name.isdecimal() and item.is_dir()]
                require(not existed or bool(slots), 'bootstrap_ssh_pin_migration_unconfirmed')
                require(not entries or entries == {item.name for item in slots},
                    'bootstrap_ssh_pin_migration_unconfirmed')
                for slot in slots:
                    old = slot/expected['intent_id']/'bootstrap-state.json'
                    if old.exists():
                        try:
                            previous = json.loads(_read(old, protected=False))
                        except (ValueError, UnicodeError):
                            raise HostKeyError('bootstrap_ssh_pin_migration_unconfirmed') from None
                        require(isinstance(previous, dict) and isinstance(previous.get('identity'), dict)
                            and previous['identity'].get('intent_id') == expected['intent_id']
                            and previous['identity'].get('instance_id') == expected['instance_id']
                            and previous['identity'].get('provider', 'lium') == expected['provider']
                            and 'ssh_host_key_identity' not in previous['identity'],
                            'bootstrap_ssh_pin_migration_unconfirmed')
                value = {'version': 1, 'identity': expected, 'mode': 'legacy' if slots else 'instance'}
                if slots:
                    value['known_hosts_file'] = str(legacy)
                else:
                    value['pin'] = None
                selected = {'version': 1, 'identity': expected, 'mode': value['mode'],
                    'known_hosts_file': str(legacy if slots else scoped)}
                # Record selection outside disposable boot files first. Any
                # incomplete commit is a review obligation, not fresh trust.
                _write_once(anchor, json.dumps(selected, sort_keys=True).encode())
                _write_once(receipt, json.dumps(value, sort_keys=True).encode())
            path = legacy if value['mode'] == 'legacy' else scoped
            require(selected == {'version': 1, 'identity': expected, 'mode': value['mode'],
                'known_hosts_file': str(path)}, 'bootstrap_ssh_pin_identity_changed')
            if value['mode'] == 'legacy':
                require(value['known_hosts_file'] == str(legacy), 'bootstrap_ssh_pin_legacy_path_changed')
                return legacy, ()
            scoped.parent.mkdir(mode=0o700, exist_ok=True)
            _directory(scoped.parent)
            return scoped, identity


def connect_pinned(client, config, coordinates, kwargs):
    """One protected instance pin; aliases must present the same original key."""
    import paramiko
    identity = validate_identity(config.host_key_identity)
    hosts = _path(config.known_hosts_file)
    directory = hosts.parent.parent
    require(hosts.name == 'known_hosts' and hosts.parent.name == 'ssh'
        and directory.name == identity['intent_id'])
    _directory(hosts.parent)
    receipt = directory/'ssh-host-identity.json'
    with _lock(directory):
        anchor = directory.parent.parent/'ssh-host-selections'/(identity['intent_id']+'.json')
        require(_selection(anchor, identity) == {'version': 1, 'identity': identity, 'mode': 'instance',
            'known_hosts_file': str(hosts)}, 'bootstrap_ssh_pin_identity_changed')
        record = _record(receipt, identity)
        require(record['mode'] == 'instance')

        def check_file(value):
            if value['pin'] is None:
                require(not hosts.exists(), 'bootstrap_ssh_pin_migration_unconfirmed')
            else:
                require(hosts.exists(), 'bootstrap_ssh_pin_missing')
                require(hashlib.sha256(_read(hosts)).hexdigest() == value['pin']['file_sha256'],
                    'bootstrap_ssh_pin_changed')

        check_file(record)
        if record['pin'] is not None:
            client.load_host_keys(str(hosts))

        class InstancePolicy(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, ssh_client, hostname, key):
                nonlocal record
                if record['pin'] is None:
                    require(config.trust_first_host_key, 'bootstrap_ssh_initial_trust_not_authorized')
                    require(isinstance(hostname, str) and re.fullmatch(r'[A-Za-z0-9_.:%\[\]-]{1,300}', hostname))
                    raw = (hostname+' '+key.get_name()+' '+key.get_base64()+'\n').encode('ascii')
                    pin = {'hostname': hostname, 'key_type': key.get_name(), 'public_key': key.get_base64(),
                        'file_sha256': hashlib.sha256(raw).hexdigest()}
                    bound = {**record, 'pin': pin}
                    # Consume trust before any authentication or file write can
                    # fail. A partial commit requires explicit review on restart.
                    next_path = receipt.with_suffix('.next')
                    _write_once(next_path, json.dumps(bound, sort_keys=True).encode())
                    os.replace(next_path, receipt)
                    _sync_directory(directory)
                    record = bound
                    _write_once(hosts, raw)
                require((key.get_name(), key.get_base64()) ==
                    (record['pin']['key_type'], record['pin']['public_key']), 'bootstrap_ssh_host_key_changed')
                # Keep aliases in this connection only; do not rewrite the pin.
                ssh_client.get_host_keys().add(hostname, key.get_name(), key)

        client.set_missing_host_key_policy(InstancePolicy())
        client.connect(coordinates['host'], **kwargs)
        key = client.get_transport().get_remote_server_key()
        require(record['pin'] is not None and (key.get_name(), key.get_base64()) ==
            (record['pin']['key_type'], record['pin']['public_key']), 'bootstrap_ssh_host_key_changed')
        require(_record(receipt, identity) == record, 'bootstrap_ssh_pin_changed')
        check_file(record)
