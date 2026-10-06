"""SQL-only stale assistant reconciliation; never resubmits an upstream call."""
from __future__ import annotations

import asyncio
from starlette.concurrency import run_in_threadpool


class QuickChatRecovery:
    def __init__(self, service, *, older_than_s=180, interval_s=30):
        # The bounded supplier request has a 120-second timeout. Recovery must
        # not fence another API process's fresh request during a rolling start.
        if older_than_s < 180 or not 1 <= interval_s <= 300:
            raise ValueError("invalid_assistant_recovery_window")
        self.service, self.older_than_s, self.interval_s = service, older_than_s, interval_s
        self.stop = asyncio.Event()
        self.task = None
        self.last_count = 0
        self.last_error_code = None

    async def reconcile(self):
        try:
            self.last_count = await run_in_threadpool(self.service.recover_assistant_runs,
                                                      older_than_s=self.older_than_s)
            self.last_error_code = None
        except Exception:
            # Database/driver exception text may contain private values.
            # No supplier request or automatic creation/retry occurs here.
            self.last_error_code = "quick_chat_recovery_failed"

    async def _run(self):
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                await self.reconcile()

    async def start(self):
        await self.reconcile()
        self.task = asyncio.create_task(self._run())

    async def close(self):
        self.stop.set()
        if self.task is not None:
            await self.task

