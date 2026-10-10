"""Offline pending-deletion handoff; injected Docker/systemd, no provider calls."""
from contextlib import ExitStack, nullcontext
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout

from test_platform_release import module, release
from test_operator_host_release import host

with patch.dict(sys.modules, {'release':release, 'operator_capacity':host}):
    handoff = module('operator_handoff_test', 'operator_handoff.py')


def ledger_rows():
    return {
        'counts':{'active_jobs':0, 'unsafe_attempts':0, 'bound_workers':0},
        'intents':[{'id':'intent', 'state':'destroying', 'provider':'targon', 'provider_instance_id':'wkl-test',
                    'hard_deadline':1000, 'created_at':100, 'updated_at':200, 'reserved_cost_microusd':500}],
        'nodes':[{'intent_id':'intent', 'command_id':'start', 'ordinal':0, 'binding_id':'binding',
                  'binding_hash':'frozen', 'desired_state':'stopped', 'runtime_state':'removal_pending',
                  'payload':{'selection':{'node_count':1}, 'hourly_cost_microusd':100}}],
        'actions':[{'intent_id':'intent', 'destroy_started_at':200, 'last_observed_at':210,
                    'last_observation':{'state':'unknown'}, 'create_started_at':100}],
        'commands':[{'id':'start', 'kind':'start', 'state':'waiting', 'updated_at':200,
                     'payload':{'selection':{'node_count':1}, 'hard_deadline':1000}},
                    {'id':'stop', 'kind':'stop', 'state':'waiting', 'payload':{'node_id':'intent'}}],
        'accounts':[{'id':'owner', 'limit_microusd':900, 'reserved_microusd':500, 'spent_microusd':20}],
        'reservations':[{'id':'reservation', 'reference_type':'instance', 'reference_id':'intent',
                         'account_id':'owner', 'amount_microusd':500, 'state':'reserved', 'actual_cost_microusd':None}],
        'policy':[{'enabled':1, 'version':2}], 'gate':[{'max_instances':2}], 'limits':[{'pool':'existing'}],
    }


