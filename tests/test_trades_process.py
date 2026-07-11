from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from poly_data.io.parquet_store import ParquetStore
from poly_data.process.trades import (
    UnresolvedMarketMetadataError,
    V2TradeModelError,
    process_trades,
    process_trades_v2,
)

_V2_EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"


def _ts(year: int, month: int, day: int = 1) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


def _seed(store: ParquetStore) -> None:
    markets = pl.DataFrame([
        {
            "id": "M1", "createdAt": "2024-01-01", "question": "?",
            "answer1": "Y", "answer2": "N", "neg_risk": False,
            "market_slug": "s", "token1": "111", "token2": "222",
            "condition_id": "c", "volume": "0", "ticker": "T",
            "closedTime": "", "timestamp": _ts(2024, 1),
        }
    ])
    store.append("markets", markets)

    orders = pl.DataFrame([
        {
            "id": "o1", "timestamp": _ts(2024, 1, 5),
            "maker": "0xaa", "makerAssetId": "111",
            "makerAmountFilled": "10000000",
            "taker": "0xbb", "takerAssetId": "0",
            "takerAmountFilled": "5000000",
            "transactionHash": "0xt1",
        },
        {
            "id": "o2", "timestamp": _ts(2024, 2, 5),
            "maker": "0xcc", "makerAssetId": "0",
            "makerAmountFilled": "3000000",
            "taker": "0xdd", "takerAssetId": "222",
            "takerAmountFilled": "6000000",
            "transactionHash": "0xt2",
        },
    ])
    store.append("orderFilled", orders)


