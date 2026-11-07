"""Owner decision D44, K1: the Binance read-only key pair is stored only encrypted with Windows DPAPI."""
from __future__ import annotations

import os

import pytest

from crypto_trader.strategies.api import StrategyError
from crypto_trader.strategies.ui import vault as vmod

KEY = "A1b2C3d4" * 8          # 64 letters/digits: the shape of a Binance key (test value, not a real key)
SECRET = "Z9y8X7w6" * 8

nt = pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows only")


@nt
def test_the_pair_round_trips_through_dpapi_and_the_file_holds_no_plaintext(tmp_path):
    v = vmod.Vault(tmp_path)
    v.save(v.credentials(KEY, SECRET))
    raw = v.path.read_bytes()
    assert KEY.encode() not in raw and SECRET.encode() not in raw
    creds = v.load()
    assert creds.api_key == KEY and creds.with_secret() == SECRET
    assert v.masked_key() == "…" + KEY[-4:]
    assert KEY not in repr(v) and KEY not in repr(creds) and SECRET not in str(creds)
    assert v.path.parent == tmp_path / "accounts" and not list(tmp_path.rglob("*.tmp"))


@nt
def test_another_entropy_or_a_damaged_file_cannot_be_read(tmp_path):
    v = vmod.Vault(tmp_path)
    v.save(v.credentials(KEY, SECRET))
    v.path.write_bytes(v.path.read_bytes()[:-8] + b"broken!!")
    with pytest.raises(StrategyError, match="تعذّر فك حماية") as e:
        v.load()
    assert KEY not in str(e.value) and SECRET not in str(e.value)


def test_passwords_and_seed_phrases_are_refused_before_anything_is_stored(tmp_path):
    v = vmod.Vault(tmp_path, protect=lambda b: b"x" * len(b), unprotect=lambda b: b)
    for key, secret in (("abandon ability able about above absent absorb abstract absurd abuse access accident", SECRET),
                        (KEY, "P@ssw0rd!"), ("", SECRET), (KEY, "short1")):
        with pytest.raises(StrategyError, match="غير صالح") as e:
            v.credentials(key, secret)
        assert (key and key not in str(e.value)) or not key
    assert not v.exists()


def test_a_protector_that_returns_plaintext_is_refused(tmp_path):
    v = vmod.Vault(tmp_path, protect=lambda b: b"prefix" + b, unprotect=lambda b: b)
    with pytest.raises(StrategyError, match="ما انحفظ"):
        v.save(v.credentials(KEY, SECRET))
    assert not v.exists()


def test_delete_removes_the_stored_pair(tmp_path):
    v = vmod.Vault(tmp_path, protect=lambda b: bytes(x ^ 0x5A for x in b), unprotect=lambda b: bytes(x ^ 0x5A for x in b))
    assert v.delete() is False
    v.save(v.credentials(KEY, SECRET))
    assert v.exists() and v.delete() is True and not v.exists() and v.load() is None