class LedgerTests(unittest.TestCase):
    def summary(self, rows):
        return handoff.ledger_summary(rows, {'binding':'frozen'})

    def test_pending_stop_adopted_without_changing_original_records(self):
        rows = ledger_rows(); original = copy.deepcopy(rows)
        value = self.summary(rows)
        self.assertEqual(value['pending_ids'], ['intent'])
        self.assertEqual(rows, original)

    def test_jobs_attempts_or_workers_block_handoff(self):
        for field in ('active_jobs', 'unsafe_attempts', 'bound_workers'):
            with self.subTest(field=field):
                rows = ledger_rows(); rows['counts'][field] = 1
                with self.assertRaisesRegex(ValueError, 'ledger_unsafe'): self.summary(rows)

    def test_unknown_creation_running_nodes_and_missing_delete_proof_are_rejected(self):
        for state in ('creating', 'creation_unknown', 'running', 'draining'):
            rows = ledger_rows(); rows['intents'][0]['state'] = state
            with self.subTest(state=state), self.assertRaises(ValueError): self.summary(rows)
        for field,value in (('provider_instance_id', None),):
            rows = ledger_rows(); rows['intents'][0][field] = value
            with self.assertRaises(ValueError): self.summary(rows)
        for value in (None, True, float('nan')):
            rows = ledger_rows(); rows['actions'][0]['destroy_started_at'] = value
            with self.assertRaises(ValueError): self.summary(rows)

    def test_incomplete_start_must_not_rent_missing_ordinal_after_handoff(self):
        rows = ledger_rows(); rows['commands'][0]['payload']['selection']['node_count'] = 2
        with self.assertRaises(ValueError): self.summary(rows)
        rows = ledger_rows(); rows['nodes'][0]['ordinal'] = 1
        with self.assertRaises(ValueError): self.summary(rows)

    def test_missing_reservation_changed_binding_or_stop_intent_rejected(self):
        for section,field,value in (('nodes','binding_hash','changed'), ('nodes','desired_state','running'),
                                    ('reservations','state','settled')):
            rows = ledger_rows(); rows[section][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.summary(rows)
        rows = ledger_rows(); rows['reservations'] = []
        with self.assertRaises(ValueError): self.summary(rows)

    def test_deadline_budget_request_and_selection_changes_break_fingerprint(self):
        original = self.summary(ledger_rows())['immutable_hash']
        mutations = [('intents','hard_deadline',1001), ('intents','provider_instance_id','wkl-other'),
                     ('accounts','limit_microusd',901), ('reservations','amount_microusd',501)]
        for section,field,value in mutations:
            rows = ledger_rows(); rows[section][0][field] = value
            self.assertNotEqual(self.summary(rows)['immutable_hash'], original)
        rows = ledger_rows(); rows['nodes'][0]['payload']['selection']['gpu_count'] = 8
        self.assertNotEqual(self.summary(rows)['immutable_hash'], original)

    def test_real_terminal_progress_and_settlement_do_not_change_identity(self):
        rows = ledger_rows(); original = self.summary(rows)
        rows['intents'][0].update(state='destroyed', updated_at=300)
        rows['actions'][0].update(last_observed_at=300, last_observation={'state':'destroyed'})
        rows['commands'][0]['state'] = 'blocked'; rows['commands'][1]['state'] = 'completed'
        rows['reservations'][0].update(state='settled', actual_cost_microusd=50)
        rows['accounts'][0].update(reserved_microusd=0, spent_microusd=70)
        current = self.summary(rows)
        self.assertEqual(current['immutable_hash'], original['immutable_hash'])
        self.assertNotEqual(current['accounting_hash'], original['accounting_hash'])
        self.assertEqual(current['pending_ids'], [])


class HostHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(host, 'ROOT', self.root))
        self.stack.enter_context(patch.object(handoff, 'locked', side_effect=lambda:nullcontext()))
        self.stack.enter_context(patch.object(release, '_protected_json', side_effect=lambda path, **kw:json.loads(Path(path).read_text())))
        self.writes = []
        def write(path,value):
            self.writes.append((Path(path).name, copy.deepcopy(value)))
            Path(path).write_text(json.dumps(value))
        self.stack.enter_context(patch.object(host, 'atomic', side_effect=write))
        self.old = {'schema_version':1, 'commit':'a'*40, 'image_id':'old-image', 'runtime_config_sha256':'d'*64, 'files':{'runtime':'e'*64}}
        self.pin = {**host.pin_for(self.old), 'state':'running', 'admission':'closed', 'controller_id':'old-controller'}
        self.runtime = {'existing':'unchanged'}
        self.environment = {'SIXNINE_IMAGE':'sixnine-platform:'+'a'*40}
        self.next_environment = {'SIXNINE_IMAGE':'sixnine-platform:'+'b'*40}
        self.overlay = {'services':{'app':{'environment':{'SIXNINE_OPERATOR_CAPACITY_OWNERS':'superdan'}}}}
        self.unit = {'MainPID':'0','ExecMainPID':'321','ActiveState':'failed','ExecStart':'original',
                     'Restart':'no','KillMode':'process','User':'root','unit_files':{'original.service':'frozen'}}
        self.ledger = handoff.ledger_summary(ledger_rows(), {'binding':'frozen'})
        self.record = {'schema_version':1, 'phase':'drain_requested', 'target_commit':'b'*40, 'target_image_id':'new-image',
            'old_prepared':self.old, 'old_pin':{**self.pin,'admission':'open'}, 'old_overlay':self.overlay,
            'supervisor_unit':'sixnine-old.service','supervisor':{**self.unit,'MainPID':'321','ActiveState':'active'}, 'ledger':self.ledger}
        write(handoff.record_path(), self.record); write(self.root/'overlay.json', self.overlay); write(self.root/'active.json', self.pin)
        self.prepared = self.mock(host, 'prepared', return_value=(self.runtime,self.old,self.root,self.environment))
        self.mock(host, 'checked_pin', side_effect=lambda value:json.loads((self.root/'active.json').read_text()))
        self.supervisor = self.mock(handoff, 'supervisor', return_value=self.unit)
        self.inspect = self.mock(host, 'inspect_controller', return_value={'Running':False,'Restarting':False,'Paused':False,
                                        'OOMKilled':False,'Status':'exited','ExitCode':0})
        self.receipt = self.mock(host, 'receipt', return_value={'state':'shutdown_complete','local_connections_released':True})
        self.mock(host, 'no_competing_controller')
        self.probe = self.mock(handoff, 'ledger_probe', return_value=self.ledger)
        self.mock(handoff, 'current_target', return_value=('b'*40,self.root,self.next_environment,'new-image'))
        self.mock(host, 'default_profile', return_value='original-profile')
        self.mock(host, 'overlay', return_value=self.overlay)
        self.mock(host, 'compose', return_value=b'{}')
        self.mock(release, 'command', return_value=b'2.38.2')
        self.validate = self.mock(host, 'validate_rendered')
        self.launch = self.mock(host, 'launch')
        self.drain = self.mock(host, 'request_drain')

    def mock(self, owner, name, **kwargs):
        return self.stack.enter_context(patch.object(owner, name, **kwargs))

    def test_successor_keeps_original_config_and_never_clears_barrier(self):
        runtime, prepared, _, _, pin = handoff.successor()
        self.assertEqual(runtime, self.runtime)
        self.assertEqual(prepared, {**self.old,'commit':'b'*40,'image_id':'new-image'})
        self.assertTrue(pin['active']); self.assertEqual(pin['admission'], 'closed')
        record = json.loads(handoff.record_path().read_text())
        self.assertEqual(record['old_prepared'], self.old)
        self.assertEqual(record['old_pin'], self.record['old_pin'])
        self.assertEqual(record['phase'], 'launch_intent')
        self.launch.assert_not_called()
        self.validate.assert_called_once()
        self.assertEqual(self.validate.call_args.kwargs, {'owners':'superdan'})

    def test_old_supervisor_still_alive_blocks_even_after_clean_container_exit(self):
        self.supervisor.return_value = {**self.unit, 'MainPID':'321', 'ActiveState':'active'}
        with self.assertRaisesRegex(release.ReleaseError, 'old_supervisor_not_retired'): handoff.successor()
        self.assertEqual(json.loads(handoff.record_path().read_text())['phase'], 'drain_requested')
        self.launch.assert_not_called()

    def test_uncertain_exit_or_local_children_block_handoff(self):
        for change in ({'ExitCode':137}, {'OOMKilled':True}, {'Running':True}, {'Paused':True}):
            original = self.inspect.return_value
            self.inspect.return_value = {**original, **change}
            with self.subTest(change=change), self.assertRaisesRegex(release.ReleaseError, 'old_exit_unconfirmed'):
                handoff.successor()
            self.inspect.return_value = original
        self.receipt.return_value = {'state':'shutdown_waiting','local_connections_released':False}
        with self.assertRaisesRegex(release.ReleaseError, 'local_ownership_unconfirmed'): handoff.successor()
        self.launch.assert_not_called()

    def test_configuration_or_ledger_change_blocks_before_pin_replacement(self):
        self.probe.return_value = {**self.ledger, 'immutable_hash':'f'*64}
        with self.assertRaisesRegex(release.ReleaseError, 'ledger_changed'): handoff.successor()
        self.assertEqual(json.loads((self.root/'active.json').read_text()), self.pin)
        self.probe.return_value = self.ledger
        self.prepared.return_value = (self.runtime,{**self.old,'files':{}},self.root,self.environment)
        with self.assertRaisesRegex(release.ReleaseError, 'configuration_changed'): handoff.successor()

    def test_post_staging_change_keeps_consumed_intent_and_blocks_replay(self):
        self.probe.side_effect = [self.ledger, {**self.ledger,'accounting_hash':'f'*64}]
        with self.assertRaisesRegex(release.ReleaseError, 'ledger_changed_before_launch'): handoff.successor()
        self.assertEqual(json.loads(handoff.record_path().read_text())['phase'], 'launch_intent')
        with self.assertRaisesRegex(release.ReleaseError, 'already_launched'): handoff.successor()
        self.launch.assert_not_called()

    def test_unknown_credential_delivery_is_never_replayed(self):
        self.launch.side_effect = TimeoutError('synthetic delivery uncertainty')
        with self.assertRaises(TimeoutError): handoff.start()
        self.assertEqual(json.loads(handoff.record_path().read_text())['phase'], 'launch_intent')
        with self.assertRaisesRegex(release.ReleaseError, 'already_launched'): handoff.start()
        self.launch.assert_called_once()
        self.drain.assert_called_once()

    def test_successful_start_launches_once_then_reopens_only_app(self):
        old_state = self.inspect.return_value
        self.inspect.side_effect = [old_state, {'Running':True,'Restarting':False,'OOMKilled':False}]
        self.receipt.side_effect = [self.receipt.return_value, {'state':'running','controller_id':'new-controller'}]
        process = Mock(returncode=0)
        process.poll.side_effect = [None, 0]
        def launch(*args):
            self.assertEqual(json.loads(handoff.record_path().read_text())['phase'], 'launch_intent')
            return process
        self.launch.side_effect = launch
        app = {'services':{'app':{}}}
        self.mock(host,'app_overlay',return_value=app)
        self.mock(host,'validate_app_rendered')
        self.mock(release,'wait_ready')
        restore = self.mock(host,'restore',return_value={'state':'cpu_restored'})
        with patch.object(release,'command',side_effect=[b'2.38.2',b'2.38.2',json.dumps(app).encode(),b'']) as command:
            self.assertEqual(handoff.start(),{'state':'cpu_restored'})
        self.launch.assert_called_once()
        self.assertEqual(command.call_args_list[-1].args[0][-4:], ['up','-d','--no-deps','app'])
        restore.assert_called_once()
        self.assertEqual(json.loads(handoff.record_path().read_text())['phase'],'running')
        pin = json.loads((self.root/'active.json').read_text())
        self.assertEqual((pin['controller_id'],pin['admission']),('new-controller','open'))

    def test_prepare_records_original_identity_before_term(self):
        handoff.record_path().unlink()
        self.supervisor.return_value = {**self.unit,'MainPID':'321','ActiveState':'active'}
        self.receipt.return_value = {'state':'running'}
        self.mock(handoff, 'only_controller')
        self.mock(handoff, 'supervisor_client', return_value={'supervisor_pid':321,'docker_client_pids':[322]})
        self.drain.side_effect = lambda *args:self.assertEqual(json.loads(handoff.record_path().read_text())['phase'],'drain_requested')
        result = handoff.prepare('b'*40, 'sixnine-old.service')
        self.assertEqual(result['pending_deletions'], 1)
        self.drain.assert_called_once()
        with self.assertRaisesRegex(release.ReleaseError, 'already_recorded'):
            handoff.prepare('b'*40, 'sixnine-old.service')
        self.drain.assert_called_once()

    def test_supervisor_process_must_own_exact_controller_client(self):
        proc = self.root/'proc'
        def process(pid, argv, children):
            folder = proc/str(pid); (folder/'task'/str(pid)).mkdir(parents=True)
            (folder/'cmdline').write_bytes(b'\0'.join(arg.encode() for arg in argv)+b'\0')
            (folder/'task'/str(pid)/'children').write_text(' '.join(str(child) for child in children))
        unit = {**self.unit,'MainPID':'321'}
        process(321, ['/usr/bin/python3','/opt/sixnine-release/operator_capacity.py','start'], [322])
        process(322, ['/usr/bin/docker','compose','-f',str(self.root/'compose.yaml'),'run',
                      '--name',self.pin['container_name'],'--label',host.LABEL+'='+self.pin['prepared_hash'],host.SERVICE], [])
        proof = handoff.supervisor_client(unit,self.pin,self.root,proc_root=proc)
        self.assertEqual(proof['docker_client_pids'], [322])
        (proc/'322'/'cmdline').write_bytes(b'/usr/bin/docker\0compose\0run\0--name\0unrelated\0')
        with self.assertRaisesRegex(release.ReleaseError, 'does_not_own_controller'):
            handoff.supervisor_client(unit,self.pin,self.root,proc_root=proc)
        (proc/'321'/'cmdline').write_bytes(b'/usr/bin/python3\0/unrelated.py\0start\0')
        with self.assertRaisesRegex(release.ReleaseError, 'process_changed'):
            handoff.supervisor_client(unit,self.pin,self.root,proc_root=proc)


class ControllerProcessTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(host, 'inspect_controller', return_value={
            'Running':True, 'Paused':False, 'Restarting':False, 'OOMKilled':False}))
        self.command = self.stack.enter_context(patch.object(release, 'command'))
        self.pin = {'container_name':'exact-original-controller'}

    def test_native_pid_comm_output_accepts_only_controller_and_optional_init(self):
        for output in (b'PID COMMAND\n101 python\n',
                       b'PID COMMAND\n100 docker-init\n101 python\n',
                       b'PID COMMAND\n100 tini\n101 python3\n'):
            with self.subTest(output=output):
                self.command.return_value = output
                handoff.only_controller({}, self.pin)
        self.assertEqual(self.command.call_args.args[0],
            ['top','exact-original-controller','-eo','pid,comm'])

    def test_extra_processes_and_shell_wrappers_block(self):
        for output in (b'PID COMMAND\n100 python\n101 python\n',
                       b'PID COMMAND\n100 python\n101 ssh\n',
                       b'PID COMMAND\n100 sh\n',
                       b'PID COMMAND\n100 tini\n101 docker-init\n102 python\n'):
            with self.subTest(output=output):
                self.command.return_value = output
                with self.assertRaisesRegex(release.ReleaseError,'owned_children_present'):
                    handoff.only_controller({}, self.pin)

    def test_empty_malformed_or_unexpected_process_format_blocks(self):
        for output in (b'', b'PID COMMAND\n', b'COMMAND\npython\n',
                       b'PID CMD\n100 python\n', b'PID COMMAND\nx python\n',
                       b'PID COMMAND\n0 python\n', b'PID COMMAND\n100 python extra\n',
                       b'PID COMMAND\n100 tini\n100 python\n'):
            with self.subTest(output=output):
                self.command.return_value = output
                with self.assertRaisesRegex(release.ReleaseError,'process_inspection_invalid'):
                    handoff.only_controller({}, self.pin)

    def test_failed_inspection_blocks_without_fallback(self):
        self.command.side_effect = release.ReleaseError('container_operation_failed_no_details_logged')
        with self.assertRaisesRegex(release.ReleaseError,'container_operation_failed'):
            handoff.only_controller({}, self.pin)
        self.command.assert_called_once()


