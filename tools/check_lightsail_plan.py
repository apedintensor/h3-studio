"""Validate an explicit, non-secret Lightsail host plan. Never calls AWS."""
from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import re


def validate(plan):
    required = {"region", "InstanceName", "AvailabilityZone", "Capacity", "KeyPairName", "AdminIpv4Cidr", "DailySnapshot"}
    if not isinstance(plan, dict) or set(plan) != required or any(not isinstance(v, str) for v in plan.values()):
        raise ValueError("plan_requires_exact_nonsecret_fields")
    if plan["region"] not in {"ap-southeast-1", "ap-southeast-2"}:
        raise ValueError("region_not_reviewed")
    if not re.fullmatch(re.escape(plan["region"])+"[abc]", plan["AvailabilityZone"]):
        raise ValueError("availability_zone_must_match_region_and_live_inventory")
    if not re.fullmatch(r"sixnine-[a-z0-9-]{1,40}", plan["InstanceName"]):
        raise ValueError("instance_name_invalid")
    if plan["Capacity"] not in {"control8gb", "cpu16gb"} or plan["DailySnapshot"] not in {"disabled", "enabled"}:
        raise ValueError("capacity_or_snapshot_choice_invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", plan["KeyPairName"]):
        raise ValueError("public_key_pair_name_required_not_key_content")
    try:
        network = ipaddress.IPv4Network(plan["AdminIpv4Cidr"], strict=True)
        if network.prefixlen != 32 or not network.network_address.is_global:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("one_public_admin_ipv4_32_required") from None
    return {"region": plan["region"], "parameters": [{"ParameterKey": k, "ParameterValue": plan[k]}
            for k in sorted(required-{"region"})], "status": "offline_structure_validated_not_deployed",
            "remaining_checks": ["regional_offer_and_zone_available", "key_pair_exists_in_region", "admin_ip_ownership",
                                 "nonroot_aws_identity", "approved_cost", "encrypted_backups_and_runtime_secrets",
                                 "fixed_deployment_runner_reachability", "cloudformation_lint_and_guard"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="Reviewed JSON containing only the seven documented non-secret fields")
    args = parser.parse_args()
    try:
        if args.plan.stat().st_size > 16*1024:
            raise ValueError("plan_too_large")
        result = validate(json.loads(args.plan.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError):
        parser.exit(2, "Invalid host plan; review documented fields and public /32. No AWS request was made.\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
