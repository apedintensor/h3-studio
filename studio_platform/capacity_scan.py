"""Explicit GET-only inventory sampler, independent of the rental controller.

Run ``python -m studio_platform.capacity_scan --providers lium targon`` using
the existing process configuration. This does not start/stop GPU instances,
initialize budgets, refresh authorization or mark execution capacity ready.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread

from .capacity_market import PROVIDERS, publish_observation


class MarketScanner:
    def __init__(self, repo, *, providers=PROVIDERS, readers=None, interval=30):
        if not providers or len(set(providers)) != len(providers) or any(x not in PROVIDERS for x in providers):
            raise ValueError("inventory_provider_invalid")
        if type(interval) is not int or not 30 <= interval <= 90:
            raise ValueError("inventory_interval_invalid")
        self.repo, self.providers, self.interval = repo, tuple(providers), interval
        if readers is None:
            from .capacity_inventory import scan_lium, scan_targon
            readers = {"lium": lambda: scan_lium(clock=repo.clock), "targon": lambda: scan_targon(clock=repo.clock)}
        self.readers = readers
        self.stop_event, self.thread = Event(), None

    def _read(self, provider):
        try:
            observation = self.readers[provider]()
            if observation.get("provider") != provider:
                raise ValueError("inventory_provider_mismatch")
            return observation
        except Exception:
            return {"provider": provider, "status": "error", "observed_at": self.repo.clock(),
                    "reason_code": "inventory_scan_failed", "offers": []}

    def once(self):
        # Parallel suppliers: one timeout must not keep another result hidden.
        with ThreadPoolExecutor(max_workers=len(self.providers), thread_name_prefix="stock-read") as executor:
            futures = {provider: executor.submit(self._read, provider) for provider in self.providers}
            for provider, future in futures.items():
                observation = future.result()
                try:
                    publish_observation(self.repo, observation)
                except ValueError:
                    publish_observation(self.repo, {"provider": provider, "status": "error",
                        "observed_at": self.repo.clock(), "offers": []})

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.once()
            except Exception:
                # A DB outage leaves the existing timestamp stale; no false
                # success, credential-bearing exception, or rental retry.
                pass
            self.stop_event.wait(self.interval)

    def start(self):
        if self.thread is not None:
            raise ValueError("inventory_scanner_already_started")
        self.thread = Thread(target=self._run, name="capacity-stock", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--providers", nargs="+", choices=PROVIDERS, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    from .repository import Repository
    from .settings import Settings
    # Schema is created by the normal application migration path, not a reader.
    repo = Repository(Settings.from_environment().database_url)
    scanner = MarketScanner(repo, providers=args.providers)
    try:
        if args.once:
            scanner.once()
        else:
            scanner.start()
            while scanner.thread.is_alive():
                scanner.thread.join(timeout=1)
    except KeyboardInterrupt:
        pass
    finally:
        scanner.stop()
        repo.close()


if __name__ == "__main__":
    main()
