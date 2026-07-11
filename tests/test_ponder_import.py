from __future__ import annotations

import json
from pathlib import Path

from poly_data.io.parquet_store import ParquetStore
from poly_data.ingest.ponder import import_ponder_v2_jsonl


def _row(**overrides):
    row = {
        "id": "0xtx:0xorder",
        "block_number": 86127122,
        "block_timestamp": 1777374288,
        "transaction_hash": "0xtx",
        "user_id": "0xuser",
        "asset": "111",
        "amount_usdc": 1.0,
        "amount_shares": 2.0,
        "price": 0.5,
        "side": "BUY",
        "order_hash": "0xorder",
        "counterparty_id": "0xmaker",
        "order_type": "maker",
        "fee": 0.0,
        "builder": "0x0",
    }
    row.update(overrides)
    return row


def test_import_ponder_jsonl_dedupes_corrected_order_type(tmp_path: Path) -> None:
    path = tmp_path / "order_filled_v2.jsonl"
    rows = [
        _row(order_type="maker"),
        _row(order_type="taker"),
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    store = ParquetStore(tmp_path / "data")
    assert import_ponder_v2_jsonl(path, store=store) == 1

    imported = store.scan("order_filled_v2").collect()
    assert imported.height == 1
    assert imported["order_type"].to_list() == ["taker"]
    assert imported["timestamp"].to_list() == [1777374288]
