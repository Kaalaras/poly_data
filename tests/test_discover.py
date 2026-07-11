from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest
import requests
import responses

from poly_data.io.parquet_store import ParquetStore
from poly_data.ingest.discover import (
    MarketDiscoveryError,
    _RateLimiter,
    discover_and_fetch,
    find_missing_token_ids,
)

_API_URL = "https://gamma-api.polymarket.com/markets"


def _seed_store(root: Path) -> ParquetStore:
    store = ParquetStore(root)
    # markets covers tokens A1/A2 only.
    store.append("markets", pl.DataFrame([{
        "id": "M1", "createdAt": "2025-01-01T00:00:00Z",
        "question": "?", "answer1": "Y", "answer2": "N", "neg_risk": False,
        "market_slug": "m1", "token1": "A1", "token2": "A2",
        "condition_id": "c", "volume": "0", "ticker": "T",
        "closedTime": "", "timestamp": 1735689600,
        "category": "Sports",
    }]))
    # orderFilled references A1/A2 (covered) plus B1/B2 (missing).
    store.append("orderFilled", pl.DataFrame([
        {"id": "o1", "timestamp": 1735689600,
         "maker": "m1", "makerAssetId": "A1", "makerAmountFilled": "1",
         "taker": "t1", "takerAssetId": "0",  "takerAmountFilled": "1",
         "transactionHash": "0x1"},
        {"id": "o2", "timestamp": 1735689600,
         "maker": "m2", "makerAssetId": "0",  "makerAmountFilled": "1",
         "taker": "t2", "takerAssetId": "B1", "takerAmountFilled": "1",
         "transactionHash": "0x2"},
        {"id": "o3", "timestamp": 1735689600,
         "maker": "m3", "makerAssetId": "B2", "makerAmountFilled": "1",
         "taker": "t3", "takerAssetId": "0",  "takerAmountFilled": "1",
         "transactionHash": "0x3"},
    ]))
    return store


def test_find_missing_returns_only_uncovered_tokens(tmp_path: Path) -> None:
    store = _seed_store(tmp_path / "data")
    missing = find_missing_token_ids(store)
    assert missing == ["B1", "B2"]


def test_find_missing_includes_v2_assets(tmp_path: Path) -> None:
    store = _seed_store(tmp_path / "data")
    store.append("order_filled_v2", pl.DataFrame([{
        "id": "v2-1",
        "timestamp": 1777374288,
        "block_number": 86127122,
        "block_timestamp": 1777374288,
        "transaction_hash": "0xtx",
        "user_id": "0xuser",
        "asset": "C1",
        "amount_usdc": 1.0,
        "amount_shares": 2.0,
        "price": 0.5,
        "side": "BUY",
        "order_hash": "0xorder",
        "counterparty_id": "0xmaker",
        "order_type": "maker",
        "fee": 0.0,
        "builder": "0x0",
    }]))

    assert find_missing_token_ids(store, sources=["order_filled_v2"]) == ["C1"]
    assert find_missing_token_ids(store) == ["B1", "B2", "C1"]


