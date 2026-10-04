"""Offline finite scaler deployment policy; no AWS/provider/service operations."""
from contextlib import ExitStack
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_platform_gpu_acceptance import load, DEPLOY, validator, release, acceptance

with patch.dict(sys.modules, {'release': release, 'check_config': validator, 'gpu_acceptance': acceptance}):
    scaler = load('test_finite_deploy', 'gpu_scaler.py')


def configuration():
    return {'cycle_id': 'synthetic-cycle', 'hard_deadline': 2000000000,
        'secret_arn': 'arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:/sixnine/platform/lium-ABCDEF',
        'secret_version_id': 'synthetic-version-'+'a'*32}


def good_status(config):
    return {'cycle_id': config['cycle_id'], 'config_hash': scaler.fingerprint(config),
        'hard_deadline': config['hard_deadline'], 'observed_at': time.time(),
        'snapshot_only': False, 'controller_exit_required': True, 'ledger_safe': True,
        'all_destroyed': True, 'drained': True, 'active_job_ids': [], 'active_jobs_truncated': False, 'billing_pending': 1,
        'instances': [{'id': 'synthetic-intent', 'state': 'destroyed'}]}


def container(config, *, running=False, **state):
    return {'Name': '/'+scaler.container_name(config), 'Config': {'Labels': {
        'com.docker.compose.project': 'sixnine-platform', 'com.docker.compose.service': scaler.SERVICE,
        'com.sixnine.finite.config-hash': scaler.fingerprint(config)}},
        'State': {'Running': running, 'Restarting': False, 'ExitCode': 0,
            'Status': 'running' if running else 'exited', 'OOMKilled': False, **state}}


class FiniteComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('docker'):
            raise unittest.SkipTest('Docker Compose CLI unavailable; no daemon required')
        with tempfile.TemporaryDirectory() as temporary:
            overlay = Path(temporary)/'overlay.json'
            overlay.write_text(json.dumps(scaler.overlay('sixnine-platform:'+'a'*40)))
            empty = Path(temporary)/'empty.env'
            empty.write_text('')
            env = dict(os.environ, SIXNINE_IMAGE='sixnine-platform:'+'a'*40,
                SIXNINE_POSTGRES_IMAGE='postgres@sha256:'+'b'*64, SIXNINE_CADDY_IMAGE='caddy@sha256:'+'c'*64,
                SIXNINE_DB_ADMIN_SECRET_FILE='/run/sixnine-secrets/db_admin_password',
                SIXNINE_APP_DSN_SECRET_FILE='/run/sixnine-secrets/app_database_url')
            raw = subprocess.run(['docker', 'compose', '--env-file', str(empty), '-f', str(DEPLOY/'compose.yaml'),
                '-f', str(overlay), 'config', '--format', 'json'], env=env, capture_output=True, timeout=30)
            if raw.returncode:
                raise AssertionError('Synthetic Compose rendering failed; output withheld')
            cls.config = json.loads(raw.stdout)
            cls.version = subprocess.run(['docker', 'compose', 'version', '--short'],
                check=True, capture_output=True, timeout=10).stdout.decode().strip()

    def check(self, config, version=None):
        return scaler.validate(config, deployment_directory=DEPLOY, compose_version=version or self.version)

    def test_exact_rendered_overlay_preserves_base_and_disabled_controller(self):
        before = copy.deepcopy(self.config)
        self.assertTrue(self.check(self.config))
        self.assertEqual(self.config, before)
        control = self.config['services'][scaler.SERVICE]
        self.assertNotIn('--enabled', control['command'])
        self.assertNotIn('--credential-stdin', control['command'])
        self.assertEqual(set(control['networks']), {'database', 'edge'})
        self.assertEqual(control['environment']['AWS_EC2_METADATA_DISABLED'], 'true')

    def test_unreviewed_controller_or_admission_deltas_fail(self):
        mutations = [lambda w: w.update(user='0:0'), lambda w: w.update(privileged=True),
            lambda w: w.update(network_mode='host'), lambda w: w.update(ports=['8188:8188']),
            lambda w: w.update(mem_limit=2*1024**3), lambda w: w.update(cpus=2),
            lambda w: w['command'].append('--enabled'),
            lambda w: w['environment'].update(AWS_ACCESS_KEY_ID='synthetic'),
            lambda w: w['volumes'].append(acceptance.bind('/var/run/docker.sock', '/var/run/docker.sock')),
            lambda w: w['volumes'][-1].update(read_only=False),
            lambda w: w['networks'].update(web={})]
        for mutation in mutations:
            value = copy.deepcopy(self.config)
            mutation(value['services'][scaler.SERVICE])
            with self.subTest(mutation=mutation), self.assertRaises(release.ReleaseError):
                self.check(value)
        for key, value in [('SIXNINE_AUTH_MODE', 'local-test'), ('SIXNINE_CLOUD_CREATION_ENABLED', '1')]:
            changed = copy.deepcopy(self.config)
            changed['services']['app']['environment'][key] = value
            with self.assertRaises(validator.ConfigurationError):
                self.check(changed)

    def test_only_observed_2382_bind_false_serialization_is_normalized(self):
        changed = copy.deepcopy(self.config)
        for service in changed['services'].values():
            for mount in service.get('volumes', []):
                if mount.get('type') == 'bind':
                    mount['bind'] = {}
        self.assertTrue(self.check(changed, '2.38.2'))
        for version in ('2.38.1', '2.39.0', '5.6.0'):
            with self.assertRaises((release.ReleaseError, validator.ConfigurationError)):
                self.check(changed, version)
        changed['services'][scaler.SERVICE]['volumes'][0]['bind'] = {'create_host_path': True}
        with self.assertRaises(release.ReleaseError):
            self.check(changed, '2.38.2')


