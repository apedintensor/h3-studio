import json
from pathlib import Path
import tempfile
import unittest

from studio_platform.runtime_catalog import (PROFILE_IDS, engine_manifest, get_profile,
    public_catalog, runtime_config, timing_hint, validate_manifest)
from studio_platform.inference.wangp_contract import EngineManifest
from studio_platform.runtime_hosts.wangp_download import selected_files


class RuntimeCatalogTests(unittest.TestCase):
    def test_catalog_is_detached_public_and_unqualified(self):
        catalog = public_catalog()
        self.assertEqual(len(catalog['profiles']), 3)
        for profile in catalog['profiles']:
            self.assertNotIn('runtime', profile)
            self.assertIs(profile['validation']['production_adapter_verified'], False)
            self.assertEqual({v['mode'] for v in profile['models']}, {'fl', 'ref'})
        catalog['profiles'][0]['components'].clear()
        self.assertTrue(get_profile(PROFILE_IDS[0])['components'])
        rendered = json.dumps(catalog)
        self.assertNotIn('C:/', rendered)
        self.assertNotIn('/workspace', rendered)
        self.assertNotIn('prompt', rendered.lower())

    def test_exact_joint_cases_do_not_interpolate_or_claim_cold_cache(self):
        profile = PROFILE_IDS[1]
        hint = timing_hint(profile, 'ref', 832, 480, 124, 24, 20, ['video', 'audio', 'image'])
        self.assertAlmostEqual(hint['cases'][0]['measurements'][0]['total_seconds'], 151.9501322209835)
        self.assertIsNone(hint['estimated_seconds'])
        for args in [('ref', 1344, 768, 124, 24, 50, ['video','audio','image']),
                     ('ref', 832, 480, 124, 24, 20, ['image']),
                     ('ref', 832, 480, 120, 24, 20, ['video','audio','image']),
                     ('ref', 832, 480, 124, 24, True, ['video','audio','image'])]:
            self.assertIsNone(timing_hint(profile, *args))
        for profile in public_catalog()['profiles']:
            for case in profile['verified_cases']:
                for sample in case['measurements']:
                    self.assertEqual(sample['cache_state'], 'not_controlled')
                    self.assertEqual(sample['sample_count'], 1)
                    self.assertIn('/blob/ff2fa85678e77ba195380b148acc23a4c47ef347/', sample['evidence'])

    def test_failed_bf16_and_trimmed_reference_samples_are_excluded(self):
        self.assertEqual([len(get_profile(p)['verified_cases']) for p in PROFILE_IDS], [8, 7, 7])
        self.assertEqual(get_profile(PROFILE_IDS[2])['runtime']['task_config'], 'bf16,bf16')
        self.assertTrue(get_profile(PROFILE_IDS[2])['runtime']['qkv_splitting'])
        self.assertEqual(get_profile(PROFILE_IDS[0])['model_id'], 'MiniMax-H3-Pruned-Rank8-INT8')

    def test_one_mode_manifest_binds_exact_assets_and_runtime(self):
        for profile_id in PROFILE_IDS:
            for mode in ('fl', 'ref'):
                manifest = engine_manifest(profile_id, mode)
                self.assertEqual(validate_manifest(manifest)['id'], profile_id)
                files = selected_files(manifest.document, manifest.digest)
                self.assertEqual(len(files), 10)
                self.assertTrue(all(('FL2VA' if mode == 'fl' else 'Ref2VA') in v[3]
                                    for v in files if v[0] == 'transformer'))
                self.assertFalse(manifest.document['inference_qualified'])
                changed = manifest.document
                changed['runtime_profile']['task_config'] += ',lower_ram'
                with self.assertRaisesRegex(ValueError, 'profile_manifest_mismatch'):
                    validate_manifest(EngineManifest.from_dict(changed))

    def test_unknown_profile_and_non_absolute_model_root_are_rejected(self):
        with self.assertRaises(ValueError):
            get_profile('../manifest')
        with self.assertRaises(ValueError):
            runtime_config(PROFILE_IDS[0], 'relative')
        with tempfile.TemporaryDirectory() as directory:
            config = runtime_config(PROFILE_IDS[2], directory)
            self.assertEqual(config['profile'], 3)
            self.assertEqual(config['transformer_quantization'], 'bf16')
            self.assertEqual(config['checkpoints_paths'], [str(Path(directory).resolve())])


if __name__ == '__main__':
    unittest.main()
