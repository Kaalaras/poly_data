# V2 Notebooks Design

## Objective

Turn the example notebooks into a V2-only, beginner-friendly curriculum that
explains the Polymarket lake, its processing pipeline, its performance limits,
and the limits of naive prediction and copy-betting experiments.

The notebooks must not read the V1 `orderFilled` source or teach legacy
commands. They must be executable against the deterministic `data_smoke`
fixture by default and against a user-selected full lake only through
`POLY_DATA_ROOT`.

## Scope and Non-goals

In scope:

- Replace V1/raw-fill notebook paths with canonical V2-derived sources.
- Reorder and rewrite the notebooks as a progressive learning sequence.
- Make lake and engine performance measurements reproducible.
- Correct the temporal and execution-model flaws in the XGBoost and
  copy-betting demonstrations.
- Materialize official market outcomes before making outcome or PnL claims.
- Smoke-test every supported notebook on `data_smoke`.

Out of scope:

- Live trading, wallet execution, market-making, or investment advice.
- A claim that any model or copy-betting rule is profitable.
- A paid data source or hosted service.

## Canonical Data Contract

Every notebook uses these sources and labels them in its opening cell:

| Layer | Source | Purpose |
|---|---|---|
| Bronze | `order_filled_v2` | Auditable exchange event rows |
| Silver | `trades` | Normalized V2 maker-fill trade rows |
| Dimension | `markets_current` | Current market metadata and close time |
| Dimension | `market_assets` | `asset -> market_id/token_side` join |
| Dimension | `market_outcomes` | Official winner and resolution provenance |
| Metadata | `_metadata/` | Partition file, byte, row, and schema manifests |

`orderFilled`, V1 CSV migration, V1 token identifiers, and `python
update_all.py` references are removed from the supported curriculum.

## Official Outcome Dimension

`market_outcomes` is a small, append-only derived source normalized from the
official Polymarket market metadata. Its contract includes at least:

- `market_id`;
- `winner_token` (`token1` or `token2`);
- `resolved_at` in UTC seconds;
- `observed_at` in UTC seconds;
- the official resolution status and source provenance;
- `timestamp = resolved_at` for normal lake partitioning.

Only closed, unambiguous outcomes enter this table. The ingest rejects absent,
contradictory, or non-binary outcomes rather than inferring a winner from the
last traded price. `update-all` refreshes current market metadata, materializes
market dimensions, refreshes outcomes, then processes V2 events. Existing
terminal-price logic may remain only in a visibly labelled exploratory section;
it cannot support a held-out PnL claim.

The smoke fixture is rebuilt around V2: it seeds `order_filled_v2`, V2 market
dimensions, and official outcomes, then derives `trades` through the V2 path.
It must contain both YES and NO resolutions, unresolved markets, enough dates
for temporal folds, and deterministic expected metrics.

## Execution Modes

The shared setup cell exposes:

- `POLY_DATA_ROOT`: explicit lake root; defaults to `data_smoke` when present.
- `POLY_NOTEBOOK_MODE`: `smoke` by default, `full` only by explicit opt-in.
- `POLY_DATA_RSS_CAP_MB`: optional RSS assertion.

Each notebook prints the resolved root, source row counts, partition/file
counts, fixture/full mode, and Git revision when available. A missing source
shows the exact `poly-data` command needed to create it.

## Notebook Sequence

### 00 — V2 Lake Quickstart

Create the smoke fixture, display bronze/silver/dimension flow, show the
partition and manifest layout, and define maker, taker, fill, token, market,
resolution, and slippage.

### 01 — V2 Lake Discovery and Quality

Inspect schemas, V2 event provenance, V2-to-trade processing, market joins,
price/amount ranges, freshness, and manifest-backed source budgets. Explain
why a fill is not necessarily a user bet.

Include a dedicated **Official outcomes versus price proxies** section. It
shows how the normalized outcome record is produced, why a final trade price is
not proof of resolution, how an unresolved market is represented, and which
notebook conclusions are permissible with each source.

### 02 — Market and Wallet Analysis

