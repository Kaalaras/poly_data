from __future__ import annotations

import numpy as np
import polars as pl

from poly_data.analysis.ml_evaluation import (
    expanding_folds,
    evaluate_edge,
    probability_metrics,
    select_fold_players,
)


def _trades() -> pl.LazyFrame:
    return pl.DataFrame([
        {"timestamp": 1, "market_id": "past", "maker": "past_winner", "taker": "other", "nonusdc_side": "token1", "maker_direction": "BUY", "taker_direction": "SELL", "price": 0.5, "usd_amount": 5.0, "token_amount": 10.0},
        {"timestamp": 2, "market_id": "future", "maker": "future_only_winner", "taker": "other", "nonusdc_side": "token1", "maker_direction": "BUY", "taker_direction": "SELL", "price": 0.5, "usd_amount": 5.0, "token_amount": 10.0},
    ]).lazy()


def _outcomes() -> pl.DataFrame:
    return pl.DataFrame([
        {"market_id": "past", "winner_token": "token1", "resolved_at": 5},
        {"market_id": "future", "winner_token": "token1", "resolved_at": 50},
    ])


def test_fold_player_selection_excludes_future_resolutions() -> None:
    players = select_fold_players(_trades(), _outcomes(), train_end_ts=10, n=5)

    assert "future_only_winner" not in players["player"].to_list()


def test_probability_metrics_reward_calibration() -> None:
    good = probability_metrics(np.array([0, 1]), np.array([0.1, 0.9]))
    bad = probability_metrics(np.array([0, 1]), np.array([0.9, 0.1]))

    assert good["log_loss"] < bad["log_loss"]
    assert good["brier"] < bad["brier"]


def test_expanding_folds_and_edge_evaluation() -> None:
    folds = expanding_folds([1, 2, 3, 4, 5, 6], n_folds=2)
    edge = evaluate_edge(np.array([0.7, 0.4]), np.array([0.5, 0.5]), np.array([1, 0]), 0.1)

    assert len(folds) == 2
    assert folds[0].train_end_ts < folds[0].test_start_ts
    assert edge["take"].to_list() == [True, False]
