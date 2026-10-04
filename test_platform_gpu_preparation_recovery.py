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
        # CI intentionally runs as an unprivileged user. File ownership gates
        # are covered by the release tests; this fixture tests recovery state.
        self.stack.enter_context(patch.object(scaler, 'read_json',
            side_effect=lambda filename, maximum=65536: json.loads(Path(filename).read_text())))
        self.config = on_demand_configuration()
        self.config['capacity_approval_id'] = 'synthetic-approval'
        self.commit, self.old_commit = 'b'*40, 'a'*40
        self.state = {'version': 1, 'config_hash': scaler.fingerprint(self.config),
            'sequence': 3, 'created_at': self.config['created_at'], 'transfer_from': 'cycle-002'}
        self.receipt = {'version': 1, 'phase': 'staged', 'target_commit': self.commit,
            'old_commit': self.old_commit, 'config_hash': scaler.fingerprint(self.config),
            'supervisor': {'unit': 'sixnine-synthetic.service'},
            'next_service_state': self.state,
            'old_service_state': {**self.state, 'sequence': 2},
            'ledger': {'host_stage_confirmed': True, 'previous_sequence': 2, 'next_sequence': 3,
                'previous_approval_id': 'synthetic-approval-002', 'next_approval_id': 'synthetic-approval-003',
                'target_runtime_revision': self.commit, 'created_at': self.config['created_at'],
                'hard_deadline': self.config['hard_deadline'],
                'old_config_hash': scaler.fingerprint(self.config), 'target_config_hash': scaler.fingerprint(self.config),
                'restored_job_hashes': {'synthetic-job': 'd'*64}, 'jobs': [{'job_id': 'synthetic-job'}]}}
        proof = {'sequence': 2, 'job_ids': ['synthetic-job']}
        self.receipt['proof_sha256'] = self.receipt['ledger']['evidence_sha256'] = scaler.fingerprint(proof)
        scaler.atomic(recovery.path(recovery.PROOF), proof)
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

    def test_skipped_cycle_changed_approval_or_dropped_job_cannot_stage(self):
        for change in ({'next_sequence': 4}, {'previous_approval_id': 'other'},
                       {'restored_job_hashes': {}}, {'jobs': []}):
            with self.subTest(change=change):
                changed = copy.deepcopy(self.receipt)
                changed['ledger'].update(change)
                with self.assertRaises(release.ReleaseError):
                    recovery.validate_ledger_shape(changed, self.config)


if __name__ == '__main__':
    unittest.main()
