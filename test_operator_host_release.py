"""Offline host activation/termination boundary; no cloud or running containers."""
from contextlib import ExitStack
import ast
import copy
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from test_platform_release import module, release, validator, DIRECTORY

with patch.dict(sys.modules,{'release':release,'check_config':validator}):
    host = module('operator_host_test','operator_capacity.py')

IMAGE = 'sixnine-platform:'+'a'*40
PROFILE = 'h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1'


def counts(**changes):
    return {'counts':{'active_jobs':0,'unsafe_attempts':0,'live_instances':0,
        'bound_workers':0,'pending_commands':0,'billing_pending':0,**changes}}


def prepared():
    return {'schema_version':1,'commit':'a'*40,'image_id':'sha256:'+'b'*64,
        'runtime_config_sha256':'c'*64,'files':{'synthetic':'d'*64}}


class HostBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.operator = self.root/'operator-capacity';self.operator.mkdir()
        (self.operator/'control').mkdir()
        self.stack = ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(host,'ROOT',self.operator))
        self.stack.enter_context(patch.object(release,'ROOT',self.root))

    def test_release_fence_rejects_active_malformed_and_unmarked_running(self):
        path=self.operator/'active.json'
        for value in ({'version':1,'active':True},{'version':True,'active':False},
                      {'version':1,'active':False,'state':'uncertain','admission':'closed'}):
            path.write_text(json.dumps(value))
            with patch.object(release,'protected_directory'),patch.object(release.os,'name','nt'), \
                 patch.object(release,'command') as command,self.assertRaises(release.ReleaseError):
                release.operator_release_fence(self.root)
            command.assert_not_called()
        path.unlink()
        with patch.object(release,'command',return_value=b'abc123'),self.assertRaisesRegex(release.ReleaseError,'still_running'):
            release.operator_release_fence(self.root)
        path.write_text(json.dumps({'version':1,'active':False,'state':'restored','admission':'closed'}))
        with patch.object(release,'protected_directory'),patch.object(release.os,'name','nt'),patch.object(release,'command',return_value=b''):
            release.operator_release_fence(self.root)

    def test_both_apply_and_legacy_cpu_restore_use_operator_fence(self):
        with patch.object(release,'operator_release_fence',side_effect=release.ReleaseError('blocked')) as fence:
            with self.assertRaisesRegex(release.ReleaseError,'blocked'):
                release.gpu_deployment_context(self.root)
            with self.assertRaisesRegex(release.ReleaseError,'blocked'):
                release.restore_current_cpu_locked(self.root)
        self.assertEqual(fence.call_count,2)

    def test_explicit_own_admission_pin_cannot_restore_another_release(self):
        pin={**host.pin_for(prepared()),'admission':'closed'}
        (self.operator/'active.json').write_text(json.dumps(pin))
        with patch.object(release,'protected_directory'),patch.object(release.os,'name','nt'), \
             patch.object(release,'current_application',return_value=('e'*40,self.root,{})), \
             patch.object(release,'command') as command,self.assertRaisesRegex(release.ReleaseError,'release_changed'):
            release.restore_current_cpu_locked(self.root,operator_pin=pin)
        command.assert_not_called()

    def test_credential_exists_only_in_closed_private_stdin(self):
        runtime={'secret_arn':'synthetic-pinned-arn','secret_version_id':'synthetic-version'}
        config=NS(service='lium',profile='lium--rig-root',base_url='https://lium.io/api',
            primary_key_variable='LIUM_API_KEY',api_key='synthetic-private-value')
        loader=Mock(return_value=config); pipe=Mock(); process=NS(stdin=pipe)
        popen=Mock(return_value=process)
        result=host.launch(self.root,{'PATH':'/usr/bin'},runtime,host.pin_for(prepared()),
            loader_factory=lambda *a:loader,popen=popen)
        self.assertIs(result,process)
        payload=json.loads(pipe.write.call_args.args[0])
        self.assertEqual(payload['payload']['api_key'],'synthetic-private-value')
        self.assertEqual(payload['secret_arn'],runtime['secret_arn'])
        pipe.close.assert_called_once();loader.close.assert_called_once()
        args,options=popen.call_args
        self.assertNotIn('synthetic-private-value',repr(args)+repr(options))
        self.assertEqual(options['env'],{'PATH':'/usr/bin'})
        self.assertEqual(options['stdout'],subprocess.DEVNULL)
        self.assertIn('--enabled',args[0]);self.assertNotIn('--rm',args[0])
        position=args[0].index(host.SERVICE)
        self.assertEqual(args[0][position+1:position+3],['-c',host.CANONICAL_ENTRYPOINT])

    def test_real_child_entrypoints_accept_factory_class_before_first_tick(self):
        # Real Python process/import behavior matters: calling main in the test
        # process never reproduced the __main__/canonical class split from -m.
        factory=self.root/'offline_host_factory.py'
        factory.write_text('''def create(path):
    from studio_platform.operator_controller import OperatorController
    controller = object.__new__(OperatorController)
    controller.enabled = True
    def tick():
        print("OFFLINE_FIRST_TICK_REACHED", flush=True)
        raise SystemExit(42)
    controller.tick = tick
    return controller
''')
        repo=Path(__file__).resolve().parent
        environment=dict(os.environ,PYTHONPATH=os.pathsep.join((str(self.root),str(repo))))
        for entry in (['-c',host.CANONICAL_ENTRYPOINT],['-m',host.MODULE]):
            with self.subTest(entry=entry):
                result=subprocess.run([sys.executable,*entry,
                    '--factory','offline_host_factory:create','--config','unused','--enabled'],
                    cwd=repo,env=environment,capture_output=True,text=True,timeout=20)
                self.assertEqual(result.returncode,42,result.stdout)
                self.assertEqual(result.stdout.strip(),'OFFLINE_FIRST_TICK_REACHED')

    def test_stdin_delivery_uncertainty_never_kills_or_relaunches(self):
        loader=Mock(return_value=NS(service='lium',profile='lium--rig-root',base_url='https://lium.io/api',
            primary_key_variable='LIUM_API_KEY',api_key='synthetic'))
        process=Mock();process.stdin.write.side_effect=BrokenPipeError
        popen=Mock(return_value=process)
        with self.assertRaisesRegex(release.ReleaseError,'delivery_unknown'):
            host.launch(self.root,{},dict(secret_arn='a',secret_version_id='b'),host.pin_for(prepared()),
                loader_factory=lambda *a:loader,popen=popen)
        popen.assert_called_once();process.kill.assert_not_called();process.terminate.assert_not_called()

    def lifecycle_patches(self, process, launch):
        installed={}
        self.stack.enter_context(patch.dict(sys.modules,{'fcntl':NS(LOCK_EX=1,LOCK_NB=2,flock=Mock())}))
        self.stack.enter_context(patch.object(host.signal,'getsignal',return_value=None))
        self.stack.enter_context(patch.object(host.signal,'signal',side_effect=lambda key,fn:installed.update({key:fn})))
        self.stack.enter_context(patch.object(host,'prepared',return_value=({},prepared(),self.root,{})))
        self.stack.enter_context(patch.object(host,'no_competing_controller'))
        self.stack.enter_context(patch.object(host,'probe',return_value=counts()))
        self.stack.enter_context(patch.object(host,'launch',side_effect=launch))
        self.stack.enter_context(patch.object(host,'inspect_controller',return_value={'Running':True,'Restarting':False,'OOMKilled':False}))
        self.stack.enter_context(patch.object(host,'receipt',return_value={'state':'running','controller_id':'test-controller'}))
        self.stack.enter_context(patch.object(host,'compose'))
        self.stack.enter_context(patch.object(release,'wait_ready'))
        return installed

    def test_start_persists_before_launch_then_signal_drains_without_forcing_exit(self):
        events=[];process=NS(returncode=None)
        process.poll=lambda:process.returncode
        def launch(*args):
            pin=json.loads((self.operator/'active.json').read_text())
            self.assertTrue(pin['active']);self.assertEqual(pin['admission'],'closed')
            events.append('launch');return process
        installed=self.lifecycle_patches(process,launch)
        def sleep(_):
            if not process.returncode and 'signal' not in events:
                events.append('signal');installed[host.signal.SIGTERM](host.signal.SIGTERM,None)
        def drain(*args):
            self.assertEqual(args[-1]['admission'],'open')
            events.append('drain');process.returncode=0
        with patch.object(host,'request_drain',side_effect=drain), \
             patch.object(host,'restore',side_effect=lambda *a:events.append('restore') or {'state':'restored'}):
            self.assertEqual(host.start(sleep=sleep),{'state':'restored'})
        self.assertEqual(events,['launch','signal','drain','restore'])

    def test_start_unknown_delivery_retains_durable_intent_and_refuses_replay(self):
        launch=Mock(side_effect=release.ReleaseError('operator_credential_delivery_unknown'))
        self.lifecycle_patches(None,launch)
        with patch.object(host,'request_drain'):
            with self.assertRaisesRegex(release.ReleaseError,'delivery_unknown'):host.start()
            self.assertTrue(json.loads((self.operator/'active.json').read_text())['active'])
            with self.assertRaisesRegex(release.ReleaseError,'previous_launch'):host.start()
        launch.assert_called_once()

    def test_sql_probe_accepts_only_unsubmitted_deferred_attempts(self):
        assignment=next(n for n in ast.walk(ast.parse(host.PROBE)) if isinstance(n,ast.Assign)
            and any(isinstance(t,ast.Name) and t.id=='queries' for t in n.targets))
        query=ast.literal_eval(assignment.value)['unsafe_attempts']
        with sqlite3.connect(':memory:') as connection:
            connection.execute('CREATE TABLE platform_attempts (status TEXT, submission_started_at REAL, upstream_task_id TEXT, upstream_stopped INTEGER)')
            connection.executemany('INSERT INTO platform_attempts VALUES (?,?,?,?)',[
                ('deferred',None,None,0),('succeeded',1,'task',1)])
            self.assertEqual(connection.execute(query).fetchone()[0],0)
            connection.executemany('INSERT INTO platform_attempts VALUES (?,?,?,?)',[
                ('deferred',1,None,0),('failed',None,'unknown-task',0),('claimed',None,None,0)])
            self.assertEqual(connection.execute(query).fetchone()[0],3)

    def test_restore_requires_exited_exact_controller_and_local_exit_proof(self):
        pin=host.pin_for(prepared())
        state={'Running':False,'Restarting':False,'OOMKilled':False,'Status':'exited','ExitCode':0}
        proof={'state':'shutdown_complete','local_connections_released':True}
        with patch.object(host,'checked_pin',return_value=pin),patch.object(host,'close_admission') as close, \
             patch.object(host,'inspect_controller',return_value=state),patch.object(host,'receipt',return_value=proof), \
             patch.object(host,'probe',return_value=counts(billing_pending=1)),patch.object(host,'atomic') as write:
            result=host.restore(self.root,{},prepared())
            self.assertFalse(write.call_args.args[1]['active'])
            self.assertFalse(result['billing_settled'])
            self.assertTrue(result['budgets_unchanged'])
            close.assert_called_once()
            for mutation in ({'Running':True},{'ExitCode':137},{'OOMKilled':True},{'Restarting':True}):
                with patch.object(host,'inspect_controller',return_value={**state,**mutation}), \
                     self.assertRaisesRegex(release.ReleaseError,'exit_unconfirmed'):
                    host.restore(self.root,{},prepared())
            with patch.object(host,'receipt',return_value={'state':'shutdown_waiting','local_connections_released':False}), \
                 self.assertRaisesRegex(release.ReleaseError,'collection_unconfirmed'):
                host.restore(self.root,{},prepared())

    def test_any_execution_obligation_blocks_restore_without_budget_changes(self):
        for key in ('active_jobs','unsafe_attempts','live_instances','bound_workers','pending_commands'):
            with self.subTest(key=key),self.assertRaisesRegex(release.ReleaseError,'obligations'):
                host.require_quiet(counts(**{key:1}))
        host.require_quiet(counts(billing_pending=4))  # Reservations remain unchanged, reported separately.
        self.assertNotIn('create_all',host.PROBE)
        self.assertNotIn('Repository(',host.PROBE)
        self.assertIn('SET TRANSACTION READ ONLY',host.PROBE)
        self.assertIn('upstream_stopped != 1',host.PROBE)

    def test_drain_sends_term_only_after_admission_closed(self):
        events=[];pin=host.pin_for(prepared())
        with patch.object(host,'close_admission',side_effect=lambda *a:events.append('closed')), \
             patch.object(host,'inspect_controller',return_value={'Running':True}), \
             patch.object(release,'command',side_effect=lambda args,**kw:events.append(args)):
            host.request_drain(self.root,{},pin)
        self.assertEqual(events,['closed',['kill','--signal','TERM',pin['container_name']]])
        self.assertIn('TimeoutStopSec=infinity',host.unit_text())
        self.assertIn('KillMode=process',host.unit_text())
        self.assertIn('SendSIGKILL=no',host.unit_text())

    def test_receipt_binds_config_controller_identity_and_freshness(self):
        pin={**host.pin_for(prepared()),'controller_id':'controller-one'}
        record={'schema_version':1,'runtime_config_sha256':pin['runtime_config_sha256'],
            'controller_id':'controller-one','observed_at':100,'state':'running',
            'cloud_removal_confirmed':False,'billing_settled':False,'local_connections_released':False}
        path=self.operator/'control'/'controller-status.json'
        with patch.object(host.time,'time',return_value=105),patch.object(release.os,'name','nt'):
            for change in ({},{'runtime_config_sha256':'wrong'},{'controller_id':'another'},
                           {'observed_at':10},{'observed_at':106},{'cloud_removal_confirmed':True}):
                path.write_text(json.dumps({**record,**change}))
                if change:
                    with self.assertRaises(release.ReleaseError):host.receipt(pin,fresh=True)
                else:self.assertEqual(host.receipt(pin,fresh=True),record)


class ComposeBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('docker'): raise unittest.SkipTest('Compose CLI unavailable')
        with tempfile.TemporaryDirectory() as td:
            overlay=Path(td)/'overlay.json';overlay.write_text(json.dumps(host.overlay(IMAGE,PROFILE)))
            empty=Path(td)/'empty.env';empty.write_text('')
            env=dict(os.environ,SIXNINE_IMAGE=IMAGE,SIXNINE_POSTGRES_IMAGE='postgres@sha256:'+'b'*64,
                SIXNINE_CADDY_IMAGE='caddy@sha256:'+'c'*64,SIXNINE_DB_ADMIN_SECRET_FILE='/run/sixnine-secrets/db_admin_password',
                SIXNINE_APP_DSN_SECRET_FILE='/run/sixnine-secrets/app_database_url')
            raw=subprocess.run(['docker','compose','--env-file',str(empty),'-f',str(DIRECTORY/'compose.yaml'),
                '-f',str(overlay),'config','--format','json'],env=env,capture_output=True,timeout=30)
            if raw.returncode: raise AssertionError('Synthetic compose render failed')
            cls.value=json.loads(raw.stdout)
            cls.version=subprocess.run(['docker','compose','version','--short'],check=True,capture_output=True,timeout=10).stdout.decode().strip()

    def check(self,value):
        return host.validate_rendered(value,DIRECTORY,self.version,IMAGE,PROFILE)

    def test_real_render_keeps_app_off_edge_and_private_paths_controller_only(self):
        self.assertTrue(self.check(self.value))
        app=self.value['services']['app'];controller=self.value['services'][host.SERVICE]
        self.assertEqual(set(app['networks']),{'database','web'})
        self.assertEqual(set(controller['networks']),{'database','edge'})
        self.assertNotIn('--enabled',controller['command'])
        self.assertEqual(app['environment']['SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID'],PROFILE)
        self.assertEqual(app['environment']['SIXNINE_OPERATOR_CAPACITY_OWNERS'],'superdan')
        for mount in app['volumes']:
            self.assertNotIn(str(host.KEY),mount['source'])
            self.assertNotIn('/control',mount['source'])
            if '/operator-capacity/' in mount['source']:self.assertTrue(mount['read_only'])

    def test_wrong_image_private_mount_network_env_or_enable_flag_rejected(self):
        for mutate in (
            lambda c:c['services'][host.SERVICE].update(image='sixnine-platform:'+'e'*40),
            lambda c:c['services']['app']['networks'].update(edge={}),
            lambda c:c['services']['app']['volumes'].append(host.bind(host.KEY,True)),
            lambda c:c['services'][host.SERVICE]['environment'].update(AWS_ACCESS_KEY_ID='synthetic'),
            lambda c:c['services'][host.SERVICE]['command'].append('--enabled'),
            lambda c:c['services']['app']['environment'].update(SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID='wrong')):
            changed=copy.deepcopy(self.value);mutate(changed)
            with self.assertRaises((release.ReleaseError,validator.ConfigurationError)):self.check(changed)


if __name__=='__main__':unittest.main()