def test_find_missing_excludes_zero_and_empty(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    store.append("orderFilled", pl.DataFrame([
        {"id": "o1", "timestamp": 1735689600,
         "maker": "m1", "makerAssetId": "0", "makerAmountFilled": "1",
         "taker": "t1", "takerAssetId": "",  "takerAmountFilled": "1",
         "transactionHash": "0x1"},
    ]))
    assert find_missing_token_ids(store) == []


def test_find_missing_handles_missing_markets_source(tmp_path: Path) -> None:
    """missing_markets table absent → still works against markets only."""
    store = ParquetStore(tmp_path / "data")
    store.append("markets", pl.DataFrame([{
        "id": "M1", "createdAt": "2025-01-01T00:00:00Z",
        "question": "?", "answer1": "Y", "answer2": "N", "neg_risk": False,
        "market_slug": "m1", "token1": "A1", "token2": "A2",
        "condition_id": "c", "volume": "0", "ticker": "T",
        "closedTime": "", "timestamp": 1735689600, "category": "",
    }]))
    store.append("orderFilled", pl.DataFrame([
        {"id": "o1", "timestamp": 1735689600,
         "maker": "m", "makerAssetId": "B1", "makerAmountFilled": "1",
         "taker": "t", "takerAssetId": "0",  "takerAmountFilled": "1",
         "transactionHash": "0x1"},
    ]))
    assert find_missing_token_ids(store) == ["B1"]


def _fake_market(token_id: str, suffix: str) -> dict:
    return {
        "id": f"M-{suffix}",
        "createdAt": "2025-01-01T00:00:00Z",
        "question": f"q {suffix}",
        "outcomes": json.dumps(["Y", "N"]),
        "clobTokenIds": json.dumps([token_id, f"{token_id}-pair"]),
        "negRiskAugmented": False, "negRiskOther": False,
        "slug": f"q-{suffix}", "conditionId": f"c-{suffix}",
        "volume": "0", "events": [{"ticker": f"T-{suffix}"}],
        "closedTime": "", "category": "Sports",
    }


def _cached_empty(root: Path) -> list[str]:
    cache_path = root / "missing_markets" / "_discover_cache.json"
    if not cache_path.is_file():
        return []
    return json.loads(cache_path.read_text(encoding="utf-8")).get("empty", [])


@responses.activate
def test_discover_and_fetch_uses_csv_batch_param(tmp_path: Path) -> None:
    store = _seed_store(tmp_path / "data")
    # One batched response covering both missing IDs.
    responses.add(
        responses.GET,
        _API_URL,
        json=[_fake_market("B1", "b1"), _fake_market("B2", "b2")],
        status=200,
    )
    n = discover_and_fetch(store, batch_size=10, workers=1, rate_per_sec=100.0)
    assert n == 2
    df = store.scan("missing_markets").collect()
    assert sorted(df["id"].to_list()) == ["M-b1", "M-b2"]
    # Single call used array-repeat batch params (gamma rejects CSV).
    assert len(responses.calls) == 1
    url = responses.calls[0].request.url
    assert "clob_token_ids=B1" in url and "clob_token_ids=B2" in url
    assert "closed=true" in url


@responses.activate
def test_discover_and_fetch_falls_back_to_singles_when_batch_fails(
    tmp_path: Path,
) -> None:
    store = _seed_store(tmp_path / "data")
    # Batch returns 500 → fallback to singletons.
    responses.add(
        responses.GET,
        _API_URL,
        status=500,
    )
    responses.add(
        responses.GET,
        _API_URL,
        json=[_fake_market("B1", "b1")],
        status=200,
    )
    responses.add(
        responses.GET,
        _API_URL,
        json=[_fake_market("B2", "b2")],
        status=200,
    )
    n = discover_and_fetch(store, batch_size=10, workers=1, rate_per_sec=100.0)
    assert n == 2


@responses.activate
def test_discover_shrinks_batch_on_414(tmp_path: Path) -> None:
    """nginx 414 (URI Too Long) should split the chunk and retry."""
    store = _seed_store(tmp_path / "data")
    # First batch (B1+B2) → 414. Halves: (B1) → ok, (B2) → ok.
    responses.add(
        responses.GET,
        _API_URL,
        body="<html>414 URI too long</html>",
        status=414,
    )
    responses.add(
        responses.GET,
        _API_URL,
        json=[_fake_market("B2", "b2")],
        status=200,
    )
    responses.add(
        responses.GET,
        _API_URL,
        json=[_fake_market("B1", "b1")],
        status=200,
    )
    n = discover_and_fetch(store, batch_size=10, workers=1, rate_per_sec=100.0)
    assert n == 2
    assert sorted(
        store.scan("missing_markets").collect()["id"].to_list()
    ) == ["M-b1", "M-b2"]


@responses.activate
def test_negative_cache_skips_known_empty_ids_on_rerun(tmp_path: Path) -> None:
    store = _seed_store(tmp_path / "data")
    # Batch returns empty list → both IDs go to negative cache.
    responses.add(
        responses.GET,
        _API_URL,
        json=[],
        status=200,
    )
    n = discover_and_fetch(store, batch_size=10, workers=1, rate_per_sec=100.0)
    assert n == 0
    cache_path = tmp_path / "data" / "missing_markets" / "_discover_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert sorted(cache["empty"]) == ["B1", "B2"]

    # Re-run: the negative cache means zero requests are issued.
    responses.calls.reset()
    n2 = discover_and_fetch(store, batch_size=10, workers=1, rate_per_sec=100.0)
    assert n2 == 0
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.parametrize("status", [429, 500])
def test_discover_retryable_status_does_not_cache_negative(
    tmp_path: Path,
    status: int,
) -> None:
    root = tmp_path / "data"
    store = _seed_store(root)
    responses.add(responses.GET, _API_URL, status=status)
    with pytest.raises(MarketDiscoveryError):
        discover_and_fetch(
            store,
            batch_size=10,
            workers=1,
            rate_per_sec=100.0,
            use_batch=False,
        )
    assert _cached_empty(root) == []


@responses.activate
def test_discover_invalid_json_does_not_cache_negative(tmp_path: Path) -> None:
    root = tmp_path / "data"
    store = _seed_store(root)
    responses.add(
        responses.GET,
        _API_URL,
        body="{not-json",
        status=200,
        content_type="application/json",
    )
    with pytest.raises(MarketDiscoveryError):
        discover_and_fetch(
            store,
            batch_size=10,
            workers=1,
            rate_per_sec=100.0,
            use_batch=False,
        )
    assert _cached_empty(root) == []


@responses.activate
@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.ConnectionError("network down"),
        requests.exceptions.SSLError("tls failed"),
    ],
)
def test_discover_network_failures_do_not_cache_negative(
    tmp_path: Path,
    error: requests.RequestException,
) -> None:
    root = tmp_path / "data"
    store = _seed_store(root)
    responses.add(responses.GET, _API_URL, body=error)
    with pytest.raises(MarketDiscoveryError):
        discover_and_fetch(
            store,
            batch_size=10,
            workers=1,
            rate_per_sec=100.0,
            use_batch=False,
        )
    assert _cached_empty(root) == []


