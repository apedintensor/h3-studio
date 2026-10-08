"""Versioned native-runtime evidence and explicit, inert deployment recipes.

Historical measurements neither authorize capacity nor qualify an adapter. This
module performs local metadata reads only; it never imports a model runtime.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

PROFILE_IDS = (
    "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1",
    "h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-lowram-v1",
    "h3-unpruned33b-bf16-qwenbf16-vaefp16-sdpa-p3-splitqkv-v2",
)
PROFILE_DIRECTORY = Path(__file__).resolve().parents[1] / "deploy" / "wangp" / "profiles"
COMPILER_ID = "sixnine-h3-native-profile-v1"


def get_profile(profile_id):
    """Return a detached metadata copy; unknown IDs never select a fallback."""
    if not isinstance(profile_id, str) or profile_id not in PROFILE_IDS:
        raise ValueError("wangp_unknown_deployment_profile")
    value = json.loads((PROFILE_DIRECTORY / (profile_id + ".json")).read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or value.get("id") != profile_id:
        raise ValueError("wangp_invalid_deployment_profile")
    return value


def public_catalog():
    """Public projection excludes runtime implementation configuration."""
    profiles = []
    for identity in PROFILE_IDS:
        value = get_profile(identity)
        value.pop("runtime")
        value.pop("schema_version")
        profiles.append(value)
    return {"schema_version": 1, "profiles": profiles}


def timing_hint(profile_id, mode, width, height, frames, fps, steps, roles):
    """Exact joint-envelope evidence only. No estimate or interpolation."""
    profile = get_profile(profile_id)
    if (mode not in {"fl", "ref"} or any(type(v) is not int for v in (width, height, frames, fps, steps))
            or not isinstance(roles, (list, tuple, set, frozenset))
            or any(not isinstance(role, str) for role in roles) or len(roles) != len(set(roles))):
        return None
    wanted = (mode, width, height, frames, fps, steps, sorted(roles))
    cases = [case for case in profile["verified_cases"] if
             (case["mode"], case["width"], case["height"], case["frames"], case["fps"],
              case["steps"], sorted(case["input_roles"])) == wanted]
    if not cases:
        return None
    return {"deployment_profile_id": profile_id, "scope": "historical_exact_case",
            "estimated_seconds": None, "cases": cases,
            "warning": "Single-run historical observations with uncontrolled cache state; not a completion estimate or SLA."}


def model_for(profile_id, mode):
    for model in get_profile(profile_id)["models"]:
        if model["mode"] == mode:
            return model
    raise ValueError("wangp_profile_mode_unsupported")


def runtime_config(profile_id, model_root):
    """Exact tested startup values; writing/starting remains caller-owned."""
    root = Path(model_root)
    if not root.is_absolute():
        raise ValueError("wangp_absolute_model_root_required")
    return {**get_profile(profile_id)["runtime"]["config"],
            "checkpoints_paths": [str(root.resolve(strict=True))]}


def weights_manifest(profile_id, mode):
    """Exact component selection for one mode, consumable by the downloader."""
    model_for(profile_id, mode)
    components = get_profile(profile_id)["components"]
    return {"transformer": components["transformer_" + mode],
            **{key: value for key, value in components.items() if not key.startswith("transformer_")}}


def engine_manifest(profile_id, mode):
    """Unqualified manifest; an immutable profile never grants runtime readiness."""
    from .inference.wangp_contract import EngineManifest, PROTOCOL_VERSION, canonical_json
    profile = get_profile(profile_id)
    model = model_for(profile_id, mode)
    runtime = profile["runtime"]
    runtime_digest = hashlib.sha256(canonical_json(runtime).encode()).hexdigest()
    return EngineManifest.from_dict({
        "protocol_version": PROTOCOL_VERSION, "engine": "wangp",
        "source_repository": "https://github.com/deepbeepmeep/Wan2GP",
        "source_revision": profile["source_revision"], "compiler_id": COMPILER_ID,
        "profile_id": profile_id, "deployment_profile_id": profile_id,
        "mode": mode, "model_id": profile["model_id"],
        "generation_recipe_id": model["generation_recipe_id"],
        "runtime_digest": runtime_digest, "runtime_digest_kind": "sixnine-native-profile-v1",
        "runtime_profile": copy.deepcopy(runtime),
        "memory_profile": "mmgp-profile-" + str(runtime["memory_profile"]),
        "kernel_profile": "sdpa-strict-no-compile", "topology": {"slots": 1, "gpus": 1},
        "components": weights_manifest(profile_id, mode), "synthetic": False,
        "inference_qualified": False, "production_adapter_verified": False,
    })


def validate_manifest(manifest):
    """Reject altered mode/assets/config bindings before upload or model import."""
    document = manifest.document
    expected = engine_manifest(document.get("deployment_profile_id"), document.get("mode"))
    if document != expected.document:
        raise ValueError("wangp_profile_manifest_mismatch")
    return get_profile(document["deployment_profile_id"])
