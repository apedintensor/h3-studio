#!/usr/bin/python3
"""One-use, protected handoff of an operator controller with pending deletions.

Run ``prepare --target-commit SHA --unit OLD.service`` after the compatible
app-only release. After the old supervisor exits, run ``start`` under a NEW
root-owned systemd unit with Restart=no, KillMode=process, TimeoutStopSec=infinity
and SendSIGKILL=no. Never repoint the old unit while it is still running.

For a later rollover, use ``prepare ... --journal-id TOKEN`` and bind the new
unit's ExecStart to ``start --journal-id TOKEN --successor-unit NEW.service``.
The original consumed journals remain untouched; a named rollover cannot be
resumed by an arbitrary process or replayed after launch intent is recorded.

This helper does not install/restart the independent guardian. Its separately
approved source installation must preserve the original guard config, requests
and receipts. Normal operator start/restore barriers remain unchanged.
"""
from __future__ import annotations

from contextlib import contextmanager
import inspect
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import release
import operator_capacity as host


JOURNAL_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')


def record_path(journal_id=None):
    if journal_id is None:
        return host.ROOT/'handoff.json'
    release.require(isinstance(journal_id, str) and JOURNAL_ID.fullmatch(journal_id),
                    'operator_handoff_journal_id_invalid')
    # This is a sibling in the existing root-protected directory, never a
    # caller-controlled path. Historical consumed journals are not overwritten.
    return host.ROOT/('handoff-'+journal_id+'.json')


def supervisor_arguments(argv, unit):
    """Allow only the installed helpers' exact supported foreground commands."""
    fixed = [
        ['/usr/bin/python3', '/opt/sixnine-release/operator_capacity.py', 'start'],
        ['/usr/bin/python3', '/opt/sixnine-release/operator_handoff.py', 'start'],
        ['/usr/bin/python3', '/opt/sixnine-release/operator_handoff_continuation.py', 'resume'],
    ]
    arguments = argv.split() if isinstance(argv, str) else []
    release.require(isinstance(argv, str) and argv == ' '.join(arguments),
                    'operator_handoff_supervisor_command_changed')
    if arguments in fixed:
        return arguments
    release.require(len(arguments) == 7
        and arguments[:4] == ['/usr/bin/python3', '/opt/sixnine-release/operator_handoff.py', 'start', '--journal-id']
        and JOURNAL_ID.fullmatch(arguments[4])
        and arguments[5:] == ['--successor-unit', unit],
        'operator_handoff_supervisor_command_changed')
    return arguments


