from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl

from poly_data.contracts import assert_valid_frame
from poly_data.io.parquet_store import ParquetStore

_REQUIRED_FIELDS = (
    "id",
    "block_number",
    "block_timestamp",
    "transaction_hash",
    "user_id",
    "asset",
    "amount_usdc",
    "amount_shares",
    "price",
    "side",
    "order_hash",
    "counterparty_id",
    "order_type",
    "fee",
    "builder",
)


class OrderFilledV2PayloadError(ValueError):
    """Raised when a Ponder V2 fill payload cannot be normalized."""


def normalize_order_filled_payload(payload: Any) -> list[dict[str, Any]]:
    events = _extract_events(payload)
    rows = [_normalize_event(e) for e in events]
    return _dedupe_rows(rows)


def import_ponder_v2_jsonl(
    path: Path,
    *,
    store: ParquetStore,
    batch_size: int = 100_000,
) -> int:
    rows: dict[str, dict[str, Any]] = {}
    total = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            for row in normalize_order_filled_payload(json.loads(line)):
                rows[row["id"]] = row
            if len(rows) >= batch_size:
                total += _flush(store, rows)
                rows.clear()
    if rows:
        total += _flush(store, rows)
    return total


def _flush(store: ParquetStore, rows: dict[str, dict[str, Any]]) -> int:
    df = pl.DataFrame(list(rows.values()))
    assert_valid_frame(df, "order_filled_v2")
    store.append("order_filled_v2", df)
    return df.height


def _extract_events(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        events = payload
    elif isinstance(payload, dict):
        for key in ("data", "events", "rows", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                events = value
                break
        else:
            events = [payload]
    else:
        raise OrderFilledV2PayloadError("payload must be an object or array")
    if not all(isinstance(e, dict) for e in events):
        raise OrderFilledV2PayloadError("payload events must be objects")
    return events


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    missing = [f for f in _REQUIRED_FIELDS if f not in event]
    if missing:
        raise OrderFilledV2PayloadError(f"missing fields: {', '.join(missing)}")
    block_ts = _int_field(event, "block_timestamp")
    return {
        "id": str(event["id"]),
        "timestamp": block_ts,
        "block_number": _int_field(event, "block_number"),
        "block_timestamp": block_ts,
        "transaction_hash": str(event["transaction_hash"]),
        "user_id": str(event["user_id"]),
        "asset": str(event["asset"]),
        "amount_usdc": _float_field(event, "amount_usdc"),
        "amount_shares": _float_field(event, "amount_shares"),
        "price": _float_field(event, "price"),
        "side": _upper_field(event, "side"),
        "order_hash": str(event["order_hash"]),
        "counterparty_id": str(event["counterparty_id"]),
        "order_type": _lower_field(event, "order_type"),
        "fee": _float_field(event, "fee"),
        "builder": str(event["builder"]),
        **_provenance_fields(event),
    }


def _provenance_fields(event: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if "exchange" in event:
        fields["exchange"] = str(event["exchange"])
    if "log_index" in event:
        fields["log_index"] = _int_field(event, "log_index")
    if "metadata" in event:
        fields["metadata"] = str(event["metadata"])
    return fields


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        deduped[row["id"]] = row
    return list(deduped.values())


def _int_field(event: dict[str, Any], key: str) -> int:
    try:
        return int(event[key])
    except (TypeError, ValueError) as e:
        raise OrderFilledV2PayloadError(f"{key} must be an integer") from e


def _float_field(event: dict[str, Any], key: str) -> float:
    try:
        return float(event[key])
    except (TypeError, ValueError) as e:
        raise OrderFilledV2PayloadError(f"{key} must be a number") from e


def _upper_field(event: dict[str, Any], key: str) -> str:
    return str(event[key]).upper()


def _lower_field(event: dict[str, Any], key: str) -> str:
    return str(event[key]).lower()
