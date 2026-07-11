# Datalake Performance Design

## Goal

Increase V2 ingestion and processing throughput while bounding memory use,
without rewriting existing V1 or V2 Parquet data and without adding a paid
service dependency.

## Constraints

- Preserve `data/<source>/year=YYYY/month=MM/` as the compatible on-disk
  layout for existing readers.
- `orderFilled` remains read-only legacy data.
- Keep every filesystem operation cross-platform through `pathlib` and atomic
  replacement.
- Keep raw V2 logs auditable and derived tables reproducible.
- Use the existing Polars, PyArrow, and standard-library dependency set.
- Analysis-affecting changes require before/after results on the synthetic
  smoke fixture.

## Chosen Architecture

The existing raw sources remain append-only. Two compact dimensions avoid
repeated historical scans:

- `market_assets` maps an outcome token to its market and token side.
- `markets_current` stores the latest observed metadata per market.

`markets` and `missing_markets` remain snapshots for audit and backwards
compatibility. The V2 trade processor joins `order_filled_v2` to
`market_assets`; it does not rebuild the mapping by scanning V1 raw fills.

Large V2 partition transforms are written by a new lazy, atomic Parquet writer
instead of collecting the full output in a Python/Polars DataFrame. Cursors are
derived with small aggregate queries after a successful write.

Partition manifests under `data/_metadata/` list files and partition statistics.
They replace repeated recursive filesystem enumeration for managed partitions.
Compaction is due only when a hot partition exceeds configured file-count or
byte thresholds; closed partitions are compacted once.

## Data Flow

```text
Polygon V2 logs ──> order_filled_v2 ──> market_assets ──> trades
Gamma snapshots ──> markets/missing_markets ──> markets_current ─┘
                                               │
                                               └──> manifests + quality metrics
```

## Components

### Benchmarking

`poly-data benchmark-lake` runs a selected source operation against a synthetic
fixture and emits elapsed time, peak RSS, files touched, rows read/written, and
bytes read/written. It records the Polars optimized plan and whether the
streaming engine was selected.

### Dimensions

`market_assets` has one row per asset ID with `market_id`, `token_side`,
`first_seen_block`, and `last_seen_block`. It is updated whenever market
metadata is inserted or refreshed.

`markets_current` has one row per market ID and is rebuilt from metadata
snapshots only when they change. Consumers that need current metadata use this
source; audit and historical uses retain the snapshot sources.

### Lazy partition writer

`ParquetStore.write_lazy_partition()` receives one partition target and a
`LazyFrame`. It writes a temporary Parquet file with `sink_parquet`, atomically
renames it to a `run-*.parquet` filename, and updates its manifest only after
the final file exists. No cursor advances before this sequence succeeds.

### Manifests and compaction

Each managed partition manifest includes relative file names, byte sizes, row
count, min/max timestamp, optional min/max block number, schema fingerprint,
and compaction state. `scan()` uses the manifest file list when valid and falls
back to filesystem discovery if the manifest is absent.

The first configuration uses `max_run_files=16` and `max_partition_bytes=512
MiB`. These are defaults, exposed through configuration and validated against
benchmark results before any tuning change.

## Migration and Compatibility

No existing Parquet file is moved or rewritten as part of the rollout.
Dimensions and manifests are backfilled by explicit CLI commands. Existing
readers continue to use `markets`, `missing_markets`, `orderFilled`,
`order_filled_v2`, and `trades`. The V2 processor falls back to the existing
metadata scan only until `market_assets` has been backfilled.

## Verification

Each implementation task follows red-green-refactor. The test suite verifies
idempotency, atomic failure behavior, and cursor safety. The synthetic fixture
is processed before and after each analysis-affecting change; results report
wall time, peak RSS, and row counts. The complete Python test suite, Ruff, and
Ponder TypeScript typecheck must pass before publishing.
