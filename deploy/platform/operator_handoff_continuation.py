#!/usr/bin/python3
"""One-use continuation of an exact, cleanly stopped handoff successor.

approve --unit FAILED.service records fresh identity/ownership/ledger evidence.
resume runs under a separate root unit with Restart=no. Archive the stopped
container by rename, preserving its ID and original launch journal; launch one
new CPU controller through the existing credential loader. Unknown outcomes
retain the continuation intent and cannot be replayed. No GPU is provisioned
by this helper; only the retained controller interprets original commands.
"""
from __future__ import annotations
import json
import math
import re
import subprocess
import time
from pathlib import Path
import release
import operator_capacity as host
import operator_handoff as handoff


def record_path():
    return host.ROOT/'handoff-continuation.json'


def failed_supervisor(unit):
    release.require(isinstance(unit,str) and re.fullmatch(r'sixnine-[A-Za-z0-9_.-]+\.service',unit),
                    'operator_continuation_unit_invalid')
    try:
        raw = subprocess.run(['/usr/bin/systemctl','show',unit,
            '--property=MainPID,ExecMainPID,Restart,KillMode,ActiveState,ExecStart,User,FragmentPath,DropInPaths,TimeoutStopUSec,SendSIGKILL'],
            env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin'},check=True,capture_output=True,timeout=20).stdout.decode()
        value = dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
    except (OSError,subprocess.SubprocessError,ValueError):
        raise release.ReleaseError('operator_continuation_supervisor_unknown') from None
    release.require(value.get('MainPID')=='0' and value.get('ExecMainPID','').isdigit()
        and int(value['ExecMainPID'])>0 and value.get('ActiveState') in ('failed','inactive')
        and value.get('Restart')=='no' and value.get('KillMode')=='process'
        and value.get('TimeoutStopUSec')=='infinity' and value.get('SendSIGKILL')=='no'
        and value.get('User') in ('','root'),'operator_continuation_supervisor_not_retired')
    match = re.fullmatch(r'\{ path=([^;]+) ; argv\[\]=([^;]+) ; ignore_errors=no ;.*\}',value.get('ExecStart',''))
    release.require(match is not None and match[1].strip()=='/usr/bin/python3'
        and match[2].strip()=='/usr/bin/python3 /opt/sixnine-release/operator_handoff.py start',
        'operator_continuation_supervisor_command_changed')
    value['ExecStart']={'path':match[1].strip(),'argv':match[2].strip()}
    paths=[value.get('FragmentPath',''),*value.get('DropInPaths','').split()]
    release.require(all(p.startswith(('/etc/systemd/system/','/run/systemd/system/',
        '/usr/lib/systemd/system/','/lib/systemd/system/')) and not any(c.isspace() for c in p)
        for p in paths),'operator_continuation_unit_path_invalid')
    value['unit_files']={p:release.checksum(host.protected_file(Path(p))) for p in paths}
    return value


def stopped_container(environment,pin):
    state=host.inspect_controller(environment,pin)
    release.require(state.get('Running') is False and state.get('Restarting') is False
        and state.get('Paused') is False and state.get('OOMKilled') is False
        and state.get('Status')=='exited' and state.get('ExitCode')==0,
        'operator_continuation_exit_unconfirmed')
    rows=json.loads(release.command(['inspect',pin['container_name']],environment=environment,timeout=20))
    release.require(isinstance(rows,list) and len(rows)==1 and isinstance(rows[0].get('Id'),str)
        and re.fullmatch(r'[a-f0-9]{64}',rows[0]['Id']),'operator_continuation_container_unknown')
    row=rows[0];labels=row.get('Config',{}).get('Labels',{})
    release.require(row.get('Name')=='/'+pin['container_name'] and row.get('Image')==pin['image_id']
        and labels.get('com.docker.compose.project')=='sixnine-platform'
        and labels.get('com.docker.compose.service')==host.SERVICE
        and labels.get(host.LABEL)==pin['prepared_hash'] and row.get('State')==state,
        'operator_continuation_container_changed')
    return {'id':row['Id'],'state':state}


def snapshot(unit):
    value=release._protected_json(handoff.record_path(),maximum=2*1024**2)
    release.require(value.get('schema_version')==1 and value.get('phase')=='launch_intent',
                    'operator_continuation_launch_intent_required')
    runtime,prepared,directory,environment=host.prepared()
    old=value['old_prepared']
    release.require(prepared=={**old,'commit':value['target_commit'],'image_id':value['target_image_id']},
                    'operator_continuation_configuration_changed')
    pin=host.checked_pin(prepared)
    release.require(pin==value['successor_pin'] and pin.get('state')=='launching'
        and pin.get('admission')=='closed','operator_continuation_pin_changed')
    target=handoff.current_target(value['target_commit'])
    release.require(target[3]==value['target_image_id'],'operator_continuation_target_changed')
    original_unit=handoff.supervisor(value['supervisor_unit'])
    release.require(original_unit['MainPID']=='0' and original_unit['ActiveState'] in ('inactive','failed')
        and original_unit['ExecMainPID']==value['supervisor']['MainPID']
        and original_unit['ExecStart']==value['supervisor']['ExecStart']
        and original_unit['unit_files']==value['supervisor']['unit_files'],
        'operator_continuation_original_supervisor_not_retired')
    stopped_container(environment,value['old_pin'])
    supervisor=failed_supervisor(unit)
    container=stopped_container(environment,pin)
    proof=host.receipt(pin)
    release.require(proof.get('state')=='shutdown_complete' and proof.get('local_connections_released') is True
        and isinstance(proof.get('controller_id'),str)
        and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',proof['controller_id'])
        and proof['controller_id']!=value['old_pin'].get('controller_id')
        and type(proof.get('observed_at')) in (int,float) and math.isfinite(proof['observed_at'])
        and type(value.get('launch_intent_at')) in (int,float) and math.isfinite(value['launch_intent_at'])
        and proof['observed_at']>=value['launch_intent_at'],
        'operator_continuation_local_ownership_unconfirmed')
    host.no_competing_controller(environment)
    ledger=handoff.ledger_probe(directory,environment,require_removal_cadence=True)
    release.require(ledger['immutable_hash']==value['ledger']['immutable_hash']
        and set(ledger['pending_ids'])<=set(value['ledger']['pending_ids']),
        'operator_continuation_ledger_changed')
    return runtime,prepared,directory,environment,pin,{
        'handoff_hash':release.canonical_hash(value),'prepared':prepared,'pin':pin,
        'failed_unit':unit,'failed_supervisor':supervisor,'container':container,
        'stopped_receipt':proof,'ledger':ledger}


def approve(unit):
    with handoff.locked():
        release.require(not record_path().exists() and not record_path().is_symlink(),
                        'operator_continuation_already_recorded')
        *_,evidence=snapshot(unit)
        host.atomic(record_path(),{'schema_version':1,'phase':'approved',
            'approved_at':time.time(),'evidence':evidence})
        return {'state':'continuation_approved_not_started','pending_deletions':len(evidence['ledger']['pending_ids'])}


def successor():
    """Called exactly once under handoff's release lock."""
    approval=release._protected_json(record_path(),maximum=2*1024**2)
    release.require(approval.get('schema_version')==1 and approval.get('phase')=='approved',
                    'operator_continuation_not_approved_or_consumed')
    runtime,prepared,directory,environment,pin,evidence=snapshot(approval['evidence']['failed_unit'])
    release.require(evidence==approval['evidence'],'operator_continuation_evidence_changed')
    archived=pin['container_name']+'-failed-'+evidence['container']['id'][:12]
    # Durable intent before rename/create/credential side effects. No retry
    # after an uncertain rename, launch, or credential delivery.
    approval={**approval,'phase':'archive_intent','archived_container_name':archived,'archive_intent_at':time.time()}
    host.atomic(record_path(),approval)
    release.command(['rename',evidence['container']['id'],archived],environment=environment,timeout=20)
    archived_proof=stopped_container(environment,{**pin,'container_name':archived})
    release.require(archived_proof==evidence['container'],'operator_continuation_archive_changed')
    host.no_competing_controller(environment)
    ledger=handoff.ledger_probe(directory,environment,require_removal_cadence=True)
    release.require(ledger==evidence['ledger'],'operator_continuation_ledger_changed_before_launch')
    host.atomic(record_path(),{**approval,'phase':'launch_intent','launch_intent_at':time.time()})
    return runtime,prepared,directory,environment,pin


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('approve','resume'))
    parser.add_argument('--unit')
    args=parser.parse_args(argv)
    try:
        release.check_host(release.ROOT)
        if args.action=='approve':
            result=approve(args.unit)
        else:
            release.require(args.unit is None,'operator_continuation_record_required')
            result=handoff.start(successor_factory=successor)
        print(json.dumps(result,sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({'state':'incomplete','barrier_retained':True,
            'code':str(error) if isinstance(error,release.ReleaseError) else 'operator_continuation_failed'}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
