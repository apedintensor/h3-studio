"""Real local key parsing and fake SSH handshakes; no network/provider calls."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import paramiko

from studio_platform import instance_host_keys as subject
from studio_platform.lium_bootstrap import BootError, SSHHost

INTENT = '11111111-1111-4111-8111-111111111111'
NEXT = '33333333-3333-4333-8333-333333333333'
INSTANCE = '22222222-2222-4222-8222-222222222222'
OTHER = '44444444-4444-4444-8444-444444444444'


class Client:
    """Exercise actual HostKeys file parsing and MissingHostKeyPolicy calls."""
    def __init__(self, key, *, authenticate=True, entered=None, release=None):
        self.key, self.authenticate = key, authenticate
        self.entered, self.release = entered, release
        self.keys = paramiko.HostKeys()
        self.connected = self.closed = False
        self.calls = []

    def load_host_keys(self, path): self.keys.load(path)
    def get_host_keys(self): return self.keys
    def set_missing_host_key_policy(self, policy): self.policy = policy
    def get_transport(self):
        return NS(is_active=lambda: self.connected and not self.closed,
            is_authenticated=lambda: self.connected and not self.closed,
            get_remote_server_key=lambda: self.key)
    def close(self): self.closed = True

    def connect(self, host, **kwargs):
        self.calls.append((host, kwargs))
        if self.entered:
            self.entered.set()
            if not self.release.wait(3):
                raise AssertionError('local test handoff timed out')
        hostname = host if kwargs['port'] == 22 else '['+host+']:'+str(kwargs['port'])
        known = self.keys.lookup(hostname)
        if known is not None:
            expected = known.get(self.key.get_name())
            if expected is None or expected != self.key:
                raise paramiko.BadHostKeyException(hostname, self.key, next(iter(known.values())))
        else:
            self.policy.missing_host_key(self, hostname, self.key)
        if not self.authenticate:
            raise paramiko.AuthenticationException('synthetic private diagnostic')
        self.connected = True


class InstanceHostKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.first = paramiko.RSAKey.generate(1024)
        cls.second = paramiko.RSAKey.generate(1024)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root/'work'
        self.work.mkdir()
        self.legacy = self.root/'historical-known-hosts'
        self.legacy.write_text('[reused.provider.example]:2345 '+self.first.get_name()+' '+self.first.get_base64()+'\n')
        self.legacy.chmod(0o600)
        self.legacy_bytes = self.legacy.read_bytes()
        self.coordinates = {'host': 'reused.provider.example', 'port': 2345}

    def config(self, identity=('lium', INTENT, INSTANCE), *, trust=True):
        hosts, scoped = subject.known_hosts_for(self.work, identity, self.legacy)
        return NS(provider=identity[0], known_hosts_file=hosts, host_key_identity=scoped,
            ssh_key_file=self.root/'never-opened-test-key', trust_first_host_key=trust)

    def connect(self, config, key=None, *, coordinates=None, **options):
        client = Client(key or self.first, **options)
        coords = {**(coordinates or self.coordinates), 'username': 'ubuntu' if config.provider == 'targon' else 'root'}
        with patch('paramiko.SSHClient', return_value=client):
            host = SSHHost(config, coords)
        self.addCleanup(host.close)
        return host, client

    def record_path(self, config): return config.known_hosts_file.parent.parent/'ssh-host-identity.json'
    def record(self, config): return json.loads(self.record_path(config).read_text())

    def test_new_rentals_do_not_inherit_historical_keys_for_reused_endpoint(self):
        lium, targon = self.config(), self.config(('targon', NEXT, 'wrk-test_new'))
        self.connect(lium, self.second)
        self.connect(targon, self.first)
        self.assertNotEqual(lium.known_hosts_file, targon.known_hosts_file)
        self.assertEqual(self.record(lium)['identity']['instance_id'], INSTANCE)
        self.assertEqual(self.record(targon)['identity']['provider'], 'targon')
        self.assertEqual(self.legacy.read_bytes(), self.legacy_bytes)
        self.assertEqual(self.record(lium)['pin']['public_key'], self.second.get_base64())

    def test_reconstructed_host_rejects_replaced_key_for_same_instance(self):
        config = self.config()
        self.connect(config)
        before = (self.record_path(config).read_bytes(), config.known_hosts_file.read_bytes())
        with self.assertRaisesRegex(BootError, 'host_key_untrusted'):
            self.connect(self.config(), self.second)
        self.assertEqual(before, (self.record_path(config).read_bytes(), config.known_hosts_file.read_bytes()))

    def test_lost_or_changed_pin_file_is_not_retrusted_after_restart(self):
        config = self.config()
        self.connect(config)
        config.known_hosts_file.write_text('different public bytes\n')
        with self.assertRaisesRegex(BootError, 'pin_changed'):
            self.connect(self.config())
        config.known_hosts_file.unlink()
        with self.assertRaisesRegex(BootError, 'pin_missing'):
            self.connect(self.config(), self.second)
        self.assertEqual(self.record(config)['pin']['public_key'], self.first.get_base64())

    def test_alias_and_new_port_require_same_pin_without_rewriting_file(self):
        config = self.config()
        self.connect(config)
        before = config.known_hosts_file.read_bytes()
        alias = {'host': 'other-address.example', 'port': 3456}
        self.connect(self.config(), coordinates=alias)
        with self.assertRaisesRegex(BootError, 'host_key_changed'):
            self.connect(self.config(), self.second, coordinates=alias)
        self.assertEqual(config.known_hosts_file.read_bytes(), before)

    def test_authentication_failure_still_consumes_first_host_key_trust(self):
        config = self.config()
        with self.assertRaisesRegex(BootError, 'host_key_untrusted'):
            self.connect(config, authenticate=False)
        self.assertEqual(self.record(config)['pin']['public_key'], self.first.get_base64())
        self.connect(self.config())
        with self.assertRaises(BootError):
            self.connect(self.config(), self.second)

    def test_unapproved_first_trust_does_not_create_pin(self):
        config = self.config(trust=False)
        with self.assertRaisesRegex(BootError, 'initial_trust_not_authorized'):
            self.connect(config)
        self.assertIsNone(self.record(config)['pin'])
        self.assertFalse(config.known_hosts_file.exists())

    def test_pin_receipt_commits_before_known_hosts_write_and_partial_commit_rejects(self):
        config = self.config()
        original = subject._write_once
        def fail_hosts(path, data):
            if Path(path) == config.known_hosts_file:
                self.assertIsNotNone(self.record(config)['pin'])
                raise OSError('synthetic write interruption')
            return original(path, data)
        with patch.object(subject, '_write_once', side_effect=fail_hosts), self.assertRaises(BootError):
            self.connect(config)
        with self.assertRaisesRegex(BootError, 'pin_missing'):
            self.connect(self.config(), self.second)

    def test_identity_conflict_and_partial_migration_do_not_fall_back_to_global(self):
        config = self.config()
        with self.assertRaisesRegex(subject.HostKeyError, 'identity_changed'):
            self.config(('lium', INTENT, OTHER))
        self.record_path(config).with_suffix('.next').write_text('unconfirmed migration')
        with self.assertRaisesRegex(subject.HostKeyError, 'migration_unconfirmed'):
            self.config()
        self.record_path(config).with_suffix('.next').unlink()
        self.record_path(config).unlink()
        with self.assertRaisesRegex(subject.HostKeyError, 'migration_unconfirmed'):
            self.config()
        self.assertEqual(self.legacy.read_bytes(), self.legacy_bytes)

    def test_old_slot_directory_keeps_global_path_and_no_scoped_identity(self):
        slot = self.work/'boot'/INTENT/'0'/INTENT
        slot.mkdir(parents=True)
        (slot/'bootstrap-state.json').write_text(json.dumps({'identity': {
            'intent_id': INTENT, 'instance_id': INSTANCE}}))
        config = self.config()
        self.assertEqual(config.known_hosts_file, self.legacy)
        self.assertEqual(config.host_key_identity, ())
        self.connect(config)
        self.assertEqual(self.config().known_hosts_file, self.legacy)
        self.assertEqual(self.legacy.read_bytes(), self.legacy_bytes)

    def test_missing_new_identity_cannot_use_legacy_bootstrap_migration(self):
        slot = self.work/'boot'/INTENT/'0'/INTENT
        slot.mkdir(parents=True)
        (slot/'bootstrap-state.json').write_text(json.dumps({'identity': {
            'ssh_host_key_identity': ['lium', INTENT, INSTANCE]}}))
        with self.assertRaisesRegex(subject.HostKeyError, 'migration_unconfirmed'):
            self.config()

    def test_entire_boot_subtree_loss_does_not_allow_new_trust_or_legacy_downgrade(self):
        config = self.config()
        self.connect(config)
        anchor = self.work/'ssh-host-selections'/(INTENT+'.json')
        original = anchor.read_bytes()
        directory = config.known_hosts_file.parent.parent.resolve()
        self.assertTrue(directory.is_relative_to(self.work.resolve()))
        self.assertEqual(directory.name, INTENT)
        shutil.rmtree(directory)
        client = Client(self.second)
        with patch('paramiko.SSHClient', return_value=client):
            with self.assertRaisesRegex(subject.HostKeyError, 'migration_unconfirmed'):
                self.config()
        self.assertEqual(client.calls, [])
        # Recreating an apparently historical slot must not erase the anchor.
        (directory/'0'/INTENT).mkdir(parents=True)
        with self.assertRaisesRegex(subject.HostKeyError, 'migration_unconfirmed'):
            self.config()
        self.assertEqual(anchor.read_bytes(), original)
        self.assertEqual(self.legacy.read_bytes(), self.legacy_bytes)

    def test_partial_control_anchor_commit_requires_review_without_connecting(self):
        receipt = self.work/'boot'/INTENT/'ssh-host-identity.json'
        original = subject._write_once
        def fail_local_receipt(path, raw):
            if Path(path) == receipt:
                raise OSError('synthetic local receipt interruption')
            return original(path, raw)
        with patch.object(subject, '_write_once', side_effect=fail_local_receipt), self.assertRaises(OSError):
            self.config()
        self.assertTrue((self.work/'ssh-host-selections'/(INTENT+'.json')).is_file())
        with self.assertRaisesRegex(subject.HostKeyError, 'migration_unconfirmed'):
            self.config()

    def test_old_bootstrap_with_different_instance_cannot_authorize_migration(self):
        slot = self.work/'boot'/INTENT/'0'/INTENT
        slot.mkdir(parents=True)
        (slot/'bootstrap-state.json').write_text(json.dumps({'identity': {
            'intent_id': INTENT, 'instance_id': OTHER}}))
        with self.assertRaisesRegex(subject.HostKeyError, 'migration_unconfirmed'):
            self.config()

    def test_concurrent_slots_pin_once_and_retry_uses_original_key(self):
        config = self.config()
        entered, release = threading.Event(), threading.Event()
        client = Client(self.first, entered=entered, release=release)
        failures = []
        def first_slot():
            try:
                subject.connect_pinned(client, config, self.coordinates, {'port': 2345})
            except Exception as error:
                failures.append(error)
        thread = threading.Thread(target=first_slot)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            second = Client(self.first)
            with self.assertRaisesRegex(subject.HostKeyError, 'pin_busy'):
                subject.connect_pinned(second, config, self.coordinates, {'port': 2345})
            self.assertEqual(second.calls, [])
        finally:
            release.set(); thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        before = self.record_path(config).read_bytes()
        self.connect(self.config())
        self.assertEqual(self.record_path(config).read_bytes(), before)
        self.assertEqual(len(paramiko.HostKeys(str(config.known_hosts_file))), 1)

    def test_linked_or_shared_pin_file_is_rejected(self):
        config = self.config()
        self.connect(config)
        alias = self.root/'hard-link'
        try:
            os.link(config.known_hosts_file, alias)
        except OSError:
            self.skipTest('hard links not available on this local test filesystem')
        with self.assertRaisesRegex((BootError, subject.HostKeyError), 'pin_invalid'):
            self.connect(self.config())
        alias.unlink()
        before = config.known_hosts_file.read_bytes()
        config.known_hosts_file.unlink()
        alias.write_bytes(before)
        try:
            config.known_hosts_file.symlink_to(alias)
        except OSError:
            return  # Windows without symlink privileges still checked the hard link.
        with self.assertRaisesRegex((BootError, subject.HostKeyError), 'pin_invalid'):
            self.connect(self.config())


if __name__ == '__main__':
    unittest.main()
