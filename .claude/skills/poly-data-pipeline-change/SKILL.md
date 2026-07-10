---
name: poly-data-pipeline-change
description: Use when changing Polymarket ingestion, orderFilled normalization, trade processing, Parquet storage, cursoring, compaction, partitioning, deduplication, or dataset publication in poly_data.
---

# Change the Poly data pipeline

Treat the current source and tests as authority. Trace the affected flow before
editing: `src/poly_data/ingest/` writes raw `orderFilled` events through
`src/poly_data/io/parquet_store.py`; `src/poly_data/process/trades.py` derives
trades; `src/poly_data/compact/monthly.py` deduplicates partitions; publication
is an optional final step in `src/poly_data/distribute/huggingface.py`.

Do not invent a contract directory, migration command, or validation CLI. Check
`src/poly_data/cli.py` for the commands that actually exist.

## Preserve pipeline invariants

- Keep raw event sources append-only and separate from derived `trades`.
- Interpret timestamps consistently as UTC Unix seconds and preserve the
  `year=YYYY/month=MM` hive layout.
- Keep event IDs stable. Incremental cursors must advance monotonically without
  dropping events that share a timestamp or replaying already processed rows.
- Make reruns idempotent. Deduplication belongs in the tested compaction path,
  keyed by `id`; never hand-edit Parquet files.
- Preserve atomic replacement of `month.parquet` and leave inputs recoverable
  if a write fails.

## Prove the change

Write the smallest failing test first. Exercise the relevant boundary with a
temporary `--data-root`: ingest/cursor behavior, transform schema and values,
partition placement, or compaction. Prefer generative or parametrized checks for
timestamp boundaries, repeated batches, duplicate IDs, empty inputs, mixed
historical dtypes, and randomized row order. Assert invariants rather than one
large golden file.

Then run the focused test modules for every touched stage and the CLI contract.
For a cross-stage change, run a fixture through ingest, process, scan, compact,
and rescan; compare row counts, IDs, partitions, and normalized values before
and after. Use an isolated data root and never mutate repository `data/**`.

`push-hf` is a public external effect. Test its adapter with mocks, and run the
real command only when the current user explicitly authorizes that exact
publication. Report any migration or backfill requirement separately instead of
silently rewriting existing data.
