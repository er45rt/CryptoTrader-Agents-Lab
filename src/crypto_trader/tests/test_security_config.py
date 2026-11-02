"""S-03 strict symbol, TLS URL, and key environment allowlist regressions."""
import pytest

from crypto_trader.input_security import require_https_base_url, valid_market_symbol
from crypto_trader.llm.config import LLMConfigError, load_profile
from crypto_trader.main import setup_environment, validate_inputs
from crypto_trader.tools.binance_public_tool import BinancePublicTool


@pytest.mark.parametrize("symbol", ["ETHUSDT", "BTCUSDT", "A1234", "X" * 20])
def test_ascii_symbol_positive(symbol):
    assert valid_market_symbol(symbol)
    assert validate_inputs({"target_symbol": symbol, "kline_interval": "4h", "kline_limit": 72})


@pytest.mark.parametrize("symbol", [
    "", "ETH", "X" * 21, "ethusdt", "ETHUSDT\n", "ETHUSDT\x00", "ETH/USDT",
    "ETH USDT", "ETHUSDT?", "../ETHUSDT", "ＥＴＨＵＳＤＴ", 12345, None,
])
def test_symbol_must_match_exact_ascii_pattern(symbol):
    assert valid_market_symbol(symbol) is False
    assert not validate_inputs({"target_symbol": symbol, "kline_interval": "4h", "kline_limit": 72})


@pytest.mark.parametrize("url", [
    "http://api.binance.com", "ftp://example.com", "https://",
    "https://user:password@example.com", "https://example.com?token=1",
    "https://example.com/#token", "HTTPS://example.com ", "https://example.com\n",
    "https://example.com:99999", "example.com", "https:///api",
])
def test_reject_insecure_or_malformed_base_url(url):
    with pytest.raises(ValueError):
        require_https_base_url(url)


def test_valid_https_url_normalized_without_trailing_slash():
    assert require_https_base_url("https://api.deepseek.com/v1/") == "https://api.deepseek.com/v1"


def test_binance_override_rejected_before_requests(monkeypatch):
    monkeypatch.setenv("BINANCE_API_BASE_URL", "http://api.binance.com")
    assert setup_environment() is False
    with pytest.raises(ValueError):
        BinancePublicTool()


def test_binance_https_override_allowed(monkeypatch):
    monkeypatch.setenv("BINANCE_API_BASE_URL", "https://api.binance.us/")
    assert setup_environment() is True
    assert BinancePublicTool().base_url == "https://api.binance.us"


@pytest.mark.parametrize("name", ["DEEPSEEK_BASE_URL", "DEEPSEEK_CANARY_BASE_URL"])
def test_deepseek_base_url_https_only(name):
    with pytest.raises(LLMConfigError, match="HTTPS"):
        load_profile("deepseek_canary", env={name: "http://api.deepseek.com"})


@pytest.mark.parametrize("name", ["DEEPSEEK_API_KEY_ENV", "DEEPSEEK_CANARY_API_KEY_ENV"])
def test_unapproved_secret_env_name_rejected(name):
    with pytest.raises(LLMConfigError, match="allowlist"):
        load_profile("deepseek_canary", env={name: "AWS_SECRET_ACCESS_KEY"})
