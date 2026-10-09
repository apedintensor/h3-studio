#!/usr/bin/python3
"""Protected host launcher for public Targon stock sampling only.

Install root-owned at /opt/sixnine-release/targon_market.py. Prepare is local;
once/sample perform one public provider GET and a normalized cache update.
Only explicit start enables the timer. Never initializes schema or capacity.
"""
from contextlib import contextmanager
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, '/opt/sixnine-release')
import release

ROOT = Path('/srv/sixnine/targon-market')
HELPER = Path('/opt/sixnine-release/targon_market.py')
SERVICE = 'targon-market-scan'
UNIT = 'sixnine-targon-market.service'
TIMER = 'sixnine-targon-market.timer'
CONTAINER = 'sixnine-targon-market-scan'
LABEL = 'com.sixnine.targon-market.pin'
SYSTEMD = Path('/etc/systemd/system')
SYSTEMCTL = '/usr/bin/systemctl'

ONCE = '''import json,time
from sqlalchemy import select
from studio_platform.settings import Settings
from studio_platform.repository import Repository
from studio_platform.capacity_scan import MarketScanner
from studio_platform.capacity_market import market_inventory
started=time.time()
repo=None
try:
 repo=Repository(Settings.from_environment().database_url)
 MarketScanner(repo,providers=('targon',)).once()
 with repo.engine.connect() as connection:
  row=connection.execute(select(market_inventory).where(market_inventory.c.provider=='targon')).mappings().one()
 payload=row['payload']; observed=row['observed_at']; now=time.time()
 ok=payload['status']=='ok' and started<=observed<=now and now-observed<=120
 print(json.dumps({'provider':'targon','status':payload['status'],'observed_at':observed,'started_at':started,'finished_at':now,'fresh_success':ok,'offer_count':len(payload['offers'])}))
except Exception:
 print(json.dumps({'provider':'targon','status':'error','fresh_success':False,'error_code':'market_observation_failed'}))
finally:
 if repo is not None:repo.close()
'''


def require(condition, code):
    release.require(condition, code)


def read(path):
    return release._protected_json(path)


def atomic(path, value):
    temporary = path.with_suffix('.next')
    require(not temporary.exists() and not temporary.is_symlink(), 'market_pending_receipt_requires_review')
    with temporary.open('x', encoding='utf-8') as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, sort_keys=True, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())
    require(not path.is_symlink(), 'market_record_link_forbidden')
    temporary.replace(path)
    release.sync_directory(path.parent)


@contextmanager
def locked():
    import fcntl
    release.protected_directory(ROOT)
    path = ROOT / 'sampler.lock'
    require(not path.is_symlink(), 'market_lock_link_forbidden')
    with path.open('a') as lock:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise release.ReleaseError('market_action_in_progress') from None
        yield


def service(image):
    return {'image': image, 'pull_policy': 'never', 'user': '10001:10001', 'init': True,
        'restart': 'no', 'read_only': True, 'cap_drop': ['ALL'],
        'security_opt': ['no-new-privileges:true'], 'pids_limit': 64,
        'mem_limit': 256 * 1024**2, 'cpus': .25,
        'environment': {'SIXNINE_DATABASE_URL_FILE': '/run/secrets/app_database_url',
            'SIXNINE_DATA': '/tmp/unused', 'SIXNINE_GENERATION_ENABLED': '0',
            'SIXNINE_RENDER_ENABLED': '0', 'SIXNINE_CLOUD_CREATION_ENABLED': '0',
            'SIXNINE_EXECUTION_BACKEND': 'disabled', 'AWS_EC2_METADATA_DISABLED': 'true',
            'PGCONNECT_TIMEOUT': '10', 'PGOPTIONS': '-c statement_timeout=15000 -c lock_timeout=5000'},
        'command': ['python', '-c', ONCE],
        'secrets': [{'source': 'app_database_url', 'target': '/run/secrets/app_database_url'}],
        'tmpfs': ['/tmp:size=16777216,mode=1777'], 'networks': {'database': {}, 'edge': {}},
        'logging': {'driver': 'json-file', 'options': {'max-size': '1m', 'max-file': '1'}}}


