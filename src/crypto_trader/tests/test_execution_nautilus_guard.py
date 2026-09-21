"""Gate 3/9: engine version and queue-aware configuration guards."""

import pytest

from crypto_trader.execution import (
    EngineVersionError,
    QueueAwareConfigurationError,
)
from crypto_trader.execution.nautilus import guard
from crypto_trader.execution.nautilus import (
    SUPPORTED_ENGINE_VERSION,
    TESTED_BOOK_TYPE_NAME,
    QueueAwareSettings,
    require_queue_aware_settings,
    require_supported_engine_version,
)


def test_supported_version_constant_matches_spike_tested_engine():
    assert SUPPORTED_ENGINE_VERSION == "1.231.0"
    assert TESTED_BOOK_TYPE_NAME == "L2_MBP"


def test_installed_engine_is_the_supported_version():
    assert guard.installed_engine_version() == SUPPORTED_ENGINE_VERSION
    assert require_supported_engine_version() == SUPPORTED_ENGINE_VERSION


@pytest.mark.parametrize("other", ["1.230.0", "1.231.1", "2.0.0", "1.220.0"])
def test_unsupported_engine_versions_fail_fast(other):
    with pytest.raises(EngineVersionError) as excinfo:
        require_supported_engine_version(other)

    assert SUPPORTED_ENGINE_VERSION in str(excinfo.value)


def test_tested_queue_aware_settings_pass_the_guard():
    require_queue_aware_settings(guard.tested_queue_aware_settings())  # must not raise


@pytest.mark.parametrize(
    "settings",
    [
        QueueAwareSettings(queue_position=False, liquidity_consumption=True, book_type="L2_MBP"),
        QueueAwareSettings(queue_position=True, liquidity_consumption=False, book_type="L2_MBP"),
        QueueAwareSettings(queue_position=True, liquidity_consumption=True, book_type="L1_MBP"),
        QueueAwareSettings(queue_position=True, liquidity_consumption=True, book_type="L3_MBO"),
    ],
)
def test_queue_aware_configuration_mismatch_fails_fast(settings):
    """Omitting any part of the tested combination silently disables queue modelling."""
    with pytest.raises(QueueAwareConfigurationError) as excinfo:
        require_queue_aware_settings(settings)

    assert "L2_MBP" in str(excinfo.value)


def test_error_message_explains_the_silent_degradation_risk():
    with pytest.raises(QueueAwareConfigurationError) as excinfo:
        require_queue_aware_settings(
            QueueAwareSettings(queue_position=True, liquidity_consumption=True, book_type="L1_MBP")
        )

    assert "silently disabled" in str(excinfo.value)
