#!/usr/bin/python3
"""Protected recovery of bootstrap_failed before any inference or fleet start.

Operator stages root-owned target.json and changed files in sources/ beneath
gpu-scaler/operator/preparation-repairs/<UUID>. Every action holds release.lock.
target.json and public staged sources are mode 0644 for the unprivileged helper;
their containing operator directories remain root-owned and non-writable.
No default action, provider calls, budget changes or task resubmission. A lost
mutation response leaves a durable *_started receipt and forbids blind replay.
staging_failed/staging_cancelled are intentionally unsupported. Do not use this
entry for submitted, ambiguous, cancelled or expired jobs or unsettled rentals.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

import release
import gpu_scaler as scaler
import gpu_handoff as handoff


def process_identity(pid):
    release.require(type(pid) is int and pid > 0, 'repair_process_identity_invalid')
    proc = Path('/proc')/str(pid)
    release.require(proc.stat().st_uid == 0, 'repair_supervisor_not_root')
    raw = (proc/'cmdline').read_bytes()
    release.require(0 < len(raw) <= 32768, 'repair_process_command_invalid')
    argv = raw.rstrip(b'\0').decode().split('\0')
    fields = (proc/'stat').read_text().rsplit(')', 1)[1].split()
    return argv, int(fields[19])  # Linux proc field 22, process start ticks.


def bound_supervisor(unit, config):
    value = handoff.supervisor(unit)
    release.require(value['pid'] > 0 and value['active_state'] == 'active', 'repair_supervisor_not_active')
    fields = dict(line.split('=', 1) for line in handoff.command('show', unit,
        '--property=InvocationID').decode().splitlines() if '=' in line)
    invocation = fields.get('InvocationID', '')
    release.require(re.fullmatch(r'[0-9a-f]{32}', invocation), 'repair_supervisor_invocation_invalid')
    argv, started = process_identity(value['pid'])
    allowed = ['/usr/bin/python3', str(Path(scaler.__file__).resolve())]
    ordinary = len(argv) == 3 and argv[:2] == allowed and argv[2] in ('start', 'resume-handoff', 'resume-preparation')
    recovery = (len(argv) == 6 and argv[:2] == ['/usr/bin/python3', str(Path(__file__).resolve())]
        and argv[2] == '--operation' and re.fullmatch(r'[0-9a-f-]{36}', argv[3])
        and argv[4:] == ['--action', 'resume'])
    release.require(ordinary or recovery, 'repair_supervisor_entrypoint_mismatch')
    children = (Path('/proc')/str(value['pid'])/'task'/str(value['pid'])/'children').read_text().split()
    release.require(len(children) == 1 and children[0].isdigit(), 'repair_supervisor_child_unknown')
    child = int(children[0])
    command, child_start = process_identity(child)
    release.require(command[0] == release.DOCKER and '--name' in command
        and command.count('--name') == 1 and command[command.index('--name')+1] == scaler.container_name(config)
        and '-m' in command and command[command.index('-m')+1] == scaler.ENTRY_MODULE
        and '--credential-stdin' in command, 'repair_controller_parent_unproven')
    return {**value, 'invocation_id': invocation, 'start_ticks': started,
            'docker_pid': child, 'docker_start_ticks': child_start}


def supervisor_exited(saved, *, include_client=False):
    raw = handoff.command('show', saved['unit'], '--property=MainPID,InvocationID,LoadState').decode()
    fields = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    if fields.get('MainPID') != '0':
        return False
    if fields.get('LoadState') != 'not-found' and fields.get('InvocationID') != saved['invocation_id']:
        return False
    # This is accepted only after our acknowledged SIGKILL of the frozen
    # exact main process. A zombie cannot execute, but may still have a /proc
    # entry with an empty cmdline until PID1 reaps it; inspect start ticks/state
    # rather than treating that ordinary termination interval as malformed.
    pairs = [('pid', 'start_ticks')]
    if include_client:
        pairs.append(('docker_pid', 'docker_start_ticks'))
    for key, ticks in pairs:
        try:
            tail = (Path('/proc')/str(saved[key])/'stat').read_text().rsplit(')', 1)[1].split()
            if int(tail[19]) == saved[ticks] and tail[0] != 'Z':
                return False
        except FileNotFoundError:
            pass
    return True


def secure_directory(path):
    info = path.lstat()
    release.require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                    'repair_directory_not_protected')


def operation_directory(operation):
    release.require(str(uuid.UUID(operation)) == operation, 'repair_operation_id_invalid')
    parent = scaler.ROOT/'operator'/'preparation-repairs'
    secure_directory(scaler.ROOT/'operator')
    secure_directory(parent)
    directory = parent/operation
    secure_directory(directory)
    return directory


def runtime_json(path):
    info = release.regular(path, maximum=131072)
    release.require(info.st_uid == 10001 and not info.st_mode & 0o077, 'repair_runtime_evidence_not_private')
    return json.loads(path.read_text())


def validate_target(old, target, directory):
    """A source repair cannot replace accepted engine/configuration semantics."""
    info = release.regular(directory/'target.json', root_owned=True, maximum=131072)
    release.require(stat.S_IMODE(info.st_mode) == 0o644, 'repair_target_config_requires_public_read_mode')
    scaler.engine_config(target)
    scaler.on_demand_config(target)
    release.require(set(old) == set(target)
        and all(old[k] == target[k] for k in old if k != 'source_sha256'), 'repair_contract_change_forbidden')
    a, b = old['source_sha256'], target['source_sha256']
    changed = sorted(k for k in a if a[k] != b.get(k))
    allowed = ({'wangp-bootstrap.py', 'wangp-package.tar.gz', 'wangp-runtime.json'}
               if scaler.engine_config(old) == 'wangp-worker' else {'bootstrap_cloud.py'})
    release.require(set(a) == set(b) and changed and set(changed) <= allowed, 'repair_source_delta_invalid')
    secure_directory(directory/'sources')
    release.require({p.name for p in (directory/'sources').iterdir()} == set(changed), 'repair_staged_source_set_invalid')
    for name in changed:
        path = directory/'sources'/name
        release.regular(path, root_owned=True, maximum=16*1024**2 if name.endswith('.gz') else 524288)
        release.require(re.fullmatch(r'[0-9a-f]{64}', b[name]) and release.checksum(path) == b[name],
                        'repair_staged_source_hash_changed')
    binding = {}
    if 'wangp-package.tar.gz' in changed or 'wangp-runtime.json' in changed:
        release.require({'wangp-package.tar.gz', 'wangp-runtime.json'} <= set(changed), 'repair_bundle_binding_required')
        # Use the preserved old document after staging; never infer it from the
        # newly published source. Backups are root-owned and authenticated below.
        old_path = directory/'before'/'wangp-runtime.json'
        if not old_path.exists():
            old_path = scaler.SOURCE/'wangp-runtime.json'
        before = scaler.read_json(old_path, 524288)
        after = scaler.read_json(directory/'sources'/'wangp-runtime.json', 524288)
        release.require(release.checksum(old_path) == a['wangp-runtime.json']
            and before.get('source_bundle_sha256') == a['wangp-package.tar.gz']
            and after.get('source_bundle_sha256') == b['wangp-package.tar.gz']
            and {k:v for k,v in before.items() if k != 'source_bundle_sha256'}
                == {k:v for k,v in after.items() if k != 'source_bundle_sha256'},
            'repair_runtime_semantics_changed')
        binding = {'previous': before, 'target': after,
            'previous_file_sha256': a['wangp-runtime.json'], 'target_file_sha256': b['wangp-runtime.json']}
    return changed, binding


def core(directory, commit, action, value, *, apply=False):
    _, release_dir, environment = scaler.checked_release(commit)
    old = scaler.read_json(directory/'previous.json')
    scaler.read_json(directory/'target.json')
    extra = scaler.overlay(environment['SIXNINE_IMAGE'], scaler.engine_config(old))
    mounts = extra['services'][scaler.SERVICE]['volumes']
    mounts.extend([{'type': 'bind', 'source': str(directory/name), 'target': '/repair/'+name,
                    'read_only': True, 'bind': {'create_host_path': False}}
                   for name in ('previous.json', 'target.json')])
    for mount in mounts:
        if mount['target'] == '/control':
            mount['read_only'] = True
    overlay = directory/'validation-overlay.json'
    scaler.atomic(overlay, extra)
    argv = ['compose', '--project-directory', str(release_dir), '-f', str(release_dir/'compose.yaml'),
        '-f', str(overlay), 'run', '--rm', '--no-deps', '-T', '--entrypoint', 'python', scaler.SERVICE,
        '-m', 'studio_platform.preparation_hold_recovery', '--previous-config', '/repair/previous.json',
        '--target-config', '/repair/target.json', '--action', action, *(['--apply'] if apply else [])]
    encoded = json.dumps(value, sort_keys=True, allow_nan=False).encode()
    release.require(len(encoded) <= 131072, 'repair_core_input_too_large')
    raw = release.command(argv, environment=environment, input_data=encoded, timeout=60)
    release.require(len(raw) <= 131072, 'repair_core_output_too_large')
    result = json.loads(raw)
    release.require(isinstance(result, dict), 'repair_core_result_invalid')
    return result


def identity(directory, receipt):
    old = scaler.read_json(directory/'previous.json')
    release.require(scaler.fingerprint(old) == receipt['old_config_hash'], 'repair_previous_config_changed')
    target = scaler.read_json(directory/'target.json')
    release.require(scaler.fingerprint(target) == receipt['target_config_hash'], 'repair_target_config_changed')
    validate_target(old, target, directory)
    _, _, environment = scaler.checked_release(receipt['old_commit'])
    return old, target, environment


def frozen(directory, receipt):
    old, target, environment = identity(directory, receipt)
    release.require(scaler.protected_inputs(starting=False) == old, 'repair_active_config_changed')
    actual = handoff.frozen_identity(environment, old, frozen_at=receipt['frozen']['frozen_at'])
    release.require(actual == receipt['frozen'] and bound_supervisor(receipt['supervisor']['unit'], old)
        == receipt['supervisor'], 'repair_frozen_controller_changed')
    return old, target, environment


def freeze(directory, commit, unit):
    receipt_path = directory/'receipt.json'
    release.require(not receipt_path.exists(), 'repair_operation_already_started')
    old = scaler.protected_inputs(starting=False)
    target = scaler.read_json(directory/'target.json')
    changed, binding = validate_target(old, target, directory)
    pin = scaler.read_json(scaler.ROOT/'active.json', 16384)
    scaler.verify_marker(pin['commit'], old)
    release.require(pin['commit'] != commit, 'repair_new_release_required')
    scaler.checked_release(commit)
    _, release_dir, environment = scaler.checked_release(pin['commit'])
    host = bound_supervisor(unit, old)
    state = runtime_json(scaler.ROOT/'control'/'service-state.json')
    sequence = state.get('sequence')
    release.require(type(sequence) is int and sequence >= 1 and state.get('config_hash') == scaler.fingerprint(old)
        and state.get('transfer_from') is None and not (scaler.ROOT/'control'/'drain.flag').exists(),
        'repair_original_service_not_held')
    cycle = scaler.ROOT/'control'/'cycles'/str(sequence).zfill(3)
    hold = runtime_json(cycle/'preparation-hold.json')
    release.require(hold.get('reason') == 'bootstrap_repair_required', 'repair_preparation_hold_required')
    value = handoff.inspect(environment, old)
    release.require(value.get('State', {}).get('Running') is True
        and value.get('State', {}).get('Paused') is False, 'repair_controller_not_running')
    # Save intent before pause: any partial result is explicit and never starts
    # a second controller or clears the original hold on a blind retry.
    scaler.atomic(directory/'previous.json', old)
    receipt = {'version': 1, 'phase': 'freeze_started', 'old_commit': pin['commit'], 'target_commit': commit,
        'old_config_hash': scaler.fingerprint(old), 'target_config_hash': scaler.fingerprint(target),
        'supervisor': host, 'sequence': sequence, 'intent_id': hold['intent_id'],
        'reviewed_source_files': changed, 'runtime_source_binding': binding, 'original_service_state': state}
    release.require(len(json.dumps(receipt, sort_keys=True, allow_nan=False).encode()) <= 8192,
                    'repair_host_receipt_exceeds_safe_limit')
    scaler.atomic(receipt_path, receipt)
    release.command(['pause', scaler.container_name(old)], environment=environment, timeout=20)
    first = handoff.frozen_identity(environment, old, frozen_at=time.time())
    scaler.close_admission(release_dir, environment)
    latest = handoff.frozen_identity(environment, old, frozen_at=time.time())
    release.require({k:v for k,v in first.items() if k != 'frozen_at'}
        == {k:v for k,v in latest.items() if k != 'frozen_at'}, 'repair_frozen_identity_changed')
    receipt.update(phase='frozen', frozen=latest)
    scaler.atomic(receipt_path, receipt)
    return {'phase': 'frozen', 'intent_id': receipt['intent_id'], 'sequence': sequence}


def prepare(directory, job_ids):
    receipt = scaler.read_json(directory/'receipt.json')
    release.require(receipt.get('phase') == 'frozen', 'repair_freeze_required')
    frozen(directory, receipt)
    proof = {k: receipt[k] for k in ('version', 'intent_id', 'sequence', 'target_config_hash',
        'reviewed_source_files', 'previous_commit', 'target_commit', 'frozen') if k in receipt}
    proof['previous_commit'] = receipt['old_commit']
    proof['job_ids'] = sorted(set(job_ids))
    proof['runtime_source_binding'] = receipt['runtime_source_binding']
    proof['frozen'] = {**receipt['frozen'], 'frozen_at': time.time()}
    core(directory, receipt['target_commit'], 'prepare', proof)
    # Hashing approved source/image inputs may take time. Re-observe exactly
    # the same continuous freeze after dry-run, never renew a user deadline.
    frozen(directory, receipt)
    proof['frozen'] = {**receipt['frozen'], 'frozen_at': time.time()}
    receipt.update(phase='prepare_started', proof_sha256=scaler.fingerprint(proof))
    scaler.atomic(directory/'receipt.json', receipt)
    ledger = core(directory, receipt['target_commit'], 'prepare', proof, apply=True)
    release.require(ledger.get('phase') == 'fenced', 'repair_fence_unconfirmed')
    receipt.update(phase='fenced', ledger=ledger)
    scaler.atomic(directory/'receipt.json', receipt)
    return {'phase': 'fenced', 'job_ids': sorted(ledger['snapshot']['job_hashes'])}


def retire(directory, *, sleep=time.sleep):
    receipt = scaler.read_json(directory/'receipt.json')
    release.require(receipt.get('phase') == 'fenced', 'repair_fence_required')
    old, _, environment = frozen(directory, receipt)
    core(directory, receipt['target_commit'], 'verify', receipt['ledger'])
    receipt['phase'] = 'retire_started'
    scaler.atomic(directory/'receipt.json', receipt)
    host = receipt['supervisor']
    handoff.command('kill', '--kill-whom=main', '--signal=SIGKILL', host['unit'])
    for _ in range(20):
        if supervisor_exited(host):
            break
        sleep(.25)
    release.require(supervisor_exited(host), 'repair_supervisor_still_running')
    # Never TERM/unpause: that would convert a repair hold into user-job drain.
    release.command(['kill', '--signal=SIGKILL', scaler.container_name(old)], environment=environment, timeout=20)
    actual = handoff.inspect(environment, old)
    stopped = actual.get('State', {})
    release.require(actual['Id'] == receipt['frozen']['container_id'] and actual['Image'] == receipt['frozen']['image_id']
        and stopped.get('Running') is False and stopped.get('Paused') is False
        and stopped.get('Restarting') is False and stopped.get('OOMKilled') is False
        and stopped.get('ExitCode') == 137 and stopped.get('Status') == 'exited'
        and actual.get('RestartCount') == 0, 'repair_exact_stop_unconfirmed')
    for _ in range(20):
        if supervisor_exited(host, include_client=True):
            break
        sleep(.25)
    release.require(supervisor_exited(host, include_client=True), 'repair_original_docker_client_still_running')
    core(directory, receipt['target_commit'], 'verify', receipt['ledger'])
    scaler.marker(receipt['old_commit'], old, False)
    receipt.update(phase='retired', retired_at=time.time())
    scaler.atomic(directory/'receipt.json', receipt)
    return {'phase': 'retired', 'intent_id': receipt['intent_id']}


def require_retired(directory, receipt):
    old, target, environment = identity(directory, receipt)
    release.require(supervisor_exited(receipt['supervisor'], include_client=True), 'repair_old_supervisor_returned')
    value = handoff.inspect(environment, old)
    state = value.get('State', {})
    release.require(value['Id'] == receipt['frozen']['container_id'] and value['Image'] == receipt['frozen']['image_id']
        and value.get('RestartCount') == 0 and state.get('Running') is False and state.get('Paused') is False
        and state.get('Restarting') is False and state.get('OOMKilled') is False and state.get('ExitCode') == 137,
        'repair_old_controller_not_retired')
    return old, target, environment


def stage(directory):
    receipt = scaler.read_json(directory/'receipt.json')
    release.require(receipt.get('phase') == 'retired', 'repair_retirement_required')
    old, target, _ = require_retired(directory, receipt)
    release.require(scaler.protected_inputs(starting=False) == old, 'repair_old_inputs_changed')
    core(directory, receipt['target_commit'], 'verify', receipt['ledger'])
    before = directory/'before'
    release.require(not before.exists(), 'repair_source_backup_already_exists')
    before.mkdir(mode=0o700)
    # All staged hashes/semantics were verified before any source is replaced.
    changed, _ = validate_target(old, target, directory)
    for name in changed:
        source = scaler.SOURCE/name
        release.regular(source, root_owned=True, maximum=16*1024**2)
        release.require(release.checksum(source) == old['source_sha256'][name], 'repair_previous_source_changed')
    for name in changed:
        data = (scaler.SOURCE/name).read_bytes()
        with (before/name).open('xb') as out:
            os.chmod(before/name, 0o600)
            out.write(data); out.flush(); os.fsync(out.fileno())
    release.sync_directory(before)
    receipt['phase'] = 'stage_started'
    scaler.atomic(directory/'receipt.json', receipt)
    for name in changed:
        source = directory/'sources'/name
        temporary = scaler.SOURCE/('.repair-'+directory.name+'-'+name)
        with temporary.open('xb') as out:
            os.chmod(temporary, 0o644)  # Public bootstrap source, read by UID10001.
            out.write(source.read_bytes()); out.flush(); os.fsync(out.fileno())
        os.replace(temporary, scaler.SOURCE/name)
    release.sync_directory(scaler.SOURCE)
    scaler.atomic(scaler.CONFIG_SOURCE, target)
    release.require(scaler.protected_inputs(starting=False) == target, 'repair_published_inputs_invalid')
    next_state = {'version': 1, 'config_hash': scaler.fingerprint(target), 'sequence': receipt['sequence']+1,
        'created_at': old['created_at'], 'transfer_from': receipt['ledger']['transfer_from']}
    state_path = scaler.ROOT/'control'/'service-state.json'
    release.require(runtime_json(state_path) == receipt['original_service_state'], 'repair_original_state_changed')
    scaler.atomic(state_path, next_state)
    os.chown(state_path, 10001, 10001); os.chmod(state_path, 0o600)
    receipt['ledger']['host_stage_confirmed'] = True
    core(directory, receipt['target_commit'], 'verify', receipt['ledger'])
    receipt.update(phase='staged', next_service_state=next_state)
    scaler.atomic(directory/'receipt.json', receipt)
    return {'phase': 'staged', 'next_sequence': next_state['sequence']}


def resume(directory):
    """Explicit root-supervised activation; return live child to normal monitor."""
    receipt = scaler.read_json(directory/'receipt.json')
    release.require(receipt.get('phase') == 'staged', 'repair_stage_required')
    old, target, old_environment = require_retired(directory, receipt)
    release.require(scaler.protected_inputs(starting=False) == target
        and runtime_json(scaler.ROOT/'control'/'service-state.json') == receipt['next_service_state'],
        'repair_staged_identity_changed')
    commit, release_dir, environment = scaler.checked_release()
    release.require(commit == receipt['target_commit'], 'repair_target_release_not_current')
    pin = scaler.verify_marker(receipt['old_commit'], old, allow_inactive=True)
    release.require(pin['active'] is False and pin.get('admission', 'closed') == 'closed', 'repair_old_admission_open')
    core(directory, commit, 'verify', receipt['ledger'])
    scaler.atomic(scaler.ROOT/'overlay.json', scaler.overlay(environment['SIXNINE_IMAGE'], scaler.engine_config(target)))
    scaler.atomic(scaler.ROOT/'app-admission.json', release.app_admission_overlay(release.ROOT))
    validation = scaler.controller_control(release_dir, environment, '--validate')
    release.require(validation.get('config_valid') is True and validation.get('provider_calls_enabled') is False
        and validation.get('config_hash') == scaler.fingerprint(target), 'repair_controller_validation_failed')
    receipt['phase'] = 'resume_started'
    scaler.atomic(directory/'receipt.json', receipt)
    release.command(['rm', scaler.container_name(old)], environment=old_environment, timeout=20)
    scaler.require_new_controller(environment)
    core(directory, commit, 'release-leader', receipt['ledger'], apply=True)
    scaler.marker(commit, target, True)
    try:
        process = scaler.launch(release_dir, environment, target)
        scaler.wait_until_ready(process, release_dir, environment, target)
        scaler.enable_admission(release_dir, environment, target)
        receipt.update(phase='activated', activated_at=time.time())
        scaler.atomic(directory/'receipt.json', receipt)
    except Exception as exc:
        # Keep an uncertain controller alive for reconciliation; close only
        # public admission while retaining the one-use resume_started receipt.
        scaler.close_admission(release_dir, environment)
        raise
    return process, release_dir, environment, target, commit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation', required=True)
    parser.add_argument('--action', choices=('freeze', 'prepare', 'retire', 'stage', 'resume'), required=True)
    parser.add_argument('--target-commit')
    parser.add_argument('--unit')
    parser.add_argument('--job-id', action='append', default=[])
    args = parser.parse_args(argv)
    active = None
    try:
        import fcntl
        release.check_host(release.ROOT)
        with (release.ROOT/'release.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            directory = operation_directory(args.operation)
            if args.action == 'freeze':
                result = freeze(directory, args.target_commit, args.unit)
            elif args.action == 'prepare':
                release.require(args.job_id, 'repair_explicit_job_ids_required')
                result = prepare(directory, args.job_id)
            elif args.action == 'resume':
                active = resume(directory)
                result = {'phase': 'activated'}
            else:
                result = {'retire': retire, 'stage': stage}[args.action](directory)
            print(json.dumps(result, sort_keys=True), flush=True)
        if active:
            process, release_dir, environment, config, commit = active
            scaler.wait_for_controller(process, release_dir, environment, config)
            with (release.ROOT/'release.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                scaler.verify_marker(commit, config, allow_inactive=True)
                scaler.close_admission(release_dir, environment)
                release.require(process.returncode == 0, 'repair_controller_exit_unconfirmed')
                result = scaler.restore_cpu(release_dir, environment, config)
                scaler.marker(commit, config, False)
                print(json.dumps({'phase': 'service_complete', **result}), flush=True)
        return 0
    except Exception as exc:
        # A partial operation never clears a hold, reruns a rental or blindly
        # unpauses a controller. Root must inspect the durable phase and ledger.
        code = str(exc) if isinstance(exc, release.ReleaseError) else ''
        if not re.fullmatch(r'[a-z0-9_]{1,120}', code):
            code = 'preparation_repair_incomplete_reconcile_receipt'
        print(json.dumps({'verified': False, 'safe_error': code}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