def overlay(image):
    return {'services': {SERVICE: service(image)}}


def compose_args(directory, *arguments):
    return ['compose', '--project-directory', str(directory), '-f', str(directory / 'compose.yaml'),
            '-f', str(ROOT / 'overlay.json'), *arguments]


def unit_text():
    return f'''[Unit]
Description=Sixnine public Targon market observation (no rental authority)
After=docker.service network-online.target
Requires=docker.service
[Service]
Type=oneshot
User=root
ExecStart=/usr/bin/python3 {HELPER.as_posix()} sample
ExecStopPost=/usr/bin/python3 {HELPER.as_posix()} cleanup
TimeoutStartSec=100
TimeoutStopSec=30
KillMode=control-group
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths={ROOT.as_posix()}
PrivateTmp=true
MemoryMax=128M
StandardOutput=journal
StandardError=journal
'''


def timer_text():
    return f'''[Unit]
Description=Refresh Sixnine public Targon stock every 30 seconds
[Timer]
OnActiveSec=1
OnUnitInactiveSec=30
AccuracySec=1
Unit={UNIT}
Persistent=false
[Install]
WantedBy=timers.target
'''


def active():
    path = ROOT / 'active.json'
    return read(path) if path.exists() else {'active': False}


def validate_rendered(value, image):
    expected = service(image)
    require(value['image'] == image and value['user'] == '10001:10001'
        and value.get('read_only') is True and value.get('cap_drop') == ['ALL']
        and set(value.get('networks', {})) == {'database', 'edge'}
        and value.get('environment') == expected['environment']
        and value.get('command') == expected['command'] and not value.get('entrypoint')
        and value.get('security_opt') == expected['security_opt']
        and value.get('restart') == 'no' and value.get('init') is True
        and str(value.get('mem_limit')) == str(expected['mem_limit'])
        and value.get('cpus') == .25 and value.get('pids_limit') == 64
        and value.get('tmpfs') == expected['tmpfs']
        and not value.get('volumes') and not value.get('ports') and not value.get('privileged')
        and not value.get('cap_add') and not value.get('devices') and not value.get('network_mode')
        and value['secrets'] == expected['secrets'], 'market_rendered_service_invalid')


