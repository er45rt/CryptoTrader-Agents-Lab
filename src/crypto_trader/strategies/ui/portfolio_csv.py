"""CSV import for My Portfolio (owner decision D40): preview -> confirm -> (optional) undo of the whole import.

Format (header row required, column names case-insensitive, order free; only the first five are mandatory):
    date,type,asset,quantity,price,fee,fee_asset,network,wallet,note
    2026-01-05,buy,BTC,0.01,65000,0.65,USDT,Bitcoin,Binance,monthly
`type` is buy | sell | transfer | fee (see portfolio.py for the accounting). Quoted fields follow RFC 4180.

Rules: a preview never writes. Confirm is all-or-nothing and re-checks everything the preview checked, against the
exact same bytes (sha256 must match). A row identical to an existing transaction or to another row of the file is a
duplicate and blocks the import unless the owner explicitly chooses to skip duplicates. The same file can not be imported
twice. Every imported row carries its batch id so the whole import can be undone in one step.
"""
from __future__ import annotations

import csv
import hashlib
import io
from typing import Any

from ..api import StrategyError
from . import portfolio

__all__ = ["preview", "confirm", "MAX_ROWS", "COLUMNS"]

MAX_ROWS = 5000
COLUMNS = ("date", "type", "asset", "quantity", "price", "fee", "fee_asset", "network", "wallet", "note")
REQUIRED = ("date", "type", "asset")


def _key(tx: dict[str, Any]) -> tuple:
    return (tx["at"], tx["type"], tx["asset"], tx.get("qty"), tx.get("price"), tx.get("fee"), tx["fee_asset"])


def _parse(text: str, ledger: portfolio.Ledger) -> dict[str, Any]:
    if text.startswith("﻿"):
        text = text[1:]                      # Excel's UTF-8 BOM
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    unknown = [h for h in header if h not in COLUMNS]
    missing = [c for c in REQUIRED if c not in header]
    if not header or unknown or missing or len(set(header)) != len(header):
        raise StrategyError(f"عنوان الملف غير صحيح. الأعمدة المسموحة: {', '.join(COLUMNS)}"
                            + (f" — غير معروفة: {', '.join(unknown)}" if unknown else "")
                            + (f" — ناقصة: {', '.join(missing)}" if missing else ""))
    existing = {_key(t) for t in ledger._load()}
    rows, seen = [], set()
    for n, raw in enumerate(reader, start=2):          # line numbers as the owner sees them in Excel
        if n - 1 > MAX_ROWS:
            raise StrategyError(f"الملف أكبر من الحد ({MAX_ROWS} صف).")
        if None in raw:
            rows.append({"line": n, "error": "أعمدة زائدة في هذا الصف.", "raw": raw.get(None)})
            continue
        row = {h.strip().lower(): (v or "").strip() for h, v in raw.items()}
        if not any(row.values()):
            continue                                    # blank line
        try:
            tx = portfolio.parse_tx({**row, "qty": row.get("quantity")})
        except StrategyError as exc:
            rows.append({"line": n, "error": str(exc), "input": row})
            continue
        k = _key(tx)
        dup = "existing" if k in existing else ("in_file" if k in seen else None)
        seen.add(k)
        rows.append({"line": n, "tx": tx, "duplicate": dup})
    return {"rows": rows, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def preview(text: str, ledger: portfolio.Ledger, *, skip_duplicates: bool = False) -> dict[str, Any]:
    parsed = _parse(text, ledger)
    rows = parsed["rows"]
    errors = [r for r in rows if "error" in r]
    dups = [r for r in rows if r.get("duplicate")]
    take = [r["tx"] for r in rows if "tx" in r and not (skip_duplicates and r.get("duplicate"))]
    batch = parsed["sha256"][:16]
    already = any(t.get("batch") == batch for t in ledger._load())
    replay_error = None
    if not errors and take:
        try:
            portfolio.compute(ledger._load() + take)
        except StrategyError as exc:
            replay_error = str(exc)
    blocking = bool(errors) or replay_error is not None or already or (dups and not skip_duplicates) or not take
    return {"sha256": parsed["sha256"], "batch": batch, "rows": rows, "n_rows": len(rows), "n_ok": len(take),
            "n_errors": len(errors), "n_duplicates": len(dups), "skip_duplicates": skip_duplicates,
            "replay_error": replay_error, "already_imported": already, "can_import": not blocking}


def confirm(text: str, sha256: str, ledger: portfolio.Ledger, *, skip_duplicates: bool = False,
            filename: str = "") -> dict[str, Any]:
    p = preview(text, ledger, skip_duplicates=skip_duplicates)
    if p["sha256"] != sha256:
        raise StrategyError("الملف تغيّر بعد المعاينة؛ أعد المعاينة.")
    if not p["can_import"]:
        raise StrategyError("لا يمكن الاستيراد: صحّح الأخطاء أو التكرار في المعاينة أولًا.")
    bodies = [{**r["tx"], "qty": r["tx"].get("qty")} for r in p["rows"]
              if "tx" in r and not (skip_duplicates and r.get("duplicate"))]
    keys = {_key(portfolio.parse_tx(b)) for b in bodies}

    def guard(current: list[dict[str, Any]]) -> None:
        # re-checked under the ledger lock: a concurrent confirm of the same file, or a matching transaction added
        # after the preview, refuses this import instead of doubling holdings
        if any(t.get("batch") == p["batch"] for t in current):
            raise StrategyError("هذا الملف مستورد من قبل.")
        if keys & {_key(t) for t in current}:
            raise StrategyError("لا يمكن الاستيراد: صحّح الأخطاء أو التكرار في المعاينة أولًا.")

    added = ledger.add_many(bodies, source=f"csv:{portfolio._text(filename)[:60]}", batch=p["batch"], guard=guard)
    return {"batch": p["batch"], "imported": len(added), "skipped_duplicates": p["n_duplicates"] if skip_duplicates else 0}
