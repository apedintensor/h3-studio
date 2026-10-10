"""Build selected one-GPU WanGP inputs locally; never install or contact a cloud.

Example: python tools/build_dstack_sources.py --output NEW_DIRECTORY
  --selection h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1:fl
Repeat --selection for each explicitly chosen profile/mode. An incomplete build
is never overwritten or resumed; only the final index is a build receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy.wangp.package_tool import small_bundle
from studio_platform.runtime_catalog import engine_manifest
from tools.build_operator_sources import SOURCE_NAMES, _json, _runtime, _write

PURPOSE = "local-unqualified-dstack-source-set"


def build(output, selections):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError("dstack_sources_output_invalid")
    for parent in output.parents:
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError("dstack_sources_linked_parent")
    selections = list(selections)
    if not 1 <= len(selections) <= 128 or any(type(item) not in (tuple, list) or len(item) != 2
            or not all(isinstance(value, str) for value in item) for item in selections):
        raise ValueError("dstack_sources_selection_invalid")
    pairs = [tuple(item) for item in selections]
    if len(set(pairs)) != len(pairs):
        raise ValueError("dstack_sources_duplicate_selection")
    # Validate every selection before creating output. No model substitution.
    manifests = [(profile, mode, engine_manifest(profile, mode)) for profile, mode in pairs]
    bootstrap = (ROOT / "deploy/wangp/bootstrap.py").read_bytes()
    output.mkdir(mode=0o700, exist_ok=False)
    entries, package = [], None
    for profile, mode, manifest in manifests:
        relative = Path(profile) / mode / "gpu-0"
        directory = output / relative
        directory.mkdir(mode=0o700, parents=True)
        archive = directory / "wangp-package.tar.gz"
        if package is None:
            small_bundle(archive)
            archive.chmod(0o600)
            package = archive.read_bytes()
        else:
            _write(archive, package)
        digest = hashlib.sha256(package).hexdigest()
        _write(directory / "wangp-bootstrap.py", bootstrap)
        _write(directory / "wangp-manifest.json", _json(manifest.document))
        _write(directory / "wangp-runtime.json", _json(_runtime(profile, 0, 1, digest)))
        entries.append({"runtime_profile_id": profile, "mode": mode, "gpu_count": 1,
            "profile_slot_index": 0, "directory": relative.as_posix(),
            "engine_manifest_digest": manifest.digest,
            "source_sha256": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                              for name in SOURCE_NAMES}})
    receipt = {"schema_version": 1, "purpose": PURPOSE,
        "production_adapter_verified": False, "sources": entries}
    _write(output / "index.json", _json(receipt))
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--selection", action="append", required=True, metavar="PROFILE:MODE")
    args = parser.parse_args(argv)
    try:
        receipt = build(args.output, [value.rsplit(":", 1) for value in args.selection])
    except (OSError, ValueError):
        print(json.dumps({"state": "dstack_source_build_failed"}))
        return 1
    print(json.dumps({"state": "built_unqualified", "slot_source_count": len(receipt["sources"]),
        "production_adapter_verified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
