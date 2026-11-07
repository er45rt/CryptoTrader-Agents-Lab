"""Local PIN login for the UI (owner decision D44).

* The PIN (6-12 digits) is never stored: only an scrypt hash with a random salt in <workspace>/auth.json.
* Wrong PINs are counted on disk; MAX_FAILS in a row lock the login for LOCK_S (the lock survives a restart).
* A successful login yields a random session token (an HttpOnly, SameSite=Strict cookie); a session ends after IDLE_S
  without use (auto-lock), on logout, or when the server restarts (sessions live in memory only).
* Login is enforced once a PIN exists; linking a Binance account requires it (accounts.py).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..api import StrategyError

__all__ = ["Auth", "IDLE_S", "MAX_FAILS", "LOCK_S"]

IDLE_S = 15 * 60
MAX_FAILS = 5
LOCK_S = 5 * 60
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def _hash(pin: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)


class Auth:
    def __init__(self, root: Path, *, now: Callable[[], float] = time.time) -> None:
        self.path = Path(root) / "auth.json"
        self._now = now
        self._lock = threading.Lock()
        self._sessions: dict[str, float] = {}

    # --- state -------------------------------------------------------------------------------------------
    def enabled(self) -> bool:
        return self.path.exists()

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, doc: dict[str, Any]) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _check_pin(pin: Any) -> str:
        pin = str(pin or "")
        if not re.fullmatch(r"\d{6,12}", pin):
            raise StrategyError("رمز الدخول لازم يكون من 6 إلى 12 رقم.")
        return pin

    # --- PIN ---------------------------------------------------------------------------------------------
    def set_pin(self, pin: Any, *, token: str | None = None) -> None:
        """First PIN: anyone at the local UI may set it. Changing it needs a live session."""
        pin = self._check_pin(pin)
        with self._lock:
            if self.enabled() and not self._valid(token):
                raise StrategyError("سجّل الدخول أولًا لتغيير رمز الدخول.")
            salt = secrets.token_bytes(16)
            self._write({"v": 1, "salt": salt.hex(), **_SCRYPT, "hash": _hash(pin, salt, **_SCRYPT).hex(),
                         "fails": 0, "locked_until": 0})
            self._sessions.clear()                 # a new PIN ends every session

    def login(self, pin: Any) -> str:
        with self._lock:
            if not self.enabled():
                raise StrategyError("ما فيه رمز دخول بعد؛ أنشئ واحد أولًا.")
            doc, now = self._read(), self._now()
            if doc.get("locked_until", 0) > now:
                wait = int(doc["locked_until"] - now) + 1
                raise StrategyError(f"الدخول مقفل مؤقتًا بعد محاولات خاطئة؛ حاول بعد {wait} ثانية.")
            ok = isinstance(pin, (str, int)) and re.fullmatch(r"\d{6,12}", str(pin)) is not None and hmac.compare_digest(
                _hash(str(pin), bytes.fromhex(doc["salt"]), doc["n"], doc["r"], doc["p"]), bytes.fromhex(doc["hash"]))
            if not ok:
                doc["fails"] = doc.get("fails", 0) + 1
                left = MAX_FAILS - doc["fails"]
                if left <= 0:
                    doc["fails"], doc["locked_until"] = 0, now + LOCK_S
                self._write(doc)
                raise StrategyError("رمز الدخول غير صحيح." + (f" باقي {left} محاولة." if left > 0
                                                              else f" قُفل الدخول {LOCK_S // 60} دقائق."))
            if doc.get("fails") or doc.get("locked_until"):
                doc["fails"], doc["locked_until"] = 0, 0
                self._write(doc)
            token = secrets.token_urlsafe(32)
            self._sessions[token] = now
            return token

    # --- sessions ----------------------------------------------------------------------------------------
    def _valid(self, token: str | None) -> bool:
        if not token or token not in self._sessions:
            return False
        now = self._now()
        if now - self._sessions[token] > IDLE_S:
            del self._sessions[token]
            return False
        self._sessions[token] = now               # activity keeps the session open
        return True

    def check(self, token: str | None) -> bool:
        """True when no PIN is set (login not in use) or the session is live."""
        if not self.enabled():
            return True
        with self._lock:
            return self._valid(token)

    def logged_in(self, token: str | None) -> bool:
        with self._lock:
            return self.enabled() and self._valid(token)

    def logout(self, token: str | None) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)
