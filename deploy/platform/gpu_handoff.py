#!/usr/bin/python3
"""Root-only exceptional recovery of a frozen, evidenced unsubmitted backlog.

Not an automatic restart. Never call a provider or change a user job/budget.
Operator supplies a post-freeze authenticated account-audit proof. Regular
start/restore retain their stricter unused-pool/drained checks.
"""
from __future__ import annotations

import copy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

import release
import gpu_scaler as scaler

RECEIPT = 'handoff-receipt.json'
PROOF = 'handoff-proof.json'
NEXT = 'handoff-next.json'
OLD = 'handoff-old.json'


def record_path(name):
    return scaler.ROOT/'operator'/name


def command(*args):
    try:
        return subprocess.run(['/usr/bin/systemctl', *args], env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin'},
            check=True, capture_output=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        raise release.ReleaseError('handoff_systemd_operation_failed') from None


def supervisor(unit):
    release.require(isinstance(unit, str) and re.fullmatch(r'sixnine-[A-Za-z0-9_.-]+\.service', unit),
        'handoff_supervisor_identity_invalid')
    values = command('show', unit, '--property=MainPID,Restart,KillMode,ActiveState').decode().splitlines()
    value = dict(line.split('=', 1) for line in values if '=' in line)
    release.require(value.get('Restart') == 'no' and value.get('KillMode') == 'process'
        and str(value.get('MainPID','')).isdigit(), 'handoff_supervisor_restart_policy_unsafe')
    return {'unit':unit, 'pid':int(value['MainPID']), 'active_state':value.get('ActiveState')}


def inspect(environment, config):
    scaler.inspect_controller(environment, config)  # Authenticated image/labels/name.
    raw = release.command(['inspect', scaler.container_name(config)], environment=environment, timeout=20)
    values = json.loads(raw)
    release.require(isinstance(values, list) and len(values) == 1, 'handoff_container_unknown')
    return values[0]


def frozen_identity(environment, config, *, frozen_at):
    value = inspect(environment, config)
    state = value.get('State', {})
    release.require(state.get('Running') is True and state.get('Paused') is True
        and state.get('Restarting') is False and state.get('OOMKilled') is False
        and value.get('RestartCount') == 0 and type(state.get('Pid')) is int and state['Pid'] > 0,
        'handoff_exact_controller_not_frozen')
    processes = release.command(['top', scaler.container_name(config), '-eo', 'pid,comm'],
        environment=environment, timeout=20).decode().splitlines()[1:]
    names = [line.split()[-1] for line in processes if line.split()]
    # Docker's tiny init is not a bootstrap child. All other processes must
    # be the one Python controller, with no shell/SSH/download/boot subprocess.
    actual = [name for name in names if name not in ('docker-init', 'tini')]
    release.require(len(names) <= 2 and len(actual) == 1 and actual[0] in ('python','python3'),
        'handoff_boot_children_or_foreign_processes')
    try:
        started = datetime.fromisoformat(state['StartedAt'].replace('Z','+00:00')).timestamp()
    except (KeyError, ValueError, TypeError):
        raise release.ReleaseError('handoff_container_started_at_invalid') from None
    return {'config_hash':scaler.fingerprint(config), 'container_id':value['Id'],
        'image_id':value['Image'], 'running':True, 'paused':True, 'restart_count':0,
        'pid':state['Pid'], 'process_count':len(actual), 'boot_children':0,
        'started_at':started, 'frozen_at':frozen_at}


def core(commit, config_path, action, value, *, apply=False):
    _, directory, environment = scaler.checked_release(commit)
    config_path = Path(config_path)
    release.require(config_path.parent == scaler.ROOT/'operator', 'handoff_config_path_invalid')
    scaler.read_json(config_path)
    extra = scaler.overlay(environment['SIXNINE_IMAGE'])
    mounts = extra['services'][scaler.SERVICE]['volumes']
    for mount in mounts:
        if mount['target'] == scaler.CONFIG_TARGET:
            mount['source'] = str(config_path)
        if mount['target'] == '/control':
            mount['read_only'] = True
    validation_path = record_path('handoff-validation-overlay.json')
    scaler.atomic(validation_path, extra)
    args = ['compose', '--project-directory', str(directory), '-f', str(directory/'compose.yaml'),
        '-f', str(validation_path), 'run', '--rm', '--no-deps', '-T', '--entrypoint', 'python',
        scaler.SERVICE, '-m', 'studio_platform.backlog_handoff', '--config', scaler.CONFIG_TARGET,
        '--action', action, *(['--apply'] if apply else [])]
    encoded = json.dumps(value, sort_keys=True, allow_nan=False).encode()
    release.require(len(encoded) <= 131072, 'handoff_evidence_too_large')
    raw = release.command(args, environment=environment, input_data=encoded, timeout=60)
    release.require(len(raw) <= 65536, 'handoff_ledger_receipt_too_large')
    result = json.loads(raw)
    release.require(isinstance(result, dict), 'handoff_ledger_receipt_invalid')
    return result


def freeze(commit, unit):
    release.require(not record_path(RECEIPT).exists(), 'handoff_receipt_already_exists')
    config = scaler.protected_inputs(starting=False)
    pin = scaler.read_json(scaler.ROOT/'active.json', 16384)
    scaler.verify_marker(pin['commit'], config)
    scaler.checked_release(commit)
    _, directory, environment = scaler.checked_release(pin['commit'])
    host = supervisor(unit)
    release.require(host['pid'] > 0 and host['active_state'] == 'active', 'handoff_supervisor_not_active')
    value = inspect(environment, config)
    release.require(value.get('State',{}).get('Running') is True
        and value.get('State',{}).get('Paused') is False, 'handoff_controller_not_running')
    # Freeze first. SIGTERM/request-drain would cancel the accepted backlog.
    release.command(['pause', scaler.container_name(config)], environment=environment, timeout=20)
    frozen = frozen_identity(environment, config, frozen_at=time.time())
    scaler.close_admission(directory, environment)
    # CPU app recreation can take over a minute. Start the supplier proof's
    # freshness window only after rechecking the same continuously paused
    # container, rather than consuming it while waiting for website health.
    confirmed = frozen_identity(environment, config, frozen_at=time.time())
    release.require({k:v for k,v in confirmed.items() if k != 'frozen_at'}
        == {k:v for k,v in frozen.items() if k != 'frozen_at'}, 'handoff_frozen_identity_changed')
    frozen = confirmed
    scaler.atomic(record_path(OLD), config)
    receipt = {'version':1, 'phase':'frozen', 'target_commit':commit, 'old_commit':pin['commit'],
        'old_config_hash':scaler.fingerprint(config), 'old_pin':pin, 'supervisor':host, 'frozen':frozen}
    scaler.atomic(record_path(RECEIPT), receipt)
    return {'phase':'frozen', 'frozen':frozen, 'supplier_audit_required':True}


def prepare():
    receipt = scaler.read_json(record_path(RECEIPT))
    release.require(receipt.get('phase') == 'frozen', 'handoff_freeze_required')
    config = scaler.protected_inputs(starting=False)
    release.require(scaler.fingerprint(config) == receipt['old_config_hash'], 'handoff_old_config_changed')
    _, _, environment = scaler.checked_release(receipt['old_commit'])
    actual = frozen_identity(environment, config, frozen_at=receipt['frozen']['frozen_at'])
    release.require(actual == receipt['frozen'], 'handoff_frozen_identity_changed')
    proof = scaler.read_json(record_path(PROOF), 131072)
    release.require(proof.get('frozen') == actual, 'handoff_supplier_proof_frozen_identity_mismatch')
    ledger = core(receipt['target_commit'], record_path(OLD), 'prepare', proof, apply=True)
    release.require(ledger.get('phase') == 'ledger_fenced', 'handoff_ledger_fence_unconfirmed')
    receipt.update(phase='ledger_fenced', ledger=ledger, proof_sha256=scaler.fingerprint(proof))
    scaler.atomic(record_path(RECEIPT), receipt)
    return {'phase':'ledger_fenced','intent_id':ledger['intent_id'], 'job_ids':sorted(ledger['job_hashes'])}


def retire(*, sleep=time.sleep):
    receipt = scaler.read_json(record_path(RECEIPT))
    release.require(receipt.get('phase') == 'ledger_fenced', 'handoff_ledger_fence_required')
    old = scaler.read_json(record_path(OLD))
    current = scaler.protected_inputs(starting=False)
    release.require(current == old, 'handoff_old_config_changed')
    core(receipt['target_commit'], record_path(OLD), 'verify', receipt['ledger'])
    _, _, environment = scaler.checked_release(receipt['old_commit'])
    actual = frozen_identity(environment, old, frozen_at=receipt['frozen']['frozen_at'])
    release.require(actual == receipt['frozen'], 'handoff_frozen_identity_changed')
    host = supervisor(receipt['supervisor']['unit'])
    release.require(host == receipt['supervisor'], 'handoff_supervisor_changed')
    # The old host's late cleanup can overwrite the new admission marker.
    # Kill ONLY its confirmed main process, never systemctl stop/TERM or all
    # processes in the cgroup; the application remains frozen until SIGKILL.
    command('kill', '--kill-whom=main', '--signal=SIGKILL', host['unit'])
    for _ in range(20):
        if supervisor(host['unit'])['pid'] == 0:
            break
        sleep(.25)
    release.require(supervisor(host['unit'])['pid'] == 0, 'handoff_supervisor_retirement_unconfirmed')
    # Moby sends SIGKILL before resuming a paused task. Never manually unpause:
    # that would expose a rotate/cancel race despite the ledger leader fence.
    release.command(['kill','--signal=SIGKILL',scaler.container_name(old)], environment=environment, timeout=20)
    state = inspect(environment, old).get('State', {})
    release.require(state.get('Running') is False and state.get('Paused') is False
        and state.get('Restarting') is False and state.get('Status') == 'exited'
        and state.get('ExitCode') == 137, 'handoff_controller_retirement_unconfirmed')
    core(receipt['target_commit'], record_path(OLD), 'verify', receipt['ledger'])
    scaler.marker(receipt['old_commit'], old, False)
    release.command(['rm',scaler.container_name(old)], environment=environment, timeout=20)
    receipt['ledger']['host_retirement_confirmed'] = True
    receipt.update(phase='retired', retired_at=time.time())
    scaler.atomic(record_path(RECEIPT), receipt)
    return {'phase':'retired','preserved_job_ids':sorted(receipt['ledger']['job_hashes'])}


def validate_delta(old, new):
    """Narrow operator selector/price/TTL change; no new user or budget grant."""
    scaler.on_demand_config(old)
    scaler.on_demand_config(new)
    old_base, new_base = copy.deepcopy(old), copy.deepcopy(new)
    old_launches, new_launches = old_base.pop('launches'), new_base.pop('launches')
    old_manifests, new_manifests = old_base.pop('manifests'), new_base.pop('manifests')
    old_scale, new_scale = old_base.pop('scale_policy'), new_base.pop('scale_policy')
    release.require(old_base == new_base and len(old_launches) == len(new_launches) == 1
        and len(old_manifests) == len(new_manifests) == 1, 'handoff_configuration_scope_changed')
    for a,b in zip(old_launches, new_launches):
        a,b = copy.deepcopy(a),copy.deepcopy(b)
        a.pop('offer_id',None);b.pop('offer_id',None)
        release.require(a == b, 'handoff_launch_contract_changed')
    selector_fields = ('executor_id','compatible_gpu_names','minimum_vram_mib','allowed_countries',
        'server_side_selection','minimum_ram_gib','minimum_disk_gib','require_docker_in_docker',
        'max_price_per_gpu_hour_microusd','termination_hours')
    a,b = copy.deepcopy(old_manifests[0]), copy.deepcopy(new_manifests[0])
    for name in selector_fields:
        a.pop(name,None);b.pop(name,None)
    release.require(a == b, 'handoff_model_template_identity_changed')
    manifest = new_manifests[0]
    release.require(manifest.get('gpu_count') == 1 and manifest.get('execution_slots',1) == 1
        and type(manifest.get('max_price_per_gpu_hour_microusd')) is int
        and 0 < manifest['max_price_per_gpu_hour_microusd'] <= 1500000
        and type(manifest.get('termination_hours')) is int and 1 <= manifest['termination_hours'] <= 3
        and manifest.get('executor_id') == '' and new_launches[0].get('offer_id') == ''
        and isinstance(manifest.get('compatible_gpu_names'),list) and manifest['compatible_gpu_names']
        and type(manifest.get('minimum_vram_mib')) is int and manifest['minimum_vram_mib'] >= 70000,
        'handoff_single_gpu_filter_or_price_limits_invalid')
    reservation = new_scale.pop('instance_reservation_microusd', None)
    old_scale.pop('instance_reservation_microusd',None)
    release.require(new_scale == old_scale and type(reservation) is int and 0 < reservation <= 4500000
        and reservation >= manifest['gpu_count']*manifest['max_price_per_gpu_hour_microusd']*manifest['termination_hours'],
        'handoff_original_budget_or_scale_limits_changed')
    return True


def runtime_json(path):
    info = release.regular(path, maximum=65536)
    release.require(info.st_uid == 10001 and stat.S_IMODE(info.st_mode) == 0o600,
        'handoff_service_state_permissions_invalid')
    value = json.loads(path.read_text())
    release.require(isinstance(value,dict),'handoff_service_state_invalid')
    return value


def stage():
    receipt = scaler.read_json(record_path(RECEIPT))
    release.require(receipt.get('phase') == 'retired', 'handoff_retirement_required')
    old, new = scaler.read_json(record_path(OLD)), scaler.read_json(record_path(NEXT))
    validate_delta(old,new)
    core(receipt['target_commit'], record_path(OLD), 'verify', receipt['ledger'])
    _, _, environment = scaler.checked_release(receipt['target_commit'])
    scaler.require_new_controller(environment)
    release.require(supervisor(receipt['supervisor']['unit'])['pid'] == 0, 'handoff_supervisor_still_running')
    pin = scaler.verify_marker(receipt['old_commit'],old,allow_inactive=True)
    release.require(pin.get('active') is False and pin.get('admission') == 'closed','handoff_old_admission_open')
    state_path = scaler.ROOT/'control'/'service-state.json'
    state = runtime_json(state_path)
    release.require(state.get('config_hash') == scaler.fingerprint(old)
        and state.get('sequence') == receipt['ledger']['previous_sequence']
        and state.get('created_at') == old['created_at'] and state.get('transfer_from') is None,
        'handoff_previous_service_state_changed')
    release.require(not (scaler.ROOT/'control'/'drain.flag').exists(), 'handoff_prior_drain_requested')
    next_state = {'version':1, 'config_hash':scaler.fingerprint(new),
        'sequence':receipt['ledger']['next_sequence'], 'created_at':old['created_at'],
        'transfer_from':receipt['ledger']['previous_approval_id']}
    scaler.atomic(record_path('handoff-service-before.json'), state)
    # Independent root stage; no process is alive to read these two files.
    scaler.atomic(scaler.CONFIG_SOURCE,new)
    scaler.atomic(state_path,next_state)
    state_path.chmod(0o600)
    os.chown(state_path,10001,10001)
    receipt.update(phase='staged',new_config_hash=scaler.fingerprint(new),next_service_state=next_state)
    scaler.atomic(record_path(RECEIPT),receipt)
    return {'phase':'staged','target_commit':receipt['target_commit'],
        'job_ids':sorted(receipt['ledger']['job_hashes'])}


def verify_resume(config, commit, environment):
    """Only the exact fully evidenced transfer may bypass fresh-start checks."""
    receipt = scaler.read_json(record_path(RECEIPT))
    release.require(receipt.get('version') == 1 and receipt.get('phase') == 'staged'
        and receipt.get('target_commit') == commit
        and receipt.get('new_config_hash') == scaler.fingerprint(config), 'handoff_resume_identity_invalid')
    old = scaler.read_json(record_path(OLD))
    validate_delta(old,config)
    release.require(receipt.get('old_config_hash') == scaler.fingerprint(old)
        and receipt.get('ledger',{}).get('host_retirement_confirmed') is True,
        'handoff_retirement_evidence_missing')
    release.require(supervisor(receipt['supervisor']['unit'])['pid'] == 0,
        'handoff_old_supervisor_still_running')
    scaler.require_new_controller(environment)
    pin = scaler.read_json(scaler.ROOT/'active.json',16384)
    release.require(pin.get('active') is False and pin.get('admission') == 'closed'
        and pin.get('commit') == receipt['old_commit'] and pin.get('config_hash') == scaler.fingerprint(old),
        'handoff_original_pin_changed')
    release.require(runtime_json(scaler.ROOT/'control'/'service-state.json') == receipt['next_service_state']
        and not (scaler.ROOT/'control'/'drain.flag').exists(), 'handoff_transfer_state_changed')
    release.require(time.time()+300 < config['hard_deadline'],'handoff_original_window_expired')
    core(commit,record_path(OLD),'verify',receipt['ledger'])
    return receipt


def activate_resume(config, commit, environment):
    receipt = verify_resume(config,commit,environment)
    core(commit,record_path(OLD),'release-leader',receipt['ledger'],apply=True)
    # Consume before real launch. A failed stdin/startup is uncertain and must
    # be reconciled separately, never repeated via this one-use handoff entry.
    receipt.update(phase='activated',activated_at=time.time())
    scaler.atomic(record_path(RECEIPT),receipt)
    return receipt


def main(argv=None):
    import argparse
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('freeze','prepare','retire','stage'))
    parser.add_argument('--target-commit')
    parser.add_argument('--unit')
    args = parser.parse_args(argv)
    try:
        release.check_host(release.ROOT)
        with (release.ROOT/'release.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if args.action == 'freeze':
                result = freeze(args.target_commit,args.unit)
            else:
                release.require(args.target_commit is None and args.unit is None,'handoff_record_identity_required')
                result = {'prepare':prepare,'retire':retire,'stage':stage}[args.action]()
        print(json.dumps(result,sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({'safe_error':'handoff_incomplete_barrier_retained'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
