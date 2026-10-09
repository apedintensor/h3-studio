"""Pinned FL/REF rollout policy regression; local metadata only, no capacity."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from studio_platform.execution_policy import validate_policy
from studio_platform.execution_profiles import read_profiles
from studio_platform.runtime_catalog import get_profile, engine_manifest, timing_hint


FIXTURE = Path(__file__).parent/'test_fixtures/targon_execution_policies.json'


class TargonExecutionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policies = json.loads(FIXTURE.read_text(encoding='utf-8'))['policies']

    def test_rollout_policies_remain_runtime_required_after_partial_native_evidence(self):
        loaded = read_profiles(FIXTURE.resolve())
        self.assertEqual(len(loaded), 2)
        for mode, value in zip(('fl', 'ref'), self.policies):
            with self.subTest(mode=mode):
                profile = get_profile(value['deployment_profile_id'])
                self.assertEqual(len(profile['verified_cases']), 2)
                self.assertEqual(len(profile['qualification_cases']), 6)
                self.assertIs(validate_policy(value), value)
                self.assertEqual(value['pool'], 'op-targon-pruned-'+mode)
                self.assertEqual(value['configuration_id'], 'op-targon-pruned-'+mode)
                self.assertEqual(value['qualification']['status'], 'runtime_required')
                self.assertEqual(value['budget_accounts'], ['demand-job-total-20261004',
                    'demand-job-owner-{owner_id}-20261004'])
                self.assertEqual(loaded[(profile['id'],value['recipe_ids'][0])], value)
                manifest = engine_manifest(profile['id'], mode)
                self.assertEqual(value['engine_manifest_digest'], manifest.digest)
                self.assertIs(manifest.document['production_adapter_verified'], False)
                case = next(c for c in profile['qualification_cases'] if c['mode']==mode)
                self.assertIsNone(timing_hint(profile['id'],mode,case['width'],case['height'],
                    case['frames'],case['fps'],case['steps'],case['input_roles']))

    def test_candidate_cannot_broaden_envelope_or_claim_completed_qualification(self):
        for original in self.policies:
            for field, value in (('max_steps',51), ('max_pixels',1032193)):
                broken = copy.deepcopy(original)
                broken['envelope'][field] = value
                with self.assertRaises(ValueError): validate_policy(broken)
            broken = copy.deepcopy(original)
            broken['qualification']['status'] = 'accepted'
            with self.assertRaises(ValueError): validate_policy(broken)

    def test_candidate_requires_explicit_pending_metadata_without_measurements(self):
        value = self.policies[0]
        for change in ('scope','production_adapter_verified','measurements'):
            profile = get_profile(value['deployment_profile_id'])
            if change=='scope': profile['validation']['scope']='historical_hardware_verified'
            elif change=='production_adapter_verified': profile['validation'][change]=True
            else: profile['qualification_cases'][0]['measurements']=[]
            with patch('studio_platform.runtime_catalog.get_profile', return_value=profile):
                with self.assertRaisesRegex(ValueError, 'candidate qualification is not pending'):
                    validate_policy(value)

    def test_missing_mode_cases_fail_closed_without_empty_maximum(self):
        value = self.policies[0]
        profile = get_profile(value['deployment_profile_id'])
        profile['qualification_cases'] = [c for c in profile['qualification_cases'] if c['mode']=='ref']
        profile['verified_cases'] = [c for c in profile['verified_cases'] if c['mode']=='ref']
        with patch('studio_platform.runtime_catalog.get_profile', return_value=profile):
            with self.assertRaisesRegex(ValueError, '^Deployment profile envelope exceeds tested scope$'):
                validate_policy(value)


if __name__ == '__main__':
    unittest.main()
