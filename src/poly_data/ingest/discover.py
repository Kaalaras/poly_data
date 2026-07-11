"""Discover and fetch markets whose token IDs appear in orderFilled but not
in the local ``markets`` / ``missing_markets`` sources.

The legacy approach materialised every (makerAssetId, takerAssetId) pair into
a Python set and then issued one HTTP request per missing id with a 0.5 s
inter-request sleep. On a 151 M-row dataset with ~195k missing tokens that's
~27 hours sequential. This module:

1. Computes the missing set with a polars lazy anti-join (no Python set
   round-trip — runs in seconds even on 150 M+ rows).
2. Fetches markets in batches via the gamma ``clob_token_ids`` CSV param,
   then falls back to per-token requests for any IDs the batch didn't
   resolve (so we still discover what we can if the API rejects batch).
3. Maintains a persistent negative cache so re-runs don't re-hammer IDs
   the API has already confirmed don't exist.
4. Uses a thread pool for concurrent fetches with a shared rate limiter.

Drop-in replacement for the in-CLI ``_discover_and_fetch_missing_tokens``.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import polars as pl
import requests

from poly_data.io.parquet_store import ParquetStore
from poly_data.io.platform import atomic_write
from poly_data.ingest.markets import API_URL, _parse_market
from poly_data.tls import configure_system_truststore

logger = logging.getLogger(__name__)


class MarketDiscoveryError(RuntimeError):
    """Raised when market metadata lookup cannot distinguish empty from failed."""


# --- step 1: lazy missing-id scan ------------------------------------------


def find_missing_token_ids(
    store: ParquetStore,
    *,
    sources: Iterable[str] | None = None,
) -> list[str]:
    """Return token IDs referenced by raw fills but absent from the local
    ``markets`` ∪ ``missing_markets`` token columns.

    Pure polars: no Python set materialisation. On a 151 M-row dataset this
    runs in < 30 s and uses < 1 GB of RAM thanks to streaming + anti-join.
    """
    selected_sources = set(sources or ("orderFilled", "order_filled_v2"))
    referenced_parts: list[pl.LazyFrame] = []

    if "orderFilled" in selected_sources:
        of_lf = store.scan("orderFilled")
        of_cols = of_lf.collect_schema().names()
        if "makerAssetId" in of_cols and "takerAssetId" in of_cols:
            referenced_parts.extend([
                of_lf.select(pl.col("makerAssetId").alias("asset_id")),
                of_lf.select(pl.col("takerAssetId").alias("asset_id")),
            ])

    if "order_filled_v2" in selected_sources:
        v2_lf = store.scan("order_filled_v2")
        v2_cols = v2_lf.collect_schema().names()
        if "asset" in v2_cols:
            referenced_parts.append(v2_lf.select(pl.col("asset").alias("asset_id")))

    if not referenced_parts:
        return []

    referenced = (
        pl.concat(referenced_parts, how="diagonal_relaxed")
        .with_columns(pl.col("asset_id").cast(pl.String))
        .filter(pl.col("asset_id").is_not_null())
        .filter((pl.col("asset_id") != "0") & (pl.col("asset_id") != ""))
        .unique()
    )

    known_parts: list[pl.LazyFrame] = []
    for source in ("markets", "missing_markets"):
        try:
            mkt = store.scan(source)
            cols = mkt.collect_schema().names()
            if "token1" in cols:
                known_parts.append(mkt.select(pl.col("token1").alias("asset_id")))
            if "token2" in cols:
                known_parts.append(mkt.select(pl.col("token2").alias("asset_id")))
        except Exception:
            continue

    if known_parts:
        known = pl.concat(known_parts).unique()
        missing = referenced.join(known, on="asset_id", how="anti")
    else:
        missing = referenced

    df = missing.collect(engine="streaming")
    if df.height == 0:
        return []
    return sorted(df["asset_id"].to_list())


# --- negative cache --------------------------------------------------------


def _cache_path(store: ParquetStore) -> Path:
    return store.root / "missing_markets" / "_discover_cache.json"


def _load_cache(store: ParquetStore) -> dict:
    p = _cache_path(store)
    if not p.is_file():
        return {"empty": []}
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            return {"empty": []}
        obj.setdefault("empty", [])
        return obj
    except (OSError, json.JSONDecodeError):
        return {"empty": []}


def _save_cache(store: ParquetStore, cache: dict) -> None:
    p = _cache_path(store)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(p, json.dumps(cache, sort_keys=True))


# --- token-bucket rate limiter ---------------------------------------------


class _RateLimiter:
    """Simple thread-safe token bucket. Default: 8 req/s."""

    def __init__(self, rate_per_sec: float = 8.0, burst: int = 8) -> None:
        self.rate = rate_per_sec
        self.capacity = burst
        self.tokens = float(burst)
        self.last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last = now
            if self.tokens < 1.0:
                wait = (1.0 - self.tokens) / self.rate
            else:
                wait = 0.0
            self.tokens -= 1.0
        if wait > 0:
            time.sleep(wait)


# --- batched + parallel fetch ----------------------------------------------


def _request_batch(
    session: requests.Session,
    ids: list[str],
    *,
    limiter: _RateLimiter,
    timeout: int = 30,
) -> tuple[list[dict] | None, str]:
    """Try a batched gamma request. Returns (markets, status).

    ``status`` is one of:
      - ``"ok"`` — payload contains markets matching some/all IDs.
      - ``"too_large"`` — HTTP 414. Caller should split and retry smaller.
      - ``"retryable"`` — fall back to per-id fetch; do not cache empties.

    gamma rejects CSV (`a,b`) with 422 — it expects array-repeat
    (`?clob_token_ids=a&clob_token_ids=b`), which `requests` produces from
    a list-of-tuples params. Verified empirically against
    https://gamma-api.polymarket.com/markets. Beyond ~30 IDs nginx replies
    414 (URI Too Long; each token id is an 80-char decimal string).
    """
    limiter.acquire()
    try:
        resp = session.get(
            API_URL,
            params=[*( ("clob_token_ids", tid) for tid in ids), ("closed", "true")],
            timeout=timeout,
        )
    except requests.RequestException as e:
        logger.warning("batch request failed: %s", e)
        return None, "retryable"
    if resp.status_code == 414:
        return None, "too_large"
    if resp.status_code != 200:
        return None, "retryable"
    try:
        payload = resp.json()
    except ValueError:
        return None, "retryable"
    if not isinstance(payload, list):
        return None, "retryable"
    return payload, "ok"


def _request_single(
    session: requests.Session,
    token_id: str,
    *,
    limiter: _RateLimiter,
    timeout: int = 30,
) -> tuple[dict | None, str]:
    limiter.acquire()
    try:
        resp = session.get(
            API_URL,
            params={"clob_token_ids": token_id, "closed": "true"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        logger.warning("single market request failed for %s: %s", token_id, e)
        return None, "retryable"
    if resp.status_code != 200:
        return None, "retryable"
    try:
        payload = resp.json()
    except ValueError:
        return None, "retryable"
    if not isinstance(payload, list):
        return None, "retryable"
    if not payload:
        return None, "empty"
    return payload[0], "ok"


def _fetch_chunk(
    chunk: list[str],
    *,
    limiter: _RateLimiter,
    session: requests.Session,
    use_batch: bool,
) -> tuple[list[dict], set[str]]:
    """Fetch one chunk of IDs. Returns (parsed_market_rows, ids_with_no_market).

    The ``ids_with_no_market`` set contains only API-confirmed empty token IDs.
    Retryable failures raise ``MarketDiscoveryError`` so callers fail closed and
    do not write transient failures to the negative cache.
    """
    rows: list[dict] = []
    matched_tokens: set[str] = set()

    if use_batch:
        # Try in halves on 414 until each sub-chunk fits.
        queue: list[list[str]] = [chunk]
        gave_up_batch = False
        while queue:
            sub = queue.pop()
            markets, status = _request_batch(session, sub, limiter=limiter)
            if status == "too_large":
                if len(sub) <= 1:
                    gave_up_batch = True
                    break
                mid = len(sub) // 2
                queue.append(sub[:mid])
                queue.append(sub[mid:])
                continue
            if status != "ok" or markets is None:
                gave_up_batch = True
                break
            for raw in markets:
                parsed = _parse_market(raw)
                if parsed is None:
                    continue
                rows.append(parsed)
                matched_tokens.add(parsed["token1"])
                matched_tokens.add(parsed["token2"])
        if not gave_up_batch:
            unmatched = [tid for tid in chunk if tid not in matched_tokens]
            return rows, set(unmatched)
        # Otherwise fall through to singletons.

    unmatched: set[str] = set()
    for tid in chunk:
        raw, status = _request_single(session, tid, limiter=limiter)
        if status == "retryable":
            raise MarketDiscoveryError(f"market discovery failed for token {tid}")
        if status == "empty" or raw is None:
            unmatched.add(tid)
            continue
        parsed = _parse_market(raw)
        if parsed is None:
            unmatched.add(tid)
            continue
        rows.append(parsed)
        matched_tokens.add(parsed["token1"])
        matched_tokens.add(parsed["token2"])
    return rows, unmatched


def discover_and_fetch(
    store: ParquetStore,
    *,
    sources: Iterable[str] | None = None,
    max_ids: int | None = None,
    batch_size: int = 25,
    workers: int = 8,
    rate_per_sec: float = 8.0,
    use_batch: bool = True,
) -> int:
    """Find missing token IDs and fetch their markets concurrently.

    Returns the number of new markets appended to ``missing_markets``.
    Honours a persistent negative cache so re-runs don't re-fetch IDs the
    API has previously returned no market for.
    """
    missing_ids = find_missing_token_ids(store, sources=sources)
    if not missing_ids:
        logger.info("discover: no missing token IDs")
        return 0

    cache = _load_cache(store)
    empty_cache: set[str] = set(cache.get("empty", []))
    fresh = [tid for tid in missing_ids if tid not in empty_cache]
    logger.info(
        "discover: %d referenced ids missing from store, "
        "%d in negative cache, %d to fetch",
        len(missing_ids), len(missing_ids) - len(fresh), len(fresh),
    )
    if max_ids is not None:
        fresh = fresh[:max_ids]
    if not fresh:
        return 0

    configure_system_truststore()

    existing_ids: set[str] = set()
    try:
        existing_ids = set(
            store.scan("missing_markets").select("id").collect()["id"].to_list()
        )
    except Exception:
        pass

    chunks = [fresh[i:i + batch_size] for i in range(0, len(fresh), batch_size)]
    limiter = _RateLimiter(rate_per_sec=rate_per_sec, burst=max(1, int(rate_per_sec)))
    session = requests.Session()

    new_rows: list[dict] = []
    rows_lock = threading.Lock()
    progress_lock = threading.Lock()
    progress = {"chunks_done": 0, "total_new": 0}

    def _runner(chunk: list[str]) -> tuple[list[dict], set[str]]:
        return _fetch_chunk(chunk, limiter=limiter, session=session,
                            use_batch=use_batch)

    last_persist = time.monotonic()
    PERSIST_EVERY_SECS = 30.0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_runner, c): c for c in chunks}
        for fut in as_completed(futures):
            try:
                rows, unmatched = fut.result()
            except MarketDiscoveryError:
                raise
            except Exception as e:
                raise MarketDiscoveryError("market discovery worker failed") from e

            added_now = 0
            with rows_lock:
                for r in rows:
                    if r["id"] in existing_ids:
                        continue
                    existing_ids.add(r["id"])
                    new_rows.append(r)
                    added_now += 1

            with progress_lock:
                empty_cache |= unmatched
                progress["chunks_done"] += 1
                progress["total_new"] += added_now
                done = progress["chunks_done"]

                # Periodically persist negative cache so a Ctrl-C mid-run
                # doesn't lose ~hours of "we already know these don't exist".
                now = time.monotonic()
                if now - last_persist > PERSIST_EVERY_SECS:
                    _save_cache(store, {
                        "empty": sorted(empty_cache),
                        "updated_at": int(time.time()),
                    })
                    last_persist = now

                if done % 10 == 0 or done == len(chunks):
                    logger.info(
                        "discover: %d/%d chunks, +%d markets so far, "
                        "%d known-empty",
                        done, len(chunks), progress["total_new"],
                        len(empty_cache),
                    )

    if new_rows:
        store.append("missing_markets", pl.DataFrame(new_rows))

    _save_cache(store, {
        "empty": sorted(empty_cache),
        "updated_at": int(time.time()),
    })
    logger.info(
        "discover: appended %d new markets, %d ids cached as empty",
        len(new_rows), len(empty_cache),
    )
    return len(new_rows)
