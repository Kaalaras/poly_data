from __future__ import annotations

import polars as pl
import pytest

from poly_data.analysis.backtest import (
    build_transaction_bars,
    simulate_next_observation_strategy,
)


def _crossing_bars() -> pl.DataFrame:
    return pl.DataFrame({
        "timestamp": [1, 2, 3],
        "close": [0.4, 0.6, 0.7],
        "signal": [0, 1, 1],
    })


def test_strategy_executes_cross_at_next_observation() -> None:
    result = simulate_next_observation_strategy(
        _crossing_bars(), fee_bps=0, slippage_bps=0,
    )

    assert result.fills["signal_timestamp"].to_list() == [2]
    assert result.fills["fill_timestamp"].to_list() == [3]


def test_strategy_applies_buy_slippage_and_fee() -> None:
    result = simulate_next_observation_strategy(
        _crossing_bars(), fee_bps=10, slippage_bps=20,
    )

    assert result.fills["fill_price"].item() == pytest.approx(0.7 * 1.002)
    assert result.fills["fee"].item() == pytest.approx(0.7 * 1.002 * 0.001)


def test_transaction_bars_keep_last_price_and_volume() -> None:
    trades = pl.DataFrame({
        "timestamp": [1, 8, 12],
        "market_id": ["M1", "M1", "M1"],
        "nonusdc_side": ["token1", "token1", "token1"],
        "price": [0.2, 0.3, 0.4],
        "usd_amount": [2.0, 3.0, 4.0],
    })

    bars = build_transaction_bars(trades, seconds=10)

    assert bars["timestamp"].to_list() == [0, 10]
    assert bars["close"].to_list() == [0.3, 0.4]
    assert bars["usd_volume"].to_list() == [5.0, 4.0]
