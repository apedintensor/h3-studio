#!/usr/bin/python3
"""Root-only, one-use resume after an evidenced failed GPU preparation.

The provider DELETE was separately fenced and reconciled by the old controller.
This helper never rents, changes a service window, or reuses the prior handoff.
"""
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

import release
import gpu_scaler as scaler
import gpu_handoff as handoff

RECEIPT = 'preparation-recovery-receipt.json'
PROOF = 'preparation-recovery-proof.json'
OLD = 'preparation-recovery-old.json'


def path(name):
    return scaler.ROOT/'operator'/name


def runtime_json(filename):
    info = release.regular(filename, maximum=65536)
    release.require(info.st_uid == 10001 and not info.st_mode & 0o022,
                    'preparation_runtime_file_untrusted')
    value = json.loads(filename.read_text())
    release.require(isinstance(value, dict), 'preparation_runtime_object_required')
    return value


def core(commit, action, value, *, apply=False):
    _, directory, environment = scaler.checked_release(commit)
    extra = scaler.overlay(environment['SIXNINE_IMAGE'])
    mounts = extra['services'][scaler.SERVICE]['volumes']
    mounts.append(scaler.bind(path(OLD), '/recovery-previous.json', True))
    for mount in mounts:
        if mount['target'] == '/control':
            mount['read_only'] = True
    overlay = path('preparation-validation-overlay.json')
    scaler.atomic(overlay, extra)
    args = ['compose', '--project-directory', str(directory), '-f', str(directory/'compose.yaml'),
        '-f', str(overlay), 'run', '--rm', '--no-deps', '-T', '--entrypoint', 'python',
        scaler.SERVICE, '-m', 'studio_platform.preparation_recovery',
        '--previous-config', '/recovery-previous.json', '--target-config', scaler.CONFIG_TARGET,
        '--action', action, *(['--apply'] if apply else [])]
    raw = release.command(args, environment=environment,
        input_data=json.dumps(value, sort_keys=True, allow_nan=False).encode(), timeout=60)
    release.require(len(raw) <= 65536, 'preparation_ledger_receipt_too_large')
    result = json.loads(raw)
    release.require(isinstance(result, dict), 'preparation_ledger_receipt_invalid')
    return result


def retired(config, unit, old_commit):
    host = handoff.supervisor(unit)
    release.require(host['pid'] == 0 and host['active_state'] in ('inactive', 'failed'),
                    'preparation_old_supervisor_still_running')
    _, directory, environment = scaler.checked_release(old_commit)
    state = scaler.inspect_controller(environment, config)
    release.require(state.get('Running') is False and state.get('Restarting') is False
        and state.get('OOMKilled') is False and state.get('ExitCode') == 0
        and state.get('Status') == 'exited', 'preparation_controller_exit_unconfirmed')
    status = scaler.controller_control(directory, environment, '--status')
    release.require(scaler.fresh_drained(status, config) and status['billing_pending'] == 0,
                    'preparation_old_ledger_unsettled')
    return {'controller_exited': True, 'no_restart': True,
            'process_count': 0, 'boot_children': 0, 'observed_at': time.time()}, host


def validate_ledger_shape(receipt, config):
    """Bind host state changes to every job and cycle of the protected proof."""
    proof = scaler.read_json(path(PROOF))
    ledger = receipt['ledger']
    previous = ledger.get('previous_sequence')
    release.require(type(previous) is int and 1 <= previous < config['max_cycles']
        and previous == proof.get('sequence') == receipt['old_service_state'].get('sequence')
        and ledger.get('next_sequence') == previous+1
        and ledger.get('previous_approval_id') == config['capacity_approval_id']+'-'+str(previous).zfill(3)
        and ledger.get('next_approval_id') == config['capacity_approval_id']+'-'+str(previous+1).zfill(3)
        and ledger.get('target_runtime_revision') == receipt['target_commit']
        and ledger.get('created_at') == config['created_at']
        and ledger.get('hard_deadline') == config['hard_deadline']
        and ledger.get('old_config_hash') == ledger.get('target_config_hash') == scaler.fingerprint(config)
        and ledger.get('evidence_sha256') == receipt.get('proof_sha256') == scaler.fingerprint(proof),
        'preparation_ledger_cycle_or_proof_changed')
    ids = sorted(proof.get('job_ids', []))
    release.require(ids and ids == sorted(ledger.get('restored_job_hashes', {}))
        == sorted(row['job_id'] for row in ledger.get('jobs', [])),
        'preparation_ledger_job_set_changed')