class SystemdIdentityTests(unittest.TestCase):
    def test_exec_metadata_changes_do_not_change_supervisor_identity(self):
        command = '/usr/bin/python3 /opt/sixnine-release/operator_capacity.py start'
        def output(metadata):
            return ('MainPID=321\nExecMainPID=321\nRestart=no\nKillMode=process\nActiveState=active\nUser=root\n'
                'FragmentPath=/etc/systemd/system/sixnine-original.service\nDropInPaths=\n'
                'ExecStart={ path=/usr/bin/python3 ; argv[]='+command+' ; ignore_errors=no ; '+metadata+' }\n').encode()
        with patch.object(handoff.subprocess,'run',side_effect=[Mock(stdout=output('pid=321 ; code=(null) ;')),
                        Mock(stdout=output('pid=321 ; code=exited ; status=1 ;'))]), \
                patch.object(host,'protected_file',side_effect=lambda path:path), \
                patch.object(release,'checksum',return_value='unit-hash'):
            before = handoff.supervisor('sixnine-original.service')
            after = handoff.supervisor('sixnine-original.service')
        self.assertEqual(before, after)
        with patch.object(handoff.subprocess,'run',return_value=Mock(stdout=output('pid=321 ;').replace(
                command.encode(),b'/usr/bin/python3 /opt/sixnine-release/operator_capacity.py start --wrong'))):
            with self.assertRaisesRegex(release.ReleaseError,'command_changed'):
                handoff.supervisor('sixnine-original.service')