def approved_image(directory, environment, commit, reference, *, identities=None):
    expected = release.manifest(directory, commit)
    if identities is None:
        identities = list(release.validate_image_archive(directory / 'image.tar.gz', expected))
    # Later samples reuse only the protected preparation receipt, after pinned()
    # verifies its hash and unchanged independently approved release manifest.
    require(isinstance(identities, list) and 1 <= len(identities) <= 3
        and all(isinstance(value, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', value) for value in identities)
        and identities == sorted(set(identities)) and expected['image_id'] in identities,
        'market_archive_identity_pin_invalid')
    image = json.loads(release.command(['image', 'inspect', reference], environment=environment))[0]
    require(image['Id'] in identities
        and image['Config']['Labels']['org.opencontainers.image.revision'] == commit,
        'market_image_revision_mismatch')
    # Containerd can inspect the index ID while a running container reports its
    # config ID. Only aliases bound by the approved archive are interchangeable.
    release.verify_running_app(directory, environment, {**expected, 'archive_image_ids': identities})
    return image['Id'], identities


def prepare():
    require(not active().get('active'), 'market_stop_before_prepare')
    require(not (ROOT / 'prepared.json').exists() and not (ROOT / 'prepared.json').is_symlink(),
        'market_existing_prepare_requires_review')
    commit, directory, environment = release.current_application()
    image_id, identities = approved_image(directory, environment, commit, environment['SIXNINE_IMAGE'])
    require(not existing(environment), 'market_container_already_exists')
    record = {'schema_version': 1, 'commit': commit, 'image_id': image_id,
        'archive_image_ids': identities,
        'helper_sha256': release.checksum(HELPER),
        'manifest_sha256': release.checksum(directory / 'release-manifest.json'),
        'compose_sha256': release.checksum(directory / 'compose.yaml'),
        'site_sha256': release.checksum(release.ROOT / 'site.env')}
    record['pin'] = release.canonical_hash(record)
    atomic(ROOT / 'overlay.json', overlay(record['image_id']))
    # Render configuration locally; do not create a container or touch schema.
    rendered = json.loads(release.command(compose_args(directory, 'config', '--format', 'json'), environment=environment))
    value = rendered['services'][SERVICE]
    validate_rendered(value, record['image_id'])
    atomic(ROOT / 'prepared.json', record)
    return {'state': 'prepared_not_started', 'commit': commit, 'pin': record['pin']}


def pinned():
    record = read(ROOT / 'prepared.json')
    require(release.canonical_hash({k: v for k, v in record.items() if k != 'pin'}) == record['pin'], 'market_pin_invalid')
    require(release.checksum(HELPER) == record['helper_sha256'], 'market_helper_changed')
    state = read(release.ROOT / 'release-state.json')
    require(state.get('current') == record['commit'] and state.get('pending') is None
        and state.get('status') in {'app_ready', 'rolled_back_app_only'}, 'market_release_changed')
    directory = release.ROOT / 'releases' / record['commit']
    release.approved_manifest(release.ROOT, directory, record['commit'])
    for path, key in ((directory / 'release-manifest.json', 'manifest_sha256'),
                      (directory / 'compose.yaml', 'compose_sha256'), (release.ROOT / 'site.env', 'site_sha256')):
        release.regular(path, root_owned=True)
        require(release.checksum(path) == record[key], 'market_pinned_file_changed')
    require(read(ROOT / 'overlay.json') == overlay(record['image_id']), 'market_overlay_changed')
    environment = release.deployment_environment(release.ROOT / 'site.env', record['commit'])
    image_id, identities = approved_image(directory, environment, record['commit'], record['image_id'],
        identities=record['archive_image_ids'])
    require(image_id == record['image_id'] and identities == record['archive_image_ids'], 'market_image_changed')
    return record, directory, environment


def existing(environment):
    return release.command(['ps', '--all', '--quiet', '--filter', 'name=^/' + CONTAINER + '$'], environment=environment, timeout=5).decode().strip()


def cleanup():
    # Cleanup must still work after an app release changes. It cannot touch a
    # controller container: exact name, image and our immutable label are required.
    record = read(ROOT / 'prepared.json')
    require(release.canonical_hash({k: v for k, v in record.items() if k != 'pin'}) == record['pin'], 'market_pin_invalid')
    environment = release.deployment_environment(release.ROOT / 'site.env', record['commit'])
    identity = existing(environment)
    if not identity:
        return {'state': 'no_market_container'}
    info = json.loads(release.command(['inspect', identity], environment=environment, timeout=5))[0]
    require(info['Name'] == '/' + CONTAINER and info['Image'] in record['archive_image_ids']
        and info['Config']['Labels'].get(LABEL) == record['pin']
        and info['Config']['Labels'].get('com.docker.compose.service') == SERVICE,
        'market_cleanup_identity_mismatch')
    release.command(['rm', '--force', identity], environment=environment, timeout=10)
    return {'state': 'market_container_removed'}


def validate_result(result, started, now):
    require(isinstance(result, dict) and result.get('provider') == 'targon'
        and result.get('status') == 'ok' and result.get('fresh_success') is True
        and type(result.get('observed_at')) in (int, float)
        and started <= result['observed_at'] <= now and now - result['observed_at'] <= 120
        and type(result.get('offer_count')) is int and 0 <= result['offer_count'] <= 5000,
        'market_scan_not_fresh_success')


def sample(*, acceptance=False):
    record, directory, environment = pinned()
    marker = active()
    if acceptance:
        require(not marker.get('active'), 'market_stop_before_acceptance')
    else:
        require(marker.get('active') is True and marker.get('pin') == record['pin'], 'market_sampler_not_enabled')
    require(not existing(environment), 'market_container_already_exists')
    started = time.time()
    try:
        raw = release.command(compose_args(directory, 'run', '--rm', '-T', '--no-deps',
            '--name', CONTAINER, '--label', LABEL + '=' + record['pin'], SERVICE), environment=environment, timeout=90)
        require(len(raw) <= 8192, 'market_result_too_large')
        result = json.loads(raw)
        validate_result(result, started, time.time())
        receipt = {'pin': record['pin'], **result}
        atomic(ROOT / 'last-sample.json', receipt)
        if acceptance:
            atomic(ROOT / 'accepted.json', receipt)
        return receipt
    finally:
        cleanup()


def systemctl(*arguments):
    try:
        result = subprocess.run([SYSTEMCTL, *arguments], env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35, check=True)
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        raise release.ReleaseError('market_systemd_operation_failed') from None


def install_unit(path, content):
    if path.exists() or path.is_symlink():
        release.regular(path, root_owned=True, maximum=16384)
        require(path.read_text() == content, 'market_existing_unit_changed')
    else:
        with path.open('x') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        path.chmod(0o644)
        release.sync_directory(path.parent)


def start():
    record, _, _ = pinned()
    receipt = read(ROOT / 'accepted.json')
    require(receipt.get('pin') == record['pin'], 'market_acceptance_pin_changed')
    validate_result(receipt, receipt['observed_at'], time.time())
    require(not active().get('active'), 'market_sampler_already_enabled')
    install_unit(SYSTEMD / UNIT, unit_text())
    install_unit(SYSTEMD / TIMER, timer_text())
    systemctl('daemon-reload')
    atomic(ROOT / 'active.json', {'active': True, 'pin': record['pin'], 'enabled_at': time.time()})
    systemctl('enable', '--now', TIMER)
    return {'state': 'market_timer_enabled', 'pin': record['pin'], 'interval_seconds': 30}


def stop():
    marker = active()
    atomic(ROOT / 'active.json', {**marker, 'active': False, 'stopped_at': time.time()})
    systemctl('disable', '--now', TIMER)
    systemctl('stop', UNIT)
    return cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'once', 'start', 'sample', 'stop', 'cleanup', 'status'))
    args = parser.parse_args()
    try:
        require(os.name == 'posix' and os.geteuid() == 0, 'market_root_host_required')
        require(Path(__file__).resolve() == HELPER, 'market_install_path_invalid')
        release.regular(HELPER, root_owned=True)
        release.protected_directory(HELPER.parent)
        if args.action == 'prepare' and not ROOT.exists():
            release.protected_directory(ROOT.parent)
            ROOT.mkdir(mode=0o700)
            release.sync_directory(ROOT.parent)
        release.protected_directory(ROOT)
        # stop/cleanup do not wait on the sampler lock: stopping the only owned
        # container interrupts a GET/cache transaction, never an owned task.
        if args.action in {'stop', 'cleanup'}:
            value = globals()[args.action]()
        else:
            with locked():
                if args.action == 'once':
                    value = sample(acceptance=True)
                elif args.action == 'status':
                    value = {'activation': active(), 'last_sample': read(ROOT / 'last-sample.json') if (ROOT / 'last-sample.json').exists() else None}
                else:
                    value = globals()[args.action]()
        print(json.dumps(value, sort_keys=True))
    except Exception as error:
        code = str(error) if isinstance(error, release.ReleaseError) and re.fullmatch('[a-z0-9_]+', str(error)) else 'market_action_failed'
        print(json.dumps({'state': 'incomplete', 'error_code': code}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
