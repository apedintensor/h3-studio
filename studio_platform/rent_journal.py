"""Private durable rent markers; no credentials, request bodies or media."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import uuid


class RentJournal:
    def __init__(self, directory):
        self.directory = Path(directory)
        if not self.directory.is_absolute():
            raise ValueError("rent_journal_absolute_path_required")

    def _path(self, tag):
        if not isinstance(tag, str) or str(uuid.UUID(tag)) != tag:
            raise ValueError("rent_journal_invalid_tag")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or self.directory.resolve() != self.directory:
            raise ValueError("rent_journal_linked_directory")
        info = self.directory.stat()
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("rent_journal_directory_not_private")
        return self.directory/(tag+".json")

    @staticmethod
    def _validate(value, tag):
        fields = {"version", "tag", "phase"}
        if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1 or value.get("tag") != tag:
            raise ValueError("rent_journal_invalid")
        phase = value.get("phase")
        if phase in {"post_started", "confirmed", "quarantined", "rejected"} and "executor_id" in value:
            fields.add("executor_id")
        if phase in {"confirmed", "quarantined"}:
            fields.add("instance_id")
        if phase not in {"checking", "not_submitted", "post_started", "confirmed", "quarantined", "rejected"} or set(value) != fields:
            raise ValueError("rent_journal_invalid")
        for key in ("executor_id", "instance_id"):
            if key in value and (not isinstance(value[key], str) or str(uuid.UUID(value[key])) != value[key]):
                raise ValueError("rent_journal_invalid_id")
        return value

    def read(self, tag):
        path = self._path(tag)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077)):
                raise ValueError("rent_journal_file_not_private")
            raw = source.read(4097)
        if len(raw) > 4096:
            raise ValueError("rent_journal_too_large")
        return self._validate(json.loads(raw), tag)

    def save(self, tag, phase, *, executor_id=None, instance_id=None):
        path = self._path(tag)
        prior = self.read(tag)
        if (prior is None and phase != "checking" or prior is not None and
                (prior["phase"], phase) not in {("checking", "not_submitted"),
                                             ("checking", "post_started"), ("post_started", "confirmed"),
                                             ("post_started", "rejected"), ("post_started", "quarantined")}):
            raise ValueError("rent_journal_transition_refused")
        value = {"version": 1, "tag": tag, "phase": phase}
        for key, candidate in (("executor_id", executor_id), ("instance_id", instance_id)):
            if candidate is not None:
                value[key] = str(uuid.UUID(candidate))
        self._validate(value, tag)
        if prior and "executor_id" in prior and value.get("executor_id") != prior["executor_id"]:
            raise ValueError("rent_journal_executor_conflict")
        raw = json.dumps(value, separators=(",", ":")).encode()
        target = path if prior is None else self.directory/(tag+"-"+str(uuid.uuid4())+".next")
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if prior is not None:
            os.replace(target, path)
        if os.name != "nt":
            descriptor = os.open(self.directory, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
