"""Offline long-output admission bounds; no provider, SSH or GPU requests."""
from dataclasses import replace
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from sqlalchemy import update

from comfy_workflow import native_output_spec
from studio_platform.capabilities import compile_request
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.execution_policy import reservation_for_duration, validate_policy
from studio_platform.production_scaler import FiniteController, ScalerError, verify_policy
from studio_platform.qualification_profiles import (
    FL50_PROFILE, MULTIMODAL_PROFILE, MULTIMODAL_INPUT_LIMITS, QUEUED_TASK_PROFILE,
)
from studio_platform.repository import Repository, registered_workers, request_hash
from studio_platform.settings import Settings
from test_platform_api import generation_request
from test_platform_execution_policy import policy
from test_platform_production_scaler import FakeProvider, configuration


class LongDurationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = time.time()
        self.config = replace(configuration(self.root, self.now), qualification_profile=QUEUED_TASK_PROFILE)
        self.value = policy(self.now)
        self.value.update(pool=self.config.pool, configuration_id=self.config.configuration_id,
                          recipe_ids=list(self.config.recipe_ids))
        self.value['qualification'].update(status='runtime_required', profile=QUEUED_TASK_PROFILE,
            evidence_id=self.config.qualification_evidence_id)
        self.value['reservation']['expected_runtime_s'] = 1800
        self.value['envelope'].update(max_duration_seconds=362/24, max_reference_files=3,
            max_guides=1, allow_first_last=True, input_limits=copy.deepcopy(MULTIMODAL_INPUT_LIMITS))
        self.value['envelope']['controls'].update(encoder_device=['cpu'], video_decode=['tiled'],
                                                 ref_image_size=['max'])
        self.path = self.root / 'synthetic-policy.json'
        self.settings = Settings(self.root, auth_mode='local-test', generation_enabled=True,
            execution_backend='comfy-worker', execution_policy_file=self.path)

    def verify(self, value=None, config=None):
        value = self.value if value is None else value
        config = self.config if config is None else config
        self.path.write_text(json.dumps(value), encoding='utf-8')
        self.path.chmod(0o600)
        return verify_policy(replace(config, execution_policy_sha256=request_hash(value)), self.settings)

    def test_fourteen_seconds_uses_native_345_frames_inside_new_envelope(self):
        spec = native_output_spec({'duration': 14, 'resolution': '480P', 'aspect_ratio': '16:9'})
        self.assertEqual(spec['frames'], 345)
        self.assertEqual(spec['actual_duration'], 14.375)
        self.assertLessEqual(spec['actual_duration'], self.verify()['envelope']['max_duration_seconds'])

    def test_fifteen_seconds_requires_362_frame_padded_limit(self):
        spec = native_output_spec({'duration': 15, 'resolution': '768P', 'aspect_ratio': '16:9'})
        self.assertEqual(spec['frames'], 362)
        self.assertGreater(spec['actual_duration'], 15)
        self.assertEqual(spec['actual_duration'], self.verify()['envelope']['max_duration_seconds'])

    def test_new_profile_rejects_operator_limit_beyond_native_request_range(self):
        value = copy.deepcopy(self.value)
        value['envelope']['max_duration_seconds'] = 362/24 + .001
        with self.assertRaises(ScalerError):
            self.verify(value)
        with self.assertRaises(ValueError):
            native_output_spec({'duration': 16})

    def test_legacy_synthetic_profiles_keep_original_output_bound(self):
        for profile in (FL50_PROFILE, MULTIMODAL_PROFILE):
            config = replace(self.config, qualification_profile=profile)
            value = copy.deepcopy(self.value)
            value['recipe_ids'] = list(config.recipe_ids)
            value['qualification'].update(status='accepted' if profile == FL50_PROFILE else 'runtime_required')
            if profile == FL50_PROFILE:
                value['qualification'].pop('profile')
                value['envelope'].pop('input_limits')
                value['envelope'].update(max_reference_files=0, max_guides=0, allow_first_last=False)
            else:
                value['qualification']['profile'] = profile
            with self.subTest(profile=profile), self.assertRaises(ScalerError):
                self.verify(value, config)
            value['envelope']['max_duration_seconds'] = 6
            with self.subTest(original_bound=profile):
                self.assertEqual(self.verify(value, config), value)

    def test_real_task_profile_cannot_claim_already_accepted_qualification(self):
        for status in ('accepted', 'unverified'):
            value = copy.deepcopy(self.value)
            value['qualification']['status'] = status
            with self.subTest(status=status), self.assertRaises(ValueError):
                validate_policy(value)
            with self.subTest(verify_status=status), self.assertRaises(ValueError):
                self.verify(value)

    def test_output_extension_does_not_broaden_other_execution_limits(self):
        for field, changed in (('max_pixels', 1344*768+1), ('max_steps', 51),
                               ('max_reference_files', 4), ('max_guides', 2)):
            value = copy.deepcopy(self.value)
            value['envelope'][field] = changed
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify(value)
        value = copy.deepcopy(self.value)
        value['envelope']['controls']['video_decode'] = ['normal']
        with self.assertRaises(ScalerError):
            self.verify(value)
        value = copy.deepcopy(self.value)
        value['envelope']['input_limits']['max_videos'] = 2
        with self.assertRaises(ValueError):
            self.verify(value)

    def test_busy_long_job_slot_uses_admitted_allowance_and_safe_missing_binding_fallback(self):
        self.value['reservation']['duration_reference_seconds'] = 124/24
        self.verify()
        repo = Repository('sqlite:///' + (self.root/'synthetic-slots.sqlite3').as_posix(),
                          clock=lambda: self.now)
        self.addCleanup(repo.close)
        repo.create_schema()
        repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        repo.configure_pool(self.config.pool, max_instances=1, max_physical_gpus=1)
        control = WorkerControl(repo)
        worker_id = 'synthetic-long-worker'
        control.register(WorkerSpec(worker_id, self.config.pool, 'synthetic-provider', 'synthetic-instance',
            ('synthetic-gpu',), self.config.recipe_ids, self.value['model_id'], self.config.configuration_id))
        control.mark_ready(worker_id, upstream_idle_confirmed=True)
        request = generation_request()
        request['controls'].update(duration=14, video_decode='tiled', encoder_device='cpu')
        compiled, _ = compile_request(request, lambda _: None)
        quote = reservation_for_duration(self.value, compiled['output_spec']['actual_duration'])
        self.assertEqual(quote['expected_runtime_s'], 5009)
        execution = {'pool': self.config.pool, 'backend': 'comfy-worker', 'enabled': True,
            'configuration_id': self.config.configuration_id, 'policy_hash': request_hash(self.value),
            'expected_runtime_s': quote['expected_runtime_s']}
        plan = repo.create_plan(self.config.scope, compiled, execution, expires_at=self.now+600,
                                estimated_cost_microusd=0)
        job = repo.create_job(self.config.scope, plan['id'], 'synthetic-long-slot')
        self.assertEqual(control.claim(worker_id, self.config.pool).job['id'], job['id'])
        controller = FiniteController(repo, self.settings, self.config, provider=FakeProvider(lambda: self.now))
        instances = [{'provider_instance_id': 'synthetic-instance', 'state': 'ready'}]
        demands, slots = controller._observations(instances)
        self.assertEqual(demands, [])
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0].state, 'busy')
        self.assertEqual(slots[0].available_after_s, 5009)
        with repo.transaction() as connection:
            connection.execute(update(registered_workers).where(registered_workers.c.id == worker_id)
                .values(current_job_id='synthetic-missing-binding'))
        _, slots = controller._observations(instances)
        self.assertEqual(slots[0].available_after_s,
            reservation_for_duration(self.value, 362/24)['expected_runtime_s'])
        self.value['reservation'].pop('duration_reference_seconds')
        self.verify()
        _, slots = controller._observations(instances)
        self.assertEqual(slots[0].available_after_s, 1800)


if __name__ == '__main__':
    unittest.main()