class RealSchemaProbeTests(unittest.TestCase):
    def test_probe_uses_real_schema_and_preserves_expired_unbound_worker(self):
        # Reuse isolated fixture setup and only its injected fake provider.
        from test_operator_capacity import OperatorTests
        from sqlalchemy import select, update
        from studio_platform.repository import instance_intents, scaler_actions, registered_workers
        from studio_platform.operator_capacity import operator_commands, operator_nodes
        from studio_platform.control import WorkerControl, WorkerSpec
        from types import SimpleNamespace
        import sqlalchemy
        import studio_platform.operator_runtime as runtime
        from studio_platform.settings import Settings
        case = OperatorTests(); case.setUp()
        try:
            case.create()
            with case.repo.engine.connect() as conn:
                command = dict(conn.execute(select(operator_commands)).mappings().one())
            case.controller._start(command)
            intent = case.repo.list_instance_intents()[0]
            control = WorkerControl(case.repo)
            control.register(WorkerSpec('historical-worker',case.binding.pool,'lium',intent['provider_instance_id'],
                ('GPU-fixture',),case.binding.recipe_ids,case.binding.model_id,case.binding.configuration_id,
                backend='wangp-worker',engine_manifest_digest=case.binding.engine_manifest_digest))
            with case.repo.transaction() as conn:
                conn.execute(update(instance_intents).values(state='destroying'))
                conn.execute(update(operator_nodes).values(desired_state='stopped'))
                conn.execute(update(scaler_actions).values(destroy_started_at=case.now))
                conn.execute(update(registered_workers).values(expires_at=1))
            scripts = []
            real_engine = case.repo.engine
            class Connection:
                def __enter__(self):
                    self.conn = real_engine.connect(); return self
                def __exit__(self,*args): self.conn.close()
                def execute(self, statement, *args, **kwargs):
                    if str(statement).startswith('SET TRANSACTION'):
                        return None  # SQLite fixture; production keeps PostgreSQL READ ONLY.
                    return self.conn.execute(statement,*args,**kwargs)
            engine = SimpleNamespace(connect=lambda:Connection(),dispose=lambda:None)
            def compose(*args, **kwargs):
                script = args[-1]; scripts.append(script)
                output = io.StringIO()
                with patch.object(sqlalchemy,'create_engine',return_value=engine), \
                        patch.object(runtime,'create_registry',return_value=case.registry), \
                        patch.object(Settings,'from_environment',return_value=SimpleNamespace(database_url='fixture')), \
                        redirect_stdout(output):
                    exec(compile(script,'<read-only-probe>','exec'),{})
                return output.getvalue().encode()
            with patch.object(host,'compose',side_effect=compose):
                result = handoff.ledger_probe(Path('/fake-release'),{},require_removal_cadence=True)
                self.assertEqual(result['pending_ids'],[intent['id']])
                with case.repo.transaction() as conn:
                    conn.execute(update(registered_workers).values(expires_at=10**12))
                with self.assertRaisesRegex(ValueError,'ledger_unsafe'):
                    handoff.ledger_probe(Path('/fake-release'),{})
            self.assertIn('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY',scripts[0])
            self.assertIn('REMOVAL_CHECK_INTERVAL_SECONDS == 60',scripts[0])
            with real_engine.connect() as conn:
                worker = conn.execute(select(registered_workers)).mappings().one()
                self.assertEqual(worker['id'],'historical-worker')
                self.assertNotEqual(worker['state'],'retired')
        finally:
            case.tearDown(); case.doCleanups()


if __name__ == '__main__':
    unittest.main()
