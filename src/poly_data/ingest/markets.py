from __future__ import annotations

import json
import logging
import time
from typing import Any

import polars as pl
import requests

from poly_data.io.parquet_store import ParquetStore
from poly_data.tls import configure_system_truststore

logger = logging.getLogger(__name__)

API_URL = "https://gamma-api.polymarket.com/markets"
KEYSET_API_URL = f"{API_URL}/keyset"

MARKET_COLUMNS = [
    "createdAt", "id", "question", "answer1", "answer2", "neg_risk",
    "market_slug", "token1", "token2", "condition_id", "volume", "ticker",
    "closedTime", "timestamp", "category",
]


def _existing_markets(store: ParquetStore) -> dict[str, dict[str, Any]]:
    try:
        rows = store.scan_markets_all().collect().to_dicts()
        return {
            str(row["id"]): {column: row.get(column) for column in MARKET_COLUMNS}
            for row in rows
            if row.get("id") is not None
        }
    except Exception:
        return {}


def _parse_market(market: dict[str, Any]) -> dict[str, Any] | None:
    try:
        outcomes_raw = market.get("outcomes", "[]")
        outcomes = (
            json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        )
        clob_raw = market.get("clobTokenIds", "[]")
        clob = json.loads(clob_raw) if isinstance(clob_raw, str) else clob_raw
        if len(clob) < 2:
            return None
        token1, token2 = str(clob[0]), str(clob[1])
        answer1 = outcomes[0] if outcomes else ""
        answer2 = outcomes[1] if len(outcomes) > 1 else ""
        neg_risk = bool(
            market.get("negRiskAugmented") or market.get("negRiskOther")
        )
        ticker = ""
        category = str(market.get("category", "") or "")
        events = market.get("events") or []
        if events:
            ticker = events[0].get("ticker", "")
            if not category:
                category = str(events[0].get("category", "") or "")

        created_at = market.get("createdAt", "")
        ts_int = _to_unix_seconds(created_at)
        return {
            "createdAt": str(created_at),
            "id": str(market.get("id", "")),
            "question": market.get("question") or market.get("title") or "",
            "answer1": str(answer1),
            "answer2": str(answer2),
            "neg_risk": neg_risk,
            "market_slug": str(market.get("slug", "")),
            "token1": token1,
            "token2": token2,
            "condition_id": str(market.get("conditionId", "")),
            "volume": str(market.get("volume", "")),
            "ticker": str(ticker),
            "closedTime": str(market.get("closedTime", "")),
            "timestamp": ts_int,
            "category": category,
        }
    except (ValueError, KeyError, IndexError, json.JSONDecodeError, TypeError) as e:
        logger.warning("market parse failed for id=%s: %s",
                       market.get("id", "?"), e)
        return None


def _to_unix_seconds(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if not value:
        return 0
    try:
        from datetime import datetime, timezone
        s = str(value).replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).astimezone(timezone.utc).timestamp())
    except Exception:
        return 0


def update_markets(store: ParquetStore, *, batch_size: int = 100) -> int:
    """Keyset-paginate Polymarket markets API and append unseen rows."""
    configure_system_truststore()
    page_size = min(max(1, batch_size), 100)
    existing = _existing_markets(store)
    if existing:
        logger.info("Checking %d locally known markets for metadata refresh", len(existing))
    total_inserted = 0
    after_cursor: str | None = None
    session = requests.Session()
    while True:
        params = {
            "ascending": "true",
            "closed": "true",
            "limit": page_size,
        }
        if after_cursor:
            params["after_cursor"] = after_cursor
        resp = session.get(KEYSET_API_URL, params=params, timeout=30)
        if resp.status_code in (429, 500, 502, 503, 504):
            logger.warning("API %s, sleeping 5s", resp.status_code)
            time.sleep(5)
            continue
        resp.raise_for_status()
        payload = resp.json()
        markets = payload.get("markets", []) if isinstance(payload, dict) else payload
        if not markets:
            break

        rows = []
        refresh_rows = []
        for raw in markets:
            row = _parse_market(raw)
            if row is None:
                continue
            previous = existing.get(row["id"])
            if previous is None:
                rows.append(row)
            elif previous != row:
                refresh_rows.append({**row, "observed_at": time.time_ns()})
            existing[row["id"]] = row
        if rows:
            store.append("markets", pl.DataFrame(rows))
            total_inserted += len(rows)
        if refresh_rows:
            store.append("market_refreshes", pl.DataFrame(refresh_rows))
            total_inserted += len(refresh_rows)

        after_cursor = payload.get("next_cursor") if isinstance(payload, dict) else None
        if not after_cursor:
            break

    return total_inserted


def update_missing_tokens(
    store: ParquetStore,
    missing_token_ids: list[str],
    *,
    inter_request_sleep: float = 0.5,
) -> int:
    """Fetch markets for token IDs not already in the store; append new ones."""
    if not missing_token_ids:
        return 0
    configure_system_truststore()

    existing_ids: set[str] = set()
    try:
        existing_ids = set(
            store.scan("missing_markets")
            .select("id")
            .collect()["id"]
            .to_list()
        )
    except Exception:
        pass

    session = requests.Session()
    new_rows: list[dict] = []
    for token_id in missing_token_ids:
        backoff = 2.0
        for attempt in range(3):
            try:
                resp = session.get(
                    API_URL,
                    params={"clob_token_ids": token_id, "closed": "true"},
                    timeout=30,
                )
                if resp.status_code == 429:
                    time.sleep(min(backoff * 4, 60))
                    backoff *= 2
                    continue
                if resp.status_code != 200:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                payload = resp.json()
                if not payload:
                    break
                row = _parse_market(payload[0])
                if row is None:
                    break
                if row["id"] in existing_ids:
                    break
                existing_ids.add(row["id"])
                new_rows.append(row)
                break
            except requests.RequestException:
                time.sleep(backoff)
                backoff *= 2
        time.sleep(inter_request_sleep)

    if new_rows:
        store.append("missing_markets", pl.DataFrame(new_rows))
    return len(new_rows)
