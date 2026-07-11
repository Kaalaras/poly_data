from __future__ import annotations

from pathlib import Path

import polars as pl

from poly_data.io.parquet_store import ParquetStore
from poly_data.quality import validate_store


def _markets() -> pl.DataFrame:
    return pl.DataFrame([{
        "id": "m1",
        "createdAt": "2024-01-01T00:00:00Z",
        "question": "Will it happen?",
        "answer1": "Yes",
        "answer2": "No",
        "neg_risk": False,
        "market_slug": "will-it-happen",
        "token1": "token-yes",
        "token2": "token-no",
        "condition_id": "cond1",
        "volume": "100",
        "ticker": "EVENT",
        "closedTime": "2024-02-01T00:00:00Z",
        "timestamp": 1_704_067_200,
        "category": "Politics",
    }])


def _trades(**overrides) -> pl.DataFrame:
    row = {
        "timestamp": 1_704_067_200,
        "market_id": "m1",
        "maker": "0xmaker",
        "taker": "0xtaker",
        "nonusdc_side": "token1",
        "maker_direction": "BUY",
        "taker_direction": "SELL",
        "price": 0.55,
        "usd_amount": 10.0,
        "token_amount": 18.0,
        "transactionHash": "0xtx",
        "orderfilled_id": "fill1",
    }
    row.update(overrides)
    return pl.DataFrame([row])


def test_validate_store_valid_source_returns_ok(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    store.append("markets", _markets())
    store.append("trades", _trades())

    report = validate_store(store, sources=["trades"])

    assert report["status"] == "ok"
    assert report["mode"] == "fast"
    assert report["sources"]["trades"]["row_count"] == 1
    assert report["sources"]["trades"]["errors"] == []


def test_validate_store_missing_source_is_warning(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")

    report = validate_store(store, sources=["trades"])

    assert report["status"] == "warning"
    assert report["sources"]["trades"]["warnings"][0]["name"] == "source_presence"


def test_validate_store_reports_missing_required_column(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    store.append("trades", _trades().drop("market_id"))

    report = validate_store(store, sources=["trades"])
    errors = report["sources"]["trades"]["errors"]

    assert report["status"] == "error"
    assert any(e.get("code") == "missing_column" and e.get("column") == "market_id" for e in errors)


def test_validate_store_reports_dtype_range_and_null_errors(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    invalid = pl.DataFrame([
        {
            "timestamp": 1_704_067_200,
            "market_id": "m1",
            "maker": None,
            "taker": "0xtaker",
            "nonusdc_side": "token1",
            "maker_direction": "BUY",
            "taker_direction": "SELL",
            "price": 1.5,
            "usd_amount": "10.0",
            "token_amount": 18.0,
            "transactionHash": "0xtx1",
            "orderfilled_id": "fill1",
        },
        {
            "timestamp": 1_704_153_600,
            "market_id": "m1",
            "maker": "0xmaker",
            "taker": "0xtaker",
            "nonusdc_side": "token1",
            "maker_direction": "BUY",
            "taker_direction": "SELL",
            "price": 0.4,
            "usd_amount": "20.0",
            "token_amount": 50.0,
            "transactionHash": "0xtx2",
            "orderfilled_id": "fill2",
        },
    ])
    store.append("trades", invalid)

    report = validate_store(store, sources=["trades"])
    errors = report["sources"]["trades"]["errors"]

    assert report["status"] == "error"
    assert any(e.get("code") == "wrong_type" and e.get("column") == "usd_amount" for e in errors)
    assert any(e.get("code") == "null" and e.get("column") == "maker" for e in errors)
    assert any(e.get("code") == "max" and e.get("column") == "price" for e in errors)


def test_validate_store_full_detects_duplicate_unique_key(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    store.append("markets", _markets())
    store.append("trades", _trades())
    store.append("trades", _trades(timestamp=1_704_153_600))

    report = validate_store(store, sources=["trades"], full=True)
    errors = report["sources"]["trades"]["errors"]

    assert report["status"] == "error"
    assert any(e["name"] == "unique_keys" and e["duplicate_count"] == 2 for e in errors)


def test_validate_store_full_detects_unknown_trade_market_id(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    store.append("trades", _trades(market_id="missing-market"))

    report = validate_store(store, sources=["trades"], full=True)
    errors = report["sources"]["trades"]["errors"]

    assert report["status"] == "error"
    assert any(e["name"] == "referential_integrity" for e in errors)
