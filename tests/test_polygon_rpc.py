from __future__ import annotations

import json
from pathlib import Path

import pytest

from poly_data.ingest.ponder import import_ponder_v2_jsonl
from poly_data.ingest.polygon_rpc import (
    ORDER_FILLED_TOPIC,
    ORDERS_MATCHED_TOPIC,
    POLYGON_V2_EXCHANGES,
    PolygonRpcError,
    download_v2_logs,
    infer_start_block,
)
from poly_data.io.parquet_store import ParquetStore

TX_HASH = "0x" + "aa" * 32
ORDER_HASH = "0x" + "11" * 32
MAKER = "0x" + "12" * 20
TAKER = "0x" + "34" * 20


class FakeRpc:
    def __init__(self, *, include_match: bool = True, fail_orders_matched: bool = False) -> None:
        self.include_match = include_match
        self.fail_orders_matched = fail_orders_matched

    def call(self, method: str, params: list) -> object:
        if method == "eth_blockNumber":
            return hex(100)
        if method != "eth_getLogs":
            raise AssertionError(method)
        topic = params[0]["topics"][0]
        if topic == ORDER_FILLED_TOPIC:
            return [_order_filled_log()]
        if topic == ORDERS_MATCHED_TOPIC:
            if self.fail_orders_matched:
                raise PolygonRpcError("transient failure")
            return [_orders_matched_log()] if self.include_match else []
        raise AssertionError(topic)

    def batch(self, calls: list[tuple[str, list]]) -> list[dict[str, str]]:
        assert calls == [("eth_getBlockByNumber", ["0x1", False])]
        return [{"timestamp": hex(1_777_374_288)}]


def test_download_decodes_order_filled_and_marks_taker(tmp_path: Path) -> None:
    output = tmp_path / "fills.jsonl"
    cursor = tmp_path / "cursor.json"
    summary = download_v2_logs(
        data_root=tmp_path / "data",
        from_block=1,
        to_block=1,
        output_path=output,
        cursor_path=cursor,
        chunk_size=1,
        min_chunk_size=1,
        client=FakeRpc(include_match=True),
    )

    rows = _read_jsonl(output)
    assert summary.rows == 1
    assert rows[0]["id"] == f"{TX_HASH}:1"
    assert rows[0]["log_index"] == 1
    assert rows[0]["exchange"] == POLYGON_V2_EXCHANGES[0].lower()
    assert rows[0]["metadata"] == "0x" + "00" * 32
    assert rows[0]["block_timestamp"] == 1_777_374_288
    assert rows[0]["user_id"] == MAKER
    assert rows[0]["counterparty_id"] == TAKER
    assert rows[0]["asset"] == "123"
    assert rows[0]["amount_usdc"] == 0.5
    assert rows[0]["amount_shares"] == 1.0
    assert rows[0]["price"] == 0.5
    assert rows[0]["side"] == "BUY"
    assert rows[0]["order_type"] == "taker"
    assert json.loads(cursor.read_text(encoding="utf-8"))["next_block"] == 2


def test_download_preserves_maker_when_no_orders_matched_log(tmp_path: Path) -> None:
    output = tmp_path / "fills.jsonl"
    download_v2_logs(
        data_root=tmp_path / "data",
        from_block=1,
        to_block=1,
        output_path=output,
        cursor_path=tmp_path / "cursor.json",
        chunk_size=1,
        min_chunk_size=1,
        client=FakeRpc(include_match=False),
    )

    assert _read_jsonl(output)[0]["order_type"] == "maker"


def test_download_does_not_advance_cursor_or_write_rows_on_partial_failure(tmp_path: Path) -> None:
    output = tmp_path / "fills.jsonl"
    cursor = tmp_path / "cursor.json"

    with pytest.raises(PolygonRpcError):
        download_v2_logs(
            data_root=tmp_path / "data",
            from_block=1,
            to_block=1,
            output_path=output,
            cursor_path=cursor,
            chunk_size=1,
            min_chunk_size=1,
            max_retries=1,
            client=FakeRpc(fail_orders_matched=True),
        )

    assert not cursor.exists()
    assert not output.exists()


def test_duplicate_download_rows_are_idempotent_on_import(tmp_path: Path) -> None:
    output = tmp_path / "fills.jsonl"
    for _ in range(2):
        download_v2_logs(
            data_root=tmp_path / "data",
            from_block=1,
            to_block=1,
            output_path=output,
            cursor_path=tmp_path / "cursor.json",
            chunk_size=1,
            min_chunk_size=1,
            client=FakeRpc(include_match=True),
        )

    store = ParquetStore(tmp_path / "store")
    assert import_ponder_v2_jsonl(output, store=store) == 1
    assert store.scan("order_filled_v2").collect().height == 1


def test_download_stops_before_unconfirmed_head_blocks(tmp_path: Path) -> None:
    summary = download_v2_logs(
        data_root=tmp_path / "data",
        from_block=1,
        confirmations=10,
        output_path=tmp_path / "fills.jsonl",
        cursor_path=tmp_path / "cursor.json",
        chunk_size=100,
        min_chunk_size=1,
        client=FakeRpc(),
    )

    assert summary.to_block == 90


def test_infer_start_block_rewinds_saved_cursor_for_overlap(tmp_path: Path) -> None:
    assert infer_start_block(
        tmp_path / "data",
        {"next_block": 100_000_000},
        overlap_blocks=128,
    ) == 99_999_872


def _order_filled_log() -> dict:
    return {
        "address": POLYGON_V2_EXCHANGES[0],
        "blockNumber": "0x1",
        "transactionHash": TX_HASH,
        "transactionIndex": "0x0",
        "logIndex": "0x1",
        "topics": [
            ORDER_FILLED_TOPIC,
            ORDER_HASH,
            _address_topic(MAKER),
            _address_topic(TAKER),
        ],
        "data": "0x" + "".join([
            _word(0),
            _word(123),
            _word(500_000),
            _word(1_000_000),
            _word(0),
            "bb" * 32,
            "00" * 32,
        ]),
    }


def _orders_matched_log() -> dict:
    return {
        "address": POLYGON_V2_EXCHANGES[0],
        "blockNumber": "0x1",
        "transactionHash": TX_HASH,
        "transactionIndex": "0x0",
        "logIndex": "0x2",
        "topics": [
            ORDERS_MATCHED_TOPIC,
            ORDER_HASH,
            _address_topic(TAKER),
        ],
        "data": "0x" + "".join([
            _word(0),
            _word(123),
            _word(500_000),
            _word(1_000_000),
        ]),
    }


def _word(value: int) -> str:
    return f"{value:064x}"


def _address_topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
