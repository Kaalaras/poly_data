from __future__ import annotations

from pathlib import Path

import polars as pl

from poly_data.ingest.v2_status import build_v2_status, latest_public_trade_timestamp
from poly_data.io.parquet_store import ParquetStore


def test_v2_status_summarizes_raw_and_derived(tmp_path: Path, mocker) -> None:
    store = ParquetStore(tmp_path / "data")
    store.append("order_filled_v2", pl.DataFrame([
        {
            "id": "evt-1",
            "timestamp": 1777374041,
            "block_number": 70000000,
            "block_timestamp": 1777374041,
            "transaction_hash": "0xtx",
            "user_id": "0xuser",
            "asset": "111",
            "amount_usdc": 5.0,
            "amount_shares": 10.0,
            "price": 0.5,
            "side": "BUY",
            "order_hash": "0xorder",
            "counterparty_id": "0xmaker",
            "order_type": "taker",
            "fee": 0.0,
            "builder": "",
        }
    ]))
    store.append("trades", pl.DataFrame([{
        "timestamp": 1777374041,
        "market_id": "M1",
        "maker": "0xaa",
        "taker": "0xbb",
        "nonusdc_side": "token1",
        "maker_direction": "SELL",
        "taker_direction": "BUY",
        "price": 0.5,
        "usd_amount": 5.0,
        "token_amount": 10.0,
        "transactionHash": "0xtx",
        "orderfilled_id": "v2:evt-1",
    }]))
    mocker.patch("poly_data.ingest.v2_status.latest_public_trade_timestamp", return_value=1777375000)
    assert build_v2_status(store) == {
        "raw_v2_rows": 1,
        "raw_v2_unique_ids": 1,
        "raw_v2_duplicate_ids": 0,
        "raw_v2_max_timestamp": 1777374041,
        "trades_v2_rows": 1,
        "trades_v2_max_timestamp": 1777374041,
        "latest_public_data_api_timestamp": 1777375000,
    }


def test_latest_public_trade_timestamp_uses_data_api(mocker) -> None:
    response = mocker.Mock()
    response.json.return_value = [{"timestamp": "1777375000"}]
    response.raise_for_status.return_value = None
    get = mocker.patch("poly_data.ingest.v2_status.requests.get", return_value=response)
    assert latest_public_trade_timestamp() == 1777375000
    _, kwargs = get.call_args
    assert kwargs["params"] == {"limit": 1, "takerOnly": "false"}