class FiniteControlTests(unittest.TestCase):
    def test_operator_policy_sources_and_runtime_identity_are_bound_before_start(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            for folder in ('operator', 'identity', 'public-source', 'control', 'tmp'):
                (root/folder).mkdir()
            source = root/'public-source'
            (source/'bootstrap_cloud.py').write_text('# synthetic public source')
            (source/'model_manifest.json').write_text('{}')
            config = {**configuration(), 'enabled': True, 'tenant': 'sixnine', 'owner': 'superdan',
                'work_dir': '/control', 'data_dir': '/data', 'source_dir': '/bootstrap-source',
                'ssh_key_file': '/worker-identity/key', 'known_hosts_file': '/control/known_hosts',
                'hard_deadline': 5000, 'source_sha256': {name: hashlib.sha256((source/name).read_bytes()).hexdigest()
                    for name in ('bootstrap_cloud.py', 'model_manifest.json')},
                'execution_policy_sha256': scaler.fingerprint({'approved': 'synthetic'})}
            config_path = root/'operator'/'scaler.json'
            config_path.write_text(json.dumps(config))
            (root/'operator'/'execution-policy.json').write_text(json.dumps({'approved': 'synthetic'}))
            metadata = {'secret_arn': config['secret_arn'], 'version_id': config['secret_version_id'],
                'service': 'lium', 'profile': 'lium--rig-root'}
            metadata_path = root/'runtime.json'
            metadata_path.write_text(json.dumps(metadata))
            for field, path in [('ROOT', root), ('SOURCE', source), ('CONFIG_SOURCE', config_path),
                                ('POLICY_SOURCE', root/'operator'/'execution-policy.json'), ('RUNTIME_METADATA', metadata_path)]:
                stack.enter_context(patch.object(scaler, field, path))
            regular = stack.enter_context(patch.object(release, 'regular', side_effect=lambda path, **kw:
                SimpleNamespace(st_uid=10001, st_mode=stat.S_IFREG|(0o600 if path.name == 'known_hosts' else 0o400))))
            stack.enter_context(patch.object(Path, 'lstat', autospec=True, side_effect=lambda path:
                SimpleNamespace(st_uid=10001 if path in (root/'control', root/'tmp') else 0, st_mode=stat.S_IFDIR|0o700)))
            self.assertEqual(scaler.protected_inputs(starting=True, now=4000), config)
            for field, value in [('owner', 'supervan'), ('work_dir', '/other'), ('secret_version_id', 'wrong')]:
                changed = {**config, field: value}
                config_path.write_text(json.dumps(changed))
                with self.subTest(field=field), self.assertRaises(release.ReleaseError):
                    scaler.protected_inputs(starting=True, now=4000)
            config_path.write_text(json.dumps(config))
            (source/'model_manifest.json').write_text('{"changed":true}')
            with self.assertRaisesRegex(release.ReleaseError, 'source_hash_mismatch'):
                scaler.protected_inputs(starting=True, now=4000)
            (source/'model_manifest.json').write_text('{}')
            (root/'control'/'known_hosts').write_text('synthetic-public-host-key')
            self.assertEqual(scaler.protected_inputs(starting=True, now=4000), config)
            self.assertTrue(any(call.args[0].name == 'known_hosts' for call in regular.call_args_list))
            (root/'control'/'cycle-state.json').write_text('{}')
            with self.assertRaisesRegex(release.ReleaseError, 'previous_cycle'):
                scaler.protected_inputs(starting=True, now=4000)

    def test_default_never_loads_or_calls_anything(self):
        with patch.object(release, 'check_host') as host, patch.object(scaler, 'launch') as launch, \
                patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(scaler.main([]), 0)
        host.assert_not_called()
        launch.assert_not_called()

    def test_credential_only_once_in_pipe_no_file_env_argv_or_output(self):
        config = configuration()
        runtime = SimpleNamespace(service='lium', profile='lium--rig-root', base_url='https://lium.io/api',
            primary_key_variable='LIUM_API_KEY', api_key='synthetic-secret-value')
        loader = Mock(return_value=runtime)
        process = Mock()
        captured = []
        process.stdin.write.side_effect = lambda data: captured.append(json.loads(data))
        factory, popen = Mock(return_value=loader), Mock(return_value=process)
        env = {'PATH': '/usr/bin', 'LANG': 'C.UTF-8'}
        self.assertIs(scaler.launch(Path('/release'), env, config, loader_factory=factory, popen=popen), process)
        loader.assert_called_once_with('lium', profile='lium--rig-root')
        loader.close.assert_called_once_with()
        process.stdin.close.assert_called_once_with()
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]['payload']['api_key'], runtime.api_key)
        args, kwargs = popen.call_args
        self.assertNotIn(runtime.api_key, json.dumps(args))
        self.assertEqual(kwargs['env'], env)
        self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        self.assertIn('--credential-stdin', args[0])
        self.assertNotIn('--rm', args[0])
        process.wait.assert_not_called()
        process.terminate.assert_not_called()

    def test_delivery_unknown_does_not_kill_or_retry(self):
        runtime = SimpleNamespace(service='lium', profile='lium--rig-root', base_url='https://lium.io/api',
            primary_key_variable='LIUM_API_KEY', api_key='synthetic')
        loader, process, popen = Mock(return_value=runtime), Mock(), Mock()
        popen.return_value = process
        process.stdin.write.side_effect = BrokenPipeError('synthetic')
        with self.assertRaisesRegex(release.ReleaseError, 'delivery_unconfirmed'):
            scaler.launch(Path('/release'), {}, configuration(), loader_factory=Mock(return_value=loader), popen=popen)
        self.assertEqual(popen.call_count, 1)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        loader.close.assert_called_once()

    def test_status_is_fresh_bound_and_not_cached_snapshot(self):
        config = configuration()
        self.assertTrue(scaler.fresh_drained(good_status(config), config))
        for key, value in [('snapshot_only', True), ('ledger_safe', False), ('drained', False),
                           ('all_destroyed', False), ('active_job_ids', ['job']), ('observed_at', 0),
                           ('config_hash', 'wrong'), ('cycle_id', 'wrong'), ('controller_exit_required', False),
                           ('active_jobs_truncated', True),
                           ('instances', [{'id': 'x', 'state': 'unknown'}]), ('billing_pending', -1)]:
            data = good_status(config)
            data[key] = value
            with self.subTest(key=key):
                self.assertFalse(scaler.fresh_drained(data, config))

    def test_already_restored_marker_is_idempotent_only_for_exact_cycle(self):
        config, commit = configuration(), 'a'*40
        value = {'version': 1, 'active': False, 'commit': commit, 'cycle_id': config['cycle_id'],
                 'config_hash': scaler.fingerprint(config), 'container_name': scaler.container_name(config)}
        with patch.object(scaler, 'read_json', return_value=value):
            with self.assertRaises(release.ReleaseError):
                scaler.verify_marker(commit, config)
            scaler.verify_marker(commit, config, allow_inactive=True)
            value['config_hash'] = 'wrong'
            with self.assertRaises(release.ReleaseError):
                scaler.verify_marker(commit, config, allow_inactive=True)

    def test_restore_closes_admission_waits_natural_exit_and_never_sends_signal(self):
        config, events = configuration(), []
        states = iter([container(configuration(), running=True), container(configuration())])
        def docker(args, **kwargs):
            events.append(args)
            self.assertEqual(args[0], 'inspect')
            return json.dumps([next(states)]).encode()
        def control(*args):
            events.append(args[-1])
            return good_status(config)
        with patch.object(scaler, 'close_admission', side_effect=lambda *_: events.append('cpu')), \
                patch.object(scaler, 'controller_control', side_effect=control), \
                patch.object(release, 'command', side_effect=docker):
            result = scaler.restore_cpu(Path('/release'), {}, config, sleep=lambda _: None)
        self.assertEqual(events[0:2], ['cpu', '--request-drain'])
        self.assertEqual(events[-1], '--status')
        self.assertEqual(result, {'billing_pending': 1, 'instance_count': 1})

    def test_unknown_running_nonzero_oom_or_wrong_container_never_complete(self):
        config = configuration()
        for data in [container(config, running=True), container(config, ExitCode=1),
                     container(config, OOMKilled=True), {**container(config), 'Name': '/other'}]:
            with patch.object(scaler, 'close_admission'), patch.object(scaler, 'controller_control', return_value=good_status(config)), \
                    patch.object(release, 'command', return_value=json.dumps([data]).encode()) as docker:
                with self.assertRaises(release.ReleaseError):
                    scaler.restore_cpu(Path('/release'), {}, config, timeout=0)
                self.assertTrue(all(c.args[0][0] == 'inspect' for c in docker.call_args_list))

    def test_control_process_is_independent_and_never_enabled(self):
        for action in ('--status', '--request-drain', '--validate'):
            with patch.object(scaler, 'compose', return_value=b'{}') as compose:
                scaler.controller_control(Path('/release'), {}, action)
            self.assertIn('--rm', compose.call_args.args)
            self.assertNotIn('--enabled', compose.call_args.args)
            self.assertNotIn('--credential-stdin', compose.call_args.args)

    def test_term_only_requests_drain_without_signalling_container(self):
        handlers, events = {}, []
        process = Mock()
        def wait(**kwargs):
            if not events:
                handlers[scaler.signal.SIGTERM]()
                events.append('term')
                raise subprocess.TimeoutExpired('synthetic', 5)
            return 0
        process.wait.side_effect = wait
        with tempfile.TemporaryDirectory() as tmp, patch.object(release, 'ROOT', Path(tmp)), \
                patch.dict(sys.modules, {'fcntl': SimpleNamespace(LOCK_EX=1, flock=lambda *_: None)}), \
                patch.object(scaler.signal, 'getsignal', return_value='original'), \
                patch.object(scaler.signal, 'signal', side_effect=lambda sig, handler: handlers.update({sig: handler})), \
                patch.object(scaler, 'close_admission', side_effect=lambda *_: events.append('cpu')), \
                patch.object(scaler, 'controller_control', side_effect=lambda *a: events.append(a[-1])):
            self.assertEqual(scaler.wait_for_controller(process, Path('/release'), {}), 0)
        self.assertEqual(events, ['term', 'cpu', '--request-drain'])
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertTrue(all(handler == 'original' for handler in handlers.values()))

    def test_auto_exit_restores_cpu_before_releasing_barrier_even_on_error(self):
        for code in (0, 1):
            config, events = configuration(), []
            process = Mock(returncode=code)
            process.wait.side_effect = lambda **_: events.append('wait')
            with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                stack.enter_context(patch.dict(sys.modules, {'fcntl': SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *_: None)}))
                stack.enter_context(patch.object(release, 'ROOT', Path(tmp)))
                for name in ('check_host', 'wait_ready'):
                    stack.enter_context(patch.object(release, name))
                stack.enter_context(patch.object(release, 'command', return_value=b'2.38.2'))
                stack.enter_context(patch.object(scaler, 'checked_release', return_value=(
                    'a'*40, Path(tmp), {'SIXNINE_IMAGE': 'sixnine-platform:'+'a'*40})))
                stack.enter_context(patch.object(scaler, 'protected_inputs', return_value=config))
                for name in ('require_new_controller', 'atomic', 'validate', 'verify_marker'):
                    stack.enter_context(patch.object(scaler, name))
                stack.enter_context(patch.object(scaler, 'compose', return_value=b'{}'))
                stack.enter_context(patch.object(scaler, 'controller_control', return_value={
                    'config_valid': True, 'provider_calls_enabled': False, 'config_hash': scaler.fingerprint(config)}))
                stack.enter_context(patch.object(scaler, 'launch', return_value=process))
                stack.enter_context(patch.object(scaler, 'marker', side_effect=lambda c, x, a: events.append(('marker', a))))
                stack.enter_context(patch.object(scaler, 'close_admission', side_effect=lambda *_: events.append('cpu')))
                stack.enter_context(patch.object(scaler, 'restore_cpu', side_effect=lambda *_: events.append('restored') or {'billing_pending': 0}))
                stack.enter_context(patch('sys.stdout', new=io.StringIO()))
                stack.enter_context(patch('sys.stderr', new=io.StringIO()))
                self.assertEqual(scaler.main(['start']), 0 if code == 0 else 1)
            self.assertLess(events.index('wait'), events.index('cpu'))
            if code == 0:
                self.assertLess(events.index('cpu'), events.index(('marker', False)))
            else:
                self.assertNotIn(('marker', False), events)


if __name__ == '__main__':
    unittest.main()
