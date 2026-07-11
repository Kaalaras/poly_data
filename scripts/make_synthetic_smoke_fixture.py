"""Generate a synthetic data_smoke/ fixture for nb04 / nb05 smoke tests.

Used when no real `data/` directory is available. Writes orderFilled +
markets parquets large enough to exercise the ML pipeline (build_dataset
needs window_days+horizon_days+ of history and a panel of active players).
"""
from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from poly_data.io.parquet_store import ParquetStore
from poly_data.process.trades import process_trades


SEED = 1234
N_MARKETS = 60
N_PLAYERS = 200
N_TRADES_PER_DAY = 800
N_DAYS = 35
START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())


def main() -> None:
    rng = random.Random(SEED)
    root = Path(__file__).resolve().parents[1] / "data_smoke"
    if root.exists():
        # Wipe so reruns are deterministic.
        import shutil
        shutil.rmtree(root)
    store = ParquetStore(root)

    # Markets — split across 2 categories so notebook category-pick has signal.
    cats = ["Sports", "Politics"]
    market_rows = []
    for i in range(N_MARKETS):
        # Most close before final ts so target labels become YES/NO, not PASS.
        close_offset_days = rng.randint(20, 30)
        closed_ts = START + close_offset_days * 86400
        market_rows.append({
            "createdAt": "2025-01-01T00:00:00Z",
            "id": f"M{i:04d}",
            "question": f"market {i}",
            "answer1": "Yes",
            "answer2": "No",
            "neg_risk": False,
            "market_slug": f"m-{i}",
            "token1": f"t{i}a",
            "token2": f"t{i}b",
            "condition_id": f"c{i}",
            "volume": "0",
            "ticker": f"M{i:04d}",
            "closedTime": datetime.fromtimestamp(closed_ts, tz=timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timestamp": START,
            "category": cats[i % len(cats)],
        })
    store.append("markets", pl.DataFrame(market_rows))

    # Per-market intended winner so we can drive prices to that side at the end.
    winners = {f"M{i:04d}": ("token1" if rng.random() < 0.55 else "token2")
               for i in range(N_MARKETS)}

    # Players — pick a "skilled" core that wins more often.
    skilled = {f"P{i:04d}" for i in range(40)}
    all_players = [f"P{i:04d}" for i in range(N_PLAYERS)]

    # OrderFilled events. id MUST sort lexicographically with timestamp for
    # process_trades resume semantics to work (we use timestamp + counter).
    of_rows: list[dict] = []
    eid = 0
    for day in range(N_DAYS):
        day_ts = START + day * 86400
        # Last 2 days: skew prices toward the market's intended winner so
        # last_price >= 0.98 → market_resolution emits a winner_token.
        finalize = day >= N_DAYS - 2
        for _ in range(N_TRADES_PER_DAY):
            mid = f"M{rng.randint(0, N_MARKETS - 1):04d}"
            ts = day_ts + rng.randint(0, 86399)
            buyer = rng.choice(all_players)
            seller = rng.choice(all_players)
            if buyer == seller:
                continue
            # Bias buyer to "skilled" + correct side near close.
            if finalize:
                side = winners[mid]
                buyer = rng.choice(list(skilled))
            else:
                side = "token1" if rng.random() < 0.55 else "token2"

            if finalize:
                price = rng.uniform(0.985, 0.999)
            else:
                price = rng.uniform(0.10, 0.90)

            tok_amount_units = rng.randint(20, 200)  # 20..200 outcome tokens
            usd_units = round(tok_amount_units * price, 6)

            # Side determines which clob token id is the non-USDC asset.
            if side == "token1":
                non_usdc_asset = f"t{int(mid[1:])}a"
            else:
                non_usdc_asset = f"t{int(mid[1:])}b"

            # In our synthetic flow buyer == taker, seller == maker, taker
            # pays USDC → takerAssetId = "0". Amounts in 6-decimal units.
            of_rows.append({
                "id": f"o{ts:010d}-{eid:08d}",
                "timestamp": ts,
                "maker": seller,
                "makerAssetId": non_usdc_asset,
                "makerAmountFilled": str(int(tok_amount_units * 10**6)),
                "taker": buyer,
                "takerAssetId": "0",
                "takerAmountFilled": str(int(usd_units * 10**6)),
                "transactionHash": f"0x{eid:064x}",
            })
            eid += 1

    of_df = pl.DataFrame(of_rows)
    store.append("orderFilled", of_df)
    print(f"wrote markets={len(market_rows)}, orderFilled={len(of_rows)}")

    # Derive trades.
    n = process_trades(store)
    print(f"derived trades: {n}")

    print(f"\nfixture ready at {root}")


if __name__ == "__main__":
    main()