def test_rate_limiter_blocks_when_bucket_empty() -> None:
    import time as _t
    rl = _RateLimiter(rate_per_sec=4.0, burst=2)
    t0 = _t.monotonic()
    rl.acquire()
    rl.acquire()
    rl.acquire()  # bucket empty: must wait ~0.25s
    elapsed = _t.monotonic() - t0
    assert elapsed >= 0.20, f"expected throttle, got {elapsed:.3f}s"


@responses.activate
def test_discover_returns_zero_when_no_missing(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    store.append("markets", pl.DataFrame([{
        "id": "M1", "createdAt": "2025-01-01T00:00:00Z",
        "question": "?", "answer1": "Y", "answer2": "N", "neg_risk": False,
        "market_slug": "m1", "token1": "A1", "token2": "A2",
        "condition_id": "c", "volume": "0", "ticker": "T",
        "closedTime": "", "timestamp": 1735689600, "category": "",
    }]))
    store.append("orderFilled", pl.DataFrame([
        {"id": "o1", "timestamp": 1735689600,
         "maker": "m", "makerAssetId": "A1", "makerAmountFilled": "1",
         "taker": "t", "takerAssetId": "0",  "takerAmountFilled": "1",
         "transactionHash": "0x1"},
    ]))
    n = discover_and_fetch(store, batch_size=10, workers=1, rate_per_sec=100.0)
    assert n == 0
    assert len(responses.calls) == 0
