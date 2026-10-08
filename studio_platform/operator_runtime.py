"""Trusted local-file composition for the existing Lium rental authority.

Construction reads bounded deployment metadata only. Credentials remain lazy;
inventory is queried only by an explicit offers request and rents only by the
fenced controller. This file neither initializes budgets nor enables a pool.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from .lium_provider import LiumError, LiumManifest, LiumProvider
from .operator_capacity import (DeploymentBinding, OperatorCapacity, OperatorError,
    OperatorRegistry, require)
from .operator_controller import OperatorController
from .repository import Repository, Scope
from .runtime_catalog import engine_manifest, get_profile, model_for, public_catalog
from .scaler import LaunchSpec

MINIMUM_LIUM_TTL_SECONDS = 3780  # Provider hour + 60s margin + 120s queue allowance.
SOURCE_NAMES = {"wangp-bootstrap.py", "wangp-manifest.json", "wangp-runtime.json", "wangp-package.tar.gz"}


def _absolute(value, *, exists=True, directory=False):
    require(isinstance(value,str) and value and not any(c in value for c in "\r\n\x00"),
        "operator_runtime_path_invalid",422)
    path=Path(value)
    require(path.is_absolute(),"operator_runtime_absolute_path_required",422)
    try:
        # Do not silently trust a symlink/junction or a relative parent escape.
        require(".." not in path.parts,"operator_runtime_path_invalid",422)
        for part in (path,*path.parents):
            require(not part.is_symlink() and not (hasattr(part,"is_junction") and part.is_junction()),
                "operator_runtime_link_forbidden",422)
            if part.exists():
                require(not getattr(part.lstat(),"st_file_attributes",0)&getattr(stat,"FILE_ATTRIBUTE_REPARSE_POINT",0x400),
                    "operator_runtime_link_forbidden",422)
        if exists:
            require(path.is_dir() if directory else path.is_file(),"operator_runtime_path_unavailable",422)
        return path
    except OSError:
        raise OperatorError("operator_runtime_path_unavailable",422) from None


def _read(path, maximum, *, protected=False):
    source=_absolute(str(path))
    try:
        with source.open("rb") as handle:
            info=os.fstat(handle.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and 0<info.st_size<=maximum,
                "operator_runtime_file_invalid",422)
            if os.name!="nt":
                forbidden=stat.S_IWGRP|stat.S_IWOTH|(stat.S_IRWXO if protected else 0)
                require(not info.st_mode&forbidden and info.st_uid in (0,os.geteuid()),
                    "operator_runtime_file_permissions",422)
            raw=handle.read(maximum+1)
        require(len(raw)<=maximum,"operator_runtime_file_too_large",422)
        return raw
    except OSError:
        raise OperatorError("operator_runtime_file_unavailable",422) from None


def _decode(raw):
    def unique(pairs):
        value={}
        for key,item in pairs:
            require(key not in value,"operator_runtime_duplicate_field",422)
            value[key]=item
        return value
    try:
        value=json.loads(raw,object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        require(isinstance(value,dict),"operator_runtime_schema_invalid",422)
        return value
    except (ValueError,UnicodeError):
        raise OperatorError("operator_runtime_json_invalid",422) from None


def load_runtime_config(path):
    source=_absolute(str(path))
    value=_decode(_read(source,128*1024,protected=True))
    required={"schema_version","registry_file","work_dir","ssh_key_file","known_hosts_file","port_start"}
    optional={"credential_source","secret_arn","secret_version_id","trust_first_host_key","runtime_python"}
    require(required<=set(value) and not set(value)-required-optional
        and type(value["schema_version"]) is int and value["schema_version"]==1,
        "operator_runtime_schema_invalid",422)
    for name in ("registry_file","ssh_key_file"):
        _absolute(value[name])
    _absolute(value["work_dir"],directory=True)
    trust=value.get("trust_first_host_key",False)
    require(type(trust) is bool,"operator_runtime_trust_invalid",422)
    hosts=_absolute(value["known_hosts_file"],exists=not trust)
    _absolute(str(hosts.parent),directory=True)
    require(type(value["port_start"]) is int and 1024<=value["port_start"]<=64511,
        "operator_runtime_port_invalid",422)
    python=value.get("runtime_python","/venv/main/bin/python")
    require(python=="/venv/main/bin/python","operator_runtime_python_unqualified",422)
    credential=value.get("credential_source","central_registry")
    require(credential in {"central_registry","aws_runtime"},"operator_runtime_credential_source_invalid",422)
    if credential=="central_registry":
        require(not {"secret_arn","secret_version_id"}&set(value),"operator_runtime_credential_conflict",422)
    else:
        require({"secret_arn","secret_version_id"}<=set(value),"operator_runtime_credential_reference_missing",422)
        from .lium_runtime_aws import AwsLiumLoader
        try: AwsLiumLoader(value["secret_arn"],value["secret_version_id"])
        except ValueError: raise OperatorError("operator_runtime_credential_reference_invalid",422) from None
    return {**value,"config_path":str(source),"credential_source":credential,
            "trust_first_host_key":trust,"runtime_python":python}


def _bindings(path):
    value=_decode(_read(path,1024*1024,protected=True))
    require(set(value)=={"schema_version","bindings"} and type(value["schema_version"]) is int
        and value["schema_version"]==1 and isinstance(value["bindings"],list) and len(value["bindings"])<=128,
        "operator_registry_schema_invalid",422)
    output=[]
    for entry in value["bindings"]:
        try:
            item=dict(entry)
            item["launch"]=LaunchSpec(**item["launch"])
            item["scope"]=Scope(**item["scope"])
            for key in ("recipe_ids","budget_account_ids"):
                require(isinstance(item[key],list),"operator_registry_binding_invalid",422)
                item[key]=tuple(item[key])
            output.append(DeploymentBinding(**item))
        except OperatorError: raise
        except (KeyError,TypeError,ValueError):
            raise OperatorError("operator_registry_binding_invalid",422) from None
    return output


def _validate_sources(binding):
    boot=binding.boot
    require(set(boot)=={"provider_manifest_file","source_dirs","source_sha256"},
        "operator_runtime_boot_schema_invalid",422)
    dirs,hashes=boot["source_dirs"],boot["source_sha256"]
    require(isinstance(dirs,list) and isinstance(hashes,list)
        and len(dirs)==len(hashes)==binding.execution_slots,
        "operator_runtime_slot_sources_invalid",422)
    roots=[_absolute(value,directory=True) for value in dirs]
    require(len({str(p.resolve()).casefold() if os.name=="nt" else str(p.resolve()) for p in roots})==len(roots),
        "operator_runtime_slot_sources_not_unique",422)
    try:
        expected=engine_manifest(binding.runtime_profile_id,binding.mode)
    except ValueError:
        raise OperatorError("operator_runtime_profile_invalid",422) from None
    require(expected.digest==binding.engine_manifest_digest,"operator_runtime_engine_mismatch",422)
    for index,(root,digests) in enumerate(zip(roots,hashes)):
        require(isinstance(digests,dict) and set(digests)==SOURCE_NAMES
            and all(isinstance(v,str) and re.fullmatch(r"[0-9a-f]{64}",v) for v in digests.values()),
            "operator_runtime_source_hashes_invalid",422)
        content={}
        for name in sorted(SOURCE_NAMES):
            raw=_read(root/name,16*1024*1024 if name.endswith(".gz") else 512*1024)
            require(hashlib.sha256(raw).hexdigest()==digests[name],"operator_runtime_source_changed",422)
            content[name]=raw
        require(_decode(content["wangp-manifest.json"])==expected.document,
            "operator_runtime_engine_mismatch",422)
        runtime=_decode(content["wangp-runtime.json"])
        require(runtime.get("deployment_profile_id")==binding.runtime_profile_id
            and type(runtime.get("profile_slot_index")) is int and runtime["profile_slot_index"]==index
            and type(runtime.get("expected_host_gpus")) is int and runtime["expected_host_gpus"]==binding.gpu_count
            and type(runtime.get("port")) is int and runtime["port"]==8199+index,
            "operator_runtime_slot_identity_mismatch",422)
        require(runtime.get("source_bundle_sha256")==digests["wangp-package.tar.gz"],
            "operator_runtime_bundle_mismatch",422)


def _manifest(binding):
    try:
        value=_decode(_read(binding.boot["provider_manifest_file"],128*1024,protected=True))
        manifest=LiumManifest(**value)
        profile=get_profile(binding.runtime_profile_id)
        model=model_for(binding.runtime_profile_id,binding.mode)
    except OperatorError: raise
    except (KeyError,TypeError,ValueError,LiumError):
        raise OperatorError("operator_runtime_provider_manifest_invalid",422) from None
    require(binding.model_id==profile["model_id"] and binding.recipe_ids==(model["generation_recipe_id"],)
        and binding.gpu_type in profile["gpu_models"] and binding.gpu_count in profile["gpu_count_options"]
        and binding.execution_slots==binding.gpu_count,"operator_runtime_profile_topology_mismatch",422)
    require(manifest.server_side_selection and not manifest.executor_id and not binding.launch.offer_id,
        "operator_runtime_server_selection_required",422)
    require(manifest.configuration_id==binding.configuration_id and manifest.model_id==binding.model_id
        and manifest.template_id==binding.launch.image_id and manifest.region==binding.launch.region
        and manifest.gpu_count==binding.gpu_count and manifest.execution_slots==binding.execution_slots
        and manifest.compatible_gpu_names==(binding.gpu_type,),"operator_runtime_launch_mismatch",422)
    require(manifest.allow_preflight_only_price_cap and binding.expires_at<=manifest.approved_until,
        "operator_runtime_provider_approval_mismatch",422)
    require(binding.min_ttl_seconds>=MINIMUM_LIUM_TTL_SECONDS
        and binding.max_ttl_seconds<=manifest.termination_hours*3600+60
        and 1<=manifest.termination_hours<=4,"operator_runtime_provider_ttl_mismatch",422)
    require(binding.hourly_cost_microusd==manifest.max_price_per_gpu_hour_microusd*binding.gpu_count
        and binding.reservation_per_node_microusd>=binding.hourly_cost_microusd*manifest.termination_hours
        and binding.reservation_per_node_microusd>=math.ceil(binding.hourly_cost_microusd*binding.max_ttl_seconds/3600),
        "operator_runtime_cost_binding_mismatch",422)
    require("min_cpu_cores" not in binding.filters,"operator_runtime_cpu_filter_unsupported",422)
    expected_filters={"min_ram_gib":manifest.minimum_ram_gib,"min_disk_gib":manifest.minimum_disk_gib,
        "max_price_per_gpu_hour_microusd":manifest.max_price_per_gpu_hour_microusd,
        "allowed_countries":list(manifest.allowed_countries)}
    if manifest.min_download_mbps is not None:
        require(int(manifest.min_download_mbps)==manifest.min_download_mbps,"operator_runtime_download_filter_invalid",422)
        expected_filters["min_download_mbps"]=int(manifest.min_download_mbps)
    require(binding.filters==expected_filters,"operator_runtime_hardware_filters_mismatch",422)
    require(manifest.minimum_ram_gib*1024**3>=profile["minimum_ram_bytes"]
        and manifest.minimum_disk_gib*1024**3>=profile["minimum_disk_bytes"]
        and manifest.minimum_vram_mib*1024**2>=profile["minimum_free_vram_bytes"],
        "operator_runtime_hardware_below_profile",422)
    return manifest


class _BoundLiumProvider(LiumProvider):
    """Recheck immutable local artifacts before each durable create reservation."""
    def __init__(self,binding,manifest,**kwargs):
        self.binding,self.bound_manifest=binding,manifest
        super().__init__(enabled=True,manifests=(manifest,),**kwargs)

    def validate_launch(self,launch,**kwargs):
        _validate_sources(self.binding)
        require(_manifest(self.binding)==self.bound_manifest,"operator_runtime_manifest_changed")
        return super().validate_launch(launch,**kwargs)


def _assemble(path, *, clock=time.time):
    config=load_runtime_config(path)
    bindings=_bindings(config["registry_file"])
    loader=None
    if config["credential_source"]=="aws_runtime":
        from .lium_runtime_aws import AwsLiumLoader
        loader=AwsLiumLoader(config["secret_arn"],config["secret_version_id"])
    providers={}
    for binding in bindings:
        _validate_sources(binding)
        manifest=_manifest(binding)
        providers[binding.binding_id]=_BoundLiumProvider(binding,manifest,loader=loader,clock=clock,
            journal_dir=Path(config["work_dir"])/"rent-journal")
    registry=OperatorRegistry(bindings,catalog=public_catalog)

    def offers(chosen):
        try:
            binding=registry.resolve(chosen)
            require(binding.enabled and binding.expires_at>clock(),"operator_deployment_not_qualified")
            result=providers[binding.binding_id].preflight_availability(binding.launch)
            reason=None if result is None else (result if result in {
                "provider_inventory_unavailable","provider_inventory_unconfirmed"} else "operator_inventory_unavailable")
            return {"status":"available" if result is None else "unavailable","observed_at":clock(),
                "offers":[],"reason_code":reason,"minimum_ttl_seconds":binding.min_ttl_seconds,
                "hourly_cost_microusd":binding.hourly_cost_microusd,"hourly_cost_basis":"approved_ceiling"}
        except Exception:
            return {"status":"unavailable","observed_at":clock(),"offers":[],
                "reason_code":"operator_inventory_unavailable"}
    registry.offers_reader=offers
    return config,registry,providers


def create_registry(path):
    """API process: lazy credentials; no inventory query until GET offers."""
    return _assemble(path)[1]


def create_controller(path, *, repository=None, settings=None, boot_factory=None):
    """CLI factory. Existing schema, policies, budgets and pools remain unchanged."""
    if settings is None:
        from .settings import Settings
        settings=Settings.from_environment()
    repo=repository if repository is not None else Repository(settings.database_url)
    config,registry,providers=_assemble(path,clock=repo.clock)
    service=OperatorCapacity(repo,settings,registry)
    if boot_factory is None:
        def boot_factory(*args):
            from .operator_boot import create_boot
            return create_boot(*args)
    def boot(binding,intent,chosen):
        return boot_factory(repo,providers[binding.binding_id],binding,intent,chosen,config)
    return OperatorController(service,provider_factory=lambda binding:providers[binding.binding_id],
        boot_factory=boot,enabled=True)
