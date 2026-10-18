"""Explicit session configuration for shadow/paper runs (Phase 10).

Configuration is explicit and fails closed: no default capital numbers, no implicit mode, no
silent queue-aware degradation, and no credential field anywhere (paper/shadow never touch an
authenticated endpoint).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4
from pathlib import Path

from .errors import PaperConfigError
from .modes import (
    QueueAwareSettings,
    TradingMode,
    require_tested_queue_aware_settings,
    tested_queue_aware_settings,
)

__all__ = ["SessionConfig", "load_session_config"]


@dataclass(frozen=True)
class SpotCashSettings:
    """R-02 opt-in settings. Legacy MARGIN defaults are deliberately unchanged."""

    initial_quote: Decimal
    initial_base: Decimal = Decimal("0")
    maker_fee: Decimal = Decimal("0.0002")
    taker_fee: Decimal = Decimal("0.0004")
    random_seed: int = 0

    def __post_init__(self):
        for name in ("initial_quote", "initial_base", "maker_fee", "taker_fee"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise PaperConfigError(f"cash {name} must be a finite nonnegative Decimal.")
        if self.initial_quote <= 0 or self.initial_base != 0:
            raise PaperConfigError("R-02 requires positive Quote and zero opening Base.")
        if max(self.maker_fee, self.taker_fee) >= 1 or type(self.random_seed) is not int:
            raise PaperConfigError("invalid cash fee or random seed.")


@dataclass(frozen=True)
class HistoricalRunSpec:
    """Independent approval pins; never inferred from the file being validated."""

    inputs_path: Path
    expected_inputs_sha256: str
    expected_code_tree: str

    def __post_init__(self):
        if not Path(self.inputs_path).is_absolute():
            raise PaperConfigError("historical inputs path must be absolute.")
        for value, size in ((self.expected_inputs_sha256, 64), (self.expected_code_tree, 40)):
            if not isinstance(value, str) or re.fullmatch(f"[0-9a-f]{{{size}}}", value) is None:
                raise PaperConfigError("invalid historical approval fingerprint.")

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,20}$")
_ALLOWED_KEYS = {
    "mode",
    "symbol",
    "strategy_id",
    "session_id",
    "producer",
    "shadow_submits",
    "queue_aware",
    "queue_position",
    "liquidity_consumption",
    "book_type",
    "starting_equity_quote",
    "historical",
}


@dataclass(frozen=True)
class SessionConfig:
    """A resolved shadow/paper session configuration."""

    mode: TradingMode
    symbol: str
    strategy_id: str
    session_id: UUID = field(default_factory=uuid4)
    producer: str = "application.paper"
    shadow_submits: bool = False
    queue_aware: bool = True
    queue: QueueAwareSettings = field(default_factory=tested_queue_aware_settings)
    starting_equity_quote: str = "100000"
    historical: HistoricalRunSpec | None = None

    def __post_init__(self) -> None:
        if self.historical is not None and not isinstance(self.historical, HistoricalRunSpec):
            raise PaperConfigError("historical must be an explicit HistoricalRunSpec.")
        if self.historical is not None and (self.mode is not TradingMode.PAPER or self.shadow_submits):
            raise PaperConfigError("historical cash requires Paper without shadow submission.")
        if not isinstance(self.mode, TradingMode):
            raise PaperConfigError("mode must be a TradingMode.")
        if not isinstance(self.symbol, str) or _SYMBOL_PATTERN.fullmatch(self.symbol) is None:
            raise PaperConfigError(
                f"symbol must be an uppercase market symbol such as ETHUSDT; got {self.symbol!r}."
            )
        if not isinstance(self.strategy_id, str) or not self.strategy_id.strip():
            raise PaperConfigError("strategy_id must be a nonempty string.")
        if not isinstance(self.producer, str) or not self.producer.strip():
            raise PaperConfigError("producer must be a nonempty string.")
        if not isinstance(self.session_id, UUID):
            raise PaperConfigError("session_id must be a UUID.")
        if not isinstance(self.shadow_submits, bool) or not isinstance(self.queue_aware, bool):
            raise PaperConfigError("shadow_submits and queue_aware must be booleans.")
        if self.places_simulated_orders and not self.queue_aware:
            raise PaperConfigError(
                "paper mode requires queue-aware simulation "
                "(queue_position=True, liquidity_consumption=True, book_type=L2_MBP)."
            )
        if self.queue_aware:
            require_tested_queue_aware_settings(self.queue)
        try:
            equity = Decimal(str(self.starting_equity_quote))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PaperConfigError("starting_equity_quote must be a positive number string.") from exc
        if not equity.is_finite() or equity <= 0:
            raise PaperConfigError("starting_equity_quote must be a positive number string.")

    @property
    def places_simulated_orders(self) -> bool:
        return self.mode.places_simulated_orders or (
            self.mode is TradingMode.SHADOW and self.shadow_submits
        )


def _strict_bool(value: object, field_name: str) -> bool:
    """Parse a config boolean strictly. Reject anything except bool or 'true'/'false'.

    bool("false") is True in Python; using bool() on untrusted config silently
    corrupts boolean settings.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    raise PaperConfigError(
        f"{field_name} must be a boolean or 'true'/'false'; got {value!r}."
    )

def load_session_config(config: Mapping[str, Any]) -> SessionConfig:
    """Build a :class:`SessionConfig` from an explicit mapping (unknown keys rejected)."""
    if not isinstance(config, Mapping):
        raise PaperConfigError("session configuration must be a mapping.")
    unknown = set(config) - _ALLOWED_KEYS
    if unknown:
        raise PaperConfigError(f"unknown session configuration keys: {sorted(unknown)}")
    for required in ("mode", "symbol", "strategy_id"):
        if required not in config:
            raise PaperConfigError(f"session configuration requires {required!r}.")
    try:
        mode = TradingMode(str(config["mode"]))
    except ValueError as exc:
        raise PaperConfigError(f"unknown trading mode {config['mode']!r}.") from exc

    session_id = config.get("session_id")
    if session_id is not None and not isinstance(session_id, UUID):
        try:
            session_id = UUID(str(session_id))
        except (TypeError, ValueError) as exc:
            raise PaperConfigError("session_id must be a UUID.") from exc

    queue = QueueAwareSettings(
        queue_position=_strict_bool(config.get("queue_position", True), "queue_position"),
        liquidity_consumption=_strict_bool(
            config.get("liquidity_consumption", True), "liquidity_consumption"
        ),
        book_type=str(config.get("book_type", "L2_MBP")),
    )

    return SessionConfig(
        mode=mode,
        symbol=config["symbol"],
        strategy_id=config["strategy_id"],
        session_id=session_id or uuid4(),
        producer=config.get("producer", "application.paper"),
        shadow_submits=_strict_bool(config.get("shadow_submits", False), "shadow_submits"),
        queue_aware=_strict_bool(config.get("queue_aware", True), "queue_aware"),
        queue=queue,
        starting_equity_quote=str(config.get("starting_equity_quote", "100000")),
        historical=config.get("historical"),
    )
