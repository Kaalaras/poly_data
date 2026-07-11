Partitioned Parquet pipeline for fetching, processing, and analyzing
Polymarket trading data. Cross-platform (Linux + Windows + macOS).

- `markets` — Polymarket markets metadata
- `market_refreshes` — append-only snapshots for updated market metadata
- `missing_markets` — markets discovered while processing trades
- `markets_current` — latest validated snapshot for each market
- `market_assets` — compact `asset -> market/côté` dimension for V2 joins
- `orderFilled` — legacy local V1 order-filled events
- `order_filled_v2` — canonical Polymarket V2 order-filled events
- `trades` — V1-style normalized maker-fill trades

All data lives under `data/<source>/year=YYYY/month=MM/{run-*.parquet,month.parquet}`.
Partition manifests live under `data/_metadata/<source>/year=YYYY/month=MM.json`.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh         # macOS / Linux
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"  # Windows
uv sync --extra dev
```

```bash
uv run poly-data benchmark-polygon-rpc  # check free Polygon RPC log limits
uv run poly-data download-v2-logs       # download narrow Polymarket V2 logs
uv run poly-data import-ponder-v2        # import Ponder JSONL into Parquet
uv run poly-data update-all              # markets + canonical V2 RPC logs + process
uv run poly-data compact                 # nightly: dedup + compact month dirs
uv run poly-data compact --due           # compact only partitions above thresholds
uv run poly-data refresh-market-dimensions
uv run poly-data benchmark-lake --source order_filled_v2
uv run poly-data push-hf --repo USER/REPO  # publish snapshot to HF Hub
```

| Subcommand        | Purpose                                               |
|-------------------|-------------------------------------------------------|
| `update-markets`  | Fetch Polymarket markets API                          |
| `benchmark-polygon-rpc` | Benchmark RPC endpoints for V2 event logs       |
| `download-v2-logs`| Download V2 event logs directly from Polygon RPC      |
| `import-ponder-v2`| Import Ponder V2 JSONL into `order_filled_v2`         |
| `refresh-market-dimensions` | Materialize `markets_current` and `market_assets` |
| `process`         | Derive `trades` from local V1/V2 raw fill sources     |
| `v2-status`       | Summarize raw V2, derived trades, and API freshness   |
| `compact`         | Rewrite month partitions; `--due` uses manifest thresholds |
| `benchmark-lake`  | Measure a local source scan, file count, bytes, and RSS |
| `push-hf`         | Upload snapshot to HuggingFace Hub                    |
| `update-all`      | Canonical flow: markets -> V2 RPC download -> import -> discover -> process |

```bash
uv run python scripts/migrate_csv_to_parquet.py
```

This reads existing `orderFilled.csv`, `markets.csv`, `missing_markets.csv`,
and `processed/trades.csv` and writes them into `data/<source>/`. Legacy CSVs
are not deleted; remove them when you've verified.

```python
import polars as pl

trades = pl.scan_parquet("data/trades/**/*.parquet", hive_partitioning=True)
big_trades = trades.filter(pl.col("usd_amount") > 10_000).collect()
```

For ad-hoc SQL:

```python
import duckdb

duckdb.sql("SELECT count(*) FROM 'data/orderFilled/**/*.parquet'").show()
```

| Var          | Purpose                                              |
|--------------|------------------------------------------------------|
| `HF_TOKEN`   | HuggingFace Hub auth token (for `push-hf`)           |
| `POLYGON_RPC_URL` | Polygon RPC for direct V2 log download/benchmark |
| `POLYMARKET_V2_JSONL_PATH` | Ponder JSONL output/import path          |

See `docs/polymarket_v2.md` for the canonical V2 data pipeline. Ponder is kept
for bounded validation only; it is not used by `update-all`. V1 support is
read-only local legacy processing from existing `orderFilled` Parquet.

MIT. See `LICENSE`.