Choose a V2 market or wallet with an explicit filter. Display market metadata,
positions, signed cash flows, and estimated mark-to-market separately. Never
label a last-price proxy as exact realized PnL; state the missing redemption,
transfer, and official-outcome information.

### 03 — Toy Backtest

Build transaction-time or explicitly reindexed bars from `trades`. Calculate a
signal at bar `t` and execute it at the next executable observation. Show a
toy SMA strategy with configurable costs and slippage, walk-forward split,
buy-and-hold comparator, turnover, exposure, and drawdown. It is an execution
mechanics lesson, not a trading recommendation.

### 04 — Lake and Query Performance

First measure the lake with `benchmark-lake`, including rows, Parquet files,
bytes, RSS, plan, manifests, and compaction state. Then compare Polars and
DuckDB on equivalent end-to-end and cached workloads. Repeat measurements and
report median and spread with hardware, library versions, thread limits, query
plans, result checksums, and source snapshot metadata.

### 05 — Probabilistic XGBoost Baseline

Use `market_outcomes` as the only held-out YES/NO target. Separate resolution
timing from YES/NO direction; do not use `PASS` as an economic outcome.
Terminal-price proxy cells, if retained for comparison, are explicitly
exploratory and excluded from model metrics and simulated PnL.

Use expanding or rolling temporal folds. Select the expert cohort within every
training fold only, freeze it for validation/test, and tune only inside the
training period. Compare XGBoost probabilities against market implied price,
majority-class, and simple linear/logistic baselines. Report log-loss, Brier
score, calibration, class support, and feature permutation importance.

Any illustrative decision rule is `p_model - p_market > threshold`, where the
threshold is fixed on training data. Its test report includes execution cost,
slippage sensitivity, and no profitability claim.

### 06 — Copy-betting Experiment

Treat copy-betting as a constrained historical simulation. Preserve BUY/SELL
direction, use a side-aware executable-price proxy, deterministic event order,
latency, missed fills, concurrent capital reservation, and settlement-time
cash realization. Compare leaders to a genuinely simulated random cohort over
rolling folds. Report drawdown, exposure, holding time, fill rate, sensitivity
to costs/latency, and confidence intervals grouped by market/date.

## Shared Engineering

- Introduce a small notebook helper module for configuration, data inventory,
  stable plotting, provenance, and safe output directories.
- Keep pedagogically useful transformations visible in notebooks; move only
  repetitive plumbing into helpers.
- Use `markets_current` and `market_assets`, never hand-built V1 token joins.
- Join outcome labels through `market_outcomes`, never from a last-price rule.
- Use stable event ordering: timestamp plus raw/order-filled identifier.
- Store generated ML artefacts with metadata: lake snapshot, source hashes,
  feature/label version, folds, seeds, parameters, and code revision.
- Keep notebooks and their builder scripts synchronized; a regeneration check
  must detect drift.

## Safety and Statistical Invariants

- No feature, player ranking, hyperparameter, or decision threshold may use a
  future test observation.
- A signal at time `t` cannot execute at a price observed before it is known.
- Official resolution is required for a production-like PnL claim.
- A raw fill, quoted last price, and executable price are distinct concepts.
- Every conclusion cell states whether it is descriptive, exploratory, or a
  held-out evaluation.

## Validation

- Regenerate and smoke-run all notebooks with `data_smoke` via `nbclient`.
- Validate the `market_outcomes` contract against valid, missing, ambiguous,
  and contradictory official metadata fixtures.
- Assert no notebook source or output references `orderFilled` or legacy
  update scripts.
- Assert notebook metadata reports its source and mode.
- Add focused tests for temporal cohort isolation, next-observation execution,
  long/short copy-bet accounting, random-cohort baseline, and benchmark result
  equality/checksum behavior.
- Run `uv run pytest tests -x`, `uv run ruff check src tests scripts`, and the
  Ponder TypeScript typecheck before publication.

## Delivery Order

1. Outcome ingest/contract and fully V2 smoke fixture.
2. Shared V2 notebook setup and quickstart/discovery notebooks.
3. Wallet analysis and toy backtest.
4. Performance benchmark and validation harness.
5. Leak-free XGBoost baseline.
6. Direction-aware copy-betting experiment and documentation.
