# Datalake Quality Roadmap

## Scope

`poly_data` is an event prediction data platform. The near-term scope is not
stock-market or trading automation. The platform should ingest event data,
clean it, expose reproducible datasets, train prediction models, and produce a
final operational decision for each event: bet or pass.

## Current Foundation

The repository already has a local, partitioned Parquet lake:

```text
data/<source>/year=YYYY/month=MM/*.parquet
```

Core sources:

- `markets`: Polymarket market metadata.
- `missing_markets`: market metadata discovered while processing trades.
- `markets_current`: materialized latest market snapshot.
- `market_assets`: materialized V2 asset-to-market dimension.
- `orderFilled`: legacy V1 raw fills, read-only.
- `order_filled_v2`: canonical raw V2 fill events.
- `trades`: normalized maker-fill trades.
- `ml`: derived training datasets.

Existing pipeline stages:

- Ingest V2 logs through Polygon RPC or Ponder-shaped JSONL.
- Import raw V2 rows into `order_filled_v2`.
- Discover missing market metadata from token ids.
- Process raw fills into normalized `trades`.
- Compact monthly partitions with source-specific deduplication keys.
- Maintain partition manifests and compact only partitions above operational
  file-count or byte thresholds.
- Publish dataset snapshots to Hugging Face Hub.
- Build analysis datasets from expert/player activity windows.

Existing quality controls:

- JSON schema-like contracts for `markets`, `order_filled_v2`, and `trades`.
- Validation on V2 import.
- Deduplication during import and monthly compaction.
- Cursor files for resumable ingestion and processing.
- Atomic partition manifests with row counts, bytes, timestamp bounds, and a
  schema hash; manifest-absent legacy partitions remain readable.
- Unit tests covering store layout, contracts, V2 import, trade processing, ML
  dataset generation, and dataloader behavior.

## Target Lake Design

Use explicit data layers:

```text
bronze  -> raw, append-only, auditable source data
silver  -> validated, normalized, deduplicated analytical tables
gold    -> feature sets, labels, model outputs, decision logs
```

Recommended mapping:

- Bronze:
  - raw RPC/Ponder JSONL logs.
  - raw `orderFilled`.
  - raw `order_filled_v2`.
  - raw market API snapshots.
- Silver:
  - `markets`.
  - `missing_markets`.
  - `trades`.
  - future `market_outcomes`.
  - future `expert_profiles`.
- Gold:
  - `ml` training panels.
  - future calibrated probability tables.
  - future `event_decisions` with `BET` or `PASS`.

## Quality Gates

Every production pipeline step should eventually emit a small quality report.
Minimum checks:

- Schema: required columns, dtypes, null policy.
- Ranges: timestamps positive, prices in `[0, 1]`, amounts positive.
- Uniqueness: primary key duplicates by source.
- Referential integrity: trade market ids and V2 assets resolve to known market
  metadata before entering silver.
- Completeness: block ranges and monthly partitions have no unexpected gaps.
- Freshness: latest source timestamp and latest block are recorded.
- Leakage control: model features must be computed strictly before
  `decision_ts`; labels must only use post-decision resolution data.
- Drift: per-month row counts, category distribution, price distribution,
  active expert count, target class balance.
- Reproducibility: each derived dataset records source snapshot ids, feature
  version, split date, model target, and code version.

## Open Source Accessibility

The open-source distribution should be usable without private services for read
and analysis workflows.

Required surface:

- Local-first Parquet layout readable by Polars and DuckDB.
- CLI commands for ingestion, compaction, validation, and snapshot publishing.
- Dataset cards for public Hugging Face releases.
- Machine-readable schemas and manifests committed with the repo.
- Small synthetic fixtures for tests and examples.
- Clear separation between public data, local caches, and secret-bearing config.

Do not publish:

- API keys, RPC URLs with tokens, local cursor internals that expose secrets, or
  non-public derived research notes.

## Modeling Path

Baseline sequence:

1. Build expert profiles from historical resolved events.
2. Build event-level panels at `decision_ts` from expert behavior and market
   state.
3. Train simple baselines first: majority class, logistic regression, random
   forest.
4. Add gradient boosting models after the feature contract is stable.
5. Calibrate probabilities with temporal validation.
6. Evaluate with log loss, Brier score, calibration curves, target recall, and
   simulated `BET`/`PASS` policy metrics.

The first operational model should answer:

```text
Given an event and data available at decision time, should we bet or pass?
```

## Next Implementation Steps

1. Add a `poly-data validate` command that validates selected sources and emits
   JSON quality reports.
2. Extend partition manifests with source snapshot and code-version provenance.
3. Add explicit bronze/silver/gold source naming or document the current source
   mapping until a migration is needed.
4. Add a `market_outcomes` silver table so labels no longer depend only on the
   last-fill price proxy.
5. Version the ML feature contract and write gold dataset manifests next to each
   training split.
6. Add a public dataset card template for Hugging Face snapshots.