def prepare(commit, unit):
    release.require(not path(RECEIPT).exists(), 'preparation_receipt_already_exists')
    config = scaler.protected_inputs(starting=False)
    pin = scaler.read_json(scaler.ROOT/'active.json', 16384)
    old_commit = pin['commit']
    scaler.verify_marker(old_commit, config, allow_inactive=True)
    release.require(pin.get('active') is False and pin.get('admission') == 'closed'
        and old_commit != commit, 'preparation_old_execution_still_active')
    scaler.checked_release(commit)
    proof = scaler.read_json(path(PROOF))
    retirement, host = retired(config, unit, old_commit)
    release.require(proof.get('repair', {}).get('target_runtime_revision') == commit
        and proof['repair'].get('previous_runtime_revision') == old_commit,
        'preparation_release_identity_mismatch')
    proof['retired'] = retirement
    state = runtime_json(scaler.ROOT/'control'/'service-state.json')
    release.require(state.get('config_hash') == scaler.fingerprint(config)
        and state.get('sequence') == proof.get('sequence') and state.get('transfer_from') is None,
        'preparation_original_service_changed')
    flag = scaler.ROOT/'control'/'drain.flag'
    info = release.regular(flag)
    release.require(info.st_uid == 10001 and info.st_size == 0 and not info.st_mode & 0o022,
                    'preparation_original_drain_flag_untrusted')
    scaler.atomic(path(OLD), config)
    scaler.atomic(path(PROOF), proof)
    core(commit, 'prepare', proof)
    receipt = {'version': 1, 'phase': 'core_starting', 'target_commit': commit,
        'old_commit': old_commit, 'config_hash': scaler.fingerprint(config),
        'supervisor': host, 'old_service_state': state,
        'proof_sha256': scaler.fingerprint(proof), 'drain_sha256': release.checksum(flag)}
    # If the transaction result is lost, never repeat apply via this entry.
    scaler.atomic(path(RECEIPT), receipt)
    ledger = core(commit, 'prepare', proof, apply=True)
    release.require(ledger.get('phase') == 'jobs_restored', 'preparation_restore_unconfirmed')
    receipt.update(phase='jobs_restored', ledger=ledger)
    scaler.atomic(path(RECEIPT), receipt)
    validate_ledger_shape(receipt, config)
    return {'phase': receipt['phase'], 'job_ids': sorted(ledger['restored_job_hashes'])}


