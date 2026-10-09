"""Run the independent Targon deadline guardian; no launch or generation authority."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import sys
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@contextmanager
def _wall_timeout(seconds):
    """Bound the entire host-main-thread request, including a dripping body."""
    def expired(*_):
        raise TimeoutError('targon_guard_request_timeout')
    if signal.getitimer(signal.ITIMER_REAL)[0]:
        raise ValueError('targon_guard_timer_conflict')
    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class ProviderHTTP:
    """Host stdlib transport; fixed origin, no redirects or proxy credentials."""
    def __init__(self, api_key):
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self._opener = build_opener(ProxyHandler({}), NoRedirect())
        self._key = api_key

    def request(self, method, path):
        import re
        if not re.fullmatch(r'/tha/v3/orgs/[a-z0-9-]+/workloads/[A-Za-z0-9_-]+', path):
            raise ValueError('targon_guard_route_invalid')
        request = Request('https://api.targon.com'+path, method=method,
                          headers={'Authorization':'Bearer '+self._key,'Accept':'application/json'})
        with _wall_timeout(15):
            try:
                response = self._opener.open(request, timeout=15)
            except HTTPError as error:
                response = error
            with response:
                raw = response.read(1024*1024+1)
                if len(raw)>1024*1024:
                    raise ValueError('targon_guard_response_limit')
                return SimpleNamespace(status_code=response.code, json=lambda:json.loads(raw))

    def get(self, path):
        return self.request('GET', path)

    def delete(self, path):
        return self.request('DELETE', path)

    def close(self):
        self._key = None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    from studio_platform.targon_cleanup import TargonDeadlineGuardian, _read, _write
    from studio_platform.targon_runtime_aws import AwsTargonLoader, SERVICE, PROFILE, BASE_URL
    try:
        path = Path(args.config)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError
        if os.name != 'nt' and (path.stat().st_uid != 0 or path.stat().st_mode & 0o022):
            raise ValueError
        config = _read(path)
        required = {'schema_version','directory','org_slug','resource_names','image_names',
                    'approval_start','approval_end','maximum_seconds','secret_arn','secret_version_id'}
        if set(config) != required or config['schema_version'] != 1:
            raise ValueError
        loader = AwsTargonLoader(config['secret_arn'], config['secret_version_id'])
        credential = loader(SERVICE, profile=PROFILE)
        client = ProviderHTTP(credential.api_key)
        guardian = TargonDeadlineGuardian(config['directory'], client,
            **{key:config[key] for key in ('org_slug','resource_names','image_names',
                                          'approval_start','approval_end','maximum_seconds')})
    except Exception:
        print('targon_guard_start_failed', flush=True)
        return 1
    try:
        while True:
            try:
                guardian.tick()
            except Exception:
                # Do not publish a fresh healthy heartbeat after a failed sweep.
                # systemd restarts on process exit; transient HTTP failures are
                # retried as exact-UID observations by the next sweep.
                print('targon_guard_sweep_incomplete', flush=True)
            time.sleep(3)
    finally:
        client.close()
        loader.close()


if __name__ == '__main__':
    raise SystemExit(main())
