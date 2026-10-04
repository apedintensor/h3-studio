"""Host recovery validation is offline; mocks never rent or launch."""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_platform_gpu_scaler import scaler, handoff, release, load, on_demand_configuration

with patch.dict(sys.modules, {'release': release, 'gpu_scaler': scaler, 'gpu_handoff': handoff}):
    recovery = load('test_preparation_host', 'gpu_preparation_recovery.py')


class PreparationHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'operator').mkdir()
        (self.root/'control').mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(scaler, 'ROOT', self.root))
        self.config = on_demand_configuration()
        self.commit, self.old_commit = 'b'*40, 'a'*40
        self.state = {'version': 1, 'config_hash': scaler.fingerprint(self.config),
            'sequence': 3, 'created_at': self.config['created_at'], 'transfer_from': 'cycle-002'}
        self.receipt = {'version': 1, 'phase': 'staged', 'target_commit': self.commit,
            'old_commit': self.old_commit, 'config_hash': scaler.fingerprint(self.config),
            'supervisor': {'unit': 'sixnine-synthetic.service'},
            'next_service_state': self.state, 'ledger': {'host_stage_confirmed': True}}
        scaler.atomic(recovery.path(recovery.RECEIPT), self.receipt)
        scaler.atomic(recovery.path(recovery.OLD), self.config)
        self.stack.enter_context(patch.object(recovery, 'runtime_json', return_value=self.state))
        self.stack.enter_context(patch.object(handoff, 'supervisor', return_value={'pid': 0}))
        self.no_controller = self.stack.enter_context(patch.object(scaler, 'require_new_controller'))
        self.stack.enter_context(patch.object(scaler, 'verify_marker', return_value={'active': False, 'admission': 'closed'}))
        self.core = self.stack.enter_context(patch.object(recovery, 'core', return_value={'verified': True}))

    def test_exact_staged_identity_releases_once_and_consumes_before_start(self):
        result = recovery.activate_resume(self.config, self.commit, {})
        self.assertEqual(result['phase'], 'activated')
        self.assertEqual(self.core.call_args.args[:2], (self.commit, 'release-leader'))
        self.assertEqual(self.core.call_args.kwargs, {'apply': True})
        with self.assertRaises(release.ReleaseError):
            recovery.activate_resume(self.config, self.commit, {})
        self.assertEqual(self.core.call_count, 2)  # verify + one release, no repeats.

    def test_other_commit_account_budget_or_state_cannot_resume(self):
        with self.assertRaises(release.ReleaseError):
            recovery.verify_resume(self.config, 'c'*40, {})
        for key, value in (('hard_deadline', self.config['hard_deadline']+1),
                           ('secret_version_id', 'different-profile-version'),
                           ('max_cycles', 7)):
            with self.subTest(key=key), self.assertRaises(release.ReleaseError):
                recovery.verify_resume({**self.config, key: value}, self.commit, {})
        with patch.object(recovery, 'runtime_json', return_value={**self.state, 'sequence': 1}):
            with self.assertRaises(release.ReleaseError):
                recovery.verify_resume(self.config, self.commit, {})
        self.core.assert_not_called()

    def test_active_supervisor_open_admission_or_drain_flag_blocks(self):
        with patch.object(handoff, 'supervisor', return_value={'pid': 22}):
            with self.assertRaises(release.ReleaseError):
                recovery.verify_resume(self.config, self.commit, {})
        with patch.object(scaler, 'verify_marker', return_value={'active': True, 'admission': 'open'}):
            with self.assertRaises(release.ReleaseError):
                recovery.verify_resume(self.config, self.commit, {})
        (self.root/'control'/'drain.flag').touch()
        with self.assertRaises(release.ReleaseError):
            recovery.verify_resume(self.config, self.commit, {})
        self.core.assert_not_called()

    def test_lost_release_result_never_launches_or_consumes_activation(self):
        self.core.side_effect = [ {'verified': True}, release.ReleaseError('synthetic_failure') ]
        with self.assertRaises(release.ReleaseError):
            recovery.activate_resume(self.config, self.commit, {})
        self.assertEqual(scaler.read_json(recovery.path(recovery.RECEIPT))['phase'], 'staged')


if __name__ == '__main__':
    unittest.main()
