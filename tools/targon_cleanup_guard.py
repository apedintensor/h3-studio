"""Run the independent Targon deadline guardian; no launch or generation authority."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import re
import subprocess
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


class OperatorManualReviewReader:
    """Root host bridge: read committed exact-ID operator audits, never write DB.

    No connection credentials leave the existing database container. This fixed
    query is opt-in through the protected host service, not browser parameters.
    """
    def __init__(self, container, *, database_name='postgres', run=subprocess.run):
        if not isinstance(container,str) or not re.fullmatch(r'[0-9a-f]{64}',container):
            raise ValueError('targon_guard_database_container_invalid')
        if (not isinstance(database_name,str)
                or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,62}',database_name)):
            raise ValueError('targon_guard_database_name_invalid')
        self.container,self.database_name,self.run=container,database_name,run

    def __call__(self, uid):
        if not isinstance(uid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',uid):
            raise ValueError('targon_guard_identity_invalid')
        identity=self.run(['/usr/bin/docker','inspect','--format',
            '{{json .Id}} {{json .Config.Labels}}',self.container],text=True,capture_output=True,
            timeout=15,check=False)
        try:
            found,labels_raw=identity.stdout.strip().split(' ',1)
            labels=json.loads(labels_raw)
            if (identity.returncode or json.loads(found)!=self.container
                    or labels.get('com.docker.compose.project')!='sixnine-platform'
                    or labels.get('com.docker.compose.service')!='db'):
                raise ValueError
        except (ValueError,AttributeError):
            raise ValueError('targon_guard_database_identity_unconfirmed') from None
        query="""SELECT (CAST(r.facts AS jsonb) || jsonb_build_object('observed_at',r.observed_at))::text
FROM platform_scaler_receipts r
JOIN platform_instance_intents i ON i.id=r.intent_id
JOIN platform_operator_capacity_nodes n ON n.intent_id=i.id
JOIN platform_operator_capacity_commands c ON c.id=(CAST(r.facts AS jsonb)->>'operation_id')
JOIN platform_operator_capacity_commands s ON s.id=(CAST(r.facts AS jsonb)->>'stop_operation_id')
JOIN platform_scaler_actions a ON a.intent_id=i.id
WHERE r.operation='manual_review' AND i.provider='targon' AND i.state='destroying'
AND i.provider_instance_id='%s' AND n.desired_state='stopped'
AND a.destroy_started_at IS NOT NULL
AND c.kind='manual_review' AND c.state='completed' AND c.actor=(CAST(r.facts AS jsonb)->>'actor')
AND (CAST(c.payload AS jsonb)->>'node_id')=i.id
AND (CAST(c.payload AS jsonb)->>'provider_instance_id')=i.provider_instance_id
AND (CAST(c.payload AS jsonb)->'account_absent')='true'::jsonb
AND (CAST(c.payload AS jsonb)->'no_continuing_charge')='true'::jsonb
AND ((CAST(n.payload AS jsonb)->'manual_review')->>'operation_id')=c.id
AND (CAST(r.facts AS jsonb)->>'instance_id')=i.provider_instance_id
AND (CAST(r.facts AS jsonb)->>'intent_id')=i.id
AND c.created_at=r.observed_at
AND s.kind='stop' AND (CAST(s.payload AS jsonb)->>'node_id')=i.id AND s.created_at<=r.observed_at
AND NOT EXISTS (SELECT 1 FROM platform_registered_workers w WHERE w.provider=i.provider
AND w.instance_id=i.provider_instance_id AND (w.current_job_id IS NOT NULL
OR (w.state!='retired' AND w.expires_at>EXTRACT(EPOCH FROM NOW()))))
AND NOT EXISTS (SELECT 1 FROM platform_registered_devices d WHERE d.provider=i.provider
AND d.instance_id=i.provider_instance_id AND d.state!='released')
AND NOT EXISTS (SELECT 1 FROM platform_attempts t JOIN platform_jobs j ON j.id=t.job_id
JOIN platform_registered_workers w ON w.id=t.worker_id WHERE w.provider=i.provider
AND w.instance_id=i.provider_instance_id AND (j.status NOT IN ('succeeded','failed','cancelled')
OR t.status NOT IN ('succeeded','failed','cancelled') OR ((t.submission_started_at IS NOT NULL
OR t.upstream_task_id IS NOT NULL) AND t.upstream_stopped!=1)))
ORDER BY r.observed_at DESC LIMIT 1;""" % uid
        result=self.run(['/usr/bin/docker','exec','--user','postgres','-i',self.container,
            'psql','-U','postgres','-d',self.database_name,'-X','-q','-A','-t','-v','ON_ERROR_STOP=1'],
            input='BEGIN READ ONLY;\n'+query+'\nCOMMIT;',text=True,capture_output=True,timeout=15,check=False)
        if result.returncode or len(result.stdout.encode('utf-8'))>16384:
            raise ValueError('targon_guard_manual_review_read_unavailable')
        raw=result.stdout.strip()
        return {**json.loads(raw),'source':'operator_database'} if raw else None


class ProtectedManualReviewReader:
    """Root-owned exception audit for exact historical standalone workloads."""
    def __init__(self, directory, fallback=None):
        self.root=Path(directory)
        self.fallback=fallback
        if not self.root.is_absolute() or '..' in self.root.parts:
            raise ValueError('targon_guard_manual_review_directory_invalid')

    def __call__(self, uid):
        from studio_platform.targon_cleanup import UID, _read, _receipt_trust
        if not isinstance(uid,str) or not UID.fullmatch(uid):
            raise ValueError('targon_guard_identity_invalid')
        path=self.root/(uid+'.json')
        if path.exists():
            _receipt_trust((self.root,*self.root.parents,path))
            review=_read(path)
            if review.get('source')!='protected_operator_attestation':
                raise ValueError('targon_guard_manual_review_source_invalid')
            return review
        return self.fallback(uid) if self.fallback else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--manual-review-database-container')
    parser.add_argument('--manual-review-database-name',help='Verified nonsecret application database name; defaults to postgres')
    parser.add_argument('--manual-review-directory')
    args = parser.parse_args(argv)
    from studio_platform.targon_cleanup import TargonDeadlineGuardian, _read, _write
    from studio_platform.targon_runtime_aws import AwsTargonLoader, SERVICE, PROFILE, BASE_URL
    try:
        if args.manual_review_database_name is not None and args.manual_review_database_container is None:
            raise ValueError('targon_guard_database_container_required')
        review_reader=None
        if args.manual_review_database_container is not None:
            database_name=args.manual_review_database_name if args.manual_review_database_name is not None else 'postgres'
            review_reader=OperatorManualReviewReader(args.manual_review_database_container,database_name=database_name)
        if args.manual_review_directory:
            review_reader=ProtectedManualReviewReader(args.manual_review_directory,review_reader)
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
            manual_review_reader=review_reader,
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