def test_process_trades_writes_partitioned_trades(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    _seed(store)

    n = process_trades(store)
    assert n == 2

    df = store.scan("trades").collect().sort("timestamp")
    assert df["market_id"].to_list() == ["M1", "M1"]
    assert df["taker_direction"].to_list() == ["BUY", "SELL"]
    assert df["maker_direction"].to_list() == ["SELL", "BUY"]
    assert df["price"].to_list() == [pytest.approx(0.5), pytest.approx(0.5)]
    assert df["orderfilled_id"].to_list() == ["o1", "o2"]


def test_process_trades_uses_missing_markets(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    store.append("missing_markets", pl.DataFrame([{
        "id": "M2", "createdAt": "2024-01-01", "question": "?",
        "answer1": "Y", "answer2": "N", "neg_risk": False,
        "market_slug": "s2", "token1": "333", "token2": "444",
        "condition_id": "c2", "volume": "0", "ticker": "T2",
        "closedTime": "", "timestamp": _ts(2024, 1),
        "category": "Sports",
    }]))
    store.append("orderFilled", pl.DataFrame([{
        "id": "o-missing", "timestamp": _ts(2024, 1, 5),
        "maker": "0xaa", "makerAssetId": "333",
        "makerAmountFilled": "10000000",
        "taker": "0xbb", "takerAssetId": "0",
        "takerAmountFilled": "5000000",
        "transactionHash": "0xt-missing",
    }]))

    assert process_trades(store) == 1
    df = store.scan("trades").collect()
    assert df["market_id"].to_list() == ["M2"]
    assert df["orderfilled_id"].to_list() == ["o-missing"]


def test_process_trades_drops_unmatched_markets(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    _seed(store)
    store.append("orderFilled", pl.DataFrame([{
        "id": "o-unknown", "timestamp": _ts(2024, 1, 6),
        "maker": "0xaa", "makerAssetId": "unknown-token",
        "makerAmountFilled": "10000000",
        "taker": "0xbb", "takerAssetId": "0",
        "takerAmountFilled": "5000000",
        "transactionHash": "0xt-unknown",
    }]))

    assert process_trades(store) == 2
    df = store.scan("trades").collect()
    assert "o-unknown" not in df["orderfilled_id"].to_list()
    assert df["market_id"].null_count() == 0


def test_process_trades_drops_out_of_range_prices(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    markets = pl.DataFrame([{
        "id": "M1", "createdAt": "2024-01-01", "question": "?",
        "answer1": "Y", "answer2": "N", "neg_risk": False,
        "market_slug": "s", "token1": "111", "token2": "222",
        "condition_id": "c", "volume": "0", "ticker": "T",
        "closedTime": "", "timestamp": _ts(2024, 1),
    }])
    store.append("markets", markets)
    store.append("orderFilled", pl.DataFrame([{
        "id": "o-price", "timestamp": _ts(2024, 1, 5),
        "maker": "0xaa", "makerAssetId": "111",
        "makerAmountFilled": "1000000",
        "taker": "0xbb", "takerAssetId": "0",
        "takerAmountFilled": "1200000",
        "transactionHash": "0xt-price",
    }]))

    assert process_trades(store) == 0


def test_process_trades_resumes_from_cursor(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    _seed(store)
    process_trades(store)
    n = process_trades(store)
    assert n == 0


def _seed_v2_markets(store: ParquetStore) -> None:
    store.append("markets", pl.DataFrame([{
        "id": "M1", "createdAt": "2026-04-28", "question": "?",
        "answer1": "Y", "answer2": "N", "neg_risk": False,
        "market_slug": "s", "token1": "111", "token2": "222",
        "condition_id": "c", "volume": "0", "ticker": "T",
        "closedTime": "", "timestamp": _ts(2026, 4, 28),
    }]))


def _v2_row(**overrides):
    base = {
        "id": "evt-1",
        "timestamp": _ts(2026, 4, 28),
        "block_number": 70000000,
        "block_timestamp": _ts(2026, 4, 28),
        "transaction_hash": "0xtx",
        "user_id": "0xuser",
        "asset": "111",
        "amount_usdc": 5.0,
        "amount_shares": 10.0,
        "price": 0.5,
        "side": "SELL",
        "order_hash": "0xorder",
        "counterparty_id": "0xcp",
        "order_type": "maker",
        "fee": 0.0,
        "builder": "",
    }
    base.update(overrides)
    return base


def test_process_trades_v2_derives_normalized_trades(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    _seed_v2_markets(store)
    store.append("order_filled_v2", pl.DataFrame([
        _v2_row(
            id="evt-maker-sell",
            user_id="0xmaker",
            counterparty_id="0xtaker",
            order_type="maker",
            side="SELL",
            asset="111",
        ),
        _v2_row(
            id="evt-taker-buy",
            user_id="0xtaker2",
            counterparty_id="0xmaker2",
            order_type="taker",
            side="BUY",
            asset="222",
        ),
    ]))
    assert process_trades_v2(store) == 1
    assert store.scan("order_filled_v2").collect().height == 2
    df = store.scan("trades").collect().sort("orderfilled_id")
    assert df["orderfilled_id"].to_list() == ["v2:evt-maker-sell"]
    assert df["market_id"].to_list() == ["M1"]
    assert df["nonusdc_side"].to_list() == ["token1"]
    assert df["maker"].to_list() == ["0xmaker"]
    assert df["taker"].to_list() == ["0xtaker"]
    assert df["maker_direction"].to_list() == ["SELL"]
    assert df["taker_direction"].to_list() == ["BUY"]
    assert df["transactionHash"].to_list() == ["0xtx"]
    assert df["usd_amount"].sum() == pytest.approx(5.0)


def test_process_trades_v2_derives_maker_buy(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    _seed_v2_markets(store)
    store.append("order_filled_v2", pl.DataFrame([
        _v2_row(
            id="evt-maker-buy",
            user_id="0xmaker",
            counterparty_id="0xtaker",
            side="BUY",
            asset="222",
        ),
    ]))
    assert process_trades_v2(store) == 1
    row = store.scan("trades").collect().to_dicts()[0]
    assert row["orderfilled_id"] == "v2:evt-maker-buy"
    assert row["nonusdc_side"] == "token2"
    assert row["maker_direction"] == "BUY"
    assert row["taker_direction"] == "SELL"


def test_process_trades_v2_filters_invalid_rows(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    _seed_v2_markets(store)
    store.append("order_filled_v2", pl.DataFrame([
        _v2_row(id="bad-price", price=1.2),
        _v2_row(id="bad-size", amount_usdc=0.0),
    ]))
    assert process_trades(store, source="v2") == 0
    assert store.last_cursor("trades_v2")["last_id"] == "bad-size"


def test_process_trades_v2_blocks_unresolved_market_metadata(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    _seed_v2_markets(store)
    store.append("order_filled_v2", pl.DataFrame([
        _v2_row(id="known", asset="111"),
        _v2_row(id="unknown", asset="999"),
    ]))
    with pytest.raises(UnresolvedMarketMetadataError):
        process_trades_v2(store)
    assert store.last_cursor("trades_v2") is None
    assert store.scan("trades").collect().height == 0


def test_process_trades_v2_reruns_after_missing_market_is_added(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    _seed_v2_markets(store)
    store.append("order_filled_v2", pl.DataFrame([_v2_row(id="unknown", asset="999")]))
    with pytest.raises(UnresolvedMarketMetadataError):
        process_trades_v2(store)
    store.append("missing_markets", pl.DataFrame([{
        "id": "M2", "createdAt": "2026-04-28", "question": "?",
        "answer1": "Y", "answer2": "N", "neg_risk": False,
        "market_slug": "s2", "token1": "999", "token2": "998",
        "condition_id": "c2", "volume": "0", "ticker": "T2",
        "closedTime": "", "timestamp": _ts(2026, 4, 28),
    }]))
    assert process_trades_v2(store) == 1
    df = store.scan("trades").collect()
    assert df["market_id"].to_list() == ["M2"]
    assert store.last_cursor("trades_v2")["last_id"] == "unknown"


def test_process_trades_v2_rejects_exchange_addresses(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    _seed_v2_markets(store)
    store.append("order_filled_v2", pl.DataFrame([
        _v2_row(id="exchange-taker", counterparty_id=_V2_EXCHANGE),
    ]))
    with pytest.raises(V2TradeModelError):
        process_trades_v2(store)
    assert store.last_cursor("trades_v2") is None
    assert store.scan("trades").collect().height == 0


def test_process_trades_v2_resumes_from_own_cursor(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    _seed_v2_markets(store)
    store.append("order_filled_v2", pl.DataFrame([_v2_row()]))
    assert process_trades(store, source="v2") == 1
    assert process_trades(store, source="v2") == 0
    assert store.last_cursor("trades_v2")["last_id"] == "evt-1"
