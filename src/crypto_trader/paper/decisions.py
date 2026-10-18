"""Strategy/decision source for shadow/paper sessions (Phase 10).

The AI/strategy layer is INJECTED, never embedded: the session calls a `DecisionSource` outside
the execution substrate, which keeps model inference off any engine event-processing thread.

The Agent/Strategist schema draft remains locked and out of scope; `StrategyDecision` here is the
minimal engine-independent shape the paper session needs (action, confidence, rationale, size).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol, runtime_checkable

from .errors import PaperConfigError, PaperStateError
from .feed import MarketObservation

__all__ = [
    "DecisionSource",
    "ScriptedDecisionSequence",
    "ScriptedDecisionSource",
    "StrategyDecision",
]

_ACTION_LEVELS = {"buy", "sell", "hold"}
_CONFIDENCES = {"low", "medium", "high"}
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,20}$")


@dataclass(frozen=True)
class StrategyDecision:
    """A decision produced by the injected AI/strategy layer."""

    action: str
    confidence: str
    rationale: str
    quantity: Decimal = Decimal("0")
    order_type: str = "limit"
    limit_price: Decimal | None = None
    decided_at: datetime | None = None
    decision_tolerance_bps: Decimal | None = None

    def __post_init__(self) -> None:
        if self.action not in _ACTION_LEVELS:
            raise PaperStateError(f"unknown decision action {self.action!r}.")
        if self.confidence not in _CONFIDENCES:
            raise PaperStateError(f"unknown decision confidence {self.confidence!r}.")
        if not self.rationale:
            raise PaperStateError("a decision requires a rationale.")
        if self.action == "hold":
            return
        if not isinstance(self.quantity, Decimal) or self.quantity <= 0:
            raise PaperStateError("a trade decision requires a positive Decimal quantity.")
        if self.order_type not in {"market", "limit"}:
            raise PaperStateError(f"unknown order type {self.order_type!r}.")
        if self.order_type == "limit":
            if not isinstance(self.limit_price, Decimal) or self.limit_price <= 0:
                raise PaperStateError("limit decisions require a positive Decimal limit_price.")
        elif self.limit_price is not None:
            raise PaperStateError("market decisions must not carry a limit_price.")


@runtime_checkable
class DecisionSource(Protocol):
    """The injected AI/strategy decision layer."""

    def decide(self, observation: MarketObservation) -> StrategyDecision: ...


@dataclass(frozen=True)
class ScriptedDecisionSource:
    """A deterministic decision source keyed by market reference (offline/replay use)."""

    script: Mapping[str, StrategyDecision]
    default: StrategyDecision | None = None

    def decide(self, observation: MarketObservation) -> StrategyDecision:
        decision = self.script.get(observation.market_reference, self.default)
        if decision is None:
            raise PaperConfigError(
                f"no scripted decision for market reference {observation.market_reference!r}."
            )
        return decision


@dataclass
class ScriptedDecisionSequence:
    """A deterministic decision source that walks a fixed sequence in order."""

    decisions: Sequence[StrategyDecision]

    def __post_init__(self) -> None:
        self._index = 0

    def decide(self, observation: MarketObservation) -> StrategyDecision:
        del observation
        if self._index >= len(self.decisions):
            raise PaperConfigError("scripted decision sequence exhausted.")
        decision = self.decisions[self._index]
        self._index += 1
        return decision


# R-02: inputs are artifacts, not callbacks. No inference provider is supported here.
import hashlib
import json
import sys
from dataclasses import asdict, is_dataclass, replace
from enum import Enum
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4


def input_json(value):
    def convert(item):
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, Enum):
            return item.value
        if is_dataclass(item):
            return convert(asdict(item))
        if isinstance(item, Mapping):
            return {str(k): convert(v) for k, v in item.items()}
        if isinstance(item, (tuple, list)):
            return [convert(v) for v in item]
        return item
    return json.dumps(convert(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def source_fingerprints():
    """Actual participating source bytes; unrelated venue WIP is excluded."""
    root = Path(__file__).resolve().parents[1]
    files = []
    for directory in ("paper", "events", "persistence", "risk", "validity", "safety", "execution/nautilus"):
        files.extend((root / directory).rglob("*.py"))
    files.extend(root / "execution" / name for name in ("ports.py", "errors.py"))
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(files)}


def _exact_keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise PaperStateError("FrozenRunInputs fields are missing or unknown.")


def _decimal(value, *, positive=False):
    if not isinstance(value, str):
        raise PaperStateError("frozen economic numbers must be Decimal strings.")
    try:
        parsed = Decimal(value)
    except Exception as exc:
        raise PaperStateError("invalid frozen Decimal.") from exc
    if not parsed.is_finite() or parsed < 0 or (positive and parsed == 0):
        raise PaperStateError("nonfinite or invalid frozen economic number.")
    if len(parsed.as_tuple().digits) > 34 or parsed > Decimal("1e15") or parsed.as_tuple().exponent < -34:
        raise PaperStateError("frozen number exceeds the supported numeric envelope.")
    return parsed


def _unique_json(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PaperStateError("duplicate frozen JSON key.")
        value[key] = item
    return value


@dataclass(frozen=True)
class FrozenRunInputs:
    """Canonical immutable bytes for a fully materialized historical input set."""

    data: bytes

    def __post_init__(self):
        if not isinstance(self.data, bytes) or len(self.data) > 16 * 1024 * 1024:
            raise PaperStateError("invalid or oversized frozen input artifact.")
        try:
            doc = json.loads(self.data, object_pairs_hook=_unique_json,
                             parse_constant=lambda _: (_ for _ in ()).throw(PaperStateError("invalid JSON number")))
            if input_json(doc) != self.data:
                raise PaperStateError("frozen input artifact is not canonical UTF-8 JSON.")
            self._validate(doc)
        except PaperStateError:
            raise
        except Exception as exc:
            raise PaperStateError("invalid FrozenRunInputs artifact.") from exc

    @property
    def sha256(self):
        return hashlib.sha256(self.data).hexdigest()

    def document(self):
        return json.loads(self.data)

    @classmethod
    def capture(cls, *, feed, decisions, config, instrument, book_updates, trades,
                cash, limits, validity_policy, safety_mode, code_tree):
        from .feed import FrozenMarketDataFeed
        if type(feed) is not FrozenMarketDataFeed or type(decisions) not in (ScriptedDecisionSource, ScriptedDecisionSequence):
            raise PaperStateError("dynamic decision or market source is unsupported by R-02.")
        observations = tuple(replace(o, observed_at=o.normalized_at()) for o in feed.observations())
        if type(decisions) is ScriptedDecisionSource:
            if decisions.default is not None or set(decisions.script) != {o.market_reference for o in observations}:
                raise PaperStateError("default/incomplete scripted decisions cannot be frozen.")
            values = tuple(decisions.script[o.market_reference] for o in observations)
        else:
            values = tuple(decisions.decisions)
            if len(values) != len(observations):
                raise PaperStateError("incomplete scripted decision sequence.")
        rows = []
        for index, (observation, decision) in enumerate(zip(observations, values)):
            if type(decision) is not StrategyDecision:
                raise PaperStateError("unproven scripted decision type.")
            row = asdict(decision)
            stamp = decision.decided_at or observation.observed_at
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise PaperStateError("frozen decision time must be aware.")
            row["decided_at"] = stamp.astimezone(timezone.utc)
            rows.append({"index": index, "market_reference": observation.market_reference,
                         "observation_sha256": hashlib.sha256(input_json(observation)).hexdigest(),
                         "decision": row})
        session = asdict(config)
        session.pop("session_id")
        session.pop("historical")
        queue = session.pop("queue")
        session.update(queue)
        effective_instrument = asdict(instrument)
        effective_instrument["starting_balance"] = f"{cash.initial_quote} {instrument.quote_currency}"
        doc = {"schema_version": 1, "source_kind": "scripted", "observations": observations,
               "decisions": rows, "instrument": effective_instrument, "book_updates": tuple(book_updates),
               "trades": tuple(trades), "cash": cash, "session": session,
               "limits": limits, "validity": validity_policy, "safety_mode": safety_mode,
               "provenance": {"code_tree": code_tree, "engine_version": "1.231.0",
                              "python_version": sys.version, "source_sha256": source_fingerprints(),
                              "dependencies": {n: version(n) for n in ("nautilus-trader", "pydantic")},
                              "decimal_precision": 34, "decimal_rounding": "ROUND_HALF_EVEN"}}
        return cls(input_json(doc))

    def _validate(self, doc):
        from crypto_trader.execution import InstrumentSpec, BookLevel, MarketBookUpdate, MarketTrade
        from crypto_trader.risk import load_limits
        from crypto_trader.validity import load_policy
        from .config import SpotCashSettings, load_session_config
        from .feed import FrozenMarketDataFeed
        _exact_keys(doc, ("schema_version", "source_kind", "observations", "decisions", "instrument",
                         "book_updates", "trades", "cash", "session", "limits", "validity", "safety_mode", "provenance"))
        if type(doc["schema_version"]) is not int or doc["schema_version"] != 1 or doc["source_kind"] != "scripted":
            raise PaperStateError("unsupported frozen schema or dynamic decisions.")
        _exact_keys(doc["cash"], ("initial_quote", "initial_base", "maker_fee", "taker_fee", "random_seed"))
        cash = SpotCashSettings(**{k: _decimal(v) if k != "random_seed" else v for k, v in doc["cash"].items()})
        _exact_keys(doc["instrument"], ("symbol", "venue", "base_currency", "quote_currency", "price_precision", "size_precision", "starting_balance"))
        instrument = InstrumentSpec(**doc["instrument"])
        if any(not isinstance(asset, str) or re.fullmatch("[A-Z0-9]{2,16}", asset) is None for asset in (instrument.base_currency, instrument.quote_currency)) or instrument.symbol != instrument.base_currency + instrument.quote_currency:
            raise PaperStateError("frozen instrument asset/symbol identity mismatch.")
        config = load_session_config(doc["session"])
        if instrument.starting_balance != f"{cash.initial_quote} {instrument.quote_currency}":
            raise PaperStateError("frozen native opening balance is not unified.")
        if type(instrument.price_precision) is not int or type(instrument.size_precision) is not int or not 0 <= instrument.price_precision <= 8 or not 0 <= instrument.size_precision <= 8 or instrument.base_currency == instrument.quote_currency:
            raise PaperStateError("unsupported frozen instrument precision/assets.")
        def representable(number, precision):
            if number.as_tuple().exponent < -precision:
                raise PaperStateError("frozen quantity/price exceeds declared precision.")
        if config.mode.value != "paper" or config.shadow_submits or config.symbol != instrument.symbol or Decimal(config.starting_equity_quote) != cash.initial_quote:
            raise PaperStateError("frozen cash/session scope or opening balance mismatch.")
        if doc["safety_mode"] != "normal":
            raise PaperStateError("R-02 requires an explicit normal initial safety state.")
        load_limits(doc["limits"])
        load_policy(doc["validity"])
        observations = self.observations()
        FrozenMarketDataFeed(entries=observations)
        if not observations or len(doc["decisions"]) != len(observations):
            raise PaperStateError("incomplete observation/decision set.")
        previous = None
        for index, (observation, row) in enumerate(zip(observations, doc["decisions"])):
            _exact_keys(row, ("index", "market_reference", "observation_sha256", "decision"))
            if type(row["index"]) is not int or row["index"] != index or row["market_reference"] != observation.market_reference or row["observation_sha256"] != hashlib.sha256(input_json(observation)).hexdigest():
                raise PaperStateError("frozen decision-to-observation identity mismatch.")
            _exact_keys(row["decision"], tuple(StrategyDecision.__dataclass_fields__))
            decision = self._decision(row["decision"])
            if observation.symbol != config.symbol or previous is not None and observation.observed_at <= previous:
                raise PaperStateError("frozen observation timeline is not strictly ordered.")
            if decision.decided_at < observation.observed_at or index + 1 < len(observations) and decision.decided_at > observations[index + 1].observed_at:
                raise PaperStateError("frozen decision violates causal time bounds.")
            if decision.action != "hold" and decision.order_type != "limit":
                raise PaperStateError("R-02 initially supports bounded limit orders only.")
            if decision.action != "hold":
                representable(decision.quantity, instrument.size_precision)
                representable(decision.limit_price, instrument.price_precision)
            previous = observation.observed_at
        for book in doc["book_updates"]:
            _exact_keys(book, ("levels", "ts_ns", "sequence", "action"))
            for level in book["levels"]:
                _exact_keys(level, ("side", "price", "size", "order_id"))
                if level["side"] not in {"BUY", "SELL"} or type(level["order_id"]) is not int:
                    raise PaperStateError("invalid frozen book level.")
                representable(_decimal(level["price"], positive=True), instrument.price_precision)
                representable(_decimal(level["size"]), instrument.size_precision)
            if not book["levels"] or book["action"] not in {"ADD", "UPDATE", "DELETE"} or type(book["sequence"]) is not int or book["sequence"] <= 0:
                raise PaperStateError("invalid frozen book update.")
        identities = set()
        for trade in doc["trades"]:
            _exact_keys(trade, ("price", "size", "aggressor", "ts_ns", "trade_id"))
            if not isinstance(trade["trade_id"], str) or not trade["trade_id"] or trade["trade_id"] in identities or trade["aggressor"] not in {"BUYER", "SELLER"}:
                raise PaperStateError("invalid or duplicate frozen trade identity.")
            identities.add(trade["trade_id"])
            representable(_decimal(trade["price"], positive=True), instrument.price_precision)
            representable(_decimal(trade["size"], positive=True), instrument.size_precision)
        for tape in (doc["book_updates"], doc["trades"]):
            times = [row["ts_ns"] for row in tape]
            if any(type(t) is not int or t <= 0 or t % 1000 for t in times) or times != sorted(times):
                raise PaperStateError("invalid/unrepresentable frozen market timeline.")
        _exact_keys(doc["provenance"], ("code_tree", "engine_version", "python_version", "source_sha256", "dependencies", "decimal_precision", "decimal_rounding"))
        prov = doc["provenance"]
        if re.fullmatch("[0-9a-f]{40}", prov["code_tree"]) is None or prov["engine_version"] != "1.231.0" or prov["decimal_precision"] != 34 or prov["decimal_rounding"] != "ROUND_HALF_EVEN":
            raise PaperStateError("unsupported frozen provenance/numeric policy.")

    def observations(self):
        rows = self.document()["observations"]
        result = []
        for row in rows:
            _exact_keys(row, tuple(MarketObservation.__dataclass_fields__))
            result.append(MarketObservation(symbol=row["symbol"], price=_decimal(row["price"], positive=True),
                observed_at=datetime.fromisoformat(row["observed_at"]), market_reference=row["market_reference"],
                volume=None if row["volume"] is None else _decimal(row["volume"])))
        return tuple(result)

    @staticmethod
    def _decision(row):
        values = dict(row)
        if any(type(values[name]) is not str for name in ("action", "confidence", "rationale", "order_type")) or not values["rationale"] or values["order_type"] not in {"limit", "market"}:
            raise PaperStateError("invalid frozen decision text/order type.")
        for name in ("quantity", "limit_price", "decision_tolerance_bps"):
            if values[name] is not None:
                values[name] = _decimal(values[name])
        values["decided_at"] = datetime.fromisoformat(values["decided_at"])
        if values["decided_at"].tzinfo is None or values["decided_at"].utcoffset() != timezone.utc.utcoffset(None) or values["decided_at"].isoformat() != row["decided_at"]:
            raise PaperStateError("frozen decision time must be aware.")
        if values["action"] == "hold" and values["quantity"] != 0:
            raise PaperStateError("frozen Hold cannot carry a trade quantity.")
        return StrategyDecision(**values)

    def decision_source(self):
        doc = self.document()
        return FrozenDecisionSource(tuple((row["observation_sha256"], self._decision(row["decision"]))
                                          for row in doc["decisions"]), self.sha256)

    def save(self, root):
        root = Path(root).resolve()
        directory = root / self.sha256
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "inputs.json"
        if target.exists():
            if target.read_bytes() != self.data:
                raise PaperStateError("existing frozen artifact conflicts; no overwrite allowed.")
            return target
        temporary = directory / f"inputs.{uuid4()}.partial"
        with temporary.open("xb") as handle:
            handle.write(self.data)
        if temporary.read_bytes() != self.data:
            raise PaperStateError("frozen artifact write verification failed.")
        # Windows rename fails if target already exists; no replacing old evidence.
        temporary.rename(target)
        return target

    @classmethod
    def approved(cls, spec):
        try:
            path = Path(spec.inputs_path)
            if path.stat().st_size > 16 * 1024 * 1024:
                raise PaperStateError("oversized frozen input artifact.")
            blob = path.read_bytes()
            if hashlib.sha256(blob).hexdigest() != spec.expected_inputs_sha256:
                raise PaperStateError("frozen input approval fingerprint mismatch.")
            bundle = cls(blob)
            prov = bundle.document()["provenance"]
            if prov["code_tree"] != spec.expected_code_tree or prov["source_sha256"] != source_fingerprints() or prov["python_version"] != sys.version or prov["dependencies"] != {n: version(n) for n in ("nautilus-trader", "pydantic")}:
                raise PaperStateError("frozen runtime/source provenance mismatch.")
            return bundle
        except PaperStateError:
            raise
        except Exception as exc:
            raise PaperStateError("frozen input artifact is unavailable or invalid.") from exc


@dataclass(frozen=True)
class FrozenDecisionSource:
    entries: tuple[tuple[str, StrategyDecision], ...]
    inputs_sha256: str

    def decide(self, observation):
        identity = hashlib.sha256(input_json(observation)).hexdigest()
        found = [decision for digest, decision in self.entries if digest == identity]
        if len(found) != 1:
            raise PaperStateError("frozen decision lookup is missing or ambiguous.")
        return found[0]
