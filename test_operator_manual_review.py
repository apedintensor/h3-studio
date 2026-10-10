"""Exact human review authority, capacity and guardian boundaries; offline only."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import insert, select, text, update

from studio_platform.auth import Principal
from studio_platform.operator_capacity import (OperatorError, operator_commands, operator_nodes,
    operator_heartbeats,MANUAL_REVIEW_CONTROLLER_PREFIX)
from studio_platform.repository import (instance_intents, scaler_actions, scaler_receipts,
    registered_workers, registered_devices, attempts, jobs)
import test_operator_capacity as operator_fixtures
import test_targon_cleanup as guardian_fixtures
from tools.targon_cleanup_guard import OperatorManualReviewReader, ProtectedManualReviewReader
from studio_platform.scaler import ProviderFact
from studio_platform.targon_cleanup import _hash


class ManualReviewTests(operator_fixtures.OperatorTests):
    def setUp(self):
        operator_fixtures.OperatorTests.setUp(self)
        self.controller.leader_id=MANUAL_REVIEW_CONTROLLER_PREFIX+'offline'
        created=operator_fixtures.OperatorTests.create(self)
        with self.repo.engine.connect() as connection:
            command=connection.execute(select(operator_commands).where(operator_commands.c.id==created['id'])).mappings().one()
        self.controller._start(command)
        node=self.service.state(self.actor)['nodes'][0]
        self.service.node_command(self.actor,node['id'],{'expected_version':node['version']},'stop-before-review','stop')
        self.uid='wrk-offline-review'
        self.node_id=node['id']
        with self.repo.transaction() as connection:
            connection.execute(insert(operator_heartbeats).values(id='global',
                controller_id=self.controller.leader_id,observed_at=self.now,state='running'))
            connection.execute(update(instance_intents).where(instance_intents.c.id==self.node_id).values(
                provider='targon',state='destroying',provider_instance_id=self.uid))
            connection.execute(update(scaler_actions).where(scaler_actions.c.intent_id==self.node_id).values(
                destroy_started_at=self.now,last_observation={'state':'unknown','instance_id':self.uid}))
        self.body=self.body_now()

    def body_now(self):
        node=self.service.state(self.actor)['nodes'][0]
        return {'expected_version':node['version'],'provider_instance_id':self.uid,
            'account_absent':True,'no_continuing_charge':True}

    def test_exact_review_preserves_reservation_deadline_and_provider_state(self):
        before=self.repo.list_instance_intents()[0]
        budget=self.repo.get_budget('owner-budget')
        result=self.service.manual_review(self.actor,self.node_id,self.body,'review-once')
        self.assertEqual(self.service.manual_review(self.actor,self.node_id,self.body,'review-once'),result)
        after=self.repo.list_instance_intents()[0]
        for key in ('state','provider_instance_id','hard_deadline','reserved_cost_microusd','actual_cost_microusd','billing_status'):
            self.assertEqual(after[key],before[key])
        self.assertEqual(after['state'],'destroying')
        self.assertEqual(after['billing_status'],'pending')
        self.assertEqual(self.repo.get_budget('owner-budget'),budget)
        snapshot=self.service.state(self.actor)
        self.assertEqual(snapshot['summary']['nodes_active'],0)
        self.assertEqual(snapshot['summary']['gpus_allocated'],0)
        self.assertEqual(snapshot['summary']['hourly_cost_microusd'],0)
        proof=snapshot['nodes'][0]['removal_confirmation']
        self.assertEqual(proof['state'],'manually_reviewed')
        self.assertIsNone(proof['next_check_at'])
        self.assertEqual(proof['manual_review']['actor'],'superdan')
        self.assertFalse(snapshot['nodes'][0]['actions']['manual_review']['allowed'])
        with self.repo.engine.connect() as connection:
            self.assertEqual(len(list(connection.execute(select(scaler_receipts).where(
                scaler_receipts.c.operation=='manual_review')))),1)
        # No constructor, provider query, invoice poll or bootstrap after closure.
        self.controller.provider_factory=Mock(side_effect=AssertionError('provider accessed'))
        self.controller.tick()
        self.controller.provider_factory.assert_not_called()
        self.assertEqual(self.repo.list_instance_intents()[0]['billing_status'],'pending')

    def test_operator_cookie_attestation_identity_version_and_replay_conflicts(self):
        for actor in (None,Principal('outsider','browser'),Principal('superdan','pat',machine=True)):
            with self.assertRaises(OperatorError): self.service.manual_review(actor,self.node_id,self.body,'denied')
        for patch,code in (({'account_absent':False},'attestation_required'),
                           ({'no_continuing_charge':False},'attestation_required'),
                           ({'provider_instance_id':'wrk-other'},'identity_mismatch'),
                           ({'expected_version':'wrong'},'version_conflict')):
            with self.assertRaisesRegex(OperatorError,code):
                self.service.manual_review(self.actor,self.node_id,{**self.body,**patch},'bad-review')
        self.service.manual_review(self.actor,self.node_id,self.body,'once')
        with self.assertRaisesRegex(OperatorError,'idempotency_conflict'):
            self.service.manual_review(self.actor,self.node_id,{**self.body,'provider_instance_id':'wrk-other'},'once')
        with self.assertRaisesRegex(OperatorError,'manually_reviewed'):
            self.service.manual_review(self.actor,self.node_id,self.body_now(),'new-key')

    def test_http_mark_is_operator_only_read_free_and_exact_replayable(self):
        from fastapi import FastAPI,Request
        from fastapi.testclient import TestClient
        from studio_platform.operator_routes import register_routes
        app=FastAPI();actor=[Principal('outsider','browser')]
        @app.middleware('http')
        async def injected_principal(request:Request,next_call):
            request.state.principal=actor[0]
            return await next_call(request)
        register_routes(app,service=self.service)
        path='/v1/operator/capacity/nodes/'+self.node_id+'/manual-review'
        with TestClient(app) as client:
            self.assertEqual(client.post(path,json=self.body,headers={'Idempotency-Key':'api-once'}).status_code,403)
            actor[0]=self.actor
            self.assertEqual(client.post(path,json=self.body).status_code,422)
            response=client.post(path,json=self.body,headers={'Idempotency-Key':'api-once'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers['Cache-Control'],'no-store')
            replay=client.post(path,json=self.body,headers={'Idempotency-Key':'api-once'})
            self.assertEqual(replay.json(),response.json())

    def test_never_reviews_live_unknown_create_missing_stop_or_wrong_provider(self):
        for values,code in (({'state':'ready'},'requires_requested_removal'),
                            ({'state':'creation_unknown'},'requires_requested_removal'),
                            ({'provider':'lium'},'targon_only')):
            with self.repo.transaction() as connection:
                connection.execute(update(instance_intents).values(**values))
            with self.assertRaisesRegex(OperatorError,code):
                self.service.manual_review(self.actor,self.node_id,self.body_now(),'invalid-state')
            with self.repo.transaction() as connection:
                connection.execute(update(instance_intents).values(state='destroying',provider='targon'))
        with self.repo.transaction() as connection:
            connection.execute(update(scaler_actions).values(destroy_started_at=None))
        with self.assertRaisesRegex(OperatorError,'requires_requested_removal'):
            self.service.manual_review(self.actor,self.node_id,self.body_now(),'missing-stop')

    def test_old_or_stale_controller_cannot_receive_manual_review(self):
        for patch in ({'controller_id':'operator-offers-v1-old'}, {'observed_at':self.now-31}):
            with self.repo.transaction() as connection:
                connection.execute(update(operator_heartbeats).values(**patch))
            with self.assertRaisesRegex(OperatorError,'controller_unavailable'):
                self.service.manual_review(self.actor,self.node_id,self.body,'old-controller')
            with self.repo.transaction() as connection:
                connection.execute(update(operator_heartbeats).values(controller_id=self.controller.leader_id,
                    observed_at=self.now))

    def test_fresh_worker_or_unreleased_device_blocks_even_with_human_attestation(self):
        with self.repo.transaction() as connection:
            connection.execute(insert(registered_workers).values(id='bound-worker',pool=self.binding.pool,
                provider='targon',instance_id=self.uid,spec={},spec_hash='a'*64,state='ready',current_job_id=None,
                drain_requested=1,fence=1,expires_at=self.now+30,updated_at=self.now))
        # State DTO needs only the existing safe worker spec projection.
        with self.assertRaisesRegex(OperatorError,'worker_active'):
            self.service.manual_review(self.actor,self.node_id,self.body,'active-worker')
        with self.repo.transaction() as connection:
            connection.execute(update(registered_workers).values(state='retired',expires_at=self.now-1))
            connection.execute(insert(registered_devices).values(provider='targon',instance_id=self.uid,
                gpu_id='GPU-old',worker_id='bound-worker',state='reserved'))
        with self.assertRaisesRegex(OperatorError,'device_unreleased'):
            self.service.manual_review(self.actor,self.node_id,self.body,'device-held')

    def test_unsafe_attempt_blocks_and_late_provider_cost_cannot_settle_manual_review(self):
        job=self.job(status='queued')
        with self.repo.transaction() as connection:
            connection.execute(insert(registered_workers).values(id='expired-worker',pool=self.binding.pool,
                provider='targon',instance_id=self.uid,spec={},spec_hash='a'*64,state='retired',current_job_id=None,
                drain_requested=1,fence=1,expires_at=self.now-30,updated_at=self.now))
            connection.execute(update(jobs).where(jobs.c.id==job['id']).values(status='failed'))
            connection.execute(insert(attempts).values(id='attempt-offline',job_id=job['id'],number=1,
                status='failed',fence=1,worker_id='expired-worker',created_at=self.now,updated_at=self.now,
                submission_started_at=self.now-1,upstream_task_id='original-upstream',upstream_stopped=0))
        with self.assertRaisesRegex(OperatorError,'attempt_unsafe'):
            self.service.manual_review(self.actor,self.node_id,self.body,'unsafe-attempt')
        with self.repo.transaction() as connection:
            connection.execute(update(attempts).values(upstream_stopped=1))
        self.service.manual_review(self.actor,self.node_id,self.body,'safe-reviewed')
        coordinator=self.controller._coordinator(self.binding)
        lease=coordinator.acquire(self.binding.pool,self.controller.leader_id)
        coordinator._apply(lease,self.node_id,ProviderFact('destroyed',instance_id=self.uid,
            actual_cost_microusd=540000),self.now)
        instance=self.repo.list_instance_intents()[0]
        self.assertEqual(instance['state'],'destroying')
        self.assertIsNone(instance['actual_cost_microusd'])
        self.assertEqual(instance['billing_status'],'pending')

    def test_reviewed_inactive_exclusion_applies_to_global_and_pool_reservation_barriers(self):
        self.repo.configure_capacity(max_instances=1,max_physical_gpus=1)
        self.repo.configure_pool(self.binding.pool,max_instances=1,max_physical_gpus=1)
        self.service.manual_review(self.actor,self.node_id,self.body,'capacity-review')
        original=self.repo.list_instance_intents()[0]
        next_intent=self.repo.reserve_instance_intent(self.scope,self.binding.pool,'new-distinct-intent',
            physical_gpus=1,slots=1,reserved_cost_microusd=1000000,hard_deadline=self.now+2000,
            budget_account_ids=('owner-budget',),provider='lium',dry_run=False)
        self.assertTrue(next_intent['created'])
        self.assertEqual(self.repo.list_instance_intents()[0]['reserved_cost_microusd'],original['reserved_cost_microusd'])

    def test_postgres_guardian_reader_executes_exact_audit_query(self):
        if self.repo.engine.dialect.name != 'postgresql':
            self.skipTest('requires explicitly configured local test PostgreSQL')
        self.service.manual_review(self.actor,self.node_id,self.body,'pg-reader-review')
        container='a'*64
        queries=[]
        def run(arguments,**options):
            if arguments[1]=='inspect':
                return SimpleNamespace(returncode=0,stdout=json.dumps(container)+' '+json.dumps({
                    'com.docker.compose.project':'sixnine-platform','com.docker.compose.service':'db'}))
            wrapper=options['input']
            self.assertTrue(wrapper.startswith('BEGIN READ ONLY;\n') and wrapper.endswith('\nCOMMIT;'))
            query=wrapper.removeprefix('BEGIN READ ONLY;\n').removesuffix('\nCOMMIT;')
            queries.append(query)
            with self.repo.engine.begin() as connection:
                connection.execute(text('SET TRANSACTION READ ONLY'))
                value=connection.exec_driver_sql(query).scalar_one_or_none()
            return SimpleNamespace(returncode=0,stdout=(value or '')+'\n')
        reader=OperatorManualReviewReader(container,run=run)
        review=reader(self.uid)
        self.assertEqual(review['source'],'operator_database')
        self.assertEqual(review['state'],'manually_reviewed')
        self.assertEqual(review['instance_id'],self.uid)
        self.assertEqual(review['intent_id'],self.node_id)
        self.assertEqual(review['actor'],'superdan')
        self.assertIs(review['account_absent'],True)
        self.assertIs(review['no_continuing_charge'],True)
        self.assertIsNone(reader('wrk-other-not-reviewed'))
        with self.repo.engine.connect() as connection:
            command=connection.execute(select(operator_commands).where(
                operator_commands.c.id==review['operation_id'])).mappings().one()
        for field in ('account_absent','no_continuing_charge'):
            with self.repo.transaction() as connection:
                connection.execute(update(operator_commands).where(operator_commands.c.id==command['id']).values(
                    payload={**command['payload'],field:'true'}))
            self.assertIsNone(reader(self.uid))  # A JSON string cannot attest the boolean fact.
        self.assertEqual(len(queries),4)


class GuardianReviewTests(guardian_fixtures.TargonCleanupTests):
    # Reuse fixtures only, without rerunning their unrelated inherited cases.
    def guardian_with_review(self, reader):
        guardian=self.guardian()
        guardian.manual_review_reader=reader
        return guardian

    def review(self):
        return {'schema_version':1,'state':'manually_reviewed','intent_id':'1'*8+'-'+ '1'*4+'-'+ '1'*4+'-'+ '1'*4+'-'+ '1'*12,
            'operation_id':'2'*8+'-'+ '2'*4+'-'+ '2'*4+'-'+ '2'*4+'-'+ '2'*12,
            'actor':'superdan','instance_id':self.uid,'deadline':self.deadline,
            'account_absent':True,'no_continuing_charge':True,'observed_at':self.now}

    def test_manual_audit_stops_only_exact_uid_preserves_original_and_not_removal_proof(self):
        self.arm();self.now=self.deadline;self.guardian().tick()
        path=self.root/'receipts'/(self.uid+'.json')
        previous=guardian_fixtures._read(path)
        self.client.get.reset_mock();self.client.delete.reset_mock()
        self.guardian_with_review(lambda uid:self.review()).tick()
        accepted=guardian_fixtures._read(path)
        self.assertEqual(accepted['state'],'manually_reviewed')
        for field in ('request','request_hash','deadline','workload_identity','observed_at','delete_started_at','delete_attempts'):
            self.assertEqual(accepted[field],previous[field])
        self.client.get.assert_not_called();self.client.delete.assert_not_called()
        self.now+=600
        self.guardian().tick()  # Persisted root-owned audit survives removal of the reader/restart.
        self.client.get.assert_not_called();self.client.delete.assert_not_called()
        with self.assertRaisesRegex(ValueError,'removal_unconfirmed'): self.guard.removal_proof(self.uid)
        with self.assertRaisesRegex(ValueError,'proof_unavailable'): self.guard.proof(self.uid)

    def test_invalid_audit_or_db_loss_never_suspends_deadline_cleanup(self):
        for reader in (lambda uid:{**self.review(),'instance_id':'wrong-uid'},
                       Mock(side_effect=RuntimeError('db-offline'))):
            with self.subTest(reader=reader):
                self.arm();self.now=self.deadline
                self.client.get.reset_mock();self.client.delete.reset_mock()
                self.guardian_with_review(reader).tick()
                self.client.get.assert_called_once();self.client.delete.assert_called_once()
                self.assertNotEqual(guardian_fixtures._read(self.root/'receipts'/(self.uid+'.json'))['state'],'manually_reviewed')
                self.now+=60;self.deadline=self.now+120
                (self.root/'requests'/(self.uid+'.json')).unlink()
                (self.root/'receipts'/(self.uid+'.json')).unlink()

    def test_root_exception_requires_original_request_and_workload_hash(self):
        self.arm();self.now=self.deadline;self.guardian().tick()
        path=self.root/'receipts'/(self.uid+'.json')
        previous=guardian_fixtures._read(path)
        record={**self.review(),'source':'protected_operator_attestation','accepted_delete':True,
            'intent_id':None,
            'request_hash':previous['request_hash'],'workload_identity_hash':_hash(previous['workload_identity'])}
        self.now+=60
        self.client.get.reset_mock();self.client.delete.reset_mock()
        self.guardian_with_review(lambda uid:{**record,'workload_identity_hash':'0'*64}).tick()
        self.client.get.assert_called_once()
        self.assertNotEqual(guardian_fixtures._read(path)['state'],'manually_reviewed')
        self.client.get.reset_mock();self.client.delete.reset_mock()
        self.guardian_with_review(lambda uid:record).tick()
        self.client.get.assert_not_called();self.client.delete.assert_not_called()
        self.assertEqual(guardian_fixtures._read(path)['manual_review']['source'],'protected_operator_attestation')


# unittest normally collects inherited fixture cases. Keep this bounded new slice.
for _name in dir(guardian_fixtures.TargonCleanupTests):
    if _name.startswith('test_'):
        setattr(GuardianReviewTests,_name,None)
for _name in dir(operator_fixtures.OperatorTests):
    if _name.startswith('test_'):
        setattr(ManualReviewTests,_name,None)


class ReviewReaderTests(unittest.TestCase):
    def test_fixed_read_only_query_bound_to_verified_db_container_and_uid(self):
        container='a'*64;calls=[]
        def run(args,**options):
            calls.append((args,options))
            if args[1]=='inspect':
                return SimpleNamespace(returncode=0,stdout=json.dumps(container)+' '+json.dumps({
                    'com.docker.compose.project':'sixnine-platform','com.docker.compose.service':'db'}))
            return SimpleNamespace(returncode=0,stdout='{"state":"manually_reviewed"}\n')
        reader=OperatorManualReviewReader(container,run=run)
        self.assertEqual(reader('wrk-offline')['state'],'manually_reviewed')
        self.assertIn('BEGIN READ ONLY;',calls[1][1]['input'])
        self.assertIn("i.provider_instance_id='wrk-offline'",calls[1][1]['input'])
        self.assertIn("s.kind='stop'",calls[1][1]['input'])
        self.assertIn('platform_attempts',calls[1][1]['input'])
        self.assertNotIn('sh',calls[1][0])
        with self.assertRaisesRegex(ValueError,'identity_invalid'): reader("unsafe' OR true")
        with self.assertRaisesRegex(ValueError,'container_invalid'): OperatorManualReviewReader('mutable-name')

    def test_protected_root_exception_checks_trust_before_read_and_exact_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);record={'source':'protected_operator_attestation','instance_id':'wrk-exact'}
            (root/'wrk-exact.json').write_text(json.dumps(record))
            fallback=Mock(return_value=None)
            reader=ProtectedManualReviewReader(root,fallback)
            with patch('studio_platform.targon_cleanup._receipt_trust',side_effect=ValueError('untrusted')):
                with self.assertRaisesRegex(ValueError,'untrusted'): reader('wrk-exact')
            with patch('studio_platform.targon_cleanup._receipt_trust') as trust:
                self.assertEqual(reader('wrk-exact'),record)
                self.assertEqual(trust.call_args.args[0][-1],root/'wrk-exact.json')
            fallback.assert_not_called()
            self.assertIsNone(reader('wrk-other'));fallback.assert_called_once_with('wrk-other')


if __name__=='__main__': unittest.main()
