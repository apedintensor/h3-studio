"""Operator-only encrypted S3 backup copy. Default action is an offline plan."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from .backup import _regular
from .backup_remote import BackupTarget, RemoteBackupError, copy_backup, need, plan_copy, restore_copy


def aws_client(target):
    # The existing host role may only assume the explicit backup role. Never
    # inject its short-lived credentials into the app environment or persist them.
    import boto3
    from botocore.config import Config
    session=boto3.Session(region_name=target.region)
    limits=Config(connect_timeout=10, read_timeout=60,
                  retries={"mode":"standard", "total_max_attempts":1}, signature_version="s3v4")
    identity_endpoint="https://sts."+target.region+".amazonaws.com"
    try:
        sts=session.client("sts",config=limits,endpoint_url=identity_endpoint)
        identity=sts.get_caller_identity()
        need(identity.get("Account")==target.account_id,"backup_caller_account_mismatch")
        role=sts.assume_role(RoleArn=target.role_arn,RoleSessionName="sixnine-backup-copy",DurationSeconds=3600)
        expected="arn:aws:sts::"+target.account_id+":assumed-role/"+target.role_arn.rsplit("/",1)[-1]+"/sixnine-backup-copy"
        need(role.get("AssumedRoleUser",{}).get("Arn")==expected,"backup_assumed_role_mismatch")
        credentials=role.get("Credentials",{})
        need(all(isinstance(credentials.get(k),str) and credentials[k] for k in
                 ("AccessKeyId","SecretAccessKey","SessionToken")),"backup_role_credentials_unconfirmed")
        expires=credentials.get("Expiration")
        need(isinstance(expires,datetime) and expires.tzinfo is not None
             and 0<(expires-datetime.now(timezone.utc)).total_seconds()<=3660,"backup_role_expiry_invalid")
        scoped=boto3.Session(region_name=target.region,aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],aws_session_token=credentials["SessionToken"])
        assumed=scoped.client("sts",config=limits,endpoint_url=identity_endpoint).get_caller_identity()
        need(assumed.get("Account")==target.account_id and assumed.get("Arn")==expected,"backup_assumed_role_mismatch")
        return scoped.client("s3",region_name=target.region,
            endpoint_url="https://s3."+target.region+".amazonaws.com",config=limits)
    except RemoteBackupError:
        raise
    except Exception:
        raise RemoteBackupError("backup_runtime_identity_unconfirmed") from None


def read_json(path, *, maximum):
    return json.loads(_regular(Path(path),max_bytes=maximum).read_bytes())


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("plan","copy","restore"),nargs="?",default="plan")
    parser.add_argument("--target",required=True,type=Path,help="Explicit nonsecret account/region/bucket/prefix/KMS-key/backup-role JSON")
    parser.add_argument("--backup",type=Path)
    parser.add_argument("--receipts",type=Path,help="New private action receipt directory for one copy")
    parser.add_argument("--receipt",type=Path,help="Exact immutable completion receipt retained independently of the source host")
    parser.add_argument("--download",type=Path,help="New private readback directory")
    parser.add_argument("--destination",type=Path,help="New isolated restore directory; never the live application data")
    parser.add_argument("--execute",action="store_true")
    args=parser.parse_args(argv)
    try:
        target=BackupTarget(**read_json(args.target,maximum=16384))
        need(args.action=="plan" or args.execute,"explicit_backup_transport_execution_required")
        if args.action=="plan":
            need(args.backup is not None,"backup_source_required")
            plan=plan_copy(args.backup,target)
            result=dict(state="offline_backup_copy_plan",objects=plan["objects"],bytes=plan["bytes"],
                        snapshot_manifest_sha256=plan["snapshot_manifest_sha256"],cloud_operations=0)
        elif args.action=="copy":
            need(args.backup is not None and args.receipts is not None,"backup_source_and_new_receipts_required")
            result=copy_backup(args.backup,target,args.receipts,client=aws_client(target))
            result={k:result[k] for k in ("phase","completion_sha256","completion_bytes",
                "completion_version_id","snapshot_manifest_sha256","objects","bytes","restore_verified")}
        else:
            need(all(x is not None for x in (args.receipt,args.download,args.destination)),"backup_restore_paths_required")
            receipt=read_json(args.receipt,maximum=16384)
            result=restore_copy(target,receipt,args.download,args.destination,client=aws_client(target))
        print(json.dumps(result,sort_keys=True));return 0
    except Exception as error:
        code=str(error) if isinstance(error,RemoteBackupError) else "backup_transport_refused_or_unconfirmed"
        print(json.dumps(dict(state="refused_or_unconfirmed",code=code,automatic_retry=False)));return 1


if __name__=="__main__":
    raise SystemExit(main())
