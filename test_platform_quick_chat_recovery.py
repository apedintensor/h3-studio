"""Background reconciliation changes SQL only, with a safe stale threshold."""
import asyncio
import unittest
from unittest.mock import Mock

from studio_platform.quick_chat_recovery import QuickChatRecovery


class QuickChatRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_shutdown_and_explicit_tick_only_use_reconciliation(self):
        service = Mock()
        service.recover_assistant_runs.return_value = 2
        service.recover_title_runs.return_value = 0
        worker = QuickChatRecovery(service)
        await worker.start()
        self.assertEqual(worker.last_count, 2)
        service.recover_assistant_runs.assert_called_once_with(older_than_s=180)
        service.recover_title_runs.assert_called_once_with(older_than_s=180)
        service.recover_assistant_runs.return_value = 0
        await worker.reconcile()
        self.assertEqual(worker.last_count, 0)
        await asyncio.wait_for(worker.close(), timeout=1)
        self.assertTrue(worker.task.done())
        self.assertEqual([v[0] for v in service.method_calls], ["recover_assistant_runs", "recover_title_runs"] * 2)

    async def test_failed_sql_is_safe_and_next_tick_can_recover(self):
        service = Mock()
        service.recover_assistant_runs.side_effect = [RuntimeError("PRIVATE"), 1]
        service.recover_title_runs.return_value = 0
        worker = QuickChatRecovery(service)
        await worker.reconcile()
        self.assertEqual(worker.last_error_code, "quick_chat_recovery_failed")
        await worker.reconcile()
        self.assertIsNone(worker.last_error_code)
        self.assertEqual(worker.last_count, 1)

    def test_fresh_remote_calls_cannot_be_fenced_with_short_threshold(self):
        with self.assertRaises(ValueError):
            QuickChatRecovery(Mock(), older_than_s=1)


if __name__ == "__main__":
    unittest.main()
