"""Materialize official closed binary market outcomes from Gamma metadata."""
from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import polars as pl

from poly_data.ingest.markets import _to_unix_seconds
from poly_data.io.parquet_store import ParquetStore


def parse_official_outcome(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return one immutable binary outcome row, or None for non-final metadata."""
    if row.get("closed") is not True:
        return None
    prices = _outcome_prices(row.get("outcomePrices"))
    if prices is None or len(prices) != 2:
        return None
    if prices.count(Decimal("1")) != 1 or prices.count(Decimal("0")) != 1:
        return None
    resolved_at = _to_unix_seconds(row.get("closedTime"))
    if resolved_at <= 0:
        return None
    winner_index = prices.index(Decimal("1"))
    return {
        "market_id": str(row["id"]),
        "winner_token": ("token1", "token2")[winner_index],
        "resolved_at": resolved_at,
        "observed_at": int(row.get("timestamp", resolved_at)),
        "resolution_source": str(row.get("resolutionSource", "") or ""),
        "resolution_status": str(row.get("umaResolutionStatus", "") or ""),
        "timestamp": resolved_at,
    }


def refresh_market_outcomes(store: ParquetStore) -> dict[str, int]:
    """Append newly observed, unambiguous official outcomes exactly once."""
    current = store.scan("markets_current")
    if "id" not in current.collect_schema().names():
        return {"added": 0, "skipped": 0}

    existing = store.scan("market_outcomes")
    existing_columns = existing.collect_schema().names()
    known = (
        set(existing.select("market_id").collect().get_column("market_id"))
        if "market_id" in existing_columns
        else set()
    )
    rows: list[dict[str, Any]] = []
    skipped = 0
    for row in current.collect().iter_rows(named=True):
        if row["id"] in known:
            continue
        outcome = parse_official_outcome(row)
        if outcome is None:
            skipped += 1
        else:
            rows.append(outcome)
    if rows:
        store.append("market_outcomes", pl.DataFrame(rows))
    return {"added": len(rows), "skipped": skipped}


def _outcome_prices(value: Any) -> list[Decimal] | None:
    try:
        raw = json.loads(value) if isinstance(value, str) else value
        if not isinstance(raw, list):
            return None
        return [Decimal(str(price)) for price in raw]
    except (InvalidOperation, TypeError, ValueError, json.JSONDecodeError):
        return None
