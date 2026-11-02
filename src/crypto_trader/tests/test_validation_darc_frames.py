"""Synthetic-only adversarial DARC frame tests. No network/market access."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import struct
import pytest

from crypto_trader.validation.darc_frames import DarcError, read_segment

MAGIC=b"DARC0001"
END=b"DARCEND1"
def record(kind="heartbeat", **kw):
    return {"record_type":kind,"record_version":1,"session_id":"test-session","monotonic_ns":1,"wall_ms":1791600000000,**kw}
def wire(value):
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode("utf-8")
    length=struct.pack(">Q",len(raw))
    return MAGIC+length+hashlib.sha256(MAGIC+length+raw).digest()+raw+END
def target(tmp_path,data):
    f=tmp_path/"test.darc"
    f.write_bytes(data)
    return f,hashlib.sha256(data).hexdigest()

def read(f,digest):
    return read_segment(f,expected_sha256=digest,expected_size=f.stat().st_size)

def test_exact_good_frame_and_two_records(tmp_path):
    data=wire(record("bootstrap"))+wire(record("clean_stop",monotonic_ns=2))
    f,d=target(tmp_path,data)
    evidence=read(f,d)
    assert evidence.record_count==2 and evidence.digest==d
    assert evidence.records[0]["record_type"]=="bootstrap"
    assert evidence.records[1]["record_type"]=="clean_stop"

@pytest.mark.parametrize("damage",["trailing","truncated","header","digest","trailer","size","empty"])
def test_corrupt_and_truncated_refuse(tmp_path,damage):
    data=bytearray(wire(record()))
    if damage=="trailing": data.extend(b"\0")
    elif damage=="truncated": data=data[:-1]
    elif damage=="header": data[0]^=0x01
    elif damage=="digest":data[17]^=1
    elif damage=="trailer":data[-1]^=1
    elif damage=="size":data[8:16]=struct.pack(">Q",2**64-1)
    else:data=bytearray()
    f,d=target(tmp_path,data)
    with pytest.raises(DarcError):
        read(f,d)

def test_malformed_utf8_duplicate_keys_nan_and_surrogate(tmp_path):
    bodies=[
        b'{"record_type":"heartbeat","record_version":1,"dup":1,"dup":2}',
        b'{"record_type":"heartbeat","record_version":1,"a":NaN}',
        b'{"record_type":"heartbeat","record_version":1,"a":"\\ud800"}',
        b'{"record_type":"heartbeat","record_version":1,"a":"\xff"}',
        b'{"record_type":"heartbeat","record_version":1,"\x65\xcc\x81":1}',
    ]
    for body in bodies:
        data=MAGIC+struct.pack(">Q",len(body))+hashlib.sha256(MAGIC+struct.pack(">Q",len(body))+body).digest()+body+END
        f,d=target(tmp_path,data)
        with pytest.raises(DarcError):
            read(f,d)

def test_deep_nested_and_too_many_records(tmp_path):
    nested='['*35+'0'+']'*35
    body=('{"record_version":1,"record_type":"heartbeat","deep":'+nested+'}').encode("utf-8")
    data=MAGIC+struct.pack(">Q",len(body))+hashlib.sha256(MAGIC+struct.pack(">Q",len(body))+body).digest()+body+END
    f,d=target(tmp_path,data)
    with pytest.raises(DarcError):
        read(f,d)
    data=wire(record())*3
    f,d=target(tmp_path,data)
    with pytest.raises(DarcError):
        read_segment(f,expected_sha256=d,expected_size=len(data),max_records=2)

def test_hash_mismatch_and_file_size_mismatch(tmp_path):
    f,d=target(tmp_path,wire(record()))
    with pytest.raises(DarcError):
        read_segment(f,expected_sha256="0"*64,expected_size=f.stat().st_size)
    with pytest.raises(DarcError):
        read_segment(f,expected_sha256=d,expected_size=f.stat().st_size+1)

def test_symlink_and_unsafe_paths_fail(tmp_path):
    real,d=target(tmp_path,wire(record()))
    link=tmp_path/"alias.darc"
    try: link.symlink_to(real)
    except (OSError,NotImplementedError): pytest.skip("symlink unavailable on host")
    with pytest.raises(DarcError):
        read(link,d)

def test_after_open_metadata_mismatch_refused(tmp_path,monkeypatch):
    f,d=target(tmp_path,wire(record()))
    import crypto_trader.validation.darc_frames as reader
    original=reader._identity
    n={"i":0}
    def changing(stream):
        identity=original(stream)
        n["i"]+=1
        if n["i"]==2:
            return identity._replace(size=identity.size+1)
        return identity
    monkeypatch.setattr(reader,"_identity",changing)
    with pytest.raises(DarcError):
        read(f,d)


@pytest.mark.parametrize("bad", [[], {}, 5, None])
def test_unhashable_record_type_cannot_escape_parser(tmp_path,bad):
    data=wire({"record_type":bad,"record_version":1})
    f,d=target(tmp_path,data)
    with pytest.raises(DarcError):
        read(f,d)

def test_large_wire_but_many_decoded_nodes_refused(tmp_path):
    body={"record_type":"heartbeat","record_version":1,"nested":[{"a":1}]*25000}
    f,d=target(tmp_path,wire(body))
    with pytest.raises(DarcError):
        read(f,d)
