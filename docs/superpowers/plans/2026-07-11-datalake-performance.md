# Datalake Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the daily V2 pipeline independent of historical V1 scans and bound the memory required to derive V2 trades.

**Architecture:** Keep all existing sources readable. Materialize small `market_assets` and `markets_current` dimensions from market snapshots, then use `market_assets` for V2 joins. Write transformed monthly V2 partitions with a lazy atomic sink. Add partition manifests after the dimensions and writer have established their file lifecycle.

**Tech Stack:** Python 3.10, Polars 1.x, PyArrow, psutil, pytest, JSON sidecars, pathlib.

## Global Constraints

- Preserve `data/<source>/year=YYYY/month=MM/` for every existing source.
- Never rewrite or delete existing `orderFilled` V1 files.
- Use `pathlib.Path` and atomic replacement for all filesystem writes.
- Keep raw V2 rows auditable; dimensions and trades are derived sources.
- Do not add a database, cloud service, or paid dependency.
- Run the synthetic smoke fixture before and after every analysis-affecting task and record wall time, RSS, and row counts.
- Run `uv run pytest tests -x`, `uv run ruff check src tests scripts`, and `npm run typecheck` in `indexers/ponder-polymarket-v2` before publishing.

---

### Task 1: Establish a reproducible lake benchmark

**Files:**
- Create: `src/poly_data/benchmark.py`
- Modify: `src/poly_data/cli.py`
- Create: `tests/test_benchmark.py`
- Modify: `tests/test_cli.py`
- Modify: `scripts/make_synthetic_smoke_fixture.py`

**Interfaces:**
- Produces `benchmark_source(store: ParquetStore, source: str) -> dict[str, object]`.
- Produces CLI `poly-data benchmark-lake --source order_filled_v2`.
- Output includes `seconds`, `peak_rss_mb`, `rows`, `files`, `bytes`, and `plan`.

- [ ] **Step 1: Write failing tests for a deterministic benchmark report.**

```python
def test_benchmark_source_reports_scan_metrics(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    report = benchmark_source(store, "order_filled_v2")
    assert report["rows"] == 2
    assert report["files"] == 1
    assert report["bytes"] > 0
    assert report["seconds"] >= 0
    assert isinstance(report["plan"], str)
```

- [ ] **Step 2: Run the targeted test and confirm it fails because `benchmark_source` is absent.**

Run: `uv run pytest tests/test_benchmark.py::test_benchmark_source_reports_scan_metrics -v`

- [ ] **Step 3: Implement `benchmark_source`.**

```python
def benchmark_source(store: ParquetStore, source: str) -> dict[str, object]:
    files = store.partition_files(source)
    lf = store.scan(source)
    bench = Bench()
    with bench(source, "streaming") as measurement:
        measurement["rows_out"] = lf.select(pl.len()).collect(engine="streaming").item()
    result = bench.df().to_dicts()[0]
    return {**result, "files": len(files), "bytes": sum(p.stat().st_size for p in files), "plan": lf.explain()}
```

Implement the actual context manager with one `Bench` instance so the report uses its recorded result. Add the CLI subcommand and JSON output.

- [ ] **Step 4: Run targeted tests and the synthetic fixture benchmark.**

Run: `uv run pytest tests/test_benchmark.py tests/test_cli.py -x`

Run: `uv run python scripts/make_synthetic_smoke_fixture.py; uv run poly-data benchmark-lake --data-root data_smoke --source orderFilled`

- [ ] **Step 5: Commit.**

```text
perf: add reproducible lake benchmark
```

### Task 2: Materialize market dimensions and remove daily V1 discovery scans

**Files:**
- Create: `src/poly_data/dimensions.py`
- Create: `src/poly_data/contracts/schema_market_assets.json`
- Create: `src/poly_data/contracts/schema_markets_current.json`
- Modify: `src/poly_data/contracts/__init__.py`
- Modify: `src/poly_data/cli.py`
- Modify: `src/poly_data/process/trades.py`
- Create: `tests/test_dimensions.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_trades_process.py`

**Interfaces:**
- Produces `refresh_market_dimensions(store: ParquetStore) -> dict[str, int]`.
- Produces `ParquetStore.replace_source(source: str, frame: pl.LazyFrame) -> int`.
- `market_assets` columns: `asset`, `market_id`, `token_side`, `timestamp`.
- `markets_current` keeps one latest row per `id` with `timestamp=observed_at`.
- Produces CLI `poly-data refresh-market-dimensions`.

