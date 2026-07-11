"""Generate a deterministic V2-only ``data_smoke/`` lake for the notebooks."""
from __future__ import annotations

import random
import shutil
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from poly_data.dimensions import refresh_market_dimensions
from poly_data.ingest.outcomes import refresh_market_outcomes
from poly_data.io.parquet_store import ParquetStore
from poly_data.process.trades import process_trades

SEED = 1234
N_MARKETS = 60
N_PLAYERS = 200
N_TRADES_PER_DAY = 800
N_DAYS = 35
START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())


def build_fixture(root: Path) -> ParquetStore:
    """Build a V2-only fixture and return its populated Parquet store."""
    rng = random.Random(SEED)
    if root.exists():
        shutil.rmtree(root)
    store = ParquetStore(root)

    winners = {
        f"M{i:04d}": ("token1" if rng.random() < 0.55 else "token2")
        for i in range(N_MARKETS)
    }
    resolved_markets = {f"M{i:04d}" for i in range(N_MARKETS) if i % 5 != 0}

    # Gamma-like market metadata.  A deterministic subset stays unresolved so
    # outcome-aware examples can demonstrate censoring rather than invent labels.
    categories = ["Sports", "Politics"]
    market_rows: list[dict[str, object]] = []
    for i in range(N_MARKETS):
        market_id = f"M{i:04d}"
        close_ts = START + rng.randint(20, 30) * 86400
        is_resolved = market_id in resolved_markets
        winner = winners[market_id]
        market_rows.append({
            "createdAt": "2025-01-01T00:00:00Z",
            "id": market_id,
            "question": f"market {i}",
            "answer1": "Yes",
            "answer2": "No",
            "neg_risk": False,
            "market_slug": f"m-{i}",
            "token1": f"t{i}a",
            "token2": f"t{i}b",
            "condition_id": f"c{i}",
            "volume": "0",
            "ticker": market_id,
            "closedTime": (
                datetime.fromtimestamp(close_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if is_resolved else ""
            ),
            "timestamp": START,
            "observed_at": close_ts if is_resolved else START + N_DAYS * 86400,
            "category": categories[i % len(categories)],
            "outcomePrices": (
                '["1", "0"]' if winner == "token1" else '["0", "1"]'
            ) if is_resolved else '["0.5", "0.5"]',
            "closed": is_resolved,
            "resolutionSource": "synthetic-official" if is_resolved else "",
            "umaResolutionStatus": "resolved" if is_resolved else "",
        })
    store.append("markets", pl.DataFrame(market_rows))
    refresh_market_dimensions(store)
    refresh_market_outcomes(store)

    skilled = [f"P{i:04d}" for i in range(40)]
    all_players = [f"P{i:04d}" for i in range(N_PLAYERS)]
    events: list[dict[str, object]] = []
    event_id = 0
    for day in range(N_DAYS):
        day_ts = START + day * 86400
        for _ in range(N_TRADES_PER_DAY):
            market_id = f"M{rng.randint(0, N_MARKETS - 1):04d}"
            timestamp = day_ts + rng.randint(0, 86399)
            buyer = rng.choice(all_players)
            seller = rng.choice(all_players)
            if buyer == seller:
                continue
            near_resolution = day >= N_DAYS - 2 and market_id in resolved_markets
            token_side = winners[market_id] if near_resolution else (
                "token1" if rng.random() < 0.55 else "token2"
            )
            if near_resolution:
                buyer = rng.choice(skilled)
                price = rng.uniform(0.985, 0.999)
            else:
                price = rng.uniform(0.10, 0.90)
            shares = float(rng.randint(20, 200))
            amount_usdc = round(shares * price, 6)
            asset = f"t{int(market_id[1:])}{'a' if token_side == 'token1' else 'b'}"
            events.append({
                "id": f"evt-{timestamp:010d}-{event_id:08d}",
                "timestamp": timestamp,
                "block_number": 70_000_000 + event_id,
                "block_timestamp": timestamp,
                "transaction_hash": f"0x{event_id:064x}",
                "user_id": seller,
                "asset": asset,
                "amount_usdc": amount_usdc,
                "amount_shares": shares,
                "price": price,
                "side": "SELL",
                "order_hash": f"0xorder{event_id:060x}",
                "counterparty_id": buyer,
                "order_type": "maker",
                "fee": 0.0,
                "builder": "",
            })
            event_id += 1

    store.append("order_filled_v2", pl.DataFrame(events))
    derived = process_trades(store, source="v2")
    if derived != len(events):
        raise RuntimeError(f"expected {len(events)} V2 trades, derived {derived}")
    return store


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "data_smoke"
    store = build_fixture(root)
    print(
        "fixture ready: "
        f"markets={store.scan('markets_current').collect().height}, "
        f"v2_events={store.scan('order_filled_v2').collect().height}, "
        f"trades={store.scan('trades').collect().height}, "
        f"outcomes={store.scan('market_outcomes').collect().height} at {root}"
    )


if __name__ == "__main__":
    main()
