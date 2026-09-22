"""Operational pre-live break-glass hardening (Phase 13).

Phase 9 shipped the break-glass *foundation*: an engine-independent port plus a simulation, with
the live break-glass path documented as a PRE-LIVE MANDATORY GAP. Phase 13 turns that foundation
into a real operational control path that is still strictly pre-live:

* deterministic operator commands (no LLM may decide, approve, translate, delay or execute);
* an ingress that is independent of CrewAI, the LLM workers, the strategist/analyst queues, the AI
  backlog and the normal strategy decision flow;
* one authoritative mutable trading-state writer, with explicit ownership/fencing/takeover so
  emergency venue-side control can never race a normal writer;
* fail-closed progression ``normal -> degraded -> recovery_required`` (or directly into a more
  restrictive, already-defined safety mode), with reconciliation and an explicit operator
  acknowledgement mandatory before normal trading may be restored;
* additive-only event contracts: the new ``safety.operator_command.recorded`` contract records every
  command and its disposition; the Phase 3/6/7/9 contracts are semantically untouched.

Scope: offline/scripted infrastructure and Binance SPOT TESTNET only. There is no mainnet endpoint,
no real capital, no withdrawal capability and no liquidation action anywhere in this package.
"""

from .commands import (
    ACTION_TARGET_MODES,
    BREAK_GLASS_ACTIONS,
    LIFECYCLE_COMMANDS,
    OperatorCommand,
    OperatorCommandType,
    parse_operator_command,
)
from .control import (
    BREAK_GLASS_PRODUCER,
    KILL_SWITCH_ACTIONS,
    BreakGlassControl,
    BreakGlassStatus,
    OperatorCommandOutcome,
)
from .epoch import (
    BreakGlassEpoch,
    BreakGlassLedger,
    OperatorAcknowledgement,
    RecordedOperatorCommand,
    derive_ledger,
)
from .errors import (
    BreakGlassActiveError,
    BreakGlassCommandError,
    BreakGlassConfigError,
    BreakGlassError,
    BreakGlassRefusedError,
    BreakGlassStateError,
    MalformedOperatorCommandError,
    OwnershipStateError,
    StaleWriterFenceError,
    VenueControlError,
)
from .ownership import (
    OWNERSHIP_SCHEMA_VERSION,
    OwnerKind,
    OwnershipRecord,
    OwnershipStore,
    WriterFence,
    WriterGuard,
)
from .state import SafetyStateLoad, SafetyStateStore, replay_safety_state
from .venue import (
    BinanceSpotTestnetEmergencyControl,
    EmergencyPreflight,
    EmergencyVenuePort,
    PolicyOnlyVenueControl,
    VenueControlOutcome,
    unavailable_outcome,
)

__all__ = [
    "ACTION_TARGET_MODES",
    "BREAK_GLASS_ACTIONS",
    "BREAK_GLASS_PRODUCER",
    "KILL_SWITCH_ACTIONS",
    "LIFECYCLE_COMMANDS",
    "OWNERSHIP_SCHEMA_VERSION",
    "BinanceSpotTestnetEmergencyControl",
    "BreakGlassActiveError",
    "BreakGlassCommandError",
    "BreakGlassConfigError",
    "BreakGlassControl",
    "BreakGlassEpoch",
    "BreakGlassError",
    "BreakGlassLedger",
    "BreakGlassRefusedError",
    "BreakGlassStateError",
    "BreakGlassStatus",
    "EmergencyPreflight",
    "EmergencyVenuePort",
    "MalformedOperatorCommandError",
    "OperatorAcknowledgement",
    "OperatorCommand",
    "OperatorCommandOutcome",
    "OperatorCommandType",
    "OwnerKind",
    "OwnershipRecord",
    "OwnershipStateError",
    "OwnershipStore",
    "PolicyOnlyVenueControl",
    "RecordedOperatorCommand",
    "SafetyStateLoad",
    "SafetyStateStore",
    "StaleWriterFenceError",
    "VenueControlError",
    "VenueControlOutcome",
    "WriterFence",
    "WriterGuard",
    "derive_ledger",
    "parse_operator_command",
    "replay_safety_state",
    "unavailable_outcome",
]
