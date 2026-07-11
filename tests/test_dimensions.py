from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from poly_data.contracts import DataContractError
from poly_data.dimensions import refresh_market_dimensions
from poly_data.io.parquet_store import ParquetStore


def _timestamp(year: int, month: int, day: int = 1) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


def _market(*, observed_at: int | None = None) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "M1",
        "createdAt": "2026-04-01T00:00:00Z",
        "question": "Will this pass?",
        "answer1": "Yes",
        "answer2": "No",
        "neg_risk": False,
        "market_slug": "will-this-pass",
        "token1": "111",
        "token2": "222",
        "condition_id": "condition-1",
        "volume": "0",
        "ticker": "PASS",
        "closedTime": "",
        "timestamp": _timestamp(2026, 4),
        "category": "Politics",
    }
    if observed_at is not None:
        row["observed_at"] = observed_at
    return row


def test_refresh_market_dimensions_writes_latest_market_and_tokens(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    store.append("markets", pl.DataFrame([_market()]))
    observed_at = _timestamp(2026, 5) * 1_000_000_000
    store.append("market_refreshes", pl.DataFrame([_market(observed_at=observed_at)]))

    assert refresh_market_dimensions(store) == {
        "market_assets": 2,
        "markets_current": 1,
    }

    assets = (
        store.scan("market_assets")
        .select(["asset", "market_id", "token_side", "timestamp"])
        .collect()
        .sort("asset")
    )
    assert assets.to_dicts() == [
        {
            "asset": "111",
            "market_id": "M1",
            "token_side": "token1",
            "timestamp": _timestamp(2026, 5),
        },
        {
            "asset": "222",
            "market_id": "M1",
            "token_side": "token2",
            "timestamp": _timestamp(2026, 5),
        },
    ]
    current = store.scan("markets_current").collect()
    assert current.height == 1
    assert current["timestamp"].item() == _timestamp(2026, 5)


def test_refresh_market_dimensions_replaces_previous_snapshot(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    store.append("markets", pl.DataFrame([_market()]))

    refresh_market_dimensions(store)
    refresh_market_dimensions(store)

    assert store.scan("market_assets").collect().height == 2
    assert store.scan("markets_current").collect().height == 1


def test_refresh_market_dimensions_keeps_legacy_snapshots_without_category(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path / "data")
    legacy = _market()
    del legacy["category"]
    store.append("markets", pl.DataFrame([legacy]))

    assert refresh_market_dimensions(store)["markets_current"] == 1
    assert store.scan("markets_current").select("category").collect().item() == ""


def test_replace_source_keeps_previous_snapshot_when_contract_validation_fails(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path / "data")
    valid = pl.DataFrame([{
        "asset": "111",
        "market_id": "M1",
        "token_side": "token1",
        "timestamp": _timestamp(2026, 4),
    }])
    store.replace_source("market_assets", valid.lazy())
    invalid = pl.concat([valid, valid]).lazy()

    with pytest.raises(DataContractError):
        store.replace_source("market_assets", invalid)

    assert store.scan("market_assets").select("asset").collect().item() == "111"
