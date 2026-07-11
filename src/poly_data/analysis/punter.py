"""Reframe Polymarket trades as a sportsbook (punter ↔ bookmaker) view.

The raw `trades` source records every order-fill symmetrically: maker and
taker are just two sides of the same orderbook event. For betting analytics
(and copy-betting) we want the *intent* layer:

- **Punter** = `taker`. They crossed the spread to enter a position now.
  Active discretionary intent, exactly like a sportsbook bet.
- **Bookmaker** = `maker`. They posted a passive limit, more like
  market-making / quote-provision than betting.

This module provides the filter + per-user entry-event detection used by
``examples/06-copy-betting.ipynb``.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

import polars as pl


# Polymarket protocol routers / internal liquidity wallets — not real punters.
# Sourced from the legacy README and confirmed by inspecting trade frequency
# (these two addresses appear on hundreds of thousands of fills each).
PLATFORM_WALLETS: frozenset[str] = frozenset({
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
})


# --- filter ----------------------------------------------------------------


def punter_view(
    trades: pl.LazyFrame,
    *,
    min_usd: float = 1.0,
    price_floor: float = 0.02,
    price_ceiling: float = 0.98,
    drop_platform_wallets: bool = True,
) -> pl.LazyFrame:
    """Filter the orderbook ``trades`` source down to "real bets".

    Rules:
    - drop fills involving platform/router wallets (PLATFORM_WALLETS),
    - drop self-trades (``maker == taker``),
    - drop dust (``usd_amount < min_usd`` — default $1),
    - drop extreme-price fills (``price < floor`` or ``> ceiling``) — those
      are typically settlement-day liquidity cleanup, not discretionary bets.

    The result keeps every column from ``trades``; downstream code uses
    ``taker`` as the canonical "punter" identifier and ``taker_direction``
    as the bet direction (BUY = punter took a long position in
    ``nonusdc_side``).
    """
    out = trades.filter(pl.col("maker") != pl.col("taker"))
    if drop_platform_wallets:
        plats = list(PLATFORM_WALLETS)
        out = out.filter(
            ~pl.col("maker").is_in(plats) & ~pl.col("taker").is_in(plats)
        )
    out = out.filter(pl.col("usd_amount") >= min_usd)
    out = out.filter(
        (pl.col("price") >= price_floor) & (pl.col("price") <= price_ceiling)
    )
    return out


# --- entry-event detection -------------------------------------------------


def punter_position_timeline(punter_trades: pl.DataFrame) -> pl.DataFrame:
    """Per ``(taker, market_id, token_side)`` cumulative position timeline.

    For each fill (already filtered to punter view), emit:
    - ``signed_tokens`` — +token_amount for BUY, -token_amount for SELL,
    - ``cum_position`` — running sum of ``signed_tokens`` per
      (taker, market_id, token_side),
    - ``prev_position`` — value of ``cum_position`` *before* this fill,
    - ``event_kind`` — one of ``"entry"``, ``"add"``, ``"reduce"``,
      ``"exit"``, ``"flip"`` (see classification rules in code).

    The frame is sorted by ``timestamp`` within each group.
    """
    df = punter_trades.with_columns(
        pl.when(pl.col("taker_direction") == "BUY")
          .then(pl.col("token_amount"))
          .otherwise(-pl.col("token_amount"))
          .alias("signed_tokens")
    )
    df = df.sort("timestamp")
    df = df.with_columns(
        pl.col("signed_tokens")
          .cum_sum()
          .over(["taker", "market_id", "nonusdc_side"])
          .alias("cum_position")
    )
    df = df.with_columns(
        (pl.col("cum_position") - pl.col("signed_tokens")).alias("prev_position")
    )
    return df.with_columns(
        pl.when(pl.col("prev_position") == 0)
          .then(pl.lit("entry"))
        .when(
            (pl.col("prev_position").sign() == pl.col("cum_position").sign())
            & (pl.col("cum_position").abs() > pl.col("prev_position").abs())
        ).then(pl.lit("add"))
        .when(
            (pl.col("prev_position").sign() != pl.col("cum_position").sign())
            & (pl.col("cum_position") != 0)
        ).then(pl.lit("flip"))
        .when(pl.col("cum_position") == 0).then(pl.lit("exit"))
        .otherwise(pl.lit("reduce"))
        .alias("event_kind")
    )


def entries_only(timeline: pl.DataFrame) -> pl.DataFrame:
    """Rows where ``event_kind == 'entry'`` — the canonical "new bet" events."""
    return timeline.filter(pl.col("event_kind") == "entry")


# --- leader cohort ---------------------------------------------------------


def punter_player_stats(
    punter_trades: pl.LazyFrame,
    *,
    win_threshold: float = 0.98,
) -> pl.DataFrame:
    """Compute per-punter aggregate stats over taker-side bets only.

    Wraps ``poly_data.analysis.positions.compute_player_stats`` after
    pinning ``player_side="taker"`` so makers (market-makers, scalpers)
    don't dilute the leader cohort.
    """
    from poly_data.analysis.positions import compute_player_stats
    return compute_player_stats(
        punter_trades, player_side="taker", win_threshold=win_threshold
    )


# --- copy-bet simulation ---------------------------------------------------


@dataclass
class CopyBetResult:
    bets: pl.DataFrame  # one row per executed copy bet
    summary: dict
    cashflows: pl.DataFrame | None = None


def _simulate_copy_bet_legacy(
    entries: pl.DataFrame,
    all_punter_trades: pl.DataFrame,
    resolutions: pl.DataFrame,
    leaders: set[str],
    *,
    train_end_ts: int,
    test_end_ts: int,
    bankroll: float = 10_000.0,
    per_bet_frac: float = 0.02,
    slippage_window_secs: int = 3600,
) -> CopyBetResult:
    """Replay leader entries within a test window with worst-case slippage.

    For each leader's ``entry`` event in ``[train_end_ts, test_end_ts)``:
    1. Find the next punter trade in the same ``(market_id, nonusdc_side)``
       within ``slippage_window_secs``. The price of *that* trade is the
       fill we'd receive (proxy for "next available liquidity").
    2. Size = ``bankroll * per_bet_frac`` USDC.
    3. Hold to market resolution; PnL via ``resolutions.winner_token``.

    No look-ahead: we only use data ``< entry.timestamp + slippage_window``.
    """
    leader_entries = (
        entries.filter(pl.col("taker").is_in(list(leaders)))
               .filter(pl.col("timestamp") >= train_end_ts)
               .filter(pl.col("timestamp") < test_end_ts)
               .sort("timestamp")
    )
    if leader_entries.height == 0:
        return CopyBetResult(
            bets=leader_entries.head(0),
            summary={"n_bets": 0, "total_pnl_usd": 0.0},
        )

    # Pre-index next-trade prices per (market_id, nonusdc_side) for slippage.
    next_trades = (
        all_punter_trades
        .select(["market_id", "nonusdc_side", "timestamp", "price"])
        .sort("timestamp")
    )

    bets_rows = []
    bet_size = bankroll * per_bet_frac
    for row in leader_entries.iter_rows(named=True):
        mid = row["market_id"]
        side = row["nonusdc_side"]
        ts = row["timestamp"]
        # next trade in same (market, side) within slippage window
        candidate = (
            next_trades
            .filter(pl.col("market_id") == mid)
            .filter(pl.col("nonusdc_side") == side)
            .filter(pl.col("timestamp") > ts)
            .filter(pl.col("timestamp") <= ts + slippage_window_secs)
            .head(1)
        )
        if candidate.height == 0:
            continue  # no fillable next print; skip
        fill_price = float(candidate["price"][0])
        fill_ts = int(candidate["timestamp"][0])
        tokens = bet_size / fill_price if fill_price > 0 else 0.0

        # PnL using resolutions: winner_token decides 0/1 per side.
        res = resolutions.filter(pl.col("market_id") == mid)
        if res.height == 0:
            payoff = None  # market not resolved (or unknown)
        else:
            winner = res["winner_token"][0]
            if winner == "open":
                payoff = None
            elif winner == side:
                payoff = tokens * 1.0
            else:
                payoff = 0.0
        bets_rows.append({
            "leader": row["taker"],
            "market_id": mid,
            "token_side": side,
            "leader_price": float(row["price"]),
            "fill_price": fill_price,
            "leader_ts": ts,
            "fill_ts": fill_ts,
            "slippage": fill_price - float(row["price"]),
            "bet_usd": bet_size,
            "tokens": tokens,
            "payoff_usd": payoff if payoff is not None else float("nan"),
            "pnl_usd": (payoff - bet_size) if payoff is not None else float("nan"),
            "resolved": payoff is not None,
        })

    bets = pl.DataFrame(bets_rows) if bets_rows else leader_entries.head(0)
    if bets.height == 0:
        return CopyBetResult(bets=bets, summary={"n_bets": 0, "total_pnl_usd": 0.0})

    resolved = bets.filter(pl.col("resolved"))
    summary = {
        "n_bets": bets.height,
        "n_resolved": resolved.height,
        "n_won": int((resolved["payoff_usd"] > 0).sum()) if resolved.height else 0,
        "total_pnl_usd": float(resolved["pnl_usd"].sum()) if resolved.height else 0.0,
        "mean_slippage": float(bets["slippage"].mean()),
        "win_rate": (
            float((resolved["payoff_usd"] > 0).mean()) if resolved.height else 0.0
        ),
    }
    return CopyBetResult(bets=bets, summary=summary)


@dataclass(frozen=True)
class RandomCohortResult:
    leaders: set[str]


def simulate_random_cohort(
    entries: pl.DataFrame, *, seed: int, n_leaders: int = 1,
) -> RandomCohortResult:
    """Choose a reproducible control cohort from eligible leader identities."""
    candidates = sorted(entries["taker"].unique().to_list())
    return RandomCohortResult(
        leaders=set(random.Random(seed).sample(candidates, min(n_leaders, len(candidates)))),
    )


def simulate_copy_bet(
    entries: pl.DataFrame,
    all_punter_trades: pl.DataFrame,
    outcomes: pl.DataFrame,
    leaders: set[str],
    *,
    train_end_ts: int,
    test_end_ts: int,
    bankroll: float = 10_000.0,
    per_bet_frac: float = 0.02,
    latency_secs: int = 1,
    fee_bps: float = 0.0,
    random_seed: int = 0,
) -> CopyBetResult:
    """Copy BUY and SELL entries at the next opposite observation, settle at resolution."""
    del random_seed
    rows: list[dict[str, object]] = []
    flows: list[dict[str, float | int]] = []
    capital = bankroll * per_bet_frac
    selected = entries.filter(pl.col("taker").is_in(list(leaders))).filter(
        (pl.col("timestamp") >= train_end_ts) & (pl.col("timestamp") < test_end_ts)
    ).sort("timestamp")
    for signal in selected.iter_rows(named=True):
        direction = str(signal["taker_direction"])
        candidate = (
            all_punter_trades
            .filter((pl.col("market_id") == signal["market_id"])
                    & (pl.col("nonusdc_side") == signal["nonusdc_side"])
                    & (pl.col("taker_direction") != direction)
                    & (pl.col("timestamp") >= int(signal["timestamp"]) + latency_secs)
                    & (pl.col("timestamp") < test_end_ts))
            .sort("timestamp").head(1)
        )
        outcome = outcomes.filter(pl.col("market_id") == signal["market_id"])
        if candidate.height == 0 or outcome.height == 0:
            continue
        fill_price = float(candidate["price"][0])
        fill_ts = int(candidate["timestamp"][0])
        settled_ts = int(outcome["resolved_at"][0])
        if settled_ts > test_end_ts:
            continue
        winner = str(outcome["winner_token"][0])
        fee = capital * fee_bps / 10_000
        if direction == "BUY":
            shares = capital / fill_price
            payoff = shares if winner == signal["nonusdc_side"] else 0.0
            pnl = payoff - capital - fee
        else:
            shares = capital / max(1 - fill_price, 1e-9)
            payoff = capital + shares * (fill_price - (1.0 if winner == signal["nonusdc_side"] else 0.0)) - fee
            pnl = payoff - capital
        rows.append({"leader": signal["taker"], "market_id": signal["market_id"], "token_side": signal["nonusdc_side"], "direction": direction, "signal_ts": signal["timestamp"], "fill_ts": fill_ts, "settled_ts": settled_ts, "capital_reserved": capital, "fill_price": fill_price, "pnl_usd": pnl, "resolved": True})
        flows.extend([{"timestamp": fill_ts, "amount_usd": -capital - fee}, {"timestamp": settled_ts, "amount_usd": payoff}])
    bets = pl.DataFrame(rows) if rows else pl.DataFrame(schema={"direction": pl.String, "signal_ts": pl.Int64, "fill_ts": pl.Int64, "settled_ts": pl.Int64, "capital_reserved": pl.Float64, "fill_price": pl.Float64, "pnl_usd": pl.Float64, "resolved": pl.Boolean})
    cashflows = pl.DataFrame(flows) if flows else pl.DataFrame(schema={"timestamp": pl.Int64, "amount_usd": pl.Float64})
    return CopyBetResult(bets=bets, cashflows=cashflows, summary={"n_bets": bets.height, "total_pnl_usd": float(bets["pnl_usd"].sum()) if bets.height else 0.0})
