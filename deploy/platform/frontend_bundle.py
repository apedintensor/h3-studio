"""Pure bounded validation of independently published static frontend bundles."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tarfile
import zlib

API_CONTRACT = "sixnine-web-v1"
ARCHIVE_NAME = "frontend.tar.gz"
MANIFEST_NAME = "frontend-manifest.json"
MAX_ARCHIVE = 64 * 1024**2
MAX_EXPANDED = 128 * 1024**2
MAX_FILE = 32 * 1024**2
MAX_INDEX = 1024**2
MAX_MANIFEST = 256 * 1024
MAX_FILES = 1000
MAX_TAR = MAX_EXPANDED + MAX_FILES * 1024 + 10240
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
ASSET = re.compile(r"assets/[A-Za-z0-9_][A-Za-z0-9_.-]*-[A-Za-z0-9_-]{8,64}\."
                   r"(?:js|css|svg|png|jpg|jpeg|gif|webp|avif|ico|woff|woff2|ttf|otf|wasm)\Z")


class FrontendBundleError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise FrontendBundleError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def linked(metadata):
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def directory(path):
    path = Path(path)
    metadata = path.lstat()
    require(not linked(metadata) and stat.S_ISDIR(metadata.st_mode), "frontend_directory_must_be_real")
    return path


def read_regular(path, maximum):
    path = Path(path)
    before = path.lstat()
    require(not linked(before) and stat.S_ISREG(before.st_mode) and before.st_nlink == 1
            and before.st_size <= maximum, "frontend_file_type_or_size_invalid")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        actual = os.fstat(source.fileno())
        require(stat.S_ISREG(actual.st_mode) and actual.st_nlink == 1
                and (actual.st_dev, actual.st_ino) == (before.st_dev, before.st_ino)
                and actual.st_size <= maximum, "frontend_file_changed")
        raw = source.read(maximum + 1)
    require(len(raw) == actual.st_size and len(raw) <= maximum, "frontend_file_changed_or_too_large")
    return raw


def allowed_name(name):
    return isinstance(name, str) and (name == "index.html" or bool(ASSET.fullmatch(name)))


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "frontend_duplicate_manifest_field")
        value[key] = item
    return value


def parse_manifest(raw, commit):
    require(isinstance(commit, str) and SHA.fullmatch(commit), "frontend_exact_commit_required")
    require(len(raw) <= MAX_MANIFEST, "frontend_manifest_too_large")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise FrontendBundleError("frontend_manifest_invalid") from None
    require(isinstance(value, dict) and set(value) == {
        "version", "commit", "api_contract", "api_compatibility", "archive_sha256", "archive_bytes", "files"},
        "frontend_manifest_fields_invalid")
    require(type(value["version"]) is int and value["version"] == 1 and value["commit"] == commit
            and value["api_contract"] == API_CONTRACT, "frontend_manifest_contract_invalid")
    for key in ("api_compatibility", "archive_sha256"):
        require(isinstance(value[key], str) and DIGEST.fullmatch(value[key]), "frontend_manifest_digest_invalid")
    require(type(value["archive_bytes"]) is int and 0 < value["archive_bytes"] <= MAX_ARCHIVE,
            "frontend_archive_size_invalid")
    files = value["files"]
    require(isinstance(files, dict) and 1 <= len(files) <= MAX_FILES and "index.html" in files,
            "frontend_file_set_invalid")
    folded, total = set(), 0
    for name, metadata in files.items():
        require(allowed_name(name) and name.casefold() not in folded, "frontend_file_name_or_collision_invalid")
        folded.add(name.casefold())
        require(isinstance(metadata, dict) and set(metadata) == {"sha256", "size"}, "frontend_file_metadata_invalid")
        require(isinstance(metadata["sha256"], str) and DIGEST.fullmatch(metadata["sha256"]),
                "frontend_file_digest_invalid")
        limit = MAX_INDEX if name == "index.html" else MAX_FILE
        require(type(metadata["size"]) is int and 0 < metadata["size"] <= limit, "frontend_file_size_invalid")
        total += metadata["size"]
    require(total <= MAX_EXPANDED, "frontend_expansion_limit")
    return value


def tar_header(name, size):
    member = tarfile.TarInfo(name)
    member.size, member.mode, member.mtime = size, 0o644, 0
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    return member.tobuf(format=tarfile.USTAR_FORMAT, encoding="ascii", errors="strict")


def unpack_verified(raw, manifest):
    require(len(raw) == manifest["archive_bytes"] and digest(raw) == manifest["archive_sha256"],
            "frontend_archive_hash_or_size_mismatch")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as compressed:
            expanded = compressed.read(MAX_TAR + 1)
    except (OSError, EOFError, zlib.error):
        raise FrontendBundleError("frontend_archive_compression_invalid") from None
    require(len(expanded) <= MAX_TAR and len(expanded) % 512 == 0, "frontend_archive_expansion_or_block_invalid")
    result, offset, total, folded = {}, 0, 0, set()
    while offset + 512 <= len(expanded):
        header = expanded[offset:offset + 512]
        if header == bytes(512):
            # No concatenated archives or bytes hidden after the end marker.
            require(len(expanded) - offset >= 1024 and not any(expanded[offset:]), "frontend_archive_trailer_invalid")
            break
        try:
            member = tarfile.TarInfo.frombuf(header, encoding="ascii", errors="strict")
        except (tarfile.TarError, UnicodeError, ValueError):
            raise FrontendBundleError("frontend_archive_header_invalid") from None
        name = member.name
        require(allowed_name(name) and name not in result and name.casefold() not in folded,
                "frontend_archive_path_or_duplicate_invalid")
        require(member.type == tarfile.REGTYPE and member.size > 0 and name in manifest["files"],
                "frontend_archive_file_type_invalid")
        require(member.size == manifest["files"][name]["size"] and header == tar_header(name, member.size),
                "frontend_archive_not_normalized")
        limit = MAX_INDEX if name == "index.html" else MAX_FILE
        total += member.size
        require(member.size <= limit and total <= MAX_EXPANDED and len(result) < MAX_FILES,
                "frontend_archive_expansion_limit")
        start, end = offset + 512, offset + 512 + member.size
        following = start + ((member.size + 511) // 512) * 512
        require(following <= len(expanded), "frontend_archive_truncated")
        content = expanded[start:end]
        require(digest(content) == manifest["files"][name]["sha256"] and not any(expanded[end:following]),
                "frontend_archive_file_hash_or_padding_invalid")
        result[name] = content
        folded.add(name.casefold())
        offset = following
    else:
        raise FrontendBundleError("frontend_archive_end_marker_missing")
    require(set(result) == set(manifest["files"]), "frontend_archive_file_set_mismatch")
    return result


def bundle_payloads(path, commit):
    root = directory(path)
    manifest_raw = read_regular(root / MANIFEST_NAME, MAX_MANIFEST)
    manifest = parse_manifest(manifest_raw, commit)
    archive = read_regular(root / ARCHIVE_NAME, MAX_ARCHIVE)
    unpack_verified(archive, manifest)
    return manifest, {ARCHIVE_NAME: archive, MANIFEST_NAME: manifest_raw}


def validate(path, commit):
    return bundle_payloads(path, commit)[0]


def archive_files(path, manifest):
    """Recheck manifest and archive bytes; never trust a caller's prior check."""
    require(isinstance(manifest, dict), "frontend_manifest_invalid")
    root = directory(path)
    actual = parse_manifest(read_regular(root / MANIFEST_NAME, MAX_MANIFEST), manifest.get("commit"))
    require(actual == manifest, "frontend_manifest_changed")
    return unpack_verified(read_regular(root / ARCHIVE_NAME, MAX_ARCHIVE), actual)
