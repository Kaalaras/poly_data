from __future__ import annotations

from pathlib import Path

import polars as pl

from poly_data.io.parquet_store import ParquetStore
from poly_data.compact.monthly import compact_all


def test_compact_all_iterates_every_month(tmp_path: Path,
                                          sample_orderfilled_df: pl.DataFrame) -> None:
    store = ParquetStore(tmp_path / "data")
    store.append("orderFilled", sample_orderfilled_df)
    store.append("orderFilled", sample_orderfilled_df)

    rewritten = compact_all(store, "orderFilled")
    assert rewritten == {"2024-1": 2, "2024-2": 1}

    files = list((tmp_path / "data" / "orderFilled").rglob("*.parquet"))
    assert all(f.name == "month.parquet" for f in files)


def test_compact_all_unknown_source_returns_empty(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    assert compact_all(store, "orderFilled") == {}


def test_compact_all_uses_orderfilled_id_for_trades(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    row = {
        "timestamp": 1700000000,
        "market_id": "M1",
        "maker": "0xaa",
        "taker": "0xbb",
        "nonusdc_side": "token1",
        "maker_direction": "SELL",
        "taker_direction": "BUY",
        "price": 0.5,
        "usd_amount": 5.0,
        "token_amount": 10.0,
        "transactionHash": "0xt1",
        "orderfilled_id": "of-1",
    }
    store.append("trades", pl.DataFrame([row]))
    store.append("trades", pl.DataFrame([row]))

    rewritten = compact_all(store, "trades")

    assert rewritten == {"2023-11": 1}
    df = store.scan("trades").collect()
    assert df.height == 1
    assert df["orderfilled_id"].to_list() == ["of-1"]
