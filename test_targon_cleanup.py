"""Independent cleanup guard contracts using local fixtures and fake HTTP."""
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from studio_platform.targon_cleanup import (TargonCleanupGuard, TargonDeadlineGuardian,
    _read, _write, _receipt_trust)


class TargonCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="h3-guardian-test-")
        self.root = Path(self.temp.name)
        (self.root/"requests").mkdir()
        (self.root/"receipts").mkdir()
        self.now = 1800000000.0
        self.start, self.end, self.deadline = self.now-100, self.now+3600, self.now+120
        self.uid = "wkl-fixture"
        self.body = {"uid":self.uid,"name":"1"*32,"type":"VM","image":"ubuntu-fixture",
            "resource":{"name":"rtx6000b-small"},"state":{"status":"running"},
            "created_at":datetime.fromtimestamp(self.now,timezone.utc).isoformat()}
        self.get_status = 200
        self.client = Mock()
        self.client.get.side_effect = lambda route:httpx.Response(self.get_status,json=self.body)
        self.client.delete.return_value = httpx.Response(204)
        self.guard = TargonCleanupGuard(self.root,clock=lambda:self.now,wait_seconds=0)
        # Tests attest fake fixtures explicitly; production must verify root ACLs.
        self.trust = patch("studio_platform.targon_cleanup._receipt_trust")
        self.trust.start()
        self.addCleanup(self.trust.stop)

    def tearDown(self):
        self.temp.cleanup()

    def guardian(self):
        return TargonDeadlineGuardian(self.root,self.client,org_slug="fixture-org",
            resource_names=["rtx6000b-small"],image_names=["ubuntu-fixture"],
            approval_start=self.start,approval_end=self.end,maximum_seconds=600,clock=lambda:self.now)

    def arm(self):
        with self.assertRaisesRegex(ValueError,"ack_pending"):
            self.guard.arm(self.uid,self.deadline)
        self.guardian().tick()
        self.guard.arm(self.uid,self.deadline)

    def test_exact_acknowledgement_no_extension_and_freshness(self):
        self.arm()
        self.assertEqual(self.guard.proof(self.uid),
            {"instance_id":self.uid,"deadline":self.deadline,"independent":True,"armed":True})
        with self.assertRaisesRegex(ValueError,"immutable"):
            self.guard.arm(self.uid,self.deadline+1)
        self.now += 46
        with self.assertRaisesRegex(ValueError,"proof_unavailable"):
            self.guard.proof(self.uid)
        self.guardian().tick()
        self.assertTrue(self.guard.proof(self.uid)["armed"])
        self.client.delete.assert_not_called()

    def test_deadline_delete_and_restart_never_repeat_mutation(self):
        self.arm()
        self.now = self.deadline
        self.guardian().tick()
        self.client.delete.assert_called_once()
        self.get_status = 404
        self.guardian().tick()
        proof = self.guard.removal_proof(self.uid)
        self.assertTrue(proof["removed"])
        self.assertEqual(proof["evidence"],"exact_uid_404_after_delete_ack")
        self.guardian().tick()
        self.client.delete.assert_called_once()

    def test_disappearance_without_delete_ack_is_unknown(self):
        self.arm()
        self.get_status = 404
        self.guardian().tick()
        with self.assertRaisesRegex(ValueError,"removal_unconfirmed"):
            self.guard.removal_proof(self.uid)
        self.assertEqual(_read(self.root/"receipts"/(self.uid+".json"))["state"],"armed")
        self.client.delete.assert_not_called()

    def test_lost_delete_reconciles_without_retry_or_false_absence(self):
        self.arm()
        self.now = self.deadline
        self.client.delete.side_effect = httpx.ReadTimeout("fake failure")
        self.guardian().tick()
        self.get_status = 404
        self.guardian().tick()
        with self.assertRaisesRegex(ValueError,"removal_unconfirmed"):
            self.guard.removal_proof(self.uid)
        self.client.delete.assert_called_once()
        self.get_status = 200
        self.body["state"]["status"] = "deleted"
        self.guardian().tick()
        self.assertEqual(self.guard.removal_proof(self.uid)["evidence"],"exact_uid_deleted")

    def test_foreign_workload_or_creation_outside_approval_is_not_armed(self):
        with self.assertRaises(ValueError):
            self.guard.arm(self.uid,self.deadline)
        original = self.body.copy()
        for changes in ({"name":"unrelated"},{"image":"another"},
                {"created_at":datetime.fromtimestamp(self.start-1,timezone.utc).isoformat()}):
            with self.subTest(changes=changes):
                self.body = {**original,**changes}
                self.guardian().tick()
                self.assertFalse((self.root/"receipts"/(self.uid+".json")).exists())
        self.client.delete.assert_not_called()

    def test_malformed_other_request_does_not_disable_existing_cleanup(self):
        self.arm()
        (self.root/"requests"/"aaa-invalid.json").write_text("not json")
        self.now = self.deadline
        self.guardian().tick()
        self.client.delete.assert_called_once()

    def test_transient_delete_retries_only_after_backoff_and_exact_fresh_get(self):
        self.arm()
        self.now = self.deadline
        self.client.delete.side_effect = [httpx.Response(503),httpx.Response(204)]
        self.guardian().tick()
        self.assertEqual(_read(self.root/"heartbeat.json")["state"],"degraded")
        self.now += 29
        self.guardian().tick()
        self.client.delete.assert_called_once()
        self.now += 1
        self.guardian().tick()
        self.assertEqual(self.client.delete.call_count,2)
        self.get_status = 404
        self.guardian().tick()
        self.assertTrue(self.guard.removal_proof(self.uid)["removed"])

    def test_delete_retry_exhaustion_is_explicitly_blocked(self):
        self.arm()
        self.now = self.deadline
        self.client.delete.return_value = httpx.Response(429)
        for _ in range(12):
            self.guardian().tick()
            receipt = _read(self.root/"receipts"/(self.uid+".json"))
            self.now = receipt["next_retry_at"]
        self.guardian().tick()
        self.assertEqual(self.client.delete.call_count,12)
        self.assertEqual(_read(self.root/"receipts"/(self.uid+".json"))["state"],"cleanup_blocked")
        self.assertEqual(_read(self.root/"heartbeat.json")["state"],"degraded")

    def test_tampered_request_or_receipt_cannot_prove_guard_or_removal(self):
        self.arm()
        request_path = self.root/"requests"/(self.uid+".json")
        value = _read(request_path)
        _write(request_path,{**value,"deadline":self.deadline+1})
        with self.assertRaisesRegex(ValueError,"proof_unavailable"):
            self.guard.proof(self.uid)
        with self.assertRaisesRegex(ValueError,"removal_unconfirmed"):
            self.guard.removal_proof(self.uid)

    def test_missing_request_cannot_drop_armed_cleanup_after_guardian_restart(self):
        self.arm()
        request_path = self.root/"requests"/(self.uid+".json")
        original = _read(request_path)
        request_path.unlink()
        self.now = self.deadline
        self.guardian().tick()
        self.client.delete.assert_called_once_with('/tha/v3/orgs/fixture-org/workloads/'+self.uid)
        receipt = _read(self.root/"receipts"/(self.uid+".json"))
        self.assertEqual(receipt["request"],original)
        self.assertEqual(receipt["deadline"],self.deadline)
        self.get_status = 404
        self.guardian().tick()
        self.assertEqual(self.guard.removal_proof(self.uid)["deadline"],self.deadline)
        self.guardian().tick()
        self.client.delete.assert_called_once()

    def test_changed_request_cannot_extend_deadline_or_retarget_armed_workload(self):
        self.arm()
        request_path = self.root/"requests"/(self.uid+".json")
        original = _read(request_path)
        _write(request_path,{**original,"instance_id":"another-workload","deadline":self.deadline+9999})
        with self.assertRaisesRegex(ValueError,"proof_unavailable"):
            self.guard.proof(self.uid)
        self.now = self.deadline
        self.guardian().tick()
        self.client.delete.assert_called_once_with('/tha/v3/orgs/fixture-org/workloads/'+self.uid)
        receipt = _read(self.root/"receipts"/(self.uid+".json"))
        self.assertEqual(receipt["request"],original)
        self.assertEqual(receipt["workload_identity"]["name"],self.body["name"])
        self.get_status = 404
        self.guardian().tick()
        self.assertTrue(self.guard.removal_proof(self.uid)["removed"])

    def test_malformed_request_cannot_disable_retained_cleanup(self):
        self.arm()
        (self.root/"requests"/(self.uid+".json")).write_text('not json')
        self.now = self.deadline
        self.guardian().tick()
        self.client.delete.assert_called_once_with('/tha/v3/orgs/fixture-org/workloads/'+self.uid)

    def test_retained_provider_identity_must_still_match_before_delete(self):
        self.arm()
        self.now = self.deadline
        # Same UID and allowed resource alone cannot substitute another original
        # workload identity, even if the provider unexpectedly changes its name.
        self.body["name"] = "2"*32
        self.guardian().tick()
        self.client.delete.assert_not_called()
        self.assertEqual(_read(self.root/"heartbeat.json")["state"],"degraded")

    def test_other_org_cannot_turn_acknowledged_delete_into_false_absence_proof(self):
        self.arm()
        self.now = self.deadline
        self.guardian().tick()
        self.client.delete.assert_called_once()
        self.get_status = 404
        self.client.get.reset_mock()
        other = TargonDeadlineGuardian(self.root,self.client,org_slug="another-org",
            resource_names=["rtx6000b-small"],image_names=["ubuntu-fixture"],
            approval_start=self.start,approval_end=self.end,maximum_seconds=600,clock=lambda:self.now)
        other.tick()
        self.client.get.assert_not_called()
        with self.assertRaisesRegex(ValueError,"removal_unconfirmed"):
            self.guard.removal_proof(self.uid)
        self.assertEqual(_read(self.root/"heartbeat.json")["state"],"degraded")

    def test_root_receipt_trust_rejects_controller_writable_or_nonroot_files(self):
        self.trust.stop()
        path = Mock()
        path.is_symlink.return_value = False
        with patch("studio_platform.targon_cleanup.os.name","posix"):
            for uid, mode in ((1000,stat.S_IFREG|0o640),(0,stat.S_IFREG|0o660)):
                with self.subTest(uid=uid,mode=mode):
                    path.lstat.return_value = SimpleNamespace(st_uid=uid,st_mode=mode)
                    with self.assertRaisesRegex(ValueError,"receipt_untrusted"):
                        _receipt_trust([path])
            path.lstat.return_value = SimpleNamespace(st_uid=0,st_mode=stat.S_IFREG|0o640)
            _receipt_trust([path])
        if os.name == "nt":
            with self.assertRaisesRegex(ValueError,"trust_unsupported"):
                _receipt_trust([path])


if __name__ == "__main__":
    unittest.main()
