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

from .errors import PaperConfigError
from .modes import (
    QueueAwareSettings,
    TradingMode,
    require_tested_queue_aware_settings,
    tested_queue_aware_settings,
)

__all__ = ["SessionConfig", "load_session_config"]

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

    def __post_init__(self) -> None:
        if not isinstance(self.mode, TradingMode):
            raise PaperConfigError("mode must be a TradingMode.")
        if not _SYMBOL_PATTERN.match(self.symbol):
            raise PaperConfigError(
                f"symbol must be an uppercase market symbol such as ETHUSDT; got {self.symbol!r}."
            )
        if not self.strategy_id:
            raise PaperConfigError("strategy_id is required.")
        if not self.producer:
            raise PaperConfigError("producer is required.")
        if not isinstance(self.shadow_submits, bool) or not isinstance(self.queue_aware, bool):
            raise PaperConfigError("shadow_submits and queue_aware must be booleans.")
        if self.mode is TradingMode.PAPER and not self.queue_aware:
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
        if equity <= 0:
            raise PaperConfigError("starting_equity_quote must be a positive number string.")

    @property
    def places_simulated_orders(self) -> bool:
        return self.mode.places_simulated_orders or (
            self.mode is TradingMode.SHADOW and self.shadow_submits
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
        queue_position=bool(config.get("queue_position", True)),
        liquidity_consumption=bool(config.get("liquidity_consumption", True)),
        book_type=str(config.get("book_type", "L2_MBP")),
    )

    return SessionConfig(
        mode=mode,
        symbol=str(config["symbol"]),
        strategy_id=str(config["strategy_id"]),
        session_id=session_id or uuid4(),
        producer=str(config.get("producer", "application.paper")),
        shadow_submits=bool(config.get("shadow_submits", False)),
        queue_aware=bool(config.get("queue_aware", True)),
        queue=queue,
        starting_equity_quote=str(config.get("starting_equity_quote", "100000")),
    )
