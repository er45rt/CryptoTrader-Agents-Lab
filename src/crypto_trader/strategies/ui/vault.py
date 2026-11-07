"""Windows DPAPI vault for the Binance READ-ONLY key pair (owner decision D44).

The owner types the key pair into the UI; this module is the only place it is written, and only in encrypted form:
CryptProtectData with the CURRENT USER's Windows key (no machine-wide scope) plus an application entropy, so the file
is useless to another Windows account or another program that does not know the entropy. The file lives in the
workspace (outside Git). Nothing here prints, logs, journals or returns a secret: the only exposed facts are whether a
pair is stored and the last four characters of the API key. Seed phrases, passwords and trading or withdrawal keys are
never accepted (the read-only permission contract is checked before anything is saved - accounts.py).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from ...mainnet.credentials import MainnetReadOnlyCredentials, normalize_credential_value
from ..api import StrategyError

__all__ = ["Vault", "dpapi_protect", "dpapi_unprotect"]

_ENTROPY = b"CryptoTrader-Agents-Lab/binance-readonly/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _blob_api():
    if os.name != "nt":
        raise StrategyError("حماية المفاتيح تعتمد على حماية ويندوز (DPAPI)، وهي متاحة على ويندوز فقط.")
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes, kernel32.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p

    def blob(data: bytes) -> tuple[Blob, ctypes.Array]:
        buf = ctypes.create_string_buffer(data, len(data))
        return Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def run(fn, data: bytes) -> bytes:
        src, _keep = blob(data)
        ent, _keep2 = blob(_ENTROPY)
        out = Blob()
        if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
            raise StrategyError("تعذّر استخدام حماية ويندوز (DPAPI) لهذا الحساب.")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))

    return crypt32, run


def dpapi_protect(data: bytes) -> bytes:
    crypt32, run = _blob_api()
    return run(lambda src, desc, ent, res, prompt, flags, out:
               crypt32.CryptProtectData(src, None, ent, res, prompt, flags, out), data)


def dpapi_unprotect(data: bytes) -> bytes:
    crypt32, run = _blob_api()
    return run(lambda src, desc, ent, res, prompt, flags, out:
               crypt32.CryptUnprotectData(src, None, ent, res, prompt, flags, out), data)


class Vault:
    """<workspace>/accounts/binance.dpapi - the encrypted key pair, nothing else."""

    def __init__(self, root: Path, *, protect: Callable[[bytes], bytes] = dpapi_protect,
                 unprotect: Callable[[bytes], bytes] = dpapi_unprotect) -> None:
        self.path = Path(root) / "accounts" / "binance.dpapi"
        self._protect, self._unprotect = protect, unprotect

    def __repr__(self) -> str:                   # never a secret, even by accident
        return f"Vault(stored={self.path.exists()})"

    def exists(self) -> bool:
        return self.path.exists()

    @staticmethod
    def credentials(api_key: str, api_secret: str) -> MainnetReadOnlyCredentials:
        """Validated read-only credentials from what the owner typed (whitespace trimmed, nothing logged)."""
        import re

        key, secret = normalize_credential_value(str(api_key or "")), normalize_credential_value(str(api_secret or ""))
        # a Binance API key / secret is one run of letters and digits: a seed phrase (words with spaces), a password
        # with symbols or anything else is refused before it is stored anywhere
        if not (re.fullmatch(r"[A-Za-z0-9]{32,128}", key) and re.fullmatch(r"[A-Za-z0-9]{32,128}", secret)):
            raise StrategyError("مفتاح API أو المفتاح السري غير صالح: انسخهما كما هما من صفحة إدارة API في Binance. "
                                "لا تُدخل كلمة مرور ولا عبارة استرداد (Seed Phrase).")
        return MainnetReadOnlyCredentials(api_key=key, api_secret=secret)

    def save(self, creds: MainnetReadOnlyCredentials) -> None:
        data = json.dumps({"api_key": creds.api_key, "api_secret": creds.with_secret()}).encode("utf-8")
        blob = self._protect(data)
        if data in blob:                          # never write plaintext, whatever the protector returned
            raise StrategyError("حماية المفاتيح لم تعمل؛ ما انحفظ شي.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, self.path)

    def load(self) -> MainnetReadOnlyCredentials | None:
        if not self.path.exists():
            return None
        try:
            doc = json.loads(self._unprotect(self.path.read_bytes()).decode("utf-8"))
            return MainnetReadOnlyCredentials(api_key=doc["api_key"], api_secret=doc["api_secret"])
        except StrategyError:
            if os.name != "nt":
                raise                              # DPAPI itself is unavailable: say that
            raise StrategyError("تعذّر فك حماية المفتاح المحفوظ (حساب ويندوز آخر أو ملف تالف). افصل الحساب واربطه من جديد.") \
                from None
        except Exception:  # noqa: BLE001 - no detail of the stored content is ever surfaced
            raise StrategyError("تعذّر فك حماية المفتاح المحفوظ (حساب ويندوز آخر أو ملف تالف). افصل الحساب واربطه من جديد.") \
                from None

    def masked_key(self) -> str | None:
        creds = self.load()
        return None if creds is None else "…" + creds.api_key[-4:]

    def delete(self) -> bool:
        if not self.path.exists():
            return False
        self.path.unlink()
        return True
