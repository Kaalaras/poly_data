# Datalake Performance Baseline

## Reproducible command

```powershell
uv run python scripts/make_synthetic_smoke_fixture.py
uv run poly-data refresh-market-dimensions --data-root data_smoke
uv run poly-data benchmark-lake --data-root data_smoke --source orderFilled
uv run poly-data benchmark-lake --data-root data_smoke --source trades
uv run poly-data benchmark-lake --data-root data_smoke --source market_assets
```

The fixture is deterministic (`SEED=1234`) and contains 60 binary markets, 200
players, 35 days, 27,847 legacy fills, and 27,847 normalized trades. Refreshing
the dimensions produces 120 `market_assets` rows and 60 `markets_current` rows.

## Local measurements (2026-07-11)

| Source | Rows | Files | Bytes | Scan seconds | Peak RSS MiB |
|---|---:|---:|---:|---:|---:|
| `orderFilled` before manifests | 27,847 | 2 | 535,401 | 0.0096 | 9.34 |
| `orderFilled` final | 27,847 | 2 | 535,400 | 0.0075 | 11.38 |
| `trades` final | 27,847 | 2 | 815,259 | 0.0193 | 12.11 |
| `market_assets` final | 120 | 1 | 2,023 | 0.0052 | 10.90 |

These are single-machine scan measurements, not comparative throughput claims:
wall time and RSS vary across runs and depend on the filesystem cache. They
verify row and file counts, while production sizing should use
`poly-data benchmark-lake` against the target source.

## Operational acceptance checklist

- [x] Daily `update-all` does not select or process `orderFilled`.
- [x] V2 derived trades use the lazy `sink_partition` writer.
- [x] Legacy `poly-data process --source v1` remains available.
- [x] A partition without a manifest remains readable through direct discovery.
- [x] `poly-data compact --due` only targets partitions over 16 run files or
  512 MiB of run data.