def stage():
    receipt = scaler.read_json(path(RECEIPT))
    release.require(receipt.get('phase') == 'jobs_restored', 'preparation_restored_jobs_required')
    config = scaler.protected_inputs(starting=False)
    release.require(scaler.fingerprint(config) == receipt['config_hash']
        and scaler.read_json(path(OLD)) == config, 'preparation_configuration_changed')
    validate_ledger_shape(receipt, config)
    release.require(handoff.supervisor(receipt['supervisor']['unit'])['pid'] == 0,
                    'preparation_supervisor_restarted')
    _, _, environment = scaler.checked_release(receipt['old_commit'])
    state = scaler.inspect_controller(environment, config)
    release.require(state.get('Running') is False and state.get('Restarting') is False
        and state.get('ExitCode') == 0 and state.get('OOMKilled') is False,
                    'preparation_controller_restarted')
    pin = scaler.verify_marker(receipt['old_commit'], config, allow_inactive=True)
    release.require(pin['active'] is False and pin.get('admission') == 'closed',
                    'preparation_old_admission_open')
    core(receipt['target_commit'], 'verify', receipt['ledger'])
    state_path = scaler.ROOT/'control'/'service-state.json'
    release.require(runtime_json(state_path) == receipt['old_service_state'],
                    'preparation_service_state_changed')
    flag = scaler.ROOT/'control'/'drain.flag'
    release.require(release.checksum(flag) == receipt['drain_sha256'], 'preparation_drain_flag_changed')
    # The exact naturally-exited container is recoverable from its immutable
    # image and durable receipts; it has no boot process, inference or assets.
    release.command(['rm', scaler.container_name(config)], environment=environment, timeout=20)
    scaler.require_new_controller(environment)
    ledger = receipt['ledger']
    next_state = {'version': 1, 'config_hash': scaler.fingerprint(config),
        'sequence': ledger['next_sequence'], 'created_at': config['created_at'],
        'transfer_from': ledger['previous_approval_id']}
    scaler.atomic(path('preparation-service-before.json'), receipt['old_service_state'])
    scaler.atomic(state_path, next_state)
    state_path.chmod(0o600)
    os.chown(state_path, 10001, 10001)
    flag.unlink()  # Only the verified service flag; prior cycle flags stay.
    release.sync_directory(flag.parent)
    ledger['host_stage_confirmed'] = True
    receipt.update(phase='staged', next_service_state=next_state, ledger=ledger)
    scaler.atomic(path(RECEIPT), receipt)
    return {'phase': 'staged', 'target_commit': receipt['target_commit'],
            'job_ids': sorted(ledger['restored_job_hashes'])}


def verify_resume(config, commit, environment):
    receipt = scaler.read_json(path(RECEIPT))
    release.require(receipt.get('version') == 1 and receipt.get('phase') == 'staged'
        and receipt.get('target_commit') == commit
        and receipt.get('config_hash') == scaler.fingerprint(config)
        and scaler.read_json(path(OLD)) == config, 'preparation_resume_identity_invalid')
    validate_ledger_shape(receipt, config)
    release.require(handoff.supervisor(receipt['supervisor']['unit'])['pid'] == 0,
                    'preparation_old_supervisor_restarted')
    scaler.require_new_controller(environment)
    pin = scaler.verify_marker(receipt['old_commit'], config, allow_inactive=True)
    release.require(pin['active'] is False and pin.get('admission') == 'closed',
                    'preparation_prior_execution_active')
    release.require(runtime_json(scaler.ROOT/'control'/'service-state.json') == receipt['next_service_state']
        and not (scaler.ROOT/'control'/'drain.flag').exists(), 'preparation_transfer_state_changed')
    release.require(time.time()+300 < config['hard_deadline'], 'preparation_original_window_expired')
    core(commit, 'verify', receipt['ledger'])
    return receipt


def activate_resume(config, commit, environment):
    receipt = verify_resume(config, commit, environment)
    core(commit, 'release-leader', receipt['ledger'], apply=True)
    receipt.update(phase='activated', activated_at=time.time())
    scaler.atomic(path(RECEIPT), receipt)
    return receipt


def main(argv=None):
    import argparse
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'stage'))
    parser.add_argument('--commit')
    parser.add_argument('--unit')
    args = parser.parse_args(argv)
    try:
        release.check_host(release.ROOT)
        with (release.ROOT/'release.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.action == 'prepare':
                release.require(isinstance(args.commit, str) and release.SHA.fullmatch(args.commit)
                    and args.unit is not None, 'preparation_target_and_supervisor_required')
                result = prepare(args.commit, args.unit)
            else:
                release.require(args.commit is None and args.unit is None, 'preparation_stage_uses_exact_receipt')
                result = stage()
        print(json.dumps(result, sort_keys=True))
        return 0
    except release.ReleaseError as error:
        code = str(error)
        print(json.dumps({'state': 'preparation_recovery_incomplete',
            'safe_error': code if re.fullmatch(r'[a-z0-9_]{1,160}', code) else 'operator_check_failed'}))
        return 1
    except Exception:
        print(json.dumps({'state': 'preparation_recovery_incomplete', 'safe_error': 'operator_state_requires_reconciliation'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