@contextmanager
def locked():
    import fcntl
    with (release.ROOT/'release.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def supervisor(unit):
    release.require(isinstance(unit, str) and re.fullmatch(r'sixnine-[A-Za-z0-9_.-]+\.service', unit),
                    'operator_handoff_unit_invalid')
    try:
        raw = subprocess.run(['/usr/bin/systemctl', 'show', unit,
            '--property=MainPID,ExecMainPID,Restart,KillMode,ActiveState,ExecStart,User,FragmentPath,DropInPaths,TimeoutStopUSec,SendSIGKILL'],
            env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin'}, check=True,
            capture_output=True, timeout=20).stdout.decode()
        value = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise release.ReleaseError('operator_handoff_supervisor_unknown') from None
    release.require(value.get('Restart') == 'no' and value.get('KillMode') == 'process'
        and value.get('TimeoutStopUSec') == 'infinity' and value.get('SendSIGKILL') == 'no'
        and value.get('User') in ('', 'root') and value.get('MainPID', '').isdigit()
        and value.get('ExecMainPID', '').isdigit() and value.get('ExecStart'),
        'operator_handoff_supervisor_unsafe')
    # systemd appends mutable pid/start/stop/result fields to ExecStart. Bind
    # only the executable/argv and the exact protected unit + drop-in bytes.
    match = re.fullmatch(r'\{ path=([^;]+) ; argv\[\]=([^;]+) ; ignore_errors=no ;.*\}', value['ExecStart'])
    release.require(match is not None and match[1].strip() == '/usr/bin/python3',
        'operator_handoff_supervisor_command_changed')
    supervisor_arguments(match[2].strip(), unit)
    value['ExecStart'] = {'path':match[1].strip(), 'argv':match[2].strip()}
    paths = [value.get('FragmentPath', ''), *value.get('DropInPaths', '').split()]
    release.require(all(path.startswith(('/etc/systemd/system/', '/run/systemd/system/',
        '/usr/lib/systemd/system/', '/lib/systemd/system/')) and not any(c.isspace() for c in path)
        for path in paths), 'operator_handoff_unit_path_invalid')
    value['unit_files'] = {path:release.checksum(host.protected_file(Path(path))) for path in paths}
    return value


def only_controller(environment, pin):
    state = host.inspect_controller(environment, pin)
    release.require(state.get('Running') is True and state.get('Paused') is False
        and state.get('Restarting') is False and state.get('OOMKilled') is False,
        'operator_handoff_controller_not_running')
    # Docker uses the PID column to select this container's processes. Request
    # only PID and comm: omitting PID fails on the production daemon, while
    # default ps arguments would expose full command lines unnecessarily.
    rows = release.command(['top', pin['container_name'], '-eo', 'pid,comm'],
                           environment=environment, timeout=20).decode().splitlines()
    release.require(bool(rows) and rows[0].split() == ['PID', 'COMMAND'],
                    'operator_handoff_process_inspection_invalid')
    fields = [row.split() for row in rows[1:] if row.strip()]
    release.require(bool(fields) and all(len(row) == 2 and row[0].isdigit()
        and int(row[0]) > 0 for row in fields)
        and len({row[0] for row in fields}) == len(fields),
        'operator_handoff_process_inspection_invalid')
    names = [row[1] for row in fields]
    actual = [name for name in names if name not in ('docker-init', 'tini')]
    release.require(len(names) <= 2 and len(actual) == 1 and actual[0] in ('python', 'python3'),
                    'operator_handoff_owned_children_present')


def supervisor_client(unit, pin, directory, *, proc_root=Path('/proc')):
    """Bind the unit's process tree to this exact foreground Docker client."""
    pid = int(unit['MainPID'])
    root = proc_root
    todo, seen, matches = [pid], set(), []
    expected = ['--name', pin['container_name']]
    while todo:
        current = todo.pop()
        release.require(current not in seen and len(seen) < 32, 'operator_handoff_supervisor_tree_invalid')
        seen.add(current)
        argv = (root/str(current)/'cmdline').read_bytes().split(b'\0')
        args = [arg.decode() for arg in argv if arg]
        if current == pid:
            # supervisor() already validates the exact protected unit command;
            # bind the live ownership tree to that same command, not merely to
            # another permitted helper with an unrelated journal identity.
            expected_args = unit['ExecStart']['argv'].split()
            release.require(args == expected_args,
                            'operator_handoff_supervisor_process_changed')
        if any(args[i:i+2] == expected for i in range(len(args)-1)):
            release.require('compose' in args and 'run' in args and host.SERVICE in args
                and str(directory/'compose.yaml') in args
                and host.LABEL+'='+pin['prepared_hash'] in args,
                'operator_handoff_supervisor_client_mismatch')
            matches.append(current)
        children = (root/str(current)/'task'/str(current)/'children').read_text().split()
        release.require(all(child.isdigit() for child in children), 'operator_handoff_supervisor_tree_invalid')
        todo.extend(int(child) for child in children)
    release.require(bool(matches), 'operator_handoff_supervisor_does_not_own_controller')
    return {'supervisor_pid':pid, 'docker_client_pids':sorted(matches)}


def successor_supervisor(unit, journal_id, *, proc_root=Path('/proc'), pid=None):
    """A new journal must be started by its exact one-shot root supervisor."""
    record_path(journal_id)
    release.require(journal_id is not None, 'operator_handoff_explicit_journal_required')
    value = supervisor(unit)
    expected = ['/usr/bin/python3', '/opt/sixnine-release/operator_handoff.py', 'start',
                '--journal-id', journal_id, '--successor-unit', unit]
    process_id = os.getpid() if pid is None else pid
    release.require(value['MainPID'] == str(process_id) and value['ActiveState'] == 'active'
        and value['ExecStart'] == {'path':'/usr/bin/python3', 'argv':' '.join(expected)},
        'operator_handoff_successor_supervisor_mismatch')
    arguments = (proc_root/str(process_id)/'cmdline').read_bytes().split(b'\0')
    release.require([argument.decode() for argument in arguments if argument] == expected,
                    'operator_handoff_successor_process_mismatch')
    return value


def ledger_summary(rows, binding_hashes):
    """Pure validation of a read-only snapshot; no ledger writes or provider calls."""
    import hashlib
    import json
    import math
    def check(condition):
        if not condition:
            raise ValueError('operator_handoff_ledger_unsafe')
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                        allow_nan=False).encode()).hexdigest()
    def stable(row, excluded):
        return {key:value for key,value in row.items() if key not in excluded}
    check(all(rows['counts'].get(key) == 0 for key in ('active_jobs', 'unsafe_attempts', 'bound_workers')))
    intents = {row['id']:row for row in rows['intents']}
    nodes = {row['intent_id']:row for row in rows['nodes']}
    actions = {row['intent_id']:row for row in rows['actions']}
    live = []
    for intent in intents.values():
        if intent['state'] == 'destroyed':
            continue
        node, action = nodes.get(intent['id']), actions.get(intent['id'])
        check(intent['state'] == 'destroying' and bool(intent.get('provider_instance_id'))
              and node is not None and node['desired_state'] == 'stopped' and action is not None
              and type(action.get('destroy_started_at')) in (int, float)
              and math.isfinite(action['destroy_started_at'])
              and node['binding_hash'] == binding_hashes.get(node['binding_id']))
        reservations = [r for r in rows['reservations'] if r['reference_type'] == 'instance'
                        and r['reference_id'] == intent['id']]
        check(bool(reservations) and all(r['state'] == 'reserved' for r in reservations))
        live.append(intent['id'])
    active = {'accepted', 'running', 'waiting', 'unknown'}
    for command in rows['commands']:
        if command['state'] not in active:
            continue
        payload = command['payload']
        if command['kind'] == 'start':
            owned = [n for n in nodes.values() if n['command_id'] == command['id']]
            count = payload['selection']['node_count']
            check(type(count) is int and count > 0 and len(owned) == count
                  and {n['ordinal'] for n in owned} == set(range(count)))
            check(all(n['intent_id'] in intents and intents[n['intent_id']]['state'] in {'destroying', 'destroyed'}
                      and n['desired_state'] == 'stopped' for n in owned))
        else:
            check(command['kind'] in {'stop', 'drain'} and payload.get('node_id') in intents
                  and intents[payload['node_id']]['state'] in {'destroying', 'destroyed'})
    # Mutable observations/status may advance during graceful shutdown. Every
    # accepted identity, request, budget limit/reservation and original deadline
    # remains bound. Settlement is allowed only through the existing controller.
    immutable = {
        'intents':[stable(r, {'state', 'updated_at'}) for r in rows['intents']],
        'nodes':[{**{k:r[k] for k in ('intent_id', 'command_id', 'ordinal', 'binding_id', 'binding_hash', 'desired_state')},
                  'selection':r['payload'].get('selection'), 'hourly_cost_microusd':r['payload'].get('hourly_cost_microusd')}
                 for r in rows['nodes']],
        'actions':[stable(r, {'last_observation', 'last_observed_at', 'application_idle_since'}) for r in rows['actions']],
        'commands':[stable(r, {'state', 'reason_code', 'updated_at'}) for r in rows['commands']],
        'accounts':[stable(r, {'spent_microusd', 'reserved_microusd'}) for r in rows['accounts']],
        'reservations':[stable(r, {'state', 'actual_cost_microusd'}) for r in rows['reservations']],
        'policy':rows['policy'], 'gate':rows['gate'], 'limits':rows['limits'],
    }
    return {'schema_version':1, 'immutable_hash':digest(immutable), 'pending_ids':sorted(live),
            'accounting_hash':digest([rows['accounts'], rows['reservations']])}


