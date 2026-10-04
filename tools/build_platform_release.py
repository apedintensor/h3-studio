"""Bundle only the tested CPU image and reviewed deployment source. No credentials."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

from release_contract import build_contracts

FILES = ("compose.yaml", "Caddyfile", "init_database.py", "check_config.py")
ROOT = Path(__file__).resolve().parents[1]


def build(commit, destination):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("A complete tested commit ID is required")
    destination = Path(destination)
    # Refuse an existing destination; never absorb private files in an artifact.
    destination.mkdir(parents=False, exist_ok=False)
    image = "sixnine-platform:"+commit
    inspected = json.loads(subprocess.check_output(["docker", "image", "inspect", image], stderr=subprocess.DEVNULL))[0]
    if inspected.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision") != commit:
        raise ValueError("Tested image revision does not match requested release")
    for name in FILES:
        shutil.copyfile(ROOT / "deploy" / "platform" / name, destination / name)
    with subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        with gzip.open(destination / "image.tar.gz", "wb", compresslevel=1) as target:
            shutil.copyfileobj(process.stdout, target, 1024*1024)
        if process.wait() != 0:
            raise RuntimeError("Could not export tested image")
    checksums = {}
    for name in (*FILES, "image.tar.gz"):
        digest = hashlib.sha256()
        with (destination / name).open("rb") as source:
            for chunk in iter(lambda: source.read(1024*1024), b""):
                digest.update(chunk)
        checksums[name] = digest.hexdigest()
    (destination / "release-manifest.json").write_text(json.dumps(
        {"commit": commit, "image": image, "image_id": inspected["Id"], "files": checksums,
         "contracts": build_contracts(ROOT)}, indent=2)+"\n", encoding="utf-8")
    print("Reviewable release manifest SHA-256: "+hashlib.sha256((destination / "release-manifest.json").read_bytes()).hexdigest())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("commit")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    build(args.commit, args.destination)
    print("CPU platform bundle created; no deployment or cloud changes performed")
