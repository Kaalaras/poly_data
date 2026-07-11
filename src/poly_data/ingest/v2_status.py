from __future__ import annotations

import logging
from typing import Any

import polars as pl
import requests

from poly_data.io.parquet_store import ParquetStore
from poly_data.tls import configure_system_truststore

logger = logging.getLogger(__name__)

POLYMARKET_TRADES_URL = "https://data-api.polymarket.com/trades"


def build_v2_status(store: ParquetStore) -> dict[str, Any]:
    raw = _raw_v2_summary(store)
    trades = _v2_trades_summary(store)
    return {
        **raw,
        **trades,
        "latest_public_data_api_timestamp": latest_public_trade_timestamp(),
    }


def latest_public_trade_timestamp() -> int | None:
    try:
        configure_system_truststore()
        response = requests.get(
            POLYMARKET_TRADES_URL,
            params={"limit": 1, "takerOnly": "false"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as e:
        logger.warning("could not fetch latest Polymarket public trade timestamp: %s", e)
        return None
    if not isinstance(payload, list) or not payload:
        return None
    try:
        return int(payload[0]["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None


def _raw_v2_summary(store: ParquetStore) -> dict[str, int | None]:
    lf = store.scan("order_filled_v2")
    cols = lf.collect_schema().names()
    if not cols:
        return {
            "raw_v2_rows": 0,
            "raw_v2_unique_ids": 0,
            "raw_v2_duplicate_ids": 0,
            "raw_v2_max_timestamp": None,
        }
    ts_col = "block_timestamp" if "block_timestamp" in cols else "timestamp"
    exprs = [
        pl.len().alias("rows"),
        pl.col(ts_col).max().alias("max_timestamp"),
    ]
    if "id" in cols:
        exprs.append(pl.col("id").n_unique().alias("unique_ids"))
    out = lf.select(exprs).collect().to_dicts()[0]
    rows = int(out["rows"])
    unique_ids = int(out.get("unique_ids", 0) or 0)
    return {
        "raw_v2_rows": rows,
        "raw_v2_unique_ids": unique_ids,
        "raw_v2_duplicate_ids": max(0, rows - unique_ids),
        "raw_v2_max_timestamp": _maybe_int(out.get("max_timestamp")),
    }


def _v2_trades_summary(store: ParquetStore) -> dict[str, int | None]:
    lf = store.scan("trades")
    cols = lf.collect_schema().names()
    if not cols or "orderfilled_id" not in cols:
        return {"trades_v2_rows": 0, "trades_v2_max_timestamp": None}
    v2 = lf.filter(pl.col("orderfilled_id").cast(pl.String).str.starts_with("v2:"))
    out = v2.select([
        pl.len().alias("rows"),
        pl.col("timestamp").max().alias("max_timestamp"),
    ]).collect().to_dicts()[0]
    return {
        "trades_v2_rows": int(out["rows"]),
        "trades_v2_max_timestamp": _maybe_int(out.get("max_timestamp")),
    }


def _maybe_int(value: Any) -> int | None:
    return int(value) if value is not None else None
