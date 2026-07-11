"""Small, explicitly delayed execution mechanics for educational backtests."""
from __future__ import annotations

from dataclasses import dataclass

import polars as pl


@dataclass(frozen=True)
class BacktestResult:
    fills: pl.DataFrame
    equity: pl.DataFrame
    turnover: float
    max_drawdown: float


def build_transaction_bars(trades: pl.DataFrame, seconds: int) -> pl.DataFrame:
    """Aggregate fills into per-market transaction bars using each bar's last price."""
    if seconds <= 0:
        raise ValueError("seconds must be positive")
    required = {"timestamp", "price", "usd_amount"}
    missing = required - set(trades.columns)
    if missing:
        raise ValueError(f"trades missing columns: {sorted(missing)}")
    group_columns = [
        column for column in ("market_id", "nonusdc_side") if column in trades.columns
    ] + ["timestamp"]
    return (
        trades.with_columns(
            ((pl.col("timestamp").cast(pl.Int64) // seconds) * seconds).alias("timestamp")
        )
        .sort("timestamp")
        .group_by(group_columns, maintain_order=True)
        .agg([
            pl.col("price").last().alias("close"),
            pl.col("usd_amount").sum().alias("usd_volume"),
            pl.len().alias("n_trades"),
        ])
        .sort(group_columns)
    )


def simulate_next_observation_strategy(
    bars: pl.DataFrame,
    *,
    fee_bps: float,
    slippage_bps: float,
) -> BacktestResult:
    """Trade a target signal strictly at the next observed close.

    The strategy holds either zero or one synthetic share.  It is deliberately
    small: its purpose is to demonstrate timing, turnover, fees, and mark-to-
    market accounting, not to make a claim about a profitable rule.
    """
    required = {"timestamp", "close", "signal"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"bars missing columns: {sorted(missing)}")
    if fee_bps < 0 or slippage_bps < 0:
        raise ValueError("fee_bps and slippage_bps must be non-negative")

    fee_rate = fee_bps / 10_000
    slippage_rate = slippage_bps / 10_000
    cash = 0.0
    shares = 0.0
    turnover = 0.0
    peak_equity = 0.0
    max_drawdown = 0.0
    previous_signal = 0
    pending: tuple[int, int] | None = None
    fills: list[dict[str, float | int]] = []
    equity_rows: list[dict[str, float | int]] = []

    for row in bars.sort("timestamp").iter_rows(named=True):
        timestamp = int(row["timestamp"])
        close = float(row["close"])
        if pending is not None:
            target, signal_timestamp = pending
            delta = float(target) - shares
            if delta:
                direction = 1.0 if delta > 0 else -1.0
                fill_price = close * (1 + direction * slippage_rate)
                notional = abs(delta) * fill_price
                fee = notional * fee_rate
                cash -= delta * fill_price + fee
                shares = float(target)
                turnover += notional
                fills.append({
                    "signal_timestamp": signal_timestamp,
                    "fill_timestamp": timestamp,
                    "target_shares": target,
                    "delta_shares": delta,
                    "fill_price": fill_price,
                    "fee": fee,
                    "notional": notional,
                })
            pending = None

        marked_equity = cash + shares * close
        peak_equity = max(peak_equity, marked_equity)
        drawdown = (
            (peak_equity - marked_equity) / peak_equity if peak_equity > 0 else 0.0
        )
        max_drawdown = max(max_drawdown, drawdown)
        equity_rows.append({
            "timestamp": timestamp,
            "cash": cash,
            "shares": shares,
            "marked_equity": marked_equity,
            "exposure": shares * close,
            "drawdown": drawdown,
        })

        signal = int(row["signal"])
        if signal != previous_signal:
            pending = (signal, timestamp)
        previous_signal = signal

    return BacktestResult(
        fills=pl.DataFrame(fills, schema={
            "signal_timestamp": pl.Int64,
            "fill_timestamp": pl.Int64,
            "target_shares": pl.Int64,
            "delta_shares": pl.Float64,
            "fill_price": pl.Float64,
            "fee": pl.Float64,
            "notional": pl.Float64,
        }),
        equity=pl.DataFrame(equity_rows, schema={
            "timestamp": pl.Int64,
            "cash": pl.Float64,
            "shares": pl.Float64,
            "marked_equity": pl.Float64,
            "exposure": pl.Float64,
            "drawdown": pl.Float64,
        }),
        turnover=turnover,
        max_drawdown=max_drawdown,
    )
