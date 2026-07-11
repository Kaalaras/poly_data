from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from poly_data.ingest.outcomes import parse_official_outcome, refresh_market_outcomes
from poly_data.io.parquet_store import ParquetStore


def _closed_market(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "M1",
        "token1": "yes-token",
        "token2": "no-token",
        "outcomePrices": '["1", "0"]',
        "closed": True,
        "closedTime": "2026-05-01T00:00:00Z",
        "resolutionSource": "official",
        "umaResolutionStatus": "resolved",
        "timestamp": 1_700_000_000,
    }
    row.update(overrides)
    return row


def test_parse_official_outcome_maps_price_one_to_token() -> None:
    outcome = parse_official_outcome(_closed_market())

    assert outcome is not None
    assert outcome["winner_token"] == "token1"
    assert outcome["market_id"] == "M1"
    assert outcome["resolved_at"] == outcome["timestamp"]


@pytest.mark.parametrize("prices", ['["0.5", "0.5"]', '["1", "1"]', "bad-json"])
def test_parse_official_outcome_rejects_ambiguous_prices(prices: str) -> None:
    assert parse_official_outcome(_closed_market(outcomePrices=prices)) is None


def test_refresh_market_outcomes_appends_once_per_market(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    store.append("markets_current", pl.DataFrame([_closed_market()]))

    assert refresh_market_outcomes(store) == {"added": 1, "skipped": 0}
    assert refresh_market_outcomes(store) == {"added": 0, "skipped": 0}
    outcomes = store.scan("market_outcomes").collect()
    assert outcomes.select("market_id").item() == "M1"
