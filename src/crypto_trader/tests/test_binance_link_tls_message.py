"""Owner 2026-11-09: linking failed with only "تعذّر الاتصال بـ Binance — TransportError"; the cause was Avast's HTTPS
scanning re-signing api.binance.com, which the link's HTTPS library (certifi) rightly rejects. A failed connection now
checks the certificate itself (a handshake only) and says so, naming who issued it."""
from __future__ import annotations

import json
import ssl

from crypto_trader.execution.binance_testnet.transport import TransportError
from crypto_trader.strategies.ui import accounts
from crypto_trader.tests.test_binance_link_accounts import _vault

AVAST = {"issuer": ((("organizationName", "Avast Web/Mail Shield"),), (("commonName", "Avast Web/Mail Shield Root"),))}


def _calls(*results):
    seen, it = [], iter(results)

    def handshake(ctx):
        seen.append(ctx)
        r = next(it)
        if isinstance(r, BaseException):
            raise r
        return r
    return handshake, seen


def test_an_untrusted_certificate_is_named_with_its_issuer():
    hs, seen = _calls(ssl.SSLCertVerificationError(1, "certificate verify failed"), AVAST)
    msg = accounts.tls_diagnosis(handshake=hs)
    assert msg.startswith("شهادة الاتصال بـ Binance ما تحققت") and "api.binance.com" in msg
    assert msg.endswith("الشهادة صادرة من: Avast Web/Mail Shield") and len(seen) == 2
    assert seen[0].verify_mode == ssl.CERT_REQUIRED and seen[0].check_hostname and seen[0].keylog_filename is None


def test_a_trusted_certificate_or_no_connection_is_not_called_a_certificate_problem():
    assert accounts.tls_diagnosis(handshake=_calls({"issuer": ()})[0]) is None
    assert accounts.tls_diagnosis(handshake=_calls(ConnectionRefusedError())[0]) is None
    msg = accounts.tls_diagnosis(handshake=_calls(ssl.SSLCertVerificationError(1, "x"), OSError("down"))[0])
    assert msg and "الشهادة صادرة من" not in msg                  # issuer unknown: no guess


class _Fails:
    def __init__(self, exc):
        self.exc = exc

    def api_restrictions(self):
        raise self.exc

    def verify_read_only_permissions(self):
        raise self.exc


def _link(tmp_path, exc, check):
    return accounts.BinanceLink(tmp_path, vault=_vault(tmp_path), client_factory=lambda c: _Fails(exc), tls_check=check)


def test_the_link_reports_the_certificate_reason_instead_of_the_bare_class_name(tmp_path):
    from crypto_trader.strategies.api import StrategyError
    import pytest

    hint = "شهادة الاتصال بـ Binance ما تحققت: سبب مفصّل"
    link = _link(tmp_path, TransportError("the transport failed; transmission is uncertain."), lambda: hint)
    with pytest.raises(StrategyError, match="سبب مفصّل"):
        link.connect("A" * 64, "B" * 64, pin_set=True)
    state = json.loads(link.state_path.read_text(encoding="utf-8"))
    assert state["last_error"] == hint and not link.vault.exists()   # nothing stored


def test_without_a_certificate_reason_the_original_message_stays(tmp_path):
    from crypto_trader.strategies.api import StrategyError
    import pytest

    def broken():
        raise RuntimeError("diagnosis failed")

    for check in (lambda: None, broken):
        link = _link(tmp_path, TransportError("x"), check)
        with pytest.raises(StrategyError, match="تعذّر الاتصال بـ Binance — TransportError"):
            link.connect("A" * 64, "B" * 64, pin_set=True)


# Sol High review 2026-11-09: a plain SSLContext still allowed the Common Name fallback that the link's library
# (urllib3) disables, so a CN-only certificate could pass the diagnosis although the link rejects it.
def test_the_diagnosis_uses_exactly_the_link_librarys_certificate_rules(monkeypatch):
    from urllib3.util.ssl_ import create_urllib3_context

    monkeypatch.delenv("SSLKEYLOGFILE", raising=False)
    mine, link = accounts.link_tls_context(), create_urllib3_context()
    for k in ("minimum_version", "maximum_version", "options", "verify_flags", "verify_mode", "check_hostname",
              "hostname_checks_common_name", "post_handshake_auth"):
        assert getattr(mine, k) == getattr(link, k), k
    assert mine.hostname_checks_common_name is False and mine.keylog_filename is None
    hs, seen = _calls({"issuer": ()})
    accounts.tls_diagnosis(handshake=hs)
    assert seen[0].hostname_checks_common_name is False                 # the context the diagnosis really used