- [ ] **Step 1: Write failing dimension and V2-join tests.**

```python
def test_refresh_market_dimensions_writes_one_row_per_token(tmp_path: Path) -> None:
    store = _store_with_market_snapshots(tmp_path)
    assert refresh_market_dimensions(store) == {"market_assets": 2, "markets_current": 1}
    assert store.scan("market_assets").select("asset").collect().height == 2

def test_process_v2_uses_materialized_asset_dimension(tmp_path: Path) -> None:
    store = _store_with_v2_fill_and_market_assets(tmp_path)
    assert process_trades_v2(store) == 1
```

- [ ] **Step 2: Run the targeted tests and confirm they fail.**

Run: `uv run pytest tests/test_dimensions.py tests/test_trades_process.py -x`

- [ ] **Step 3: Implement the compact dimensions.**

```python
def refresh_market_dimensions(store: ParquetStore) -> dict[str, int]:
    current = store.scan_markets_all().unique(subset=["id"], keep="last")
    assets = current.select(["id", "token1", "token2", "timestamp"]).unpivot(
        index=["id", "timestamp"], on=["token1", "token2"],
        variable_name="token_side", value_name="asset",
    ).rename({"id": "market_id"})
    store.replace_source("market_assets", assets)
    store.replace_source("markets_current", current)
```

`replace_source` writes a new temporary source directory and swaps it only after
its Parquet output and schema checks succeed; it returns the replacement row
count. Update V2 processing to load `market_assets` if present, otherwise retain
the current `scan_markets_all()` fallback. Make
`update-all` refresh dimensions, discover only V2 assets, and process only V2;
the explicit `process --source v1` path remains available for legacy work.

- [ ] **Step 4: Run targeted tests and record before/after benchmark data.**

Run: `uv run pytest tests/test_dimensions.py tests/test_cli.py tests/test_trades_process.py -x`

Run: `uv run poly-data benchmark-lake --data-root data_smoke --source market_assets`

- [ ] **Step 5: Commit.**

```text
perf: materialize market dimensions for V2 processing
```

### Task 3: Stream V2 derived-trade writes to Parquet

**Files:**
- Modify: `src/poly_data/io/parquet_store.py`
- Modify: `src/poly_data/process/trades.py`
- Modify: `tests/test_parquet_store.py`
- Modify: `tests/test_trades_process.py`

**Interfaces:**
- Produces `ParquetStore.sink_partition(source, year, month, frame) -> Path`.
- `frame` is a `pl.LazyFrame`; the method writes one atomically published
  `run-*.parquet` in the supplied partition.
- `process_trades_v2` never calls `collect()` on the transformed trade rows.

- [ ] **Step 1: Write failing atomic-sink and no-materialization tests.**

```python
def test_sink_partition_writes_lazy_result_atomically(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    path = store.sink_partition("trades", 2026, 4, pl.DataFrame(_rows()).lazy())
    assert path.is_file()
    assert store.scan("trades", 2026, 4).collect().height == len(_rows())

def test_process_v2_uses_lazy_partition_sink(tmp_path: Path, mocker) -> None:
    sink = mocker.patch.object(ParquetStore, "sink_partition", wraps=ParquetStore.sink_partition)
    assert process_trades_v2(_seed_v2_store(tmp_path)) == 1
    assert sink.called
```

- [ ] **Step 2: Run targeted tests and confirm they fail.**

Run: `uv run pytest tests/test_parquet_store.py tests/test_trades_process.py -x`

- [ ] **Step 3: Implement lazy atomic writes.**

```python
def sink_partition(self, source: str, year: int, month: int, frame: pl.LazyFrame) -> Path:
    directory = self.root / source / f"year={year}" / f"month={month}"
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / self._run_filename()
    temporary = final.with_suffix(".parquet.tmp")
    frame.sink_parquet(temporary, compression="zstd")
    os.replace(temporary, final)
    return final
```

Keep the cursor-tail query separate and run it only after `sink_partition`
succeeds. Compute the returned row count from a lazy `len()` query, not from a
materialized transformed DataFrame.

- [ ] **Step 4: Run targeted tests and compare V2 benchmark RSS.**

Run: `uv run pytest tests/test_parquet_store.py tests/test_trades_process.py -x`

Run: `uv run poly-data benchmark-lake --data-root data_smoke --source trades`

- [ ] **Step 5: Commit.**

