"""Per-invocation signed Binance startup evidence. No order mutation/cache adoption."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from crypto_trader.recovery import StaticVenueStateSource
from crypto_trader.recovery.models import VenueOrderReport

from .client import BinanceSpotTestnetClient
from .errors import BinanceTestnetError, OrderNotFoundError
from .runtime import TestnetOrderRuntime
from .translate import lifecycle_state_name


class StartupEvidenceError(BinanceTestnetError):
    """Authoritative startup evidence is incomplete, stale, mismatched or changed."""




@dataclass(frozen=True)
class StartupVenueEvidence:
    nonce: UUID
    fence_term: int
    client: BinanceSpotTestnetClient
    transport: object
    credentials: object
    endpoint: str
    symbol: str
    instrument: object
    journal: object
    journal_fingerprint: str
    required_references: tuple[str, ...]
    open_references: tuple[str, ...]
    status_references: tuple[str, ...]
    absent_references: tuple[str, ...]
    reports: tuple[VenueOrderReport, ...]
    collected_at: datetime

    def summary(self):
        return {
            "source": "binance.signed.startup", "invocation_id": str(self.nonce),
            "fence_term": self.fence_term, "endpoint": self.endpoint, "symbol": self.symbol,
            "journal_fingerprint": self.journal_fingerprint,
            "open_orders_queried": True, "open_order_count": len(self.open_references),
            "status_lookup_count": len(self.status_references),
            "absent_order_count": len(self.absent_references),
            "required_references": list(self.required_references),
            "collected_at": self.collected_at.isoformat(),
        }


class BinanceStartupCollector:
    def __init__(self, execution, *, journal, instrument, endpoint):
        self.execution = execution
        self.client = getattr(execution, "client", None)
        self.journal = journal
        self.instrument = instrument
        self.endpoint = endpoint
        self.transport = getattr(self.client, "transport", None)
        self.credentials = getattr(self.client, "credentials", None)
        self.require_context()

    def require_context(self):
        if (
            type(self.execution) is not TestnetOrderRuntime
            or type(self.client) is not BinanceSpotTestnetClient
            or self.execution.client is not self.client
            or self.execution.instrument != self.instrument
            or self.execution.durable_attempt_journal() is not self.journal
            or self.client.symbol != self.instrument.symbol
            or self.client.base_url != self.endpoint
            or self.client.transport is not self.transport
            or self.client.credentials is not self.credentials
        ):
            raise StartupEvidenceError("unsupported or changed Binance startup execution context.")

    def collect(self, *, nonce, fence_term, view, clock):
        self.require_context()
        self.client.synchronize_clock()
        orders = self.client.startup_open_orders(self.instrument.symbol)
        open_refs = tuple(order.client_order_id for order in orders)
        if len(set(open_refs)) != len(open_refs):
            raise StartupEvidenceError("duplicate open-order identities in startup evidence.")
        queried, absent = [], []
        all_orders = list(orders)
        for reference in view.required_references:
            if reference in open_refs:
                continue
            queried.append(reference)
            try:
                all_orders.append(self.client.startup_order_status(self.instrument.symbol, reference))
            except OrderNotFoundError:
                absent.append(reference)
        self.require_context()
        return StartupVenueEvidence(
            nonce=nonce, fence_term=fence_term, client=self.client,
            transport=self.transport, credentials=self.credentials, endpoint=self.endpoint,
            symbol=self.instrument.symbol, instrument=self.instrument, journal=self.journal,
            journal_fingerprint=view.fingerprint, required_references=view.required_references,
            open_references=open_refs, status_references=tuple(queried),
            absent_references=tuple(absent), collected_at=clock(),
            reports=tuple(VenueOrderReport(
                order_reference=order.client_order_id, venue_order_id=order.venue_order_id,
                state=lifecycle_state_name(order.state), filled_quantity=order.executed_quantity,
                remaining_quantity=order.remaining_quantity, reported_at=order.transact_time,
            ) for order in all_orders),
        )

    def validate(self, batch, *, nonce, fence_term, view):
        self.require_context()
        if type(batch) is not StartupVenueEvidence or (
            batch.nonce != nonce or batch.fence_term != fence_term
            or batch.client is not self.client or batch.transport is not self.transport
            or batch.credentials is not self.credentials or batch.journal is not self.journal
            or batch.endpoint != self.endpoint or batch.symbol != self.instrument.symbol
            or batch.instrument != self.instrument or batch.journal_fingerprint != view.fingerprint
            or batch.required_references != view.required_references
        ):
            raise StartupEvidenceError("missing, stale or mismatched startup evidence.")
        expected_status = tuple(ref for ref in view.required_references if ref not in batch.open_references)
        report_refs = tuple(report.order_reference for report in batch.reports)
        if (
            batch.status_references != expected_status
            or len(set(batch.open_references)) != len(batch.open_references)
            or len(set(batch.absent_references)) != len(batch.absent_references)
            or not set(batch.absent_references).issubset(expected_status)
            or len(set(report_refs)) != len(report_refs)
            or set(report_refs) != (
                set(batch.open_references) | (set(expected_status) - set(batch.absent_references))
            )
        ):
            raise StartupEvidenceError("startup query/report coverage is incomplete.")
        return StaticVenueStateSource(entries=batch.reports, name="production.validated-binance-startup")