def ledger_probe(directory, environment, *, require_removal_cadence=False):
    # Snapshot is consistent and read-only. No Repository/factory construction,
    # schema initialization, credentials, provider requests or mutation SQL.
    script = inspect.getsource(ledger_summary) + '''
from sqlalchemy import create_engine,text,select
from studio_platform.settings import Settings
from studio_platform.operator_runtime import create_registry
from studio_platform.repository import metadata
import json,time
registry=create_registry(RUNTIME)
engine=create_engine(Settings.from_environment().database_url)
with engine.connect() as conn:
 conn.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
 tables={'intents':'platform_instance_intents','nodes':'platform_operator_capacity_nodes',
 'actions':'platform_scaler_actions','commands':'platform_operator_capacity_commands',
 'accounts':'platform_budget_accounts','reservations':'platform_budget_reservations',
 'policy':'platform_operator_capacity_policy','gate':'platform_capacity_gate','limits':'platform_pool_limits'}
 rows={key:[dict(r) for r in conn.execute(select(metadata.tables[table]).order_by(metadata.tables[table].c[0])).mappings()] for key,table in tables.items()}
 queries={
 'active_jobs':"SELECT count(*) FROM platform_jobs WHERE status NOT IN ('succeeded','failed','cancelled')",
 'unsafe_attempts':"SELECT count(*) FROM platform_attempts WHERE status NOT IN ('succeeded','failed','cancelled','deferred') OR ((submission_started_at IS NOT NULL OR upstream_task_id IS NOT NULL) AND upstream_stopped != 1)",
 'bound_workers':"SELECT count(*) FROM platform_registered_workers WHERE current_job_id IS NOT NULL OR (state != 'retired' AND expires_at > :now)"}
 observed_at=time.time()
 rows['counts']={key:conn.execute(text(sql),{'now':observed_at}).scalar_one() for key,sql in queries.items()}
 print(json.dumps(ledger_summary(rows,{key:b.fingerprint for key,b in registry.bindings.items()})))
engine.dispose()
'''
    script = script.replace('create_registry(RUNTIME)', 'create_registry('+repr(host.RUNTIME.as_posix())+')')
    if require_removal_cadence:
        script = ('from studio_platform.scaler import REMOVAL_CHECK_INTERVAL_SECONDS\n'
                  'assert REMOVAL_CHECK_INTERVAL_SECONDS == 60\n') + script
    raw = host.compose(directory, environment, 'run', '--rm', '-T', '--no-deps', '--entrypoint',
                       'python', host.SERVICE, '-c', script, timeout=60)
    release.require(len(raw) <= 16384, 'operator_handoff_probe_invalid')
    value = json.loads(raw)
    release.require(value.get('schema_version') == 1 and isinstance(value.get('pending_ids'), list)
        and all(isinstance(value.get(key), str) and re.fullmatch(r'[a-f0-9]{64}', value[key])
                for key in ('immutable_hash', 'accounting_hash')), 'operator_handoff_probe_invalid')
    return value


