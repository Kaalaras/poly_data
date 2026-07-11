from __future__ import annotations

import polars as pl

from poly_data.analysis.punter import (
    PLATFORM_WALLETS,
    entries_only,
    punter_position_timeline,
    punter_view,
)


def _trade(ts, mid, maker, taker, side, t_dir, m_dir, price, usd, tok, h):
    return {
        "timestamp": ts, "market_id": mid,
        "maker": maker, "taker": taker,
        "nonusdc_side": side,
        "maker_direction": m_dir, "taker_direction": t_dir,
        "price": price, "usd_amount": usd, "token_amount": tok,
        "transactionHash": h,
    }


def test_punter_view_drops_platform_wallets() -> None:
    plat = list(PLATFORM_WALLETS)[0]
    df = pl.DataFrame([
        _trade(1, "M1", plat,    "P2", "token1", "BUY",  "SELL", 0.5, 50, 100, "h1"),
        _trade(2, "M1", "P3",    plat, "token1", "BUY",  "SELL", 0.5, 50, 100, "h2"),
        _trade(3, "M1", "P3",    "P4", "token1", "BUY",  "SELL", 0.5, 50, 100, "h3"),
    ])
    out = punter_view(df.lazy()).collect()
    assert out.height == 1
    assert out["taker"][0] == "P4"


def test_punter_view_drops_dust_and_extreme_prices() -> None:
    df = pl.DataFrame([
        _trade(1, "M1", "Pa", "Pb", "token1", "BUY", "SELL", 0.50, 50.0,   100,  "h1"),
        _trade(2, "M1", "Pa", "Pc", "token1", "BUY", "SELL", 0.50, 0.10,    1,   "h2"),  # dust
        _trade(3, "M1", "Pa", "Pd", "token1", "BUY", "SELL", 0.005,5.00,  1000,  "h3"),  # too cheap
        _trade(4, "M1", "Pa", "Pe", "token1", "BUY", "SELL", 0.99, 99.0,   100,  "h4"),  # too expensive
    ])
    out = punter_view(df.lazy()).collect()
    assert out["taker"].to_list() == ["Pb"]


def test_punter_view_drops_self_trades() -> None:
    df = pl.DataFrame([
        _trade(1, "M1", "P1", "P1", "token1", "BUY", "SELL", 0.5, 50, 100, "h1"),
        _trade(2, "M1", "Pa", "Pb", "token1", "BUY", "SELL", 0.5, 50, 100, "h2"),
    ])
    assert punter_view(df.lazy()).collect().height == 1


def test_position_timeline_classifies_events() -> None:
    """P1: entry → add → reduce → flip (short via single oversized SELL) → exit."""
    df = pl.DataFrame([
        _trade(1, "M1", "Mk", "P1", "token1", "BUY",  "SELL", 0.5, 50, 100, "h1"),  # entry, cum=+100
        _trade(2, "M1", "Mk", "P1", "token1", "BUY",  "SELL", 0.5, 25,  50, "h2"),  # add, cum=+150
        _trade(3, "M1", "Mk", "P1", "token1", "SELL", "BUY",  0.5, 10,  20, "h3"),  # reduce, cum=+130
        _trade(4, "M1", "Mk", "P1", "token1", "SELL", "BUY",  0.5, 100, 200, "h4"), # flip, cum=-70
        _trade(5, "M1", "Mk", "P1", "token1", "BUY",  "SELL", 0.5, 35,  70, "h5"),  # exit, cum=0
    ])
    timeline = punter_position_timeline(df).sort("timestamp")
    kinds = timeline["event_kind"].to_list()
    assert kinds == ["entry", "add", "reduce", "flip", "exit"]


def test_entries_only_filters_to_entry_kind() -> None:
    df = pl.DataFrame([
        _trade(1, "M1", "Mk", "P1", "token1", "BUY", "SELL", 0.5, 50, 100, "h1"),
        _trade(2, "M1", "Mk", "P1", "token1", "BUY", "SELL", 0.5, 25,  50, "h2"),
    ])
    timeline = punter_position_timeline(df)
    e = entries_only(timeline)
    assert e.height == 1
    assert e["transactionHash"][0] == "h1"
