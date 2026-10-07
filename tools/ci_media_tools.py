"""CI-only media dependencies; keep runner mirrors and APT's signature checks."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import platform
import re
import subprocess
import sys


SOURCES = ("/etc/apt/sources.list.d/ubuntu.sources", "/etc/apt/apt-mirrors.txt",
           "/etc/apt/apt-mirrors-security.txt")
FONT = "Noto Sans CJK SC"


def https_sources(text):
    # Preserve the hosted runner's selected mirror and priority. Replacing Azure
    # with archive.ubuntu.com caused measured slow package downloads in CI.
    return re.sub(r"http://((?:azure\.)?archive\.ubuntu\.com|security\.ubuntu\.com)/ubuntu\b",
                  r"https://\1/ubuntu", text)


def run(args, **kwargs):
    return subprocess.run(args, check=True, timeout=kwargs.pop("timeout", 30), **kwargs)


def usable(args):
    try:
        run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def missing_packages():
    packages = []
    if not (usable(["ffmpeg", "-version"]) and usable(["ffprobe", "-version"])):
        packages.append("ffmpeg")
    try:
        family = run(["fc-match", "--format", "%{family}", FONT],
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        packages += ["fontconfig", "fonts-noto-cjk"]
    else:
        if FONT not in family:
            packages.append("fonts-noto-cjk")
    return packages


def cache_key():
    # No package indexes, credentials or installed binaries are cached. Source
    # and APT policy changes select a different archive cache automatically.
    digest = hashlib.sha256(Path(__file__).read_bytes())
    for value in (platform.machine(), os.environ.get("ImageOS", ""),
                  os.environ.get("ImageVersion", "")):
        digest.update(value.encode()+b"\0")
    paths = [Path("/etc/os-release"), *map(Path, SOURCES), Path("/etc/apt/apt.conf")]
    policy = Path("/etc/apt/apt.conf.d")
    if policy.is_dir():
        paths += sorted(path for path in policy.iterdir() if path.is_file())
    for path in paths:
        if path.is_file():
            digest.update(str(path).encode()+b"\0"+path.read_bytes()+b"\0")
    return "sixnine-ubuntu24-media-"+digest.hexdigest()


def install(cache):
    packages = missing_packages()
    if not packages:
        print("Required media tools and CJK font are already usable")
        return
    cache = Path(cache).resolve()
    root = Path(os.environ["RUNNER_TEMP"]).resolve()
    if cache != root / "sixnine-ci-apt":
        raise ValueError("Unexpected CI archive cache directory")
    cache.mkdir(parents=True, exist_ok=True)
    for name in SOURCES:
        path = Path(name)
        if path.is_file():
            original = path.read_text()
            converted = https_sources(original)
            if converted != original:
                run(["sudo", "tee", str(path)], input=converted, text=True, stdout=subprocess.DEVNULL)
    # Custom archive directory survives Docker's literal /var/cache/apt cleanup
    # hook. Keep normal APT metadata/signature/hash validation and install by
    # package name, never `dpkg -i` or `apt install ./restored-file.deb`.
    # Match the official x64 runner's bounded failover rather than retrying the
    # same slow mirror for minutes before consulting its next listed mirror.
    options = ["-o", "Acquire::Retries=1", "-o", "Acquire::http::Timeout=15",
        "-o", "Acquire::https::Timeout=15", "-o", "APT::Update::Error-Mode=any",
        "-o", "Dir::Cache::archives="+str(cache),
        "-o", "APT::Keep-Downloaded-Packages=true",
        "-o", "Binary::apt::APT::Keep-Downloaded-Packages=true"]
    run(["sudo", "apt-get", *options, "update"], timeout=180)
    run(["sudo", "apt-get", *options, "install", "-y", "--no-install-recommends", *packages], timeout=480)
    if missing_packages():
        raise RuntimeError("Media tool installation did not satisfy CI requirements")


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true" or sys.platform != "linux":
        raise RuntimeError("This helper only runs in Linux GitHub Actions")
    if sys.argv[1:] == ["cache-key"]:
        print("key="+cache_key())
    elif len(sys.argv) == 3 and sys.argv[1] == "install":
        install(sys.argv[2])
    else:
        raise ValueError("Expected cache-key or install CACHE_DIR")


if __name__ == "__main__":
    main()