```text
perf: stream V2 trade partitions to parquet
```

### Task 4: Add manifests and compaction eligibility

**Files:**
- Create: `src/poly_data/io/manifests.py`
- Modify: `src/poly_data/io/parquet_store.py`
- Modify: `src/poly_data/compact/monthly.py`
- Modify: `src/poly_data/cli.py`
- Create: `tests/test_manifests.py`
- Modify: `tests/test_compact.py`

**Interfaces:**
- Produces `read_manifest(root, source, year, month) -> PartitionManifest | None`.
- Produces `write_manifest(...) -> Path` after append, sink, and compaction.
- Produces `partition_needs_compaction(manifest, max_run_files=16, max_bytes=536_870_912) -> bool`.
- Produces CLI `poly-data compact --due`.

- [ ] **Step 1: Write failing manifest lifecycle tests.**

```python
def test_append_writes_partition_manifest(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    store.append("trades", pl.DataFrame(_trade_rows()))
    manifest = read_manifest(store.root, "trades", 2026, 4)
    assert manifest.row_count == 2
    assert manifest.files[0].endswith(".parquet")

def test_due_compaction_skips_small_partition_and_compacts_many_runs(tmp_path: Path) -> None:
    store = _store_with_run_files(tmp_path, count=17)
    assert compact_due(store, "trades") == {"2026-4": 2}
```

- [ ] **Step 2: Run targeted tests and confirm they fail.**

Run: `uv run pytest tests/test_manifests.py tests/test_compact.py -x`

- [ ] **Step 3: Implement JSON manifests and due compaction.**

```python
@dataclass(frozen=True)
class PartitionManifest:
    files: list[str]
    row_count: int
    bytes: int
    min_timestamp: int | None
    max_timestamp: int | None
    schema_sha256: str
    compacted: bool
```

Write manifests using the existing atomic-write helper at
`data/_metadata/<source>/year=YYYY/month=MM.json`. `scan()` uses a valid
manifest file list and falls back to discovery for legacy partitions. `compact
--due` uses the stated thresholds; plain `compact` continues to compact all
selected partitions.

- [ ] **Step 4: Run targeted tests and the full quality suite.**

Run: `uv run pytest tests/test_manifests.py tests/test_compact.py tests/test_quality.py -x`

- [ ] **Step 5: Commit.**

```text
perf: manage parquet partitions with manifests
```

### Task 5: Document measured outcomes and verify compatibility

**Files:**
- Modify: `docs/datalake_quality_roadmap.md`
- Modify: `docs/polymarket_v2.md`
- Create: `docs/datalake_performance_baseline.md`
- Modify: `README.md`

**Interfaces:**
- `docs/datalake_performance_baseline.md` records the exact fixture command,
  machine-independent dataset shape, before/after elapsed time, peak RSS,
  rows, and output file counts.

- [ ] **Step 1: Write the documentation acceptance checklist.**

```markdown
- [ ] Daily V2 update does not scan `orderFilled`.
- [ ] V2 processing writes with `sink_partition`.
- [ ] Existing legacy `process --source v1` remains available.
- [ ] Manifest-absent legacy partitions remain readable.
```

- [ ] **Step 2: Execute the fixture and collect the reports.**

Run: `uv run python scripts/make_synthetic_smoke_fixture.py`

Run: `uv run poly-data benchmark-lake --data-root data_smoke --source orderFilled`

Run: `uv run poly-data benchmark-lake --data-root data_smoke --source trades`

- [ ] **Step 3: Update the roadmap and operational documentation with the measured values.**

Document the new dimensions, manifest location, compaction thresholds, and
legacy fallback behavior. Do not claim an improvement unless the benchmark
report contains the corresponding before/after values.

- [ ] **Step 4: Run complete verification.**

Run: `uv run pytest tests -x`

Run: `uv run ruff check src tests scripts`

Run: `npm run typecheck` from `indexers/ponder-polymarket-v2`

- [ ] **Step 5: Commit.**

```text
docs: document datalake performance results
```

## Plan Self-Review

- Spec coverage: Tasks 1–5 cover measurement, dimensions, lazy writes,
  manifests/compaction, and documented verification.
- Placeholders: no deferred implementation markers are present; each task has
  files, interfaces, a failing test, a command, and a commit boundary.
- Type consistency: `market_assets`, `markets_current`, `sink_partition`, and
  `PartitionManifest` use the same names in all dependent tasks.
