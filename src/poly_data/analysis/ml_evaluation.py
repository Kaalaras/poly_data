"""Temporal, official-outcome evaluation helpers for probability models."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import polars as pl
from sklearn.metrics import brier_score_loss, log_loss

from poly_data.analysis.positions import compute_player_stats_from_outcomes
from poly_data.analysis.ranking import score_C, select_top_n


@dataclass(frozen=True)
class TemporalFold:
    train_end_ts: int
    test_start_ts: int
    test_end_ts: int


def expanding_folds(decision_dates: Sequence[int], n_folds: int) -> list[TemporalFold]:
    """Create chronological expanding-window folds without date overlap."""
    dates = sorted(set(int(value) for value in decision_dates))
    if n_folds < 1:
        raise ValueError("n_folds must be positive")
    if len(dates) < n_folds + 1:
        return []
    boundaries = np.linspace(1, len(dates) - 1, n_folds + 1, dtype=int)
    folds: list[TemporalFold] = []
    for index in range(n_folds):
        start_index = int(boundaries[index])
        end_index = int(boundaries[index + 1])
        if end_index <= start_index:
            continue
        folds.append(TemporalFold(
            train_end_ts=dates[start_index - 1],
            test_start_ts=dates[start_index],
            test_end_ts=dates[end_index],
        ))
    return folds


def select_fold_players(
    trades: pl.LazyFrame,
    outcomes: pl.DataFrame,
    train_end_ts: int,
    n: int,
) -> pl.DataFrame:
    """Rank only trades and official outcomes knowable at a fold cutoff."""
    historical = trades.filter(pl.col("timestamp") < train_end_ts)
    observed_outcomes = outcomes.filter(pl.col("resolved_at") < train_end_ts)
    stats = compute_player_stats_from_outcomes(historical, observed_outcomes)
    return select_top_n(stats, n=n, min_win_rate=0.5, min_n_bets=20, score_fn=score_C)


def probability_metrics(y_true: np.ndarray, probabilities: np.ndarray) -> dict[str, float]:
    """Return proper scoring rules for binary probabilistic forecasts."""
    if y_true.size == 0:
        raise ValueError("y_true must not be empty")
    probabilities = np.clip(np.asarray(probabilities, dtype=float), 1e-6, 1 - 1e-6)
    y_true = np.asarray(y_true, dtype=int)
    return {
        "log_loss": float(log_loss(y_true, probabilities, labels=[0, 1])),
        "brier": float(brier_score_loss(y_true, probabilities)),
    }


def evaluate_edge(
    probabilities: np.ndarray,
    market_prices: np.ndarray,
    outcomes: np.ndarray,
    threshold: float,
) -> pl.DataFrame:
    """Report held-out model edges; it deliberately makes no PnL claim."""
    probabilities = np.asarray(probabilities, dtype=float)
    market_prices = np.asarray(market_prices, dtype=float)
    outcomes = np.asarray(outcomes, dtype=int)
    if not (len(probabilities) == len(market_prices) == len(outcomes)):
        raise ValueError("probabilities, market_prices, and outcomes must have equal length")
    edge = probabilities - market_prices
    return pl.DataFrame({
        "probability": probabilities,
        "market_price": market_prices,
        "edge": edge,
        "take": edge > threshold,
        "outcome": outcomes,
    })
