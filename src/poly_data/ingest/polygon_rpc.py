from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from poly_data.io import cursor as cursor_io
from poly_data.tls import configure_system_truststore

logger = logging.getLogger(__name__)

POLYGON_V2_EXCHANGES = (
    "0xE111180000d2663C0091e4f400237545B87B996B",
    "0xe2222d279d744050d28e00520010520000310F59",
)
ORDER_FILLED_TOPIC = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
ORDERS_MATCHED_TOPIC = "0x174b3811690657c217184f89418266767c87e4805d09680c39fc9c031c0cab7c"
DEFAULT_POLYGON_RPC_URL = "https://polygon.drpc.org"
DEFAULT_START_BLOCK = 86_126_998
DEFAULT_CONFIRMATIONS = 128
DEFAULT_OVERLAP_BLOCKS = 128
USDC_DECIMALS = 1_000_000


class PolygonRpcError(RuntimeError):
    """Raised when a Polygon JSON-RPC call fails or returns malformed data."""


@dataclass(frozen=True)
class DownloadSummary:
    from_block: int
    to_block: int
    output_path: Path
    ranges: int
    rows: int
    order_filled_logs: int
    orders_matched_logs: int
    retries: int


class JsonRpcClient:
    def __init__(
        self,
        url: str,
        *,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self._session = session or requests.Session()
        self._next_id = 1

    def call(self, method: str, params: list[Any]) -> Any:
        payload = self._payload(method, params)
        response = self._post(payload)
        if not isinstance(response, dict):
            raise PolygonRpcError(f"{method}: expected JSON-RPC object response")
        return self._result(response, method)

    def batch(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        if not calls:
            return []
        payload = [self._payload(method, params) for method, params in calls]
        response = self._post(payload)
        if not isinstance(response, list):
            raise PolygonRpcError("batch: expected JSON-RPC array response")
        by_id = {item.get("id"): item for item in response if isinstance(item, dict)}
        results: list[Any] = []
        for request in payload:
            item = by_id.get(request["id"])
            if item is None:
                raise PolygonRpcError(f"batch: missing response for id {request['id']}")
            results.append(self._result(item, str(request["method"])))
        return results

    def _payload(self, method: str, params: list[Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}

    def _post(self, payload: dict[str, Any] | list[dict[str, Any]]) -> Any:
        try:
            response = self._session.post(
                self.url,
                json=payload,
                headers={"content-type": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            raise PolygonRpcError(f"rpc request failed: {e}") from e
        except ValueError as e:
            raise PolygonRpcError("rpc response was not valid JSON") from e

    @staticmethod
    def _result(item: dict[str, Any], method: str) -> Any:
        if item.get("error") is not None:
            raise PolygonRpcError(f"{method}: {item['error']}")
        if "result" not in item:
            raise PolygonRpcError(f"{method}: missing result")
        return item["result"]


def default_rpc_url() -> str:
    return (
        os.environ.get("POLYGON_RPC_URL")
        or os.environ.get("PONDER_RPC_URL_137")
        or DEFAULT_POLYGON_RPC_URL
    )


def default_output_path(data_root: Path, from_block: int, to_block: int) -> Path:
    return data_root / "_polygon_rpc" / f"order_filled_v2_{from_block}_{to_block}.jsonl"


def default_cursor_path(data_root: Path) -> Path:
    return data_root / "_polygon_rpc" / "cursor.json"


def infer_start_block(
    data_root: Path,
    state: dict[str, Any] | None = None,
    *,
    overlap_blocks: int = DEFAULT_OVERLAP_BLOCKS,
) -> int:
    if overlap_blocks < 0:
        raise ValueError("overlap_blocks must be non-negative")
    cursor = state if state is not None else cursor_io.load(default_cursor_path(data_root))
    next_block = cursor.get("next_block") if cursor else None
    if isinstance(next_block, int) and next_block > 0:
        return max(DEFAULT_START_BLOCK, next_block - overlap_blocks)
    raw_next = _infer_raw_v2_next_block(data_root)
    if raw_next is not None:
        return max(DEFAULT_START_BLOCK, raw_next - overlap_blocks)
    return DEFAULT_START_BLOCK


def latest_block(client: JsonRpcClient) -> int:
    return _hex_to_int(client.call("eth_blockNumber", []))


def download_v2_logs(
    *,
    data_root: Path,
    rpc_url: str | None = None,
    from_block: int | None = None,
    to_block: int | None = None,
    output_path: Path | None = None,
    cursor_path: Path | None = None,
    chunk_size: int = 1_000,
    min_chunk_size: int = 25,
    max_retries: int = 5,
    timeout: float = 30.0,
    sleep_seconds: float = 0.0,
    confirmations: int = DEFAULT_CONFIRMATIONS,
    overlap_blocks: int = DEFAULT_OVERLAP_BLOCKS,
    limit_ranges: int | None = None,
    client: JsonRpcClient | None = None,
) -> DownloadSummary:
    configure_system_truststore()
    url = rpc_url or default_rpc_url()
    rpc = client or JsonRpcClient(url, timeout=timeout)
    cur = cursor_path or default_cursor_path(data_root)
    state = cursor_io.load(cur) or {}
    if confirmations < 0:
        raise ValueError("confirmations must be non-negative")
    start = (
        from_block
        if from_block is not None
        else infer_start_block(data_root, state, overlap_blocks=overlap_blocks)
    )
    end = to_block if to_block is not None else latest_block(rpc) - confirmations
    if start > end:
        out = output_path or _output_from_state(state) or default_output_path(data_root, start, end)
        return DownloadSummary(start, end, out, 0, 0, 0, 0, 0)
    out = output_path or _output_from_state(state) or default_output_path(data_root, start, end)
    out.parent.mkdir(parents=True, exist_ok=True)
    cur.parent.mkdir(parents=True, exist_ok=True)

    completed = state.get("completed_ranges")
    if not isinstance(completed, list):
        completed = []

    next_block = start
    current_chunk = max(min_chunk_size, chunk_size)
    ranges = rows = order_logs = matched_logs = retries = 0

    while next_block <= end:
        if limit_ranges is not None and ranges >= limit_ranges:
            break
        range_from = next_block
        range_to = min(end, range_from + current_chunk - 1)
        attempt = 0
        while True:
            try:
                result = _download_range(rpc, range_from, range_to)
                written = _append_jsonl(out, result["rows"])
                checksum = _checksum_rows(result["rows"])
                completed.append(
                    {
                        "from_block": range_from,
                        "to_block": range_to,
                        "rows": written,
                        "order_filled_logs": len(result["order_filled_logs"]),
                        "orders_matched_logs": len(result["orders_matched_logs"]),
                        "sha256": checksum,
                    }
                )
                cursor_io.save(
                    cur,
                    {
                        "from_block": start,
                        "to_block": end,
                        "next_block": range_to + 1,
                        "endpoint": redact_rpc_url(url),
                        "chunk_size": current_chunk,
                        "min_chunk_size": min_chunk_size,
                        "retries": retries,
                        "completed_ranges": completed,
                        "updated_at": int(time.time()),
                        "output_path": str(out),
                    },
                )
                ranges += 1
                rows += written
                order_logs += len(result["order_filled_logs"])
                matched_logs += len(result["orders_matched_logs"])
                next_block = range_to + 1
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
                if current_chunk < chunk_size:
                    current_chunk = min(chunk_size, current_chunk * 2)
                break
            except PolygonRpcError:
                retries += 1
                attempt += 1
                if current_chunk > min_chunk_size:
                    current_chunk = max(min_chunk_size, current_chunk // 2)
                    range_to = min(end, range_from + current_chunk - 1)
                    logger.warning(
                        "rpc range failed; shrinking chunk to %d for %d..%d",
                        current_chunk,
                        range_from,
                        range_to,
                    )
                    continue
                if attempt >= max_retries:
                    raise
                delay = min(2.0**attempt, 30.0)
                logger.warning("rpc range failed; retrying %d..%d in %.1fs", range_from, range_to, delay)
                time.sleep(delay)

    return DownloadSummary(start, end, out, ranges, rows, order_logs, matched_logs, retries)


def benchmark_polygon_rpc(
    *,
    rpc_urls: list[str],
    from_block: int = 86_127_999,
    spans: tuple[int, ...] = (50, 250, 500, 1_000, 1_500),
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    configure_system_truststore()
    results: list[dict[str, Any]] = []
    for url in rpc_urls:
        client = JsonRpcClient(url, timeout=timeout)
        endpoint_result: dict[str, Any] = {"endpoint": redact_rpc_url(url), "checks": []}
        try:
            started = time.perf_counter()
            head = latest_block(client)
            endpoint_result["block_number"] = head
            endpoint_result["block_ms"] = round((time.perf_counter() - started) * 1000)
        except PolygonRpcError as e:
            endpoint_result["block_error"] = str(e)
            results.append(endpoint_result)
            continue
        best_span = 0
        for span in spans:
            to_block = from_block + span - 1
            started = time.perf_counter()
            try:
                logs = _fetch_logs(client, ORDER_FILLED_TOPIC, from_block, to_block)
                ms = round((time.perf_counter() - started) * 1000)
                endpoint_result["checks"].append(
                    {"span": span, "ok": True, "ms": ms, "logs": len(logs)}
                )
                best_span = span
            except PolygonRpcError as e:
                ms = round((time.perf_counter() - started) * 1000)
                endpoint_result["checks"].append(
                    {"span": span, "ok": False, "ms": ms, "error": str(e)}
                )
        endpoint_result["recommended_chunk_size"] = best_span or None
        results.append(endpoint_result)
    return results


def _download_range(client: JsonRpcClient, from_block: int, to_block: int) -> dict[str, Any]:
    order_filled_logs = _fetch_logs(client, ORDER_FILLED_TOPIC, from_block, to_block)
    orders_matched_logs = _fetch_logs(client, ORDERS_MATCHED_TOPIC, from_block, to_block)
    timestamps = _fetch_block_timestamps(
        client,
        sorted({_hex_to_int(log["blockNumber"]) for log in order_filled_logs}),
    )
    taker_order_keys = {
        f"{str(log['transactionHash']).lower()}:{str(log['topics'][1]).lower()}"
        for log in orders_matched_logs
    }
    rows = [
        _decode_order_filled(log, timestamps[_hex_to_int(log["blockNumber"])], taker_order_keys)
        for log in sorted(
            order_filled_logs,
            key=lambda item: (
                _hex_to_int(item["blockNumber"]),
                _hex_to_int(item.get("transactionIndex", "0x0")),
                _hex_to_int(item.get("logIndex", "0x0")),
            ),
        )
    ]
    return {
        "rows": rows,
        "order_filled_logs": order_filled_logs,
        "orders_matched_logs": orders_matched_logs,
    }


def _infer_raw_v2_next_block(data_root: Path) -> int | None:
    try:
        from poly_data.io.parquet_store import ParquetStore

        lf = ParquetStore(data_root).scan("order_filled_v2")
        if "block_number" not in lf.collect_schema().names():
            return None
        value = lf.select("block_number").max().collect().item()
    except Exception:
        return None
    return int(value) + 1 if value is not None else None


def _output_from_state(state: dict[str, Any]) -> Path | None:
    value = state.get("output_path")
    return Path(value) if isinstance(value, str) and value else None


def _fetch_logs(client: JsonRpcClient, topic: str, from_block: int, to_block: int) -> list[dict[str, Any]]:
    result = client.call(
        "eth_getLogs",
        [
            {
                "fromBlock": _int_to_hex(from_block),
                "toBlock": _int_to_hex(to_block),
                "address": list(POLYGON_V2_EXCHANGES),
                "topics": [topic],
            }
        ],
    )
    if not isinstance(result, list):
        raise PolygonRpcError("eth_getLogs: result must be an array")
    if not all(isinstance(item, dict) for item in result):
        raise PolygonRpcError("eth_getLogs: log entries must be objects")
    return result


def _fetch_block_timestamps(client: JsonRpcClient, blocks: list[int]) -> dict[int, int]:
    if not blocks:
        return {}
    timestamps: dict[int, int] = {}
    for block_batch in _chunks(blocks, 100):
        calls = [("eth_getBlockByNumber", [_int_to_hex(block), False]) for block in block_batch]
        try:
            results = client.batch(calls)
        except PolygonRpcError:
            results = [
                client.call("eth_getBlockByNumber", [_int_to_hex(block), False])
                for block in block_batch
            ]
        for block, result in zip(block_batch, results):
            if not isinstance(result, dict) or "timestamp" not in result:
                raise PolygonRpcError(f"eth_getBlockByNumber: missing timestamp for {block}")
            timestamps[block] = _hex_to_int(result["timestamp"])
    return timestamps


def _decode_order_filled(
    log: dict[str, Any],
    block_timestamp: int,
    taker_order_keys: set[str],
) -> dict[str, Any]:
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) < 4:
        raise PolygonRpcError("OrderFilled log is missing indexed topics")
    words = _decode_words(str(log.get("data", "")), 7)
    side = _word_to_int(words[0])
    token_id = _word_to_int(words[1])
    maker_amount = _decimal(_word_to_int(words[2]))
    taker_amount = _decimal(_word_to_int(words[3]))
    fee = _decimal(_word_to_int(words[4]))
    builder = _word_to_hex(words[5])
    order_side = "BUY" if side == 0 else "SELL"
    amount_usdc = maker_amount if order_side == "BUY" else taker_amount
    amount_shares = taker_amount if order_side == "BUY" else maker_amount
    price = amount_usdc / amount_shares if amount_shares > 0 else 0.0
    transaction_hash = str(log["transactionHash"]).lower()
    order_hash = str(topics[1]).lower()
    log_index = _hex_to_int(log.get("logIndex", "0x0"))
    row_id = f"{transaction_hash}:{log_index}"
    return {
        "id": row_id,
        "exchange": str(log["address"]).lower(),
        "log_index": log_index,
        "block_number": _hex_to_int(log["blockNumber"]),
        "block_timestamp": block_timestamp,
        "transaction_hash": transaction_hash,
        "user_id": _topic_to_address(str(topics[2])),
        "asset": str(token_id),
        "amount_usdc": amount_usdc,
        "amount_shares": amount_shares,
        "price": price,
        "side": order_side,
        "order_hash": order_hash,
        "counterparty_id": _topic_to_address(str(topics[3])),
        "order_type": (
            "taker"
            if f"{transaction_hash}:{order_hash}" in taker_order_keys
            else "maker"
        ),
        "fee": fee,
        "builder": builder,
        "metadata": _word_to_hex(words[6]),
    }


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            f.write(b"\n")
        f.flush()
        os.fsync(f.fileno())
    return len(rows)


def _checksum_rows(rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _decode_words(data: str, count: int) -> list[str]:
    value = data[2:] if data.startswith("0x") else data
    if len(value) < count * 64:
        raise PolygonRpcError("event data is shorter than expected")
    return [value[i : i + 64] for i in range(0, count * 64, 64)]


def _word_to_int(word: str) -> int:
    return int(word, 16)


def _word_to_hex(word: str) -> str:
    return "0x" + word.lower()


def _topic_to_address(topic: str) -> str:
    value = topic[2:] if topic.startswith("0x") else topic
    if len(value) != 64:
        raise PolygonRpcError("address topic must be 32 bytes")
    return "0x" + value[-40:].lower()


def _decimal(raw: int) -> float:
    return raw / USDC_DECIMALS


def _hex_to_int(value: str) -> int:
    return int(str(value), 16)


def _int_to_hex(value: int) -> str:
    return hex(value)


def _chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def redact_rpc_url(url: str) -> str:
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    path = parts.path
    if path and path not in {"", "/"}:
        path = "/<redacted>"
    query = "<redacted>" if parts.query else ""
    return urlunsplit((parts.scheme, netloc, path, query, ""))