def current_target(commit):
    value = host.approved_current()
    release.require(isinstance(commit, str) and release.SHA.fullmatch(commit) and value[0] == commit,
                    'operator_handoff_target_not_current')
    return value


def prepare(commit, unit, *, journal_id=None):
    with locked():
        path = record_path(journal_id)
        release.require(not path.exists() and not path.is_symlink(),
                        'operator_handoff_already_recorded')
        runtime, old, directory, environment = host.prepared()
        pin = host.checked_pin(old)
        release.require(pin.get('active') is True and pin.get('state') == 'running',
                        'operator_handoff_active_controller_required')
        target, _, _, image = current_target(commit)
        release.require(target != old['commit'], 'operator_handoff_target_unchanged')
        unit_state = supervisor(unit)
        release.require(int(unit_state['MainPID']) > 0 and unit_state['ActiveState'] == 'active',
                        'operator_handoff_supervisor_not_running')
        only_controller(environment, pin)
        client = supervisor_client(unit_state, pin, directory)
        proof = host.receipt(pin, fresh=True)
        release.require(proof.get('state') in ('running', 'degraded'), 'operator_handoff_receipt_not_running')
        ledger = ledger_probe(directory, environment)
        release.require(bool(ledger['pending_ids']), 'operator_handoff_pending_deletion_required')
        value = {'schema_version':1, 'phase':'drain_requested', 'target_commit':target, 'target_image_id':image,
            'old_prepared':old, 'old_pin':pin, 'old_overlay':release._protected_json(host.ROOT/'overlay.json'),
            'supervisor_unit':unit, 'supervisor':unit_state, 'supervisor_client':client,
            'ledger':ledger, 'prepared_at':time.time()}
        if journal_id is not None:
            value['journal_id'] = journal_id
        # Intent precedes admission/TERM side effects. Any ambiguous outcome is
        # retained for inspection; prepare never retries by removing this file.
        host.atomic(path, value)
        host.request_drain(directory, environment, pin)
        return {'state':'drain_requested', 'target_commit':target, 'pending_deletions':len(ledger['pending_ids'])}


