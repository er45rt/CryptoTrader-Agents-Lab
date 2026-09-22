"""Helper process for cross-process journal durability tests (not a test module).

Usage: python journal_writer_child.py <db_path> <count>
Writes ``count`` deterministic Phase 3 events, then exits. Never used by production code.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from datetime import timezone as _tz
from uuid import UUID

from crypto_trader.events import StrategyDecisionPayload, StrategyDecisionRecorded
from crypto_trader.persistence import SQLiteEventJournal

CORRELATION = UUID("11111111-1111-4111-8111-111111111111")
BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def build_event(index: int) -> StrategyDecisionRecorded:
    return StrategyDecisionRecorded(
        occurred_at=BASE + timedelta(seconds=index),
        correlation_id=CORRELATION,
        producer="agent.trading_plan_strategist",
        symbol="SYNTHUSDT",
        payload=StrategyDecisionPayload(
            action="buy", confidence="medium", rationale=f"child-process event {index}"
        ),
    )


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: journal_writer_child.py <db_path> <count>")
        return 2
    db_path, count = sys.argv[1], int(sys.argv[2])
    with SQLiteEventJournal(db_path) as journal:
        journal.append_batch([build_event(index) for index in range(count)])
        print(f"wrote {journal.count()} events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
