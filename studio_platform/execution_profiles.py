"""Read exact operator-owned deployment bindings; no provider/runtime side effects."""
import json
import os
from pathlib import Path
import stat
import copy


def tested_envelope(profile_id, mode):
    """Public conservative scope; not approval, a price or a readiness promise."""
    from .runtime_catalog import get_profile
    profile = get_profile(profile_id)
    cases = [c for c in profile['verified_cases'] if c['mode']==mode]
    if not cases:
        raise ValueError('Unknown profile mode')
    reference = mode=='ref'
    inputs = {'max_images':1 if reference else 0,'max_videos':1 if reference else 0,
        'max_audios':1 if reference else 0,'max_image_pixels':832*480 if reference else 2048**2,
        'max_video_pixels':832*480,'max_video_duration_seconds':56/24,
        'max_audio_duration_seconds':5.2,'guide_kinds':[],'guide_recipe_ids':[],
        'max_guide_time_seconds':5,'allow_video_audio':False}
    return {'max_pixels':max(c['width']*c['height'] for c in cases),'max_duration_seconds':124/24,
        'max_steps':max(c['steps'] for c in cases),'max_reference_files':3 if reference else 2,
        'max_guides':0,'allow_first_last':not reference,'allow_audio':True,
        'controls':{'sampler_name':['euler'],'scheduler':['auto'],'video_decode':['tiled'],
            'audio_decode':['normal'],'encoder_device':['default']},'input_limits':inputs}


def read_profiles(path):
    if path is None:
        return {}
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Execution profiles path must be absolute")
    try:
        with path.open("rb") as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or os.name != "nt" and meta.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ValueError("Execution profiles must be operator-owned")
            raw = stream.read(1048577)
        value = json.loads(raw)
        if (len(raw) > 1048576 or not isinstance(value, dict)
                or set(value) != {"schema_version", "policies"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or not isinstance(value["policies"], list) or len(value["policies"]) > 32):
            raise ValueError("Invalid execution profiles")
        from .execution_policy import validate_policy
        result = {}
        for item in value["policies"]:
            policy = validate_policy(item)
            if "deployment_profile_id" not in policy or len(policy["recipe_ids"]) != 1:
                raise ValueError("Profile requires one explicit mode")
            key = (policy["deployment_profile_id"], policy["recipe_ids"][0])
            if key in result:
                raise ValueError("Ambiguous execution profile")
            result[key] = policy
        return result
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, KeyError):
        raise ValueError("Execution profiles unavailable or invalid") from None


def selected_policy(settings, *, profile_id=None, recipe_id=None):
    if profile_id is None:
        from .execution_policy import read_policy
        return read_policy(settings.execution_policy_file)
    if not isinstance(profile_id, str) or not profile_id:
        raise ValueError("Invalid deployment profile")
    return read_profiles(getattr(settings, "execution_profiles_file", None)).get((profile_id, recipe_id))


def default_profile_id(settings, profiles=None):
    """Only an explicitly configured, current FL binding can seed a new draft."""
    identity = getattr(settings, "default_deployment_profile_id", None)
    if identity is None:
        return None
    for profile in public_profiles(settings) if profiles is None else profiles:
        support = profile.get("generation_support", {}).get("fl", {})
        if profile["id"] == identity and support.get("configured") and support.get("enabled"):
            return identity
    return None


def public_profiles(settings):
    from .runtime_catalog import public_catalog
    from .inference.wangp_profile_compiler import control_schema
    import time
    profiles = public_catalog()["profiles"]
    try:
        policies = read_profiles(getattr(settings, "execution_profiles_file", None))
    except ValueError:
        policies = {}
    now = time.time()
    for profile in profiles:
        modes = {}
        for mode, recipe in (("fl", "h3-base-fl2va-v1"), ("ref", "h3-base-ref2va-v1")):
            policy = policies.get((profile["id"], recipe))
            constraints = copy.deepcopy(policy['envelope']) if policy else tested_envelope(profile['id'],mode)
            limits = {'max_images':1 if mode=='ref' else 0,'max_videos':1 if mode=='ref' else 0,
                'max_audios':1 if mode=='ref' else 0,'max_total_files':3 if mode=='ref' else 2,
                'max_guides':0,'min_clip_duration':2,'max_clip_duration':5.2,
                'max_video_clip_duration':56/24,'max_audio_clip_duration':5.2,
                'max_total_video_duration':56/24,'max_total_audio_duration':5.2}
            current = bool(policy and policy["enabled"]
                and policy["qualification"]["status"] in {"accepted", "runtime_required"}
                and policy["qualification"]["verified_at"] <= now
                and now + policy["reservation"]["expected_runtime_s"] < min(
                    policy["qualification"]["expires_at"], policy["reservation"]["expires_at"]))
            enabled = current and settings.generation_enabled and settings.execution_backend == "wangp-worker"
            modes[mode] = {"configured": policy is not None, "enabled": enabled,
                "reason": "仍需预检实际容量和账户额度" if enabled else "此配置尚未接入当前执行池",
                "capacity_checked": False, "controls": control_schema(profile["id"], mode),
                'constraints':constraints,'limits':limits,
                'custom_canvas_constraints':{'maximum_pixel_area':constraints['max_pixels'],
                    'minimum_aspect_ratio':16/9,'maximum_aspect_ratio':16/9},
                'input_notes':'仅开放已测组合；视频参考需规范化为56帧/24fps且关闭原声，独立参考音频2–5.2秒。',
                'joint_cases':[{'input_roles':c['input_roles'],'width':c['width'],'height':c['height'],
                    'frames':c['frames'],'steps':c['steps']} for c in profile['verified_cases'] if c['mode']==mode]}
        profile["generation_support"] = modes
    return profiles