def successor(*, journal_id=None):
    """Called under release.lock; preserve all old evidence before replacing pins."""
    path = record_path(journal_id)
    value = release._protected_json(path, maximum=2*1024**2)
    release.require(value.get('journal_id') == journal_id, 'operator_handoff_journal_identity_changed')
    release.require(value.get('schema_version') == 1 and value.get('phase') == 'drain_requested',
                    'operator_handoff_not_ready_or_already_launched')
    runtime, old, old_directory, old_environment = host.prepared()
    release.require(old == value['old_prepared'] and
        release._protected_json(host.ROOT/'overlay.json') == value['old_overlay'], 'operator_handoff_configuration_changed')
    pin = host.checked_pin(old)
    release.require(pin == {**value['old_pin'], 'admission':'closed'}, 'operator_handoff_old_pin_changed')
    unit = supervisor(value['supervisor_unit'])
    release.require(unit['MainPID'] == '0' and unit['ActiveState'] in ('inactive', 'failed')
        and unit['ExecMainPID'] == value['supervisor']['MainPID']
        and unit['ExecStart'] == value['supervisor']['ExecStart']
        and unit['unit_files'] == value['supervisor']['unit_files'], 'operator_handoff_old_supervisor_not_retired')
    state = host.inspect_controller(old_environment, pin)
    release.require(state.get('Running') is False and state.get('Restarting') is False
        and state.get('Paused') is False and state.get('OOMKilled') is False
        and state.get('Status') == 'exited' and state.get('ExitCode') == 0,
        'operator_handoff_old_exit_unconfirmed')
    proof = host.receipt(pin)
    release.require(proof.get('state') == 'shutdown_complete' and proof.get('local_connections_released') is True,
                    'operator_handoff_local_ownership_unconfirmed')
    host.no_competing_controller(old_environment)
    ledger = ledger_probe(old_directory, old_environment)
    release.require(ledger['immutable_hash'] == value['ledger']['immutable_hash']
        and set(ledger['pending_ids']) <= set(value['ledger']['pending_ids']), 'operator_handoff_ledger_changed')
    if journal_id is not None:
        release.require(ledger['accounting_hash'] == value['ledger']['accounting_hash'],
                        'operator_handoff_accounting_changed')
    commit, directory, environment, image = current_target(value['target_commit'])
    release.require(image == value['target_image_id'], 'operator_handoff_target_image_changed')
    prepared = {**old, 'commit':commit, 'image_id':image}
    next_pin = host.pin_for(prepared)
    # A launch intent is one-use even after a crash or unknown stdin delivery.
    value.update(phase='launch_intent', retired_proof=proof, retired_ledger=ledger, successor_pin=next_pin,
                 launch_intent_at=time.time())
    host.atomic(path, value)
    owners = value['old_overlay']['services']['app']['environment']['SIXNINE_OPERATOR_CAPACITY_OWNERS']
    host.atomic(host.ROOT/'overlay.json', host.overlay(environment['SIXNINE_IMAGE'], host.default_profile(), runtime, owners=owners))
    host.atomic(host.ROOT/'prepared.json', prepared)
    host.atomic(host.ROOT/'active.json', next_pin)
    version = release.command(['compose', 'version', '--short'], environment=environment).decode().strip()
    host.validate_rendered(json.loads(host.compose(directory, environment, 'config', '--format', 'json')),
                           directory, version, environment['SIXNINE_IMAGE'], host.default_profile(), runtime, owners=owners)
    # Recheck through the replacement image before giving it credentials. This
    # verifies compatible metadata and preserves every old rental identity.
    after = ledger_probe(directory, environment, require_removal_cadence=True)
    release.require(after == ledger, 'operator_handoff_ledger_changed_before_launch')
    return runtime, prepared, directory, environment, next_pin


