from __future__ import annotations

from pathlib import Path

import polars as pl

from scripts.make_synthetic_smoke_fixture import build_fixture


def test_synthetic_fixture_is_v2_only(tmp_path: Path) -> None:
    store = build_fixture(tmp_path / "data_smoke")

    assert not (store.root / "orderFilled").exists()
    assert store.scan("order_filled_v2").collect().height > 0
    assert store.scan("trades").collect().height > 0
    outcomes = store.scan("market_outcomes").collect()
    assert {"token1", "token2"} <= set(outcomes["winner_token"])
    markets = store.scan("markets_current").collect()
    assert markets.height > outcomes.height
    assert markets.filter(~pl.col("closed")).height > 0
