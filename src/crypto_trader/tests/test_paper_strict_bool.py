"""High-3: strict boolean parsing in paper session config."""
from __future__ import annotations

import pytest

from crypto_trader.paper.config import load_session_config
from crypto_trader.paper.errors import PaperConfigError


def _base(**overrides):
    cfg = {
        "mode": "shadow",
        "symbol": "ETHUSDT",
        "strategy_id": "s1",
    }
    cfg.update(overrides)
    return cfg


def test_string_false_does_not_become_true():
    """Regression: 'false' string must not silently become True."""
    cfg = load_session_config(
        _base(shadow_submits="false", queue_aware="true")
    )
    assert cfg.shadow_submits is False
    assert cfg.queue_aware is True


@pytest.mark.parametrize(
    "value,expected",
    [("true", True), ("false", False), (True, True), (False, False)],
)
def test_shadow_submits_strict_parsing(value, expected):
    cfg = load_session_config(_base(shadow_submits=value))
    assert cfg.shadow_submits is expected


@pytest.mark.parametrize(
    "value,expected",
    [("true", True), ("false", False)],
)
def test_queue_aware_strict_parsing(value, expected):
    cfg = load_session_config(_base(queue_aware=value))
    assert cfg.queue_aware is expected


@pytest.mark.parametrize(
    "value,expected",
    [("true", True), ("false", False)],
)
def test_queue_position_strict_parsing(value, expected):
    cfg = load_session_config(
        _base(queue_position=value, queue_aware=False)
    )
    assert cfg.queue.queue_position is expected


@pytest.mark.parametrize(
    "value,expected",
    [("true", True), ("false", False)],
)
def test_liquidity_consumption_strict_parsing(value, expected):
    cfg = load_session_config(
        _base(liquidity_consumption=value, queue_aware=False)
    )
    assert cfg.queue.liquidity_consumption is expected


@pytest.mark.parametrize(
    "field",
    [
        "shadow_submits",
        "queue_aware",
        "queue_position",
        "liquidity_consumption",
    ],
)
@pytest.mark.parametrize(
    "value",
    ["yes", "no", "1", "0", "on", "off", ""],
)
def test_non_boolean_strings_rejected(field, value):
    with pytest.raises(PaperConfigError, match=field):
        load_session_config(_base(**{field: value}))
