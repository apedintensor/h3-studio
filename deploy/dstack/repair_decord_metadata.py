"""Fix the pinned PyPI py3-none wheel's inconsistent internal CPython3.6 tag.

No decoder/library code is modified. Only WHEEL and its RECORD digest change.
Unexpected input is rejected; pip check remains mandatory after installation.
"""
import base64
import csv
import hashlib
import importlib.metadata
import io
from pathlib import Path

def repair():
    dist=importlib.metadata.distribution("decord")
    if dist.version != "0.6.0":
        raise ValueError("decord_metadata_version_changed")
    directory=Path(dist._path)
    wheel=directory/"WHEEL"
    before=wheel.read_bytes()
    old=b"Tag: cp36-cp36m-manylinux2010_x86_64"
    new=b"Tag: py3-none-manylinux2010_x86_64"
    if before.count(old)!=1 or before.count(b"Tag:")!=1:
        raise ValueError("decord_metadata_tag_changed")
    after=before.replace(old,new)
    record=directory/"RECORD"
    rows=list(csv.reader(io.StringIO(record.read_text(encoding="utf-8"))))
    target=directory.name+"/WHEEL"
    matched=0
    for row in rows:
        if row[0]==target:
            row[1]="sha256="+base64.urlsafe_b64encode(hashlib.sha256(after).digest()).decode().rstrip("=")
            row[2]=str(len(after)); matched+=1
    if matched!=1:
        raise ValueError("decord_metadata_record_changed")
    wheel.write_bytes(after)
    with record.open("w",encoding="utf-8",newline="") as stream:
        csv.writer(stream).writerows(rows)

if __name__=="__main__":
    repair()
