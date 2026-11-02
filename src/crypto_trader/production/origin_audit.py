"""Opt-in host-keyed provenance, never recovered execution authority.

The deterministic host supplies a secret (outside the journal/artifact writer's
trust boundary) and pins the journal identity. No key is generated or loaded
from these files. This does not defend a compromised host/key or simultaneous
rollback of journal AND proofs: that needs an external monotonic checkpoint.
Dedicated proof directory/journal; legacy unsealed roots fail closed. Partial
writes deliberately require operator reconciliation, never automatic repair.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from uuid import UUID, uuid5

from crypto_trader.events import (
    StrategyDecisionRecorded, TradeIntentCreated, OrderSubmissionAttempted,
    OrderVenueIdentityRecorded,
)
from .errors import ProductionStateError

_SOURCE_NAMESPACE = UUID("f5a9b7ba-a3fb-4a58-9abe-877251452edf")
_TRACKED = (StrategyDecisionRecorded, TradeIntentCreated,
            OrderSubmissionAttempted, OrderVenueIdentityRecorded)
_LIMIT = 10000
_BYTES = 131072


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _event(event):
    return _json(event.model_dump(mode="json"))


def _fail():
    raise ProductionStateError("origin audit: missing, conflicting or unauthenticated lineage.")


class CrewOriginAudit:
    """Read-only restart verification plus private trusted-host issuance hooks."""

    def __init__(self, directory, *, key: bytes, journal_id: UUID,
                 checkpoint_reader=None):
        if type(key) is not bytes or len(key) < 32 or type(journal_id) is not UUID:
            raise ProductionStateError("origin audit requires an external host key and pinned journal id.")
        self._directory = Path(directory)
        self._key, self._journal_id = key, journal_id
        # This callback must read a pin independently protected from both journal
        # and proof directory. An ordinary nearby file cannot provide rollback safety.
        self._checkpoint_reader = checkpoint_reader
        self._directory_ok()

    def _directory_ok(self):
        p = self._directory
        if not p.is_dir() or p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
            _fail()

    def _mac(self, body):
        return hmac.new(self._key, b"crew-origin-audit-v1\0" + _json(body).encode(), hashlib.sha256).hexdigest()

    def _load(self):
        self._directory_ok()
        records = {}
        for n, path in enumerate(self._directory.iterdir()):
            if n >= _LIMIT or not path.is_file() or path.is_symlink() or path.stat().st_size > _BYTES:
                _fail()
            try:
                raw = path.read_bytes()
                if len(raw) > _BYTES:
                    _fail()
                envelope = json.loads(raw)
                # Exact canonical bytes also reject duplicate keys and ambiguous encodings.
                if raw != (_json(envelope) + "\n").encode() or set(envelope) != {"body", "mac"}:
                    _fail()
                body, mac = envelope["body"], envelope["mac"]
                if (body["version"] != 1 or body["journal_id"] != str(self._journal_id)
                        or type(mac) is not str or not hmac.compare_digest(mac, self._mac(body))
                        or path.name != str(UUID(body["id"])) + ".json"
                        or body["id"] in records):
                    _fail()
                records[body["id"]] = body
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                raise ProductionStateError("origin audit: invalid authenticated record.") from exc
        return records

    def _write(self, body):
        self._directory_ok()
        raw = (_json({"body": body, "mac": self._mac(body)}) + "\n").encode()
        if len(raw) > _BYTES:
            _fail()
        with (self._directory / (body["id"] + ".json")).open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())

    def _body(self, kind, event_id, **fields):
        return dict(version=1, journal_id=str(self._journal_id), kind=kind,
                    id=str(event_id), **fields)

    def _verify(self, journal, records, *, extra=()):
        if journal.journal_id() != self._journal_id:
            _fail()
        events = {}
        for event in (*journal.read_range(), *extra):
            if isinstance(event, _TRACKED):
                if event.event_id in events or len(events) >= _LIMIT:
                    _fail()
                events[event.event_id] = event
        covered, sources, attempts, roots, venues = set(), set(), {}, {}, set()
        for body in records.values():
            if body.get("kind") != "origin":
                continue
            try:
                source = UUID(body["source"])
                decision = events[uuid5(_SOURCE_NAMESPACE, f"crew-decision:{source}")]
                created = events[uuid5(_SOURCE_NAMESPACE, f"crew-intent:{source}")]
                digest = body["plan_sha256"]
                if (type(decision) is not StrategyDecisionRecorded or type(created) is not TradeIntentCreated
                        or source in sources or created.causation_id != decision.event_id
                        or (created.correlation_id, created.symbol, created.strategy_id, created.producer)
                        != (decision.correlation_id, decision.symbol, decision.strategy_id, decision.producer)
                        or created.payload.side != decision.payload.action
                        or type(digest) is not str or len(digest) != 64
                        or any(c not in "0123456789abcdef" for c in digest)
                        or body != self._body("origin", created.event_id, source=str(source),
                            plan_sha256=digest, decision=_event(decision), intent=_event(created))):
                    _fail()
                sources.add(source)
                roots[created.event_id] = created
                covered.update((created.event_id, decision.event_id))
            except (KeyError, ValueError, TypeError) as exc:
                raise ProductionStateError("origin audit: missing or conflicting source root.") from exc
        for event in events.values():
            if not isinstance(event, OrderSubmissionAttempted):
                continue
            from crypto_trader.execution.binance_testnet.translate import deterministic_order_uuid
            intent = roots.get(event.payload.intent_id)
            if intent is None or intent.event_id in attempts:
                _fail()
            if ((event.correlation_id, event.symbol, event.strategy_id) !=
                    (intent.correlation_id, intent.symbol, intent.strategy_id)
                    or any(getattr(event.payload, name) != getattr(intent.payload, name)
                           for name in ("side", "order_type", "quantity", "limit_price"))
                    or event.order_id != deterministic_order_uuid(event.payload.venue_reference)
                    or records.get(str(event.event_id)) != self._body(
                        "attempt", event.event_id, intent=str(intent.event_id), event=_event(event))):
                _fail()
            attempts[intent.event_id] = event
            covered.add(event.event_id)
        references = set()
        for attempt in attempts.values():
            reference = (attempt.symbol, attempt.payload.venue_reference)
            if reference in references:
                _fail()
            references.add(reference)
        seen_identities = set()
        for event in events.values():
            if not isinstance(event, OrderVenueIdentityRecorded):
                continue
            attempt = attempts.get(event.payload.intent_id)
            if attempt is None or event.payload.intent_id in seen_identities:
                _fail()
            venue = (event.symbol, event.payload.venue_order_id)
            if ((event.order_id, event.symbol, event.payload.venue_reference, event.producer, event.correlation_id)
                    != (attempt.order_id, attempt.symbol, attempt.payload.venue_reference,
                        attempt.payload.venue_producer, attempt.correlation_id)
                    or venue in venues or records.get(str(event.event_id)) != self._body(
                        "identity", event.event_id, attempt=str(attempt.event_id), event=_event(event))):
                _fail()
            seen_identities.add(event.payload.intent_id)
            venues.add(venue)
            covered.add(event.event_id)
        if covered != set(events) or len(records) != len(roots) + len(attempts) + len(venues):
            _fail()
        return tuple(sorted(sources, key=str))

    def audit(self, journal):
        """Return inert source IDs; never mint tickets, append events or send requests."""
        return self._verify(journal, self._load())

    def export_checkpoint(self, journal):
        """Deterministic snapshot to be pinned OUTSIDE this journal/proof trust domain.

        Exporting does not persist, sign, advance or certify a monotonic witness.
        The trusted host must separately protect and authenticate the returned pin.
        """
        before = journal.revision()
        records = self._load()
        self._verify(journal, records)
        after = journal.revision()
        if before != after:
            raise ProductionStateError("origin checkpoint: journal changed during audit.")
        snapshot = {
            "journal_id": str(self._journal_id),
            "revision": after.fingerprint,
            "entries": len(after.entries),
            "receipts": sorted((key, _json(body)) for key, body in records.items()),
        }
        digest = hashlib.sha256(
            b"crew-origin-checkpoint-v1\\0" + _json(snapshot).encode("utf-8")
        ).hexdigest()
        return {"version": 1, "journal_id": str(self._journal_id),
                "entries": len(after.entries), "digest": digest}

    def verify_external_checkpoint(self, journal):
        """Fail closed if an independently trusted restart pin is missing or stale."""
        reader = self._checkpoint_reader
        if not callable(reader):
            raise ProductionStateError("origin checkpoint: no external trusted reader.")
        try:
            expected = reader()
        except Exception as exc:
            raise ProductionStateError("origin checkpoint: external reader failed.") from exc
        actual = self.export_checkpoint(journal)
        if (type(expected) is not dict
                or set(expected) != set(actual)
                or type(expected.get("version")) is not int
                or type(expected.get("entries")) is not int
                or type(expected.get("journal_id")) is not str
                or type(expected.get("digest")) is not str
                or expected["version"] != actual["version"]
                or expected["entries"] != actual["entries"]
                or expected["journal_id"] != actual["journal_id"]
                or len(expected["digest"]) != 64
                or not hmac.compare_digest(expected["digest"], actual["digest"])):
            raise ProductionStateError("origin checkpoint: untrusted or stale host pin.")


    def seal_origin(self, journal, source_delivery_id, raw_plan, decision, created):
        body = self._body("origin", created.event_id, source=str(source_delivery_id),
                          plan_sha256=hashlib.sha256(raw_plan.encode()).hexdigest(),
                          decision=_event(decision), intent=_event(created))
        records = self._load()
        if body["id"] in records:
            _fail()
        records[body["id"]] = body
        self._verify(journal, records)
        self._write(body)

    def seal_attempt(self, journal, marker):
        body = self._body("attempt", marker.event_id, intent=str(marker.payload.intent_id), event=_event(marker))
        records = self._load()
        if body["id"] in records:
            _fail()
        records[body["id"]] = body
        self._verify(journal, records)
        self._write(body)

    def seal_identity(self, journal, identity):
        attempts = [event for event in journal.read_range() if isinstance(event, OrderSubmissionAttempted)
                    and event.payload.intent_id == identity.payload.intent_id]
        if len(attempts) != 1:
            _fail()
        body = self._body("identity", identity.event_id, attempt=str(attempts[0].event_id), event=_event(identity))
        records = self._load()
        if body["id"] in records:
            _fail()
        records[body["id"]] = body
        self._verify(journal, records, extra=(identity,))
        self._write(body)