def start(*, clock=time.monotonic, sleep=time.sleep, successor_factory=None, journal_id=None, successor_unit=None):
    stopping = [False]
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.getsignal(sig)
        signal.signal(sig, lambda *_: stopping.__setitem__(0, True))
    process = pin = directory = environment = None
    try:
        with locked():
            release.require(not stopping[0], 'operator_handoff_interrupted')
            journal_path = record_path(journal_id)
            if journal_id is not None:
                release.require(successor_factory is None, 'operator_handoff_custom_factory_forbidden')
                identity = successor_supervisor(successor_unit, journal_id)
                value = release._protected_json(journal_path, maximum=2*1024**2)
                release.require(value.get('journal_id') == journal_id and value.get('phase') == 'drain_requested',
                                'operator_handoff_not_ready_or_already_launched')
                host.atomic(journal_path, {**value, 'successor_supervisor_unit':successor_unit, 'successor_supervisor':identity})
                runtime, prepared, directory, environment, pin = successor(journal_id=journal_id)
            else:
                release.require(successor_unit is None, 'operator_handoff_successor_unit_without_journal')
                runtime, prepared, directory, environment, pin = (successor_factory or successor)()
            process = host.launch(directory, environment, runtime, pin)
            deadline = clock()+120
            while True:
                release.require(not stopping[0] and process.poll() is None, 'operator_handoff_startup_unconfirmed')
                try:
                    state = host.inspect_controller(environment, pin)
                except release.ReleaseError as error:
                    # Compose creates the named container asynchronously. Retry
                    # only Docker command failures; identity/format gates stay fatal.
                    if str(error) != 'container_operation_failed_no_details_logged':
                        raise
                    release.require(clock() < deadline, 'operator_handoff_startup_timeout')
                    sleep(2)
                    continue
                try:
                    proof = host.receipt(pin, fresh=True)
                except (OSError, ValueError, release.ReleaseError):
                    proof = {}
                if (state.get('Running') is True and state.get('Restarting') is False
                        and state.get('OOMKilled') is False and proof.get('state') == 'running'
                        and proof.get('controller_id') != release._protected_json(journal_path)['old_pin'].get('controller_id')):
                    pin = {**pin, 'state':'running', 'controller_id':proof['controller_id']}
                    host.atomic(host.ROOT/'active.json', pin)
                    # Existing app-only activation verifies the current release.
                    # Use its protected API owner settings without editing any
                    # immutable deployment binding or execution configuration.
                    app = host.app_overlay(admission='open')
                    path = host.ROOT/'handoff-app-overlay.json'
                    host.atomic(path, app)
                    args = ['compose', '--project-directory', str(directory), '-f', str(directory/'compose.yaml'), '-f', str(path)]
                    version = release.command(['compose', 'version', '--short'], environment=environment).decode().strip()
                    rendered = json.loads(release.command([*args, 'config', '--format', 'json'], environment=environment))
                    host.validate_app_rendered(rendered, directory, version, environment['SIXNINE_IMAGE'], app)
                    release.command([*args, 'up', '-d', '--no-deps', 'app'], environment=environment)
                    release.wait_ready(directory, environment)
                    pin = {**pin, 'admission':'open'}
                    host.atomic(host.ROOT/'active.json', pin)
                    value = release._protected_json(journal_path, maximum=2*1024**2)
                    host.atomic(journal_path, {**value, 'phase':'running', 'controller_id':proof['controller_id']})
                    break
                release.require(clock() < deadline, 'operator_handoff_startup_timeout')
                sleep(2)
        while process.poll() is None:
            if stopping[0]:
                with locked():
                    host.request_drain(directory, environment, pin)
                stopping[0] = False
            sleep(2)
        with locked():
            release.require(process.returncode == 0, 'operator_handoff_exit_unknown')
            return host.restore(directory, environment, prepared)
    except Exception:
        if pin is not None:
            try:
                with locked():
                    host.request_drain(directory, environment, pin)
            except Exception:
                pass
        raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'start'))
    parser.add_argument('--target-commit')
    parser.add_argument('--unit')
    parser.add_argument('--journal-id')
    parser.add_argument('--successor-unit')
    args = parser.parse_args(argv)
    try:
        release.check_host(release.ROOT)
        if args.action == 'prepare':
            release.require(args.successor_unit is None, 'operator_handoff_successor_unit_start_only')
            result = prepare(args.target_commit, args.unit, journal_id=args.journal_id)
        else:
            release.require(args.target_commit is None and args.unit is None, 'operator_handoff_record_required')
            result = start(journal_id=args.journal_id, successor_unit=args.successor_unit)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({'state':'incomplete', 'barrier_retained':True,
            'code':str(error) if isinstance(error, release.ReleaseError) else 'operator_handoff_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
